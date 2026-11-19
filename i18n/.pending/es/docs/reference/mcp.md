<!-- i18n: source=docs/reference/mcp.md blob=6a2cb653387481ccd3b42d2036cf7323fc58a763 model=claude-sonnet-5-5 -->
> Esta página es una traducción automática de la [página en inglés](https://github.com/ClickHouse/loom/blob/main/docs/reference/mcp.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/reference/mcp.md), which is authoritative.

<a id="mcp-reference"></a>

# Referencia de MCP

Loom expone sus operaciones como herramientas MCP en `<loom>/mcp`. Cualquier cliente MCP que
hable HTTP en streaming se conecta con una URL y un token bearer. La lista de herramientas
se genera a partir del mismo registro de operaciones que la [API HTTP](https://github.com/ClickHouse/loom/blob/main/docs/reference/http-api.md),
y una llamada a una herramienta sigue la misma ruta de autorización, límite de tasa, auditoría e idempotencia
que `POST /v1/<op>`. El transporte está en `server/transport/mcp.py`, con la llamada a una
herramienta en `mcp_call.py` y `mcp_outcome.py`, las comprobaciones del bearer en `mcp_auth.py`, su
listado en `mcp_tools.py`, el modelado de esquemas en `mcp_schemas.py`, las constantes de protocolo y
sesión en `mcp_config.py` y las descripciones curadas en
`mcp_descriptions.py`.

La configuración de un cliente concreto (Claude Code, Codex, Cursor, VS Code, Claude Desktop
y otros) con `loom install` o a mano está en
[Clientes MCP](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/mcp-clients.md).

<a id="endpoint"></a>

## Punto de conexión

| Solicitud                        | Qué hace                                                                                                                                                                            |
| -------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `POST /mcp`                      | Una solicitud JSON-RPC o un lote de 1 a 64. La respuesta es un cuerpo JSON, no un flujo de eventos. Una notificación responde `202` sin cuerpo.                                       |
| `DELETE /mcp` + `Mcp-Session-Id` | Termina la sesión y cierra su ejecución implícita. Tiene límite de frecuencia como `run.complete`, por llamador dentro del presupuesto de la organización: al superar el tope responde `429` con `Retry-After`. |

Los parámetros de consulta en la URL seleccionan lo que `tools/list` muestra para esa conexión:

- `?toolset=` elige la lista que se muestra ([Conjuntos de herramientas](#toolsets)). Valor por defecto:
  `LOOM_MCP_TOOLSET` (`core`).
- `?schema_profile=` elige la forma del listado: `standard` (la descripción
  curada y el esquema de entrada; el valor por defecto, `LOOM_MCP_SCHEMA_PROFILE`),
  `full` (añade `outputSchema`) o `compact` (el primer párrafo y el
  árbol de argumentos).

Un valor desconocido en cualquiera de los dos vuelve al valor por defecto del despliegue, de modo que un error tipográfico nunca amplía ni reduce un listado en silencio. El valor de `?toolset=` también da forma al
primer de sesión (las instrucciones de `initialize`, `server/discover`, el prompt `loom`, `loom://primer` y `loom_activate`): el primer nombra la herramienta propia de una op cuando el toolset la incluye; si no, nombra la op marcada con "(via loom_op)", y omite una op que `loom_op` no enruta allí.

Métodos atendidos: `initialize`, `notifications/initialized`,
`notifications/cancelled`, `ping`, `tools/list`, `tools/call`, `prompts/list`,
`prompts/get` (el prompt `loom`: el primer de sesión), `resources/list`,
`resources/read` (`loom://primer`) y `server/discover`. Los resultados de listas incluyen las pistas `ttlMs` y `cacheScope` para que un cliente pueda almacenarlos en caché.

**Versiones del protocolo.** `2025-11-25`, `2025-06-18`, `2025-03-26` y
`2024-11-05` (`MCP_SUPPORTED_PROTOCOL_VERSIONS`). `initialize` responde con la
versión del cliente cuando es una de estas; si no, con `2025-11-25`. Un
encabezado `MCP-Protocol-Version` que nombre otra revisión produce un HTTP 400 con el error JSON-RPC
`-32022`, cuyo `data.supported` enumera las versiones atendidas. `server/discover`
responde con la misma lista, las capacidades, la identidad del servidor y las
instrucciones.

<a id="authentication"></a>

## Autenticación

`LOOM_MCP_AUTH_MODE` decide qué métodos necesitan un bearer:

| Modo                      | Bearer requerido en                                             |
| ------------------------- | --------------------------------------------------------------- |
| `required` (predeterminado) | todos los métodos excepto las notificaciones                  |
| `discovery_only`          | solo `tools/call`; todos los demás métodos están abiertos       |
| `open`                    | ninguno; un bearer inválido se ignora (solo redes de confianza) |

Un servidor en un `LOOM_BIND_HOST` que no es loopback se niega a iniciar con `open` a menos que
`LOOM_ALLOW_INSECURE=1`. Un despliegue con la autenticación desactivada (`LOOM_AUTH_REQUIRED=false`)
se comporta como `open`. Un bearer ausente o inválido en un método protegido produce HTTP `401` con
`WWW-Authenticate: Bearer realm="loom"` (más `error="invalid_token"` cuando se
envió un token). Un token que todavía no se puede comprobar, porque la lista de
revocación no se ha cargado, produce en cambio HTTP `503` con `Retry-After`, de modo que el cliente
conserva el token y reintenta. **Por qué un 401 y no un error JSON-RPC:** los clientes basan su flujo de
"necesita autenticación", y su descubrimiento OAuth, en el estado HTTP; un cuerpo de
error JSON-RPC se mostraría en cambio como un error de análisis o de herramienta. El token
es un JWT ordinario de Loom; consulta [Tokens y permisos](https://github.com/ClickHouse/loom/blob/main/docs/guides/tokens-and-grants.md).

El bearer se lee únicamente del encabezado `Authorization`. Un token pasado como `?token=` en
la URL de `/mcp` se ignora y la solicitud queda sin autenticar: las líneas de solicitud terminan en los registros
de proxy y de acceso, donde un token en la URL quedaría escrito en texto plano.

<a id="oauth"></a>

### Autorización con OAuth

Se sirve solo con `LOOM_OAUTH_ENABLED=true`. Loom es un servidor de autorización OAuth 2.1 para clientes que
se conectan mediante inicio de sesión en el navegador en lugar de un token pegado (`server/auth/oauth.py`):

- Descubrimiento en `/.well-known/oauth-authorization-server` y
  `/.well-known/oauth-protected-resource`.
- Registro dinámico de clientes en `/oauth/register`: clientes públicos, PKCE
  `S256` obligatorio.
- `/oauth/authorize` y `/oauth/token` emiten un token de acceso más un token de
  actualización rotativo. Una actualización tiene éxito solo cuando la solicitud indica el
  `client_id` al que se emitió el token.
- El token de acceso es un JWT de Loom para el usuario que inició sesión, de modo que los espacios de nombres a los que el usuario
  se una más adelante son accesibles en la misma conexión.
- El inicio de sesión usa el login con contraseña de Loom, a menos que `LOOM_OAUTH_LOGIN_PROVIDER=oidc`
  lo delegue a un proveedor de identidad externo (`LOOM_OIDC_ISSUER`,
  `LOOM_OIDC_CLIENT_ID`, `LOOM_OIDC_CLIENT_SECRET`). Delegar el inicio de sesión también
  cierra el autorregistro con contraseña.
- Un despliegue remoto debe servir HTTPS y establecer `LOOM_OAUTH_ISSUER` con su
  URL base pública `https://`.
- Un URI de redirección registrado debe ser absoluto y sin fragmento: `https`, `http`
  hacia un host loopback, o un esquema de aplicación (`cursor://…`). La página de inicio de sesión indica
  el host al que va la autorización.
- Cada actualización vuelve a comprobar que la cuenta sigue activa y vuelve a derivar sus
  roles, de modo que una cuenta liberada, o un sujeto eliminado de
  `LOOM_PLATFORM_ADMINS`, no conserva su acceso al actualizar.

[Seguridad](https://github.com/ClickHouse/loom/blob/main/docs/architecture/security.md) cubre el modelo de tokens que hay detrás de esto.

<a id="sessions-and-identity"></a>

## Sesiones e identidad

- **Id de sesión.** `initialize` asigna un `Mcp-Session-Id` y cada respuesta
  lo repite. El `clientInfo.name` del cliente se guarda en caché asociado a ese id y pasa a ser
  el `agent` de las llamadas posteriores, convertido a slug (`Cursor` → `cursor`; si falta →
  `claude-desktop`). La caché está en el proceso y tiene un límite de tamaño.
- **Valores de identidad por defecto.** Las llamadas a herramientas suelen llevar solo los argumentos de la acción. El
  servidor completa `org` a partir del token, y `namespace` y `agent` a partir del
  workspace predeterminado del llamador; los argumentos explícitos tienen prioridad. Las llamadas `code.*` se resuelven en el plano de código
  en su lugar, y en el toolset `assistant` una lectura que no indica `namespace` o no indica `agent`
  sigue las reglas de ese toolset (consulta [El toolset assistant](#the-assistant-toolset)). Un argumento `org`
  en el que el token no puede actuar se rechaza (`ORG_MISMATCH`): un token actúa en su
  propia org, un token de toda la org (`*`) o un administrador de la plataforma en cualquier org, la misma regla que
  sobre HTTP (`authz.may_act_in_org`). Un espacio de nombres restringido sigue rechazando a cualquier otra
  org, incluidos los administradores de la plataforma. `loom_search` no acepta el argumento `namespace`: busca
  en el espacio de nombres principal del llamador y en sus otros espacios de nombres de tipo memoria, hasta
  el límite de dispersión.
- **Ámbito de código.** Un cliente puede enviar `X-Loom-Code-Scope: <org>/<namespace>` (el
  plugin de Claude Code envía el repositorio al que se resolvió su checkout). Se aplica
  a toda llamada `code.*` que no indique un espacio de nombres, y la autorización se ejecuta entonces sobre ese
  espacio de nombres como si se hubiera indicado. Con esa cabecera, `loom_search` también lee
  los documentos del repositorio y las memorias `decision` de su espacio de nombres de código principal (trunk),
  cuando el llamador puede leerlo, y ejecuta la comprobación de duplicados del tracker para una
  consulta con forma de defecto.
- **Ejecución implícita.** Una ejecución por (org, sujeto del token, sesión), iniciada en el
  registro de ejecuciones por la primera llamada a una herramienta con ámbito de ejecución de la sesión y archivada en el
  espacio de nombres predeterminado del llamador; las escrituras llevan su `run_id` (excepto `run.*`, que
  indica su propia ejecución, `code.*`, que actúa como la identidad de código del despliegue, y
  `working_memory.*` y `conversation.update`, donde `run_id` delimita o vincula
  la fila), y las lecturas se etiquetan con él pero no se filtran. La ejecución se cierra con
  `DELETE /mcp`, o cuando el worker `mcp_session_run_sweep` no encuentra ninguna llamada del
  token bajo ella durante `LOOM_MCP_SESSION_RUN_IDLE_SECONDS` (1800 por defecto); un
  despliegue que no ejecuta workers en segundo plano en ningún lugar mantiene abiertas las ejecuciones inactivas. La
  ejecución no almacena un recuento de llamadas: sus llamadas son las filas de `agent_events` con su
  `run_id`. Consulta [Ejecuciones](https://github.com/ClickHouse/loom/blob/main/docs/features/runs.md).

<a id="tool-list"></a>

## Lista de herramientas

Toda operación del registro que no esté en `_MCP_HIDDEN_OPS` es una herramienta:

- **Nombre.** `loom_` más el nombre de la operación con los puntos convertidos en guiones bajos (`memory.set` →
  `loom_memory_set`).
- **Descripción.** El texto curado en `TOOL_DESCRIPTIONS`
  (`server/transport/mcp_descriptions.py`) cuando existe; si no, un texto de una línea de respaldo con el presupuesto de latencia de la operación, su idempotencia y el rol mínimo. Los
  textos curados indican cuándo usar una herramienta y cuándo no; la elección se deja al
  modelo. Un texto nombra otras operaciones por su nombre de operación, y cada listado lo representa para el cliente que lo lee (`name_ops_for_listing` en
  `server/transport/mcp_op.py`): una operación que el listado incluye se nombra por su
  herramienta (`loom_memory_update`), y una operación que no incluye conserva su nombre de operación,
  el que acepta `loom_op`, con "(via loom_op)" después de su primera mención cuando
  `loom_op` la enruta en esa conexión; no enruta una operación oculta, una
  escritura en `readonly` ni nada en `assistant`. Las descripciones de los campos de entrada, y
  el esquema de salida que conserva el perfil `full`, se representan de la misma manera, y también
  el texto `instead` de una operación oculta; el texto propio de `loom_op`, que trata de los nombres de operación, se construye en cambio para cada listado. El texto del mentor se representa para el
  listado del llamador también por MCP (el `hint` del resultado de una herramienta, la sección `mentor` de
  `context.pack`, y los hints y el `when` del plano de `mentor.brief`); por HTTP
  se lee tal como está almacenado.
- **Esquema de entrada.** El modelo de entrada Pydantic de la operación sin los campos que el
  servidor completa por sí mismo (`org`, `agent`, `user_id`, `session_id`, `consumer`),
  los campos solo del dashboard (`_HIDDEN_OP_PROPS` en
  `server/transport/mcp_schemas.py`) y todo campo marcado como `MCP_HIDDEN`;
  `namespace` se mantiene como propiedad opcional. Los campos ocultos se siguen aceptando
  por nombre.
- **Anotaciones.** `readOnlyHint`, `destructiveHint` e `idempotentHint` se
  derivan del registro (rol, idempotencia, auditoría, verbos destructivos en el
  nombre). `readOnlyHint` es verdadero solo para una operación idempotente de viewer que no está
  auditada ni marcada como `writes`, de modo que un cliente que aprueba automáticamente las herramientas de solo lectura
  nunca ejecuta un cambio de estado sin confirmación. Son indicaciones; la comprobación de rol en el momento de la llamada es el límite real.

Se listan tres herramientas sintéticas en todos los toolsets excepto `assistant`:

| Herramienta     | Qué hace                                                                                                                                                                                                             |
| --------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `loom_search`   | Una sola llamada busca en memoria, perfil, conocimiento y conversaciones en paralelo y devuelve las cuatro secciones. `explain: true` añade por qué cada resultado de memoria quedó en su posición.                  |
| `loom_activate` | Devuelve el primer de la sesión (perfil, preferencias, workspace predeterminado), el mismo texto que el prompt `loom`.                                                                                               |
| `loom_op`       | `{op, arguments}` ejecuta cualquier operación que la lista no incluya, por la misma ruta que una herramienta listada; `{op: "help", query}` lista hasta 15 operaciones coincidentes (`_CATALOG_LIMIT`), cada una con sus argumentos obligatorios y opcionales. |

`loom_search` es la operación `search.query` del registro (`POST /v1/search.query`
por HTTP, las mismas secciones sin el resumen de texto). Por MCP sigue la misma
ruta que toda herramienta del registro (valores predeterminados del llamador, ejecución de sesión, autorización, auditoría de
acceso restringido, clase de tráfico, política de historial, límite de tasa, idempotencia) y conserva su
propio esquema listado, sus opciones tolerantes (un `top_k` fuera de rango o un `recipe` / `bodies` desconocido se ajusta, no se rechaza) y un resumen de texto junto al
resultado reducido. `Operation.mcp_tool` la nombra, así que no se lista ni se puede invocar una segunda herramienta `loom_search_query`. Consulta la
[búsqueda unificada](https://github.com/ClickHouse/loom/blob/main/docs/features/unified-search.md#searchquery-over-http-and-the-sdks).

**Las operaciones ocultas** (`_MCP_HIDDEN_OPS` en `server/operations/toolsets.py`) no están
listadas ni se pueden invocar por MCP: las búsquedas por almacén que `loom_search` reemplaza
(`memory.search`, `knowledge.search`, `conversation.search`), las colas de revisión,
snapshot y restore, la administración de políticas, los dashboards de auditoría y costos, y la ingesta del índice de código. Una llamada a una de ellas responde `isError` con el código `OP_NOT_ON_MCP` y qué usar
en su lugar. Siguen disponibles por HTTP y los SDK. En el toolset `assistant`,
`memory.search` y `knowledge.search` están listadas y se pueden invocar, y una llamada a cualquier
otra operación oculta responde `UNKNOWN_OP`. La regla para ocultar una operación: ¿la usaría en el chat un usuario final típico, no un operador? Una vista de todo el espacio de nombres puede ocultarse
mientras una variante limitada al llamador sigue en MCP: `namespace.list` está oculta y
`namespace.list_mine` se puede invocar (listada en `all` y `readonly`, mediante `loom_op` en
todos los demás toolsets excepto `assistant`); `memory.extraction_status` (cada lote en cola
en el espacio de nombres) está oculta y `memory.extraction_status_mine` (los lotes que puso en cola el llamador,
por `queue_id`, como máximo 20 por llamada) se puede invocar por nombre o mediante `loom_op`
en todos los toolsets excepto `assistant` (`readonly` incluido). Su gemela de escritura,
`memory.extraction_retry_mine`, vuelve a ejecutar una vez más los lotes fallidos del llamador;
`loom_op` la enruta en todos los toolsets excepto `readonly` y `assistant`.

<a id="toolsets"></a>

### Conjuntos de herramientas

Un toolset decide qué se **lista**; toda operación no oculta sigue pudiéndose invocar por nombre de
herramienta, o mediante `loom_op`, sea cual sea el toolset. Dos excepciones: en
`assistant` solo se pueden invocar sus propias lecturas, y en `readonly`, indicado por
`?toolset=` o por `LOOM_MCP_TOOLSET` cuando la solicitud no indica ninguno, `loom_op`
enruta solo operaciones de lectura.

| Toolset     | Lista                                                                                                                                                                                                                                                                                                                                                                                                                                | Para                                           |
| ----------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ---------------------------------------------- |
| `core`      | El ciclo de memoria: `memory.set/update/delete/pin/get/history/apply/lock`, `blacklist.add`, `context.pack`. Todas las demás operaciones se ejecutan mediante `loom_op`; ninguna herramienta listada en `core` registra turnos de conversación. Las herramientas de código se suman con `LOOM_MCP_CODE_TOOLS=true`.                                                                                                                    | clientes de chat (el valor predeterminado)     |
| `coding`    | `memory.set/update/get/delete`, `working_memory.set`, `context.pack`, las herramientas de código (`code.search`, `grep`, `enclosing`, `ask`, `symbol`, `outline`, `callers`, `context_pack`), `tracker.duplicate_check`, `code.bisect_candidates`, `code.pr_check`, `code.impact`.                                                                                                                                                    | agentes de programación                        |
| `tracker`   | `tracker.answer`, `binding`, `component`, `duplicate_check`, `flaky`, `item`, `links`, `release`, `similar`, `test_signal`, además de `code.bisect_candidates`, `code.pr_check`, `code.impact`.                                                                                                                                                                                                                                       | sesiones que trabajan en una prueba fallida, un fallo o un PR |
| `tasks`     | `task.pull/create/complete/fail/wont_do/defer/heartbeat/updates/list/get`, `inbox.list`.                                                                                                                                                                                                                                                                                                                                             | una sesión que actúa como agente con una bandeja de tareas |
| `atomic`    | `memory.set/get/delete/ask`, `working_memory.set/append`, `run.begin/complete`.                                                                                                                                                                                                                                                                                                                                                      | agentes mínimos, configurados por política     |
| `readonly`  | El listado `all` sin ninguna herramienta cuyo `readOnlyHint` sea falso; `loom_op` enruta solo operaciones de lectura.                                                                                                                                                                                                                                                                                                                | registros de solo lectura                      |
| `assistant` | Un conjunto fijo de lecturas para investigar Loom en nombre de una persona: `namespace.get/summary/inventory/tune/taxonomy/audit/validate/guide_get/guide_history/list_categories`, `mentor.brief/guide`, `help.search/get`, `grants.list`, `memory.inspect/search/ask/get/list/history/provenance/aggregate`, `facts.traverse/path`, `knowledge.search/get/list/inspect`, `code.search/symbol`, `insights.catalog/query`. Sin herramientas sintéticas. | un cliente de chat que actúa por una persona con sesión iniciada |
| `all`       | Toda operación no oculta cuya `maturity` sea `stable`, más las operaciones `experimental` que lista el roster core o atomic. Una operación experimental que ninguno de los dos rosters lista queda fuera; sigue pudiéndose invocar por nombre o mediante `loom_op`. Las herramientas de código solo con `LOOM_MCP_CODE_TOOLS=true`.                                                                                                    | operadores                                     |

Los rosters curados se combinan: `?toolset=coding+tracker`, `coding,tracker`, o un
`+` sin escapar que llega como espacio. Solo se pueden unir rosters curados (`all`,
`readonly` y `assistant` son superficies completas), y una parte desconocida hace que todo el valor
sea desconocido, así que el listado vuelve al valor predeterminado del despliegue. Los rosters son
los frozensets de `server/operations/toolsets.py`; `ops.capabilities` informa los
`toolsets` de cada operación. Un segundo registro MCP con otro toolset
(`?toolset=tracker`, `?toolset=tasks`) añade un roster sin cambiar el primero.

**Por qué el listado es pequeño.** El nombre, la descripción y el esquema de cada herramienta listada
se pagan en tokens en cada turno de cada sesión, y la precisión en la selección de herramientas
baja a medida que crece la lista. `tests/unit/test_mcp_listing_budget.py`
mantiene cada roster por debajo de un tope de tokens. Es preferible dejar una operación invocable por nombre
antes que listarla; [Cómo añadir una operación](https://github.com/ClickHouse/loom/blob/main/docs/development/adding-an-operation.md) explica
cuándo y cómo listar una.

<a id="results"></a>

## Resultados

`structuredContent` (y el JSON en `content[0].text`) es la salida de la op, recortada
a la respuesta (`server/transport/lean_results.py`): los resultados con su clave,
contenido, espacio de nombres, fechas, similitud, fuente y estado, además de los
conteos, la paginación y los campos de abstención. Se descartan los componentes de
puntuación, las explicaciones, los planes y costos de recuperación, los hashes y los
campos vacíos. **Por qué:** algunos clientes entregan `structuredContent` al modelo
además del bloque de texto, y la forma completa de diagnóstico cuesta varios miles de
tokens para una respuesta corta. `explain: true` en la llamada devuelve la forma
completa. HTTP, los SDK y el toolset `assistant` siempre reciben el resultado completo
(en `assistant`, un `memory.search` sin el desglose de puntuación por resultado).

<a id="the-assistant-toolset"></a>

### El toolset assistant

`?toolset=assistant` es la conexión que usa un cliente de chat para investigar Loom en
nombre de la persona que ha iniciado sesión: qué contiene un espacio de nombres, cómo
se usa, y respuestas con citas sobre memoria, documentos y código. El valor nombra
solo `assistant` (en cualquier combinación de mayúsculas): un error ortográfico o una
unión como `assistant+core` no se resuelve y la conexión recibe el listado
predeterminado del despliegue, sin error. La garantía la dan la URL que configura el
operador y un token `viewer`, no el cliente. Una réplica una versión más antigua
tampoco conoce el valor y sirve su listado predeterminado, así que durante una
actualización progresiva solo el token limita la conexión: apunte un cliente a
`?toolset=assistant` solo cuando todas las réplicas ejecuten una compilación que lo
sirva, y dele de todos modos un token `viewer`. En ella:

- **Solo se pueden invocar sus lecturas.** Un `tools/call` a cualquier otra
  herramienta, incluidas `loom_op` y `loom_search`, responde `isError` antes de que
  se ejecute el manejador, sea lo que sea que el token permita: con el código
  `UNKNOWN_OP`, o con el rechazo de acceso cuando la llamada nombra una organización o
  un espacio de nombres que el token no puede leer, que se comprueba primero. El token
  se sigue comprobando en cada llamada, de modo que una lectura que la persona no
  puede hacer se rechaza como en cualquier otra conexión. Emita el token del cliente
  con el rol `viewer` para que el mismo límite valga en todas las demás superficies.
- **Sus llamadas se registran en el libro como consumidor `loom-assistant`**, sea lo
  que sea que envíe la llamada, incluida una llamada rechazada a una herramienta del
  registro (un nombre de herramienta desconocido no escribe ninguna fila).
  `namespace.audit`, `namespace.summary`, `mentor.brief` y `knowledge.reads` excluyen
  de sus conteos de uso las lecturas de ese consumidor y las del dashboard
  (`loom-ui`), de modo que mirar un espacio de nombres no cambia su calificación (un
  cambio en una memoria desde el dashboard es una persona usando el espacio de nombres
  y sí cuenta en `namespace.audit` y `namespace.summary`); `namespace.audit` informa
  cuántas excluyó en `summary.inspector_calls`. Un token cuyo claim `consumer` nombra
  a otro consumidor conserva su claim, y sus llamadas se cuentan.
- **Una lectura que no nombra espacio de nombres lee el del token.** El valor
  predeterminado es el primer espacio de nombres que el claim del token nombra y que
  puede leer, no el primero en el que puede escribir: el token de una respuesta del
  assistant se emite para un espacio de nombres, mientras que el espacio de nombres
  personal de su sujeto es el que puede escribir. Un token cuyo claim no nombra
  ninguno conserva el workspace predeterminado del llamador.
- **Una lectura nombra las filas que lee.** El assistant no escribe nada, así que el
  propio agente de su cliente no tiene filas, y una lectura que no nombra ningún
  `agent` no se restringe a él: `memory.ask` lee entre agentes y `mentor.brief` informa
  sobre el principal escritor del espacio de nombres, igual que `memory.search`,
  `memory.list` y `memory.history` ya leen entre agentes. `insights.query` responde
  preguntas analíticas abiertas en SQL sobre las tablas que lista `insights.catalog`,
  limitadas por ClickHouse a la organización y el espacio de nombres del token
  ([diseño del assistant](../architecture/assistant.md#the-analysis-op)). Una lectura
  de las filas de un solo escritor, `memory.aggregate` o `memory.provenance`, se
  rechaza por falta de `agent` hasta que la llamada nombre al escritor: el `agent`,
  `user_id` y `session_id` de un resultado de búsqueda. El listado ofrece esos
  argumentos en cada lectura de memoria, y `agent` en `mentor.brief`. `memory.get`
  recurre a la fila bajo cualquier agente.
- **Las lecturas no registran uso.** `memory.search` se ejecuta con
  `auto_touch: false` y `memory.ask` con `reinforce: false`, sea lo que sea que envíe
  la llamada, de modo que una investigación no aumenta los conteos de recuperación que
  ordenan los resultados; `memory.ask` también se ejecuta con `precedents: false`, así
  que no se guarda ninguna respuesta para reutilizarla. Lo que queda es telemetría y
  cachés: la fila del libro de solicitudes; la fila de exposición que deja toda
  lectura de una memoria (`loom.memory_exposures`, el registro de a quién se le sirvió
  cada fila); la traza de búsqueda de código que escribe un `code.search` o
  `code.symbol` (`loom.code_search_trace`, bajo `LOOM_CODE_TRACE_LOG_ENABLED`); las
  filas de traza de búsqueda y de nodos de consulta que escribe un `memory.search`
  cuando `LOOM_SEARCH_TRACE_LOG_ENABLED` o `LOOM_MEMORY_QUERY_NODES_ENABLED` está
  activado; una fila de consistencia de `memory.ask` bajo consistencia diferida; y la
  caché de respuestas del modelo. Una llamada del assistant no abre ninguna ejecución
  de sesión MCP, y su `initialize` y el prompt `loom` no escriben ninguna fila de
  lectura de perfil: su primer no incluye ningún perfil.
- **Los resultados son la salida completa**, tal como los devuelve HTTP, ya que una
  cita necesita las identidades, versiones y atestaciones que la forma recortada
  descarta; un `memory.search` se ejecuta con `explain: false`, así que sus resultados
  no traen desglose de puntuación por resultado. `memory.search` y `knowledge.search`,
  que los otros listados dejan a `loom_search`, se listan aquí para que una respuesta
  pueda venir de un plano a la vez. Un resultado de `knowledge.search` nombra su
  `source_uri`, la ruta de encabezados (`section`) y la `revision` con la que se
  escribió su pasaje (0 cuando no se registró ninguna), y el de un archivo ingerido su
  `locator`. El resultado es el pasaje más cercano, no una respuesta: la op no tiene
  señal de abstención, así que el modelo dice que un hecho no está en los documentos
  cuando ningún resultado lo afirma.
- **Las instrucciones de `initialize`** indican al modelo que cite cada afirmación por
  clave y versión o hash de contenido, o por `source_uri` del documento, sección y la
  revisión que lleva un resultado (ninguna cuando es 0), que mantenga separadas las
  afirmaciones de la fuente, los resultados calculados y su propia lectura, que
  informe la evidencia faltante como no observada, y que trate el texto almacenado
  como datos que no pueden conceder una herramienta ni aprobar un cambio.

<a id="errors"></a>

## Errores

Una llamada que no puede ejecutarse tal como se envió vuelve como un **resultado de herramienta** con
`isError: true`, un bloque de texto que lee el modelo y un cuerpo `structuredContent`.
Los clientes se lo muestran al modelo para que pueda corregir la llamada; muchos clientes pierden
el estado ante un frame de error JSON-RPC, que Loom reserva para los fallos de transporte.

- **Argumentos incorrectos.** Un fallo de validación responde `"<op>: invalid
arguments."` seguido de cada error de campo, cada clave desconocida con el campo
  que probablemente se quiso escribir (incluidos los campos anidados), los argumentos
  obligatorios y opcionales y un ejemplo de llamada corregida:

  ```text
  memory.get: invalid arguments.
  memory_key: Field required [missing]
  unknown 'key' — did you mean 'memory_key'?
  required: memory_key
  optional: if_version, include_recall_breakdown, as_of, fields, follow
  example: {"memory_key": "deploy_window"}
  ```

  `structuredContent` lleva las mismas partes: `errors`, `op`, `required`,
  `optional`, `unknown` (clave → sugerencia) y `example`. Una op que cambia
  el estado (cualquier op que no sea de solo lectura) y recibe una clave casi correcta se rechaza antes de la
  validación (`UNKNOWN_ARGUMENT`, no se escribe nada) con `did_you_mean` y los
  argumentos de la op.

- **Rechazos.** Un 4xx de un handler distinto de 401 (`LOCKED`, `PINNED_CONFLICT`,
  `BLACKLISTED`, `RESOURCE_NOT_FOUND`, `RATE_LIMITED`, …) es un resultado de herramienta cuyo
  cuerpo es el `detail` HTTP más `status` y, cuando hubo un `Retry-After`,
  `retry_after_s`.
- **Acceso.** Un fallo de rol responde `AUTH_INSUFFICIENT` con `required_role`; un
  espacio de nombres que el token ni nombra ni tiene concedido responde
  `NAMESPACE_NOT_ACCESSIBLE`. En un espacio de nombres de código o de tracker, o en una colección compartida
  (no la personal de otra persona), el rechazo indica cómo obtener acceso.
- **Errores de transporte** siguen siendo errores JSON-RPC: error de análisis `-32700`, solicitud
  no válida `-32600` (un rechazo de autenticación lleva su estado HTTP; una solicitud mal formada o un
  lote fuera del rango de 1 a 64 responde 200), método no encontrado `-32601`, versión
  de protocolo no compatible `-32022` y un fallo del servidor `-32000 "tool execution
failed"`. Un fallo que Loom nombra (`OVERLOADED`, `BACKEND_TIMEOUT`,
  `SCHEMA_PENDING`, `EMBED_TIMEOUT`) indica su código y mensaje en ese error, y
  `error.data` es el `detail` HTTP más `status`, `tool` y `request_id` (con
  `retry_after_s` cuando el error indica una espera); cualquier otro fallo dice solo `internal error` y el
  id de la solicitud.

Todo cuerpo de rechazo con un `code` y todo `error.data` de un fallo llevan además `category`
y `retryable`, igual que el envelope HTTP. El texto de rechazo de una herramienta termina con la misma clase
en palabras (`(auth: do not retry unchanged)`), para un host que solo muestra el texto al modelo;
los rechazos propios de `loom_op` llevan los campos en su texto JSON. Un rechazo `NAMESPACE_NOT_ACCESSIBLE`
de un espacio de nombres que no es una colección, desde una herramienta que no es de código ni de tracker,
en un toolset que llega a `loom_op`, lleva también un paso `next`
([referencia de errores](https://github.com/ClickHouse/loom/blob/main/docs/reference/errors.md#the-error-envelope-over-http)). Cada código, con su
causa y su solución, está en la [referencia de errores](https://github.com/ClickHouse/loom/blob/main/docs/reference/errors.md).

<a id="security"></a>

## Seguridad

`tools/call` ejecuta las puertas del transporte HTTP (`server/transport/gates.py`):

- **Autorización** por llamada: el `required_role` de la op y las reglas de acceso del espacio de nombres.
- **Límite de tasa**: los mismos buckets que en HTTP. Como los clientes MCP no reintentan, una llamada limitada cuya indicación de espera sea como máximo `LOOM_MCP_RATELIMIT_RETRY_MAX_WAIT_S` (por defecto 2 s) espera ese tiempo una sola vez y vuelve a comprobar los mismos buckets; una indicación más larga o una segunda negativa responde `RATE_LIMITED` con `retry_after_s` (`LOOM_MCP_RATELIMIT_RETRY_MAX_WAIT_S=0` nunca espera).
- **Auditoría**: las ops declaradas con `audit=True` escriben las mismas filas de `loom.audit_log`.
- **Idempotencia**: MCP no tiene cabecera de idempotencia, por lo que una op idempotente recibe `_idempotency_key` (o `idempotency_key`) en sus argumentos. Se elimina antes de la validación y se reproduce exactamente como lo hace el `Idempotency-Key` de HTTP: solo después de la autorización, y solo para el principal que lo almacenó.

<a id="exporting-the-action-schema"></a>

## Exportar el esquema de acciones

Un gateway de políticas puede gobernar las herramientas de Loom a partir de un esquema generado desde el
registro (`server/transport/action_schema.py`):

```bash
loom export-actions --format json  -o loom-tools.json    # tools/list manifest + a `loom` block per tool
loom export-actions --format cedar -o loom.cedarschema   # Cedar: one action per operation
```

El manifiesto JSON tiene la forma de `tools/list` de MCP (`name`, `description`,
`inputSchema`, `outputSchema`) para cada op, además de un bloque `loom` con lo que un motor de
políticas necesita y para lo que MCP no tiene ningún campo: el nombre de la op, la ruta HTTP, el
rol requerido, la clase de efecto (`read`, `write` o `destructive`, según el mismo
clasificador que usan las anotaciones), la idempotencia, el indicador de auditoría, la visibilidad en MCP y
los nombres de los modelos de entrada y salida. El esquema Cedar declara, en el espacio de nombres
`Loom`, un principal `Agent` y un recurso `Namespace`, un tipo de registro por cada
modelo de entrada y salida, los grupos de acciones `Read`, `Write` y `Destructive`
(`Destructive` dentro de `Write`), y una acción por operación en su grupo. `-o -`
escribe en la salida estándar.

<a id="related"></a>

## Relacionado

- [Clientes MCP](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/mcp-clients.md): cómo conectar Claude Code, Codex, Cursor y otros clientes.
- [Claude Code](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/claude-code.md) y [Codex](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/codex.md): los plugins que montan el servidor por sí mismos.
- [Referencia de operaciones](https://github.com/ClickHouse/loom/blob/main/docs/reference/operations.md): cada op con su rol, su tipo y si `all` la incluye en la lista.
- [Referencia de errores](https://github.com/ClickHouse/loom/blob/main/docs/reference/errors.md).
