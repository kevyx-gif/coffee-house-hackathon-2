# Agente de WhatsApp para Coffee & Matcha House

## Descripción del proyecto

Este proyecto construye un agente de WhatsApp para ayudar a clientes de una cafetería a consultar el menú y preparar pedidos, y a su personal a revisar y atenderlos. El trabajo combina las piezas principales del Módulo 2: WhatsApp Cloud API, conversación con estado, uso acotado de herramientas, control de acceso, seguridad y monitoreo del flujo.

El sistema responde en español y consulta los datos disponibles del menú. Si una solicitud es ambigua, pide una aclaración; no debe inventar información faltante. Para formar un pedido, presenta los artículos, tamaños, extras y total, y espera la confirmación explícita del cliente. La confirmación crea una solicitud con folio; el inventario de demostración se descuenta cuando el personal autorizado acepta el pedido.

## Funciones demostradas

- Consulta de menú, precio, tamaño, extras y existencias simuladas.
- Aclaración de productos cuando la solicitud no permite identificar una opción con seguridad.
- Confirmación de pedido antes de crear la solicitud y asignación gradual al personal.
- Seguimiento por folio y cambios de estado autorizados: aceptado, en preparación, listo para recoger y entregado.
- Roles de administrador, gerente y cajero, con acceso restringido en el servidor.
- Ajustes auditados de existencias y precios provisionales, únicamente sobre la base de demostración.
- Lectura local de una foto de ticket como propuesta; el personal corrige y confirma antes de aplicar movimientos.
- Cola durable para consultas, cancelación y recuperación conservadora después de interrupciones.

## Tecnologías y decisiones

El backend utiliza Python y FastAPI. SQLite conserva el estado de demostración, los pedidos, el inventario simulado, la auditoría y los avisos pendientes. Meta WhatsApp Cloud API es el canal previsto de entrada y salida. Qwen mediante Groq se usa para proponer una herramienta conversacional acotada; el servidor valida la solicitud y obtiene precios y disponibilidad de sus propios datos. Qwen3Guard se ejecuta como guardia local y Tesseract realiza el OCR local.

Llama se mantuvo como parte de Hackathon 1. Para Hackathon 2 se probó su capacidad de seleccionar herramientas con entradas sintéticas; las versiones revisadas no alcanzaron los criterios de selección y aclaración fijados para pedidos. Por eso esta entrega usa Qwen mediante Groq, cuya ruta de herramientas y sus límites se documentan en la evaluación. La elección no modifica el proyecto ni el modelo de Hackathon 1.

Las acciones de negocio no dependen de una respuesta libre del modelo. Los permisos, totales, cambios de inventario y transiciones de pedido se verifican en el backend. Una falla de la guardia pausa el flujo; un evento repetido no debe producir una segunda operación. El OCR no descuenta existencias automáticamente.

## Evaluación

La suite H2+T1 pasó **109 pruebas y omitió 2** (las omitidas requieren Tesseract). La limpieza automática, la cola, las cancelaciones y los estados se comprobaron con datos de demostración. H2 está desplegado en una ruta exclusiva del VPS; la app de prueba Mozart está suscrita al campo `messages`, y el desafío del webhook HTTPS se verificó. Qwen3Guard corre en la torre y se comunica con el VPS por un túnel SSH restringido a loopback. La evaluación integrada más reciente pasó **5/5 recorridos sintéticos** con Groq y Qwen3Guard. Se validó desde Android una consulta automática sobre los lattes y Meta marcó la respuesta como enviada, entregada y leída. H2 normaliza el identificador mexicano histórico `521...` a `+52`. Falta probar el pedido completo por WhatsApp, desde la confirmación del cliente hasta la aceptación del personal. Los detalles, tiempos y límites están en `evaluation/t10/live-integration-2026-10-09.md`.

## Alcance y limitaciones

Las recetas, existencias, pedidos, tickets y números incluidos son ficticios. Algunos precios se señalan como provisionales. La app y el teléfono configurados en Meta son de prueba. La suscripción de Mozart al campo `messages` y la dirección de devolución de Hackathon 2 se verificaron. Desde Android se comprobó una consulta, la respuesta automática y su lectura; el teléfono de prueba está autorizado. Falta probar, de manera controlada, la confirmación del pedido y el aviso al personal. En la demo actual el texto externo y la entrega por WhatsApp están habilitados solo con la configuración de prueba; el OCR está apagado y los datos son ficticios. La prueba automatizada no acredita operación de producción.

El resultado es un prototipo académico funcional a nivel de código y pruebas locales, diseñado para la demostración del Hackathon 2. No es un sistema de venta listo para atender clientes reales.
