"""Construye el .exe en dist/Transcriptor.

    .\\.venv\\Scripts\\python.exe -m pip install -r requirements-dev.txt
    .\\.venv\\Scripts\\python.exe construir.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

RAIZ = Path(__file__).resolve().parent
DIST = RAIZ / "dist"
DESTINO = DIST / "Transcriptor"
# Se construye aparte: si algo falla, la versión que funciona sigue intacta.
NUEVA = DIST / "nueva" / "Transcriptor"
ANTERIOR = DIST / "anterior"


# El antivirus revisa los archivos recién creados y los bloquea unos segundos.
INTENTOS, ESPERA_S = 30, 2.0


def mover(origen: Path, destino: Path) -> None:
    for n in range(INTENTOS):
        try:
            origen.rename(destino)
            return
        except PermissionError:
            if n == INTENTOS - 1:
                raise
            time.sleep(ESPERA_S)


def en_uso(carpeta: Path) -> bool:
    """Windows no deja mover una carpeta con programas o archivos abiertos."""
    if not carpeta.exists():
        return False
    prueba = carpeta.with_name(carpeta.name + ".prueba")
    try:
        carpeta.rename(prueba)
    except OSError:
        return True
    prueba.rename(carpeta)
    return False


def cambiar() -> int:
    """Pone la versión recién construida en lugar de la anterior."""
    if not (NUEVA / "Transcriptor.exe").exists():
        print("No hay ninguna versión nueva. Ejecuta: python construir.py")
        return 1
    shutil.rmtree(ANTERIOR, ignore_errors=True)
    if ANTERIOR.exists():
        print(f"No se pudo borrar {ANTERIOR}. Bórrala a mano y ejecuta: "
              "python construir.py --cambiar")
        return 1
    try:
        if DESTINO.exists():
            mover(DESTINO, ANTERIOR)
        try:
            mover(NUEVA, DESTINO)
        except OSError:
            if ANTERIOR.exists():
                mover(ANTERIOR, DESTINO)    # se deja todo como estaba
            raise
    except OSError as e:
        print(f"No se pudo reemplazar la versión anterior, que sigue igual ({e}). Cierra la "
              "aplicación, si está abierta, y ejecuta: python construir.py --cambiar")
        return 1
    shutil.rmtree(ANTERIOR, ignore_errors=True)
    shutil.rmtree(NUEVA.parent, ignore_errors=True)
    return 0


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
    if "--cambiar" in sys.argv[1:]:
        return cambiar()
    if en_uso(DESTINO):
        print("La aplicación está abierta. Ciérrala antes de construir.")
        return 1
    # PyInstaller busca DLL en el PATH. Con el de siempre incluyó las de un Java
    # antiguo, y CTranslate2 se cerraba al arrancar: se deja solo Windows.
    windows = os.environ["SystemRoot"]
    entorno = os.environ | {"PATH": f"{windows}/System32;{windows}".replace("/", os.sep)}
    codigo = subprocess.call(
        [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--log-level", "WARN",
         "--distpath", str(NUEVA.parent), "--workpath", str(RAIZ / "build"),
         str(RAIZ / "transcriptor.spec")], cwd=RAIZ, env=entorno)
    if codigo:
        return codigo
    if (RAIZ / "modelos").exists():
        enlazar(RAIZ / "modelos", NUEVA / "modelos")

    if cambiar():
        return 1

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
