# Datos de demostración — Hackathon 2

Esta copia de trabajo contiene fixtures inventados únicamente para probar el flujo de pedidos, revisión de tickets y movimientos de inventario. Las cantidades de existencias y consumos **no son datos reales**, recetas, porciones ni instrucciones de preparación de Coffee & Matcha House. No deben usarse para operar el negocio.

El catálogo aprobado de H1 se conserva en `data/catalog.json` y es la única fuente de los nombres, precios y presentaciones del menú de demostración. Los fixtures referencian sus identificadores y consultan sus precios allí; no replican ni modifican el catálogo.

## Archivos

- `data/demo-hackathon2/inventory.json`: existencias iniciales sintéticas. Incluye algunos artículos agotados para probar bloqueos y alternativas.
- `data/demo-hackathon2/recipes.json`: consumos ficticios por tamaño y ejemplos de extras. No representa la receta real de las bebidas.
- `data/demo-hackathon2/ticket-fixtures.json`: un pedido WhatsApp pendiente de aprobación, un ticket de caja legible y otro ambiguo.

## Reglas que ilustran los ejemplos

1. El cliente ve el resumen y total y confirma. En ese momento se crea una solicitud pendiente; todavía no se registra una venta ni se descuenta inventario.
2. El cajero asignado recibe la solicitud; si no existe uno, la recibe el administrador. La venta y el descuento ocurren solo al aceptar y volver a comprobar existencias.
3. La foto de un ticket de venta de mostrador produce una lectura propuesta. El cajero o administrador revisa y confirma las líneas antes de descontar.
4. Un ticket de pedido emitido por el bot no se trata como ticket de caja. Esta separación evita contabilizar dos veces el mismo pedido.
5. Una línea incierta, un artículo agotado o un mensaje duplicado no producen un movimiento automático.

Los datos de este directorio se pueden reemplazar sin cambiar el catálogo público. Antes de usar recetas, existencias, ventas o tickets reales se requiere una fuente autorizada, aprobación del negocio y una revisión de privacidad. Las capturas reales de WhatsApp para la entrega se agregarán solo después de ocultar números, nombres, códigos y otros datos personales.
