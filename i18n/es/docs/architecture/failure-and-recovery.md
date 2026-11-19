<!-- i18n: source=docs/architecture/failure-and-recovery.md blob=40f8e4b57e275dada1e2090b1f563fcdaafd8095 model=claude-sonnet-5-5 -->
> Esta página es una traducción automática de la [página en inglés](https://github.com/ClickHouse/loom/blob/main/docs/architecture/failure-and-recovery.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/architecture/failure-and-recovery.md), which is authoritative.

<a id="failure-and-recovery"></a>

# Fallos y recuperación

La escritura principal y toda decisión de acceso fallan de forma cerrada (fail closed). Los datos derivados y la telemetría fallan de forma abierta (fail open) y se reparan. Una solicitud que devuelve éxito ha almacenado de forma duradera lo que prometió; todo lo que se proyecta junto a ella lo pone al día un worker. La última sección, [Capacidad](#capacity), explica qué escala y cómo, y dónde están los límites. Los runbooks por síntoma están en [solución de problemas](https://github.com/ClickHouse/loom/blob/main/docs/operations/troubleshooting.md).

<a id="errors-retries-and-idempotency"></a>

## Errores, reintentos e idempotencia

| Límite                          | Comportamiento                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| ------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Cliente de ClickHouse           | Un fallo transitorio de red en una sentencia idempotente se reintenta una vez en una conexión nueva. Un error del servidor ligado a una réplica (códigos 10, 242, 999) se reintenta en hasta dos conexiones nuevas. Cualquier otro error se propaga.                                                                                                                                                                                                                                                                                |
| Plazos de las sentencias        | Pasado el plazo de su clase (`LOOM_CH_TIMEOUT_READ_S`, `_WRITE_S`, `_DDL_S`), una solicitud falla con 504 `BACKEND_TIMEOUT` y no se reintenta.                                                                                                                                                                                                                                                                                                                                                                                       |
| Admisión al pool                | Si no hay un hueco libre dentro de `LOOM_CH_POOL_WAIT_MS`: 503 `OVERLOADED` con `Retry-After: 1`.                                                                                                                                                                                                                                                                                                                                                                                                                                   |
| Inserciones                     | Un INSERT reintentado lleva el mismo `insert_deduplication_token`, de modo que un lote ya confirmado no se escribe dos veces. Sin el token, una inserción no se reintenta. Una inserción por lotes comparte el flush de ClickHouse con inserciones concurrentes en la misma tabla; si un flush falla en conjunto, falla cada una de ellas, y cada reintento se aplica una sola vez bajo su propio token ([inserciones por lotes](https://github.com/ClickHouse/loom/blob/main/docs/architecture/clickhouse.md#batched-inserts)). |
| Ajustes no admitidos            | Si una réplica rechaza el ajuste de lectura de códigos cuantizados, el cliente lo descarta durante el resto del proceso y reintenta.                                                                                                                                                                                                                                                                                                                                                                                                |
| Lecturas puntuales              | Las lecturas posteriores a la propia escritura del llamador usan consistencia secuencial; un fallo justo después de una escritura vuelve a leer una vez (`LOOM_CH_READ_YOUR_WRITES`); una búsqueda en un espacio de nombres escrito en los últimos 20 s lee de forma secuencial.                                                                                                                                                                                                                                                     |
| Desajuste de esquema            | Una sentencia que nombra una tabla que crearía una migración pendiente se rechaza con 503 `SCHEMA_PENDING` (más abajo).                                                                                                                                                                                                                                                                                                                                                                                                             |
| Idempotencia de las ops         | Las ops declaradas `idempotent` aceptan `Idempotency-Key` (HTTP) o `_idempotency_key` (MCP). La respuesta se conserva en `loom.idempotency` durante 24 horas, con clave por organización, sujeto, op y clave. Un duplicado mientras se ejecuta el primero recibe un 409.                                                                                                                                                                                                                                                            |
| Escritura primaria              | La fila del hecho y su fila de vector son inserciones confirmadas y esperadas. Un fallo hace fallar la solicitud.                                                                                                                                                                                                                                                                                                                                                                                                                   |
| Autoridad de Postgres           | Con `LOOM_STORAGE_AUTHORITY=postgres`, la escritura es la confirmación en Postgres; sus filas de ClickHouse se publican a partir de ella y las repara el worker `pg_delivery`. Si Postgres no está disponible, las escrituras y las lecturas puntuales fallan en modo cerrado (503 `POSTGRES_UNAVAILABLE`) ([autoridad de almacenamiento](https://github.com/ClickHouse/loom/blob/main/docs/architecture/storage-authority.md#failure)).                                                                                              |
| Proyecciones                    | Las tablas laterales de grafo, fragmentos, evidencia y sustitución fallan en modo abierto y las reparan los workers.                                                                                                                                                                                                                                                                                                                                                                                                                |
| Telemetría                      | `agent_events` y el flujo de eventos se encolan en el proceso; el flush de `agent_events` es una inserción por lotes y el del flujo de eventos es una confirmada. Ninguno hace fallar una solicitud; un fallo del proceso puede perder la cola sin vaciar.                                                                                                                                                                                                                                                                           |
| Workers                         | Una pasada fallida se reintenta con espera progresiva. El trabajo se escribe como valores absolutos o versiones nuevas, así que una pasada repetida converge.                                                                                                                                                                                                                                                                                                                                                                       |
| Borrado                         | `erasure.request` es una saga con puntos de control. `erasure_recovery` la reanuda, y un censo de residuos sobre todas las tablas decide si tuvo éxito.                                                                                                                                                                                                                                                                                                                                                                              |

**Respuestas reintentables.** `OVERLOADED`, `AUTH_UNAVAILABLE` y `RATE_LIMITED` incluyen
`Retry-After` (y `retry_after_s`); reintenta después del tiempo indicado. `SCHEMA_PENDING` se resuelve cuando un
operador aplica la migración indicada. `EMBED_TIMEOUT`: un llamador HTTP o MCP
directo reintenta una vez; un llamador del SDK ya ha reintentado (ops idempotentes), así que debe esperar antes de volver a intentarlo.
`BACKEND_TIMEOUT` solo es reintentable para una op `idempotent`, y con una clave de idempotencia en el caso de una
escritura. La lista completa está en la [referencia de errores](https://github.com/ClickHouse/loom/blob/main/docs/reference/errors.md).

**Por qué la caché de idempotencia es de mejor esfuerzo:** una caché consistente en todo el clúster necesita un bloqueo por
solicitud. Las ops que la usan escriben el mismo contenido en una ejecución duplicada en una
`ReplacingMergeTree`, de modo que una condición de carrera cuesta una segunda ejecución del manejador, no un segundo efecto. Las ops cuya
repetición duplicaría un efecto (`memory.set_from_messages`) no son idempotentes y la
omiten.

<a id="consistency-boundaries"></a>

## Límites de consistencia

Las escrituras entre tablas no son transaccionales. El hecho y su vector se guardan juntos; los datos derivados se ponen al día después:

| Datos derivados                      | Se ponen al día                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   |
| ------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Estadísticas de BM25                 | Vistas actualizables cada minuto; la búsqueda lee una cola en vivo, por lo que el ranking se mantiene exacto (`LOOM_MEMORY_SEARCH_BM25_LIVE_TAIL`).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              |
| Contadores de uso, refuerzo          | Una pasada de `usage_fold` (cada 300 s).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| Proyección del grafo de entidades    | Poco después de la respuesta. Un `facts.traverse` del mismo espacio de nombres que empiece después de la respuesta la espera (como máximo 3 s) en cada proceso del despliegue: cada proyección en curso se registra en `loom.graph_projections` (claves de Keeper donde el servidor tiene un Keeper), y al terminar registra la escritura del grafo del espacio de nombres antes de borrarla, con el reloj del servidor. Un proceso que muere durante una proyección deja su registro durante 15 s como máximo: cada proceso lo espera una vez (3 s), y mientras el registro no caduque, los recorridos de ese espacio de nombres se leen siempre frescos, sin servirse del memo ni guardarse en él. Si hay más de 32 proyecciones simultáneas de un mismo espacio de nombres, las adicionales no se esperan; cada una cuenta como una escritura del grafo antes de que responda su `memory.set` y de nuevo cuando termina. |

**Por qué no transacciones:** ClickHouse no las tiene entre tablas, y emularlas añade un viaje de ida y vuelta y un modo de fallo a cada escritura. Un par primario duradero junto con una reparación idempotente mantiene la escritura en un solo viaje de ida y vuelta.

<a id="backpressure"></a>

## Contrapresión

Ninguna solicitud espera indefinidamente por un recurso compartido:

- **Pool de ClickHouse:** una espera acotada y luego `OVERLOADED` ([cliente de ClickHouse](#clickhouse-client)).
- **Bucket de documentos:** 8 transferencias y 4 streams por proceso, cada uno con una espera acotada
  (`LOOM_CH_POOL_WAIT_MS`) y luego `OVERLOADED`; los listados y borrados (borrado por solicitud de eliminación, el barrido)
  esperan uno de 4 slots propios.
- **Límites de tasa:** 429 `RATE_LIMITED` con `Retry-After`. Sobre MCP, el transporte espera una vez
  una pista corta (`LOOM_MCP_RATELIMIT_RETRY_MAX_WAIT_S`) y vuelve a comprobar.
- **Embedding de consultas:** `LOOM_MEMORY_SEARCH_EMBED_BOUND_S` (2.5 s) por intento, reintentado
  `LOOM_MEMORY_SEARCH_EMBED_BOUND_RETRIES` veces (1) y luego 503 `EMBED_TIMEOUT`.
- **Colas de telemetría:** el emisor de `agent_events` (10,000 filas) y el stream de eventos
  (20,000) descartan y cuentan las filas cuando están llenos (`dropped_queue_full`, `dropped_flush_failed`),
  y registran el primer descarte y luego uno de cada 1000. La entrega es como máximo una vez.
- **Workers:** `usage_fold` omite una pasada mientras ClickHouse tiene demasiadas mutaciones pendientes;
  cada pasada está acotada por el tamaño del lote y por un plazo.
- **Presupuestos:** una op que supera su `budget_p99_ms` aun así responde y deja una fila `budget.violated`
  (como máximo una por op cada `LOOM_BUDGET_VIOLATION_AUDIT_INTERVAL_S`, 60 s).

<a id="schema_pending"></a>

## `SCHEMA_PENDING`

Un servidor construido a partir de un commit más reciente que su esquema (un servidor de rama con
`LOOM_BOOT_MIGRATE=false`, o un color que arrancó antes de aplicar sus migraciones) puede nombrar una tabla
que todavía no existe. `server/core/ch_schema_guard.py` (`guard_pending_schema`) rechaza esa sentencia
con un 503 `SCHEMA_PENDING`, indicando la tabla y la migración. Una etapa de recuperación que trata una
lectura fallida como opcional continúa sin ella, y la fila del registro anota la tabla. Las sentencias DDL
nunca se rechazan.

El conjunto de migraciones pendientes proviene del registro de migraciones (`server/core/schema/status.py`),
y se mantiene en caché 60 s por proceso. Cuando el registro indica
que no se ha aplicado nada o que hay más de 25 archivos pendientes, el guardián queda inactivo y emite una sola advertencia.
`/v1/health` informa `schema.pending`, y un cambio blue/green espera a que ese conjunto esté vacío.

<a id="worker-supervision"></a>

## Supervisión de workers

`server/workers/supervisor.py` ejecuta cada worker habilitado como su propia tarea de asyncio:

- **Escalonamiento.** La primera pasada espera `crc32(name) % interval`, salvo que un worker cuya tabla de salida esté vacía se ejecuta de inmediato, y un worker cuya última pasada sea más antigua que su intervalo arranca dentro de los 300 s posteriores al inicio.
- **Plazo límite.** Una pasada que supera `min(4 × interval, 900 s)`, con un mínimo de 60 s (salvo que el worker declare el suyo propio), se cancela y se cuenta como un fallo.
- **Retroceso.** Una pasada fallida espera `min(60 s, 2^(n-1) s)`. Una pasada cortada en su plazo límite, o una pasada fallida de un worker cuyo intervalo es más largo que su plazo límite, espera el intervalo.
- **Degradado.** Tres fallos consecutivos registran `DEGRADED`; la racha es `loom.worker.consecutive_failures{worker}`, y una pasada con una etapa fallida cuenta en `loom.worker.partial_failures`.
- **Estado.** Cada pasada escribe una fila en `loom.worker_status` (`GET /v1/admin/workers.status`); un cursor de reanudación permite que un nuevo color continúe un escaneo largo.
- **Apagado.** `stop_all` cancela las tareas, espera hasta 10 s y luego vuelca las filas de estado.

Las excepciones de un worker permanecen en su tarea. Los workers escriben valores absolutos o nuevas versiones, de modo que una pasada que se ejecuta dos veces converge.

<a id="repair-workers"></a>

## Workers de reparación

| Worker                    | Repara                                                                                                                                                                       | Garantía                                                                                  |
| ------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------- |
| `projection_repair`       | Reproduce las proyecciones fallidas (vínculos de evidencia, fragmentos, tablas laterales del grafo, el espejo de búsqueda de la limpieza por expiración) registradas en `loom.memory_projection_failures`. | Las entradas no compatibles se conservan para su inspección.                              |
| `entity_extractor`        | Extrae entidades en las filas que no tienen ninguna; vuelve a emitir las filas de `memory_embeddings` cuyas `entities` difieren de `memory`.                                 | Toda fila con capacidad de búsqueda acaba teniendo sus entidades.                         |
| `orphan_vector_reconcile` | Marca con tombstone las filas de embeddings y de vectores de entidades cuya memoria tiene tombstone o ya no existe, tras un periodo de gracia.                               | Ningún vector sobrevive a su hecho.                                                       |
| `embedding_worker`        | Genera embeddings de los stubs con `embedding_dim = 0`, de los más nuevos a los más antiguos, y de las memorias sin fila de embedding; regenera los stubs de un modelo anterior. | Una fila que falla 5 veces se marca con `embed_failed_at` y se omite.                     |
| `erasure_recovery`        | Reanuda las sagas atascadas en `in_progress` o `failed` a partir de `steps_completed`.                                                                                       | Tras `max_attempts` (5): `failed` terminal más una fila de auditoría `erasure.abandoned`. |
| `task_lease_sweep`        | Devuelve las concesiones expiradas a `todo`, o a `blocked` tras agotar sus intentos; reanuda las tareas diferidas.                                                           | La tarea de un agente que falla no se pierde.                                             |
| `audit_verifier`          | Recalcula los hashes de auditoría y verifica la cadena de checkpoints.                                                                                                       | Las alteraciones se informan, no se reparan.                                              |

<a id="fail-closed-choices"></a>

## Decisiones de cierre ante fallos

Estas rechazan en lugar de degradarse, porque degradarse ampliaría el acceso o mentiría sobre un borrado:

- **Revocación.** Hasta que un proceso haya cargado una vez el conjunto de tokens revocados, todo token recibe 503 `AUTH_UNAVAILABLE`. Después de eso, una caída de ClickHouse conserva la última instantánea.
- **Autorización.** Una lectura fallida del mapa de permisos del sujeto responde 503, salvo que el proceso conserve una entrada en caché más reciente que `LOOM_JWT_REVOCATION_MAX_STALE_SECONDS` (600 s) que ningún cambio de permisos haya reemplazado, en cuyo caso la sirve ([token](https://github.com/ClickHouse/loom/blob/main/docs/architecture/security.md#tokens)). La consulta única de permisos (`authz.grant_role_for`) trata una lectura fallida como ausencia de permiso. Se deniega un espacio de nombres restringido cuya clasificación no se puede leer.
- **Migraciones de arranque.** Una migración fallida detiene el proceso antes de que se inicie cualquier worker.
- **Borrado.** Un backend federado que no puede confirmar el borrado hace fallar su paso; cualquier residuo dentro del alcance hace fallar la saga.
- **Secreto de firma de desarrollo.** Un despliegue que requiere autenticación se niega a iniciar con el secreto incluido en el repositorio.

Estas se abren ante fallos, con conteo o registro, porque rechazar dejaría el servicio caído por un control de disponibilidad: la clasificación del tráfico, el limitador de tasa, la lectura de la caché de idempotencia, la inserción de auditoría (excepto las dos restauraciones en [límites de pérdida de datos](#data-loss-boundaries)), la calibración de arranque, las cachés de embeddings y toda escritura de telemetría. Consulta [seguridad](https://github.com/ClickHouse/loom/blob/main/docs/architecture/security.md).

<a id="when-a-dependency-is-down"></a>

## Cuando una dependencia está caída

| Caída                 | Qué ocurre                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 |
| --------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| ClickHouse            | `/v1/health` y `/v1/health/ready` responden 503; `/v1/health/live` sigue en 200. Las solicitudes fallan con `OVERLOADED`, `BACKEND_TIMEOUT` o un 500 que incluye el id de la solicitud. Los tokens se verifican con la última instantánea de revocaciones. Los workers fallan en sus pasadas y esperan antes de reintentar.                                                                                                                                                                                                                                                                                                 |
| Proveedor de embeddings | `memory.set` y `memory.bulk_set` fallan: ningún hecho se almacena sin su vector. Las rutas que pueden esperar (`memory.promote`, embeddings de chunks) almacenan un stub para `embedding_worker`. Un turno de conversación se almacena con su error de embedding registrado y queda sin embeber (nada lo reintenta); `conversation.compact` no escribe ningún resumen y no marca ningún turno como eliminado (`embed-failed`). `memory.search` falla con 503 `EMBED_TIMEOUT` a menos que el vector de la consulta esté en la caché de embeddings. Un proveedor bloqueado no activa ningún indicador de salud (el log muestra `query embed exceeded`); uno que responde con errores muestra `provider_failing:<name>` en `/v1/health`. |
| Falta la clave de embeddings | El proceso usa el embedder falso, con un log de error y una entrada en `problems`; las búsquedas solo encuentran filas escritas con el modelo falso.                                                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| Proveedor de LLM      | Cada llamada se reintenta (cuatro intentos). Las etapas que fallan en abierto (HyDE, rerank, el verificador de extracción, los títulos) se omiten; la extracción y la síntesis de `memory.ask` no tienen sustituto. `LOOM_LLM_FALLBACK_MODEL` traspasa una caída a un segundo modelo.                                                                                                                                                                                                                                                                                                                                       |
| Bucket de documentos  | Las subidas y descargas de originales guardados en el bucket responden 503 `BACKEND_UNAVAILABLE`, y la descarga de uno que ya no existe responde 410. Un paso de borrado que llega al bucket falla en cerrado y `erasure_recovery` lo reintenta. El barrido de originales registra el fallo de la pasada y su recorrido del bucket se reanuda desde su cursor en la siguiente pasada.                                                                                                                                                                                                                                       |
| Langfuse, ClickStack  | El trazado y la exportación se pierden; las solicitudes no se ven afectadas. Un backend de borrado configurado que esté caído hace fallar su paso de la saga, y la recuperación lo reintenta.                                                                                                                                                                                                                                                                                                                                                                                                                              |

**Por qué `memory.set` falla en lugar de almacenar un stub:** un stub es invisible para todas las ramas de búsqueda hasta que se embebe, así que un llamador al que se le dijo "almacenado" no encontraría su propia escritura. Al fallar, puede reintentar con su clave de idempotencia.

<a id="bluegreen-rollback"></a>

## Rollback blue/green

`./redeploy.sh` inicia el color inactivo y cambia el tráfico solo después de que su `/v1/health` informe `status: ok`, con ClickHouse accesible, `schema.pending` vacío, ningún proveedor degradado y ninguna discrepancia en el ancho de los embeddings, dentro de `LOOM_REDEPLOY_READY_TIMEOUT_S` (una variable del script, 180 s).
El color anterior se detiene, no se elimina, y su imagen se etiqueta como `loom:prev`.
`./redeploy.sh --rollback` lo inicia y vuelve a cambiar el tráfico a él. Todas las migraciones son aditivas
(`tests/unit/test_migrations_additive.py`), por lo que la compilación anterior funciona con el esquema más reciente. Consulta [actualizaciones](https://github.com/ClickHouse/loom/blob/main/docs/operations/upgrades.md).

<a id="a-replica-that-dies-mid-migration-or-mid-reconfiguration"></a>

## Una réplica que muere durante una migración o una reconfiguración

Cada proceso es un participante del proveedor: una fila en `loom._provider_participants`, escrita
cada 10 s desde un hilo propio, y un User-Agent que indica su id en cada consulta a ClickHouse
que envía. Un participante se considera detenido cuando se retiró, o cuando su último heartbeat
tiene más de 60 s (reloj del servidor) y `system.processes` no muestra ninguna consulta suya en ningún
nodo. Nada más cuenta: un proceso inalcanzable no es un proceso detenido.

- **Terminado durante una migración.** Las migraciones retienen únicamente el bloqueo de migración (`ttl_s`, renovado mientras
  se aplican), nunca el bloqueo del proveedor, que un arranque retiene solo para comprobar que no queda ninguna reconstrucción
  sin terminar. Un segundo contenedor toma el control del bloqueo de migración una vez que expiró y aplica
  lo que queda.
- **Terminado mientras retiene el bloqueo del proveedor** (un paso de reconstrucción, una configuración guardada). El TTL de la clave
  es de 60 s, renovado cada 20 s; un competidor lo toma una vez que expiró y el
  participante del titular cuenta como detenido. `loom provider-lock clear --holder <holder>`
  libera uno cuyo titular no tiene fila de participante, lo cual nunca cuenta como detenido.
- **Terminado durante una reconfiguración.** La transición espera a un participante cuyo heartbeat
  expiró, ya que un proceso en pausa puede reanudarse con trabajo admitido, hasta que un proceso de
  su mismo host demuestra que ya no existe (su contenedor se reinició, o su pid terminó) y lo retira.
  El líder de workers
  elimina las filas de los participantes retirados hace un día y lista, una vez por minuto, aquellos a los que una
  transición esperaría como `provider_participants_stale` en su `/v1/health`. Retire
  uno manualmente con `loom participant retire --participant <id> --yes` una vez que se confirme que su proceso
  ya no existe; el comando rechaza a un participante cuyo heartbeat es reciente o cuyas
  consultas siguen en ejecución.
- **Un arranque que falla después de inscribirse** retira su fila; un contenedor en bucle de fallos no deja
  ninguna.

Un proceso congelado más allá de su heartbeat (un contenedor detenido, una pausa de la VM) mantiene la
transición hasta que se reanude y termine de drenar, o hasta que un operador lo retire.

<a id="data-loss-boundaries"></a>

## Límites de pérdida de datos

- **Escrituras confirmadas.** Un `memory.set` exitoso ha confirmado sus dos filas.
- **Filas de telemetría y auditoría.** Una caída pierde la cola de `agent_events` que aún no se ha vaciado (se vacía cada 100 ms) y la cola del flujo de eventos (cada `LOOM_EVENTS_STREAM_FLUSH_INTERVAL_S`, 1 s).
  El flujo de eventos solo transporta violaciones de presupuesto. Las filas de auditoría, exposición y trazas de búsqueda se insertan directamente; una inserción de auditoría fallida se registra en el log y hace fallar solo las dos operaciones que dependen de su registro, un `memory.restore` que deshace una retirada y
  `namespace.restore` ([límites de la auditoría](https://github.com/ClickHouse/loom/blob/main/docs/features/observability/audit.md#limits-and-failure-modes)).
  La recuperación de información no lee ninguna de ellas.
- **Las proyecciones posteriores a la respuesta** que se pierdan en una caída las reconstruyen los workers de reparación.
- **ClickHouse OSS de un solo nodo.** Sin replicación ni recuperación a un punto en el tiempo. ClickHouse
  Cloud, o un clúster OSS replicado, es el camino para producción. Haga copias de seguridad con `BACKUP` de ClickHouse o con copias de seguridad nativas; `dev/backup_loom.py` traslada un despliegue, una organización o un espacio de nombres, pero no es recuperación ante desastres. Consulte [copia de seguridad y recuperación](https://github.com/ClickHouse/loom/blob/main/docs/operations/backup-and-recovery.md).
- **Archivos fuera de ClickHouse.** Los originales de los documentos están en ClickHouse
  (`loom.document_blobs`) o, en el caso de los escritos con `LOOM_DOCUMENT_OBJECT_STORE=s3`,
  en un bucket compatible con S3, cuya copia de seguridad se hace con el versionado o la replicación del propio bucket; una regla de ciclo de vida limita durante cuánto tiempo esas copias conservan originales borrados. Los espejos de código residen en un volumen y, en un host que no los tenga, se vuelven a clonar desde el remoto del repositorio Git cuyo registro se guarda en ClickHouse. La reconfiguración de proveedores no conserva nada en el host
  ([reconfiguración de proveedores](https://github.com/ClickHouse/loom/blob/main/docs/architecture/provider-reconfiguration.md)).
- **Un traslado a otro host o a otro ClickHouse.** Lleve ClickHouse, la configuración de entorno,
  `LOOM_JWT_SECRET` y, mientras haya filas que nombren objetos en él, el bucket de documentos; todo lo demás se restablece
  ([traslado de un despliegue](https://github.com/ClickHouse/loom/blob/main/docs/operations/backup-and-recovery.md#moving-a-deployment)).

<a id="capacity"></a>

## Capacidad

Un proceso del servidor no conserva estado duradero, así que la capacidad web escala ejecutando más procesos.
ClickHouse realiza el trabajo de recuperación (escaneos de vectores, índices de texto, recorridos de grafos), por lo que la capacidad
más allá del nivel de Python es capacidad de ClickHouse. Los workers no escalan por replicación: un
supervisor los ejecuta. Los procedimientos están en [despliegue](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md) y
[ajuste de rendimiento](https://github.com/ClickHouse/loom/blob/main/docs/guides/performance-tuning.md).

| Nivel                      | Escala mediante                                            | Limitado por                                                              |
| -------------------------- | ---------------------------------------------------------- | ------------------------------------------------------------------------- |
| Web (HTTP, MCP, panel) | Más contenedores detrás de un balanceador de carga; `LOOM_WEB_WORKERS` | CPU de Python por proceso; el pool por proceso; límites de tasa por réplica |
| Workers en segundo plano   | Un supervisor por despliegue; un contenedor de workers más grande | Duración del paso frente al intervalo; cuota del proveedor para el trabajo de LLM y embeddings |
| Recuperación y almacenamiento | Cómputo, memoria y almacenamiento de ClickHouse         | Filas escaneadas por lectura de tenant; presión de merges durante la ingesta |
| Proveedores                | Cuota del proveedor y tamaños de lote                      | Límites de tasa del proveedor; `loom.embed.pending` muestra el backlog de embeddings |

<a id="web-tier"></a>

### Nivel web

| Parámetro                        | Efecto                                                                                                                                                                                                                                       |
| -------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Más réplicas web                 | N réplicas idénticas detrás de un balanceador de carga, todas con `LOOM_BACKGROUND_WORKERS=auto`.                                                                                                                                            |
| `LOOM_WEB_WORKERS`               | Procesos uvicorn por contenedor; si no se define, la imagen inicia uno por cada CPU que el contenedor pueda usar, como máximo uno por GiB de memoria y como máximo 8. Cada proceso ejecuta su Python en un núcleo; el embedder local da a cada uno una parte igual de las CPU. |
| `LOOM_REPLICA_COUNT`             | Mínimo del número de réplicas que divide cada bucket de límite de tasa (las filas `loom.instances` que están sirviendo, abajo).                                                                                                              |
| `LOOM_LATENCY_BUDGET_MULTIPLIER` | Escala el `budget_p99_ms` de cada op antes de que se dispare una violación de presupuesto (por defecto 1.0).                                                                                                                                 |

Una réplica nueva empieza a servir cuando `/v1/health/ready` responde 200: ClickHouse responde, no hay
ninguna migración pendiente, la instantánea de revocación y el keyring de firma están cargados y vigentes,
el registro de trackers y las sobrescrituras de runtime se han cargado una vez (el arranque espera hasta
`LOOM_BOOT_WARMUP_TIMEOUT_S` por ambos), la calibración de similitud está adoptada, su
configuración coincide con la del despliegue y el sandbox se ejecuta donde es obligatorio. Con
SIGTERM responde 503 ahí y sigue sirviendo durante `LOOM_SHUTDOWN_READY_DELAY_S` antes de que
se cierre su puerto. La lectura de las propias escrituras entre procesos la gestiona ClickHouse (inserciones con espera,
confirmadas o por lotes, relecturas secuenciales), no la afinidad de proceso.

<a id="clickhouse-client"></a>

### Cliente de ClickHouse

`server/core/ch.py` da a cada proceso un pool y un semáforo de admisión del mismo tamaño.

| Ajuste                                          | Por defecto | Efecto                                                                                                                 |
| ----------------------------------------------- | ------- | ---------------------------------------------------------------------------------------------------------------------- |
| `LOOM_CH_POOL_MAXSIZE`                          | 64      | Conexiones y sentencias concurrentes por proceso; una inserción por lotes retiene una durante su espera y su flush.    |
| `LOOM_CH_POOL_WAIT_MS`                          | 500     | Espera por un slot; pasado ese tiempo, 503 `OVERLOADED` con `Retry-After: 1`. 0 desactiva la compuerta.                |
| `LOOM_CH_TIMEOUT_READ_S`                        | 30      | Límite de tiempo de lectura.                                                                                           |
| `LOOM_CH_TIMEOUT_WRITE_S`                       | 60      | Límite de tiempo de `INSERT` / `UPDATE`.                                                                               |
| `LOOM_CH_TIMEOUT_DDL_S`                         | 300     | Límite de tiempo de DDL, `OPTIMIZE`, `SYSTEM`.                                                                         |
| `LOOM_CH_BATCHED_INSERTS`                       | true    | Agrupa en lotes las inserciones escritas en cada solicitud, cada una reteniendo una conexión durante su espera; false las confirma, una parte cada una. |
| `LOOM_CH_BATCHED_INSERT_WAIT_MIN_MS`            | 10      | Espera más corta de una inserción asíncrona por lotes o con espera, lo que espera una inserción aislada; la conexión se retiene durante la espera. |
| `LOOM_CH_BATCHED_INSERT_WAIT_MAX_MS`            | 50      | Espera más larga de una inserción asíncrona por lotes o con espera que una solicitud aguarda; la conexión se retiene durante ella. |
| `LOOM_CH_BATCHED_INSERT_BACKGROUND_WAIT_MAX_MS` | 200     | Lo mismo para los ticks y el drenado del flusher de eventos de uso y para el flush de marcas de escritura.             |

**Por qué una espera acotada:** cuando ClickHouse se ralentiza, una cola sin límite convierte cada solicitud en
un timeout del cliente después de que el trabajo se puso en cola de todos modos. Una espera corta falla rápido con un 503 reintentable. Los gauges `loom.ch.pool.in_use`, `waiters`, `overloaded_total` y `timeouts_total`
muestran qué lado está saturado.

<a id="traffic-classes"></a>

### Clases de tráfico

Cada llamada tiene una clase: `production`, `interactive` (por defecto), `bench` o `background`
(la fija el supervisor, nunca una solicitud). Proviene del claim del token; si no, del registro de
espacios de nombres; si no, de la sugerencia de la solicitud. Cambia la planificación, nunca los resultados:

- Ajustes de lectura de ClickHouse por clase (`LOOM_CH_SETTINGS_BY_TRAFFIC_CLASS`): `production`
  prioridad 1, `interactive` 2, `background` 5 con 3 hilos, `bench` 10 con 4 hilos.
- Buckets de límite de tasa (`LOOM_RATELIMIT_CLASS_FRACTIONS`; `bench` recibe la mitad).
- Política de LLM por clase (`LOOM_LLM_POLICY_BY_TRAFFIC_CLASS`).

Un espacio de nombres nuevo `bench-…` se clasifica como `bench`, de modo que una prueba de carga no deja sin recursos a producción.

<a id="rate-limits"></a>

### Límites de tasa

`server/core/ratelimit.py` mantiene token buckets por (espacio de nombres, clase de op) y por (org, clase de
op); el nivel de org es tres veces el nivel del espacio de nombres. Un nivel opcional por actor toma una
fracción del bucket del espacio de nombres (`LOOM_RATELIMIT_ACTOR_FRACTION`, 1.0 = desactivado). Una llamada debe
pasar todos los buckets, y un rechazo no descuenta de ninguno.

| Clase de op        | Cubre                                                                                                         | Capacidad | Recarga / s |
| ------------------ | ------------------------------------------------------------------------------------------------------------- | -------- | ---------- |
| `memory`           | `memory.*`, `conversation.*` (excepto las lecturas por clave exacta y `memory.import`)                        | 100      | 17         |
| `memory.read`      | Lecturas por clave exacta: `memory.get`, `bulk_get`, `history`, `working_memory.get`, …                       | 300      | 50         |
| `memory.import`    | `memory.import`                                                                                               | 10       | 2          |
| `knowledge.search` | `knowledge.search`                                                                                            | 20       | 3          |
| `knowledge.query`  | `knowledge.query`                                                                                             | 20       | 3          |
| `knowledge.read`   | Las demás ops `knowledge.*` `read_only`, salvo `knowledge.inspect`: `knowledge.get`, `knowledge.list`, …      | 50       | 10         |
| `knowledge.add`    | Todas las demás ops `knowledge.*`, incluidas `knowledge.inspect`, `knowledge.synthesize` y `knowledge.dedup_list` | 10       | 2          |
| `code`             | `code.*` (excepto `code.outcome`, que toma de `memory`)                                                       | 60       | 10         |
| `insights.query`   | `insights.query`                                                                                              | 20       | 3          |
| `assistant.ask`    | `assistant.ask` y la ruta de chat del asistente                                                               | 10       | 1          |
| `default`          | Todo lo demás                                                                                                 | 50       | 10         |

Estos son los valores por defecto del nivel de espacio de nombres (`DEFAULT_LIMITS`, `op_class_for`):

- **Por réplica.** Los buckets viven en el proceso o el host. Cada réplica mantiene el límite
  dividido entre los hosts con una fila `loom.instances` viva que está sirviendo, de modo que la suma de las réplicas
  aproxima el límite. `LOOM_REPLICA_COUNT` es el mínimo de ese número, y el número
  mientras las filas no se pueden leer.
- **Dentro de un host.** Con `LOOM_WEB_WORKERS > 1`, los procesos comparten una tabla de buckets en un
  archivo mapeado en memoria (`LOOM_RATELIMIT_SHARED=auto`, `LOOM_RATELIMIT_SHARED_PATH`,
  `LOOM_RATELIMIT_SHARED_SLOTS`). `/v1/health` informa `ratelimit.backend`.
- **Fail open.** Un error del limitador admite la llamada y se contabiliza.

**Por qué buckets locales:** un viaje de ida y vuelta a ClickHouse por cada admisión pondría la base de datos en
la ruta de cada solicitud para proteger la propia base de datos.

<a id="background-workers"></a>

### Workers en segundo plano

Un supervisor se ejecuta por despliegue. En modo `auto`, cada proceso del despliegue
(`LOOM_DEPLOYMENT=1`) compite por un lease en ClickHouse: clave `workers_supervisor` en
la tabla de locks de Keeper `loom._migration_lock` (KeeperMap estricto). Fuera del despliegue
(un servidor iso o de rama sobre el mismo ClickHouse) `auto` no ejecuta workers y el log de arranque
lo indica; `on` los ejecuta ahí, pero solo el titular del despliegue hace el trabajo del líder
(ALTER de retención, filas de espacios de nombres de trackers, la configuración publicada, el barrido de
participantes de proveedores).
La expiración se juzga con el reloj del servidor de ClickHouse. Con T = `LOOM_SUPERVISOR_LEASE_TTL_S`
(por defecto 30 s, como mínimo 24):

| Paso      | Cuándo                                                                       | Por qué                                                                                     |
| --------- | ---------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------- |
| Renovar   | cada T/6; una renovación fallida se reintenta cada T/12                      | Caben varios fallos antes de que el titular se rinda                                        |
| Perder    | T/2 después de enviada la última renovación que se registró                  | Se mide desde el envío, así que la marca del servidor nunca es anterior                     |
| Detener   | en ese plazo, incluso si una llamada de renovación sigue bloqueada           | Una llamada colgada no puede mantener los workers en ejecución; cada llamada se envía una vez, con timeout max(1, T/24) s |
| Drenar    | hasta T/6, de modo que los workers estén detenidos a los 2T/3                | Un proceso hijo de workers que siga en ejecución entonces se mata                          |
| Expiración | T después de que el servidor selló la última renovación; luego otro proceso toma el relevo | T/3 de margen tras el drenado                                                |

Un titular que perdió sus renovaciones no inicia ningún paso nuevo hasta que renueve (un proceso en pausa
que se reanuda no ejecuta uno junto al nuevo titular), y uno cuya clave de Keeper aún lo nombra
recupera el lease de inmediato en lugar de esperar al TTL. Un titular que encuentra el lease
tomado detiene sus workers; cuando el titular muere, otro proceso toma el relevo tras T. Un
proceso que se convierte en titular después del arranque también hace el trabajo de arranque del líder (filas de espacios de nombres
de trackers, ajustes publicados, `LOOM_RETENTION_*_DAYS`).

Detener los workers cancela cada paso en su siguiente `await`. El trabajo que la cancelación no puede alcanzar
(un hilo como el cálculo de PageRank o de comunidades, un paso que se reanuda tras una
pausa antes de que se ejecute su bucle de lease) se detiene en el siguiente `pass_context.check_stop()` del paso,
que los bucles largos invocan entre lotes: los tramos de ingesta, las rondas delta de SCIP,
`code_pr_history`, `code_graph_stats` y `entity_communities`. Lanza una excepción una vez que el paso
fue detenido o el lease ya no está vigente, de modo que el paso no envía más escrituras. No
hay fencing tokens: una sentencia ya enviada igualmente se aplica.

Un proceso se aparta (el titular detiene sus workers y cede la clave; un aspirante
no la solicita) mientras:

- se drena tras SIGTERM, de modo que otro proceso toma el relevo durante su retardo de ready;
- su configuración difiere de la publicada por el despliegue (no está ready, y
  podría escribir vectores en el espacio equivocado o firmar con la raíz equivocada);
- una fila viva de `loom.instances` de un build más nuevo compite por el mismo lease.

Lo último es el traspaso de actualización progresiva. Cada fila lleva la versión de esquema de su build
(su último número de migración) y el lease por el que compite ahora mismo: ninguno mientras ese
proceso se drena, se ha desviado o cedió el lease, así que una fila así nunca hace ceder a un build
más antiguo. El traspaso solo se dispara cuando las versiones de esquema difieren, por lo que un release solo de código
nunca hace traspaso: el titular antiguo conserva los workers hasta que se detiene. Cuando ya no hay
ningún build más nuevo vivo (un rollback), el más antiguo vuelve a competir. Un proceso cuyo hijo de workers
sigue sin poder iniciarse cede el lease durante cinco minutos (abajo).

Por lo tanto:

- Varios hosts pueden ejecutarse en `auto`; uno de ellos ejecuta los workers. `on` omite la elección.
- Una segunda copia de los workers de mantenimiento desperdicia trabajo de ClickHouse; una segunda copia de los workers
  de LLM duplica sus escaneos y su gasto.
- El titular ejecuta los workers en un proceso hijo (`LOOM_WORKERS_PROCESS=child`), de modo que un
  paso largo no comparte el bucle de eventos que atiende sus solicitudes (los pasos en el bucle
  de servicio casi duplicaron el p90 de las solicitudes: `memory.get` 148 ms frente a 74–79 ms durante
  un paso de `code_ingest`). El hijo ejecuta el
  supervisor y lo que usan sus workers (el cliente de ClickHouse, la configuración de proveedores,
  el emisor de eventos, los registros de trackers y de repositorios), no el lifespan de servicio.
  Lee de su padre el plazo del lease cada segundo; el plazo deja de avanzar
  cuando el lease no está vigente. El hijo desaparece en ese plazo más T/6: sale
  por sí mismo entonces, su padre lo mata entonces, y el kernel lo mata cuando muere el padre
  (`PR_SET_PDEATHSIG`). El bucle de eventos del hijo envía un latido a su padre cada segundo, y
  `/v1/health` muestra un hijo sin latido durante 15 s como no vivo. Un proceso que se drena
  (SIGTERM) detiene sus workers, en línea o en un hijo, y cede el lease mientras
  sigue sirviendo. Un hijo que sale en menos de 60 s tras iniciarse cinco veces seguidas hace que su
  proceso ceda el lease e informe `workers_lease_given_up`; otro proceso toma
  los workers, y este lo solicita de nuevo cinco minutos después.

El trabajo de los workers no se particiona entre procesos. Un worker se mantiene al día mediante pasos acotados:
un presupuesto de lote o de tiempo por paso (por ejemplo `embedding_worker` calcula embeddings de hasta 100 stubs;
`usage_fold` pliega hasta 1000 memorias en 120 s y se retira con 20 mutaciones pendientes)
y el plazo anterior. Un paso que rutinariamente alcanza su plazo significa que el contenedor de workers
necesita más CPU o el proveedor más cuota.

<a id="batch-sizes-and-queues"></a>

### Tamaños de lote y colas

| Ruta                              | Límite                                                                                                     |
| --------------------------------- | ---------------------------------------------------------------------------------------------------------- |
| Cuerpo de la solicitud            | `LOOM_MAX_REQUEST_BYTES` (32 MiB); 413 por encima                                                          |
| `memory.bulk_set`                 | Hasta 1000 entradas en un `embed_many`; comprobaciones de conflicto hasta `LOOM_BULK_CONFLICT_CHECK_MAX_ENTRIES` (200) |
| `memory.bulk_get`                 | Hasta 1000 claves                                                                                          |
| `memory.set_from_messages`        | Hasta 400 mensajes                                                                                         |
| Solicitudes de embeddings         | Hasta 100 entradas por solicitud al proveedor (OpenAI, Gemini)                                             |
| Emisor de `agent_events`          | Cola de 10,000 filas, vaciada cada 0.1 s o cada 500 filas                                                  |
| Stream de eventos (violaciones de presupuesto) | Cola de 20,000, vaciada cada `LOOM_EVENTS_STREAM_FLUSH_INTERVAL_S` o cada `_FLUSH_BATCH` filas |
| Filas de estado de workers        | Vaciadas cada 15 s o cada 200 filas                                                                        |

<a id="vector-cost"></a>

### Costo de los vectores

Una memoria almacena hasta dos vectores (valor y clave) de `Array(BFloat16)`: 6 KiB con 1536
dimensiones, 12 KiB con 3072. El almacenamiento y los bytes escaneados crecen linealmente con `LOOM_EMBEDDING_DIM`.
No hay índice HNSW, así que una lectura escanea todos los vectores del espacio de nombres del tenant y la latencia
crece con el tamaño del espacio de nombres. Los códigos cuantizados reducen el escaneo donde el códec puede activarse
([ajustes restringidos](https://github.com/ClickHouse/loom/blob/main/docs/architecture/clickhouse.md#gated-clickhouse-settings)).
Los detalles están en [ClickHouse](https://github.com/ClickHouse/loom/blob/main/docs/architecture/clickhouse.md#vector-storage).

<a id="measuring"></a>

### Medición

Esta página no indica cifras de latencia ni de rendimiento; dependen del despliegue de ClickHouse,
la distancia de red y el tamaño del espacio de nombres. Mida las suyas, contra un servidor
aislado:

| Comando                                                     | Mide                                                                                                   | Salida              |
| ----------------------------------------------------------- | ------------------------------------------------------------------------------------------------------ | ------------------- |
| `make bench OP=memory.set C=10 D=30`                        | p50 / p95 / p99 y RPS de una op con concurrencia C durante D segundos (`scripts/ops/benchmark.py`)     | stdout, o `--json`  |
| `python bench/search_latency.py --namespaces … --queries …` | p50 / p95 de `memory.search` por banda de tamaño de espacio de nombres, con tiempos por etapa de `retrieval_work.stage_ms` | CSV de `--out` |
| `make bench-quick`                                          | Calidad de recuperación en una muestra de 30 preguntas (requiere proveedores reales)                   | CSV bajo `/tmp`     |

`scripts/ops/benchmark.py` escribe en un espacio de nombres `bench-…` nuevo, así que se ejecuta en la
clase `bench`; una ejecución de `memory.search` necesita un espacio de nombres que una ejecución de `memory.set` haya llenado. Consulte
[ejecución de benchmarks](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/running.md).

<a id="what-to-scale"></a>

### Qué escalar

| Señal                                                           | Escalar                                                   |
| --------------------------------------------------------------- | --------------------------------------------------------- |
| CPU web alta, p99 de solicitudes en aumento, ClickHouse respondiendo rápido | Réplicas web o `LOOM_WEB_WORKERS`             |
| `loom.ch.pool.waiters` y `OVERLOADED` en aumento, ClickHouse rápido | `LOOM_CH_POOL_MAXSIZE`, o más réplicas                |
| `BACKEND_TIMEOUT` y entradas lentas en `system.query_log`       | Cómputo de ClickHouse, o espacios de nombres más pequeños por lectura |
| `loom.embed.pending` en aumento                                 | Rendimiento del embedder (cuota del proveedor)            |
| `loom.worker.consecutive_failures` o duraciones de paso en aumento | CPU y memoria del contenedor de workers                |
| Presión de merges, errores de memoria durante la ingesta        | Cómputo y memoria de ClickHouse, o menor concurrencia de escritura |

Las métricas se describen en [monitoreo](https://github.com/ClickHouse/loom/blob/main/docs/operations/monitoring.md).

<a id="known-limits"></a>

### Límites conocidos

- **Un líder de workers por despliegue.** Los pasos de los workers no se reparten entre hosts.
- **Los límites de tasa por réplica** son aproximados: el llenado de los buckets es por host. Los límites de intentos de inicio de sesión y
  de registro se cuentan entre todos los procesos en `loom.auth_attempts`.
- **Comprobar y luego insertar entre réplicas** (el nombre y el correo de un registro, un espacio de nombres
  personal, un código OAuth o refresh token, una clave de idempotencia en curso) se ejecuta bajo un
  claim a nivel de despliegue, una clave KeeperMap estricta en `loom._migration_lock`, con la
  comprobación leída de forma secuencial. Un segundo
  aspirante recibe un 409; un almacén de claims que no puede responder da un 503 (una clave de idempotencia
  se ejecuta entonces como un simple miss).
- **La telemetría del stream de eventos** (violaciones de presupuesto) se drena con `SIGTERM`; un `SIGKILL` o
  OOM pierde como máximo un intervalo de flush de ella, nunca filas de auditoría ni de memoria.
- **El tamaño del espacio de nombres determina la latencia de lectura**, porque las lecturas de vectores escanean las filas
  del espacio de nombres sin índice vectorial.
- **ClickHouse OSS de un solo nodo** no tiene replicación ni failover.

<a id="knowledge-restore"></a>

## Restauración de conocimiento

`knowledge.restore` ([restaurar un documento eliminado](https://github.com/ClickHouse/loom/blob/main/docs/features/knowledge.md#restoring-a-deleted-document))
nunca debe recuperar lo que alcanzó un borrado. Un borrado deja sus propias marcas: marca
cada fragmento de su ámbito, también los de un documento eliminado, y reemplaza in situ una fila viva.
Restore también lee el registro de borrados en busca de un borrado que pueda alcanzar al documento y que
no hubiera terminado cuando se escribió el documento: del espacio de nombres del documento, de una lista de espacios de nombres que lo nombre,
o del usuario que escribió la revisión restaurada (de cualquier usuario, para los restos vivos de
una restauración interrumpida, que ninguna firma vincula a un autor).

Lee el registro antes de escribir la fila y otra vez después: cuando la segunda lectura encuentra
un borrado, deshace lo que escribió, y hace lo mismo cuando algo falla una vez que
empieza a escribir. Ante una purga, elimina su propia fila, de modo que no queda ninguna copia del contenido.
Cuando una fusión ha incorporado en ella la fila del delete, eso deja a la fuente sin ninguna fila: una
purga que cubra el documento tampoco dejaría ninguna, y una que solo pueda cubrirlo
(una lista ilegible, o la purga de cualquier usuario para los restos de una restauración interrumpida) cuesta
la fila retirada del documento, que es la opción de fallo seguro.
En los demás casos, y cuando no se puede leer el registro, vuelve a escribir la fila como retirada, conservando cualquier
marca que un borrado le haya puesto entretanto: eliminarla podría dejar a la fuente sin ninguna fila, ya que una
fusión puede incorporar la fila del delete en cualquier momento. Cuando no queda ninguna fila de la fuente
(una purga las eliminó todas), no vuelve a escribir nada.

Con la autoridad Postgres, restore escribe a través de las cabeceras de documento ([autoridad de
almacenamiento](https://github.com/ClickHouse/loom/blob/main/docs/architecture/storage-authority.md#knowledge-documents)). Se niega, con posibilidad de reintento, mientras una
publicación de la fuente aún pueda llegar a ClickHouse (una pendiente, o una que el worker de entrega
asumió y resolvió dentro del límite de inserción de su lease), ya que de otro modo la retirada de los fragmentos
por un delete podría llegar después de que restore los recuperara. Su escritura de vuelta
se confirma sobre la versión que leyó, así que una escritura de texto que tomó la fuente entretanto
la rechaza (409). Una que se confirma y no puede publicar se deshace y responde 503. El deshacer de una purga
también retira la cabecera restaurada y su entrega, y espera a que venza el lease de esa entrega
antes de eliminar la fila de ClickHouse.

Una restauración cuyo llamador desaparece (el cliente se desconecta, se cancela la solicitud)
antes de responder se ejecuta igualmente hasta el final bajo el claim y se deshace a sí misma, ya que ninguna
fila de auditoría la registraría; su deshacer termina antes de liberar el claim. Un llamador
que desaparece después de que restore respondió, mientras el transporte escribe su fila de auditoría, puede dejar
la restauración vigente sin fila de auditoría, como cualquier escritura auditada.

Lo que no puede deshacer:

- Una restauración interrumpida antes de su deshacer deja los fragmentos buscables bajo una fila retirada;
  volver a llamarla la termina. Un delete que se ejecute antes retira esos fragmentos marcados con el
  delete del propio documento, así que restore todavía los encuentra.
- Un borrado que se registra y se completa entre sus dos lecturas del registro, seguido de un fallo
  del deshacer (o de la segunda lectura y el deshacer, o de la finalización del proceso entre
  ellos): la fila queda viva, y una llamada posterior responde `live`.
- Una purga para la que el deshacer no puede leer el registro deja la fila retirada: el censo de residuos
  de la purga, que cuenta todas las filas, solo lo informa si se ejecuta después del deshacer.
- El barrido de originales elimina un original que ninguna fila viva nombra una vez que es lo bastante antiguo, y
  comprueba su antigüedad solo en el momento de eliminar. Una pasada del barrido que leyó el espacio de nombres mientras
  el documento estaba eliminado puede borrar el original después de que restore lo encontró, así que la fila restaurada
  nombra un original que ya no existe (y responde `kept`); entonces la descarga falla. Cualquier
  escritura que vuelva a nombrar un original existente comparte esta ventana.

Escrituras concurrentes: una escritura de texto (`knowledge.add`, `knowledge.upsert`) y el delete de un
documento vivo no toman ningún claim. Con la autoridad ClickHouse, una escritura de texto que llega a
la misma fuente durante una restauración puede dejar buscables los fragmentos de ambas versiones, como puede ocurrir
durante un delete, hasta la siguiente escritura de esa fuente; una que empezó antes de la
restauración guarda su fila con una versión menor, así que la fila restaurada la oculta, igual que la
fila de un delete oculta una que empezó antes del delete: vuelva a escribirla. Con la
autoridad Postgres, la que se confirme en segundo lugar responde 409 y retira sus propios
fragmentos. Cuando una restauración se deshace después de que la fila de una escritura posterior prevaleció, deja
esa fila tal como está y retira solo sus propios fragmentos. Un delete de una fuente ya eliminada
retira sus fragmentos restantes bajo el claim de la fuente, leyendo de nuevo el documento
bajo él con consistencia secuencial: uno que una restauración recuperó entretanto está entonces vivo
y conserva sus fragmentos. Retira y cuenta solo los fragmentos hasta la lápida; los fragmentos de una
escritura más reciente, aún por confirmar, permanecen, y un delete que encuentra solo esos responde que
no se eliminó nada.

<a id="knowledge-move"></a>

## Movimiento de conocimiento

`knowledge.move` ([mover documentos](https://github.com/ClickHouse/loom/blob/main/docs/features/knowledge.md#moving-documents)) escribe cada
documento en el espacio de nombres al que va y luego lo elimina de donde estaba, de modo que un movimiento interrumpido
deja el documento en ambos lugares y nunca en ninguno. Por documento, lee el lugar al que
va y responde `exists` si hay allí otro documento vivo antes de escribir nada; bajo
los claims vuelve a leer ambos. Después guarda el original y el artefacto de extracción en
el nuevo espacio de nombres, fuera de cualquier claim (un original que ninguna fila nombra todavía es del barrido, como cualquier
carga cuya fila no llegó a escribirse). Un original guardado como fila de ClickHouse se copia dentro de
ClickHouse; uno que está en el bucket, o que se movió allí entretanto, se lee y se vuelve a escribir bajo
el lugar del nuevo espacio de nombres en el bucket. Un original que una eliminación definitiva se llevó (`blob_erased_at` en el
documento) no se copia, incluso cuando los bytes de su sha se siguen conservando para otra carga.

Luego, bajo el claim del origen en ambos espacios de nombres, tomados en orden y sin esperar
(un origen retenido responde `busy` de inmediato), rechaza (`busy`) cuando alguno de los dos lugares cambió desde
que los leyó, cuando una publicación de cualquiera de los dos lugares aún puede llegar, o mientras un trabajo de ingesta
de cualquiera de ellos esté abierto (su publicación volvería a escribir el documento donde estaba, o sobre la copia).
Copia los fragmentos de la revisión del documento, con sus vectores, dentro de ClickHouse
(`INSERT … SELECT`, cada versión que sirve la fila: una edición de metadatos deja los fragmentos en
su propia versión), luego las tablas, luego la fila, y elimina el origen sobre la versión que
leyó. Una copia dejada por un movimiento anterior que se detuvo antes de su eliminación (el mismo texto, título,
descripción, etiquetas, metadatos, autor y hora de ingesta en el destino) no se vuelve a escribir cuando
sirve tantos fragmentos vivos como el origen y las mismas tablas: solo se elimina el origen.
Una con menos fragmentos, o sin tablas donde el origen las tiene (una inserción de fila cancelada
puede llegar después de su reversión, lo que deja la fila intacta), se sobrescribe, igual que un documento eliminado,
antes de eliminar el origen. Una con más está completa: una eliminación interrumpida entre
retirar los fragmentos del origen y escribir su lápida (los retira primero) deja la
fila del origen viva sin fragmentos, y solo queda la eliminación; nunca se escribe una copia sobre una
que sirve más fragmentos que el origen: una que además carece de sus tablas responde `exists`.
Las tablas que difieren por otras causas cambiaron en uno de los lados desde entonces (una aprobación, una
aclaración), lo cual no se distingue de un cambio en el otro: esa copia, como una cuyo
texto o metadatos se editaron, es otro documento y responde `exists`.

Todo lo que no se escribe completo, o una escritura que tomó alguno de los dos lugares antes de la eliminación,
revierte lo que el movimiento escribió en el destino, y ese origen responde `busy`. La reversión
se ejecuta hasta el final antes de liberar los claims, por muchas veces que se cancele la solicitud
entretanto (la cancelación se propaga una vez terminada). Su primera sentencia retira los fragmentos de la copia
bajo todos los modelos de embedding a la vez; una reversión empieza por el corte de sus claims, o
justo después de una eliminación iniciada antes que la autoridad Postgres rechaza, cuyo commit
responde dentro de `pg_commit_lock_timeout_ms` y `pg_statement_timeout_ms` (2 s y 5 s por
defecto). Con el límite propio de la sentencia (15 % del TTL, 9 s), esa retirada termina
hacia los 40 s, dentro del TTL de 60 s del claim. El resto puede excederlo, pero un movimiento posterior que
tome los claims lee la copia incompleta: bajo los claims comprueba de nuevo que la copia de un movimiento
anterior esté completa, responde `busy` cuando no lo está, y en una llamada posterior vuelve a escribir la copia;
los demás orígenes de la llamada sí se mueven. Tras la retirada, la reversión
descarta su commit (bajo la autoridad Postgres), elimina su fila, luego sus fragmentos y sus tablas:
una reversión que se detiene a medias no deja ningún fragmento sirviéndose sin fila, y un
`INSERT … SELECT` cancelado que llega después de la eliminación de los fragmentos también se retira. El trabajo bajo los
claims se corta al 40 % del TTL de 60 s del claim (24 s); un documento cuya copia se prolonga
más allá se revierte y responde `busy`. La eliminación se ejecuta hasta el final sin importar cómo terminen la solicitud o
los claims entretanto, y el documento se cuenta como movido en el momento en que su lápida está escrita,
de modo que un corte o una cancelación mientras la lápida está en vuelo sigue respondiendo movido y lo audita,
y una lápida que la autoridad Postgres rechaza entonces aún revierte la copia. Un fallo de
la propia eliminación (una respuesta perdida, que hace fallar la llamada) puede seguir a la lápida, así que la
copia permanece: el documento está en ambos lugares, o solo donde va, y un reintento encuentra la
copia y solo elimina el origen, o responde `not_found`. Una vez escrita la lápida, el movimiento
está hecho: las tablas del origen se eliminan después de liberar los claims, ya que la eliminación de un
documento vivo no toma ninguno, así que ningún corte la alcanza; un fallo ahí se registra en el log, y una
eliminación posterior del origen las resuelve. Un documento cuenta como movido en el momento en que su lápida
está escrita, bajo los claims: su fila de auditoría y la limpieza de sus tablas siguen, sea lo que sea lo que termine la llamada
después (una cancelación al liberar los claims, o durante la limpieza). Un fallo que
no es de un origen concreto (un almacén que no responde) se lanza una vez que cada origen de la llamada
ha respondido, después de la fila de auditoría de los que se movieron.

El registro de eliminaciones se lee por cada documento dos veces, como lo lee `knowledge.restore`
(`unfinished_reaching` para el espacio de nombres y el escritor del documento, filtrado con `may_cover`):
antes de guardar su original, por una eliminación que pueda alcanzarlo donde está (sin terminar cuando
su texto se escribió allí: el `created_at` de su primer fragmento, que un movimiento marca con el momento en que
copió los fragmentos, mientras que la fila conserva su hora de ingesta) o el lugar al que va (sin terminar
cuando el movimiento empezó), y de nuevo bajo los claims, justo antes de su eliminación, por una
registrada desde entonces: una puede haber pasado la copia, la de este movimiento o la de uno anterior. Esa
revierte una copia que escribió este movimiento, deja la copia de un movimiento anterior a la eliminación definitiva,
y responde `erased` sin eliminar el origen, de modo que un documento `erased` siempre se queda donde estaba.
Bajo la autoridad Postgres, una eliminación sobre un documento que una eliminación definitiva retiró entretanto responde también
`erased`. La primera lectura ocurre justo antes de guardar el original en el nuevo lugar, así que
una eliminación definitiva cuyo paso de originales pudiera pasarlo por alto debe registrarse en el instante entre esa lectura
y el guardado; la nueva lectura antes de la eliminación responde entonces `erased`, y ese original, nombrado
por ninguna fila, es del barrido tras `document_blob_min_age_seconds`, como cualquier original cuya fila
no llegó a escribirse. El movimiento no elimina ningún original por sí mismo: uno se identifica por sus bytes, y una
escritura concurrente de los mismos bytes cuya fila aún no se ha escrito puede necesitarlo.

Escrituras concurrentes: una escritura de texto no toma ningún claim. Bajo la autoridad Postgres, la fila de la copia y la
eliminación se confirman cada una sobre la versión leída, así que una escritura que tomó el origen o
el lugar primero las rechaza (409), y la copia se revierte. Bajo la autoridad ClickHouse
el movimiento lee la cabecera del destino bajo los claims antes de copiar y otra vez después de su fila:
una escritura con una versión posterior gana y la copia se revierte, mientras que una que llegó entre
esa primera lectura y la fila (toda la copia de fragmentos y tablas), con una versión anterior, se
reemplaza, como con dos escrituras de texto de un mismo origen.

<a id="invariants"></a>

## Invariantes

- Una escritura exitosa ha confirmado sus filas primarias; si cualquiera de las dos falla, la solicitud falla.
- Un INSERT reintentado lleva su token de deduplicación, o no se reintenta.
- `OVERLOADED` y `AUTH_UNAVAILABLE` incluyen una indicación de reintento; `SCHEMA_PENDING` espera a un operador.
- Una pasada de un worker es idempotente: ejecutarla dos veces converge.
- Las comprobaciones de acceso y el borrado fallan de forma cerrada; los controles de disponibilidad y la telemetría fallan de forma abierta, y se contabilizan.
- Ninguna solicitud espera indefinidamente al pool, a un proveedor o a una cola. Cada sentencia espera como máximo `LOOM_CH_POOL_WAIT_MS` por un slot y se ejecuta como máximo durante el plazo límite de su clase.
- Una clase de tráfico cambia la planificación y los buckets, nunca las filas devueltas.
- Toda migración es aditiva, de modo que la compilación anterior funciona con el esquema actual.

<a id="source-entry-points"></a>

## Puntos de entrada del código fuente

| Qué                                      | Dónde                                                                                                                                                                                                                                       |
| ---------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Reintentos y compuertas del cliente      | `server/core/ch.py` (`_with_retry`, `_acquire_slot`, `_bounded`, `statement_timeout`), `server/core/ch_schema_guard.py`                                                                                                                     |
| Códigos de error                         | `server/core/errors.py`                                                                                                                                                                                                                     |
| Idempotencia                             | `server/core/idempotency.py`                                                                                                                                                                                                                |
| Supervisor y lease                       | `server/workers/supervisor.py`, `server/workers/leader.py`, `server/workers/registry.py` (`default_max_pass_seconds`)                                                                                                                       |
| Workers de reparación                    | `server/memory/workers/projection_repair.py`, `orphan_vector_reconcile.py`, `embedding.py`, `entity_extractor.py`; `server/erasure/workers/recovery.py`; `server/tasks/workers/lease_sweep.py`; `server/insights/workers/audit_verifier.py` |
| Snapshot de revocación                   | `server/auth/revocation.py` (`load_at_boot`)                                                                                                                                                                                                |
| Límite de embedding de consultas         | `server/memory/app/search/embed.py`                                                                                                                                                                                                         |
| Proyecciones posteriores a la escritura  | `server/memory/app/after_write.py`, `server/memory/app/projection_failures.py`                                                                                                                                                              |
| Colas de telemetría                      | `server/observability/events.py`, `server/observability/events_stream.py`, `server/workers/status_store.py`                                                                                                                                 |
| Clases de tráfico                        | `server/observability/traffic_class.py`, `server/identity/app/traffic_class_registry.py`                                                                                                                                                    |
| Limitador de tasa                        | `server/core/ratelimit.py` (`DEFAULT_LIMITS`, `op_class_for`), `server/core/ratelimit_shared.py`, `server/auth/attempts.py`                                                                                                                 |
| Estado del esquema                       | `server/core/schema/status.py`                                                                                                                                                                                                              |
| Despliegue azul/verde                    | `redeploy.sh`, `dev/docker-compose.colour.yml`                                                                                                                                                                                              |
| Prueba de carga                          | `scripts/ops/benchmark.py`, `bench/search_latency.py`                                                                                                                                                                                       |

<a id="related"></a>

## Relacionado

- [Solución de problemas](https://github.com/ClickHouse/loom/blob/main/docs/operations/troubleshooting.md): guías de resolución organizadas a partir del síntoma.
- [Monitorización](https://github.com/ClickHouse/loom/blob/main/docs/operations/monitoring.md): las métricas mencionadas aquí.
- [Seguridad](https://github.com/ClickHouse/loom/blob/main/docs/architecture/security.md): las comprobaciones de acceso que fallan en modo cerrado.
- [ClickHouse](https://github.com/ClickHouse/loom/blob/main/docs/architecture/clickhouse.md): tablas, columnas vectoriales y atribución de consultas.
