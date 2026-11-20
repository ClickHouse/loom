<!-- i18n: source=docs/guides/performance-tuning.md blob=285ede1bf84d35f39aa2de165f838e15263c115e model=claude-sonnet-5-5 -->
> Esta página es una traducción automática de la [página en inglés](https://github.com/ClickHouse/loom/blob/main/docs/guides/performance-tuning.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/guides/performance-tuning.md), which is authoritative.

<a id="tune-looms-performance"></a>

# Ajustar el rendimiento de Loom

Cada palanca de esta página es un ajuste de configuración o un campo de solicitud. La
[referencia de configuración](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md) indica el valor predeterminado y el
significado de cada ajuste. Cuando un equilibrio se ha medido, la página nombra el comando que lo mide.

<a id="measure-first"></a>

## Mide primero

| Pregunta                                           | Comando                                                                               | Requiere                                             |
| -------------------------------------------------- | ------------------------------------------------------------------------------------- | ---------------------------------------------------- |
| p50 / p95 / p99 y RPS de una op con concurrencia N | `make bench OP=memory.search C=5 D=60` (`scripts/ops/benchmark.py`)                   | un servidor en ejecución; las escrituras van a un espacio de nombres `bench-…` |
| ¿Un cambio de ranking o de embedder perjudicó el recall? | `make bench-quick` (un fragmento de un benchmark público de memoria a largo plazo con un juez de respuestas) | proveedores reales, `OPENAI_API_KEY`                 |
| Recall frente a la línea base registrada           | `make bench-quality-gate MIN_RECALL=…`                                                | proveedores reales                                   |
| modo de búsqueda × rerank × ancho del pool         | `make bench-matrix` (escribe `bench/MATRIX_RESULTS.md`)                               | un servidor en ejecución                             |
| Regresiones de extracción y de extracción de grafos | `make bench-algorithm-gate`                                                          | un servidor en ejecución                             |
| Símbolo, callers, recall y costo de tokens del plano de código | `make bench-code-intel`, `make bench-code-legs`                    | un embedder real                                     |
| Qué cambió después de un despliegue                | `loom deploy-delta` (formas de las sentencias del ledger y de ClickHouse a cada lado del swap) | `var/deployed.json`                  |

> **Advertencia:** un valor de recall medido con el embedder falso no dice nada sobre la
> recuperación real. Por eso los quality gates se mantienen fuera de CI. Ejecútalos contra
> un servidor con los proveedores que vas a desplegar, y vuelve a fijar la línea base tras
> cualquier cambio de embedder.

Para una sola petición lenta, lee su cabecera `Server-Timing` (`app` frente a `ch`) y sus
filas de `system.query_log` antes de tocar cualquier ajuste ([depuración](https://github.com/ClickHouse/loom/blob/main/docs/guides/debugging.md)). El costo por
operación en todo el despliegue es una consulta sobre `log_comment`:

```sql
SELECT splitByChar(' ', log_comment)[1] AS source,
       count() AS queries, sum(read_bytes) AS bytes, sum(query_duration_ms) AS ms
FROM system.query_log
WHERE type = 'QueryFinish' AND event_time > now() - INTERVAL 1 HOUR AND log_comment != ''
GROUP BY source ORDER BY ms DESC LIMIT 20;
```

El tráfico de benchmark justo después de un swap lee cachés frías; espera antes de
comparar la latencia de un despliegue con la de la compilación anterior.

<a id="ranking-and-thresholds"></a>

## Ranking y umbrales

`memory.search` ordena un conjunto de candidatos de primera etapa y después aplica términos de ranking y umbrales ([recuperación y ranking](https://github.com/ClickHouse/loom/blob/main/docs/concepts/retrieval-and-ranking.md)). La mayoría de los términos se pueden configurar por despliegue y sobrescribir por solicitud, de modo que un cambio se puede probar con A/B primero en un solo cliente.

| Palanca                                      | Configuración (campo de la solicitud)                                                                                                                                   | Compromiso                                                                                                                         |
| -------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------- |
| Umbrales de similitud                        | `LOOM_ADAPTIVE_THRESHOLDS` (activado por defecto)                                                                                                                       | Activado: los umbrales se derivan al arrancar de la propia distribución de coseno del embedder. Desactivado: usted gestiona los umbrales escalares de abajo. |
| Umbrales escalares (respaldo)                | `LOOM_MEMORY_SEARCH_MIN_SIMILARITY`, `LOOM_MEMORY_SEARCH_ABSTAIN_THRESHOLD`, `LOOM_MEMORY_SEARCH_HYDE_FALLBACK_THRESHOLD`, `LOOM_MEMORY_SEARCH_QUERY_REWRITE_THRESHOLD` | Ajustados para `text-embedding-3-small`; se usan con embedders falsos, una sonda fallida o la adaptación desactivada.              |
| Resultados bajo el umbral mínimo             | `LOOM_MEMORY_SEARCH_FLOOR_MODE` (`floor_mode`)                                                                                                                          | `readmit` completa `top_k` y marca `below_floor`; `ids_only` completa con cuerpos vacíos; `drop` devuelve menos.                   |
| Conjunto de candidatos                       | `LOOM_MEMORY_SEARCH_POOL_MULTIPLIER_STANDARD`, `_RERANK`, `_DEEP`, limitado por `LOOM_MEMORY_SEARCH_MAX_POOL_TOP_K`                                                      | Un conjunto más amplio aumenta la cobertura y la entrada del rerank; cuesta filas vectoriales leídas y tokens de rerank.           |
| Omitir rerank                                | `LOOM_LLM_RERANK_SKIP_ABOVE` (respaldo cuando el umbral adaptativo no tiene sonda)                                                                                      | Omite la llamada de rerank cuando el mejor resultado vectorial ya es sólido.                                                       |
| Cascada de respaldo                          | `LOOM_MEMORY_SEARCH_FALLBACK_MAX_ROUNDS`, `_MAX_WALL_MS`, `_MAX_LLM_CALLS`, `_MAX_VECTOR_QUERIES`                                                                       | Limita los reintentos que puede gastar una primera pasada débil (reescritura de la consulta, HyDE).                                |
| Refuerzo                                     | `LOOM_MEMORY_SEARCH_REINFORCEMENT_WEIGHT` (`reinforcement_weight`), `_HALF_LIFE_DAYS`                                                                                   | Cuánto elevan una fila las recuperaciones y los resultados positivos; 0 lo desactiva. Limitado por debajo de la relevancia.        |
| Procedencia                                  | `LOOM_MEMORY_SEARCH_TRUST_WEIGHT` (por defecto 0)                                                                                                                       | Un término acotado que favorece las fuentes verificadas.                                                                           |
| Recencia                                     | `LOOM_MEMORY_SEARCH_RECENCY_POOL_WEIGHT` (`recency_pool_weight`)                                                                                                        | Reordena el conjunto hacia filas recientes en una consulta con intención de recencia; 0 = desactivado.                             |
| Rama léxica en consultas con forma de código | `rrf_adaptive_lexical` (solicitud), `LOOM_MEMORY_RRF_CODE_SHAPE_LO` / `_HI`                                                                                             | Reduce el peso de la rama léxica para consultas con forma de ruta o de código bajo RRF.                                            |
| Modo por defecto                             | `LOOM_MEMORY_SEARCH_DEFAULT_MODE` (`search_mode`)                                                                                                                       | Se usa cuando el cliente no indica ni un modo ni una receta.                                                                       |
| Duplicados por clave                         | `LOOM_MEMORY_SEARCH_PER_KEY_CAP`                                                                                                                                        | Filas que una clave puede aportar a una página.                                                                                    |

> **Advertencia:** un umbral ajustado para un embedder es incorrecto para el siguiente. Con
> `LOOM_ADAPTIVE_THRESHOLDS=false`, o con un embedder falso, vuelva a ejecutar el benchmark (`make bench-quick`,
> `make bench-quality-gate`) después de cada cambio de embedder. `calibration.get` muestra qué
> nivel produjo los umbrales vigentes para un espacio de nombres.

<a id="vector-reads"></a>

## Lecturas vectoriales

Las columnas vectoriales son `Array(BFloat16)`; donde `enable_quantized_codec` puede estar activado, el migrador las crea codificadas ([ClickHouse](../architecture/clickhouse.md#gated-clickhouse-settings)).

| Palanca                                  | Ajuste                                                                                                         | Compromiso                                                                                                                                                                                                                                                                                                                                                                                                                                  |
| ---------------------------------------- | -------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Escaneo de códigos primero               | `LOOM_VECTOR_SEARCH_QUANTIZED_CODES` (activado por defecto), `LOOM_MEMORY_SEARCH_FACT_CODES_FIRST` (`auto`)    | Lee los códigos cuantizados y luego vuelve a puntuar con precisión completa. `auto` lo desactiva mientras `memory_embeddings` tenga partes de parche, donde se omitiría la nueva puntuación.                                                                                                                                                                                                                                                  |
| Sobrecarga de lectura de códigos         | `LOOM_VECTOR_SEARCH_QUANTIZED_FETCH_MULTIPLIER`, `LOOM_MEMORY_SEARCH_FACT_CODES_FETCH_MULTIPLIER`              | Filas que se conservan por distancia de código antes de la nueva puntuación. Un valor menor es más barato y cambia con más frecuencia los primeros resultados.                                                                                                                                                                                                                                                                                |
| Hilos por sentencia de hechos            | `LOOM_MEMORY_SEARCH_FACT_LEG_MAX_THREADS` (0 = sin fijar)                                                      | Fijar los hilos limita la CPU y la memoria máxima por sentencia a costa de algo de latencia.                                                                                                                                                                                                                                                                                                                                                 |
| Lecturas de pares en escrituras masivas  | `LOOM_MEMORY_BULK_PEER_READ` (por defecto `batched`; `per_fact`)                                               | `per_fact` lee los pares de conflicto y de actualización de valor de cada hecho masivo con códigos primero, como hace `memory.set`: para espacios de nombres grandes, donde la sentencia de pares por lotes falla con `MEMORY_LIMIT_EXCEEDED` (`batched conflict candidates failed` en el log del servidor) y una entrada se escribe sin verificar cuando también falla su alternativa por hecho. Un par puede diferir cuando las distancias empatan. |

Para mover una tabla existente a un códec o a una disposición, usa `scripts/ops/vector_codec_rebuild.py` (tabla gemela, controles de consistencia y de recall, `EXCHANGE`; [pasos](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#recommended-clickhouse-settings)), nunca un `ALTER` en una migración.

<a id="clickhouse-connections-and-deadlines"></a>

## Conexiones y plazos de ClickHouse

| Palanca                   | Configuración                                                                                                                                                                       | Compromiso                                                                                                                                                                                                                                                                         |
| ------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Tamaño del pool           | `LOOM_CH_POOL_MAXSIZE` (por defecto 64 por proceso)                                                                                                                                 | Dimensiónalo según la concurrencia de solicitudes esperada más los workers, más las conexiones que las inserciones por lotes retienen durante su espera; el valor por defecto del propio driver se agota con escrituras masivas.                                                    |
| Inserciones por lotes     | `LOOM_CH_BATCHED_INSERTS` (por defecto activado), `LOOM_CH_BATCHED_INSERT_WAIT_MIN_MS` (10), `LOOM_CH_BATCHED_INSERT_WAIT_MAX_MS` (50), `LOOM_CH_BATCHED_INSERT_BACKGROUND_WAIT_MAX_MS` (200) | Las inserciones concurrentes comparten una parte, con una latencia de hasta la de la ventana cada una; una inserción aislada espera el mínimo; cada inserción en espera retiene una conexión del pool, por lo que el pool crece con la tasa de escritura ([inserciones por lotes](../architecture/clickhouse.md#batched-inserts)). |
| Espera del pool           | `LOOM_CH_POOL_WAIT_MS` (por defecto 500)                                                                                                                                            | Pasado ese tiempo, una solicitud falla rápido con 503 `OVERLOADED` en lugar de quedar en cola; 0 = sin límite.                                                                                                                                                                     |
| Keep-alive                | `LOOM_CH_KEEPALIVE_TIMEOUT` (por defecto 5 s)                                                                                                                                       | Debe mantenerse por debajo del corte por inactividad del balanceador de carga, de modo que Loom cierre primero una conexión del pool.                                                                                                                                              |
| Plazos de sentencias      | `LOOM_CH_TIMEOUT_READ_S`, `LOOM_CH_TIMEOUT_WRITE_S`, `LOOM_CH_TIMEOUT_DDL_S`                                                                                                        | Se aplican como `max_execution_time` y como un límite del cliente que libera el espacio del pool; 0 = sin límite para esa clase.                                                                                                                                                   |
| Prioridad por clase de tráfico | `LOOM_CH_SETTINGS_BY_TRAFFIC_CLASS` (JSON)                                                                                                                                     | `priority` y `max_threads` por clase (`production`, `interactive`, `bench`, `background`), de modo que los benchmarks y los workers cedan ante las lecturas de producción.                                                                                                         |
| Lectura de las propias escrituras | `LOOM_CH_READ_YOUR_WRITES`, `LOOM_CH_READ_YOUR_WRITES_WINDOW_S`                                                                                                             | Lecturas secuencialmente consistentes después de una escritura; tiene un costo en servicios replicados y no hace nada en un solo nodo.                                                                                                                                             |
| Presupuestos de operación | `LOOM_LATENCY_BUDGET_MULTIPLIER`                                                                                                                                                    | Escala el `budget_p99_ms` de cada op; un salto WAN hacia ClickHouse necesita cerca de 20. Los presupuestos solo generan eventos `budget.violated`; no cortan solicitudes.                                                                                                          |

La capacidad web es `LOOM_WEB_WORKERS` por contenedor multiplicado por el número de contenedores; el limitador de tasa divide
sus límites entre las réplicas activas que cuenta en `loom.instances`, nunca menos que
`LOOM_REPLICA_COUNT` ([réplicas](https://github.com/ClickHouse/loom/blob/main/docs/operations/replicas.md#rate-limits)).

<a id="providers"></a>

## Proveedores

| Palanca                                  | Configuración                                                                                                                     | Compromiso                                                                                                                                                                                                                         |
| ---------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Límite del embedding de consulta         | `LOOM_MEMORY_SEARCH_EMBED_BOUND_S`, `LOOM_MEMORY_SEARCH_EMBED_BOUND_RETRIES`                                                      | Límite por intento sobre el embedding de la consulta; tras los reintentos la búsqueda responde 503 `EMBED_TIMEOUT`.                                                                                                                |
| Ortografía de la consulta                | `LOOM_MEMORY_SEARCH_QUERY_SPELLING_MAX_WORDS`                                                                                     | Una consulta de como máximo este número de palabras lee el espacio de nombres una vez, junto al embedding, para generar el embedding de sus palabras en minúsculas con la ortografía del corpus: 100–300 ms para un espacio de nombres de 40k filas, reutilizado durante un minuto; `0` = desactivado. |
| Cachés del embedding de consulta         | `LOOM_EMBEDDING_CACHE_SIZE`, `LOOM_EMBEDDING_CACHE_TTL_S`, `LOOM_EMBEDDING_CACHE_SHARED`, `LOOM_EMBEDDING_CACHE_SHARED_MAX_CHARS` | Una LRU por proceso delante de un `loom.embedding_cache` compartido; las consultas repetidas se saltan la llamada al proveedor.                                                                                                    |
| Caché de respuestas del LLM por clase    | `LOOM_LLM_POLICY_BY_TRAFFIC_CLASS` (JSON)                                                                                         | Almacena en caché las respuestas del LLM (activado por defecto para `bench`) y puede impedir que una clase use el nivel fuerte o el intermedio.                                                                                    |
| Modelo de respaldo                       | `LOOM_LLM_FALLBACK_MODEL`                                                                                                         | Responde cuando el principal sigue sin estar disponible tras los reintentos, en lugar de hacer fallar la op.                                                                                                                       |
| Tope de gasto                            | `LOOM_DEFAULT_DAILY_COST_CAP_USD` (por espacio de nombres `daily_cost_cap_usd`)                                                   | Al superar el tope, los workers basados en LLM omiten el espacio de nombres y `memory.ask` se abstiene con `budget_capped`; las lecturas y escrituras simples continúan.                                                           |

<a id="embedder-choice"></a>

### Elección del embedder

| Embedder (`LOOM_EMBEDDING_MODEL`)                            | Compromiso                                                                                                      |
| ------------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------- |
| `text-embedding-3-large`, `gemini-embedding-001`             | El valor por defecto de Loom, de 3072 dimensiones; aun así hay que medir la calidad y el costo con tus datos.   |
| `text-embedding-3-small`                                     | Opción de vectores más estrechos, de 1536; cambiar la anchura requiere volver a generar los embeddings.         |
| `local`, `local:bge-small`, `local:bge-base`, `local:minilm` | Embedding en CPU dentro del proceso, sin llamada a un proveedor ni clave; sacrifica calidad por latencia y costo. |
| `fake-ngram-3072`                                            | N-gramas de caracteres con la anchura estándar, solo para pruebas y cableado; las cifras de recall no significan nada. |

Cambiar el embedder cambia el espacio vectorial: ajusta `LOOM_EMBEDDING_DIM` para que coincida, ejecuta
`loom reembed` y, con los umbrales adaptativos activados, deja que la sonda de arranque recalibre. Ejecuta
`make bench-quick` antes y después; lo mismo vale para `LOOM_LLM_MODEL`, con el que están calibrados
los prompts de extracción.

<a id="workers-and-batches"></a>

## Workers y lotes

Cada worker tiene un interruptor `*_ENABLED` y una cadencia `*_INTERVAL_SECONDS`, y muchos tienen
una pausa `*_IDLE_INTERVAL_SECONDS` después de una pasada que no encontró nada
([referencia de workers](https://github.com/ClickHouse/loom/blob/main/docs/reference/workers.md)). Los intervalos más cortos reducen el retraso
del trabajo en segundo plano a costa de más escaneos; el intervalo de inactividad mantiene barata una cola vacía.

| Worker                | Parámetros                                                                                                                                                                                                                           |
| --------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `embedding_worker`    | `LOOM_EMBEDDING_WORKER_INTERVAL_SECONDS`, `_IDLE_INTERVAL_SECONDS`; `LOOM_EMBEDDING_WORKER_ORPHAN_FULL_SCAN_SECONDS` / `_ORPHAN_RECENT_SECONDS` acotan el anti-join de huérfanos.                                                    |
| `extraction_queue`    | `LOOM_EXTRACTION_QUEUE_INTERVAL_SECONDS`, `_MAX_BATCHES_PER_PASS`, `_NAMESPACE_CONCURRENCY`, `_LEASE_SECONDS`; `LOOM_EXTRACTION_DEFER_AGENTS` difiere `set_from_messages` para los agentes indicados, de modo que la solicitud regrese antes de la extracción. |
| `memory_expiry_sweep` | `LOOM_MEMORY_EXPIRY_SWEEP_BATCH_SIZE`.                                                                                                                                                                                               |
| `task_lease_sweep`    | `LOOM_TASK_LEASE_SWEEP_INTERVAL_SECONDS`, `_IDLE_INTERVAL_SECONDS`.                                                                                                                                                                  |

Los workers se ejecutan en la clase de tráfico `background`, por lo que `LOOM_CH_SETTINGS_BY_TRAFFIC_CLASS` también
limita su prioridad y sus hilos en ClickHouse. Ejecuta exactamente un proceso de workers: una segunda
copia de un worker respaldado por un LLM duplica sus escaneos y su gasto. Los workers de ingesta del plano de código y del
nivel del compilador tienen sus propios parámetros ([operaciones del índice de código](https://github.com/ClickHouse/loom/blob/main/docs/operations/code-index-operations.md)).

<a id="client-side-levers"></a>

## Optimizaciones desde el cliente

Algunas de las mayores mejoras están en la propia solicitud:

- Pedir menos cuerpos: `bodies` y `body_budget_tokens` en `memory.search`, `token_budget`
  en `context.pack` y `code.search`.
- Cargar el contexto una sola vez por tarea (`context.pack`) en lugar de hacer una búsqueda por paso.
- Usar `retrieval_budget` para elegir un perfil de búsqueda más económico o más profundo en cada llamada.
- Reintentar las operaciones idempotentes con un encabezado `Idempotency-Key` (los SDKs generan uno): una
  repetición devuelve la respuesta almacenada en lugar de ejecutar la operación de nuevo.

<a id="related"></a>

## Relacionado

- [Fallos y recuperación](../architecture/failure-and-recovery.md): capacidad y qué escalar
- [Monitorización](https://github.com/ClickHouse/loom/blob/main/docs/operations/monitoring.md)
- [Ejecución de benchmarks](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/running.md)
- [Depuración](https://github.com/ClickHouse/loom/blob/main/docs/guides/debugging.md)
