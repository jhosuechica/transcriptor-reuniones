"""Grabación sincronizada del micrófono y del audio del sistema.

Cada pista recorre este camino:

    callback de audio -> cola -> hilo escritor -> AlineadorPista -> WAV

El callback solo copia datos: si hiciera trabajo pesado se perdería audio.
AlineadorPista coloca cada bloque en su instante real según un reloj común
(time.perf_counter), de modo que el segundo N de ambos WAV es el mismo momento.
"""

from __future__ import annotations

import json
import logging
import math
import queue
import re
import threading
import time
import wave
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np
import pyaudiowpatch as pyaudio
import soxr

from config import FRECUENCIA, Config

log = logging.getLogger(__name__)

SILENCIO_DBFS = -120.0


class ErrorGrabacion(Exception):
    """Error de grabación con un mensaje apto para mostrar al usuario."""


# --------------------------------------------------------------------------
# Dispositivos
# --------------------------------------------------------------------------


def listar_dispositivos(pa: pyaudio.PyAudio) -> tuple[list[dict], list[dict]]:
    """Devuelve (micrófonos, loopbacks) de WASAPI."""
    try:
        wasapi = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
    except OSError as e:
        raise ErrorGrabacion("WASAPI no está disponible en este equipo.") from e
    micros, loopbacks = [], []
    for i in range(pa.get_device_count()):
        d = pa.get_device_info_by_index(i)
        if d["hostApi"] != wasapi["index"] or d["maxInputChannels"] < 1:
            continue
        (loopbacks if d.get("isLoopbackDevice") else micros).append(d)
    return micros, loopbacks


def _por_nombre(dispositivos: list[dict], texto: str) -> dict | None:
    texto = texto.strip().lower()
    return next((d for d in dispositivos if texto in d["name"].lower()), None)


def elegir_microfono(pa: pyaudio.PyAudio, nombre: str) -> tuple[dict, str | None]:
    """Devuelve (micrófono, aviso). El aviso indica que se usó otro distinto."""
    micros, _ = listar_dispositivos(pa)
    if not micros:
        raise ErrorGrabacion(
            "No se encontró ningún micrófono. Conéctalo y revisa Configuración > "
            "Privacidad y seguridad > Micrófono."
        )
    if nombre.strip():
        d = _por_nombre(micros, nombre)
        if d:
            return d, None
    indice = pa.get_host_api_info_by_type(pyaudio.paWASAPI)["defaultInputDevice"]
    d = next((m for m in micros if m["index"] == indice), micros[0])
    aviso = f"No se encontró el micrófono '{nombre}'; se usa '{d['name']}'." if nombre.strip() else None
    return d, aviso


def elegir_loopback(pa: pyaudio.PyAudio, nombre: str) -> tuple[dict, str | None]:
    """Devuelve (loopback, aviso) de la salida pedida o de la predeterminada."""
    _, loopbacks = listar_dispositivos(pa)
    if not loopbacks:
        raise ErrorGrabacion("No se encontró ninguna salida de audio que grabar.")
    if nombre.strip():
        d = _por_nombre(loopbacks, nombre)
        if d:
            return d, None
    try:
        d = pa.get_default_wasapi_loopback()
    except (OSError, LookupError) as e:
        raise ErrorGrabacion("Windows no tiene una salida de audio predeterminada.") from e
    aviso = f"No se encontró la salida '{nombre}'; se usa '{d['name']}'." if nombre.strip() else None
    return d, aviso


def _salida_de_loopback(pa: pyaudio.PyAudio, loopback: dict) -> dict | None:
    """Busca el dispositivo de reproducción al que pertenece un loopback."""
    nombre = loopback["name"].removesuffix(" [Loopback]")
    for i in range(pa.get_device_count()):
        d = pa.get_device_info_by_index(i)
        if (
            d["hostApi"] == loopback["hostApi"]
            and d["maxOutputChannels"] > 0
            and d["name"] == nombre
        ):
            return d
    return None


# --------------------------------------------------------------------------
# Sincronización
# --------------------------------------------------------------------------


