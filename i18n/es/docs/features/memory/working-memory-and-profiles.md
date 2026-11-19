<!-- i18n: source=docs/features/memory/working-memory-and-profiles.md blob=bedd8394f653eb3b5cd589b4bd7aed2749be116b model=claude-sonnet-5-5 -->
> Esta página es una traducción automática de la [página en inglés](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/working-memory-and-profiles.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/working-memory-and-profiles.md), which is authoritative.

<a id="working-memory-and-profiles"></a>

# Memoria de trabajo y perfiles

Dos planos guardan texto que se entrega al agente en cada ocasión, en lugar de encontrarlo mediante búsqueda:

- **Memoria de trabajo**: bloques pequeños con nombre para leases, cursores, checkpoints y "dónde estaba". Los bloques no se incrustan, ni se ordenan, ni se buscan.
- **Perfiles**: contexto siempre cargado sobre el usuario o el agente (persona, preferencias, foco), renderizado como un único fragmento de prompt.

Usa `memory.set` para todo aquello que una sesión posterior deba encontrar al preguntar. Usa estos planos para lo que se le debe comunicar sin que lo pida. Una línea de bloc de notas escrita como un hecho compite con los hechos reales en cada búsqueda y en cada comprobación de conflictos.

<a id="working-memory"></a>

## Memoria de trabajo

