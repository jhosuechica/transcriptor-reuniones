# GPU y ejecutable

Dos pasos opcionales. Todos los comandos se ejecutan desde la raíz del proyecto
y suponen hecha la instalación del [README](../README.md#puesta-en-marcha).

## Usar la GPU

La separación de voces siempre usa la CPU.

**NVIDIA.** CTranslate2 detecta CUDA si están instaladas las librerías que
indica [su documentación](https://opennmt.net/CTranslate2/hardware_support.html).
No se ha probado en este proyecto.

**AMD.** Se probó con una RX 7600 XT. Primero hay que obtener el motor compilado
para ROCm:

1. Descarga `rocm-python-wheels-Windows.zip` de la
   [versión 4.9.1 de CTranslate2](https://github.com/OpenNMT/CTranslate2/releases/tag/v4.9.1).
2. Copia el archivo `ctranslate2-4.9.1-cp312-cp312-win_amd64.whl` que contiene en
   una carpeta `vendor` dentro del proyecto.
3. Instálalo junto con las librerías de ROCm, que ocupan más de 1 GB:

```powershell
.\.venv\Scripts\python.exe -m pip install --no-deps --force-reinstall -r requirements-amd.txt
```

Este comando comprueba si se detecta la GPU; debe mostrar `1` o más:

```powershell
.\.venv\Scripts\python.exe -c "import ctranslate2; print(ctranslate2.get_cuda_device_count())"
```

---

## Crear el ejecutable

Genera una carpeta que funciona sin Python, con `Transcriptor.exe` y
`transcriptor-cli.exe`:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
```

```powershell
.\.venv\Scripts\python.exe construir.py
```

Se construye aparte y la versión anterior se reemplaza al final. Si en ese momento
está abierta, se conservan las dos; después de cerrarla basta con:

```powershell
.\.venv\Scripts\python.exe construir.py --cambiar
```

El resultado queda en `dist\Transcriptor`. Hay que conservar la carpeta entera:
el `.exe` solo no funciona. Con las librerías de AMD ocupa 1,29 GB, sin contar
los modelos. Esas librerías se incluyen únicamente para la GPU indicada en
`transcriptor.spec` (`gfx1102`); en otra tarjeta AMD hay que cambiar ese valor.

El ejecutable guarda su configuración en `%LOCALAPPDATA%\TranscriptorReuniones`.
