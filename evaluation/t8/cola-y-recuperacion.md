# T8 — Cola, límites y recuperación

Fecha: 2026-10-09. Resultado: **T8 completada; cola, fallos y recuperación verificados con casos sintéticos y un túnel SSH de prueba**. WhatsApp conectado y móvil siguen en T9.

## Comportamiento implementado

- Tres consultas como máximo en ejecución, diez en espera FIFO, una pendiente por remitente y plazo absoluto de 120 segundos, aplicados de forma durable en SQLite.
- Cancelación de una consulta en espera; para una consulta activa, el espacio se conserva hasta que las llamadas asíncronas confirman su parada.
- Duplicados de Meta deduplicados antes de crear otro trabajo. Recibos de entrega se procesan en un trabajador separado.
- Tras reiniciar, un trabajo interrumpido se marca incierto y no se vuelve a ejecutar a ciegas. Las consultas vencidas no arrancan. Los avisos de cancelación, saturación y recuperación se escriben en la bandeja.
- No se libera un pedido ni se descuenta inventario por saturación, cancelación, vencimiento o resultado incierto.

## Verificación local

La suite de Hackathon 2 y los gates T1 cerraron con **91 pruebas aprobadas, 2 omitidas y 1 aviso de compatibilidad de Starlette TestClient con HTTPX**. La regresión completa del árbol de desarrollo (Hackathon 1 + Hackathon 2, Python 3.11.2) también pasó: 419 pruebas correctas y 2 opcionales omitidas. La copia aislada limpia de H2 con Python 3.13 pasó 91 pruebas y omitió las mismas 2. Las omitidas son las dos pruebas opcionales que necesitan el ejecutable OCR en esta computadora; la evaluación OCR con Tesseract en la torre está en `evaluation/t6/`. Ruff, compilación y `git diff --check` pasaron. Los casos de cola están en `tests/hackathon2/test_jobs.py` y los de inicio/apagado en `tests/hackathon2/test_runtime.py`.

Las pruebas locales prueban con dobles y reloj controlado la concurrencia 3+10, orden FIFO, límite por remitente, cancelación, expiración, evento repetido, recibos fuera de orden y recuperación sin reejecución. La prueba de arranque confirma además que cargar un token Meta no habilita el cliente ni envía nada; para activar entrega deben configurarse los permisos explícitos.

El gate aislado T1 en la torre ya había ejecutado tres sesiones sintéticas con clasificación de entrada y salida en **38.882 segundos** y comprobó que H1 permaneciera saludable. Esa medición es del guardia de seguridad; **no equivale** a probar aquí las tres conversaciones completas del webhook/Groq/WhatsApp sobre la cola integrada.

### Revalidación de cierre — 2026-10-09

Después de completar las alternativas de disponibilidad, la suite aislada de Hackathon 2 + T1 pasó **93 pruebas y omitió 2 opcionales** en Python 3.13 limpio; la regresión completa H1+H2 del árbol de desarrollo pasó **421 y omitió las mismas 2**. Ruff, compilación y `git diff --check` también pasaron. Se agregaron casos para alternativa por producto/extra agotado, falta total de existencia y borrador que pierde stock antes de la confirmación. Las propuestas exigen stock y precio verificables, no sustituyen el pedido y no descuentan inventario hasta una nueva confirmación.

La prueba de recuperación de una consulta interrumpida reabre la misma SQLite desde una nueva instancia de `H2Store`, marca el trabajo activo como incierto, deja su aviso en la bandeja y rechaza volver a reclamarlo. Esto valida el límite durable de reinicio en una prueba local; no sustituye reiniciar el servicio real ni cortar la conexión a Meta o al túnel.

Como refuerzo, se inició el runtime FastAPI completo sobre una SQLite presembrada con una consulta activa, mediante su ciclo de vida normal y clientes externos simulados. Al arrancar, la aplicación recuperó el trabajo como incierto, dejó un único aviso pendiente y no volvió a reclamar la consulta. La prueba nueva está en `tests/hackathon2/test_runtime.py`; la suite local H2+T1 quedó en **94 aprobadas y 2 omitidas** (Python 3.11), Ruff y compilación pasaron. Esto valida la recuperación al iniciar el runtime, pero no una interrupción física de la torre, el túnel o Meta.

