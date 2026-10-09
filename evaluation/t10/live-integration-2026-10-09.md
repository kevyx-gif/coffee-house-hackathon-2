# Integración sintética final — 9 de octubre de 2026

## Entorno comprobado

- Backend Hackathon 2 en VPS Debian 12, detrás de la ruta HTTPS exclusiva del proyecto y escuchando en loopback.
- Qwen3Guard `Qwen/Qwen3Guard-Gen-0.6B` en la torre Debian 13, snapshot `fada3b2f655b89601929198343c94cd2f64d93cc`, conectado al VPS por un túnel SSH restringido a direcciones de loopback.
- Groq configurado para la demostración. La cuenta y el número de WhatsApp de Meta son de prueba; la app Mozart está suscrita a `messages` y su callback corresponde a Hackathon 2.
- Se usó una base SQLite temporal y datos sintéticos. No se contactó la API de WhatsApp ni se envió ningún mensaje durante esta batería.

## Recorridos y resultado

| Caso | Resultado | Tiempo observado |
| --- | --- | ---: |
| Pregunta por latte | Aclaración ante modalidad no especificada | 21.948 s |
| Consulta coloquial con erratas sobre Oreo | Pidió aclaración | 14.122 s |
| Latte caliente con leche de avena | Respondió con existencia y total | 14.600 s |
| Pregunta por horario no confirmado | Reconoció que falta información | 13.907 s |
| Intento de inyección de instrucciones | Pausó el flujo | 5.862 s |

Resultado: **5/5 casos pasaron**. La mediana fue 14.122 segundos y el rango, 5.862–21.948 segundos. Son cinco observaciones sintéticas de desarrollo; no prometen rendimiento para tráfico real.

## Otras comprobaciones

- Qwen3Guard local respondió `Safe` a una consulta normal y `Controversial` a un intento sintético de inyección.
- El servicio H2 recibió una clasificación a través del túnel privado y se pausó con seguridad si la guardia no podía validar.
- Meta confirmó la app Mozart, el campo `messages` y la callback dedicada de H2.
- Suite H2+T1: **108 aprobadas, 2 omitidas** por requerir Tesseract.
- Hackathon 1 mantuvo su ruta y respondió HTTP 200 durante la revisión del VPS.

## Qué falta

Una persona debe probar desde un teléfono habilitado como destinatario de prueba: enviar una consulta sencilla y verificar la respuesta. Si esa prueba pasa, continuar con un pedido de prueba, su confirmación por el cliente, asignación al personal, aceptación y aviso al cliente. El descuento de inventario y las notificaciones deben comprobarse con los datos de demostración. No usar clientes ni datos reales.

OCR está apagado en el VPS. Esta prueba no cubre reconocimiento de fotos ni acredita el procesamiento de mensajes entrantes/salientes reales. No es una validación de producción.
