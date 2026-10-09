# Ejecutar Hackathon 2

Este código es la demostración de Hackathon 2: atención y pedidos por WhatsApp, control de personal, inventario de ejemplo y revisión de tickets. No es un punto de venta de producción. **El inventario y las recetas son ficticios.** SQLite es una base exclusiva de H2; no apunta ni migra la base de Hackathon 1.

## Preparación local

Se probó con Python 3.13. Instala primero los paquetes fijados y copia la plantilla de entorno:

```powershell
py -3.13 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements\hackathon2-backend.lock
.venv\Scripts\python.exe -m pip install -r requirements-test.txt
Copy-Item .env.example .env
```

Completa `.env` en privado. El servicio carga este archivo localmente sin imprimir sus valores; las variables del sistema prevalecen. **No pegues las claves en una celda, chat, captura ni archivo versionado.** En Linux/VPS, usa el archivo de entorno privado de systemd descrito en [la guía de despliegue](../deployment/README.md).

El inicio del webhook necesita `PHONE_NUMBER_ID`, `WHATSAPP_VERIFY_TOKEN` y `META_APP_SECRET`. La entrega saliente requiere también `WHATSAPP_ACCESS_TOKEN`. `H2_OTP_PEPPER` debe tener al menos 32 caracteres aleatorios. El primer administrador se crea una sola vez en una base H2 vacía con `H2_INITIAL_ADMIN_PHONE`; al reiniciar no se vuelve a crear ni a restablecer inventario.

El modelo de intención y Qwen3Guard son conexiones distintas. Para habilitar texto de cliente se requiere la API aprobada configurada y `H2_ALLOW_EXTERNAL_TEXT=true`. Para enviar respuestas/OTP y procesar fotografías desde WhatsApp se requiere `H2_ENABLE_META_DELIVERY=true`. Las dos opciones permanecen desactivadas por defecto y deben activarse solo en el ambiente de demostración con números de prueba y datos ficticios. Tener las claves guardadas no las activa.

Qwen3Guard debe estar descargado en la torre y accesible para el proceso mediante una conexión privada de loopback. Su identificador de modelo y revisión están fijados en el cliente; el servicio falla de forma segura si no verifica el modelo o si no responde. No abras el puerto de guardia a Internet. Las fotos del ticket se procesan en el servidor con Tesseract local y el idioma español, nunca se envían al modelo. Para habilitar ese paso opcional, instala Tesseract y el paquete de idioma `spa`, y configura `H2_ENABLE_TICKET_OCR=true`.

## Iniciar y probar

Con los servicios privados necesarios levantados:

```powershell
$env:PYTHONPATH = 'src'
.venv\Scripts\python.exe scripts\run_hackathon2.py
```

La API escucha solo en `127.0.0.1:18795`; el webhook está en `/webhook`. La ruta pública dedicada es `https://www.xn--k-9gaa.com/soodo/CoffeeHouse-Hackathon2/webhook`, reenviada por Nginx únicamente al endpoint local de H2. La página de Hackathon 1 conserva su ruta independiente `/soodo/CoffeeHouse/`. El ejecutor rechaza una dirección de escucha pública; no abre puertos ni registra el webhook en Meta.

Pruebas automáticas, con mensajes, cuentas y pedidos inventados:

```powershell
$env:PYTHONPATH = 'src;tests\hackathon2'
.venv\Scripts\python.exe -B -m pytest tests\hackathon2 tests\t1 -q -p no:cacheprovider
.venv\Scripts\ruff.exe check src\coffee_house\hackathon2 scripts\run_hackathon2.py tests\hackathon2 tests\t1
```

La guía [de secretos](gestion-de-secretos.md), [de pedidos](pedidos.md), [de cola](cola-y-recuperacion.md) y [de operación del personal](operacion-personal.md) describe el flujo y las restricciones. Los comandos de personal solo funcionan con sesión y autorización en servidor. El administrador puede cambiar precios existentes, marcados como provisionales; administrador y gerente consultan o ajustan el inventario sintético con motivo y auditoría. El cliente confirma un resumen antes de crear un pedido; el inventario solo se descuenta tras la aceptación de personal. Las imágenes de tickets generan una propuesta para corregir y confirmar; OCR no descuenta existencias.

## Límite de la demostración

La suite local y las evaluaciones sintéticas no confirman un recorrido real con WhatsApp, Meta, la cuenta de Groq, la red móvil, ni el VPS. Antes de usar el servicio con cualquier cliente real hay que configurar y probar explícitamente esas integraciones, revisar retención/privacidad y obtener una validación humana del despliegue.
