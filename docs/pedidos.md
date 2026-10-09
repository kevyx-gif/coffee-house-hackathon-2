# Pedidos de WhatsApp (demo)

El flujo usa solamente catálogo e inventario sintéticos. El asistente prepara un resumen con productos, tamaños, extras y total; un «sí» inequívoco crea el pedido. Un «no», una petición de cambio o una respuesta dudosa no crea ticket ni asignación. Se vuelve a comprobar la existencia y el precio antes de guardar la confirmación.

Cada pedido recibe un folio corto y un ticket `whatsapp_order`, distinto de un recibo de caja `pos_receipt`. El ticket de pedido queda en revisión y no modifica inventario. La venta y el descuento se realizan juntos en SQLite únicamente cuando el personal autorizado acepta el pedido; la receta de consumo se calcula a partir de la copia del pedido y las recetas demo guardadas, nunca a partir de datos del modelo.

La asignación considera el número de pedidos pendientes asignados a cada cajero y desempata al azar. Los avisos de asignación dejan de enviarse al vencer su turno, aunque la bandeja se recupere más tarde. Los dos primeros turnos duran cinco minutos, salvo que exista un solo cajero, en cuyo caso tiene diez. Si no hay cajeros, se avisa al administrador desde el inicio. Después de dos turnos, se avisa al siguiente cajero disponible y al administrador a la vez por diez minutos. El administrador puede pasar el pedido a un cajero que aún no lo haya recibido sin reiniciar ese plazo. Un vencimiento no es un rechazo: expira sin venta ni descuento y se informa al cliente.

Una aceptación válida gana de manera atómica aunque dos destinatarios respondan al mismo tiempo. El cliente y todas las personas que recibieron el pedido reciben el aviso de aceptación desde la bandeja durable. Los estados avanzan en orden: aceptado, en preparación, listo para recoger y entregado. Cada estado visible queda auditado y genera un aviso idempotente.

La consulta manual busca pedidos por el número de origen validado. Un folio ajeno no se confirma ni revela; se consulta el pedido más reciente del mismo número. La respuesta al cliente contiene únicamente folio y estado.

## Verificación

Desde la raíz, ejecutar:

```powershell
$env:PYTHONPATH = 'src;tests\hackathon2'
.venv\Scripts\python.exe -B -m pytest tests\hackathon2\test_orders.py tests\hackathon2\test_storage.py -q -p no:cacheprovider
.venv\Scripts\ruff.exe check src\coffee_house\hackathon2 tests\hackathon2
```

Las pruebas usan teléfonos, pedidos, mensajes y existencias ficticios. No llaman a Meta ni envían mensajes reales. La bandeja y el trabajador de entrega están conectados en el runtime H2, pero el envío real permanece desactivado por defecto. El resultado de pruebas de dominio no acredita envío en producción.
