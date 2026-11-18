<!-- i18n: source=docs/features/integrations/langfuse.md blob=6a704ef0765498c439dcf20e7c5ac032a353ecb4 model=claude-sonnet-5-5 -->
> Esta página es una traducción automática de la [página en inglés](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/langfuse.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/langfuse.md), which is authoritative.

<a id="langfuse"></a>

# Langfuse

Necesita `LOOM_LANGFUSE_URL` y `LOOM_LANGFUSE_ADMIN_TOKEN`; no se envía nada hasta que ambos estén definidos. Una vez definidos, el árbol de spans de OpenTelemetry se reenvía a Langfuse mediante OTLP.
Langfuse es opcional y no forma parte de la pila Compose local de Loom.

La exportación de spans y el borrado se prueban con **Langfuse 3.227.0** y
**Langfuse 4.76.0** autoalojados. Langfuse Cloud ejecuta Langfuse 4. Un despliegue local de Langfuse también
ejecuta PostgreSQL, ClickHouse, Redis/Valkey y almacenamiento de objetos compatible con S3, por lo que es
considerablemente más grande que el perfil de dos servicios de Loom.

Loom mantiene su propia telemetría en ClickHouse ([telemetría](https://github.com/ClickHouse/loom/blob/main/docs/features/observability/telemetry.md)).
Langfuse añade una traza por solicitud con llamadas anidadas al modelo, costo por generación,
puntuaciones filtrables y versiones de prompts. Loom desempeña cuatro roles frente a él:

| Rol                      | Qué hace Loom                                                                                                         | Necesita                          |
| ------------------------ | --------------------------------------------------------------------------------------------------------------------- | --------------------------------- |
| Escritor de trazas       | el árbol de spans de OTel de cada solicitud, con sus generaciones, reenviado mediante OTLP                            | un token `pk:sk`, OTel activado   |
| Escritor de puntuaciones | puntuaciones de calidad de recuperación en la traza de cada búsqueda y consulta (ask)                                 | un token `pk:sk`                  |
| Vinculador de prompts    | etiqueta cada generación con el nombre y la versión del prompt de Langfuse que usó, a partir de un lockfile confirmado | el lockfile; sin llamada en tiempo de ejecución |
| Backend de borrado       | elimina las trazas de un usuario o espacio de nombres borrado                                                         | URL y token (cualquier forma)     |

Loom **no** está en la ruta de obtención de prompts. Un agente que gestiona sus propios prompts en
Langfuse los obtiene con `get_prompt` del SDK de Langfuse.

<a id="setup"></a>

## Configuración

```bash
LOOM_LANGFUSE_URL=https://langfuse.example.com
LOOM_LANGFUSE_ADMIN_TOKEN=pk-lf-...:sk-lf-...
```

El token es un par `public_key:secret_key`, enviado como HTTP Basic. Un único token bearer
(sin `:`) sigue funcionando para el borrado, pero desactiva la emisión de trazas y de puntuaciones; el servidor registra
una advertencia que lo indica.

Comprueba el resultado con `GET /v1/health?include=langfuse`: informa del estado del exportador local
y, en `scores` y `retrieval_judge`, cuántas puntuaciones se enviaron, fallaron o se descartaron
(cola llena, circuit breaker abierto). No es una sonda de disponibilidad remota de Langfuse. El token `mode` del
registro de arranque incluye `langfuse` cuando ambas variables están presentes.

<a id="traces"></a>

## Trazas

Loom añade un segundo procesador de spans junto al exportador de ClickHouse y reenvía el árbol de spans de OpenTelemetry al endpoint OTLP de Langfuse. La traza conserva el anidamiento real (el span HTTP, el handler y cada generación), el id de sesión y el costo por generación.
Una llamada de chat es una observación `GENERATION` y una llamada de embedding que llegó al proveedor es una `EMBEDDING`; un embedding servido desde la caché propia de Loom es un span simple. Cada span que una solicitud abre una vez resuelto su llamador lleva el id de usuario, el id de sesión, las etiquetas `org:` y `namespace:` y el nombre de la traza de la solicitud, porque Langfuse 4 los lee por observación y no desde la raíz de la traza. Estas claves también llegan a los spans de ClickStack.
Cada etapa de búsqueda (`search.<stage>`) es una observación `retriever`, uno de los tipos a partir de los cuales se dibuja la vista de grafo de agentes de Langfuse. Con OTel desactivado (`LOOM_OTEL_ENABLED=false`) ninguna traza llega a Langfuse.

| Configuración                                                                 | Valor por defecto                                   | Efecto                                                                                                   |
| ----------------------------------------------------------------------------- | --------------------------------------------------- | -------------------------------------------------------------------------------------------------------- |
| `LOOM_LANGFUSE_SAMPLE_RATE`                                                   | `1.0`                                               | fracción de trazas reenviadas, muestreadas por id de traza para que cada traza llegue completa           |
| `LOOM_LANGFUSE_SPAN_DENYLIST`                                                 | `ch.query,ch.insert,GET /v1/health,POST /v1/health` | prefijos de nombre de span que nunca se reenvían (los spans de infraestructura pertenecen a ClickStack)  |
| `LOOM_LANGFUSE_CIRCUIT_FAILURE_THRESHOLD`, `LOOM_LANGFUSE_CIRCUIT_COOLDOWN_S` | `5`, `30`                                           | el interruptor: tras N POST de scores fallidos consecutivos, los scores se descartan durante el enfriamiento y después se hace una sola sonda |

Langfuse nunca está en la ruta de solicitud de Loom. El interruptor evita que los POST de scores fallidos se conviertan en presión de reintentos; el procesador por lotes de OTLP gestiona su propia cola de entrega, por lo que un fallo en la exportación de spans **no** lo abre. Por tanto, el gauge `loom.langfuse.breaker` (0 cerrado, 1 semiabierto, 2 abierto) refleja el envío de scores, no la disponibilidad remota. Las trazas de gasto de los workers se llaman `worker.<name>` e incluyen la organización y el espacio de nombres del worker. Los spans de embedding que son raíces de traza (el backfill de embeddings) y las raíces de pasada de los workers nunca se reenvían; un embedding en la ruta de solicitud se anida bajo el span de su handler y se conserva.

> **Tip:** el volumen de spans está dominado por los embeddings de los workers. Reduce
> `LOOM_LANGFUSE_SAMPLE_RATE` en producción antes de configurar Langfuse.

<a id="what-text-leaves-loom"></a>

## Qué texto sale de Loom

Cuánto contenido del usuario llega a Langfuse:

- **Payloads de solicitud y respuesta.** Nunca se envían: los spans no incluyen cuerpos de solicitud ni de respuesta.
- **Texto del prompt y de la respuesta de las llamadas a modelos propias de Loom**, según lo que contenga el prompt:

  | `LOOM_LANGFUSE_CAPTURE_IO_LEVEL` | Registra                                                                                                                                       |
  | -------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------- |
  | `off` (default)                  | nada                                                                                                                                           |
  | `query`                          | solo las etapas cuyo prompt es el texto de búsqueda del propio usuario (HyDE, reescritura de consultas, enriquecimiento, …)                    |
  | `all`                            | también las etapas que llevan memorias o turnos de conversación (rerank, juez iterativo, extracción, resumen de sesión, …)                     |

  El campo `capture_generation_io` de un espacio de nombres (se define con `namespace.register`) lo sobrescribe: `false` significa off sin importar lo que diga el servidor, `true` significa `all`, y si no se define, hereda el valor del servidor. Cada campo se limita a `LOOM_LANGFUSE_GENERATION_IO_MAX_CHARS` (2000) con un marcador de truncamiento explícito. El mismo nivel controla `loom.search_trace_log.query`.

Los recuentos de tokens, el costo, la latencia, las puntuaciones de calidad y los enlaces de versión de prompt no contienen contenido del usuario y nunca se restringen.

<a id="scores"></a>

## Puntuaciones

`server/observability/langfuse_scores.py` publica puntuaciones de calidad de recuperación en la traza de cada búsqueda: `retrieval_confidence`, `top_similarity`, `abstained` y `abstain_reason`. `code.ask` añade `code_ask_cache_hit` y `code_ask_coverage`. Con `LOOM_RETRIEVAL_JUDGE_SAMPLE_RATE` por encima de 0 (por defecto 0; cuesta una llamada a un modelo por cada búsqueda muestreada), un juez LLM independiente escribe una puntuación `retrieval_relevance` que no comparte ningún estado con la recuperación.

Las puntuaciones permanecen fuera de la ruta de la petición: una cola acotada, que se vacía en segundo plano, descarta primero las más antiguas cuando Langfuse va lento y cuenta los descartes. Una puntuación solo se publica cuando su traza fue reenviada (la misma regla de muestreo) y queda detrás del mismo interruptor de circuito.

<a id="prompt-versions"></a>

## Versiones de prompts

Los prompts de sistema de Loom son constantes de Python. `scripts/codegen/push_langfuse_prompts.py`
los registra en Langfuse y escribe `server/observability/langfuse_prompts.lock.json`,
indexado por `sha256(prompt)[:12]`. En tiempo de ejecución, `server/observability/genai.py` busca en ese lockfile el
prompt que envió y etiqueta la generación con el nombre del prompt en Langfuse y su versión
entera, sin hacer ninguna llamada a Langfuse. Un prompt editado que no se haya enviado queda
sin vincular, en lugar de vincularse a una versión incorrecta.

```bash
python scripts/codegen/push_langfuse_prompts.py          # plan against the lockfile, offline
python scripts/codegen/push_langfuse_prompts.py --write  # push, then write the lockfile
```

`--write` necesita ambos ajustes de Langfuse. Un prompt cuyo texto ya es la última versión
no se vuelve a enviar.

`POST /v1/admin/prompts.evaluate` (administrador de la plataforma) devuelve un veredicto de si conviene promover
una versión de un prompt a partir de las puntuaciones de rúbrica de sus trazas, frente a un recuento mínimo y un umbral.
Cada generación vinculada a una versión de prompt etiqueta su traza como `prompt:<name>:<version>`, y
la evaluación lee hasta 200 trazas con esa etiqueta y sus puntuaciones, tanto en Langfuse 3 como en 4.
Nunca escribe en Langfuse, responde 501 `INTEGRATION_NOT_CONFIGURED` cuando
Langfuse no está configurado, y 502 `BACKEND_UNAVAILABLE` cuando no se puede leer Langfuse.

<a id="erasure"></a>

## Borrado

`erasure.request` ejecuta el backend de Langfuse (`LangfuseBackend` en
`server/erasure/app/backends.py`) como un paso de su saga:

- **Un alcance de usuario** busca las trazas del usuario filtradas por `userId` y por la etiqueta `org:<org>`,
  y luego las elimina por lotes con `DELETE /api/public/traces`. Eliminar una traza borra
  sus observaciones y puntuaciones. El filtro de organización es importante porque cada organización escribe en el
  único proyecto de Langfuse que implica el par de claves, y un `user_id` solo es único dentro de una organización.
- **Un alcance de espacio de nombres** elimina las trazas que llevan tanto `namespace:<ns>` como `org:<org>`.

La búsqueda depende de la versión mayor del servidor, que se lee de `GET /api/public/health`:
Langfuse 3 lista trazas (`GET /api/public/traces`), y Langfuse 4, que ya no ofrece
ese listado, lista observaciones (`GET /api/public/v2/observations`) y recopila los ids de sus trazas.
Langfuse 4 elimina de forma asíncrona, por lo que una traza eliminada puede seguir visible durante unos pocos
minutos. La versión del servidor se lee una vez por cada paso de borrado.

En un despliegue actualizado desde Langfuse 3, la búsqueda de Langfuse 4 solo encuentra lo que contiene el
listado de observaciones, y un borrado no informa de nada más. Las trazas que Langfuse 3.227.0
almacenó están ahí (probado actualizándolo in situ a 4.76.0): Loom envía la
cabecera `x-langfuse-ingestion-version: 4`, y esa versión las escribe donde Langfuse 4
lee. Para las trazas almacenadas por una versión anterior de Langfuse 3, ejecuta el backfill
histórico de Langfuse después de la actualización y antes de confiar en el borrado.

El paso falla de forma cerrada. Una respuesta de health sin versión, un 5xx, un tiempo de espera agotado o un 404 del
endpoint de eliminación (una compilación autoalojada más antigua, o un proxy que solo reenvía GET y
POST) lanza un error, la saga marca la solicitud como `failed` y el worker `erasure_recovery`
la reintenta. Una nueva ejecución es idempotente, porque eliminar una traza ya eliminada tiene éxito; en
Langfuse 4 puede encontrar y volver a eliminar las trazas que aún están pendientes de borrarse. Con Langfuse
sin configurar, un backend de relleno registra la intención e informa 0.
`LOOM_EXTERNAL_ERASURE_TIMEOUT_S` (30) limita cada llamada. Consulta [retención y
borrado](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/retention-and-erasure.md).

<a id="joining-a-request"></a>

## Unir una solicitud

El id de solicitud que Loom devuelve en `X-Request-Id` está en los metadatos de la traza de Langfuse, en
`loom.agent_events.request_id` y en el atributo de span `loom.request_id`, de modo que se puede seguir una solicitud
desde Langfuse hasta el registro de Loom. Consulta
[unir una solicitud entre sistemas](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/index.md#joining-a-request-across-systems).

<a id="related"></a>

## Relacionado

- [Telemetría](https://github.com/ClickHouse/loom/blob/main/docs/features/observability/telemetry.md): el lado de ClickHouse de los mismos spans.
- [ClickStack](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/clickstack.md).
- [Referencia de configuración](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md#observability) para cada ajuste `LOOM_LANGFUSE_*`.
