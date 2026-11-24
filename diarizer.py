"""Diarización: averigua quién habla en cada momento de la pista del sistema.

Usa pyannote.audio en CPU y en un proceso aparte. El modelo se carga desde la
carpeta local, sin Internet y con la telemetría de pyannote desactivada.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np

import config
import proceso
from transcriber import (
    CARPETA_MODELOS,
    FRECUENCIA_WHISPER,
    PICO_SILENCIO,
    ErrorTranscripcion,
    leer_wav,
)

log = logging.getLogger(__name__)

REPOSITORIO = "pyannote/speaker-diarization-community-1"
CARPETA = CARPETA_MODELOS / "diarizacion"
MAX_HABLANTES = 20
# Parte del progreso total que corresponde a cada etapa lenta de pyannote.
ETAPAS = {"segmentation": (0.0, 0.3), "embeddings": (0.3, 1.0)}


class ErrorDiarizacion(Exception):
    """Error de diarización con un mensaje apto para mostrar al usuario."""


@dataclass
class Turno:
    inicio: float
    fin: float
    hablante: str


@dataclass
class Diarizacion:
    turnos: list[Turno] = field(default_factory=list)
    hablantes_pedidos: int | None = None
    segundos_proceso: float = 0.0
    aviso: str | None = None


def disponible() -> bool:
    return (CARPETA / "config.yaml").exists()


def descargar(token: str) -> Path:
    """Descarga el modelo. El token se usa solo aquí y no se guarda."""
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    from huggingface_hub import snapshot_download
    from huggingface_hub.errors import GatedRepoError, HfHubHTTPError

    try:
        snapshot_download(REPOSITORIO, local_dir=str(CARPETA), token=token)
    except GatedRepoError as e:
        raise ErrorDiarizacion(
            "Hugging Face denegó el acceso. Comprueba que el token es de tipo Read y acepta las condiciones en "
            f"https://huggingface.co/{REPOSITORIO} con la misma cuenta del token."
        ) from e
    except HfHubHTTPError as e:
        codigo = getattr(e.response, "status_code", None)
        if codigo == 401:
            raise ErrorDiarizacion("El token no es válido o ha caducado.") from e
        raise ErrorDiarizacion(f"Hugging Face respondió con un error ({codigo}).") from e
    except Exception as e:
        raise ErrorDiarizacion(f"No se pudo descargar el modelo: {e}") from e
    return CARPETA


def comprobar(num_hablantes: int | None) -> None:
    """Valida los datos antes de empezar un trabajo largo."""
    if num_hablantes is not None and not 1 <= num_hablantes <= MAX_HABLANTES:
        raise ErrorDiarizacion(f"El número de hablantes debe estar entre 1 y {MAX_HABLANTES}.")
    # Con un solo hablante no hay nada que separar: no hace falta el modelo.
    if num_hablantes != 1 and not disponible():
        raise ErrorDiarizacion(
            f"Falta el modelo de diarización. Ejecuta: {config.comando('descargar diarizacion')}"
        )


class HuellasSinRepetir:
    """Acelera el cálculo de las huellas de voz sin cambiar el resultado.

    pyannote pasa el mismo fragmento por la red una vez por posible hablante,
    cambiando solo la máscara final. Aquí se pasa una vez con todas las máscaras.
    """

    def __init__(self, original):
        self._original = original

    def __getattr__(self, nombre):
        return getattr(self._original, nombre)

    def __call__(self, ondas, masks=None):
        import torch

        n = len(ondas)
        if masks is None or n < 2 or not all(torch.equal(ondas[0], o) for o in ondas[1:]):
            return self._original(ondas, masks=masks)
        # (1, canal, muestras) y (1, hablantes, tramas) -> (1, hablantes, dimensión)
        return self._original(ondas[:1], masks=masks[None])[0]


def _acelerar(tuberia) -> None:
    """Agrupa en cada lote las máscaras de un mismo fragmento."""
    try:
        hablantes = len(tuberia._segmentation.model.specifications.classes)
        tuberia._embedding = HuellasSinRepetir(tuberia._embedding)
        tuberia.embedding_batch_size = hablantes
    except AttributeError:
        # Otra versión de pyannote: se trabaja igual, solo que más despacio.
        pass


def _trabajador(conexion, texto_ruta: str, num_hablantes: int | None) -> None:
    """Se ejecuta en el proceso hijo."""
    # Debe ocurrir antes de importar pyannote: su telemetría viene activada.
    os.environ["PYANNOTE_METRICS_ENABLED"] = "0"
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    try:
        conexion.send(("estado", "Cargando el modelo de diarización"))
        import warnings

        import torch
        from pyannote.audio import Pipeline

        # Aviso inofensivo de pyannote al medir el audio mínimo admitido.
        warnings.filterwarnings("ignore", message=r"std\(\): degrees of freedom")
        torch.set_num_threads(max(1, (os.cpu_count() or 2) // 2))
        tuberia = Pipeline.from_pretrained(CARPETA)
        if tuberia is None:
            raise RuntimeError("pyannote no pudo cargar el modelo")
        _acelerar(tuberia)

        audio = leer_wav(Path(texto_ruta))
        conexion.send(("estado", "Separando hablantes"))
        t0 = time.perf_counter()

        def progreso(etapa, *_args, total=None, completed=None, **_kw):
            if etapa in ETAPAS and total:
                a, b = ETAPAS[etapa]
                conexion.send(("progreso", a + (b - a) * min(1.0, completed / total)))

        # El audio va en memoria: así no interviene torchcodec, que en Windows
        # depende de la versión de FFmpeg instalada.
        salida = tuberia(
            {"waveform": torch.from_numpy(audio)[None], "sample_rate": FRECUENCIA_WHISPER},
            num_speakers=num_hablantes,
            hook=progreso,
        )
        # La versión exclusiva no solapa turnos: cada palabra tendrá un hablante.
        turnos = [
            Turno(round(s.start, 3), round(s.end, 3), str(h))
            for s, _, h in salida.exclusive_speaker_diarization.itertracks(yield_label=True)
        ]
        conexion.send(("resultado", Diarizacion(
            turnos, num_hablantes, round(time.perf_counter() - t0, 2)
        )))
        conexion.send(("fin", None))
    except ErrorTranscripcion as e:
        conexion.send(("error_usuario", str(e)))
    except BaseException as e:
        conexion.send(("error", f"{type(e).__name__}: {e}"))
    finally:
        conexion.close()


def diarizar(
    wav: Path,
    num_hablantes: int | None = None,
    al_estado: Callable[[str], None] = lambda _t: None,
    al_progreso: Callable[[float], None] = lambda _p: None,
    cancelar: threading.Event | None = None,
) -> Diarizacion:
    comprobar(num_hablantes)
    try:
        audio = leer_wav(wav)
    except ErrorTranscripcion as e:
        raise ErrorDiarizacion(str(e)) from e
    duracion = len(audio) / FRECUENCIA_WHISPER
    if duracion < 0.5 or float(np.abs(audio).max()) < PICO_SILENCIO:
        return Diarizacion([], num_hablantes, aviso="La pista está vacía o en silencio.")
    del audio
    if num_hablantes == 1:
        return Diarizacion([Turno(0.0, round(duracion, 3), "SPEAKER_00")], 1)
    try:
        resultados = proceso.ejecutar(
            _trabajador, (str(wav), num_hablantes), al_estado, al_progreso, cancelar
        )
    except proceso.FalloProceso as e:
        log.error("Falló la diarización: %s", e)
        raise ErrorDiarizacion(f"No se pudo separar a los hablantes: {proceso.explicar(str(e))}") from e
    except proceso.ErrorUsuario as e:
        raise ErrorDiarizacion(str(e)) from e
    d = resultados[0]
    if not d.turnos:
        d.aviso = "No se detectó voz en la pista del sistema."
    return d


def guardar(d: Diarizacion, wav: Path) -> Path:
    destino = wav.with_suffix(".diarizacion.json")
    try:
        destino.write_text(json.dumps(asdict(d), indent=1, ensure_ascii=False), encoding="utf-8")
    except OSError as e:
        raise ErrorDiarizacion(f"No se pudo guardar {destino}: {e}") from e
    return destino
