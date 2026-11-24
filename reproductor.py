"""Reproduce una reunión mezclando sus dos pistas.

No crea hilos: `Sesion.ejecutar` se bloquea hasta terminar y debe llamarse desde
el hilo de audio de la aplicación. Los demás métodos sirven desde cualquiera.
"""

from __future__ import annotations

import logging
import threading
import wave
from pathlib import Path

import numpy as np
import pyaudiowpatch as pyaudio
import soxr

log = logging.getLogger(__name__)

BLOQUE_S = 0.05     # lo que tarda, como mucho, en atender una parada o un salto


class ErrorReproduccion(Exception):
    """Error al reproducir, con un mensaje apto para mostrar al usuario."""


def mezclar(pistas: list[np.ndarray]) -> np.ndarray:
    """Suma pistas de 16 bits, de largo distinto si hace falta, sin desbordar."""
    suma = np.zeros(max((len(p) for p in pistas), default=0), np.int32)
    for p in pistas:
        suma[:len(p)] += p
    return np.clip(suma, -32768, 32767).astype(np.int16)


class Sesion:
    """Una reproducción, desde un instante hasta otro (o hasta el final)."""

    def __init__(self, carpeta: Path, desde: float, hasta: float | None = None):
        self.carpeta = carpeta
        self.posicion = max(0.0, desde)
        self._hasta = hasta
        self._salto: float | None = None
        self._parar = threading.Event()

    def parar(self) -> None:
        self._parar.set()

    def saltar(self, t: float) -> None:
        # Tras un salto se sigue hasta el final, aunque fuera una sola frase.
        self._hasta = None
        self._salto = max(0.0, t)

    def ejecutar(self) -> None:
        if self._parar.is_set():
            return
        archivos: list[wave.Wave_read] = []
        pa = salida = None
        try:
            for nombre in ("mic.wav", "sistema.wav"):
                if (self.carpeta / nombre).exists():
                    archivos.append(wave.open(str(self.carpeta / nombre), "rb"))
            if not archivos:
                raise ErrorReproduccion(f"No hay audio en {self.carpeta}")
            formatos = {a.getparams()[:3] for a in archivos}
            if len(formatos) > 1 or archivos[0].getparams()[:2] != (1, 2):
                raise ErrorReproduccion("Las pistas no tienen el formato esperado.")
            frecuencia = archivos[0].getframerate()
            pa = pyaudio.PyAudio()
            # Windows solo acepta la frecuencia y los canales del dispositivo.
            disp = pa.get_default_output_device_info()
            destino = int(disp["defaultSampleRate"])
            canales = min(2, int(disp["maxOutputChannels"]))
            salida = pa.open(
                format=pyaudio.paInt16, channels=canales, rate=destino, output=True,
                output_device_index=disp["index"],
                frames_per_buffer=max(1, int(destino * BLOQUE_S)),
            )
            self._bucle(archivos, salida, frecuencia, destino, canales)
        except ErrorReproduccion:
            raise
        except (OSError, EOFError, wave.Error) as e:
            raise ErrorReproduccion(f"No se pudo reproducir el audio: {e}") from e
        finally:
            for a in archivos:
                a.close()
            if salida is not None:
                try:
                    salida.close()      # descarta lo que quede por sonar
                except OSError:
                    log.warning("Error al cerrar la salida", exc_info=True)
            if pa is not None:
                pa.terminate()

    def _bucle(self, archivos, salida, frecuencia: int, destino: int, canales: int) -> None:
        def adaptador():
            return (soxr.ResampleStream(frecuencia, destino, 1, dtype=np.int16)
                    if destino != frecuencia else None)

        latencia = salida.get_output_latency()
        bloque = max(1, int(frecuencia * BLOQUE_S))
        trozo = max(1, int(destino * BLOQUE_S))
        leidas = self._colocar(archivos, self.posicion, frecuencia)
        origen = leidas / frecuencia    # segundo en que empezó este tramo
        remuestreo, escritas, pendiente = adaptador(), 0, np.zeros(0, np.int16)
        agotado = False
        while not self._parar.is_set():
            salto, self._salto = self._salto, None
            if salto is not None:
                leidas = self._colocar(archivos, salto, frecuencia)
                origen = self.posicion = leidas / frecuencia
                # Se empieza de cero, sin mezclar los dos lados del salto.
                remuestreo, escritas, pendiente = adaptador(), 0, np.zeros(0, np.int16)
                agotado = False
            # El remuestreo entrega a ráfagas: se escribe siempre en trozos cortos
            # para que parar y saltar respondan enseguida.
            while len(pendiente) < trozo and not agotado:
                n = bloque
                if self._hasta is not None:
                    n = min(n, round(self._hasta * frecuencia) - leidas)
                datos = mezclar([
                    np.frombuffer(a.readframes(n), np.int16) for a in archivos
                ]) if n > 0 else np.zeros(0, np.int16)
                leidas += len(datos)
                agotado = not len(datos)
                if remuestreo is not None:
                    datos = remuestreo.resample_chunk(datos, last=agotado)
                pendiente = np.concatenate((pendiente, datos))
            if not len(pendiente):
                salida.stop_stream()    # deja sonar lo pendiente
                self.posicion = leidas / frecuencia
                return
            actual, pendiente = pendiente[:trozo], pendiente[trozo:]
            salida.write(np.repeat(actual, canales).tobytes())
            escritas += len(actual)
            # Lo que se oye va por detrás de lo que se ha escrito.
            self.posicion = max(self.posicion, origen + escritas / destino - latencia)

    @staticmethod
    def _colocar(archivos, t: float, frecuencia: int) -> int:
        marco = int(t * frecuencia)
        for a in archivos:
            a.setpos(min(marco, a.getnframes()))
        # Más allá del final se considera que ya se leyó todo.
        return min(marco, max(a.getnframes() for a in archivos))
