# ADR-0007 · Dar a Whisper un ejemplo de puntuación

- **Estado:** Aceptada
- **Fecha:** 2026-11-24
- **Aplica a:** `transcriber.py`

## Contexto

En una reunión real (ES2004a del corpus AMI), el 49 % de las palabras salía en
rachas de más de doce sin ningún signo de puntuación. Además de ser incómodo de
leer, los cambios de hablante se ajustan a los finales de frase: sin puntos, ese
ajuste no puede actuar.

## Decisión

Pasar a Whisper, como contexto de cada tramo, cuatro frases breves bien puntuadas
en el idioma de ese tramo, seguidas del vocabulario configurado. Se descarta el
segmento que reproduzca más de una de esas frases seguidas.

## Alternativas consideradas

| Alternativa | Por qué se descartó |
|---|---|
| No dar ejemplo | Recupera algo más de texto (85,9 % frente a 85,1 %), pero la mitad queda sin puntuar |
| Un ejemplo más largo y específico de reuniones | Puntúa casi igual, pierde más palabras (83,1 %) y apareció repetido en la transcripción |
| Un ejemplo más corto | No apareció repetido ninguna vez, lo que lo hace más seguro; recuperaba el 84,5 % y puntuaba algo peor |
| Puntuar después con otro modelo | Separaría los dos problemas y no influiría en lo transcrito; añade otro modelo y otra etapa |

## Consecuencias

**Positivas:** las palabras en rachas sin puntuar bajan del 49 % al 17 %, y las
atribuidas a la persona correcta suben del 96,7 % al 98,1 %.

**Negativas:** se pierde en torno a un punto de palabras, dentro de la variación
entre pruebas. El ejemplo puede inducir texto donde hay ruido y el filtro solo
reconoce su copia literal.

**A vigilar:** si en transcripciones reales aparecen frases como «No, ninguna» sin
que nadie las diga, conviene pasar al ejemplo corto.
