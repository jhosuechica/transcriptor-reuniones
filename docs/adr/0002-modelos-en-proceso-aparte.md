# ADR-0002 · Ejecutar los modelos en un proceso aparte

- **Estado:** Aceptada
- **Fecha:** 2026-11-23
- **Aplica a:** `proceso.py`, `transcriber.py`, `diarizer.py`

## Contexto

Transcribir dos horas ocupa varios gigabytes de memoria y muchos minutos. El
código nativo de la GPU puede cerrar el proceso que lo ejecuta sin lanzar una
excepción de Python, o dejar de responder. Si eso ocurriera en el proceso de la
ventana, se perdería una grabación en curso.

## Decisión

Lanzar cada intento de transcripción y la separación de voces en un proceso
nuevo, con prioridad inferior a la normal, que se comunica mediante mensajes. El
proceso principal detecta el cierre inesperado y la falta de respuesta, y prueba
la siguiente configuración: GPU por lotes, GPU sin lotes y CPU con un modelo
menor.

## Alternativas consideradas

| Alternativa | Por qué se descartó |
|---|---|
| Un hilo en el mismo proceso | Es más sencillo y evita recargar los modelos; un fallo nativo cerraría la aplicación y la memoria de la GPU no siempre se libera |
| Un servicio local permanente | Mantendría el modelo cargado, mejor opción si se transcribiera a menudo; aquí ocuparía la memoria de la GPU todo el día para usarla unas pocas veces |

## Consecuencias

**Positivas:** un fallo de la GPU se convierte en un reintento, la memoria se
libera al terminar y cancelar equivale a cerrar el proceso.

**Negativas:** el modelo se carga en cada reunión, unos segundos, y todo lo que
cruza entre procesos debe poder serializarse. El ejecutable necesita cuidado
especial para que los procesos hijos arranquen.

**A vigilar:** si se añade una cola de reuniones y el tiempo de carga supera al
de transcripción en grabaciones cortas, convendría reutilizar el proceso.