También se bloqueó SQLite con una transacción exclusiva: la admisión del webhook falló de forma segura, no creó filas ni mensajes parciales, y al liberar la base el mismo evento se admitió exactamente una vez. Está en `tests/hackathon2/test_storage.py`. La suite local actual H2+T1 pasa **95 pruebas y omite 2**; Ruff y compilación pasan. La pérdida real del túnel y el flujo real con Meta continúan pendientes de T9/T10.

Repetición de compatibilidad en entorno limpio Python 3.13 y en el VPS Debian 12 con Python 3.11.2: **100 pruebas aprobadas, 2 omitidas**, `pip check`, Ruff y compilación correctos. En el VPS se instaló en una carpeta temporal con código saneado, luego se borró; H1 siguió activo y no se creó servicio ni listener H2. La suite aumentó con la prueba vertical sintética de T9 y cuatro casos de aclaración de modalidad, sin alterar las reglas de la cola.

La pérdida y recuperación del túnel también se probó con un endpoint HTTP sintético de loopback: una conexión SSH inversa real al VPS respondió; al cerrar el túnel, el listener remoto desapareció y la solicitud falló; tras reconectar, el endpoint volvió a responder. El túnel y servidor sintético se cerraron y no dejaron puertos abiertos. H1 siguió activo y devolvió HTTP 200.

La bandeja de Meta se probó con respuestas sintéticas de rechazo y timeout: los envíos quedan `failed` o `uncertain` sin replay automático; después de recuperar el transporte, un aviso nuevo sí se envía y el incierto anterior sigue sin repetirse. No se enviaron mensajes reales.

### Revalidación tras la aclaración caliente/fría — 2026-10-09

La revisión final incorporó cuatro pruebas para que el sistema no decida la temperatura por el cliente ni arrastre la preferencia de una bebida a otra en un pedido múltiple. La suite H2+T1 pasó **100 pruebas y omitió 2 opcionales**; la regresión H1+H2 pasó **428 y omitió 2**. También pasó la evaluación conversacional sintética final en **5/5** con llamadas Groq y Qwen3Guard local. Se registran tanto el fallo de interpretación previo como la corrida corregida en `evaluation/t5/`. Estas comprobaciones usan datos sintéticos y no sustituyen validar WhatsApp real.

### Retención automática y vencimiento de avisos — 2026-10-09

Se conectó un trabajador de retención al ciclo de vida de FastAPI: hace una limpieza al iniciar y repite cada 60 segundos. Elimina o redacta conversaciones, códigos y sesiones vencidas; los mensajes del outbox vencen por defecto a las 24 horas, y las notificaciones de asignación caducan al terminar el turno del cajero. Las filas antiguas sin vencimiento reciben el plazo de 24 horas durante la limpieza; mensajes vencidos se marcan fallidos, no se reclaman para envío y se redacta su destinatario/cuerpo. Pruebas cubren el inicio, la repetición periódica, registros vencidos y vigentes, mensajes antiguos, turnos de cajero y el límite del reclamador. La suite H2+T1 pasó **106 pruebas y omitió 2** en Python 3.13 local y Debian 12/Python 3.11.2; `pip check`, Ruff y `compileall` pasaron. No se guardaron datos reales. Esto verifica el mecanismo técnico, no determina por sí solo la política legal de retención para operar con clientes.

## Aún no probado externamente

No se cortó el túnel de una instancia H2 desplegada, el webhook ni Meta durante una conversación real; no se enviaron mensajes reales ni se midió la cuota de Groq. Tampoco se desplegó la cola en el VPS. El flujo conectado, el dispositivo móvil y el VPS de producción conservan T9/T10 abiertos y se registran en la [matriz de integración](../t9/matriz-integracion.md).
