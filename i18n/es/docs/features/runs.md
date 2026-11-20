<!-- i18n: source=docs/features/runs.md blob=7aa894eee29c65b040ebfbad315df18b437034ae model=claude-sonnet-5-5 -->
> Esta página es una traducción automática de la [página en inglés](https://github.com/ClickHouse/loom/blob/main/docs/features/runs.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/features/runs.md), which is authoritative.

<a id="runs"></a>

# Ejecuciones

Una ejecución (run) es una ejecución de una tarea, como "triage issue 1234". `run.begin` la abre y devuelve un `run_id`; el
agente pasa ese id en sus escrituras (`memory.set`, `memory.update`, `observe.record`); `run.complete` la cierra con
un estado, las memorias en las que se apoyó el agente y, opcionalmente, una reflexión. Un `run_id` en el cuerpo de una lectura (`memory.search`,
`memory.ask`) filtra por las filas propias de la ejecución, por lo que las lecturas se etiquetan de otra forma: mediante la cabecera `X-Loom-Run-Id` en HTTP
(`run_scope` en el SDK de Python) y mediante la propia ejecución de la sesión en MCP. Una memoria en la que se apoyó una ejecución exitosa
se posiciona más alto la próxima vez; una en la que se apoyó una ejecución fallida pierde el impulso que había ganado.

<a id="what-a-run-records"></a>

## Qué registra una ejecución

Una fila de ejecución vive en `loom.agent_runs`. La mayor parte de lo que muestra `run.get` se deriva en el momento de la lectura a partir de los otros planos:

| Campo                              | Almacenado o derivado | Fuente                                                                                                                                                  |
| ---------------------------------- | --------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `goal`, `user_request`, `model`, … | almacenado            | `run.begin`                                                                                                                                             |
| `status`, `final_response`         | almacenado            | `run.complete` (`active` hasta entonces)                                                                                                                |
| `memories_used`                    | almacenado            | `run.complete`: la propia declaración del agente sobre en qué se apoyó                                                                                  |
| `grade`, `grade_source`, …         | almacenado            | `run.grade`, después de que la ejecución se cerró                                                                                                       |
| `memories_written`                 | derivado              | filas de `loom.memory` que llevan el `run_id` de la ejecución                                                                                           |
| `recalled`, `recalled_keys`        | derivado              | el registro de trazas de búsqueda bajo el `run_id`, unido con `run.complete(recalled_keys)`                                                             |
| `observations`                     | derivado              | filas episódicas de `observe.record` etiquetadas con el `run_id`                                                                                        |
| `turns`, `cost_usd`                | derivado              | los turnos de la sesión en `loom.agent_turn_usage` realizados mientras la ejecución estaba abierta (escritos por el plugin de Claude Code, o por un agente con `cost.record_turns`) |
| `budget_usd`, `budget_seconds`     | almacenado            | `run.begin` (0 = sin presupuesto)                                                                                                                       |
| `elapsed_seconds`, `over_budget`   | derivado              | desde el inicio de la ejecución hasta su cierre (o hasta ahora), según el reloj del servidor, y `cost_usd`, frente al presupuesto                       |

`recalled` es `-1` cuando `LOOM_SEARCH_TRACE_LOG_ENABLED` está desactivado y no se enviaron `recalled_keys`.

<a id="budgets"></a>

## Presupuestos

`run.begin` acepta un `budget_usd` opcional (gasto en modelos) y un `budget_seconds` opcional (cuánto tiempo puede tardar la ejecución). `run.get` los compara con `cost_usd` y `elapsed_seconds` y enumera en `over_budget` los que la ejecución ha superado (`cost`, `time` o ambos). Loom no ejecuta el agente, así que un presupuesto es una señal para quien lo ejecuta, nunca una detención.

El gasto de una ejecución son los turnos de su sesión realizados mientras la ejecución estaba abierta, según la hora propia de cada turno (`turn_at`, tomada de la transcripción), por lo que `budget_usd` requiere un `session_id` (de lo contrario, un 422). Se guarda con 6 decimales, igual que el costo, así que el valor mínimo es $0.000001. El tiempo transcurre desde el inicio de la ejecución hasta su cierre según el reloj del servidor, sea cual sea el `duration_ms` que haya informado el cliente. Define el presupuesto en el primer `run.begin` de la ejecución.

El primer `run.get` de una sesión que encuentra una ejecución activa por encima del presupuesto incluye además la sugerencia del mentor `run_over_budget`, que los plugins entregan al modelo como consejo. Una ejecución cerrada sigue informando `over_budget`, pero no recibe la sugerencia. Como toda sugerencia del mentor, se dice una sola vez por sesión, y un llamador que no envía sesión (el SDK, HTTP plano) comparte una por espacio de nombres y proceso web, así que decide según `over_budget`, no según la sugerencia.

Límites:

- **Solo entre prompts.** El plugin de Claude Code envía los turnos de un prompt cuando este termina (un agente puede enviar los suyos con `cost.record_turns`), por lo que un presupuesto de costo se comprueba entre prompts, y un único prompt headless (`claude -p`) muestra su gasto solo después de terminar. Esos turnos se cuentan igualmente aunque lleguen después del `run.complete` del propio agente. El costo de una llamada solo se conoce después de que esta devuelve, así que una ejecución puede superar `budget_usd` por el costo de su último turno antes de que nada lo detecte.
- **El reloj del cliente.** La ventana compara las horas de los turnos del cliente con las horas de ejecución del servidor; un reloj desfasado unos minutos coloca mal los turnos en los bordes de una ejecución. Un turno conserva la hora con la que se envió, así que un turno reenviado permanece en la ejecución en la que cayó la primera vez. Uno enviado sin hora (o con una que no se pueda interpretar) toma la hora con la que se registra su `message_id`, o la actual si es nuevo; un reenvío a una fracción de segundo del primero aún puede quedar fechado con la hora actual. `cost.record_turns` responde con `turns_dated_ahead`, los turnos que recibió con fecha más de 5 s posterior al reloj del servidor, de modo que un llamador que envía turnos por su cuenta pueda detectar un reloj adelantado; el plugin de Claude Code los envía sin leer la respuesta.
- **Una sesión, varias ejecuciones.** Las ejecuciones abiertas a la vez en una misma sesión ven los mismos turnos: una ejecución anidada cuenta para ambas, y los workers concurrentes que quieran cada uno un presupuesto de costo necesitan sus propias sesiones.
- **Modelos sin precio.** Un turno cuyo modelo no figura en la tabla de precios cuesta 0; `run.get` los cuenta en `unpriced_turns` y no cuentan para `budget_usd`.
- **MCP.** Las herramientas MCP ofrecen solo `budget_seconds`, ya que el modelo no conoce el id de sesión con el que se registran sus turnos.
- **Reintentos.** Un `run.begin` reenviado reemplaza la fila de la ejecución como cualquier reintento: conserva la sesión y el presupuesto que no se vuelven a indicar, un nuevo presupuesto distinto de cero reemplaza al anterior, y 0 lo mantiene.
  Un nuevo `budget_usd` debe enviarse con su `session_id` (de lo contrario, un 422); un nuevo `budget_seconds` no necesita nada más.
  Un begin reenviado que mueve la ejecución a otra sesión elimina su presupuesto de costo, que se definió para los turnos de la sesión anterior, conserva su presupuesto de tiempo y lo indica en su `next`. Esa es la única forma de quitar un presupuesto.
  Cuando `run.begin` respondió con un `run_id` distinto del enviado, reenvía ese `run_id`, o el mismo `session_id`; de lo contrario, el reintento abre una nueva ejecución.

<a id="recalled-is-not-relied-on"></a>

## Recuperado no es lo mismo que utilizado

Dos listas viajan en `run.complete`:

- **`memories_used`** — las claves en las que el agente se apoyó realmente. Cada una recibe un evento de uso `used`, más un evento `evidence` si el resultado es positivo o un evento `harm` si es negativo. `outcome` toma su valor por defecto de `status`: `completed` es positivo, `failed` es negativo y `cancelled` es ninguno.
- **`recalled_keys`** — las claves que las herramientas de Loom devolvieron durante la ejecución (los plugins de Claude Code y Codex las guardan a partir de los resultados de `loom_search` y `memory.get` y las envían al final de la sesión). Se registran para `run.get` y nunca se acreditan.

Una fila que se devolvió pero se ignoró no recibe nada; acreditar cada resultado de búsqueda premiaría lo que quede primero en el ranking.
El worker `usage_fold` integra los eventos en el término de refuerzo por el que ordena la [búsqueda de memoria](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/search-and-ask.md). Un evento `harm` retira el impulso ganado, con un mínimo igual a la base de la fila en el momento de escritura, de modo que una mala ejecución nunca empuja una fila por debajo de su relevancia y nunca la elimina.

Una clave de procedimiento mencionada en `memories_used` también obtiene una fila en el registro de procedimientos (ruta vacía, `ok` según el resultado); un agente que llama a `procedure.record_run` por su cuenta debe dejar esa clave fuera de `memories_used` para que la ejecución no se cuente dos veces. Consulta
[procedimientos y aprendizajes](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/procedures-and-learnings.md).

<a id="reflections"></a>

## Reflexiones

`run.complete(reflection=...)` registra `what_worked`, `what_failed`, `assumptions`, `lessons`, `improvements` (hasta
20 elementos cada uno), un `confidence` y las claves de `evidence` en las que se apoya la lección. Se almacena como una memoria de tipo
`reflection` bajo `reflection:<run_id>`:

- se genera su embedding, de modo que una ejecución posterior puede encontrarla con `memory.search(memory_type="reflection")`,
  pero queda fuera de la búsqueda sin filtros y de `memory.ask`;
- `what_failed` y `lessons` necesitan al menos una clave de `evidence` (de lo contrario, se devuelve un 422);
- caduca tras `expires_in_days` (90 por defecto, `0` = nunca) a menos que `memory.verify`
  o un resultado positivo la renueve;
- una ejecución abierta con `role` (planner, executor, critic, o una palabra propia) etiqueta su
  reflexión con `role:<role>`, de modo que el `memory.search(role=...)` de un agente con el mismo rol la prioriza.

<a id="what-happens-to-the-rows-a-run-wrote"></a>

## Qué ocurre con las filas que escribió una ejecución

`retain` en `run.complete` decide el destino de las memorias escritas durante la ejecución:

| `retain`         | Efecto                                                                                                                                        |
| ---------------- | --------------------------------------------------------------------------------------------------------------------------------------------- |
| `keep` (default) | No cambia nada.                                                                                                                               |
| `demote`         | Su confianza se reduce de inmediato.                                                                                                          |
| `expire`         | Se establece una expiración a `LOOM_MEMORY_RUN_COMPLETE_EXPIRE_DAYS` días (por defecto 7) con `on_expire=demote`; el worker `memory_expiry_sweep` la aplica. |

Las filas fijadas, verificadas, promovidas, bloqueadas, episódicas y de reflexión se conservan sea cual sea el
modo, y ningún modo marca nada como eliminado. El valor por defecto proviene de
`LOOM_MEMORY_RUN_COMPLETE_RETAIN`; la ejecución implícita de una sesión MCP siempre conserva, ya que
el transporte que la cierra no puede juzgar el trabajo.

<a id="graded-runs"></a>

## Ejecuciones calificadas

`status` indica que la ejecución terminó; una calificación indica si su resultado se sostuvo. A menudo la calificación llega días después, cuando CI
pasa o un revisor rechaza el pull request. `run.grade` recibe `grade` (`pass` o `fail`), un `source` (`maintainer`,
`ci`, `reviewer`, … texto libre), un `reason` y un `ref` (una URL, una clave de hallazgo, el id de una ejecución de
verificación), dirigidos a un `run_id` o a todas las ejecuciones cerradas de un `session_id` (por ejemplo
`pr_1234`). Una recalificación reemplaza el veredicto; el mismo veredicto de la misma fuente no
cambia nada. Las ejecuciones activas se omiten.

Cada memoria en `memories_used` de una ejecución calificada se cuenta frente a la calificación. Esos
recuentos son lo que `memory.proposals` muestra junto a cada dato a prueba (una
fila de `memory.set(proposed=true)`). Loom decide una propuesta a partir de ellos solo cuando el
propietario del espacio de nombres configuró `proposal_policy.mode` como `auto`; cualquier escritor decide con
`memory.proposal_decide` en cualquiera de los dos modos. Consulta [escritura de memoria](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/writing.md).

`run.list(grade="ungraded")` lista las ejecuciones cerradas a las que aún se les debe un veredicto.

Una sesión MCP obtiene una ejecución implícita sin pedirla: su primera llamada a una herramienta
con alcance de ejecución inicia una, y `DELETE /mcp` o el worker `mcp_session_run_sweep` la cierra
(consulta [MCP](https://github.com/ClickHouse/loom/blob/main/docs/reference/mcp.md)). El barrido cierra una ejecución activa con
`environment='mcp'` y `metadata.implicit` en true cuyo id es `mcp-` más 40 caracteres
hexadecimales (la forma que solo produce el transporte). La cierra cuando ninguna llamada de
su token (de cualquiera, si la ejecución no registra token) la alcanzó dentro de
`LOOM_MCP_SESSION_RUN_IDLE_SECONDS`. La ejecución reside en el espacio de nombres predeterminado del llamador, de modo que lo que ven sus lectores se queda allí: su objetivo es la etiqueta
`mcp session (<agent>)`, nunca el texto de una llamada (la consulta de una lectura puede alcanzar cualquier
espacio de nombres que lea el llamador), y `memories_written` lista únicamente las escrituras en ese
espacio de nombres (las demás siguen llevando su `run_id`). Cerrarla no juzga nada:
su resultado es `none` y sus memorias se conservan sea cual sea el modo de retención predeterminado del servidor, de modo que dos cierres que compiten por ella (el barrido en dos hosts, un `DELETE`)
a lo sumo escriben dos veces la misma finalización.

Cómo la ejecución abarca varios procesos web. Su id es un hash de la organización del llamador, del
sujeto del token y del `Mcp-Session-Id`, de modo que todos los procesos del servidor nombran
la misma ejecución para la sesión y ningún otro token puede nombrarla enviando el mismo
id de sesión. Un proceso registra la ejecución en la primera llamada admitida con alcance de ejecución de la sesión y escribe la fila de inicio en segundo plano (un insert con espera
retendría esa llamada durante segundos en ClickHouse Cloud); una llamada que las compuertas rechazan
no escribe ninguna fila de ejecución. Dos procesos que no encuentran ninguna fila escriben cada uno un primer inicio,
y gana el inicio más temprano y su propietario (la versión de fila de un primer inicio cuenta hacia atrás
desde su inicio; toda escritura posterior la reemplaza). Cada proceso comprueba una ejecución en caché contra el registro como máximo una vez
por minuto y la reabre cuando un `DELETE` o el barrido la cerró entretanto.
Un `DELETE` cierra la ejecución en cualquier proceso al que llegue, por su id; el cierre
cuenta todas las escrituras bajo ese id, sin importar quién la cierre. Un `DELETE` que llega a
otro proceso antes de que la fila de inicio haya aterrizado no encuentra nada que cerrar,
y el inicio aterriza entonces como una ejecución abierta que el barrido cierra cuando queda inactiva: un reintento corto no cubriría un inicio que tarda segundos, y retrasaría el `DELETE`
de toda sesión que nunca tomó una ejecución. Una ejecución reabierta conserva su `started_at` original, de modo que una ejecución cerrada por el barrido y reabierta más tarde informa un
`duration_ms` contado desde la primera llamada de la sesión, huecos incluidos. Una comprobación que
no encuentra ninguna fila, porque un borrado por erasure la eliminó, inicia una nueva ejecución con inicio en ese momento,
de modo que no vuelve ninguna ejecución con fecha anterior al borrado.

<a id="operations"></a>

## Operaciones

| Operación      | Rol    | Qué hace                                                                                                                                                                                                                                                                                                                         |
| -------------- | ------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `run.begin`    | writer | Abre una ejecución: `goal` (obligatorio), `user_request`, `expected_outcome`, `run_id` (vacío o `-`: se genera uno), `session_id`, `role`, `model`, `metadata`. Reenviar el mismo `run_id` reemplaza la fila; un id con la forma del transporte MCP (`mcp-` y 40 dígitos hexadecimales) recibe un id propio. |
| `run.complete` | writer | La cierra una sola vez: `status`, `memories_used`, `recalled_keys`, `outcome`, `reflection`, `retain`. No es idempotente: completarla dos veces añade los eventos de uso dos veces.                                                                                                                                              |
| `run.get`      | viewer | Una ejecución con sus derivaciones vigentes.                                                                                                                                                                                                                                                                                     |
| `run.list`     | viewer | Una página de ejecuciones, filtradas por `agent`, `session_id`, `status`, `grade`, `q` y `memory_activity` (`with` conserva las ejecuciones que se apoyaron en una memoria o escribieron una, `without` las vacías; los filtros se aplican antes de `limit`).                                                                 |
| `run.grade`    | writer | Califica una ejecución cerrada, o las ejecuciones de una sesión, como `pass` / `fail`.                                                                                                                                                                                                                                           |

`run.list` pagina con `order_by` (hora de inicio, duración o memorias usadas, cada una
ascendente o descendente; por defecto `started_desc`, las más recientes primero) y `offset` (como máximo
100,000). `q` conserva las ejecuciones cuyo objetivo, id de ejecución o agente contiene el texto, sin distinguir mayúsculas de minúsculas. `total`
cuenta todas las ejecuciones que coinciden con los filtros, y es null en una página completa posterior a la primera, donde
la página por sí sola no puede determinarlo.

Las listas completas de campos están en la [referencia de operaciones](https://github.com/ClickHouse/loom/blob/main/docs/reference/operations.md#run).
`memory.outcome_run` registra un resultado helped / hurt para cada memoria recuperada durante una
ejecución, para los llamadores que califican en bloque en lugar de nombrar claves.

<a id="example"></a>

## Ejemplo

El `run.scope` del SDK de Python abre la ejecución al entrar y la cierra al salir. Las claves que una respuesta de
`memory.ask` citó dentro del bloque, más las que se pasen a `used()`, pasan a ser
`memories_used`; una excepción cierra la ejecución como `failed` (resultado negativo) y luego
se propaga. Las escrituras dentro del bloque llevan el id de la ejecución.

```python
from loom import Agent

async with Agent(org="acme", namespace="support", agent="triage-bot") as agent:
    async with agent.run.scope(goal="Answer ticket 4411: refund policy for annual plans") as run:
        answer = await agent.memory.ask("what is the refund policy for annual plans?")
        run.used("policy/refunds/annual")  # relied on beyond what the answer cited
        run.final_response = answer["answer"]

    replay = await agent.run.get(run.run_id)
    print(replay["run"]["status"], replay["memories_written"], replay["recalled"])

    # Later, once the customer confirmed the answer was right:
    await agent.run.grade("pass", run_id=run.run_id, source="support-lead")
```

Por HTTP, el ciclo de vida son dos llamadas:

```bash
curl -sX POST "$LOOM_BASE_URL/v1/run.begin" -H "Authorization: Bearer $LOOM_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"org":"acme","namespace":"support","agent":"triage-bot","goal":"Answer ticket 4411"}'
# → {"run_id":"…","status":"active","started_at":"…","next":"…"}

curl -sX POST "$LOOM_BASE_URL/v1/run.complete" -H "Authorization: Bearer $LOOM_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"org":"acme","namespace":"support","run_id":"…","status":"completed",
       "memories_used":["policy/refunds/annual"]}'
```

`Agent(auto_run=True)` (o `LOOM_AUTO_RUN=on` en el entorno del cliente) abre una ejecución
de forma diferida en la primera llamada al plano de memoria, hace pasar por ella todas las llamadas posteriores y
la completa cuando el `Agent` se cierra, con resultado `none`, de modo que nunca acredita ni penaliza nada. Está desactivado por
defecto porque una escritura bajo una ejecución queda limitada a ella: la reformulación de una fila de otra ejecución se almacena como `duplicate`
en lugar de fusionarse.

<a id="settings"></a>

## Configuración

| Configuración                                                 | Efecto                                                                                                                |
| ------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------- |
| `LOOM_MEMORY_RUN_COMPLETE_RETAIN`                             | El modo `retain` cuando una llamada no indica ninguno (`keep`).                                                       |
| `LOOM_MEMORY_RUN_COMPLETE_EXPIRE_DAYS`                        | Cuánto tiempo después `retain=expire` fija la caducidad (7 días).                                                     |
| `LOOM_SEARCH_TRACE_LOG_ENABLED`                               | Registra cada búsqueda bajo su `run_id`, del cual se deriva `recalled`. Desactivado por defecto porque registra el texto de las consultas. |
| `LOOM_USAGE_FOLD_ENABLED`                                     | El proceso en segundo plano `usage_fold`, que convierte los eventos de uso, evidencia y daño en ranking.              |
| `LOOM_MEMORY_SEARCH_REINFORCEMENT_WEIGHT`, `…_HALF_LIFE_DAYS` | Cuánto, y durante cuánto tiempo, la confianza en una fila la eleva en la búsqueda.                                    |

Consulta [configuración: deduplicación y caducidad](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md#dedup-and-expiry),
[procesos en segundo plano](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md#workers) y
[búsqueda de memoria](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md#memorysearch).

<a id="limits"></a>

## Límites

- `memories_used` y `recalled_keys` aceptan como máximo 500 claves cada uno; `goal` acepta como máximo 2,000
  caracteres.
- Una clave de `memories_used` que no se resuelve a una fila activa no suma nada;
  `used_applied` en la respuesta cuenta las que sí se resolvieron.
- `run.complete` no es idempotente. Si se reintenta una finalización, los eventos de uso se contabilizan dos veces;
  complétala una sola vez.
- El `run.scope` del SDK escribe el registro con el mejor esfuerzo: un `run.complete` fallido nunca
  oculta la excepción del propio bloque, por lo que una ejecución cuya finalización falló sigue en estado `active`.
- `turns` y `cost_usd` valen 0 cuando la ejecución no tiene `session_id` o el cliente no
  informa el uso de turnos. Solo cuentan los turnos realizados mientras la ejecución estaba abierta.

<a id="related"></a>

## Relacionado

- [Trazar y reanudar ejecuciones largas](https://github.com/ClickHouse/loom/blob/main/docs/guides/trace-and-resume.md) — checkpoints en la memoria de trabajo indexados por `run_id`, y cómo trazar lo que hizo una ejecución.
- [Buscar y preguntar](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/search-and-ask.md) — cómo entra el refuerzo en el ranking.
- [Tareas y decisiones](https://github.com/ClickHouse/loom/blob/main/docs/features/tasks-and-decisions.md) — `decision.outcome` acredita las memorias en las que se apoyó una ejecución que tomó una decisión.
- [Arquitectura de la memoria](https://github.com/ClickHouse/loom/blob/main/docs/architecture/memory-architecture.md) — el plegado de eventos de uso y las tablas que lo sustentan.
