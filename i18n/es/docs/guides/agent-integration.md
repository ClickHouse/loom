<!-- i18n: source=docs/guides/agent-integration.md blob=456e73fdf0db8acbbcc8465c94067cbbf581e556 model=claude-sonnet-5-5 -->
> Esta página es una traducción automática de la [página en inglés](https://github.com/ClickHouse/loom/blob/main/docs/guides/agent-integration.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/guides/agent-integration.md), which is authoritative.

<a id="integrate-loom-into-an-agent"></a>

# Integrar Loom en un agente

Esta guía cubre lo que un agente envía a Loom y cómo lee las respuestas: la superficie del cliente, la identidad, qué escribir y cómo leer. Da por supuestos los conceptos de [memoria](https://github.com/ClickHouse/loom/blob/main/docs/concepts/memory.md) y de [identidades y espacios de nombres](https://github.com/ClickHouse/loom/blob/main/docs/concepts/identities-and-namespaces.md). Un agente de programación con un plugin incluido ([Claude Code](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/claude-code.md), [Codex](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/codex.md)) obtiene la mayor parte de esto del plugin.

Las mismas reglas, escritas para el agente en lugar de para ti, son la Agent Skill `loom` (`skills/loom/`). Un agente sin plugin la lee desde el servidor: la operación experimental [`mentor.guide`](https://github.com/ClickHouse/loom/blob/main/docs/reference/operations/mentor.md#mentorguide) devuelve el bucle, dónde va cada tipo de estado y cómo leer las respuestas (`part: "skill"`), o bien todos los planos con sus operaciones (`part: "reference"`). Colócala en el prompt de sistema del agente:

```python
system_prompt = "You triage support tickets.\n\n" + await agent.guide()
```

Con MCP, la introducción indica la llamada; un cliente de Agent Skills la instala con `npx skills add ClickHouse/loom --skill loom`.

<a id="1-choose-the-client-surface"></a>

## 1. Elegir la superficie de cliente

| Superficie                                                   | Úsala cuando                                                                                                    | Qué obtienes                                                                                                                                                                                                      |
| ------------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **MCP** (`/mcp`)                                             | El agente es un cliente LLM que ya habla MCP (Claude Code, Codex, Cursor, una aplicación de escritorio).        | Un listado curado de herramientas por `?toolset=`, descripciones de herramientas escritas para el modelo, valores de identidad por defecto tomados de la conexión. El modelo decide cuándo llamar.                  |
| **SDK de Python** (`clickhouse-loom`, `from loom import Agent`) | El agente es tu propio proceso de Python.                                                                       | Clientes tipados por plano, reintentos y claves de idempotencia, `agent.tools()` para el bucle de herramientas de un modelo, ámbitos de ejecución, adaptadores de frameworks (LangGraph `LoomStore`, Strands `LoomMemoryStore`). |
| **SDK de TypeScript** (`@clickhouse/loom`)                   | El agente es un servicio de Node o del lado del navegador.                                                      | `Datapack` / `Agent`, `agent.call(op, body)` tipado a partir de la tabla de ops generada, `AgentTools`, `Session`.                                                                                                |
| **HTTP** (`POST /v1/<op>`)                                   | Cualquier otro lenguaje, o un script puntual.                                                                   | Todas las ops, con los mismos cuerpos JSON; tú gestionas los reintentos y el encabezado `Idempotency-Key`.                                                                                                        |

Las cuatro sirven las mismas ops desde un único registro. Elige según quién decide llamar: MCP cuando decide el
modelo, un SDK cuando decide tu código. Muchos agentes usan ambos: el SDK para cargar contexto al
inicio y registrar la ejecución al final, y las herramientas MCP para lo que el modelo elige recordar o
consultar. Con MCP, elige el listado con `?toolset=` ([toolsets de MCP](https://github.com/ClickHouse/loom/blob/main/docs/reference/mcp.md#toolsets));
una op que el listado omite sigue siendo invocable mediante `loom_op`.

<a id="2-choose-the-identity"></a>

## 2. Elegir la identidad

Cada fila lleva la tupla de identidad `(org, namespace, agent, user_id, session_id)`, además de
`run_id`. El token fija `org` y los espacios de nombres; el resto viaja en cada llamada.

| Campo        | Valor recomendado                                                 | Por qué importa                                                                                                 |
| ------------ | ----------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------- |
| `namespace`  | Uno por agente (`agent-<name>`), o uno compartido por un equipo de agentes | **La pertenencia es el límite de lectura**: quien puede leer el espacio de nombres lee todas sus filas, sin importar qué agente las escribió. |
| `agent`      | Un nombre estable para el agente que escribe                      | Indica autoría, no aislamiento. Por MCP el valor predeterminado es el nombre del cliente (`clientInfo.name`, en formato slug).            |
| `user_id`    | El usuario final al que atiende el agente, cuando atiende a varios | Con un token creado con `--read-scope own_user`, el prompt de un usuario no puede extraer las memorias de otro usuario.             |
| `session_id` | La conversación o el trabajo                                      | Delimita la memoria de trabajo y las lecturas `own_session`; agrupa una transcripción.                                             |
| `run_id`     | Una ejecución de una tarea                                        | Vincula las escrituras, búsquedas, checkpoints y la transcripción de una ejecución ([ejecuciones](https://github.com/ClickHouse/loom/blob/main/docs/features/runs.md)).               |

- **Un agente, varios usuarios, un token:** crea el token con `--read-scope own_user` (o fija
  `--read-user-id`) y pasa `user_id` en cada llamada. Una lectura con alcance restringido sin ese `user_id`
  responde 400 `IDENTITY_INCOMPLETE`; nunca se amplía.
- **Varios agentes que comparten conocimiento:** un espacio de nombres compartido que todos leen, y cada uno escribe bajo
  su propio `agent`; o un token que lee un espacio de nombres compartido y escribe en el suyo (`ns_write`).
  Consulta [acceso a espacios de nombres](https://github.com/ClickHouse/loom/blob/main/docs/guides/tokens-and-grants.md#namespace-access).
- **No pongas secretos ni identificadores personales en `agent`:** se registra en cada fila del ledger.

<a id="3-decide-what-to-write"></a>

## 3. Decide qué escribir

Elige primero el plano. La memoria duradera se incrusta, se ordena por relevancia, se comprueba en busca de conflictos y se versiona. La memoria de trabajo se lee por clave y nunca se ordena. Knowledge contiene documentos divididos en fragmentos.

| El agente tiene…                                                                                                         | Escribe con                                                                                         | No con                                   |
| ------------------------------------------------------------------------------------------------------------------------ | --------------------------------------------------------------------------------------------------- | ---------------------------------------- |
| Un hecho que seguirá siendo cierto la próxima semana y que otra sesión necesita (una decisión, una restricción, un responsable, una preferencia) | `memory.set`: un hecho por llamada, una clave por hecho                                             | memoria de trabajo                       |
| Su plan actual, supuestos abiertos, una lista de comprobación para esta tarea                                            | `working_memory.set` / `append`, cargada por `context.pack`                                         | `memory.set`                             |
| Una transcripción, un correo o un hilo que vale la pena recordar                                                         | `memory.set_from_messages`: el servidor extrae hechos con claves cortas                             | un único `memory.set` gigante            |
| Un documento (especificación, runbook, informe, cualquier cosa de más de unos 2 KB)                                      | `knowledge.upsert` como Markdown, más un párrafo con un puntero en `memory.set` bajo la misma clave | un `memory.set` que contenga el texto    |
| Una tabla de hechos con identidad (PR por número, servicios por id)                                                      | `memory_schema.register` una vez, luego `memory.record.write`                                       | una fila de memoria por lote             |
| Estado de coordinación (un lease, un indicador de congelación, «dónde estamos»)                                          | `working_memory.set` por `block_key`                                                                | `memory.set` + `memory.get` en un bucle  |
| Un punto de control desde el que reanudar                                                                                | un bloque de memoria de trabajo, o `context.offload` para un blob grande                            | una memoria duradera por paso            |
| Una conversación para mostrar y buscar más tarde                                                                         | `conversation.create` / `conversation.append`                                                       | memoria                                  |
| Razonamiento, saludos, salida intermedia de herramientas                                                                 | nada                                                                                                | cualquier cosa                           |

[Memoria de agente de investigación](https://github.com/ClickHouse/loom/blob/main/docs/guides/investigation-agent-memory.md) aplica esta tabla a un agente
que trabaja casos; [rastreo y reanudación](https://github.com/ClickHouse/loom/blob/main/docs/guides/trace-and-resume.md) cubre los puntos de control.

**Da forma a cada hecho para que una sesión posterior pueda confiar en él:**

- **Primero la afirmación.** El valor expresa el hecho en texto plano; la línea de la fuente va después. Solo el
  inicio de un valor se incrusta (el extracto se recorta a 1,200 caracteres).
- **Claves legibles**, con la forma `<scope>/<kind>/<id>`: `sre/case/1234/evidence/dns-sidecar`, no un
  hash. Las facetas van en `tags` (`case:1234`, `kind:evidence`) y en `entities`.
- **Una revisión reescribe la misma clave.** Loom conserva las versiones (`memory.history`) y sirve
  la actual; nunca crees `deploy_window_v2`, nunca mantengas una fila de índice que nombre la clave
  actual.
- **Indica de dónde viene:** `source_resource_id` para el archivo, la URL o el documento del que se
  leyó el hecho.
- **Nunca disfraces una inferencia como un hecho.** Escribe «Inferred from A and B: C» con
  `confidence` por debajo de 1.0.
- **Resuelve las fechas** («on <YYYY-MM-DD>», no «ayer») y establece `valid_at` con el momento en que se hizo
  la observación.
- **Sin secretos.** Para una clase de cosa que el usuario nunca quiere que se recuerde, usa `blacklist.add` con el
  patrón: las escrituras que coincidan se rechazan con 422 `BLACKLISTED`.

Una afirmación del usuario («desplegamos los martes») es el desencadenante para escribir. No busques
primero; el servidor se encarga de los duplicados y los conflictos.

<a id="4-read-the-writes-answer"></a>

## 4. Lee la respuesta de la escritura

Una respuesta de `memory.set` es un recibo. Léela:

| Campo                                     | Significado                                                                                                                                                      |
| ----------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `memory_id`, `version`, `content_sha256`  | La escritura se guardó. Sin ellos no se guardó; nunca le digas al usuario que algo se guardó si no fue así.                                                      |
| `conflict_verdict: conflict`, `peers`     | La escritura choca con un hecho existente (ambas filas quedan marcadas como `conflicted`). Muestra ambos valores y pregunta cuál es el vigente; no sobrescribas en silencio. |
| 409 `PINNED_CONFLICT`                     | Una persona fijó el hecho existente. Infórmalo; no reintentes con una clave nueva.                                                                               |
| `duplicate_of`, `merged_into_key`, `noop` | El hecho ya era conocido; no se guardó nada nuevo.                                                                                                               |
| `advisories`                              | Una advertencia sobre la forma del valor: base64, JSON con doble codificación, un valor que supera `LOOM_MEMORY_VALUE_ADVISORY_CHARS`.                           |

Pasa `if_match_sha256` (obtenido de un `memory.get`) cuando dos escritores puedan competir por una misma clave; un hash obsoleto se rechaza en lugar de sobrescribir.

<a id="5-read-with-one-call-per-question"></a>

## 5. Leer con una llamada por pregunta

1. **Carga el contexto una vez por tarea.** `context.pack` devuelve un paquete con presupuesto de tokens: secciones del perfil, memoria de trabajo, las memorias relevantes para `query`, procedimientos y, opcionalmente, conocimiento, conversaciones y ejecuciones recientes (`include_knowledge`, `include_conversations`, `include_runs`). Los plugins lo llaman al inicio de la sesión.

   ```python
   pack = await agent.context.pack("fix the flaky ingest test", token_budget=2000)
   system_prompt = base_prompt + "\n\n" + pack.rendered
   ```

2. **Busca una vez por pregunta.** Por MCP, `loom_search` cubre memoria, perfil, conocimiento y conversaciones en una sola llamada. En el SDK, usa `agent.memory.search(query)`, o `agent.memory.ask(question)` para obtener una respuesta con citas. Pagina con `next_page` solo cuando la respuesta necesite más filas (una lista, un conteo).
3. **Trata una abstención como una respuesta.** `abstain: true` significa que nada superó los umbrales de similitud; responde "no está en la memoria", no con el resultado débil más cercano. `abstain_code` indica el umbral ([códigos de abstención](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/search-and-ask.md)). Mientras `pending_extraction` sea mayor que 0, la extracción diferida no se ha ejecutado, por lo que una respuesta vacía no es evidencia de ausencia.
4. **Respeta la procedencia.** El `source_class` de un resultado indica cómo entró el contenido en la memoria y `verified` indica si lo cubre una atestación vigente. `memory.get` devuelve `verification` (`human`, `process`, `stale` cuando el contenido cambió después de la atestación, o `none`). `memory.ask` deja fuera de su síntesis, por defecto, las filas declaradas `external` y sin atestación.

```python
hits = await agent.memory.search("refund window for EU customers", top_k=5)
if hits.abstain:
    reply = "I don't have that in memory."
else:
    reply = hits[0].content_excerpt
```

<a id="6-record-runs"></a>

## 6. Registrar ejecuciones

Una ejecución (run) es una pasada de una tarea: su objetivo, lo que recuperó y en lo que se apoyó, lo que escribió y su resultado. Las ejecuciones registradas permiten que el ranking aprenda qué memorias ayudan ([ejecuciones](https://github.com/ClickHouse/loom/blob/main/docs/features/runs.md)).

```python
async with agent.run_scope(goal="Fix the flaky CI job") as run:
    answer = await agent.memory.ask("what broke CI last week?")
    run.used("ci/flaky/runner-image")
    run.final_response = "Pinned the runner image."
```

El scope llama a `run.begin` al entrar y a `run.complete` al salir, con el estado `completed` o `failed` y, como `memories_used`, las claves que citaron sus respuestas de `memory.ask` más las que se pasaron a `used()`. Las escrituras dentro del bloque llevan su `run_id`. Sin el SDK, llama tú mismo a `run.begin` y a `run.complete(run_id, status=..., memories_used=[...])`. `memories_used` es lo que el agente utilizó, no todo lo que se le mostró; las claves recuperadas se registran por separado.

Un agente que no use los plugins también debería entregar la transcripción terminada a `memory.set_from_messages` (o cerrar una `agent.session(...)`), para que el servidor extraiga lo que era duradero.

<a id="7-follow-the-memory-skills-review"></a>

## 7. Sigue la revisión de la skill de memoria

Los plugins incluyen una skill `loom-memory`
(`integrations/claude-code-plugin/skills/loom-memory/`, generada en el plugin de Codex)
que codifica esta guía para un modelo. Un agente de programación que use MCP sin plugin utiliza el bloque de [instrucciones para agentes](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/agent-instructions.md), cuya última línea sobre memoria es una versión resumida de esta revisión; para ejecutar la revisión completa, pega las preguntas siguientes debajo del bloque. Cualquier otro agente copia las preguntas en sus instrucciones. Antes de informar que terminó, escribe solo lo que merezca un sí:

1. ¿Tomé o aprendí una decisión, una restricción o un responsable que una sesión futura necesite?
2. ¿Alguna memoria existente resultó ser incorrecta o estar desactualizada? Actualízala, con la misma clave.
3. ¿El usuario me corrigió? Actualiza el dato; si lo rechazó, elimínalo con `memory.delete` y añade el patrón con `blacklist.add` si podría repetirse.
4. ¿Produje un artefacto (un archivo, un PR, un dashboard) que el trabajo futuro deba conocer?
5. ¿Descubrí algo poco evidente que costó un esfuerzo real?
6. ¿Una inferencia que escribí ahora está confirmada? Sube `confidence` y quita el prefijo "Inferred".

Después ejecuta `namespace.validate` y actúa sobre los hallazgos que nombren las claves que escribiste. Di "guardado" solo cuando el recibo incluyó `memory_id` y `version`, "verificado" solo cuando una persona o un proceso lo certificó, y "no verificado aquí" cuando la comprobación no pudo ejecutarse. Otras skills incluidas cubren las preguntas sobre código (`loom-code`, también en `skills/loom-code/` para cualquier cliente), la reorganización de un espacio de nombres (`loom-optimize`), la restauración de sesiones (`loom-session`) y el estado del cliente (`loom-status`).

<a id="mistakes-to-avoid"></a>

## Errores que debes evitar

`namespace.audit` detecta cada uno de estos; [optimizar un espacio de nombres](https://github.com/ClickHouse/loom/blob/main/docs/guides/optimize-namespace.md) convierte
sus hallazgos en una corrección.

| Hallazgo                              | El error                                                                                | En su lugar                                                                                     |
| ------------------------------------- | --------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------- |
| `double_encoded_values`               | `json.dumps(json.dumps(doc))`: el texto incrustado empieza con una comilla escapada.    | Envía texto, o el propio objeto, como `value`.                                                  |
| `json_prefix_excerpts`                | Un objeto JSON más largo que el recorte de embedding; el vector se construye a partir de su prefijo. | Primero declara la afirmación como texto plano; los documentos a `knowledge.upsert`. |
| `hash_keys`                           | Un sha como identidad de la fila: ilegible, no se puede agrupar, y una revisión se convierte en una clave nueva. | `<scope>/<kind>/<id>`; revisa bajo la misma clave.                       |
| `exact_key_store`                     | Las lecturas por clave exacta superan en número a las búsquedas: el espacio de nombres es un almacén de objetos. | Recupera con `memory.search`; leases y punteros en la memoria de trabajo. |
| `coordination_as_memory`              | Leases, locks y flags de congelación guardados como memorias y leídos en bucle.         | `working_memory.set` / `get`: nunca se incrustan, se ordenan ni se comprueban en busca de conflictos. |
| `hand_rolled_index_rows`              | Una fila que lista otras claves porque el agente no encuentra sus propias filas.        | `tags` + `memory.list prefix=`; encadena revisiones con `memory.link supersedes`.               |
| `oversized_values`, `unembedded_rows` | Documentos como memorias; por encima del límite del embedder un valor nunca se incrusta. | `knowledge.upsert` más una fila de puntero de un párrafo.                                      |
| `episodic_blobs`, `version_churn`     | Volcados de ejecución como filas episódicas, reescritos en cada paso.                   | `context.offload` para el blob; una fila episódica corta por ejecución; revisa la afirmación, no el blob. |

Dos más que ninguna auditoría detecta:

- **Un blob por turno.** Cinco hechos son cinco llamadas a `memory.set`, cada una bajo su propia clave.
- **Retirar claves que un agente activo todavía lee.** Sus lecturas responden `found: false`, un cliente puede
  recurrir al estado local, y dos almacenes se contradicen. Mueve con `memory.move` (deja
  una redirección) y no borres nada hasta que todos los lectores hayan migrado.

<a id="related"></a>

## Relacionado

- [Bots sin interfaz](https://github.com/ClickHouse/loom/blob/main/docs/guides/headless-bots.md)
- [Escritura de memoria](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/writing.md)
- [Búsqueda unificada](https://github.com/ClickHouse/loom/blob/main/docs/features/unified-search.md)
- [Marcos de trabajo](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/frameworks.md)
- [Clientes MCP](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/mcp-clients.md)
