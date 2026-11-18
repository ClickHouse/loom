<!-- i18n: source=docs/faq.md blob=d1e8cdcb41e8e749ecd46d194697f615642a3ca8 model=claude-sonnet-5-5 -->
> Esta página es una traducción automática de la [página en inglés](https://github.com/ClickHouse/loom/blob/main/docs/faq.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/faq.md), which is authoritative.

<a id="frequently-asked-questions"></a>

# Preguntas frecuentes

Respuestas breves a las primeras preguntas que suele hacer la gente. Cada una enlaza a la página que la explica.

<a id="using-it"></a>

## Uso

<a id="what-is-the-fastest-way-to-try-loom"></a>

### ¿Cuál es la forma más rápida de probar Loom?

Si ya existe un servidor de Loom, instala el plugin de Claude Code o de Codex y apúntalo al
servidor: [conectar un agente de código](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md). Para ejecutar un servidor
en un portátil, `loom up` levanta uno en pocos minutos: [inicio rápido](getting-started/quickstart.md).

<a id="how-do-i-give-claude-code-codex-or-cursor-long-term-memory"></a>

### ¿Cómo doy memoria a largo plazo a Claude Code, Codex o Cursor?

Usa los plugins de Claude Code o Codex para el contexto y la captura al inicio de sesión, o conecta cualquier
otro cliente MCP con `loom install --client <name>` (consulta [clientes MCP](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/mcp-clients.md)).
Levanta un servidor con el [inicio rápido](getting-started/quickstart.md). Cursor recibe la entrada
de MCP y los skills `loom` y `loom-code` (sin hooks para el contexto al inicio de sesión; aún no
probado en Cursor); la captura de transcripciones proviene del hook independiente `sessionEnd`. Consulta
[Conectar un agente de código](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md) y
[Cursor](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/cursor.md).

<a id="why-are-my-search-results-nonsense"></a>

### ¿Por qué mis resultados de búsqueda no tienen sentido?

La instalación ejecuta un embedder falso: su `.env` (el de un checkout, o `~/.loom/.env`) nombra
`fake-ngram-*`, cuyos vectores hacen que la recuperación carezca de sentido. Cambia su embedder, lo que
vuelve a calcular los embeddings de lo almacenado ([Modelos y claves](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/models-and-keys.md)); una instalación
nueva del stack publicado ya usa un modelo local para los embeddings. Un embedder alojado
recupera mejor en preguntas con muchas paráfrasis
([proveedores](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#providers)).

<a id="why-does-memorysearch-say-abstain-instead-of-returning-something"></a>

### ¿Por qué `memory.search` responde `abstain` en lugar de devolver algo?

Ningún resultado superó el umbral de similitud calibrado para tu embedder, así que Loom indica que no
lo sabe en lugar de devolver la coincidencia débil más cercana. El motivo aparece en la respuesta. Consulta
[recuperación y ranking](https://github.com/ClickHouse/loom/blob/main/docs/concepts/retrieval-and-ranking.md).

<a id="why-was-my-write-flagged-as-a-conflict-or-refused"></a>

### ¿Por qué mi escritura se marcó como conflicto o se rechazó?

Una escritura de significado cercano a una memoria existente se marca como `conflicted` y se vincula a ella; una
escritura cercana a una memoria fijada se rechaza con `409 PINNED_CONFLICT`; una escritura que coincide con un
patrón de la lista negra recibe `422 BLACKLISTED`. Resuelve los conflictos en la cola de revisión del dashboard o
con `conflicts.resolve`. Consulta [conflictos y rechazo](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/conflicts-and-refusal.md).

<a id="can-i-use-loom-just-for-myself"></a>

### ¿Puedo usar Loom solo para mí?

Sí. Ejecuta el [stack local](getting-started/quickstart.md), que no tiene inicio de sesión y tiene un solo
espacio de nombres, y conecta tus agentes con el [plugin o `loom install`](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md).
En un servidor con inicio de sesión, cada persona recibe un espacio de nombres `personal-<slug>` en el que sus clientes MCP
escriben por defecto y que nadie más lee sin una concesión. La memoria guardada desde
Claude Code está entonces disponible para Codex, Cursor y el dashboard.

<a id="can-two-agents-see-each-others-memories"></a>

### ¿Pueden dos agentes ver las memorias del otro?

Todos los miembros de un espacio de nombres pueden leer sus filas. Cada agente escribe en su propia capa, y un
administrador puede copiar una memoria a la capa compartida del espacio de nombres con `memory.promote`. Un token puede leer varios
espacios de nombres mientras escribe en uno. Consulta [identidades y espacios de nombres](https://github.com/ClickHouse/loom/blob/main/docs/concepts/identities-and-namespaces.md).

<a id="when-should-i-use-knowledge-documents-instead-of-memories"></a>

### ¿Cuándo debo usar documentos de conocimiento en lugar de memorias?

Una memoria es una afirmación corta que se puede revisar. Un documento, una página larga o cualquier cosa de más de
un par de kilobytes va al plano de conocimiento con `knowledge.add`, que lo divide en fragmentos y calcula sus embeddings.
Consulta [memoria](https://github.com/ClickHouse/loom/blob/main/docs/concepts/memory.md#what-goes-where) y [documentos de conocimiento](https://github.com/ClickHouse/loom/blob/main/docs/features/knowledge.md).

<a id="how-do-i-make-loom-forget-something"></a>

### ¿Cómo hago que Loom olvide algo?

`memory.delete` marca una memoria con una lápida (restaurable durante un periodo de gracia);
`erasure.request` elimina todo lo que posee un usuario o espacio de nombres, en todas las tablas, y
verifica que no quede nada. Loom no borra nada por su cuenta según un calendario, salvo que un operador defina una
ventana de retención. Consulta [retención y borrado](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/retention-and-erasure.md).

<a id="how-do-i-find-out-why-a-result-ranked-where-it-did"></a>

### ¿Cómo averiguo por qué un resultado quedó en esa posición del ranking?

Llama a `memory.search` con `explain=true`: el `score_components` de cada resultado contiene las señales de
ranking y, tras un reranker, su movimiento (`rerank_from`, `rerank_pos`, `rerank_delta`) y,
cuando el reranker cambió el orden, su propia puntuación fusionada (`rank_score_pre_rerank`).
`memory.search_trace` puntúa solo la rama vectorial. Consulta [búsqueda y
ask](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/search-and-ask.md).

<a id="can-loom-answer-questions-about-my-code"></a>

### ¿Puede Loom responder preguntas sobre mi código?

Sí, para los repositorios que indexa el servidor: `code.search`, `code.symbol`, `code.callers`,
`code.impact` y más, y cada respuesta indica qué commit y qué nivel de índice la respaldan. Consulta el
[índice de código](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md) y [añadir un repositorio](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/repositories.md).

<a id="why-doesnt-my-coding-agent-use-the-code-index"></a>

### ¿Por qué mi agente de código no usa el índice de código?

El plugin adjunta el índice solo cuando la sesión empieza dentro de un checkout de git cuyo
remoto es un repositorio indexado que puedes leer. Inicia la sesión desde el checkout del repositorio,
comprueba que el repositorio aparece en **Repositories** en el dashboard (`<server>/repositories`), y consulta
[cuándo se usa el índice de código](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md#when-the-code-index-is-used).

<a id="running-it"></a>

## Ejecutarlo

<a id="do-i-need-clickhouse-cloud"></a>

### ¿Necesito ClickHouse Cloud?

No. Loom también funciona con ClickHouse OSS, y el stack para portátil levanta uno en Docker. Cloud es
la ruta de producción prevista porque ClickHouse OSS de un solo nodo no tiene replicación ni recuperación
a un punto en el tiempo. Consulta el [despliegue](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md).

<a id="can-i-use-anthropic-or-gemini-models"></a>

### ¿Puedo usar modelos de Anthropic o Gemini?

Sí. El proveedor de LLM puede ser OpenAI o Anthropic, y los embeddings pueden venir de OpenAI,
Gemini o de un modelo local en CPU. Cambiar el modelo de embeddings cambia el espacio vectorial, así que ejecuta
`loom reembed` después. Consulta [LLM y embeddings](https://github.com/ClickHouse/loom/blob/main/docs/architecture/llm-architecture.md).

<a id="how-do-i-give-someone-access"></a>

### ¿Cómo doy acceso a otra persona?

Genera un token (`loom mint-token`, o en el dashboard, Settings → Tokens) y concede un rol sobre un
espacio de nombres. Consulta [tokens y permisos](https://github.com/ClickHouse/loom/blob/main/docs/guides/tokens-and-grants.md).

<a id="something-is-wrong-where-do-i-start"></a>

### Algo va mal. ¿Por dónde empiezo?

`loom doctor` en el servidor, `/loom:loom-status` en Claude Code y `/v1/health`. Después, la
[solución de problemas](https://github.com/ClickHouse/loom/blob/main/docs/operations/troubleshooting.md), que está organizada por síntoma.

<a id="the-project"></a>

## El proyecto

<a id="why-is-an-operation-missing-from-my-mcp-tool-list"></a>

### ¿Por qué falta una operación en mi lista de herramientas MCP?

El listado de MCP depende del toolset (`?toolset=` o `LOOM_MCP_TOOLSET`, por defecto
`core`), y las herramientas de código solo se listan con `LOOM_MCP_CODE_TOOLS=true`. Bajo `all`, una
op que ni el roster core ni el atómico listan y que está marcada como `experimental` se
deja fuera. Aun así, toda op se puede invocar por nombre mediante `loom_op` (solo lecturas en
`readonly`), por HTTP y desde los SDK. Consulta [toolsets de MCP](https://github.com/ClickHouse/loom/blob/main/docs/reference/mcp.md#toolsets).

<a id="how-good-is-the-retrieval-and-how-do-you-know"></a>

### ¿Qué tan buena es la recuperación y cómo se sabe?

Loom incluye su suite de pruebas de rendimiento con el método, los datasets y los comandos exactos. Solo se indican resultados
reproducibles a partir de archivos confirmados en el repositorio. Consulta [pruebas de rendimiento](benchmarks/overview.md).

<a id="how-is-loom-different-from-putting-a-vector-database-next-to-my-app"></a>

### ¿En qué se diferencia Loom de poner una base de datos vectorial junto a mi aplicación?

Con la autoridad ClickHouse por defecto, una sola base de datos ClickHouse contiene las filas, los vectores,
el índice de texto, el grafo, la auditoría y el estado de borrado, de modo que no hay nada que mantener sincronizado. La
[autoridad de memoria en Postgres](architecture/storage-authority.md#why-two-stores) guarda el estado vivo de la
memoria y del conocimiento en Postgres, y Loom mantiene por sí mismo la coherencia entre ambos almacenes.
Cada escritura se comprueba contra lo almacenado. La
[descripción general](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/overview.md) enumera lo que cuesta cada propiedad y cuándo Loom
no encaja.
