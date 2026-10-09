# Cola, cancelación y recuperación — Hackathon 2

La cola de consultas es durable: el webhook autentica el evento, lo guarda en SQLite y responde a Meta; trabajadores separados consumen las consultas. Los límites aprobados son **3 consultas activas, 10 en espera, una pendiente por número y 120 segundos** desde que se admite el trabajo. El orden de espera es FIFO. Si se llena, el evento se conserva como diferido y no se presenta como una respuesta completada.

Una persona puede enviar `CANCELAR CONSULTA` o `/cancelar`. Si aún esperaba, se elimina de la cola. Si ya estaba en ejecución, el espacio no se libera hasta confirmar que se detuvieron sus llamadas. El cliente recibe una confirmación solo después de guardar ese estado. Si no queda espacio, el webhook guarda un aviso breve para intentarlo más tarde; no crea un trabajo que parezca atendido.

Al iniciar, el servicio recupera trabajos incompletos y revisa la bandeja de avisos. Un trabajo que estaba siendo procesado cuando el proceso cayó pasa a estado incierto; se informa que no se pudo confirmar el resultado y **no se repite automáticamente**. Así evitamos cobrar, descontar o responder dos veces por una operación cuyo resultado externo no se conoce. Las solicitudes vencidas no empiezan y tampoco generan un pedido.

Los recibos de WhatsApp se procesan aparte de la cola de consultas. Aceptaciones, movimientos, auditoría y avisos de pedido se guardan juntos en una transacción; los envíos con respuesta incierta de Meta quedan pendientes de conciliación, sin reintento a ciegas.

## Evidencia local

`tests/hackathon2/test_jobs.py` prueba límite de 3 + 10, FIFO, consulta única por persona, cancelación en espera y en curso, plazo vencido, reinicio con trabajo incierto y recibos de estado fuera de orden. `tests/hackathon2/test_runtime.py` inicia y cierra el conjunto de trabajadores y confirma que la clave de Meta por sí sola no activa envíos.

Estas pruebas usan eventos y teléfonos inventados. No equivalen a carga en VPS ni a una conversación real de WhatsApp. Ver también [evaluación T8](../evaluation/t8/cola-y-recuperacion.md).


## Limpieza automática de datos vencidos — 2026-10-09

El runtime H2 ahora ejecuta `purge_expired()` al iniciar y cada 60 segundos. La prueba verifica tanto el borrado de una conversación expirada al abrir FastAPI como la limpieza periódica de una conversación que vence durante el funcionamiento. Los errores de SQLite se registran sin incluir contenido y se reintentan en el siguiente ciclo. Conserva idempotencia y auditoría mientras redacta o elimina el contenido según los plazos configurados.


La bandeja saliente también tiene vencimientos: los mensajes generales caducan a las 24 horas y los avisos a cajeros al vencer su ronda. `claim_outbox` rechaza una fila vencida antes de llamar a Meta; la limpieza marca y redacta pendientes ya caducados. Las filas heredadas sin fecha reciben 24 horas desde su creación, por lo que una actualización no revive avisos antiguos.
