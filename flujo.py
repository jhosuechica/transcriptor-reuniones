"""Pasos que comparten la consola y la ventana: listar y procesar reuniones."""

from __future__ import annotations

import json
import logging
import threading
import wave
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import diarizer
import exporter
import merger
import transcriber
from config import Config
from recorder import ErrorGrabacion

log = logging.getLogger(__name__)

# Errores con un mensaje pensado para mostrarse al usuario.
ERRORES = (
    ErrorGrabacion, transcriber.ErrorTranscripcion, diarizer.ErrorDiarizacion,
    merger.ErrorUnion, exporter.ErrorExportacion,
)

GRABADA, TRANSCRITA, INTERRUMPIDA = "Grabada", "Transcrita", "Interrumpida"


@dataclass
class Reunion:
    carpeta: Path
    titulo: str
    inicio: datetime | None
    duracion: float
    estado: str


@dataclass
class Procesado:
    transcripcion: merger.Transcripcion
    exportacion: exporter.Exportacion
    avisos: list[str]
    segundos_diarizacion: float
    # La diarización falló: el resultado tiene un solo hablante.
    incompleto: bool


def leer_sesion(carpeta: Path) -> dict:
    try:
        d = json.loads((carpeta / "sesion.json").read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _duracion(wav: Path) -> float:
    try:
        with wave.open(str(wav), "rb") as w:
            return w.getnframes() / w.getframerate()
    except (OSError, EOFError, wave.Error, ZeroDivisionError):
        return 0.0


def listar(raiz: Path) -> list[Reunion]:
    """Reuniones de la carpeta, de la más reciente a la más antigua."""
    reuniones = []
    try:
        carpetas = [c for c in raiz.iterdir() if (c / "mic.wav").exists()]
    except FileNotFoundError:
        return []       # todavía no se ha grabado nada
    except OSError as e:
        raise ErrorGrabacion(f"No se puede leer {raiz}: {e}") from e
    for c in carpetas:
        s = leer_sesion(c)
        try:
            inicio = datetime.fromisoformat(s["inicio"])
        except (KeyError, TypeError, ValueError):
            inicio = None
        if (c / "transcripcion.json").exists():
            estado = TRANSCRITA
        else:
            estado = GRABADA if s.get("terminada") else INTERRUMPIDA
        reuniones.append(Reunion(
            c, str(s.get("titulo") or c.name), inicio, _duracion(c / "mic.wav"), estado,
        ))
    # El nombre de la carpeta empieza por la fecha.
    return sorted(reuniones, key=lambda r: r.carpeta.name, reverse=True)


def procesar(
    carpeta: Path,
    cfg: Config,
    hablantes: int | None = None,
    retranscribir: bool = False,
    al_estado: Callable[[str], None] = lambda _t: None,
    al_progreso: Callable[[float], None] = lambda _p: None,
    cancelar: threading.Event | None = None,
) -> Procesado:
    """Transcribe, separa hablantes, une las pistas y exporta."""
    wavs = [carpeta / "mic.wav", carpeta / "sistema.wav"]
    for w in wavs:
        if not w.exists():
            raise transcriber.ErrorTranscripcion(f"No existe {w}")
    # Se valida antes de empezar, para no fallar tras una larga espera.
    diarizer.comprobar(hablantes)

    # Permite repetir con otro número de hablantes sin volver a transcribir.
    previos = [transcriber.cargar_resultado(w) for w in wavs]
    if retranscribir or not all(previos):
        resultados = transcriber.transcribir(wavs, cfg, al_estado, al_progreso, cancelar)
        for w, r in zip(wavs, resultados, strict=True):
            transcriber.guardar_resultado(r, w)
    else:
        resultados = previos
        al_estado("Se reutiliza la transcripción anterior")

    fallo = None
    try:
        d = diarizer.diarizar(wavs[1], hablantes, al_estado, al_progreso, cancelar)
        diarizer.guardar(d, wavs[1])
    except diarizer.ErrorDiarizacion as e:
        # La transcripción ya está hecha: se conserva con un único hablante.
        d, fallo = diarizer.Diarizacion(), f"{e} Se usa un solo hablante."

    sesion = leer_sesion(carpeta)
    t = merger.unir(
        *resultados, d.turnos, sesion.get("titulo") or carpeta.name,
        sesion.get("inicio") or "", cfg.nombre_usuario,
    )
    if fallo:
        t.avisos.append(fallo)
    try:
        avisos_nombres = merger.heredar_nombres(t, merger.cargar(carpeta))
    except merger.ErrorUnion:
        avisos_nombres = []     # primera vez, o archivo anterior ilegible
    merger.guardar(t, carpeta)
    e = exporter.exportar(t, carpeta)
    return Procesado(t, e, t.avisos + avisos_nombres, d.segundos_proceso, fallo is not None)
