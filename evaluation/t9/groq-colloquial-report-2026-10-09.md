# Evaluación de mensajes coloquiales — 2026-10-09

Se probaron mensajes sintéticos con abreviaturas, errores de escritura, diminutivos, temperatura, extras y pedidos de varios artículos a través del enrutador Qwen/Groq real. No se utilizaron conversaciones de clientes y no se guardaron respuestas libres del modelo.

## Resultado

La primera batería obtuvo **20/21 propuestas completas correctas** y **5/5 mensajes ambiguos sin ejecutar herramientas**. El único desacuerdo fue un caso mal etiquetado en la propia prueba: «tienen frape oreo?» pregunta si una bebida específica está disponible, por lo que `check_availability` era una respuesta válida; el evaluador había esperado una lista de menú. Se guardó la corrida original para conservar el hallazgo.

Se reformuló ese ejemplo como «q tipos de frapes tienen?» y se agregó «ola, tiene baso mediano de horeo latttee?». La batería corregida se ejecutó completa en una sola corrida: **22/22 propuestas claras correctas y 5/5 ambiguas sin herramienta**. Consumió 35,374 tokens de entrada y 1,214 de salida, con mediana de 471 ms, máximo de 1,060 ms, cero reintentos de cuota y cero solicitudes fallidas. El modelo reconoció la errata severa como Oreo Latte mediano y dejó la modalidad pendiente para que la aplicación pregunte.

## Correcciones aplicadas

- El prompt y las descripciones de herramientas distinguen preguntas de lista («qué tipos hay») de existencia («¿hay?, ¿tienen?, ¿queda?»).
- Se indicó al modelo normalizar ejemplos claros de tamaño (`medianito`, `medianita`, `mediani`, `grand`) y el extra «lechita de avena», pero preservar expresiones de extras desconocidos para pedir confirmación al servidor.
- Se registró la variante «frape de oreo» como la bebida Frappé Oreo cuando la identificación es inequívoca.
- El catálogo ahora traduce esos diminutivos y abreviaturas claras a los tamaños admitidos. Temperatura no se trata como extra.
- Las ambigüedades siguen sin llamada de herramienta; faltantes y extras que el catálogo no reconoce se consultan en lugar de inventarlos.

La corrida inicial está en [`groq-colloquial-stress-2026-10-09-first.json`](groq-colloquial-stress-2026-10-09-first.json); las verificaciones puntuales están en [`groq-colloquial-menu-review-2026-10-09.json`](groq-colloquial-menu-review-2026-10-09.json) y [`groq-colloquial-severe-typo-review-2026-10-09.json`](groq-colloquial-severe-typo-review-2026-10-09.json). La batería corregida completa y reproducible está en [`groq-colloquial-stress-2026-10-09.json`](groq-colloquial-stress-2026-10-09.json). El ejecutor limita el ritmo entre consultas a 15 segundos para evitar exceder la cuota observada.

## Alcance

Es una evaluación pequeña y fija de 21 frases claras más cinco ambiguas; no prueba toda la variación del español, usuarios reales, latencia sostenida ni WhatsApp. Por ello RF-02 conserva estado **parcial**. La integración de Meta sigue pendiente de renovar su autorización y probar con números/dispositivos de prueba; no se envió mensaje real.
