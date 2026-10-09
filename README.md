# Coffee & Matcha House — Hackathon 2

Agente de WhatsApp para una cafetería: atiende consultas del menú, arma pedidos con confirmación, permite que el personal autorizado los gestione y demuestra el control de existencias con datos ficticios.

## Qué incluye

- Webhook FastAPI para WhatsApp Cloud API, con verificación de firma y deduplicación.
- Consultas en español sobre menú, precios, tamaños y disponibilidad simulada. Qwen mediante Groq propone herramientas limitadas; el backend verifica los argumentos y responde con datos del catálogo.
- Guardia local Qwen3Guard de entrada y salida. Si no está disponible o no puede verificar una respuesta, el flujo se pausa.
- Personal con roles de administrador, gerente y cajero, códigos de un solo uso, sesiones y permisos revisados en el servidor.
- Pedidos con resumen y confirmación del cliente, asignación al personal, estados, folio y avisos preparados en una bandeja durable.
- Existencias y recetas de demostración en SQLite. El inventario solo se descuenta tras la aceptación del personal.
- Limpieza automática al iniciar y cada minuto: elimina conversaciones, códigos y sesiones vencidas; redacta contenido expirado sin perder las claves de deduplicación/auditoría necesarias. Los mensajes no enviados vencen a las 24 horas y los avisos de asignación al terminar su turno.
- Lectura local de tickets con Tesseract como propuesta para revisión humana; una lectura OCR no registra por sí misma una venta.
- Precios provisionales editables para productos existentes, guardados aparte en SQLite y auditados.

## Datos y alcance

Los datos de existencias, recetas, ventas, tickets y números de prueba son sintéticos. Los precios marcados como provisionales son solo para la demostración. El sistema no representa un punto de venta ni un inventario de producción.

La demo está desplegada en el VPS tras una ruta HTTPS propia, distinta de Hackathon 1. La app de prueba de Meta «Mozart» está suscrita al campo `messages`; el desafío del webhook y el rechazo de un token incorrecto se comprobaron. Qwen3Guard corre en la torre y se comunica con H2 mediante un túnel SSH restringido que solo escucha en loopback. Groq y la entrega de WhatsApp están habilitados únicamente para esta configuración de prueba; OCR está apagado. H2 normaliza los identificadores mexicanos `521...` a `+52`. La consulta de menú recibió una respuesta marcada como enviada, entregada y leída. El cliente confirmó el pedido de prueba **CH-F9B87265** por **$85 MXN**. El administrador de respaldo recibió el aviso, pero no respondió dentro de los 10 minutos: el pedido expiró, el cliente recibió aviso y no se descontó inventario. La demo no está habilitada para atender clientes reales.

Hackathon 2 es independiente de la aplicación y los datos de Hackathon 1. No incluye su interfaz web, historial privado, documentos SDD, modelos descargados ni credenciales.

## Ejecución local

Requiere Python 3.13. La configuración inicia solo en loopback. Qwen3Guard debe estar ejecutándose en la máquina de inferencia en una dirección local, por defecto 127.0.0.1:19091.

1. Crear un entorno virtual e instalar las dependencias fijadas y de prueba:

    py -3.13 -m venv .venv
    .venv\Scripts\python.exe -m pip install -r requirements\hackathon2-backend.lock
    .venv\Scripts\python.exe -m pip install -r requirements-test.txt

2. Copiar la plantilla .env.example a .env y completar los valores en privado. No subir .env ni enviar credenciales por chat.

3. Para recibir el webhook se requieren PHONE_NUMBER_ID, WHATSAPP_VERIFY_TOKEN, META_APP_SECRET y H2_OTP_PEPPER. La entrega saliente y los códigos de personal requieren WHATSAPP_ACCESS_TOKEN y H2_ENABLE_META_DELIVERY=true. El uso de texto externo mediante Groq requiere GROQ_API_KEY y H2_ALLOW_EXTERNAL_TEXT=true. Las opciones externas permanecen apagadas por defecto.

4. Iniciar el servicio:

    .venv\Scripts\python.exe scripts\run_hackathon2.py

