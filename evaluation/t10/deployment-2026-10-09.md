# Registro de despliegue H2 — 9 de octubre de 2026

## Resultado

Hackathon 2 quedó instalado en el VPS Debian 12 como servicio independiente. La callback pública es:

`https://www.xn--k-9gaa.com/soodo/CoffeeHouse-Hackathon2/webhook`

La ruta de Hackathon 1 sigue siendo `/soodo/CoffeeHouse/`; no se reutilizó ni modificó esa ubicación. Nginx envía la ruta exacta de H2 al proceso local `127.0.0.1:18795/webhook`. No se abrió un puerto público nuevo. El registro de acceso de esa ubicación está desactivado para no guardar el token de verificación que Meta manda en la URL.

El código está en `/opt/coffee-house-hackathon2`, los datos de H2 en `/var/lib/coffee-house-hackathon2` y los secretos/configuración en `/etc/coffee-house-hackathon2/hackathon2.env`, fuera del repositorio. El servicio `coffee-house-hackathon2.service` está activo y habilitado al arranque. La API escucha en loopback.

## Comprobaciones realizadas

- `nginx -t`: correcto; configuración recargada.
- Servicio systemd: activo y habilitado.
- Listener H2: `127.0.0.1:18795`; no se publica directamente.
- Prueba de desafío con el formato de Meta contra la ruta HTTPS pública: correcta; se obtuvo el desafío enviado, sin mostrar ni guardar el token en el informe.
- Una consulta GET sin token válido recibió HTTP 403, como corresponde.
- La página de Hackathon 1 siguió respondiendo HTTP 200.
- Las banderas de envío de mensajes y texto externo permanecen apagadas. No se enviaron mensajes de WhatsApp.

## Pendiente antes de una prueba real

1. Conectar Qwen3Guard de la torre al VPS con el túnel SSH privado en loopback (`127.0.0.1:19091`). La guardia no está conectada y el flujo debe permanecer pausado sin ella.
2. En Meta Developers, configurar el producto **WhatsApp Business Account** con la URL pública de arriba y el mismo valor privado de `WHATSAPP_VERIFY_TOKEN`; verificar y guardar.
3. Suscribir el campo `messages` solo después de confirmar el estado de la guardia y revisar las opciones de salida. Ejecutar una prueba controlada con un número de prueba y validar entrada, respuesta, pedidos y avisos antes de considerar la entrega completa.

La API vigente validada permite leer el recurso de número configurado, pero eso no demuestra que Meta haya guardado la callback ni que `messages` esté suscrito. No se ha afirmado que la integración WhatsApp de extremo a extremo esté finalizada.

## Actualización final — 9 de octubre de 2026

La sección anterior conserva el estado del preflight inicial. Desde entonces se completaron los siguientes pasos y estos datos la reemplazan:

- La cuenta de WhatsApp de prueba quedó suscrita a la app de Meta **Mozart**; Meta muestra el campo `messages` y la callback de Hackathon 2.
- Qwen3Guard quedó activo en la torre como servicio persistente, con el snapshot `Qwen/Qwen3Guard-Gen-0.6B` fijado y accesible únicamente mediante el túnel SSH restringido de loopback. El servicio H2 en el VPS validó una clasificación a través de ese túnel.
- Groq quedó configurado en el entorno de prueba del VPS; la salida de WhatsApp está habilitada solo para la cuenta de prueba. OCR sigue apagado.
- Se ejecutó la batería T5 integrada en el VPS con base SQLite temporal, Groq y Qwen3Guard locales: **5/5** casos sintéticos aprobados. Latencias observadas: 5.862–21.948 segundos; mediana 14.122 segundos. El caso de latencia máxima fue la consulta de menú. Esta pequeña muestra no es un SLO ni una estimación de producción.
- La suite local H2+T1 quedó en **108 aprobadas y 2 omitidas**, estas últimas dependen del ejecutable OCR opcional.

No se envió ningún mensaje real, no se creó un pedido real y no se probó la entrega a un teléfono. La última validación necesaria para cerrar el flujo real es que una persona envíe una consulta desde un destinatario de prueba autorizado y confirme que llega una respuesta. Después debe probarse por separado el ciclo de pedido y aceptación del personal usando únicamente datos de prueba. Por tanto, el despliegue y sus comprobaciones automatizadas están listos, pero la integración conversacional de extremo a extremo aún necesita esa prueba humana.
