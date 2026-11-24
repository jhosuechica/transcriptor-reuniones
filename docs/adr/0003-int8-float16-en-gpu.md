# ADR-0003 · Usar int8_float16 en la GPU

- **Estado:** Aceptada
- **Fecha:** 2026-11-23
- **Aplica a:** `transcriber.py`

## Contexto

En una RX 7600 XT con CTranslate2 4.9.1 y ROCm 7.2, la transcripción con
`float16` funcionaba con el primer audio. Al encadenar varios en el mismo
proceso, la GPU empezaba a devolver cadenas de `!!!!` o un texto vacío. No había
ningún error: el proceso terminaba con normalidad y guardaba un documento
inservible. Con `bfloat16` ocurría lo mismo y `float32` fallaba con un error.

La causa no está confirmada. El paquete publicado de CTranslate2 se compila con
ROCm 10 y el entorno de ejecución que indica su guía es el 7.2.

## Decisión

Usar `int8_float16` en la GPU y comprobar el resultado. Un texto con un símbolo
repetido muchas veces, o vacío cuando se detectó voz, se considera corrupto: se
descarta y se pasa a la siguiente configuración. Las transcripciones guardadas
pasan el mismo control al reutilizarse.

## Alternativas consideradas

| Alternativa | Por qué se descartó |
|---|---|
| Seguir con `float16` y reiniciar el proceso por audio | Era más rápido (9,2 veces el tiempo real frente a 5,7), pero la corrupción podía aparecer dentro de una grabación larga |
| Instalar ROCm 10 | Puede ser la solución correcta; exige varias descargas grandes y no está probado con el controlador usado |
| whisper.cpp con Vulkan | Evita ROCm y es razonable para AMD; sus versiones recientes no incluían binario de Vulkan para Windows |
| Transcribir solo en CPU | Es fiable, pero obliga a usar un modelo menor para tardar un tiempo razonable |

## Consecuencias

**Positivas:** 24 pasadas seguidas dieron el mismo texto, y al forzar el fallo
original el control lo detectó y el trabajo continuó.

**Negativas:** se pierde velocidad. El control es una heurística: un texto
corrupto con otra forma no se detectaría.

**A vigilar:** si una versión nueva de CTranslate2 o de ROCm da el mismo texto
en 24 pasadas encadenadas con `float16`, conviene volver a él.
