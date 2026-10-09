# Despliegue aislado H2

Hackathon 2 está desplegado en el VPS con una ruta, unidad y base SQLite propias. La callback pública H2 es `https://www.xn--k-9gaa.com/soodo/CoffeeHouse-Hackathon2/webhook`; Hackathon 1 conserva `/soodo/CoffeeHouse/`. No se abrió un puerto nuevo en el firewall ni se cambió DNS.

El 9 de octubre de 2026 se instaló y habilitó `coffee-house-hackathon2.service` en Debian 12. El proceso queda en `127.0.0.1:18795`; Nginx reenvía solo la ruta H2 indicada arriba. Se comprobó que systemd mantiene el servicio activo, `nginx -t` pasa, la callback pública llega al servicio y H1 sigue respondiendo HTTP 200. La callback no valida solicitudes sin el token correcto. El detalle verificable del despliegue está en [evaluation/t10/deployment-2026-10-09.md](../evaluation/t10/deployment-2026-10-09.md).

Qwen3Guard está ejecutándose en la torre Debian 13 como servicio persistente del usuario del sistema. La inferencia escucha solo en `127.0.0.1:18473`; el túnel SSH inverso lo publica únicamente en el loopback del VPS, `127.0.0.1:19091`. En el VPS se creó un usuario de túnel sin shell, con una clave dedicada, `AllowTcpForwarding remote`, `PermitListen 127.0.0.1:19091`, `GatewayPorts no` y cero sesiones. La configuración pasó `sshd -t` y la comprobación efectiva; el túnel de Hackathon 1 conserva su puerto y sus restricciones. Los archivos `*-user.service` son las plantillas correspondientes al servicio activo de la torre; las plantillas de sistema muestran una alternativa con cuentas dedicadas.

La suite H2+T1 pasó **111 pruebas y omitió 2** (OCR opcional). La evaluación integrada en el VPS con Groq y la guardia real pasó **5/5 recorridos sintéticos**. Meta confirmó la suscripción de Mozart al campo `messages`; la callback devolvió el desafío válido y rechazó el inválido. Tras corregir el formato de números mexicanos, consultas reales de prueba recibieron respuesta. Un primer pedido expiró correctamente sin descontar inventario. Después de corregir la sustitución de leche de avena, el pedido **CH-13959459** por **$95 MXN** fue aceptado por personal autorizado: se registraron una venta y tres movimientos únicos (café, 300 ml de avena y vaso grande), sin descontar leche entera. Los avisos al cliente y al personal se procesaron correctamente. Las capturas redactadas están en `evaluation/t10/evidence/`.

## Servicio Linux

La unidad `systemd/coffee-house-hackathon2.service` define un usuario dedicado, entorno privado, `UMask=0077`, directorio de datos separado, carpeta de sistema de solo lectura y escucha únicamente local en `127.0.0.1:18795`. Está instalada con código en `/opt/coffee-house-hackathon2`, datos en `/var/lib/coffee-house-hackathon2` y configuración en `/etc/coffee-house-hackathon2`. El secreto de entorno no se guarda en este repositorio.

Prepara `/etc/coffee-house-hackathon2/hackathon2.env` con permisos `600`; toma como guía los nombres de [`.env.example`](../.env.example) y [la guía de secretos](../docs/gestion-de-secretos.md), sin copiar secretos a este repositorio. En el despliegue de demostración, el uso de Groq y el envío por WhatsApp están habilitados con cuentas y datos de prueba; el OCR permanece apagado. Para una instalación nueva, deja `H2_ALLOW_EXTERNAL_TEXT`, `H2_ENABLE_META_DELIVERY` y `H2_ENABLE_TICKET_OCR` apagadas hasta configurar y validar cada integración. El puerto del modelo de guardia no se publica.

El servicio H2 usa `QWEN_GUARD_URL=http://127.0.0.1:19091`. El túnel se inicia desde la torre hacia el VPS y permite solo el tramo `127.0.0.1:18473` a `127.0.0.1:19091`. No abras la torre en el router ni en el cortafuegos público.

La cuenta de prueba de WhatsApp Business está suscrita a la app Mozart y al campo `messages`. Meta usa la callback H2 indicada arriba. Nunca apuntes la app a la página H1. Los primeros errores de token vencido y formato de destinatario se resolvieron; H2 normaliza el identificador mexicano entrante `521...` a `+52`. El pedido **CH-F9B87265** expiró a los 10 minutos sin aceptación, venta ni cambio de inventario. Una prueba posterior, **CH-13959459**, confirmó el recorrido corregido: Latte grande con leche de avena como sustitución, aceptación por personal, aviso al cliente y descuento único de 300 ml de avena sin descontar leche entera. Consulta el informe vivo y sus capturas redactadas para ver el detalle.

## Estado de la prueba en vivo

La consulta del menú, dos pedidos confirmados y la aceptación de una venta ya se comprobaron en vivo con números de prueba. La segunda prueba verificó que la sustitución de leche de avena descuenta solo el ingrediente correspondiente. El OCR continúa apagado en este despliegue y se evaluó por separado con datos sintéticos. Usa únicamente números y datos de prueba. No uses información de clientes reales.

## Retirada

Detén solo `coffee-house-hackathon2.service` y el túnel H2. Retira solo el include de proxy H2 después de probar la configuración. Conserva o respalda la base H2 por separado según la retención aprobada. No restaures ni reviertas archivos del servicio H1, su base, entorno o DNS.