class AlineadorPista:
    """Coloca bloques de audio en una línea de tiempo común.

    Corrige dos problemas:
    - Huecos: el dispositivo deja de entregar audio. Se rellenan con silencio.
    - Deriva: el reloj de la tarjeta no va exactamente igual que el del PC.
      Se corrige estirando o encogiendo los bloques un 0,2 % como máximo.

    Un bloque puede llegar tarde solo porque el PC estaba ocupado. Por eso nunca
    se decide con un único bloque: se usa el desfase mínimo de varios, ya que un
    retraso puntual solo puede aumentar el desfase, nunca reducirlo.
    """

    UMBRAL_HUECO = 0.25      # s de desfase a partir de los que se sospecha un hueco
    ESPERA = 0.5             # s de observación antes de confirmarlo
    VENTANA_DERIVA = 10.0    # s entre ajustes de deriva
    TOLERANCIA = 0.010       # s de desfase que se dejan sin corregir
    ESTIRADO_MAX = 0.002     # proporción máxima de estirado por bloque
    BLOQUE_SILENCIO = FRECUENCIA * 10

    def __init__(self, frecuencia: int = FRECUENCIA):
        self.frecuencia = frecuencia
        self.escritas = 0              # muestras ya emitidas
        self.silencio_insertado = 0    # muestras de relleno tras el inicio
        self.huecos = 0
        self.deriva_corregida = 0      # muestras añadidas (+) o quitadas (-)
        self._pendientes: list[tuple[np.ndarray, float]] = []
        self._iniciada = False
        self._ajuste = 0               # muestras de deriva por corregir
        self._min_ventana = math.inf
        self._inicio_ventana = 0.0

    def agregar(self, datos: np.ndarray, t_fin: float) -> list[np.ndarray]:
        """Añade un bloque que terminó de sonar en t_fin (s desde el inicio)."""
        if len(datos) == 0:
            return []
        if self._pendientes or not self._iniciada:
            self._pendientes.append((datos, t_fin))
            return self._resolver_pendientes(forzar=False)
        desfase = t_fin * self.frecuencia - (self.escritas + len(datos))
        if desfase > self.UMBRAL_HUECO * self.frecuencia:
            self._pendientes.append((datos, t_fin))
            return []
        return [self._emitir(datos, t_fin)]

    def finalizar(self, t_fin: float) -> list[np.ndarray]:
        """Emite lo pendiente y rellena con silencio hasta t_fin."""
        salida = self._resolver_pendientes(forzar=True) if self._pendientes else []
        falta = round(t_fin * self.frecuencia) - self.escritas
        if falta > 0:
            salida += self._silencio(falta)
        return salida

    def _resolver_pendientes(self, forzar: bool) -> list[np.ndarray]:
        acumuladas = self.escritas
        minimo = math.inf
        for datos, t in self._pendientes:
            acumuladas += len(datos)
            minimo = min(minimo, t * self.frecuencia - acumuladas)

        umbral = self.UMBRAL_HUECO * self.frecuencia
        esperado = self._pendientes[-1][1] - self._pendientes[0][1]
        era_retraso = self._iniciada and minimo <= umbral
        if not (forzar or era_retraso or esperado >= self.ESPERA):
            return []

        salida: list[np.ndarray] = []
        relleno = max(0, round(minimo))
        if not self._iniciada:
            # Silencio inicial: los dos dispositivos no arrancan a la vez.
            self._iniciada = True
            salida += self._silencio(relleno)
        elif minimo > umbral:
            self.huecos += 1
            self.silencio_insertado += relleno
            salida += self._silencio(relleno)
            # La línea de tiempo acaba de realinearse: se descarta lo anterior.
            self._ajuste = 0
            self._min_ventana = math.inf
            self._inicio_ventana = self._pendientes[0][1]
        pendientes, self._pendientes = self._pendientes, []
        salida += [self._emitir(d, t) for d, t in pendientes]
        return salida

    def _emitir(self, datos: np.ndarray, t_fin: float) -> np.ndarray:
        desfase = t_fin * self.frecuencia - (self.escritas + len(datos))
        self._min_ventana = min(self._min_ventana, desfase)
        if t_fin - self._inicio_ventana >= self.VENTANA_DERIVA:
            if abs(self._min_ventana) > self.TOLERANCIA * self.frecuencia:
                self._ajuste = round(self._min_ventana)
            else:
                self._ajuste = 0
            self._min_ventana = math.inf
            self._inicio_ventana = t_fin
        if self._ajuste:
            datos = self._estirar(datos)
        self.escritas += len(datos)
        return datos

    def _estirar(self, datos: np.ndarray) -> np.ndarray:
        n = len(datos)
        limite = max(1, int(n * self.ESTIRADO_MAX))
        delta = max(-limite, min(limite, self._ajuste))
        if n + delta < 2 or n < 2:
            return datos
        posiciones = np.linspace(0, n - 1, n + delta)
        self._ajuste -= delta
        self.deriva_corregida += delta
        return np.interp(posiciones, np.arange(n), datos).astype(np.int16)

    def _silencio(self, n: int) -> list[np.ndarray]:
        """Silencio en bloques, para no reservar mucha memoria de una vez."""
        self.escritas += n
        bloques = []
        while n > 0:
            k = min(n, self.BLOQUE_SILENCIO)
            bloques.append(np.zeros(k, dtype=np.int16))
            n -= k
        return bloques


