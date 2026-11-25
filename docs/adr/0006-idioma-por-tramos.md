# ADR-0006 · Decidir el idioma tramo a tramo

- **Estado:** Aceptada
- **Fecha:** 2026-11-24
- **Aplica a:** `transcriber.py`

## Contexto

La primera versión fijaba el español. Con el idioma fijado, Whisper no transcribe
lo que oye en otro: lo traduce. Una reunión en inglés salía entera en español y
las frases inglesas de una reunión mixta se perdían o se traducían.

## Decisión

Cortar el audio por las pausas, calcular para cada tramo si es más probable el
español o el inglés, unir los seguidos del mismo idioma y transcribir cada grupo
con su idioma fijado. Los tramos de menos de un segundo, o dudosos, toman el del
vecino más próximo. Se puede fijar un solo idioma en los ajustes.

Los tramos se separan con 250 ms de silencio y conservan 300 ms de margen a cada
lado. Con 100 ms, en una reunión real se recuperaba el 82,8 % de las palabras, por
debajo del 84,6 % con el idioma fijado; con 300 ms, el 85,9 %. Con pausas de 400 ms
se recuperaban más, pero la prueba que alterna idiomas bajaba a 91 de 120.

La detección rellena cada tramo hasta 10 segundos en lugar de 30 y se hace de uno
en uno. Por lotes, en una RX 7600 XT con ROCm 7.2, devolvía números sin sentido;
los resultados imposibles se rechazan y pasan al siguiente intento.

## Alternativas consideradas

| Alternativa | Por qué se descartó |
|---|---|
| Detectar el idioma una vez por reunión | Es lo más barato y basta si solo se habla uno; en una reunión mixta se pierde el otro |
| La opción `multilingual` de faster-whisper | Es la más sencilla y no exige código propio; decide por bloques de hasta 30 segundos y en la prueba acertó lo mismo que el español fijado |
| Elegir entre todos los idiomas de Whisper | Sería más general; en tramos cortos confunde el español con lenguas parecidas |
| Un modelo específico de identificación de idioma | Puede ser más preciso en tramos cortos; añade otra descarga y otra dependencia |

## Consecuencias

**Positivas:** en un diálogo sintético que alterna ambos idiomas se pasa del 56,7 %
al 98,3 % de palabras correctas (118 de 120) al adoptarlo, sin cambios en el diálogo solo en
español (144 de 144).

**Negativas:** la transcripción tarda más, una palabra suelta puede quedar en el
idioma equivocado y quien cambie de idioma sin pausa verá la frase en uno solo.

**A vigilar:** si se necesitan más idiomas, basta con ampliar la lista, pero hay
que repetir la medición: cada idioma añadido aumenta las confusiones.
