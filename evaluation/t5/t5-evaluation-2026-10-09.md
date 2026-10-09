# T5 — Conversación verificada, Qwen/Groq y herramientas

Fecha: 2026-10-09. Ejecución en la copia aislada del Hackathon 2, con datos de menú y mensajes sintéticos.

## Resultado

T5 completada para el flujo de consultas de menú, precio, tamaño, extras y disponibilidad. Qwen por Groq propone únicamente `search_menu` o `check_availability`. El backend valida forma y argumentos, resuelve alias con prudencia, consulta el catálogo y la receta local, suma extras reconocidos y genera el texto final. Las respuestas libres del modelo nunca se muestran al cliente y el módulo no modifica inventario ni crea pedidos.

La guardia real Qwen3Guard-Gen-0.6B, snapshot `fada3b2f655b89601929198343c94cd2f64d93cc`, revisó entrada y salida. La inyección de prueba se detuvo antes de llamar a Groq. El proceso de la torre solo escuchó en loopback detrás de un túnel SSH temporal. Hackathon 1 respondió HTTP 200 antes y después; el proceso temporal y el túnel se cerraron al terminar.

Cinco recorridos sintéticos completos pasaron: consulta de tipos de latte, solicitud mal escrita de Oreo Latte con aclaración entre caliente/frío, latte mediano con leche de avena y total de $85 MXN, pregunta de horario sin dato inventado, e intento de inyección bloqueado. Cuatro recorridos consultaron Groq; el ataque no salió del guardia. La duración observada fue de 5.606 a 22.264 segundos por recorrido (mediana 14.228 s); el guardia local domina ese tiempo. Son cinco observaciones de desarrollo, no una garantía de producción. La prueba independiente T1 ya midió tres sesiones con entrada/salida del guardia en 38.882 s en total.

El texto de una consulta no se envía a Groq salvo autorización explícita de `H2_ALLOW_EXTERNAL_TEXT=true`; esta variable permanece apagada por defecto. El código de acceso de personal se maneja localmente y no se pasa al guardia ni al modelo. La muestra usó únicamente textos inventados, un remitente reservado y una base temporal. No se contactó Meta, ningún cajero ni cliente, y no se usaron llamadas de escritura.

## Verificación

- 46 pruebas combinadas de H2 y T1 correctas; una advertencia informativa de deprecación de Starlette TestClient con HTTPX, sin fallos.
- Ruff y compilación de los módulos de H2, pruebas y evaluación correctos.
- El guardia fue detenido tras la prueba; su puerto de loopback quedó cerrado y H1 permaneció saludable.
- La clave de Groq no aparece en evidencias ni salidas, y `.env` continúa ignorado y sin seguimiento Git.

Código principal: `src/coffee_house/hackathon2/conversation.py`, `catalog.py`, `model.py` y `guard.py`. Suite: `tests/hackathon2/test_conversation.py` y `test_model.py`. Corrida completa reproducible: `evaluation/t5/run_synthetic_e2e.py`; resultados sintéticos: `evaluation/t5/synthetic-e2e-results-2026-10-09.json`.

## Límites que siguen abiertos

El webhook y Cloud API tienen pruebas simuladas; el recorrido real de WhatsApp, la experiencia en dispositivos, las colas, las ventas, OCR, el despliegue y la exportación pública quedan para sus tareas. La disponibilidad es una demostración basada en ingredientes de fixture, no el inventario real. No se habilita texto real de clientes para el proveedor remoto.

## Revalidación de ambigüedad de temperatura — 2026-10-09

Al repetir los mismos cinco casos con Qwen/Groq y Qwen3Guard reales, la primera corrida obtuvo 4/5: el modelo eligió el producto canónico `Oreo Latte` ante una escritura incorrecta y, como ese nombre coincide con una versión caliente y otra fría, el servidor consultó el stock de la caliente. No afirmó que hubiera existencias, pero no preguntó la modalidad. Se conservó esa corrida en `synthetic-e2e-results-2026-10-09-modality-fix-before.json`.

Se corrigió el backend para contrastar la modalidad escrita por la persona con las variantes caliente/fría del catálogo. Si falta, pregunta «¿Lo quieres caliente (Oreo Latte) o frío (Iced Oreo Latte)?» antes de mostrar precio, existencia o crear un borrador; si el modelo propone la variante opuesta a la escrita, se usa la que pidió la persona. En pedidos con varios productos, la modalidad se vincula a cada fragmento del mensaje para no aplicar la de una bebida a otra. Tres pruebas nuevas cubren la falta de modalidad, la discrepancia entre texto y ruta del modelo, y pedidos simples/múltiples sin borrador prematuro.

La segunda corrida pasó **5/5**: menú 21.870 s, error escrito con aclaración 13.615 s, extra y total 14.065 s, pregunta desconocida 13.389 s e inyección detenida antes de Groq 5.533 s. Mediana 13.615 s; máximo 21.870 s. Solo cuatro casos consultaron Groq; todos los textos y el remitente eran sintéticos. El resultado está en `synthetic-e2e-results-2026-10-09-modality-fix-after.json`; la salida previa aprobada `synthetic-e2e-results-2026-10-09.json` se conserva.
