"""Configuración de la aplicación y registro de eventos (logging)."""

from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import asdict, dataclass, fields
from logging.handlers import RotatingFileHandler
from pathlib import Path

# PyInstaller marca así el programa empaquetado.
EMPAQUETADO = getattr(sys, "frozen", False)
# Carpeta del programa: la del .exe o la del código. Aquí está `modelos`.
RAIZ = Path(sys.executable if EMPAQUETADO else __file__).resolve().parent
# Configuración y registro. El .exe los guarda en la carpeta del usuario: la suya
# puede ser de solo lectura y se reemplaza entera al volver a construirlo.
DATOS = (
    Path(os.environ.get("LOCALAPPDATA", Path.home())) / "TranscriptorReuniones"
    if EMPAQUETADO else RAIZ
)
CARPETA_MODELOS = RAIZ / "modelos"
RUTA_CONFIG = DATOS / "config.json"
RUTA_LOG = DATOS / "logs" / "app.log"

# Formato común de las dos pistas: PCM de 16 bits, mono, 48 kHz.
FRECUENCIA = 48_000

log = logging.getLogger(__name__)


@dataclass
class Config:
    # Nombre con el que se etiqueta la pista del micrófono.
    nombre_usuario: str = "Yo"
    # Carpeta raíz donde se crea una subcarpeta por reunión.
    carpeta_reuniones: str = str(Path.home() / "Documents" / "Reuniones")
    # Parte del nombre del micrófono. Vacío = predeterminado de Windows.
    microfono: str = ""
    # Parte del nombre de la salida que se graba. Vacío = predeterminada.
    salida: str = ""
    # Modelo de Whisper: auto, small, medium, large-v3-turbo o large-v3.
    modelo: str = "auto"
    # Dónde transcribir: auto, gpu o cpu.
    dispositivo: str = "auto"
    # Idioma de las reuniones: auto (español e inglés), es o en.
    idioma: str = "auto"
    # Términos técnicos y nombres que suelen aparecer, separados por comas.
    vocabulario: str = ""
    # Atajo global para grabar y detener.
    atajo: str = "ctrl+alt+r"
    # Muestra un indicador sobre las demás ventanas mientras se graba.
    indicador_flotante: bool = True
    # Procesa la reunión en cuanto se detiene la grabación.
    transcribir_al_terminar: bool = False


def comando(argumentos: str) -> str:
    """Texto del comando de consola que debe ejecutar el usuario."""
    return f"{'.\\transcriptor-cli.exe' if EMPAQUETADO else 'python cli.py'} {argumentos}"


def cargar(ruta: Path = RUTA_CONFIG) -> Config:
    """Lee config.json; si no existe, lo crea con los valores por defecto."""
    if not ruta.exists():
        cfg = Config()
        guardar(cfg, ruta)
        return cfg
    try:
        # utf-8-sig acepta también archivos guardados con BOM por el Bloc de notas.
        datos = json.loads(ruta.read_text(encoding="utf-8-sig"))
        if not isinstance(datos, dict):
            raise ValueError("se esperaba un objeto JSON")
    except (ValueError, OSError) as e:
        # No se sobrescribe el archivo: puede tener un error fácil de corregir.
        raise RuntimeError(f"No se pudo leer {ruta}: {e}") from e
    conocidas = {f.name for f in fields(Config)}
    for clave in datos.keys() - conocidas:
        log.warning("Clave desconocida en config.json: %s", clave)
    return Config(**{k: v for k, v in datos.items() if k in conocidas})


def guardar(cfg: Config, ruta: Path = RUTA_CONFIG) -> None:
    ruta.parent.mkdir(parents=True, exist_ok=True)
    ruta.write_text(
        json.dumps(asdict(cfg), indent=2, ensure_ascii=False), encoding="utf-8"
    )


def configurar_logging() -> None:
    """Envía el registro a logs/app.log (3 archivos de 1 MB como máximo)."""
    RUTA_LOG.parent.mkdir(parents=True, exist_ok=True)
    manejador = RotatingFileHandler(
        RUTA_LOG, maxBytes=1_000_000, backupCount=2, encoding="utf-8"
    )
    manejador.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s [%(threadName)s] %(name)s: %(message)s")
    )
    raiz = logging.getLogger()
    raiz.setLevel(logging.INFO)
    raiz.addHandler(manejador)