En ejecución local, el proceso escucha en 127.0.0.1:18795. En el VPS, Nginx reenvía la ruta dedicada `https://www.xn--k-9gaa.com/soodo/CoffeeHouse-Hackathon2/webhook` a ese servicio. El programa no abre puertos, cambia el firewall ni registra el webhook en Meta. Consulta docs/ejecucion-hackathon2.md y deployment/README.md.

## Pruebas

Desde la raíz del repositorio:

    $env:PYTHONPATH = 'src;tests\hackathon2'
    .venv\Scripts\python.exe -B -m pytest tests -q -p no:cacheprovider
    .venv\Scripts\ruff.exe check src\coffee_house\hackathon2 scripts\run_hackathon2.py tests\hackathon2 tests\t1

En esta copia de Hackathon 2, la suite H2+T1 pasó **109 pruebas y omitió 2** en Python 3.13 local; las dos omitidas requieren el ejecutable OCR, cubierto en una evaluación sintética independiente. La batería conversacional integrada volvió a pasar **5/5 casos** con Groq y Qwen3Guard reales, incluida una consulta clara de latte caliente con leche de avena. Incluye una aclaración cuando el cliente omite si una bebida es caliente o fría, y verifica que la preferencia de una bebida no se aplique por error a otra dentro del mismo pedido. Las consultas y la base de esa evaluación fueron sintéticas; no hubo mensajes de WhatsApp ni llamadas de Graph API durante la batería. H2 y la guardia están activos en el VPS y la torre, y H1 siguió respondiendo HTTP 200. Los resultados de antes y después del ajuste están en `evaluation/t5/`.

La primera prueba manual desde Android falló porque el token anterior ya no era válido (HTTP 401, error 190/463). Se instaló un token nuevo en el entorno privado del VPS; las consultas Graph de solo lectura confirmaron el número configurado y el permiso `whatsapp_business_messaging`. En pruebas posteriores se encontró una diferencia entre el identificador mexicano `521...` recibido por webhook y el destinatario `52...` que aparece permitido en la configuración de Meta. Un envío directo de comprobación al formato permitido fue aceptado por Graph (HTTP 200); no se volvió a añadir el número ni se cambió la cuenta WABA. Se añadió y desplegó la normalización del identificador entrante, cubierta por pruebas. Desde Android quedó comprobada la respuesta del menú; en el pedido más reciente, el cliente confirmó y recibió folio, pero el personal no respondió en diez minutos. El pedido expiró y no afectó las existencias. El detalle está en `evaluation/t10/live-integration-2026-10-09.md`.

La primera batería coloquial tuvo 20/21 por una expectativa mal etiquetada en el evaluador. La batería corregida completa pasó **22/22 intenciones claras y 5/5 ambiguas** en una sola corrida, con cero reintentos y cero fallos; incluye una frase con varias erratas. El ejecutor espera 15 segundos entre consultas para respetar la cuota observada. La explicación, las limitaciones y los resultados están en `evaluation/t9/groq-colloquial-report-2026-10-09.md`; son frases sintéticas, no una garantía de comprensión general.

## Evidencia y operación

- Descripción de la entrega: docs/entrega-hackathon2.md
- Instrucciones y controles: docs/
- Operación, cola y límites externos: evaluation/t8/ y evaluation/t9/
- Compatibilidad y preflight temporal del VPS: evaluation/t10/
- Registro actualizado del despliegue aislado: `evaluation/t10/deployment-2026-10-09.md`
- Prueba de integración sintética con la guardia y Groq: `evaluation/t10/live-integration-2026-10-09.md`
- Pruebas de conversación sintética: evaluation/t5/
- Guion para completar la prueba humana de WhatsApp con el número de prueba: `evaluation/t9/whatsapp-smoke-test.md`
- Medición sintética del enrutamiento Qwen/Groq: evaluation/t9/groq_intent_threshold.py y sus resultados JSON
- Prueba ampliada de lenguaje coloquial: evaluation/t9/groq_colloquial_stress.py y groq-colloquial-report-2026-10-09.md
- Prueba OCR sintética: evaluation/t6/
- Flujo de pedidos y estados: evaluation/t7/
- Evaluación local de la guardia: evaluation/t1/qwen3guard-gates.md

Para cualquier uso real se requiere revisar privacidad y retención, configurar secretos fuera del repositorio, probar WhatsApp con números de prueba y validar el VPS, el teléfono, WhatsApp Web y la recuperación ante cortes de red.
