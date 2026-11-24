"""Transcripción local en español con faster-whisper.

Cada intento se ejecuta en un proceso aparte. Así se libera toda la memoria al
terminar y, si el controlador de la GPU falla, el programa principal sigue vivo
y puede reintentar con otra configuración.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import wave
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np

import proceso
from config import CARPETA_MODELOS, Config, comando

log = logging.getLogger(__name__)

# De mayor a menor calidad.
MODELOS = ("large-v3", "large-v3-turbo", "medium", "small")
# Los más rápidos primero: en CPU, large-v3 sería demasiado lento.
PREFERENCIA_CPU = ("small", "medium", "large-v3-turbo", "large-v3")
FRECUENCIA_WHISPER = 16_000
# Por debajo de este pico la pista se considera en silencio. Whisper tiende a
# inventar frases si se le da silencio.
PICO_SILENCIO = 10 ** (-60 / 20)
# Sin noticias de la GPU durante este tiempo, se considera bloqueada.
LIMITE_GPU_S = 300
LOTE_GPU = 8
# En la RX 7600 XT con ROCm 7.2, float16 y bfloat16 acaban devolviendo "!!!!" al
# encadenar audios; int8_float16 dio siempre el mismo texto en las pruebas.
CALCULO_GPU = "int8_float16"
# Segundos de voz a partir de los que un resultado vacío de la GPU es sospechoso.
VOZ_MINIMA_S = 5.0


class ErrorTranscripcion(Exception):
    """Error de transcripción con un mensaje apto para mostrar al usuario."""


@dataclass
class Palabra:
    inicio: float
    fin: float
    texto: str
    probabilidad: float


@dataclass
class Segmento:
    inicio: float
    fin: float
    texto: str
    palabras: list[Palabra] = field(default_factory=list)


@dataclass
class Resultado:
    archivo: str
    duracion: float
    modelo: str
    dispositivo: str
    calculo: str
    segundos_proceso: float
    segmentos: list[Segmento]
    aviso: str | None = None


@dataclass(frozen=True)
class Intento:
    dispositivo: str
    modelo: str
    calculo: str
    # Tramos que se procesan a la vez; 1 = uno detrás de otro.
    lote: int = 1

    def __str__(self) -> str:
        nombre = "GPU" if self.dispositivo == "cuda" else "CPU"
        return f"{self.modelo} en {nombre} ({self.calculo})"


# --------------------------------------------------------------------------
# Modelos y dispositivos
# --------------------------------------------------------------------------


def modelos_disponibles() -> list[str]:
    return [m for m in MODELOS if (CARPETA_MODELOS / m / "model.bin").exists()]


def descargar_modelo(nombre: str) -> Path:
    """Descarga un modelo de Hugging Face. Es lo único que usa Internet."""
    if nombre not in MODELOS:
        raise ErrorTranscripcion(f"Modelo desconocido: {nombre}. Opciones: {', '.join(MODELOS)}")
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    from faster_whisper import download_model

    destino = CARPETA_MODELOS / nombre
    try:
        download_model(nombre, output_dir=str(destino))
    except Exception as e:
        raise ErrorTranscripcion(f"No se pudo descargar {nombre}: {e}") from e
    return destino


def hay_gpu() -> bool:
    """Indica si CTranslate2 ve una GPU (NVIDIA con CUDA o AMD con ROCm)."""
    try:
        import ctranslate2

        return ctranslate2.get_cuda_device_count() > 0
    except Exception:  # faltan DLL del controlador
        log.warning("No se pudo consultar la GPU", exc_info=True)
        return False


def planificar(modelo: str, dispositivo: str, gpu: bool, disponibles: list[str]) -> list[Intento]:
    """Devuelve los intentos en orden: de la mejor opción a la más segura."""
    if not disponibles:
        raise ErrorTranscripcion(
            f"No hay ningún modelo descargado. Ejecuta: {comando('descargar small')}"
        )
    if modelo != "auto":
        if modelo not in MODELOS:
            raise ErrorTranscripcion(
                f"Modelo desconocido: {modelo}. Opciones: auto, {', '.join(MODELOS)}"
            )
        if modelo not in disponibles:
            raise ErrorTranscripcion(
                f"El modelo {modelo} no está descargado. Ejecuta: {comando('descargar ' + modelo)}"
            )
    if dispositivo not in ("auto", "gpu", "cpu"):
        raise ErrorTranscripcion(f"Dispositivo desconocido: {dispositivo}. Opciones: auto, gpu, cpu")
    if dispositivo == "gpu" and not gpu:
        raise ErrorTranscripcion("No se detecta ninguna GPU compatible. Usa dispositivo auto o cpu.")

    intentos = []
    if gpu and dispositivo != "cpu":
        m = modelo if modelo != "auto" else next(x for x in MODELOS if x in disponibles)
        # El segundo intento, sin lotes, es más lento pero usa menos memoria.
        intentos += [Intento("cuda", m, CALCULO_GPU, LOTE_GPU), Intento("cuda", m, CALCULO_GPU)]
    if dispositivo != "gpu":
        rapido = next(x for x in PREFERENCIA_CPU if x in disponibles)
        # Si la GPU estaba prevista, el respaldo usa el modelo rápido.
        m = modelo if modelo != "auto" and not intentos else rapido
        intentos.append(Intento("cpu", m, "int8"))
    return intentos


# --------------------------------------------------------------------------
# Audio
# --------------------------------------------------------------------------


def leer_wav(ruta: Path) -> np.ndarray:
    """Lee un WAV PCM de 16 bits como float32 mono a 16 kHz."""
    import soxr

    try:
        with wave.open(str(ruta), "rb") as w:
            if w.getsampwidth() != 2:
                raise ErrorTranscripcion(f"{ruta.name}: solo se admite WAV de 16 bits.")
            frecuencia, canales = w.getframerate(), w.getnchannels()
            x = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    except FileNotFoundError:
        raise ErrorTranscripcion(f"No existe {ruta}") from None
    except (wave.Error, EOFError) as e:
        raise ErrorTranscripcion(f"{ruta} no es un WAV válido: {e}") from e
    y = x.astype(np.float32) / 32768
    if canales > 1:
        y = y.reshape(-1, canales).mean(axis=1)
    if len(y) and frecuencia != FRECUENCIA_WHISPER:
        y = soxr.resample(y, frecuencia, FRECUENCIA_WHISPER)
    return y


# --------------------------------------------------------------------------
# Proceso hijo
# --------------------------------------------------------------------------


class TextoCorrupto(Exception):
    """La GPU devolvió un resultado sin sentido."""


def es_corrupto(texto: str) -> bool:
    """Detecta la salida típica de un cálculo inválido: un símbolo repetido."""
    return re.search(r"([^\w\s.…])\1{9,}|([^\W\d])\2{19,}", texto) is not None


def _transcribir_audio(
    modelo, audio: np.ndarray, vocabulario: str, lote: int, avisar
) -> list[Segmento]:
    extra = {}
    if lote > 1:
        from faster_whisper import BatchedInferencePipeline

        modelo = BatchedInferencePipeline(modelo)
        # Con marcas de tiempo devuelve frases; sin ellas, bloques de 30 s.
        extra = {"batch_size": lote, "without_timestamps": False}
    segmentos, info = modelo.transcribe(
        audio,
        language="es",
        task="transcribe",
        beam_size=5,
        # Descarta los tramos sin voz antes de transcribir.
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 500},
        # Necesario para asignar cada palabra a un hablante.
        word_timestamps=True,
        # Evita que un error se repita en bucle en el resto de la reunión.
        condition_on_previous_text=False,
        # Términos técnicos y nombres propios que suelen aparecer.
        hotwords=vocabulario or None,
        **extra,
    )
    lista = []
    en_gpu = modelo.model.model.device == "cuda" if lote > 1 else modelo.model.device == "cuda"
    # La transcripción ocurre al recorrer el generador.
    for s in segmentos:
        texto = s.text.strip()
        if es_corrupto(texto):
            raise TextoCorrupto(f"texto sin sentido en el segundo {s.start:.0f}")
        if texto:
            lista.append(Segmento(
                round(s.start, 3), round(s.end, 3), texto,
                [Palabra(round(p.start, 3), round(p.end, 3), p.word, round(p.probability, 3))
                 for p in s.words or []],
            ))
        avisar(min(1.0, s.end / info.duration) if info.duration else 1.0)
    # El mismo fallo también puede dar un resultado vacío. Si de verdad no había
    # palabras, el último intento (en CPU) lo confirmará.
    if en_gpu and not lista and info.duration_after_vad >= VOZ_MINIMA_S:
        raise TextoCorrupto("no devolvió texto pese a detectarse voz")
    return lista


def _trabajador(conexion, rutas: list[str], intento: Intento, vocabulario: str) -> None:
    """Se ejecuta en el proceso hijo y se comunica mediante mensajes."""
    # Garantiza que el audio y los modelos se traten sin acceso a Internet.
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    try:
        from faster_whisper import WhisperModel

        conexion.send(("estado", f"Cargando {intento}"))
        t0 = time.perf_counter()
        modelo = WhisperModel(
            str(CARPETA_MODELOS / intento.modelo),
            device=intento.dispositivo,
            compute_type=intento.calculo,
            # Núcleos físicos: usar también los lógicos no acelera la CPU.
            cpu_threads=max(1, (os.cpu_count() or 2) // 2),
            local_files_only=True,
        )
        conexion.send(("log", f"{intento}: modelo cargado en {time.perf_counter() - t0:.1f} s"))
        for i, texto_ruta in enumerate(rutas):
            ruta = Path(texto_ruta)
            conexion.send(("estado", f"Transcribiendo {ruta.name} con {intento}"))
            t0 = time.perf_counter()
            audio = leer_wav(ruta)
            duracion = len(audio) / FRECUENCIA_WHISPER
            segmentos, aviso = [], None
            if duracion < 0.5:
                aviso = "La grabación está vacía."
            elif float(np.abs(audio).max()) < PICO_SILENCIO:
                aviso = "La grabación está en silencio."
            else:
                segmentos = _transcribir_audio(
                    modelo, audio, vocabulario, intento.lote,
                    lambda p, i=i: conexion.send(("progreso", (i + p) / len(rutas))),
                )
                if not segmentos:
                    aviso = "No se detectó voz."
            conexion.send(("resultado", Resultado(
                ruta.name, round(duracion, 3), intento.modelo, intento.dispositivo,
                intento.calculo, round(time.perf_counter() - t0, 2), segmentos, aviso,
            )))
        conexion.send(("fin", None))
    except ErrorTranscripcion as e:
        conexion.send(("error_usuario", str(e)))
    except BaseException as e:
        conexion.send(("error", f"{type(e).__name__}: {e}"))
    finally:
        conexion.close()


# --------------------------------------------------------------------------
# Proceso principal
# --------------------------------------------------------------------------


def _ejecutar(
    rutas: list[Path],
    intento: Intento,
    vocabulario: str,
    al_estado: Callable[[str], None],
    al_progreso: Callable[[float], None],
    cancelar: threading.Event | None,
    trabajador=_trabajador,
    limite_gpu: float = LIMITE_GPU_S,
) -> list[Resultado]:
    try:
        return proceso.ejecutar(
            trabajador, ([str(r) for r in rutas], intento, vocabulario),
            al_estado, al_progreso, cancelar,
            limite_gpu if intento.dispositivo == "cuda" else None,
        )
    except proceso.ErrorUsuario as e:
        raise ErrorTranscripcion(str(e)) from e


def transcribir(
    rutas: list[Path],
    cfg: Config,
    al_estado: Callable[[str], None] = lambda _t: None,
    al_progreso: Callable[[float], None] = lambda _p: None,
    cancelar: threading.Event | None = None,
) -> list[Resultado]:
    """Transcribe los WAV y pasa a una configuración más segura si algo falla."""
    for r in rutas:
        if not r.exists():
            raise ErrorTranscripcion(f"No existe {r}")
    intentos = planificar(cfg.modelo, cfg.dispositivo, hay_gpu(), modelos_disponibles())
    fallos = []
    for n, intento in enumerate(intentos):
        try:
            return _ejecutar(rutas, intento, cfg.vocabulario, al_estado, al_progreso, cancelar)
        except proceso.FalloProceso as e:
            log.error("Falló %s: %s", intento, e)
            fallos.append(f"{intento}: {proceso.explicar(str(e))}")
            if n + 1 < len(intentos):
                al_estado(f"Falló {fallos[-1]}. Se prueba {intentos[n + 1]}.")
    raise ErrorTranscripcion("No se pudo transcribir.\n  - " + "\n  - ".join(fallos))


def ruta_json(wav: Path) -> Path:
    return wav.with_suffix(".transcripcion.json")


def guardar_resultado(r: Resultado, wav: Path) -> Path:
    destino = ruta_json(wav)
    try:
        destino.write_text(
            json.dumps(asdict(r), indent=1, ensure_ascii=False), encoding="utf-8"
        )
    except OSError as e:
        raise ErrorTranscripcion(f"No se pudo guardar {destino}: {e}") from e
    return destino


def cargar_resultado(wav: Path) -> Resultado | None:
    """Recupera una transcripción anterior si es más reciente que el WAV."""
    ruta = ruta_json(wav)
    try:
        if ruta.stat().st_mtime < wav.stat().st_mtime:
            return None
        d = json.loads(ruta.read_text(encoding="utf-8"))
        d["segmentos"] = [
            Segmento(s["inicio"], s["fin"], s["texto"], [Palabra(**p) for p in s["palabras"]])
            for s in d["segmentos"]
        ]
        if any(es_corrupto(s.texto) for s in d["segmentos"]):
            return None
        return Resultado(**d)
    except FileNotFoundError:
        return None
    except (OSError, ValueError, KeyError, TypeError):
        log.warning("Se ignora %s: no se puede leer", ruta, exc_info=True)
        return None
