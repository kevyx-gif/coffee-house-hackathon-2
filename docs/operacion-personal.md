# Operación del personal por WhatsApp

El primer administrador se configura al iniciar una base H2 vacía. Personal autorizado usa `/acceso`; el código vence en cinco minutos y solo habilita acciones tras validarlo. Cada mensaje de personal se valida de nuevo con la sesión y el rol del servidor. Un folio por sí solo nunca da permiso.

| Rol | Acciones de esta demo |
| --- | --- |
| Administrador | Invitar y cambiar roles, consultar/ajustar existencias, cambiar precios provisionales, consultar auditoría, aceptar/pasar/rechazar pedidos y revisar tickets. |
| Gerente | Consultar y ajustar existencias; las cantidades solo afectan la simulación. No puede cambiar precios ni leer auditoría. |
| Cajero | Aceptar o pasar pedidos asignados, avanzar estados de su pedido, revisar/corregir/confirmar tickets de caja. |

Comandos principales:

- `AYUDA`: muestra las opciones habilitadas para el rol activo.
- `/ALTA +52... cajero` o `/ALTA +52... gerente`: el administrador invita un número. La persona inicia después con `/acceso`.
- `/ROL +52... gerente`: el administrador cambia el rol; las sesiones anteriores se revocan.
- `INVENTARIO`: gerente o administrador consultan cantidades sintéticas.
- `AJUSTAR_EXISTENCIA grano_cafe +100 conteo de ejemplo`: gerente o administrador registran un ajuste con motivo. Un evento repetido no vuelve a mover la cantidad.
- `PRECIO hot_latte mediano 72.50`: el administrador ajusta un tamaño existente. `PRECIO_EXTRA leche_avena 5.00` aplica a un extra. El cambio se guarda solo en SQLite H2, se marca provisional y queda auditado.
- `AUDITORIA`: el administrador consulta las diez acciones recientes; el número de personal se oculta parcialmente.
- `ACEPTAR CH-...`, `PASAR CH-...`, `PREPARAR CH-...`, `LISTO CH-...`, `ENTREGAR CH-...`, `VER CH-...`: acciones de pedido sujetas al rol, asignación y estado.
- `REVISAR POS-...`, `CORREGIR POS-...: 2x latte caliente mediano` y `CONFIRMAR POS-...`: corrección humana del OCR antes de aplicar cualquier movimiento.

Los identificadores de productos, tamaños e insumos se consultan en los datos de ejemplo del repositorio; estos mensajes operativos están pensados para la demostración, no como una interfaz natural para clientes. Esta versión cambia precios de entradas existentes, pero no agrega productos ni modifica nombres/fotos del menú. Las cantidades y recetas jamás son inventario real.
