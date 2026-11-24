# ADR-0001 · Grabar dos pistas separadas

- **Estado:** Aceptada
- **Fecha:** 2026-11-23
- **Aplica a:** `recorder.py`, `merger.py`

## Contexto

Una reunión en línea mezcla la voz de quien graba, que entra por el micrófono, y
la de los demás, que sale por los altavoces. Para una transcripción útil hay que
saber quién dijo cada cosa, y separar voces dentro de un mismo audio es la parte
menos fiable de todo el proceso.

Además, el audio no debe salir del equipo ni puede entrar un participante
automático en la llamada.

## Decisión

Grabar el micrófono y la salida del sistema en dos WAV independientes, de 48 kHz
y mono, mediante WASAPI y su modo *loopback*. La pista del micrófono se atribuye
completa a quien graba; la separación de voces se aplica solo a la otra.

Los dos dispositivos tienen relojes distintos y el *loopback* puede no entregar
datos cuando no suena nada. Cada bloque se coloca según un reloj común
(`time.perf_counter`): los huecos se rellenan con silencio y la deriva se corrige
cada diez segundos. Mientras se graba se reproduce silencio para mantener activa
la salida.

## Alternativas consideradas

| Alternativa | Por qué se descartó |
|---|---|
| Una sola pista mezclada | Es más simple y no exige sincronizar nada, pero pierde la certeza sobre quién graba y empeora cuando dos personas hablan a la vez |
| Un bot que entra en la llamada | Da una pista por participante, que es lo ideal para atribuir voces; exige integrarse con cada plataforma y pasar el audio por sus servidores |
| Capturar solo el proceso de la aplicación | Evitaría grabar notificaciones; la librería usada no lo ofrece y habría que detectar el proceso correcto en cada plataforma |

## Consecuencias

**Positivas:** la voz de quien graba se atribuye sin modelos, funciona con
cualquier programa de videollamada y un fallo en una pista no afecta a la otra.

**Negativas:** el código de sincronización es el más delicado del proyecto. Con
altavoces, los demás entran también por el micrófono y hay que descartar ese eco
comparando textos, lo que puede eliminar una frase repetida a propósito.

**A vigilar:** si aparece de forma habitual más de una persona ante el mismo
micrófono, habría que separar voces también en esa pista.
