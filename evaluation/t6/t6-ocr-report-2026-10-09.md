# T6: inventario y revisión de tickets sintéticos

**Resultado: aprobada para la demo local.** El OCR únicamente propone productos, tamaño y cantidad. Las existencias no cambian hasta que un cajero o administrador autorizado revisa y confirma expresamente el ticket.

La evaluación usó Tesseract 5.5.3, Pillow 12.3.0 y el modelo español `spa` de `tessdata_fast`, fijado al commit `923915d4ced2a7235221788285785a29c4a42d4a` (SHA-256 `6f2e04d02774a18f01bed44b1111f2cd7f3ba7ac9dc4373cd3f898a40ea6b464`). Tesseract reconoce español con el paquete `spa`; Pillow recomienda limitar formatos, tamaño y dimensiones al procesar imágenes no confiables ([datos de idioma oficiales](https://tesseract-ocr.github.io/tessdoc/Data-Files.html), [seguridad de Pillow](https://pillow.readthedocs.io/en/stable/handbook/security.html)).

| Caso sintético | Resultado |
|---|---|
| Ticket nítido | Latte caliente mediano reconocido; confianza OCR 0.95 |
| Rotado 90°, 180° y 270° | Los tres recuperaron exactamente producto, tamaño y cantidad |
| Borroso | Se dejó para revisión manual; no propuso producto |
| Parcial | Identificó solo la línea que seguía legible; aun requiere confirmación humana |
| Producto desconocido (“Latte de temporada”) | Se dejó sin resolver; no lo confundió con el Latte del catálogo |
| Imagen repetida | El segundo registro fue rechazado por su huella; no duplicó el movimiento |

El flujo sintético mantuvo el inventario idéntico antes de confirmar. Tras la confirmación válida, escribió en una sola transacción los tres consumos de receta ficticia —18 g de café, 220 ml de leche y un vaso— y la auditoría. El mismo ticket confirmado concurrentemente dos veces produjo una sola venta. Dos tickets distintos compitiendo por la última porción permitieron solo uno; el otro quedó pendiente, sin stock negativo. Un stock insuficiente revirtió todos los cambios. La corrección manual de líneas exige producto, tamaño y cantidad explícitos y una receta de demo existente.

La imagen se valida en memoria: JPEG/PNG, máximo 5 MB, lado máximo de 8,000 px y 20 MP; se comprueba el formato real y se decodifica antes de OCR. Tesseract recibe los bytes por stdin dentro de un directorio temporal privado y limitado por tiempo, que se elimina al finalizar. No se guarda la imagen, no se registra texto de ticket en logs y el OCR no se envía a Groq ni a Qwen3Guard. Al cumplirse las 24 horas de la retención de prueba, la tarea borra teléfono, líneas OCR y huella; conserva solo el estado de expiración y la auditoría ficticia aprobada.

La batería real de siete imágenes terminó en aproximadamente 0.1–0.6 s por imagen en este equipo. Las 58 pruebas de `tests/hackathon2` y `tests/t1` pasaron con el OCR real habilitado; Ruff pasó. Solo queda un aviso de obsolescencia de Starlette TestClient con HTTPX, sin impacto en los resultados.

Los tickets borrosos, parciales, sin cantidad explícita, de catálogo desconocido o sin receta aprobada permanecen en revisión y no se registran como venta hasta que el personal los corrija o rechace. Esta tarea implementa el componente local; todavía falta conectarlo al flujo de webhook/descarga de medios de WhatsApp durante T9. No se usaron fotos, teléfonos ni ventas reales y no se envió ningún mensaje.

**Evidencia:** [`run_ocr_evaluation.py`](run_ocr_evaluation.py), [`t6-ocr-results-2026-10-09.json`](t6-ocr-results-2026-10-09.json), [`tests/hackathon2/test_tickets.py`](../../tests/hackathon2/test_tickets.py) y [`tests/hackathon2/test_tickets_ocr_integration.py`](../../tests/hackathon2/test_tickets_ocr_integration.py).
