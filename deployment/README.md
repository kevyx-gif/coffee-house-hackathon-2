# Despliegue aislado H2

Hackathon 2 está desplegado en el VPS con una ruta, unidad y base SQLite propias. La callback pública H2 es `https://www.xn--k-9gaa.com/soodo/CoffeeHouse-Hackathon2/webhook`; Hackathon 1 conserva `/soodo/CoffeeHouse/`. No se abrió un puerto nuevo en el firewall ni se cambió DNS.

El 9 de octubre de 2026 se instaló y habilitó `coffee-house-hackathon2.service` en Debian 12. El proceso queda en `127.0.0.1:18795`; Nginx reenvía solo la ruta H2 indicada arriba. Se comprobó que systemd mantiene el servicio activo, `nginx -t` pasa, la callback pública llega al servicio y H1 sigue respondiendo HTTP 200. La callback no valida solicitudes sin el token correcto. El detalle verificable del despliegue está en [evaluation/t10/deployment-2026-10-09.md](../evaluation/t10/deployment-2026-10-09.md).

Qwen3Guard está ejecutándose en la torre Debian 13 como servicio persistente del usuario del sistema. La inferencia escucha solo en `127.0.0.1:18473`; el túnel SSH inverso lo publica únicamente en el loopback del VPS, `127.0.0.1:19091`. En el VPS se creó un usuario de túnel sin shell, con una clave dedicada, `AllowTcpForwarding remote`, `PermitListen 127.0.0.1:19091`, `GatewayPorts no` y cero sesiones. La configuración pasó `sshd -t` y la comprobación efectiva; el túnel de Hackathon 1 conserva su puerto y sus restricciones. Los archivos `*-user.service` son las plantillas correspondientes al servicio activo de la torre; las plantillas de sistema muestran una alternativa con cuentas dedicadas.

La suite H2+T1 pasó **106 pruebas y omitió 2** (OCR opcional). La evaluación integrada en el VPS con Groq y la guardia real pasó **5/5 recorridos sintéticos**. Meta confirmó la suscripción de Mozart a la cuenta de WhatsApp de prueba y al campo `messages`; la callback devolvió el desafío válido y rechazó el inválido. No se envió un mensaje real durante estas comprobaciones automatizadas.

## Servicio Linux

La unidad `systemd/coffee-house-hackathon2.service` define un usuario dedicado, entorno privado, `UMask=0077`, directorio de datos separado, carpeta de sistema de solo lectura y escucha únicamente local en `127.0.0.1:18795`. Está instalada con código en `/opt/coffee-house-hackathon2`, datos en `/var/lib/coffee-house-hackathon2` y configuración en `/etc/coffee-house-hackathon2`. El secreto de entorno no se guarda en este repositorio.

Prepara `/etc/coffee-house-hackathon2/hackathon2.env` con permisos `600`; toma como guía los nombres de [`.env.example`](../.env.example) y [la guía de secretos](../docs/gestion-de-secretos.md), sin copiar secretos a este repositorio. En el despliegue de demostración, el uso de Groq y el envío por WhatsApp están habilitados con cuentas y datos de prueba; el OCR permanece apagado. Para una instalación nueva, deja `H2_ALLOW_EXTERNAL_TEXT`, `H2_ENABLE_META_DELIVERY` y `H2_ENABLE_TICKET_OCR` apagadas hasta configurar y validar cada integración. El puerto del modelo de guardia no se publica.

El servicio H2 usa `QWEN_GUARD_URL=http://127.0.0.1:19091`. El túnel se inicia desde la torre hacia el VPS y permite solo el tramo `127.0.0.1:18473` a `127.0.0.1:19091`. No abras la torre en el router ni en el cortafuegos público.

La cuenta de prueba de WhatsApp Business está suscrita a la app Mozart y al campo `messages`. Meta usa la callback H2 indicada arriba. Nunca apuntes la app a la página H1. Aunque la suscripción se verificó desde Meta y el procesamiento sintético pasó, todavía falta una prueba manual desde un teléfono permitido por la cuenta de prueba para confirmar entrada y respuesta reales.

## Estado y siguiente paso

1. Confirma que el servicio H2, la guardia y el túnel están activos antes de probar; consulta el registro de despliegue.
2. Desde un teléfono habilitado como destinatario de prueba en Meta, envía primero una consulta sencilla y comprueba que llega la respuesta. Esta prueba humana sigue pendiente.
3. Después prueba un borrador de pedido, cambios antes de confirmar y la aceptación del personal. Confirma que el inventario solo se descuenta al aceptar; todavía no se ha probado este recorrido por WhatsApp real.
4. El OCR está apagado en el despliegue actual; las fotos de tickets solo se evaluaron con datos sintéticos y no se deben enviar a esta demo hasta activar y comprobar esa función.
5. Usa solo números y datos de prueba. No uses información de clientes reales.
6. Antes de migraciones, respalda solo los datos H2. No abras, copies ni modifiques la base de Hackathon 1.

## Retirada

Detén solo `coffee-house-hackathon2.service` y el túnel H2. Retira solo el include de proxy H2 después de probar la configuración. Conserva o respalda la base H2 por separado según la retención aprobada. No restaures ni reviertas archivos del servicio H1, su base, entorno o DNS.
