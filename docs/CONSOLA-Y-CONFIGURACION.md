# Consola y configuración

Los comandos se ejecutan desde la raíz del proyecto, por ejemplo
`.\.venv\Scripts\python.exe cli.py procesar --hablantes 3`.

## Uso desde la consola

Todo lo que hace la ventana se puede hacer por comandos. Si no se indica una
carpeta, se usa la última grabación.

| Comando | Qué hace |
|---|---|
| `cli.py dispositivos` | Lista micrófonos y salidas; marca los elegidos |
| `cli.py grabar --titulo "Sprint"` | Graba hasta pulsar Ctrl+C |
| `cli.py verificar` | Analiza una grabación: niveles, duración y saturación |
| `cli.py procesar` | Transcribe, separa voces, une y exporta |
| `cli.py procesar --hablantes 3` | Igual, indicando las personas |
| `cli.py renombrar` | Pregunta los nombres; con `h1=Ana` cambia uno directamente |
| `cli.py exportar` | Vuelve a crear los tres archivos |

---

## Configuración

La ventana cubre lo habitual. Los valores se guardan en `config.json`, que se
crea la primera vez que se abre la aplicación:

| Clave | Valor inicial | Para qué sirve |
|---|---|---|
| `nombre_usuario` | `Yo` | Nombre de la pista del micrófono |
| `carpeta_reuniones` | `Documents\Reuniones` | Dónde se guarda todo |
| `microfono`, `salida` | vacío | Parte del nombre del dispositivo |
| `modelo` | `auto` | Modelo de transcripción |
| `dispositivo` | `auto` | `auto`, `gpu` o `cpu` |
| `vocabulario` | vacío | Nombres y términos frecuentes, separados por comas |
| `atajo` | `ctrl+alt+r` | Letra, número o F1 a F24, con ctrl o alt |
| `indicador_flotante` | `true` | Indicador rojo al grabar |
| `transcribir_al_terminar` | `false` | Procesar al detener |

Con `auto` se usa el mejor modelo descargado si hay GPU y el más rápido si no.
