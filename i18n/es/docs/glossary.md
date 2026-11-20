<!-- i18n: source=docs/glossary.md blob=8dd4bdacf4b7cf0e994f5fcf1af3353f19f26540 model=claude-sonnet-5-5 -->
> Esta página es una traducción automática de la [página en inglés](https://github.com/ClickHouse/loom/blob/main/docs/glossary.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/glossary.md), which is authoritative.

<a id="glossary"></a>

# Glosario

Los términos de Loom, en orden alfabético, cada uno definido una sola vez. Los identificadores de código van entre acentos graves.

**Abstain** — `memory.search` devuelve `abstain=true` con un código de motivo en lugar de la coincidencia débil más cercana, cuando el mejor resultado queda por debajo del umbral de abstención calibrado para el embedder.
Distingue entre "no se almacenó nada" y "algo no relacionado obtuvo 0.3".
Consulta [recuperación y ranking](https://github.com/ClickHouse/loom/blob/main/docs/concepts/retrieval-and-ranking.md).

**Actor** — la columna estampada por el servidor que indica quién escribió una fila: `human:<subject>` o `process:<subject>`, con el tipo tomado del token. Nunca se lee del cuerpo de la solicitud.
Consulta [identidades y espacios de nombres](https://github.com/ClickHouse/loom/blob/main/docs/concepts/identities-and-namespaces.md).

**Agente** — el tercer elemento de la tupla de identidad: el nombre del programa que escribe y lee (`claude-code`, `support-bot`). Registra la autoría; no es un límite de lectura: todos los miembros de un espacio de nombres leen las filas de todos los agentes.

**Attestation** — las columnas `verified_*` que registran que una fila fue verificada. Una edición las deja en su lugar y las marca como obsoletas; el lector decide la confianza comparando el hash de contenido atestiguado. Consulta [historial y procedencia](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/history-and-provenance.md).

**Registro de auditoría** — `loom.audit_log`: una fila con hash de contenido por cada operación auditada, agrupadas en puntos de control horarios encadenados por hash. Las filas contienen hashes, no contenido.
Consulta [auditoría](https://github.com/ClickHouse/loom/blob/main/docs/features/observability/audit.md).

**Revisión de creencias** — la perspectiva de diseño del ciclo de vida de la memoria: Expand (`memory.set`), Revise (`memory.update_by_id`), Contract (`memory.delete`), Reinforce (`memory.pin`), Refuse (`blacklist.add`). Consulta [cómo se juzga una escritura](https://github.com/ClickHouse/loom/blob/main/docs/concepts/memory.md#how-a-write-is-judged).

**Blacklist** — subcadenas o expresiones regulares registradas que rechazan las escrituras coincidentes (`422 BLACKLISTED`); `sweep=true` también marca con tombstone las coincidencias existentes.

**Fragmento (chunk)** — una porción de un documento de conocimiento o de un archivo fuente, almacenada y con embedding por separado para que la recuperación pueda devolver el pasaje en lugar del documento completo.

**Enlace de código** — el vínculo de una memoria con una definición o un archivo que nombra, seguido a través de renombrados y ediciones. Su estado es `fresh`, `moved`, `changed`, `unresolved` o `missing`.
Un nombre de código que aún no está en trunk espera hasta `memory_code_links_pending_days` (14 días).
Consulta [memorias enlazadas a código](https://github.com/ClickHouse/loom/blob/main/docs/concepts/memory.md#memories-linked-to-code).

**Espacio de nombres de código** — un espacio de nombres de tipo `code` que contiene un repositorio git indexado: sus fragmentos, símbolos, aristas, commits e historial de PR.
Consulta [índice de código](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md).

**Cobertura de commit** — si todos los niveles del índice han procesado un commit dado; `code.index_status(commit=...)` lo responde, y `code.coverage.watch` notifica cuando pasa a ser verdadero. Consulta [frescura](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/freshness.md).

**Nivel del compilador** — hechos del índice de código derivados de un indexador de calidad de compilador (SCIP) en lugar del nivel del parser; aristas de llamada y referencias resueltas mediante información de tipos.

**Conflicto** — una escritura semánticamente cercana a una memoria existente (coseno ≥ `LOOM_CONFLICT_THRESHOLD`). Ambas filas se marcan como `conflicted` y quedan enlazadas entre sí; una persona o un agente las resuelve. Consulta [conflictos y rechazo](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/conflicts-and-refusal.md).

**Consolidación** — el proceso en segundo plano que fusiona memorias fragmentadas sobre el mismo sujeto y registra el linaje (`memory_belief_sources`, `support_type='derived_from'`).

**Hash de contenido** — `content_sha256` del valor de una memoria. Deduplica repeticiones exactas, respalda la precondición `if_match_sha256` y ancla las filas de auditoría.

**Paquete de contexto** — `context.pack`: un conjunto acotado de memoria de trabajo, perfil, memorias relevantes, procedimientos y decisiones, cargado una vez al inicio de una sesión.
Consulta [búsqueda unificada](https://github.com/ClickHouse/loom/blob/main/docs/features/unified-search.md#contextpack).

**Panel** — la aplicación web en la raíz del servidor, para personas: memoria, actividad, fuentes, cola de revisión, tareas, repositorios y configuración. Consulta [panel](https://github.com/ClickHouse/loom/blob/main/docs/features/dashboard.md).

**Decisión** — un registro de primera clase de una elección (`decision.record`) con su razonamiento, alternativas descartadas y, más adelante, su resultado (`decision.outcome`).
Consulta [tareas y decisiones](https://github.com/ClickHouse/loom/blob/main/docs/features/tasks-and-decisions.md).

**Arista de entidad** — un vínculo ponderado de coocurrencia entre dos entidades en `loom.entity_edges`, con un intervalo de validez; el grafo que recorre `facts.traverse`.
Consulta [grafo de entidades](https://github.com/ClickHouse/loom/blob/main/docs/features/entity-graph.md).

**Saga de borrado** — la secuencia reanudable y con cierre ante fallos de pasos que ejecuta `erasure.request` para eliminar los datos de un sujeto de todas las tablas (y de los backends externos configurados), y que termina con un censo de residuos. Consulta [retención y borrado](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/retention-and-erasure.md).

**Extracción** — convertir mensajes de conversación en memorias candidatas (`memory.set_from_messages`), con un LLM y filtros de calidad.
Consulta [extracción](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/extraction.md).

**Extractor stamp** — un hash de los tokens de código de los módulos extractores del índice de código. Un cambio en él vuelve a fragmentar todos los repositorios indexados; los comentarios y docstrings no cuentan.
Consulta [arquitectura del índice de código](architecture/code-index-architecture.md).

**FINAL** — el modificador de ClickHouse que reduce una tabla `ReplacingMergeTree` a la última versión por clave en el momento de la lectura. Loom lee `loom.memory` con él.
Consulta [ClickHouse](https://github.com/ClickHouse/loom/blob/main/docs/architecture/clickhouse.md).

**Fence** — el token que `task.claim` devuelve junto con un arrendamiento; las llamadas posteriores a `task.progress`, `task.heartbeat`, `task.complete` y `task.fail` deben presentarlo, de modo que quien haya perdido el arrendamiento no pueda escribir.

**Grant** — una fila que otorga a un sujeto un rol sobre un espacio de nombres (`grants.*`).
Consulta [tokens y grants](https://github.com/ClickHouse/loom/blob/main/docs/guides/tokens-and-grants.md).

**Tupla de identidad** — `(org, namespace, agent, user_id, session_id)` en cada fila. Los tres primeros son obligatorios; los dos últimos toman por defecto `'-'`.
Consulta [identidades y espacios de nombres](https://github.com/ClickHouse/loom/blob/main/docs/concepts/identities-and-namespaces.md).

**`ignored_args`** — el campo de respuesta que nombra los campos de la solicitud que el servidor no usó (desconocidos o retirados), de modo que el cliente detecta la deriva sin que haya un fallo.

**Inbox** — una cola con nombre de tareas que pertenece a un espacio de nombres (`task.*`, `inbox.*`).
Consulta [tareas y decisiones](https://github.com/ClickHouse/loom/blob/main/docs/features/tasks-and-decisions.md).

**Documento de conocimiento** — un documento añadido con `knowledge.add`, dividido en fragmentos y con embeddings para su recuperación. Distinto de una memoria: es material de referencia, no una creencia.
Consulta [documentos de conocimiento](https://github.com/ClickHouse/loom/blob/main/docs/features/knowledge.md).

**Learning** — un candidato que espera en `loom.learnings_queue` la decisión de una persona (`learnings.list`, `learnings.review`): un hecho respaldado por varios escritores, promovido a la capa compartida; un hecho originado en una conversación, promovido a una fila de memoria del espacio de nombres; o una regla que un bot enunció sobre sí mismo, promovida al conocimiento del espacio de nombres.

**`log_comment`** — el ajuste de consulta de ClickHouse que Loom rellena con la op, el worker o el comando de CLI que realiza la llamada, de modo que `system.query_log` atribuye cada sentencia.

**`maturity`** — el campo declarado de una op, `stable`/`experimental`. Una op experimental que ni el conjunto core ni el atomic incluyen queda fuera del listado `all`; sigue pudiéndose llamar por su nombre. Consulta [toolsets de MCP](https://github.com/ClickHouse/loom/blob/main/docs/reference/mcp.md#toolsets).

**Herramienta MCP** — la proyección de una op en el Model Context Protocol, llamada `loom_<op>` con los puntos sustituidos por guiones bajos (`loom_memory_set`), más algunas herramientas sintéticas como `loom_search` y `loom_op`. Consulta [MCP](https://github.com/ClickHouse/loom/blob/main/docs/reference/mcp.md).

**Memoria** — una fila en `loom.memory`: una clave, un valor, su tupla de identidad, tipo, etiquetas, validez y procedencia. Consulta [memoria](https://github.com/ClickHouse/loom/blob/main/docs/concepts/memory.md).

**Clave de memoria** — el identificador legible de una memoria dentro de su espacio de nombres y agente, por convención `<scope>/<kind>/<id>`. Una escritura sobre una clave existente la reemplaza.

**Tipo de memoria** — la clase de una memoria: `semantic`, `episodic`, `procedural`, `preference`, `profile` y otras; el tipo decide si se le calcula un embedding y cómo se busca.

**Mentor hint** — una breve sugerencia de uso que Loom adjunta a los resultados o sirve mediante `mentor.brief`, y que orienta a un agente hacia un plano o una op que no está aprovechando bien.

**Espacio de nombres** — la unidad de almacenamiento, compartición y acceso: un espacio de trabajo con nombre dentro de una org. Los tipos son `memory`, `code` y `bench`.
Consulta [espacios de nombres y compartición](https://github.com/ClickHouse/loom/blob/main/docs/features/namespaces-and-sharing.md).

**Operación (op)** — una entrada declarada en el registro (`server.operations.registry.ALL_OPERATIONS`), servida en `POST /v1/<op>`, como herramienta MCP y mediante los SDK.
Consulta [referencia de operaciones](https://github.com/ClickHouse/loom/blob/main/docs/reference/operations.md).

**Org** — el tenant de nivel superior; el primer elemento de la tupla de identidad.

**Pinned** — una memoria marcada con `memory.pin`. Una escritura en conflicto con una memoria fijada se rechaza con `409 PINNED_CONFLICT`.

**Plano** — un área de producto con sus propias ops y tablas: memoria, conocimiento, conversación, grafo, código, tracker, ejecuciones y tareas, auditoría.

**Procedimiento** — un how-to reutilizable (`procedure.add` / `match`), almacenado como una memoria de tipo `procedural`. Consulta [procedimientos y learnings](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/procedures-and-learnings.md).

**Perfil** — el contexto permanente de un agente o usuario (`profile.*`), cargado en primer lugar por `context.pack`.

**Proyección** — una fila derivada de una escritura (un embedding, una arista de grafo, un resumen). Una proyección que falla se registra y se reintenta (`ops.projection_failures.*`).

**Alcance de lectura** — `read_scope`, que acota una lectura dentro de un espacio de nombres: `namespace` (todas las filas, el valor por defecto), `own_user` u `own_session`. La reclamación de un token es un techo que una solicitud puede estrechar pero no ampliar.
Consulta [identidades y espacios de nombres](https://github.com/ClickHouse/loom/blob/main/docs/concepts/identities-and-namespaces.md#read-scope-inside-a-namespace).

**Registro (record)** — una memoria estructurada validada contra un esquema de registro (`memory_schema.*`). No debe confundirse con un registro del tracker.

**ReplacingMergeTree (RMT)** — el motor de ClickHouse detrás de la mayoría de las tablas de Loom: una fila de nueva versión reemplaza a la anterior al fusionar o al leer con `FINAL`, por lo que toda ruta de escritura debe arrastrar todas las columnas. Consulta [ClickHouse](https://github.com/ClickHouse/loom/blob/main/docs/architecture/clickhouse.md).

**Censo de residuos** — el último paso de una saga de borrado: un recuento de filas vivas dentro del alcance en todas las tablas que tocan las rutas de lectura. Cualquier residuo hace fallar la saga.

**Rol** — `viewer`, `writer`, `admin`, `org_admin`, `platform_admin`, en orden creciente de privilegio. Cada op declara el mínimo que necesita.

**Ejecución (run)** — una ejecución de un agente, delimitada por `run.begin` y `run.complete`, que registra lo que recuperó y escribió. Consulta [ejecuciones](https://github.com/ClickHouse/loom/blob/main/docs/features/runs.md).

**Sesión** — una conversación o período de trabajo, el quinto elemento de la tupla de identidad; un **resumen de sesión** (`session.brief_*`) es su resumen guardado para retomarla más tarde.

**Capa compartida** — una fila canónica por `(org, namespace, memory_key)`, promovida a partir de las filas que escriben los agentes (`memory.promote` / `memory.demote`, tabla `loom.shared`).
"Privada" para la otra capa significa no promovida, no oculta.

**Clase de fuente** — la procedencia de una fila estampada por el servidor: `asserted`, `extracted`, `observed`, `derived`, `imported` o `external`. Una solicitud solo puede degradarla.

**Nivel (tier)** — qué extractor produjo un hecho del índice de código (parser o compilador); las respuestas indican qué nivel las respalda.

**Tombstone** — una fila de versión superior que marca una memoria como eliminada. `memory.restore` puede deshacerlo dentro del período de gracia; `memory.delete` no elimina nada físicamente.

**Toolset** — el listado de MCP que solicita un cliente (`core`, `coding`, `atomic`, `tasks`, `tracker`, `readonly`, `all`); cambia lo que se lista, no lo que el token puede llamar.

**Tracker** — el plano que lee los registros de un issue tracker (issues, PRs, ejecuciones de CI) desde una fuente configurada para comprobar duplicados, bisect e historial de tests.
Consulta [rastreador](https://github.com/ClickHouse/loom/blob/main/docs/features/tracker/overview.md).

**Modo de confianza** — la política de aceptación que el propietario de un inbox establece para quien publica; Loom la almacena y nunca la cambia por sí mismo.

**Tiempo de validez** — `valid_at` / `valid_to` en las memorias (`valid_from` / `valid_to` en las aristas de entidad): cuándo un hecho fue verdadero en el mundo. Loom registra solo el tiempo de validez; no es bitemporal.

**Versión** — la columna monótonamente creciente de la que un `ReplacingMergeTree` conserva el valor más alto; cada escritura de una memoria (incluido un borrado) es una nueva versión.

**Worker** — un bucle supervisado en segundo plano registrado como una fila en `server/workers/registry.py` (`_WORKERS`). Consulta [procesos de trabajo](https://github.com/ClickHouse/loom/blob/main/docs/reference/workers.md).

**Memoria de trabajo** — bloques de notas (`working_memory.*`) para la tarea en curso: sin embedding, sin ranking, cargados por `context.pack` antes que la memoria duradera.
Consulta [memoria de trabajo y perfiles](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/working-memory-and-profiles.md).
