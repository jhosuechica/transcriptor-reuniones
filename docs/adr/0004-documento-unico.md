# ADR-0004 · Generar los archivos desde un documento único

- **Estado:** Aceptada
- **Fecha:** 2026-11-24
- **Aplica a:** `merger.py`, `exporter.py`, `visor.py`

## Contexto

La separación de voces produce etiquetas como «Hablante 1» que después se
cambian por nombres reales, en tres formatos distintos. Alguien puede decir
«Hablante 1» en la reunión, dos etiquetas pueden ser la misma persona y el
`.docx` puede haberse corregido en Word.

## Decisión

Guardar la transcripción en `transcripcion.json`, donde cada intervención lleva
un identificador interno (`yo`, `h1`, `h2`) y una tabla lo traduce al nombre
visible. Renombrar cambia esa tabla y vuelve a generar los archivos.

Las correcciones hechas en la ventana se escriben en ese mismo documento, que
queda marcado como corregido; volver a procesar la reunión guarda antes una copia.

Antes de reemplazar un archivo se compara su huella SHA-256 con la de la última
exportación. Si no coinciden, se guarda una copia de la versión editada.

## Alternativas consideradas

| Alternativa | Por qué se descartó |
|---|---|
| Buscar y reemplazar en los archivos | Cambiaría también lo que alguien dijo y no permite deshacer la unión de dos hablantes |
| Una base de datos SQLite | Mejor opción para buscar entre muchas reuniones; con un documento por carpeta, copiar o borrar una reunión es copiar o borrar su carpeta |
| No reemplazar nunca un archivo editado | Evita las copias, pero los nombres nuevos no llegarían a ese archivo |

## Consecuencias

**Positivas:** renombrar es reversible, los tres formatos no pueden discrepar al
generarse y añadir otro formato es añadir una función.

**Negativas:** lo corregido a mano en Word no vuelve al documento, solo se
conserva en la copia. Abrir y guardar sin cambios también genera una.

**A vigilar:** si se vuelve habitual repetir reuniones ya corregidas, habría que
trasladar las correcciones al documento nuevo en lugar de guardar una copia.
