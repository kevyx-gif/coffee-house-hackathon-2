# Gestión de secretos del Hackathon 2

Este archivo indica dónde configurar cada credencial. **No escribas valores reales aquí** ni los compartas por chat, capturas, documentación o Git.

## Credenciales del servicio H2

Configura estas variables en el entorno privado del servicio que corre en el VPS:

| Variable | Uso | Dónde va |
| --- | --- | --- |
| `GROQ_API_KEY` | Llamar a Qwen mediante Groq durante el Hackathon 2 | VPS, solo servicio H2 |
| `PHONE_NUMBER_ID` | Identificar el número de WhatsApp Cloud API | VPS, solo servicio H2 |
| `WHATSAPP_ACCESS_TOKEN` | Enviar y recibir mensajes mediante Meta | VPS, solo servicio H2 |
| `WHATSAPP_VERIFY_TOKEN` | Validar el desafío inicial del webhook; debe coincidir con el valor configurado en Meta | VPS y configuración del webhook en Meta |
| `META_APP_SECRET` | Validar la firma de los webhooks entrantes | VPS, solo servicio H2; configurado en el entorno privado del servicio |
| `META_GRAPH_API_VERSION` | Fijar la versión de Graph API usada en llamadas salientes, con formato `vN.N` | VPS, configuración privada del servicio; usar una versión vigente en Meta |
| `H2_INITIAL_ADMIN_PHONE` | Número internacional del primer administrador en formato E.164, con `+` (por ejemplo, `+52` y el número nacional); se usa solo al iniciar una base H2 vacía | VPS, configuración privada; no crea cuentas nuevas al reiniciar |
| `H2_OTP_PEPPER` | Clave aleatoria usada para calcular el verificador HMAC de los códigos de acceso | VPS, configuración privada; al menos 32 caracteres aleatorios; no compartir ni cambiar después de emitir códigos |
| `GROQ_MODEL_ID` | Mantener el identificador fijado en la evaluación vigente | VPS, servicio H2; cambiarlo requiere volver a evaluar y aprobar el modelo |
| `QWEN_GUARD_URL` | Dirección local de la guardia fijada de Qwen3Guard | VPS, por defecto `http://127.0.0.1:19091`; nunca debe ser pública |
| `H2_ALLOW_EXTERNAL_TEXT` | Autoriza enviar el texto de una consulta al modelo de Groq | Desactivada por defecto; solo activarla en pruebas con texto sintético. No habilitar para conversaciones de clientes hasta aprobar por separado su tratamiento |
| `H2_ENABLE_META_DELIVERY` | Autoriza entrega de mensajes salientes/OTP y descarga de fotos desde Meta | Desactivada por defecto; requiere `WHATSAPP_ACCESS_TOKEN` |
| `H2_ENABLE_TICKET_OCR` | Habilita revisión local con Tesseract | Desactivada por defecto; requiere motor Tesseract y datos de idioma español instalados localmente |
| `H2_STATE_DIR`, `H2_BIND_HOST`, `H2_PORT` | Directorio SQLite exclusivo y dirección/puerto de escucha | VPS, rutas de configuración de systemd; mantener `127.0.0.1` y `18795` salvo revisión de despliegue |

En desarrollo local, copia `.env.example` a `.env` y reemplaza los valores vacíos en tu equipo. El ejecutor carga ese archivo sin imprimirlo; las variables ya definidas por el sistema tienen prioridad. `.env` está excluido de Git. En el VPS, guarda la configuración fuera del repositorio, por ejemplo en `/etc/coffee-house-hackathon2/hackathon2.env`, con permisos `600` y propietario de administración. La unidad de despliegue usa ese archivo privado.

El número configurado para el administrador debe conservar el `+`. El cliente de WhatsApp lo elimina solo al construir el campo `to` de Graph API, cuyo valor se envía como dígitos. No copies al campo de administrador el formato que aparece en un ejemplo de solicitud a la API.

`H2_ALLOW_EXTERNAL_TEXT` y `H2_ENABLE_META_DELIVERY` permanecen en `false` por defecto. La presencia de una clave Groq o token de Meta no activa llamadas ni mensajes. Para una prueba intencional con datos sintéticos, cambia cada permiso por separado. La entrega real de Meta requiere `WHATSAPP_ACCESS_TOKEN`; el servicio también exige `WHATSAPP_VERIFY_TOKEN` y `META_APP_SECRET` para iniciar el webhook. Para una prueba de fotos, instala Tesseract con el idioma español y habilita `H2_ENABLE_TICKET_OCR` solo en ese entorno aislado.

## Credenciales que no van en el servicio H2

- `HF_TOKEN`: úsalo solo en la torre para descargar las guardias de Hugging Face. No hace falta para atender mensajes una vez que los modelos estén descargados. Retíralo del entorno después de la descarga si no se necesita para otra operación.
- `NGROK_AUTHTOKEN`: solo sirve para túneles de desarrollo local con ngrok. No lo pongas en el entorno de producción del VPS si el webhook ya usa el dominio y HTTPS.
- `GROQ_API_KEY`, `PHONE_NUMBER_ID`, `WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_VERIFY_TOKEN` y `META_APP_SECRET` no deben guardarse en Colab para el despliegue. Los Secretos de Colab solo están disponibles para los notebooks a los que se habilite acceso y no se transfieren al VPS.

## Retención automática

El runtime ejecuta una limpieza al iniciar y cada minuto. Elimina conversaciones, códigos y sesiones vencidos, y redacta los mensajes, teléfonos y líneas de pedido al concluir el plazo guardado. Los avisos salientes vencen a las 24 horas; los avisos de asignación vencen al concluir la ronda de 5 o 10 minutos. Una actualización heredada obtiene una fecha de vencimiento a partir de su creación y tampoco se vuelve a enviar si ya venció. Conserva las filas de deduplicación y auditoría necesarias para no reprocesar eventos ni perder trazabilidad. La política de demostración no sustituye una revisión de privacidad antes de usar datos reales.

## Preparación local

La plantilla versionada `.env.example` contiene nombres y opciones seguras, sin valores secretos. El archivo local `.env` se debe mantener fuera de Git; no copies valores reales al ejemplo.

Antes de publicar, comprobar que `.env` y cualquier archivo de secretos no estén versionados. Si una credencial llega a mostrarse o publicarse, revócala y genera otra en el proveedor correspondiente.
