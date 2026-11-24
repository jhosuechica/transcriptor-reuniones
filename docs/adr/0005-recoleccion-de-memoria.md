# ADR-0005 · Recoger la memoria solo en el hilo de la ventana

- **Estado:** Aceptada
- **Fecha:** 2026-11-24
- **Aplica a:** `gui.py`

## Contexto

El recolector de Python se ejecuta en el hilo que en ese momento crea objetos.
Si encuentra restos de una ventana cerrada, llama a sus destructores desde ahí.
Los de Tk, por ejemplo el de una fuente, esperan al hilo de la ventana.

En las pruebas, un hilo que exportaba un `.docx` quedó bloqueado en el
destructor de una fuente. El mismo fallo podía alcanzar al hilo que escribe el
audio y detener una grabación.

## Decisión

Desactivar el recolector automático y ejecutarlo cada cinco segundos en el hilo
de la ventana. Tras construirla se llama a `gc.freeze()`, para que los objetos
que duran toda la sesión no se revisen en cada pasada.

## Alternativas consideradas

| Alternativa | Por qué se descartó |
|---|---|
| Romper a mano los ciclos al cerrar cada diálogo | Es lo más limpio si se controla todo el código; aquí los ciclos están dentro de CustomTkinter |
| Grabar en otro proceso | Aísla la grabación de cualquier fallo de la ventana y es más robusto; añade comunicación entre procesos a la parte más delicada |
| Dejar el recolector activo | El bloqueo depende del momento y aparecería sin aviso en plena reunión |

## Consecuencias

**Positivas:** ningún hilo secundario destruye objetos de la ventana, y cada
pasada revisa pocos objetos.

**Negativas:** la memoria en ciclos se libera con hasta cinco segundos de
retraso. La regla no es visible en el código que la necesita, por lo que se
documenta al principio de `gui.py` y una prueba la comprueba.

**A vigilar:** si la ventana llegara a mostrar cientos de miles de objetos, las
pasadas podrían retrasar el audio; se notaría como desbordamientos en
`sesion.json` sin que el equipo esté cargado.
