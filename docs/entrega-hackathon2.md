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

La suite H2+T1 pasó **111 pruebas y omitió 2** (las omitidas requieren Tesseract). La limpieza automática, la cola, las cancelaciones, los estados y la sustitución de ingredientes se comprobaron con datos de demostración. H2 está desplegado en una ruta exclusiva del VPS; la app de prueba Mozart está suscrita a `messages`, y el desafío HTTPS se verificó. Qwen3Guard corre en la torre mediante un túnel SSH restringido a loopback. La evaluación integrada pasó **5/5 recorridos sintéticos** con Groq y Qwen3Guard. En la prueba en vivo, el pedido inicial **CH-F9B87265** expiró sin afectar inventario; tras corregir la leche de avena como sustitución de la leche entera, **CH-13959459** por **$95 MXN** fue aceptado por personal autorizado. Se verificó una venta y tres movimientos de inventario: café, 300 ml de avena y vaso grande, sin descontar leche entera. El cliente y el personal recibieron avisos. H2 normaliza el identificador mexicano `521...` a `+52`. Los detalles y capturas redactadas están en `evaluation/t10/live-integration-2026-10-09.md`.

![Captura redactada de la consulta del menú de latte respondida por el asistente en WhatsApp; el contacto está oculto.](../evaluation/t10/evidence/whatsapp-menu-query.png)

![Captura redactada del resumen del pedido antes de la confirmación del cliente; el contacto está oculto.](../evaluation/t10/evidence/whatsapp-order-draft.png)

![Confirmación del cliente y aviso de expiración. El pedido venció sin aceptación y no se descontó inventario. Captura con el contacto oculto.](../evaluation/t10/evidence/whatsapp-order-expired.png)

![Pedido grande aceptado por personal autorizado; la captura muestra la sustitución por leche de avena y el total de $95 MXN. Datos de contacto ocultos.](../evaluation/t10/evidence/whatsapp-order-oat-substitution-accepted-admin.png)

![Vista del cliente del mismo pedido, desde la confirmación del total hasta el aviso de aceptación. Encabezado y número del contacto recortados.](../evaluation/t10/evidence/whatsapp-corrected-order-accepted-customer.png)

## Alcance y limitaciones

Las recetas, existencias, pedidos, tickets y números incluidos son ficticios. Algunos precios se señalan como provisionales. La app y los teléfonos configurados en Meta son de prueba. La suscripción de Mozart, la callback y el intercambio de mensajes se verificaron. Dos pedidos de demostración se confirmaron: uno expiró sin venta y el siguiente fue aceptado por personal; en ese último, se comprobó que el backend guardó una venta y descontó una sola vez la receta correcta. El OCR sigue apagado en el despliegue y la prueba automatizada no acredita operación de producción. Los datos y precios son ficticios; el texto externo y la entrega por WhatsApp están habilitados únicamente en la configuración de prueba.

El resultado es un prototipo académico funcional a nivel de código y pruebas locales, diseñado para la demostración del Hackathon 2. No es un sistema de venta listo para atender clientes reales.
