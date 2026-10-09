# Guion de prueba real de WhatsApp — T9

Este guion queda preparado para cerrar la validación externa cuando la cuenta de Meta vuelva a autorizar la Cloud API. **No actives la entrega ni pruebes con clientes.** Usa solamente el número de prueba de Meta, destinatarios que el administrador controla y la base sintética de Hackathon 2.

## Antes de empezar

- [ ] Renovar `WHATSAPP_ACCESS_TOKEN` en el archivo privado del servicio. No copiarlo al repositorio, una celda, una captura ni el chat.
- [ ] Confirmar con una consulta de solo lectura que `PHONE_NUMBER_ID` y el token coinciden. Esta consulta no envía mensajes.
- [ ] Confirmar que `WHATSAPP_VERIFY_TOKEN` coincide exactamente con el configurado en Meta y que `META_APP_SECRET` valida la firma.
- [ ] Usar HTTPS hacia el webhook `/webhook`; el proceso FastAPI debe seguir escuchando solo en `127.0.0.1:18795`.
- [ ] Mantener `H2_ALLOW_EXTERNAL_TEXT=true` y `H2_ENABLE_META_DELIVERY=true` únicamente mientras se ejecuta esta prueba con texto sintético y números permitidos de prueba. Apagarlos al terminar.
- [ ] Inicializar una base H2 vacía de demostración con un administrador de prueba; no restaurar ni consultar la base de Hackathon 1.

## Recorrido del cliente

1. Desde un destinatario autorizado, enviar «¿Qué tipos de latte tienen?». Verificar respuesta breve en español y que nombre opciones presentes en el catálogo de demostración.
2. Enviar «ola, tiene baso mediano de oreo latttee?». Verificar que pregunta si se desea caliente o frío; no debe anunciar precio ni existencia antes de aclararlo. Responder «frío» y comprobar que consulta la variante fría del catálogo.
3. Pedir un producto, tamaño y extra que existan en el catálogo sintético. Verificar producto, tamaño, extra y total en el resumen. El inventario no debe cambiar todavía.
4. Responder «no» o modificar el resumen y comprobar que no se crea pedido. Repetir y responder «sí» de forma explícita; comprobar folio y pedido pendiente.
5. Consultar el folio desde el mismo número y, desde otro número de prueba, verificar que no se divulgue el pedido ajeno.

## Recorrido del personal

1. Usar el administrador de prueba inicial y solicitar `/acceso`. Comprobar que el código de un solo uso se entrega solo al destinatario autorizado, vence y no permite reutilización.
2. Invitar un cajero de prueba con `/ALTA`; comprobar rol, inicio de sesión y permisos denegados de operaciones ajenas a ese rol.
3. Aceptar el pedido de prueba como personal asignado. Comprobar una sola venta y un solo descuento de la base sintética. Probar también que un duplicado, otro cajero o una transición fuera de orden no repita la operación.
4. Cambiar el estado en orden: «en preparación», «listo para recoger» y «entregado». Verificar que cada transición válida notifique al mismo número que originó el pedido.
5. Si se prueba un ticket, usar una imagen inventada sin datos personales. Corregir la propuesta OCR y confirmar como personal; sin confirmación no debe cambiar el inventario.

## Dispositivos y cierre

- [ ] Repetir una consulta en WhatsApp móvil y WhatsApp Web/escritorio; comprobar lectura, orden de mensajes, teclado y botones disponibles.
- [ ] Registrar hora, versión desplegada, resultados esperados/observados y errores, sin incluir tokens, números completos ni capturas con información personal.
- [ ] Apagar los dos permisos externos después de la prueba. Confirmar que H1 sigue saludable y que no quedan datos de prueba que no deban conservarse.

## Estado observado antes de la prueba

El 9 de octubre de 2026, la lectura de `PHONE_NUMBER_ID` en Graph API v23.0 devolvió HTTP 401, código 190/subcódigo 463. No se enviaron mensajes. El flujo sintético local pasó; esta prueba real y los dispositivos permanecen pendientes hasta renovar la autorización.
