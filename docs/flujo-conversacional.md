# Flujo de conversación de Hackathon 2

El cliente puede consultar el menú, tamaños, precios y disponibilidad en WhatsApp. La respuesta usa el catálogo local y la base SQLite H2; Qwen por Groq solo propone una herramienta de lectura. El servidor valida el nombre y los argumentos, calcula disponibilidad a partir de las recetas y existencias cargadas, y redacta el mensaje. El texto libre del modelo nunca se envía al cliente.

Cada consulta pasa por Qwen3Guard local antes del modelo y cada respuesta factual vuelve a pasar por la guardia. Solo `Safe` con respuesta válida permite continuar. Un bloqueo, un dato ambiguo, una herramienta o argumento desconocido, un error de modelo o la falta de guardia detienen la consulta con una respuesta breve; no se escribe inventario, se crea un pedido ni se registra una venta en este flujo.

El acceso del personal y los códigos de un solo uso se resuelven localmente. Ni el número remitente ni el código de acceso se incluyen en la petición de Groq. El texto normal de clientes tampoco se envía mientras `H2_ALLOW_EXTERNAL_TEXT` esté ausente o no sea exactamente `true`. Esta autorización se mantiene apagada por defecto. Las llamadas de prueba de esta implementación usan solamente ejemplos sintéticos.

La disponibilidad procede de las recetas y cantidades de demostración del repositorio y no está conectada a la operación real de la cafetería. Si un producto o tamaño no tiene receta, se indica que el personal debe confirmarlo. Los precios provisionales se identifican como tales. Los extras reconocidos se suman al total; los extras desconocidos no se aceptan por inferencia.

Ejemplo de salida generada desde los datos locales: «Sí, todavía tenemos el Latte mediano (360 ml / 12 oz). $70.00 MXN. Con sustitución por Leche de Avena, el total sería $85.00 MXN».

## Límites pendientes

- La prueba de rutas con la API se hace con consultas inventadas; no es una prueba con clientes ni una evaluación independiente del proveedor.
- El modelo puede pedir una aclaración o proponer una herramienta incorrecta. El servidor debe mantener las validaciones y responder sin adivinar.
- Los pedidos, el OCR y los avisos se implementan y prueban en sus tareas posteriores; este módulo no los activa.
- No habilitar `H2_ALLOW_EXTERNAL_TEXT=true` en una instancia accesible a clientes hasta que el tratamiento y la retención de mensajes reales se revisen y aprueben.