# --------------------------------------------------------------------------
# Pistas
# --------------------------------------------------------------------------


def a_mono(datos: bytes, canales: int) -> np.ndarray:
    muestras = np.frombuffer(datos, dtype=np.int16)
    if canales == 1:
        return muestras
    # En sonido envolvente bastan los canales frontales (la voz va al centro).
    frontales = muestras.reshape(-1, canales)[:, : min(canales, 3)]
    return frontales.mean(axis=1).astype(np.int16)


def dbfs(valor: float) -> float:
    """Convierte una amplitud de 0 a 32768 en dBFS."""
    return 20 * math.log10(valor / 32768) if valor > 0 else SILENCIO_DBFS


@dataclass
class EstadoPista:
    """Datos para el medidor de nivel y para el resumen final."""

    pico_dbfs: float = SILENCIO_DBFS       # nivel actual
    pico_max_dbfs: float = SILENCIO_DBFS   # máximo de toda la grabación
    segundos: float = 0.0
    bloques: int = 0
    desbordamientos: int = 0
    error: str | None = None


class Pista:
    """Captura un dispositivo y lo escribe en un WAV mono de 48 kHz."""

    ESPERA_COLA = 0.2      # s
    INTERVALO_FLUSH = 2.0  # s

    def __init__(self, pa: pyaudio.PyAudio, dispositivo: dict, ruta: Path, nombre: str):
        self.nombre = nombre
        self.dispositivo = dispositivo
        self.ruta = ruta
        self.estado = EstadoPista()
        self.alineador = AlineadorPista()
        self._pa = pa
        self._cola: queue.SimpleQueue = queue.SimpleQueue()
        self._stream = None
        self._hilo: threading.Thread | None = None
        self._t0 = 0.0
        self._t_fin: float | None = None
        self._detener = threading.Event()
        self._archivo = None
        self._wav: wave.Wave_write | None = None
        self._frecuencia = int(dispositivo["defaultSampleRate"])
        self._canales = int(dispositivo["maxInputChannels"])
        self._remuestreo = (
            soxr.ResampleStream(self._frecuencia, FRECUENCIA, 1, dtype="int16")
            if self._frecuencia != FRECUENCIA
            else None
        )

    def abrir(self) -> None:
        try:
            self._stream = self._pa.open(
                format=pyaudio.paInt16,
                channels=self._canales,
                rate=self._frecuencia,
                input=True,
                input_device_index=self.dispositivo["index"],
                frames_per_buffer=self._frecuencia // 50,  # bloques de 20 ms
                stream_callback=self._al_recibir,
                start=False,
            )
        except (OSError, ValueError) as e:
            raise ErrorGrabacion(
                f"No se pudo abrir '{self.dispositivo['name']}' ({self.nombre}). Puede estar "
                f"desconectado, en uso exclusivo o sin permiso. Detalle: {e}"
            ) from e

    def iniciar(self, t0: float) -> None:
        self._t0 = t0
        try:
            self._archivo = open(self.ruta, "wb")
            self._wav = wave.open(self._archivo, "wb")
            self._wav.setnchannels(1)
            self._wav.setsampwidth(2)
            self._wav.setframerate(FRECUENCIA)
        except OSError as e:
            raise ErrorGrabacion(f"No se pudo crear {self.ruta}: {e}") from e
        self._hilo = threading.Thread(
            target=self._escribir_bucle, name=f"escritor-{self.nombre}", daemon=True
        )
        self._hilo.start()
        try:
            self._stream.start_stream()
        except OSError as e:
            raise ErrorGrabacion(f"No se pudo iniciar '{self.dispositivo['name']}': {e}") from e

    def activa(self) -> bool:
        try:
            return self._stream is not None and self._stream.is_active()
        except OSError:
            return False

    def cerrar_stream(self) -> None:
        if self._stream is not None:
            try:
                self._stream.close()
            except OSError:
                log.exception("Error al cerrar el dispositivo de %s", self.nombre)
            self._stream = None

    def detener(self, t_fin: float) -> None:
        """Deja de capturar y espera a que se escriba todo lo pendiente."""
        self.cerrar_stream()
        self._t_fin = t_fin
        self._detener.set()
        if self._hilo is not None and self._hilo.is_alive():
            self._hilo.join()
        self._cerrar_wav()

    def _al_recibir(self, datos, _n, _tiempos, banderas):
        # Se ejecuta en el hilo de audio: solo anotar la hora y encolar.
        self._cola.put((datos, time.perf_counter(), banderas))
        return None, pyaudio.paContinue

    def _escribir_bucle(self) -> None:
        ultimo_flush = time.monotonic()
        try:
            while True:
                try:
                    datos, t, banderas = self._cola.get(timeout=self.ESPERA_COLA)
                except queue.Empty:
                    self.estado.pico_dbfs = SILENCIO_DBFS
                    if self._detener.is_set():
                        break
                    continue
                self._procesar(datos, t - self._t0, banderas)
                if time.monotonic() - ultimo_flush > self.INTERVALO_FLUSH:
                    self._archivo.flush()
                    ultimo_flush = time.monotonic()
            if self._remuestreo is not None:
                # Recupera las últimas muestras retenidas por el remuestreador.
                resto = self._remuestreo.resample_chunk(np.zeros(0, np.int16), last=True)
                self._guardar(self.alineador.agregar(resto, self._t_fin))
            self._guardar(self.alineador.finalizar(self._t_fin))
        except Exception as e:  # el hilo no puede morir sin avisar
            log.exception("Fallo al escribir la pista %s", self.nombre)
            self.estado.error = f"No se pudo seguir escribiendo {self.ruta.name}: {e}"

    def _procesar(self, datos: bytes, t: float, banderas: int) -> None:
        if banderas & pyaudio.paInputOverflow:
            self.estado.desbordamientos += 1
        mono = a_mono(datos, self._canales)
        if len(mono):
            pico = dbfs(float(np.abs(mono.astype(np.int32)).max()))
            self.estado.pico_dbfs = pico
            self.estado.pico_max_dbfs = max(self.estado.pico_max_dbfs, pico)
        if self._remuestreo is not None:
            mono = self._remuestreo.resample_chunk(mono)
        self.estado.bloques += 1
        self._guardar(self.alineador.agregar(mono, t))

    def _guardar(self, bloques: list[np.ndarray]) -> None:
        for b in bloques:
            # writeframes actualiza la cabecera en cada llamada: si el programa
            # se cierra de golpe, el WAV sigue siendo válido hasta ese punto.
            self._wav.writeframes(b.tobytes())
        self.estado.segundos = self.alineador.escritas / FRECUENCIA

    def _cerrar_wav(self) -> None:
        try:
            if self._wav is not None:
                self._wav.close()
        except OSError as e:
            log.exception("Error al cerrar %s", self.ruta)
            self.estado.error = self.estado.error or f"Error al cerrar {self.ruta.name}: {e}"
        finally:
            self._wav = None
            if self._archivo is not None:
                try:
                    self._archivo.close()
                except OSError:
                    log.exception("Error al cerrar el archivo %s", self.ruta)
                self._archivo = None

    def resumen(self) -> dict:
        a = self.alineador
        return {
            "archivo": self.ruta.name,
            "dispositivo": self.dispositivo["name"],
            "frecuencia_original": self._frecuencia,
            "duracion_s": round(a.escritas / FRECUENCIA, 3),
            "pico_max_dbfs": round(self.estado.pico_max_dbfs, 1),
            "huecos_rellenados": a.huecos,
            "silencio_insertado_s": round(a.silencio_insertado / FRECUENCIA, 3),
            "deriva_corregida_ms": round(a.deriva_corregida / FRECUENCIA * 1000, 1),
            "desbordamientos": self.estado.desbordamientos,
            "error": self.estado.error,
        }


