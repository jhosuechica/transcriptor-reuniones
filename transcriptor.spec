# Receta de PyInstaller. Se usa desde construir.py.
#
# Genera una carpeta con dos programas que comparten las librerías:
#   Transcriptor.exe        la ventana
#   transcriptor-cli.exe    los comandos de consola (descargar modelos, etc.)
# En un único archivo, cada arranque descomprimiría varios cientos de MB.

import sysconfig
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

PAQUETES = Path(sysconfig.get_paths()["purelib"])

# GPU cuyas librerías se incluyen: las de todas las tarjetas ocupan varios GB.
# gfx1102 = RX 7600 y 7600 XT. En otra tarjeta se usaría la CPU.
GPU = "gfx1102"
DLL_ROCM = {
    "_rocm_sdk_core": ("amdhip64_7.dll", "amd_comgr0702.dll", "hiprtc0702.dll",
                       "hiprtc-builtins0702.dll"),
    "_rocm_sdk_libraries_custom": ("hipblas.dll", "rocblas.dll", "rocsolver.dll",
                                   "libhipblaslt.dll"),
}

ICONO = "recursos/icono.ico"

datos, binarios, ocultos = [(ICONO, "recursos")], [], []

# CTranslate2 busca sus DLL dentro de su propio paquete y, las de ROCm, en las
# carpetas vecinas con este mismo nombre. Se copian tal cual, como datos, para
# que PyInstaller no las mueva: rocblas busca sus archivos junto a su DLL.
datos += [(str(d), "ctranslate2") for d in (PAQUETES / "ctranslate2").glob("*.dll")]
for paquete, nombres in DLL_ROCM.items():
    carpeta = PAQUETES / paquete / "bin"
    if not carpeta.exists():
        continue        # instalación con NVIDIA o solo CPU
    datos += [(str(carpeta / n), f"{paquete}/bin") for n in nombres]
    for libreria in ("rocblas", "hipblaslt"):
        raiz = carpeta / libreria
        for f in raiz.rglob("*"):
            if f.is_file() and ("gfx" not in f.name or GPU in f.name):
                datos.append((str(f), f"{paquete}/bin/{f.parent.relative_to(carpeta).as_posix()}"))

datos += collect_data_files("faster_whisper")       # detector de voz
datos += collect_data_files("pyannote.audio", excludes=["**/*.wav", "**/*.ipynb"])
# Consultan su propia versión, o la de otros paquetes, al importarse.
datos += copy_metadata("pyannote.audio", recursive=True)
# pyannote elige sus clases por el nombre que aparece en la configuración del modelo.
for p in ("pyannote.audio", "pyannote.core", "pyannote.database", "pyannote.metrics",
          "pyannote.pipeline"):
    ocultos += collect_submodules(p, filter=lambda n: ".cli" not in n)

# Se importan dentro de funciones, en los procesos hijos.
ocultos += ["faster_whisper", "pyannote.audio", "torch", "pyaudiowpatch"]

# No se usan: el audio se pasa a pyannote ya cargado en memoria.
EXCLUIDOS = ["torchcodec", "IPython", "jupyter", "notebook", "pytest", "tensorboard"]


def analizar(programa):
    a = Analysis(
        [programa], pathex=[SPECPATH], binaries=binarios, datas=datos,
        hiddenimports=ocultos, excludes=EXCLUIDOS, noarchive=False,
    )
    # El análisis puede añadir otra copia suelta de estas DLL, fuera de su carpeta.
    # Las que van dentro de otros paquetes (PyTorch trae las suyas) se respetan.
    propias = {n for nombres in DLL_ROCM.values() for n in nombres}
    propias |= {d.name for d in (PAQUETES / "ctranslate2").glob("*.dll")}
    a.binaries = [b for b in a.binaries if b[0] not in propias]
    return a


def programa(a, nombre, consola):
    return EXE(PYZ(a.pure), a.scripts, [], exclude_binaries=True, name=nombre,
               console=consola, icon=ICONO, upx=False)


ventana = analizar("gui.py")
terminal = analizar("cli.py")
COLLECT(
    programa(ventana, "Transcriptor", False), ventana.binaries, ventana.datas,
    programa(terminal, "transcriptor-cli", True), terminal.binaries, terminal.datas,
    upx=False, name="Transcriptor",
)
