<!-- i18n: source=docs/guides/extraction-pipeline.md blob=a8d7e423185b09c1fa986972aa01b8c1b70d77d8 model=claude-sonnet-5-5 -->
> Esta página es una traducción automática de la [página en inglés](https://github.com/ClickHouse/loom/blob/main/docs/guides/extraction-pipeline.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/guides/extraction-pipeline.md), which is authoritative.

<a id="customize-the-extraction-pipeline"></a>

# Personalizar el pipeline de extracción

`memory.set_from_messages` procesa su transcripción mediante una lista ordenada de pasos que comparten un mismo estado. Una solicitud puede reemplazar esa lista a través de `pipeline_steps`: reordenar u omitir pasos, o llamar a un paso en proceso que un operador haya registrado. Esta página cubre solo la lista de pasos; [Extracción](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/extraction.md) explica qué hace la extracción.

`pipeline_steps` es un campo del cuerpo HTTP. Está oculto en el esquema de herramientas de MCP y no es un parámetro del helper `memory.set_from_messages` del SDK de Python; envíalo por HTTP o mediante `call(...)`. Los tipos de entrada de las operaciones generados (`sdk/python/loom/ops.py`, `sdk/typescript/src/generated/ops.ts`) lo incluyen.

<a id="the-default-pipeline"></a>

## El pipeline predeterminado

`DEFAULT_PIPELINE` en `server/memory/app/pipeline.py`:

| Paso                              | Qué hace                                                                                                            |
| --------------------------------- | ------------------------------------------------------------------------------------------------------------------- |
| `loom.fetch_namespace_categories` | Carga la taxonomía de etiquetas del espacio de nombres para el extractor.                                           |
| `loom.fetch_existing_memories`    | Las memorias existentes más cercanas (hasta 50), que se muestran al extractor.                                      |
| `loom.llm_extract`                | La llamada de extracción.                                                                                           |
| `loom.drop_recall_echoes`         | Descarta hechos que repiten memorias servidas a la sesión.                                                          |
| `loom.drop_session_echoes`        | Descarta hechos que repiten lo que transportaron las propias llamadas a Loom de la sesión.                          |
| `loom.drop_activity_narration`    | Descarta la narración de actividad de herramientas sin un resultado duradero.                                       |
| `loom.drop_non_durable`           | Sesiones de programación: descarta instantáneas de estado, tareas de mantenimiento y planes.                        |
| `loom.verify_durable`             | Sesiones de programación: pasada de verificación sobre los hechos restantes.                                        |
| `loom.hash_dedup`                 | Descarta hechos cuyo hash canónico ya está almacenado.                                                              |
| `loom.bulk_write`                 | Envía las reglas del bot (`about="agent"`) a la cola de aprendizajes y escribe el resto mediante `memory.bulk_set`. |

Una sesión de programación es una transcripción cuyos `extra_tags` incluyen una etiqueta de origen de agente de programación (`source:claude-code`, `source:codex`, `source:cursor`), o una reproducción de trayectoria. Los dos pasos de programación dejan pasar sin cambios las demás transcripciones.

**Un `pipeline_steps` vacío ejecuta esta lista. Uno no vacío ejecuta exactamente los pasos que nombra, en orden**, sobre un único estado compartido. Si omites `loom.bulk_write`, no se escribe nada: la respuesta sigue informando de lo que se extrajo.

<a id="run-a-shorter-pipeline"></a>

## Ejecutar un pipeline más corto

Extraer y escribir sin contexto de deduplicación, para una importación única de datos que se sabe que son nuevos:

```bash
curl -s "$LOOM_BASE_URL/v1/memory.set_from_messages" \
  -H "Authorization: Bearer $LOOM_API_KEY" -H 'Content-Type: application/json' -d '{
  "org": "acme",
  "namespace": "support",
  "agent": "importer",
  "messages": [{ "role": "user", "content": "..." }],
  "pipeline_steps": [{ "step_id": "loom.llm_extract" }, { "step_id": "loom.bulk_write" }]
}'
```

Esto requiere un proveedor de LLM real (`LOOM_LLM_PROVIDER` y su clave); el proveedor falso de la pila predeterminada no extrae hechos significativos.

<a id="step-behaviour"></a>

## Comportamiento de los pasos

Cada descriptor es `{"step_id": ..., ...}`; el objeto completo se pasa al paso como su configuración.

- **Como máximo 16 pasos** por solicitud.
- **Los pasos `hook.*` se rechazan** con 422 `VALIDATION_FAILED`: el servidor no realiza ninguna llamada saliente en nombre de quien llama.
- **Los ids de paso desconocidos** se omiten y se informan en `pipeline_errors` de la respuesta como `unknown_step:<id>`.
- **Pasos que fallan de forma abierta.** Un paso de obtención que falla registra el error en `pipeline_errors` (por ejemplo, `fetch_existing_failed:<error>`) y el pipeline continúa.
- **`loom.llm_extract` falla de forma cerrada**: un error de extracción aborta la solicitud con 502, de modo que una transcripción nunca recibe en silencio la respuesta `written=[]`. Acepta `{"judge": true}` en su descriptor para activar el filtro de juicio; `judge_extraction=true` en la solicitud hace lo mismo para la lista predeterminada.
- **Tiempos.** El campo `stage_ms` de la respuesta incluye el tiempo real de cada paso (`step.<step_id>`). Cada paso también se ejecuta dentro de un span de OpenTelemetry llamado `pipeline.step` con el id del paso como atributo.

<a id="register-an-in-process-step"></a>

## Registrar un paso en proceso

Un operador registra un paso en código que el proceso del servidor importa al arrancar:

```python
from server.memory.app.pipeline import WorkflowState, register_step


async def spam_filter(state: WorkflowState, config: dict) -> WorkflowState:
    state.extracted_facts = [
        f for f in state.extracted_facts if "spam" not in str(f.get("value", "")).lower()
    ]
    return state


register_step("operator.spam_filter", spam_filter)
```

Las solicitudes pueden entonces nombrar `{"step_id": "operator.spam_filter"}`; cualquier otra clave del descriptor llega como `config`. El paso se ejecuta en proceso con acceso de lectura y escritura a todo el `WorkflowState`: identidad, mensajes, memorias existentes, hechos extraídos, contadores. `register_step` reemplaza un registro existente con el mismo id, por lo que un paso del operador también puede reemplazar uno integrado.

Un paso que reemplace a `loom.bulk_write` debe dejar fuera de lo que escribe los hechos con `about="agent"`: son las reglas del bot, que el paso integrado envía a la cola de aprendizajes.

> **Advertencia:** un paso que descarta o reescribe `extracted_facts` cambia lo que almacena cada quien llama que lo nombra. Un paso ubicado después de `loom.bulk_write` ve las filas escritas en `state.written`, pero no puede deshacer su escritura.

Para añadir un paso integrado a Loom, agrega la función y su llamada a `register_step` en `server/memory/app/pipeline.py`, colócalo en `DEFAULT_PIPELINE` y ejecuta `make bench-quick` antes y después: la lista predeterminada da forma a cada extracción.

<a id="limits"></a>

## Límites

- Los pasos se ejecutan de forma secuencial; no hay ramificación ni paralelismo.
- La lista predeterminada es global; no hay un pipeline por espacio de nombres.
- No hay reintentos a nivel de paso.

<a id="related"></a>

## Relacionado

- [Extracción](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/extraction.md)
- [Extender Loom](https://github.com/ClickHouse/loom/blob/main/docs/development/extending-loom.md)
- [Escribir memoria](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/writing.md)