# --------------------------------------------------------------------------
# Grabadora
# --------------------------------------------------------------------------


def nombre_seguro(titulo: str) -> str:
    """Convierte un título en un nombre válido de carpeta de Windows."""
    limpio = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", titulo)
    limpio = re.sub(r"\s+", "-", limpio.strip())
    return limpio[:60].strip(".-") or "reunion"


def crear_carpeta(raiz: Path, titulo: str, momento: datetime) -> Path:
    base = f"{momento:%Y-%m-%d_%H-%M}_{nombre_seguro(titulo)}"
    for n in range(1, 100):
        ruta = raiz / (base if n == 1 else f"{base}_{n}")
        try:
            ruta.mkdir(parents=True)
            return ruta
        except FileExistsError:
            continue
        except OSError as e:
            raise ErrorGrabacion(f"No se pudo crear la carpeta {ruta}: {e}") from e
    raise ErrorGrabacion(f"Ya existen demasiadas carpetas llamadas {base}.")


class Grabadora:
    """Graba las dos pistas. Uso: iniciar(titulo) -> detener()."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.carpeta: Path | None = None
        self.avisos: list[str] = []
        self.mic: Pista | None = None
        self.sistema: Pista | None = None
        self._pa: pyaudio.PyAudio | None = None
        self._silencio = None
        self._t0 = 0.0
        self._fecha: datetime | None = None

    @property
    def grabando(self) -> bool:
        return self._pa is not None

    @property
    def transcurrido(self) -> float:
        return time.perf_counter() - self._t0 if self.grabando else 0.0

    def iniciar(self, titulo: str) -> Path:
        if self.grabando:
            raise ErrorGrabacion("Ya hay una grabación en curso.")
        self.avisos = []
        self.carpeta = None
        # Se crea en cada grabación para detectar dispositivos recién conectados.
        self._pa = pyaudio.PyAudio()
        try:
            disp_mic, aviso_mic = elegir_microfono(self._pa, self.cfg.microfono)
            disp_sis, aviso_sis = elegir_loopback(self._pa, self.cfg.salida)
            self.avisos += [a for a in (aviso_mic, aviso_sis) if a]

            self._fecha = datetime.now().astimezone()
            self.carpeta = crear_carpeta(
                Path(self.cfg.carpeta_reuniones), titulo, self._fecha
            )
            self.mic = Pista(self._pa, disp_mic, self.carpeta / "mic.wav", "mic")
            self.sistema = Pista(self._pa, disp_sis, self.carpeta / "sistema.wav", "sistema")
            # Se abren ambos antes de arrancar para que empiecen casi a la vez.
            self.mic.abrir()
            self.sistema.abrir()
            self._mantener_salida_activa(disp_sis)
            self._fecha = datetime.now().astimezone()
            self._t0 = time.perf_counter()
            self.mic.iniciar(self._t0)
            self.sistema.iniciar(self._t0)
        except Exception as e:
            log.exception("No se pudo iniciar la grabación")
            self._abortar()
            if isinstance(e, ErrorGrabacion):
                raise
            raise ErrorGrabacion(f"No se pudo iniciar la grabación: {e}") from e
        for aviso in self.avisos:
            log.warning(aviso)
        log.info("Grabando en %s | mic=%s | sistema=%s",
                 self.carpeta, disp_mic["name"], disp_sis["name"])
        self._guardar_sesion(titulo, terminada=False)
        self._titulo = titulo
        return self.carpeta

    def problemas(self) -> list[str]:
        """Fallos en curso: dispositivo desconectado o error de escritura."""
        lista = []
        for p in (self.mic, self.sistema):
            if p is None or not self.grabando:
                continue
            if p.estado.error:
                lista.append(p.estado.error)
            elif not p.activa():
                lista.append(f"Se perdió el dispositivo '{p.dispositivo['name']}' ({p.nombre}).")
        return lista

    def detener(self) -> dict:
        if not self.grabando:
            raise ErrorGrabacion("No hay ninguna grabación en curso.")
        self.avisos += [p for p in self.problemas() if p not in self.avisos]
        t_fin = time.perf_counter() - self._t0
        # Primero se cierran ambos dispositivos y después se vacían las colas.
        self.mic.cerrar_stream()
        self.sistema.cerrar_stream()
        self.mic.detener(t_fin)
        self.sistema.detener(t_fin)
        self._liberar()

        for p, texto in (
            (self.mic, "La pista del micrófono está vacía o en silencio."),
            (self.sistema, "La pista del sistema está vacía o en silencio: "
                           "comprueba que la reunión sonaba por la salida grabada."),
        ):
            if p.estado.error and p.estado.error not in self.avisos:
                self.avisos.append(p.estado.error)
            if p.estado.pico_max_dbfs < -60:
                self.avisos.append(texto)
            if p.estado.desbordamientos:
                self.avisos.append(
                    f"La pista {p.nombre} perdió audio {p.estado.desbordamientos} veces."
                )
        sesion = self._guardar_sesion(self._titulo, terminada=True)
        log.info("Grabación terminada: %s", json.dumps(sesion, ensure_ascii=False))
        return sesion

    def _mantener_salida_activa(self, loopback: dict) -> None:
        """Reproduce silencio para que el loopback entregue datos sin pausas."""
        salida = _salida_de_loopback(self._pa, loopback)
        if salida is None:
            log.warning("No se encontró la salida de %s", loopback["name"])
            return
        canales = int(salida["maxOutputChannels"])

        def callback(_datos, n, _tiempos, _banderas):
            return b"\x00" * (n * canales * 2), pyaudio.paContinue

        try:
            self._silencio = self._pa.open(
                format=pyaudio.paInt16,
                channels=canales,
                rate=int(salida["defaultSampleRate"]),
                output=True,
                output_device_index=salida["index"],
                stream_callback=callback,
            )
        except (OSError, ValueError):
            # No es grave: AlineadorPista rellena los huecos igualmente.
            log.warning("No se pudo mantener activa la salida", exc_info=True)

    def _guardar_sesion(self, titulo: str, terminada: bool) -> dict:
        sesion = {
            "titulo": titulo,
            "inicio": self._fecha.isoformat(timespec="milliseconds"),
            "terminada": terminada,
            "nombre_usuario": self.cfg.nombre_usuario,
            "frecuencia": FRECUENCIA,
            "pistas": {"mic": self.mic.resumen(), "sistema": self.sistema.resumen()},
            "avisos": self.avisos,
        }
        try:
            (self.carpeta / "sesion.json").write_text(
                json.dumps(sesion, indent=2, ensure_ascii=False), encoding="utf-8"
            )
        except OSError:
            # Perder los metadatos no debe interrumpir la grabación del audio.
            log.exception("No se pudo guardar sesion.json")
        return sesion

    def _liberar(self) -> None:
        if self._silencio is not None:
            try:
                self._silencio.close()
            except OSError:
                log.exception("Error al cerrar la salida de silencio")
            self._silencio = None
        if self._pa is not None:
            self._pa.terminate()
            self._pa = None

    def _abortar(self) -> None:
        for p in (self.mic, self.sistema):
            if p is not None:
                p.detener(0.0)
        self._liberar()
        # Borra solo lo que acaba de crear este intento fallido.
        if self.carpeta is not None:
            for nombre in ("mic.wav", "sistema.wav"):
                (self.carpeta / nombre).unlink(missing_ok=True)
            try:
                self.carpeta.rmdir()
            except OSError:
                log.warning("No se pudo borrar la carpeta vacía %s", self.carpeta)
        self.mic = self.sistema = None
