"""Pasos que comparten la consola y la ventana: listar y procesar reuniones."""

from __future__ import annotations

import json
import logging
import re
import shutil
import threading
import wave
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import diarizer
import exporter
import merger
import reproductor
import transcriber
from config import Config
from recorder import ErrorGrabacion, nombre_seguro
from windows import ErrorPapelera

log = logging.getLogger(__name__)

# Errores con un mensaje pensado para mostrarse al usuario.
ERRORES = (
    ErrorGrabacion, transcriber.ErrorTranscripcion, diarizer.ErrorDiarizacion,
    merger.ErrorUnion, exporter.ErrorExportacion, reproductor.ErrorReproduccion,
    ErrorPapelera,
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


def renombrar_reunion(carpeta: Path, titulo: str) -> Path:
    """Cambia el título y el nombre de la carpeta. Devuelve la carpeta nueva."""
    titulo = re.sub(r"\s+", " ", titulo).strip()
    if not titulo or not titulo.isprintable():
        raise ErrorGrabacion("El título está vacío o no es válido.")
    if len(titulo) > 100:
        raise ErrorGrabacion("El título supera los 100 caracteres.")
    nueva = carpeta
    fecha = re.match(r"\d{4}-\d{2}-\d{2}_\d{2}-\d{2}_", carpeta.name)
    if fecha:       # solo las carpetas con el nombre que pone la aplicación
        base, n = fecha[0] + nombre_seguro(titulo), 1
        nueva = carpeta.with_name(base)
        # En Windows, cambiar solo mayúsculas apunta a la misma carpeta.
        while nueva.exists() and not nueva.samefile(carpeta):
            n += 1
            nueva = carpeta.with_name(f"{base}_{n}")
    # Es lo único que suele fallar: se hace antes de cambiar nada más.
    try:
        if nueva.name != carpeta.name:
            carpeta.rename(nueva)
    except OSError as e:
        raise ErrorGrabacion(
            "No se pudo cambiar el nombre. Cierra los archivos de esa reunión que estén "
            f"abiertos y vuelve a intentarlo. Detalle: {e}") from e
    sesion = leer_sesion(nueva) | {"titulo": titulo}
    temporal = nueva / "sesion.json.tmp"
    try:
        temporal.write_text(json.dumps(sesion, indent=2, ensure_ascii=False), encoding="utf-8")
        temporal.replace(nueva / "sesion.json")
    except OSError as e:
        raise ErrorGrabacion(f"No se pudo guardar el título: {e}") from e
    if (nueva / "transcripcion.json").exists():
        t = merger.cargar(nueva)
        t.titulo = titulo
        merger.guardar(t, nueva)
        exporter.exportar(t, nueva)
    return nueva


def _guardar_copia(carpeta: Path) -> str:
    base = f"transcripcion (corregida {datetime.now():%Y-%m-%d %H-%M-%S})"
    copia, n = carpeta / f"{base}.json", 2
    while copia.exists():
        copia, n = carpeta / f"{base} {n}.json", n + 1
    try:
        shutil.copy2(carpeta / "transcripcion.json", copia)
    except OSError as e:
        # Sin copia no se reemplaza: se perdería el trabajo del usuario.
        raise merger.ErrorUnion(f"No se pudo guardar una copia de las correcciones: {e}") from e
    return copia.name


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
        anterior = merger.cargar(carpeta)
    except merger.ErrorUnion:
        avisos_nombres = []     # primera vez, o archivo anterior ilegible
    else:
        avisos_nombres = merger.heredar_nombres(t, anterior)
        if anterior.corregida:
            avisos_nombres.append(
                "Las correcciones hechas a mano no pasan a la transcripción nueva. La anterior "
                f"se conserva en {_guardar_copia(carpeta)}.")
    merger.guardar(t, carpeta)
    e = exporter.exportar(t, carpeta)
    return Procesado(t, e, t.avisos + avisos_nombres, d.segundos_proceso, fallo is not None)
