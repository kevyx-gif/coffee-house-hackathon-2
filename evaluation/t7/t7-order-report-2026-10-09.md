# T7 — pedidos, folios, estados y avisos automáticos

Fecha: 2026-10-09. Entorno: Windows, Python del proyecto, SQLite temporal y catálogo/recetas demo. Esta es la revisión propia de implementación, no una revisión independiente ni una prueba con WhatsApp real.

## Resultado

El flujo de dominio exige confirmación inequívoca, calcula la suma desde el catálogo, revisa la existencia antes de preparar y volver a confirmar, y crea el folio/ticket únicamente después del «sí». Los pedidos confirmados quedan pendientes del personal; todavía no son una venta. La aceptación autorizada revalida la receta y la existencia dentro de una transacción, calcula los consumos desde los datos guardados y aplica todos los movimientos o ninguno.

El ruteo quedó persistido en rondas. Se probó reparto por menor carga, desempate aleatorio permitido, dos turnos de cinco minutos, diez minutos cuando solo hay un cajero, aviso inmediato al administrador cuando no hay cajeros, escalación conjunta a cajero no probado y administrador, pase del administrador al siguiente cajero conservando el plazo, vencimiento y rechazo definitivo sin descuento. Una carrera sintética entre dos destinatarios produjo una sola aceptación, un único conjunto de movimientos y avisos deduplicados.

La secuencia de estados aceptado → en preparación → listo para recoger → entregado se autoriza contra la identidad/sesión, rol, folio, asignación y etapa actual. Cada transición crea una entrada idempotente de outbox y auditoría. La consulta pública está ligada al número de origen; un folio de otra persona no revela información y conduce a la consulta del último pedido propio. La respuesta pública solo contiene folio y estado.

La base pasó de esquema 1 a 2 con migración aditiva: conservó el inventario/pedido anteriores y creó la estructura de rondas y destinatarios sin reconstruir la base. H1 no se abrió ni se modificó.

## Pruebas

- Confirmación clara, no, cambio y ambigüedad; resumen de total y extras; revalidación de existencia que cae antes de la confirmación, sin ticket ni pedido.
- Folio y ticket de WhatsApp; inventario sin cambios mientras espera; asignación y aviso al destinatario.
- Reparto de carga, colas de cajeros, horarios con cero/uno/dos cajeros, escalación, pase al siguiente, administrador que pasa y expiración final.
- Aceptación de rol/turno correcto, rechazo de tercero, carrera de dos aceptaciones, receta exacta, ticket confirmado, movimientos atómicos y notificación al cliente y a cada destinatario.
- Rechazo del administrador sin descuento; transición omitida/revertida bloqueada; avisos/auditoría; búsqueda de estado por teléfono y folio ajeno.
- Migración desde esquema 1, reinicio, deduplicación de outbox e incertidumbre del proveedor simulados por las pruebas de storage/webhook.

Comando reproducible para esta unidad:

```powershell
$env:PYTHONPATH = 'src;tests\hackathon2'
.venv\Scripts\python.exe -B -m pytest tests\hackathon2\test_orders.py tests\hackathon2\test_storage.py -q -p no:cacheprovider
```

Las notificaciones se verificaron en la outbox local, no en Meta. Se debe ejecutar el trabajador de esa bandeja y el webhook en T9 para conectar la operación completa; ninguna credencial real ni número de cliente fue usado.
