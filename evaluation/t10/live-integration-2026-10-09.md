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

El token se renovó y se comprobó junto con el permiso de mensajería, pero las respuestas manuales fueron rechazadas por Meta con HTTP 400/código `131030`. Agregar y verificar el teléfono receptor en la lista de destinatarios de prueba del mismo número de Meta que usa H2 y repetir una consulta. Cuando llegue la respuesta, continuar con un pedido de prueba, su confirmación por el cliente, asignación al personal, aceptación y aviso al cliente. El descuento de inventario y las notificaciones deben comprobarse con los datos de demostración. No usar clientes ni datos reales.

OCR está apagado en el VPS. La prueba sintética no cubre reconocimiento de fotos ni acredita el ciclo real de mensajes entrantes y salientes. No es una validación de producción.

## Intento manual desde el teléfono

El 9 de octubre se conectó un Android de prueba y se abrió el chat del número de prueba de Meta. La primera consulta llegó al webhook y el trabajo del asistente terminó, pero Meta rechazó la respuesta con HTTP 401; la comprobación privada devolvió error 190/subcódigo 463. El token se renovó después y las consultas Graph confirmaron el recurso telefónico y el permiso `whatsapp_business_messaging`. Dos consultas posteriores llegaron al webhook y se completaron, pero Meta rechazó las respuestas con HTTP 400/código `131030`. No se recibió respuesta del asistente, no se creó ningún pedido ni se afectó el inventario. El diagnóstico apunta a que el teléfono receptor todavía no está en la lista permitida del número de prueba de Meta usado por H2. El token y el webhook están comprobados; queda agregar/verificar el destinatario y repetir. No se incluye captura porque la pantalla contiene mensajes previos y elementos que no forman parte de la evidencia del proyecto.
