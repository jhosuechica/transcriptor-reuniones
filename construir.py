"""Construye el .exe en dist/Transcriptor.

    .\\.venv\\Scripts\\python.exe -m pip install -r requirements-dev.txt
    .\\.venv\\Scripts\\python.exe construir.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent
DESTINO = RAIZ / "dist" / "Transcriptor"


def enlazar(origen: Path, destino: Path) -> None:
    """Pone los modelos junto al .exe sin duplicar el espacio que ocupan.

    Un enlace duro es otro nombre para el mismo archivo: sigue valiendo si se
    borra el original. En otro disco no es posible y se copia.
    """
    for f in origen.rglob("*"):
        partes = f.relative_to(origen).parts
        if not f.is_file() or ".cache" in partes:
            continue
        nuevo = destino.joinpath(*partes)
        nuevo.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(f, nuevo)
        except OSError:
            shutil.copy2(f, nuevo)


def main() -> int:
    # PyInstaller busca DLL en el PATH. Con el de siempre incluyó las de un Java
    # antiguo, y CTranslate2 se cerraba al arrancar: se deja solo Windows.
    windows = os.environ["SystemRoot"]
    entorno = os.environ | {"PATH": f"{windows}/System32;{windows}".replace("/", os.sep)}
    codigo = subprocess.call(
        [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--log-level", "WARN",
         "--distpath", str(RAIZ / "dist"), "--workpath", str(RAIZ / "build"),
         str(RAIZ / "transcriptor.spec")], cwd=RAIZ, env=entorno)
    if codigo:
        return codigo
    if (RAIZ / "modelos").exists():
        enlazar(RAIZ / "modelos", DESTINO / "modelos")

    # La primera vez, el .exe hereda la configuración que ya se usaba.
    datos = Path(os.environ["LOCALAPPDATA"]) / "TranscriptorReuniones"
    actual = RAIZ / "config.json"
    if actual.exists() and not (datos / "config.json").exists():
        datos.mkdir(parents=True, exist_ok=True)
        shutil.copy2(actual, datos / "config.json")
        print("Configuración copiada a", datos)
    total = sum(f.stat().st_size for f in DESTINO.rglob("*")
                if f.is_file() and "modelos" not in f.relative_to(DESTINO).parts)
    print(f"Listo: {DESTINO / 'Transcriptor.exe'} ({total / 1e9:.2f} GB sin los modelos)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
