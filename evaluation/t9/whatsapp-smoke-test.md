# Guion de prueba real de WhatsApp — T9

Este guion permite cerrar la validación externa después de renovar y verificar el token. La entrega está habilitada solo para la demo; **no pruebes con clientes**. Usa únicamente el número de prueba de Meta, destinatarios controlados por el administrador y la base sintética de Hackathon 2.

## Antes de empezar

- [x] Renovar `WHATSAPP_ACCESS_TOKEN` con permiso `whatsapp_business_messaging` y reemplazarlo en el archivo privado del servicio. No copiarlo al repositorio, una celda, una captura ni el chat.
- [x] Confirmar con una consulta de solo lectura que `PHONE_NUMBER_ID` y el token coinciden. Esta consulta no envía mensajes. Meta documenta el requisito en su [colección oficial de mensajes](https://www.postman.com/meta/whatsapp-business-platform/folder/13382743-ba8d099d-007e-4b52-b9f2-3cf3c60e4fbc).
- [x] En la configuración de API de Mozart, comprobar que el teléfono receptor aparece en «A» para el número de prueba que usa H2. No añadirlo como línea de negocio en el paso 5.
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

## Estado de la primera prueba manual

El 9 de octubre de 2026, la primera consulta desde Android llegó al webhook y fue procesada, pero el envío salió con HTTP 401; una lectura privada devolvió error Graph API 190/463. Ese token se renovó y el nuevo pasó las consultas Graph de solo lectura con permiso `whatsapp_business_messaging`. Las consultas siguientes volvieron a llegar al webhook y a completarse; la bandeja de salida registró `meta_http_400_graph_131030`. La interfaz de Meta ya mostraba el teléfono receptor en «A». La revisión privada mostró que el webhook entregó el identificador mexicano con formato `521...`, mientras la interfaz usa el número actual `52...` sin el `1` histórico. Un envío directo de comprobación al formato de «A» fue aceptado y sus estados posteriores confirmaron entrega y lectura. H2 ya normaliza el identificador mexicano entrante a `+52`; el cambio está desplegado y la batería cubre este comportamiento. Falta repetir el recorrido conversacional desde Android; no se ha probado un pedido ni se ha cambiado inventario. Esta prueba no se considera aprobada hasta completar la verificación extremo a extremo.

Agregar y verificar el teléfono receptor en **WhatsApp > Configuración de la API** para el mismo número de prueba y repetir una consulta sencilla. No hace falta regenerar el token ni cambiar el webhook. No copiar credenciales al repositorio o al chat. Cuando responda la consulta simple, completar el flujo controlado de pedido descrito arriba.