Un bloque vive en `loom.working_memory`, identificado por la tupla de identidad más `run_id` y
`block_key` (por defecto `scratchpad`, con un máximo de 80 caracteres). Los bloques sobreviven a los reinicios y
no tienen TTL, por lo que sirven como punto de reanudación para una ejecución larga
([traza y reanudación](https://github.com/ClickHouse/loom/blob/main/docs/guides/trace-and-resume.md)). Un bloque solo desaparece cuando
su escritor lo elimina o cuando lo cubre un borrado.

| Op                       | Rol       | Qué hace                                                                                  |
| ------------------------ | --------- | ----------------------------------------------------------------------------------------- |
| `working_memory.set`     | escritor  | Reemplaza el contenido de un bloque; se aplica `char_limit`.                              |
| `working_memory.append`  | escritor  | Añade `addition` con `separator` (por defecto, salto de línea).                           |
| `working_memory.replace` | escritor  | Reemplaza cada aparición de `old` por `new` dentro de un bloque.                          |
| `working_memory.delete`  | escritor  | Marca un bloque con una lápida (tombstone).                                               |
| `working_memory.get`     | lector    | Lee un bloque, con `fill_ratio`.                                                          |
| `working_memory.render`  | lector    | Renderiza los bloques del ámbito (o hasta 20 `block_keys` indicados) como XML o Markdown. |

Listas de campos: [referencia de operaciones](https://github.com/ClickHouse/loom/blob/main/docs/reference/operations.md#working_memory).

<a id="size-limits"></a>

### Límites de tamaño

| Límite                | Valor                                                                     |
| --------------------- | ------------------------------------------------------------------------- |
| `char_limit` por defecto | 4,096 caracteres por bloque                                            |
| `char_limit` máximo   | 16,384 caracteres                                                         |
| Al superar el límite  | 422 `WORKING_MEMORY_LIMIT_EXCEEDED`, con `value_length` y `char_limit`    |
| Bloques por render    | `limit` 1–100 (por defecto 20)                                            |

Cada escritura devuelve `value_length` y `fill_ratio` (`len(content) / char_limit`), de modo que
quien llama pueda recortar antes de que se rechace una escritura. Un `replace` cuyo texto `old` no existe responde
422 `SUBSTRING_NOT_FOUND`; uno sobre un bloque inexistente responde
`WORKING_MEMORY_BLOCK_NOT_FOUND`. `working_memory.get` sobre un bloque inexistente responde
`found=false`.

```python
from loom import Agent

async with Agent(org="acme", namespace="support", agent="triage-bot") as bot:
    await bot.working_memory.set(
        "cursor: ticket 4812\nlease: holds refunds queue", block_key="state", run_id="run-7"
    )
    await bot.working_memory.append("step 3 done", block_key="state", run_id="run-7")
    print(await bot.working_memory.render(run_id="run-7"))
    # <working-memory block="state">cursor: ticket 4812 ...</working-memory>
```

<a id="profiles"></a>

## Perfiles

Una sección de perfil es una fila de memoria con `memory_type='profile'` cuya clave es el `label` de la sección. `profile.set`, `profile.append` y `profile.replace` son envoltorios delgados sobre `memory.set` que fijan ese tipo y un `char_limit`. Leer una sección es `memory.get` con el label como clave; el SDK de Python lo envuelve como `agent.profile.get(label)`, `agent.profile.list()` y `agent.profile.delete(label)` (métodos del SDK, no ops del protocolo).

| Op                 | Rol    | Qué hace                                                                                                      |
| ------------------ | ------ | ------------------------------------------------------------------------------------------------------------- |
| `profile.set`      | writer | Crea o reemplaza la sección en `label` (`if_match_sha256` para escritores concurrentes).                      |
| `profile.append`   | writer | Añade contenido a una sección; reintenta con compare-and-swap cuando otro cliente escribió primero.           |
| `profile.replace`  | writer | Reescribe una subcadena dentro de una sección (422 `SUBSTRING_NOT_FOUND` si no existe).                       |
| `profile.render`   | viewer | Renderiza todas las secciones (o los `labels` indicados, en ese orden) como un único fragmento XML o Markdown. |
| `profile.activity` | viewer | Qué clientes leyeron el perfil y cuándo (`lookback_days`, con un máximo de 30).                               |

Listas de campos: [referencia de operaciones](https://github.com/ClickHouse/loom/blob/main/docs/reference/operations.md#profile).

- **Límite.** El `char_limit` predeterminado es 8,192 caracteres, medidos sobre el JSON canónico del valor; un valor más largo responde 422 `CHAR_LIMIT_EXCEEDED`.
- **Alcance.** `profile.render` lee por defecto las secciones de todos los agentes del espacio de nombres (`scope="namespace"`), de modo que una persona escrita en el panel llega a todos los clientes MCP. `scope="this_agent"` se limita a las del propio agente que llama. Con `read_scope` `own_user` o `own_session`, solo se renderizan las secciones de ese usuario (o de esa sesión) y las del nivel de espacio de nombres (`user_id` `-`).
- **Búsqueda.** Las filas de perfil quedan fuera de `memory.search`. La herramienta `loom_search` tiene una rama de perfil que puntúa las secciones por significado cuando la pregunta trata sobre el usuario; el vector de cada sección se actualiza fuera de la ruta de la solicitud tras cada escritura. Consulta [búsqueda unificada](https://github.com/ClickHouse/loom/blob/main/docs/features/unified-search.md).
- **Consolidar hechos en un perfil.** El worker `profile_consolidation` (desactivado por defecto, `LOOM_PROFILE_CONSOLIDATION_ENABLED`) consolida las memorias de preferencias y decisiones de un usuario en una sección versionada (label `consolidated_profile`) mediante una llamada a un modelo. Las filas etiquetadas con un atributo protegido se excluyen. Su procedencia es `derived` (`source_trace` `profile.fold:<label>:<memory ids>`, los hechos que leyó).

```python
async with Agent(org="acme", namespace="support", agent="triage-bot") as bot:
    await bot.profile.set("persona", "Terse. Cite ticket ids. Never promise refunds.")
    await bot.profile.append("preferences", "Prefers EUR invoices.", user_id="u-42")
    system = await bot.profile.render(user_id="u-42", read_scope="own_user")
```

<a id="structured-profiles"></a>

## Perfiles estructurados

Un bot de chat que debe conocer a su usuario antes de la primera respuesta lee un objeto con una forma que define su operador, en lugar de texto libre. Un administrador asigna al espacio de nombres un esquema de perfil; el worker `structured_profile` mantiene actualizado el perfil de cada usuario con esa forma a partir de las memorias del usuario; `profile.structured` lo devuelve junto con su estado. Una lectura nunca llama a un modelo.

| Op                     | Rol    | Qué hace                                                                                                                                                         |
| ---------------------- | ------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `profile_schema.set`   | admin  | Define el esquema del espacio de nombres (`json_schema`); queda auditado. Reemplaza el anterior.                                                                 |
| `profile_schema.get`   | viewer | El esquema, su `schema_sha256`, quién lo definió y cuándo.                                                                                                       |
| `profile_schema.clear` | admin  | Elimina el esquema; queda auditado. Los perfiles ya construidos permanecen como filas de memoria.                                                                |
| `profile.structured`   | viewer | El perfil de un usuario: `profile`, `status`, `built_at`, `memory_id`, `version` (y `version_text`, lo mismo como cadena, exacta en JavaScript).                  |

El esquema es un objeto JSON Schema con entre 1 y 64 `properties`, de 16 KiB como máximo. Cada propiedad tiene un `type` (`string`, `number`, `integer`, `boolean` o `array` con `items` de uno de esos tipos y un `maxItems` opcional de hasta 50) y una `description`, que el modelo lee. Una respuesta completa contiene como máximo 256 valores, contando uno por propiedad y `maxItems` por cada array, de modo que cabe en el límite de salida del modelo. Las cadenas admiten `enum`, `pattern`, `minLength` y `maxLength`; los números admiten `minimum` y `maximum`, las restricciones que usan los [registros tipados](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/writing.md#typed-records).
Cada propiedad está en todos los perfiles, con valor `null` cuando las memorias no la determinan, por lo que el esquema no tiene `required`; `additionalProperties` solo puede ser `false`. Se acepta un `title` en cualquier nivel y se ignora.

```python
schema = {
    "type": "object",
    "properties": {
        "preferred_name": {"type": "string", "description": "What the user asks to be called"},
        "timezone": {"type": "string", "description": "IANA time zone, e.g. Europe/Berlin"},
        "languages": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Languages they speak",
        },
    },
}
async with Agent(org="acme", namespace="support", agent="chat", api_key=ADMIN_TOKEN) as admin:
    await admin.client.profile_schema.set({"json_schema": schema})

async with Agent(org="acme", namespace="support", agent="chat") as bot:
    out = await bot.client.profile.structured({"user_id": "u-42"})
    # {"status": "ready", "profile": {"preferred_name": "Sam", "timezone": "Europe/Berlin",
    #  "languages": ["en", "de"]}, "built_at": "...", ...}
```

| `status`    | Significado                                                                                                                                                                                                                            |
| ----------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `ready`     | Construido a partir de las memorias actuales del usuario con el esquema actual; todo `null` para un usuario sin hechos.                                                                                                                |
| `stale`     | Una memoria o el esquema cambiaron desde que se construyó, o uno de sus hechos no se sirvió cuando se construyó; se sirve tal como se construyó hasta que una pasada lo reconstruya.                                                   |
| `pending`   | No hay perfil vigente: aún no se construyó, o la fila construida expiró, tiene el contenido sensible eliminado por redacción (hasta que el worker la reconstruya) o está etiquetada como `high` (retenida hasta que la clave pase a una nueva versión); todas las propiedades valen `null`. |
| `no_schema` | El espacio de nombres no tiene esquema, o el almacenado ya no se puede analizar; `profile` es `{}`.                                                                                                                                    |

- **Almacenamiento.** El perfil es la fila `profile` del usuario con la clave `structured_profile` (`agent` `-`), por lo que `profile.render` y `context.pack` también lo incluyen, y [`memory.history`](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/history-and-provenance.md#versions-memoryhistory) lista sus versiones. Su procedencia es `derived`; `source_trace` es `profile.fold:structured_profile:<memory ids>`, las memorias a partir de las cuales se construyó. Solo se sirve una fila que escribió el worker (su `actor`): una fila que un llamador escribe en la clave se lee como ausencia de perfil (`pending`), sean cuales sean sus etiquetas, y el worker la reconstruye encima a menos que esté fijada, bloqueada o etiquetada como `high`.
- **Fuentes.** Los hechos `semantic`, `preference` y `decision` vigentes del usuario, bajo el mismo filtro que el fold de texto libre: nunca una fila `high`, un duplicado (cuenta su fila canónica), un hecho sobre otra persona (`about:other`), un atributo protegido ni una solicitud almacenada de olvidar. Cada hecho se fecha por el momento en que pasó a ser verdadero (`valid_at`), o si no por su último cambio, y una construcción lee primero los más confiables y los cambiados más recientemente, de modo que una edición se lee como la declaración más nueva.
- **Obsolescencia.** Cada perfil lleva el hash de las propiedades de su esquema (un `title` o el orden de las propiedades no cambian ninguno), un digest de sus fuentes (el id de cada una, su hash de contenido, su confianza y la fecha por la que una construcción lee y ordena: `valid_at`, o si no el último cambio) y su número. Un hecho nuevo o editado, una reversión, una restauración, un hecho eliminado, un cambio de confianza o de fecha, y un cambio que mueve una fila dentro o fuera de las fuentes (pasa a `high`, cambia de tipo, gana o pierde una etiqueta `about:other`, `attribute:` o `forget`, expira o supera su `valid_to`) cambian el digest. También lo hace una reafirmación o un refuerzo, ya que mueve el hecho al lugar más nuevo de lo que lee el modelo: cuesta una reconstrucción tras el cooldown. Igual ocurre con un `memory.lock` o `memory.unlock` de un hecho fuente: escribe una versión nueva con el mismo contenido, que fecha el hecho en el momento del bloqueo y lo mueve al lugar más nuevo de lo que lee el modelo y al más nuevo de los hechos que lee una construcción. De dos hechos que se contradicen bajo claves distintas, el último bloqueado o desbloqueado se lee entonces como el más reciente. Una recuperación (recall) o un backfill, que conserva el último cambio de la fila, no lo hace. El digest cubre todas las fuentes, mientras que una construcción lee solo los `LOOM_STRUCTURED_PROFILE_MEMORY_LIMIT` (100) más confiables y más nuevos: para un usuario con más hechos que eso, un cambio en uno fuera de ese conjunto sigue costando una llamada al modelo que no puede cambiar la respuesta. Un cambio de esquema se reconstruye a partir de la siguiente pasada, como máximo `LOOM_STRUCTURED_PROFILE_MAX_USERS_PER_PASS` perfiles por pasada. Una eliminación (menos fuentes de las que contó el perfil) omite el cooldown y se reconstruye una vez que el espacio de nombres se escanea de nuevo, como máximo `LOOM_STRUCTURED_PROFILE_SCAN_MAX_AGE_SECONDS` (600) después mientras las pasadas tengan presupuesto; `profile.structured` la informa como `stale` de inmediato. Los demás cambios se reconstruyen tras `LOOM_STRUCTURED_PROFILE_COOLDOWN_SECONDS` (600). Una construcción a la que le falta un hecho que debía haber leído (uno de los hechos que contó, hasta el límite de memoria, no se sirvió: una réplica de ClickHouse por detrás de aquella en la que se contaron sus fuentes, o, con Postgres, un cambio que la autoridad tiene sobre ese hecho aún no había llegado a ClickHouse) se lee como `stale` y se construye de nuevo tras el cooldown, con una llamada al modelo cada vez, mientras ese cambio siga ausente. Una construcción a la que no se sirvió ninguno de los hechos que contó no hace llamada al modelo: reemplaza un perfil servido por el de todo `null`, que se escribe de nuevo tras cada cooldown, y no escribe nada para un usuario sin perfil servido (`pending`). Un usuario cuyos hechos han desaparecido todos recibe un perfil de todo `null` sin llamada al modelo. Una fila de perfil expirada o con el contenido sensible eliminado por redacción se lee como ausencia de perfil (`pending`) y se reconstruye. Una fila de perfil `high` también se lee como ausencia de perfil, pero el worker la deja como está: se retiene como una fijación hasta que la clave pase a una nueva versión.
- **Límite de salida.** El modelo puede escribir la entrada `structured_profile` de `LLM_MAX_TOKENS` (32, modificada mediante `LOOM_LLM_MAX_TOKENS_OVERRIDES`) más la estimación que hace el esquema de una respuesta completa: cada clave con su puntuación, y cada valor (uno por propiedad, `maxItems` por array) dimensionado por su `maxLength` o su valor de `enum` más largo a dos caracteres por token, 48 tokens para una cadena sin ninguno de los dos, 12 para un número o un booleano. `profile_schema.set` rechaza un esquema cuya estimación supere 16,384 tokens. Una cadena más larga que su estimación aún puede cortarse; da a una propiedad de texto libre un `maxLength` para dejarle espacio.
- **Validación.** La respuesta del modelo se contrasta con el esquema antes de escribirse: un valor de tipo incorrecto, fuera de su enum o de sus restricciones, un número que no es finito (o un entero superior a 2^53), o una propiedad que el esquema no declara se descarta (`null`).
- **Pasadas.** Una pasada construye como máximo `LOOM_STRUCTURED_PROFILE_MAX_USERS_PER_PASS` (20) perfiles. Recorre los espacios de nombres con esquema en orden desde donde se detuvo la última pasada, y cada uno recibe una parte igual. Un espacio de nombres sin nada que construir no gasta ninguna, así que la pasada sigue con los siguientes espacios hasta agotar su presupuesto o haberlos alcanzado todos; lo que un espacio de nombres deja va a los que usaron toda su parte, tomando de los usuarios que su escaneo ya listó. Una pasada puede ejecutarse `LOOM_STRUCTURED_PROFILE_MAX_PASS_SECONDS` (600) antes de que el supervisor la abandone, y no toma ningún usuario en la segunda mitad de ese tiempo: la construcción en curso tiene la segunda mitad para terminar, y los usuarios no alcanzados siguen listados para la siguiente pasada. Un escaneo de un espacio de nombres lista hasta diez veces su parte, y las siguientes pasadas toman de esa lista hasta que se agote, cambie el esquema o tenga `LOOM_STRUCTURED_PROFILE_SCAN_MAX_AGE_SECONDS` (600) de antigüedad; un escaneo que encontró a todos los usuarios atrasados (menos de los que pidió) no se repite antes de ese momento una vez agotado, de modo que un espacio de nombres se escanea como máximo una vez por esa antigüedad mientras nadie más esté atrasado. Un usuario cuya construcción falló (un error del modelo, una respuesta que no es un objeto JSON) espera `LOOM_STRUCTURED_PROFILE_BACKOFF_BASE_SECONDS` (300), duplicándose hasta `LOOM_STRUCTURED_PROFILE_BACKOFF_CAP_SECONDS` (21600). Se deja fuera de los escaneos del espacio de nombres hechos mientras espera, así que se reintenta en el primer escaneo posterior al fin de la espera: como máximo la espera más `LOOM_STRUCTURED_PROFILE_SCAN_MAX_AGE_SECONDS` (600) tras el fallo. Un espacio de nombres cuyo escaneo o esquema almacenado falla se cuenta y retrocede del mismo modo hasta que termine la espera o cambie su esquema; un espacio de nombres que está esperando no ocupa lugar en la pasada. Una construcción que la fila de perfil rechaza (bloqueada, fijada, etiquetada `high`, en lista negra), o cuya respuesta el modelo cortó en el límite de salida, no se reintenta hasta que cambien las memorias del usuario, la fila de perfil (un desbloqueo o una desfijación, también en una fila con el contenido sensible eliminado por redacción) o el esquema, o hasta que pasen `LOOM_STRUCTURED_PROFILE_REFUSAL_RETRY_SECONDS` (21600), de modo que un bloqueo que caduca por sí solo se reintenta. Estas esperas y las listas de escaneo se guardan en el proceso del worker y comienzan de nuevo cuando otro proceso toma el lease del worker. Una pasada que alcanza todos los espacios de nombres con esquema y no encuentra ningún usuario atrasado espera `LOOM_STRUCTURED_PROFILE_IDLE_INTERVAL_SECONDS` (600) antes de la siguiente; una que alcanzó solo parte de la rotación se ejecuta de nuevo al intervalo del worker.
- **Borrado.** Una construcción no escribe nada para un usuario sin hechos y sin fila de perfil, así que un usuario borrado después de que un escaneo lo listara no recibe una fila nueva cuando una pasada posterior lo alcanza. Una construcción tampoco escribe nada cuando un hecho que leyó deja de estar vigente una vez que el modelo respondió (borrado o eliminado mientras se ejecutaba). Lo que queda es el breve intervalo entre esa comprobación y la escritura: una fila escrita ahí mientras corre la saga de borrado es una fila viva que encuentra su censo final, y la saga falla de forma cerrada ante ella. `memory.redact` de un hecho fuente depura toda versión del perfil cuya traza lo nombre, y se rechaza cuando la clave del perfil está bloqueada. Un redact reintentado tras un fallo alcanza las mismas claves de perfil. Una versión de perfil cuya traza no nombra ids de memoria (solo `profile.fold:<label>`, como la escribe una construcción que no registra sus fuentes, incluida una construcción anterior durante una actualización progresiva) no se alcanza y conserva el texto hasta que se borre al usuario ([seguridad](https://github.com/ClickHouse/loom/blob/main/docs/architecture/security.md#sensitivity)).
- **Alcance.** La lectura es siempre de un solo `user_id`. Un token fijado a un usuario (`read_user_id`, el token de un usuario final) lee solo el perfil de ese usuario, con o sin `user_id` en el cuerpo; otro usuario da 403. Un token limitado a una sesión (`own_session`) se rechaza (`IDENTITY_INCOMPLETE` sin `session_id`, `AUTH_INSUFFICIENT` con uno): un perfil abarca las sesiones del usuario.
- **Llamadas al modelo.** Solo el worker llama al modelo, solo para un espacio de nombres con esquema, nunca para una organización con las llamadas al modelo desactivadas ([`org.set_model_calls`](https://github.com/ClickHouse/loom/blob/main/docs/reference/operations.md#org)), y a través del proveedor o el gateway de inferencia configurado.

<a id="how-contextpack-loads-them"></a>

## Cómo los carga `context.pack`

`context.pack` renderiza `working_memory` primero y `profile` después, antes de las secciones que dependen de la consulta, de modo que el inicio del pack se mantiene igual entre preguntas y un cliente puede colocar un punto de corte de la caché de prompts después de él (`stable_prefix_chars`, `stable_hash`). La memoria de trabajo se organiza en capas, empezando por la más estrecha: un pack para una ejecución también incluye los bloques de la sesión (`run_id` `-`), y un pack para una sesión también incluye los bloques del agente (`session_id` `-`, `run_id` `-`). La nota «¿dónde me quedé?» de un plugin (`session:<project>`) vive en la capa del agente; con `session_block` definido, las notas `session:*` de otros proyectos se omiten. El orden de las secciones, los presupuestos y las demás ops de `context.*` están en [búsqueda unificada](https://github.com/ClickHouse/loom/blob/main/docs/features/unified-search.md).

<a id="settings"></a>

## Configuración

| Configuración                   | Efecto                                                                                                                                                      |
| ------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `LOOM_PROFILE_CONSOLIDATION_*`  | El worker de consolidación de perfiles ([Consolidación](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md#consolidation)).                                                                  |
| `LOOM_STRUCTURED_PROFILE_*`     | El worker de perfiles estructurados ([Perfiles estructurados](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md#structured-profiles)).                                                |
| `LOOM_MEMORY_ATTRIBUTE_TAGGING` | Etiqueta las filas con atributos protegidos, que la consolidación deja fuera ([Sensibilidad y procedencia](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md#sensitivity-and-provenance)). |

<a id="limits"></a>

## Límites

- **Las escrituras que superan el límite se rechazan, no se truncan.** Vigila `fill_ratio`.
- **La memoria de trabajo no es recall.** Un bloque nunca se clasifica ni se incrusta. Además de `context.pack` y las operaciones render/get, solo una llamada que nombre una ejecución (run) o una sesión incluye bloques: `memory.ask` incluye los bloques de la ejecución en su prompt de síntesis, y `memory.search` devuelve los bloques de la ejecución y de la sesión en `scratchpad`. Ambas leen dentro del alcance de lectura de la solicitud.
- **Una sección de perfil es una fila por etiqueta.** `profile.set` la reemplaza; las versiones anteriores siguen siendo legibles mediante [`memory.history`](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/history-and-provenance.md#versions-memoryhistory).
- **Render abarca a todos los agentes por defecto** salvo que se use `scope="this_agent"`.

<a id="related"></a>

## Relacionado

- [Escritura de memoria](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/writing.md): hechos duraderos.
- [Conversaciones y sesiones](https://github.com/ClickHouse/loom/blob/main/docs/features/conversations-and-sessions.md): resúmenes de sesión.
- [Búsqueda unificada](https://github.com/ClickHouse/loom/blob/main/docs/features/unified-search.md): `context.*` y `loom_search`.
