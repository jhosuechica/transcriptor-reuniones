# Transcriptor de reuniones local: graba, transcribe y separa hablantes sin nube

[![Python](https://img.shields.io/badge/Python-3.12-3776AB)](https://www.python.org/)
[![Windows](https://img.shields.io/badge/Windows-11-0078D4)](https://www.microsoft.com/windows)
[![faster-whisper](https://img.shields.io/badge/faster--whisper-1.2.1-4B8BBE)](https://github.com/SYSTRAN/faster-whisper)
[![pyannote.audio](https://img.shields.io/badge/pyannote.audio-4.1.1-6A5ACD)](https://github.com/pyannote/pyannote-audio)
[![License](https://img.shields.io/badge/license-MIT-blue)](LICENSE)

Aplicación de escritorio para Windows que graba reuniones de Teams, Zoom, Meet o
cualquier otro programa, las transcribe en español e inglés y distingue quién habla. El
resultado se exporta a `.txt`, `.docx` y `.srt`.

**Todo ocurre en el equipo.** Ningún bot entra en la llamada y el audio no sale
del ordenador: solo se usa Internet para descargar los modelos, una vez.

La idea que lo articula es grabar **dos pistas separadas y sincronizadas**, el
micrófono por un lado y el sonido del equipo por otro. Así se sabe con certeza
qué dijo quien graba, y el reconocimiento de voces solo tiene que resolver al
resto de participantes.

![Ventana principal con una reunión transcrita](docs/img/ventana.png)

---

## Tabla de contenido

- [Antes de grabar: consentimiento](#antes-de-grabar-consentimiento)
- [Puesta en marcha](#puesta-en-marcha)
- [Uso de la aplicación](#uso-de-la-aplicación)
- [Qué archivos genera](#qué-archivos-genera)
- [Consola y configuración](#consola-y-configuración)
- [GPU y ejecutable](#gpu-y-ejecutable)
- [Solución de problemas](#solución-de-problemas)
- [Cómo funciona](#cómo-funciona)
- [Rendimiento medido](#rendimiento-medido)
- [Pruebas](#pruebas)
- [Estructura del repositorio](#estructura-del-repositorio)
- [Limitaciones conocidas](#limitaciones-conocidas)
- [Autor](#autor)

---

## Antes de grabar: consentimiento

Grabar una conversación sin avisar puede ser ilegal según el país. La aplicación
muestra un recordatorio antes de cada grabación, pero la responsabilidad de
informar a los participantes y obtener su permiso es de quien graba.

Las grabaciones y las transcripciones se guardan **sin cifrar**. Conviene
tenerlas en un disco protegido, por ejemplo con BitLocker.

---

## Puesta en marcha

**Requisitos:** Windows 11, [Python 3.12](https://www.python.org/downloads/),
[Git](https://git-scm.com/) y una cuenta gratuita de
[Hugging Face](https://huggingface.co/join). Todos los comandos son de
PowerShell.

### 1. Instalar

```powershell
git clone https://github.com/jhosuechica/transcriptor-reuniones.git
```

```powershell
cd transcriptor-reuniones
```

Se crea un entorno aislado para no mezclar las librerías con las del sistema:

```powershell
py -3.12 -m venv .venv
```

La instalación descarga las librerías, entre ellas PyTorch, y tarda unos
minutos:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

### 2. Descargar un modelo de transcripción

`small` es el más rápido y sirve para empezar en cualquier equipo:

```powershell
.\.venv\Scripts\python.exe cli.py descargar small
```

Con una GPU compatible, `large-v3` da la mejor calidad y ocupa unos 3 GB. También hay `medium` y `large-v3-turbo`:

```powershell
.\.venv\Scripts\python.exe cli.py descargar large-v3
```

### 3. Descargar el modelo que separa las voces

Este modelo es gratuito, pero exige aceptar sus condiciones:

1. Entra en Hugging Face y acepta las condiciones de
   [speaker-diarization-community-1](https://huggingface.co/pyannote/speaker-diarization-community-1).
2. En [Settings, Access Tokens](https://huggingface.co/settings/tokens), crea un
   token de tipo **Read**.
3. Ejecuta el comando y pega el token cuando lo pida. No se muestra en pantalla:

```powershell
.\.venv\Scripts\python.exe cli.py descargar diarizacion
```

El token solo se usa en esa descarga y no se guarda. Después se puede borrar en
Hugging Face.

### 4. Abrir la aplicación

Con doble clic en `Transcriptor.bat`, o desde la consola:

```powershell
.\Transcriptor.bat
```

En **Ajustes**, elige el micrófono y la salida por la que suena la reunión. Si
se dejan en «Predeterminado de Windows» se usan los del sistema.

---

## Uso de la aplicación

### Grabar

Antes de una reunión importante conviene pulsar **Probar**: escucha el micrófono
y la salida unos segundos, sin guardar nada, y dice si el nivel es correcto.

1. Pulsa **Grabar** o `Ctrl+Alt+R`. El atajo funciona aunque la ventana esté
   minimizada.
2. Escribe un título y confirma que los participantes están avisados.
3. Comprueba que los dos medidores se mueven. Si una pista lleva diez segundos
   en silencio, aparece «Sin señal».
4. Pulsa **Detener** o repite el atajo.

![Aviso de consentimiento antes de grabar](docs/img/consentimiento.png)

Mientras se graba, un indicador rojo permanece sobre las demás ventanas. Se
puede arrastrar, y con doble clic abre la aplicación. Si se comparte la pantalla
completa, los demás lo ven; se desactiva en Ajustes.

![Grabación en curso con los medidores de nivel](docs/img/grabando.png)

Conviene usar **auriculares**: con altavoces, el micrófono capta también a los
demás. Las notificaciones y la música del equipo quedan grabadas.

### Transcribir

1. Elige la grabación en la lista.
2. Si se conoce, indica cuántas personas hablan **sin contar a quien graba**.
   Ayuda a separar las voces.
3. Pulsa **Transcribir**. Se puede cancelar y el audio se conserva. Con
   **Transcribir al terminar de grabar**, el proceso empieza solo.

Si hay otra en curso o se está grabando, la reunión queda **en cola**. Abajo se
estima lo que falta de cada paso, y al terminar avisa en la barra de tareas.

Para repetir con otro número de personas, pulsa **Repetir** y responde «No»: se
separan de nuevo las voces sin volver a transcribir.

### Escuchar y corregir

Con **doble clic** en una frase se oye la reunión desde ese momento, con las dos
pistas mezcladas y la frase en curso resaltada. Un clic solo la selecciona.

**Corregir** abre la frase elegida para cambiar el texto o la persona, y permite
avanzar por las siguientes oyendo cada una. Con el botón derecho se puede además
reasignar o borrar una frase.

![Diálogo para corregir una frase](docs/img/corregir.png)

| Tecla, sobre el texto | Acción |
|---|---|
| Espacio | Escuchar o pausar |
| Flechas izquierda y derecha | Retroceder o avanzar 5 segundos |
| F2 | Corregir la frase |
| Ctrl+Z | Deshacer |
| Ctrl+Intro, en el diálogo | Guardar y pasar a la siguiente |

Cada corrección se guarda al momento y los archivos exportados se actualizan a
los pocos segundos. **Repetir** la transcripción no conserva las correcciones,
pero guarda antes una copia del documento corregido.

### Ordenar las grabaciones

**Cambiar título** (F2) renombra también la carpeta y los archivos exportados.
**Borrar** (Supr) la mueve a la papelera. No se puede mientras está en uso.

### Poner nombres y exportar

El botón **Nombres** muestra cuánto habla cada persona y dos frases suyas, para
reconocerla. Al guardar, el nombre cambia en los tres archivos. Si la aplicación
dividió a una persona en dos, basta con poner el mismo nombre a ambas.

![Diálogo para poner nombre a los hablantes](docs/img/nombres.png)

Los archivos se crean al transcribir y **Exportar** los regenera. Si alguno se
editó a mano, esa versión se conserva como `transcripcion (editado fecha)`.

---

## Qué archivos genera

Cada reunión tiene su carpeta en `Documents\Reuniones`, con el audio de las dos
pistas, la transcripción en tres formatos y los datos intermedios, detallados en
[docs/CONSOLA-Y-CONFIGURACION.md](docs/CONSOLA-Y-CONFIGURACION.md#archivos-de-una-reunión).

El texto sigue este formato:

```
[00:00:08] Luis: El módulo de autenticación ya está terminado.
[00:00:14] Yo: Perfecto. Yo estoy con la base de datos.
```

---

## Consola y configuración

Todo lo que hace la ventana se puede hacer también por comandos. La lista, junto
con las claves de `config.json`, está en
[docs/CONSOLA-Y-CONFIGURACION.md](docs/CONSOLA-Y-CONFIGURACION.md).

---

## GPU y ejecutable

Sin GPU la aplicación funciona igual, solo que más despacio y con un modelo
menor. Para usar una tarjeta AMD o NVIDIA, o para crear un `.exe` que funcione
sin Python, se siguen los pasos de
[docs/GPU-Y-EJECUTABLE.md](docs/GPU-Y-EJECUTABLE.md).

---

## Solución de problemas

| Síntoma | Qué hacer |
|---|---|
| Graba otro micrófono | Elegirlo en Ajustes; `cli.py dispositivos` los lista |
| La pista del sistema sale vacía | La reunión sonaba por otra salida: elegirla en Ajustes |
| El sonido distorsiona o casi no se oye | Pulsar **Probar** y seguir el consejo |
| No se encuentra ningún micrófono | Revisar Privacidad y seguridad, Micrófono, en Windows |
| El atajo está ocupado | Otro programa lo usa; se cambia en Ajustes |
| Mis frases aparecen repetidas | Los altavoces entran por el micrófono: usar auriculares |
| Confunde a las personas | Indicar cuántas hay y pulsar **Repetir** |
| No se puede exportar el `.docx` | Está abierto en Word: cerrarlo y exportar de nuevo |
| Falla la GPU | La aplicación pasa sola a la CPU; el motivo queda en el registro |

El registro está en `logs\app.log`, o en
`%LOCALAPPDATA%\TranscriptorReuniones\logs` si se usa el ejecutable.

---

## Cómo funciona

```
micrófono ──► mic.wav ──────► transcripción ──────────────────────┐
                                                                  ├─► unión ─► exportación
sistema ────► sistema.wav ──► transcripción + separación de voces ┘
```

1. **Grabación.** WASAPI captura el micrófono y, en modo *loopback*, lo que suena
   por los altavoces. Un reloj común coloca cada bloque en su instante: se
   rellenan los huecos y se corrige la deriva entre dispositivos.
2. **Transcripción.** faster-whisper procesa cada pista en un proceso aparte,
   decidiendo el idioma de cada tramo y con marcas de tiempo por palabra.
3. **Separación de voces.** pyannote analiza solo la pista del sistema, en CPU y
   con su telemetría desactivada.
4. **Unión.** Cada palabra recibe un hablante y los cambios de persona se llevan al
   final de frase más próximo. Se ordenan las dos pistas y se descarta el eco.

---

## Rendimiento medido

Equipo: Ryzen 7 5700X, 32 GB de RAM y RX 7600 XT de 16 GB, con Windows 11.

| Medida | Resultado |
|---|---|
| `large-v3` en GPU, español e inglés | 1084 s de audio en 199 s: 5,4 veces el tiempo real |
| `large-v3` en GPU, solo español | 1084 s de audio en 149 s: 7,3 veces el tiempo real |
| `small` en CPU, ambos idiomas o solo español | 2,5 y 3,4 veces el tiempo real |
| Palabras correctas alternando idiomas | 117 de 120 con `large-v3` |
| Separación de voces en CPU | 1084 s de audio en 154 s: 7 veces el tiempo real |
| Hablante correcto, diálogo sintético de 3 voces | 136 de 144 palabras (94,4 %) |
| Hablante correcto, reunión real de 4 personas | 1777 de 1811 palabras (98,1 %) |

Salvo la reunión real, son **voces sintéticas y limpias**, con el modelo ya cargado.
La real es ES2004a del [corpus AMI](https://groups.inf.ed.ac.uk/ami/corpus/): 17
minutos en inglés con micrófonos de diadema, sin indicar cuántas personas había. De
ella se transcribe el 85,1 % de las palabras dichas sin solape ni muletillas.

Se vigiló la red durante el proceso completo y no se detectó ninguna conexión.

---

## Pruebas

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests
```

Son 216 y no necesitan modelos, micrófono ni GPU. Cubren la alineación de las
pistas, los reintentos, la asignación de hablantes, los archivos exportados, el
reproductor y la ventana, que se abre de verdad con el audio simulado. Durante
unos segundos aparecen ventanas en pantalla.

La calidad de la transcripción no se prueba de forma automática.

---

## Estructura del repositorio

```
.
├── gui.py                  Ventana; las reglas de hilos están al principio
├── visor.py                Panel para leer, escuchar y corregir
├── componentes.py          Base común de los diálogos
├── cli.py                  Los mismos pasos por consola
├── flujo.py                Lo que comparten ambas: listar y procesar
├── recorder.py             Las dos pistas y su sincronización
├── transcriber.py          Whisper, con una escalera de intentos GPU y CPU
├── diarizer.py             Separación de voces
├── merger.py               Une las pistas y gestiona los nombres
├── exporter.py             Archivos finales, sin perder ediciones a mano
├── reproductor.py          Suena en el hilo de audio, con las pistas mezcladas
├── proceso.py              Ejecuta el trabajo pesado en otro proceso
├── windows.py              Atajo global, instancia única y suspensión
├── config.py               Configuración, rutas y registro
├── construir.py            Genera el ejecutable con un PATH limpio
├── transcriptor.spec       Qué entra en el ejecutable y qué no
├── Transcriptor.bat        Abre la aplicación sin consola
├── requirements*.txt       Versiones fijadas: base, GPU AMD y ejecutable
├── recursos/               Icono
├── tests/                  Una suite por módulo principal
└── docs/
    ├── CONSOLA-Y-CONFIGURACION.md Comandos y claves
    ├── GPU-Y-EJECUTABLE.md Pasos opcionales, fuera del README por extensión
    └── img/                Capturas, con datos inventados
```

---

## Limitaciones conocidas

- **Solo funciona en Windows.** La captura usa WASAPI y el atajo, la API de
  Windows. Se ha probado únicamente en Windows 11 con Python 3.12.
- **La separación de voces se ha medido con una sola reunión real,** en inglés y
  bien grabada. Encontró 5 personas en lugar de 4; indicar el número lo corrige.
- **Fallan las intervenciones de una o dos palabras** (11 de 21) y los solapes.
- **Nadie puede compartir el micrófono.** Todo lo que entra por él se atribuye a
  quien graba.
- **Solo español e inglés.** Las partes en otro idioma salen mal. Un cambio de
  idioma sin pausa, o una palabra suelta, puede quedar en el idioma equivocado.
- **Se graba todo lo que suena en el equipo.** Incluye notificaciones y música;
  no se aísla una aplicación concreta.
- **La GPU se aprovecha a medias:** no separa voces y usa un cálculo más lento
  porque el rápido devolvía texto corrupto en la tarjeta probada.
- **Las correcciones no sobreviven a una nueva transcripción.** Se guarda una
  copia, pero no hay forma de aplicarlas al documento nuevo ni de restaurarla
  desde la ventana.
- **No se puede escuchar mientras se graba:** quedaría en la grabación.
- **Los subtítulos largos tienen tiempos aproximados.** Al dividir una frase, el
  tiempo se reparte según la longitud del texto.
- **El audio ocupa unos 0,7 GB por hora.** No se comprime ni se borra solo; si el
  disco está casi lleno, la grabación se detiene y se guarda.
- **No hay integración continua ni ejecutable publicado.** Las pruebas se
  ejecutan en local y el `.exe` hay que construirlo.
- **La licencia MIT cubre solo este código,** no los modelos ni las librerías.

---

## Autor

**Jhosué Chica**, Desarrollador de Software
[LinkedIn](https://www.linkedin.com/in/jhosue-chica-0811a933a) · [GitHub](https://github.com/jhosuechica)
