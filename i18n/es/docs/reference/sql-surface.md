<!-- i18n: source=docs/reference/sql-surface.md blob=cbe1b629ce7bff1c19c5d51c3a461f058ad2fd2c model=claude-sonnet-5-5 -->
> Esta página es una traducción automática de la [página en inglés](https://github.com/ClickHouse/loom/blob/main/docs/reference/sql-surface.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/reference/sql-surface.md), which is authoritative.

<a id="sql-surface"></a>

# Superficie SQL

La superficie SQL es un conjunto de vistas de ClickHouse de solo lectura sobre las memorias de un tenant, el historial de memorias, las conversaciones y el registro de solicitudes. El cliente SQL, la herramienta de BI o el trabajo de warehouse de una organización las lee con un usuario de ClickHouse que crea `loom sql-access`. Ese usuario lee las vistas y nada más, y cada vista muestra solo las organizaciones y los espacios de nombres a los que se le dio acceso.

<a id="version-and-compatibility"></a>

## Versión y compatibilidad

El nombre de cada vista incluye su versión: `sql_v1_memories` es la versión 1. Los nombres de las columnas, sus tipos y sus significados son un contrato público:

- Una vista `v1` puede ganar columnas. Nunca pierde ni cambia el nombre de una columna, y una columna nunca cambia de tipo ni de significado.
- Un cambio que rompa esa regla se publica como una vista `sql_v2_*` junto a `v1`. Ambas se mantienen durante al menos una versión, y las notas de la versión indican qué vista `v1` se retira.
- Selecciona las columnas por nombre: `SELECT *` incorpora las columnas que se añadan.

<a id="views"></a>

## Vistas

Cada vista está en la base de datos de Loom (`loom`, a menos que `LOOM_CH_DATABASE` indique otra) y
incluye las columnas de identidad `org` y `namespace`. `sql_v1_memories`, `sql_v1_memory_history`
y `sql_v1_agent_events` incluyen además `agent`, `user_id` y `session_id`;
`sql_v1_conversations` incluye `agent` y `user_id`; `sql_v1_usage_daily` incluye `agent`.
Un join puede coincidir con cualquier parte de la [tupla de identidad](https://github.com/ClickHouse/loom/blob/main/docs/concepts/identities-and-namespaces.md)
que ambos lados incluyan.

<a id="sql_v1_memories"></a>

### `sql_v1_memories`

Una fila por memoria (identidad y clave): su versión actual. Una memoria eliminada o borrada
no aparece, ni tampoco una rechazada. Los demás estados se listan con su `status`:
`memory.list` oculta las filas `duplicate`, `superseded` y `proposed` salvo que se pidan, así que filtre
por `status = 'active'` para obtener lo que muestra un listado predeterminado. Una memoria que ha pasado su `expires_at` se
lista con su valor hasta que el barrido de expiración actúa sobre ella, igual que la lista `memory.list`; añada
`(expires_at IS NULL OR expires_at > now())` para obtener lo que sirve `memory.search`.

| Columna                                                            | Significado                                                                                        |
| ------------------------------------------------------------------ | -------------------------------------------------------------------------------------------------- |
| `org`, `namespace`, `agent`, `user_id`, `session_id`, `run_id`     | a quién pertenece la memoria y la ejecución que la escribió (`-` si no hay ninguna)                |
| `memory_key`, `memory_id`, `memory_type`                           | la clave, el id de la memoria (una versión con `version`), su tipo (`fact`, `decision`, ...)       |
| `value`                                                            | el valor tal como lo sirve `memory.get`, o NULL cuando se retiene                                  |
| `value_withheld`                                                   | `''` cuando se sirve `value`, `sensitive` para una fila `high`, `redacted` para una versión redactada |
| `content_sha256`                                                   | el SHA-256 del valor, NULL siempre que `value` lo sea: el hash de un secreto corto se puede adivinar |
| `version`, `status`, `confidence`, `pinned`, `tags`                | igual que en `memory.get`                                                                          |
| `sensitivity`, `source_class`, `actor`                             | la etiqueta, la clase de procedencia, el sujeto del token que la escribió                          |
| `created_at`, `updated_at`, `expires_at`, `valid_at`, `invalid_at` | cuándo se escribió y se modificó por última vez, cuándo expira, cuándo el hecho pasó a ser verdadero y dejó de serlo |

<a id="sql_v1_memory_history"></a>

### `sql_v1_memory_history`

Una fila por versión de cada memoria, incluida la versión de una eliminación (`status =
'tombstoned'`, con `tombstoned_at` establecido). Una versión rechazada no aparece. Las columnas son las de
`sql_v1_memories` sin `expires_at`, `valid_at` ni `invalid_at`, más `tombstoned_at`.
`value` y `content_sha256` son NULL en una versión redactada y en cada versión de una clave
que alguna vez se etiquetó como `high`: la versión anterior a que el barrido de sensibilidad etiquetara una fila contiene
el mismo valor sin etiqueta.

<a id="sql_v1_conversations"></a>

### `sql_v1_conversations`

Una fila por turno de conversación almacenado de cada usuario: `org`, `namespace`, `agent`, `user_id`,
`thread_id`, `msg_id`, `role`, `content`, `tokens`, `run_id` y `ts` (la última
escritura del turno). Un turno eliminado o compactado (`tombstoned_at` establecido) no aparece, y uno borrado
desaparece junto con su fila. Un turno no lleva etiqueta de sensibilidad, por lo que `content` se sirve tal como se escribió,
igual que `conversation.get` sirve los turnos de un hilo; la comprobación de los listados entre hilos para texto que
se lee como `high` se ejecuta en Loom, no en SQL.

<a id="sql_v1_agent_events"></a>

### `sql_v1_agent_events`

El registro de solicitudes: una fila por llamada a una op o pasada de un worker, con `kind` `''` para una solicitud.
`ts`, `org`, `namespace`, `agent`, `user_id`, `session_id`, `run_id`, `operation`, `kind`,
`request_id`, `trace_id`, `latency_ms`, `success`, `error_code`, `status_class`,
`memory_key`, `input_tokens`, `output_tokens`, `embedding_tokens`, `llm_calls`,
`upstream_cost_usd` (gasto del modelo en USD), `bytes_written`, `model`, `consumer`, `subject`
(el sujeto del token que llamó) y `transport`. Los argumentos de la solicitud no están incluidos. Las filas de un
usuario final borrado muestran `user_id` `<erased>`.

Contiene los 31 días UTC completos anteriores a hoy y lo que va de hoy; cada sentencia lee su propio
`now()`. La longitud de la ventana y su alineación con los días UTC forman parte del contrato `v1`.

<a id="sql_v1_usage_daily"></a>

### `sql_v1_usage_daily`

`sql_v1_agent_events` sumada por `day` UTC, `org`, `namespace`, `agent`, `operation` y
`kind`, sobre la misma ventana: `ops`, `errors`, `input_tokens`, `output_tokens`,
`embedding_tokens`, `llm_calls`, `upstream_cost_usd`, `bytes_written`, `latency_sum_ms` (con
`ops`, una media entre filas), y `latency_p50_ms` y `latency_p95_ms`, aproximadas y por
fila: no se combinan entre filas. Todos los `day` salvo el de hoy son días completos.

<a id="what-a-read-costs"></a>

### Qué cuesta una lectura

Una vista aplica el filtro del llamante sobre las filas que devuelve, así que un filtro sobre `ts`, `day`,
`thread_id` o `memory_key` acota la respuesta, no lo que se lee. Cada lectura de
`sql_v1_memories`, `sql_v1_memory_history` y `sql_v1_conversations` lee todo el ámbito de la concesión:
el historial ordena todas las versiones que contiene, y la vista de conversaciones agrupa todos los turnos
que contiene, contenido incluido. Estas lecturas crecen con lo que almacena el ámbito, y un ámbito amplio puede superar
los límites de lectura que se indican más abajo. Cada lectura de una vista del registro lee todo el ámbito de la concesión sobre la
ventana: como máximo desde el inicio del mes en que se abre la ventana, normalmente cerca de la propia
ventana. Una concesión más amplia (`*` espacios de nombres, `--all`) lee más. Los límites de lectura del usuario SQL
se aplican a esa lectura (`LOOM_CH_ANALYSIS_MAX_ROWS_TO_READ`, `_MAX_EXECUTION_S`,
`_MAX_MEMORY_BYTES`, `_MAX_THREADS`), por lo que un ámbito con mucha actividad puede superarlos: reduzca la concesión, o
aumente esos ajustes, que `insights.query` comparte, y ejecute de nuevo `loom sql-access grant`,
que los escribe en el perfil de los usuarios SQL. Los eventos anteriores a la ventana no están en la
superficie; la [exportación de analítica](https://github.com/ClickHouse/loom/blob/main/docs/operations/analytics-export.md) escribe los totales de uso diarios,
con nombres con hash, en el bucket del operador. `loom.agent_events` en sí no tiene TTL a menos que `LOOM_RETENTION_AGENT_EVENTS_DAYS` establezca uno.

<a id="what-the-views-never-show"></a>

## Lo que las vistas nunca muestran

Las vistas de memoria aplican las reglas de los listados propios de Loom
([sensibilidad](https://github.com/ClickHouse/loom/blob/main/docs/architecture/security.md#sensitivity)); la vista de conversaciones entrega un
turno como lo hace `conversation.get`, según se describe arriba. Ninguna de ellas muestra:

- el valor de una fila `high` ni su hash, ni en la vista actual ni en ninguna versión de su
  clave en el historial;
- el valor de una versión redactada ni su hash;
- una memoria rechazada, una memoria eliminada o borrada, ni un turno de conversación eliminado;
- ninguna fila de un espacio de nombres clasificado como `restricted` en `loom.namespaces` o `loom.code_repos`,
  sea cual sea la concesión, desde el momento en que se clasifica: Loom audita cada lectura de un
  [espacio de nombres restringido](https://github.com/ClickHouse/loom/blob/main/docs/architecture/security.md#restricted-namespaces), algo que una vista
  no puede hacer;
- ninguna fila de un espejo (un espacio de nombres llamado `acl-` seguido de 24 dígitos hexadecimales), sea cual sea la concesión:
  cada uno de sus documentos se muestra solo a un lector que la lista de permisos de su almacén admita
  ([fuentes de documentos](https://github.com/ClickHouse/loom/blob/main/docs/architecture/document-sources.md#the-mirror)), algo que una vista
  no puede comprobar;
- las tablas ajenas a las cinco vistas: tokens, concesiones, secretos, el registro de auditoría, documentos,
  embeddings y argumentos de las solicitudes;
- las tablas `system` que muestran las sentencias de otros usuarios, como `system.query_log`,
  `system.mutations` y `system.processes`. ClickHouse se las niega a un usuario sin
  concesión solo si `access_control_improvements.select_from_system_db_requires_grant` está activado (el
  valor por defecto), por lo que `loom sql-access grant` inicia sesión como cada usuario al que le asigna una contraseña y
  lo elimina cuando `system.processes` responde. En la base de datos de Loom, `system.tables` lista
  solo las cinco vistas.

Las vistas de memoria y de conversaciones leen las tablas de las que borra la
[saga de borrado](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/retention-and-erasure.md) y que cuenta su censo de residuos, por lo que las filas de un usuario o espacio de nombres borrado salen de ellas junto con las tablas. Las vistas
del libro mayor conservan sus filas, que no contienen contenido: el borrado de un usuario reescribe el
`user_id` del usuario como `<erased>`, igual que en `loom.agent_events`.

<a id="freshness"></a>

## Actualización de los datos

Las vistas leen ClickHouse en el momento de la consulta; no se copia nada. Postgres guarda la memoria activa ([autoridad de almacenamiento](../architecture/storage-authority.md)), y `loom.memory` y `loom.memory_versions` son proyecciones que se publican después de cada commit, en un flush cada `LOOM_PG_PUBLISH_INTERVAL_MS` (250 ms por defecto). Una escritura de memoria confirmada aparece en `sql_v1_memories` y `sql_v1_memory_history` cuando se completa su flush, normalmente en aproximadamente un intervalo; una entrega que dejó pendiente un escritor detenido la publica el worker de entregas, lo que puede tardar más. Las vistas no esperan a nada: a diferencia de `memory.search`, que responde `PROJECTION_BEHIND` pasado `LOOM_PG_PROJECTION_WAIT_MS`, una vista devuelve lo que ClickHouse contiene. Las conversaciones y el registro de solicitudes se escriben directamente en ClickHouse.

<a id="access"></a>

## Acceso

Un usuario SQL de la superficie es un usuario de ClickHouse con `SELECT` sobre las cinco vistas y ningún otro permiso, en el perfil de configuración `loom_sql_surface`:

- `readonly = 1`: una sentencia no cambia ninguna configuración salvo `final`, `join_use_nulls`, `group_by_use_nulls` y `transform_null_in`, que dejan las filas de cada vista como están y que los clientes envían por su cuenta (`pg_clickhouse` entre ellos). Cualquier otra configuración podría cortar el filtro de una vista: un modo de desbordamiento, `additional_table_filters`, PREWHERE bajo `FINAL`.
- valores que el perfil mantiene sean cuales sean los valores por defecto del servidor: `apply_deleted_mask = 1` y `apply_patch_parts = 1` (una fila eliminada sigue oculta), `use_query_cache = 0` y `use_query_condition_cache = 0` (ambas se indexan por el texto de la sentencia, que todos los usuarios comparten), `use_skip_indexes_if_final_exact_mode = 1`, `do_not_merge_across_partitions_select_final = 0` y `optimize_move_to_prewhere_if_final = 0` (`FINAL` lee la última versión de cada clave), todos los modos de desbordamiento en `'throw'` (read y timeout con sus formas `_leaf`, set, transfer, join, sort, distinct, group by y result), `send_logs_level = 'none'`, sin funciones de introspección, y `query_plan_filter_push_down = 0` y `enable_optimize_predicate_expression = 0`, de modo que el filtro de quien llama se ejecuta sobre lo que devuelve una vista y nunca sobre filas de otro inquilino (un filtro que lance un error sobre un valor lo citaría);
- los límites de `insights.query`, salvo sus topes de resultado, ya que un cliente SQL lee conjuntos de resultados completos: `LOOM_CH_ANALYSIS_MAX_EXECUTION_S`, `_MAX_ROWS_TO_READ`, `_MAX_MEMORY_BYTES` y `_MAX_THREADS`, y `_MAX_CONCURRENT` sentencias a la vez.

Cada vista se ejecuta con los privilegios de su definidor (`SQL SECURITY DEFINER`, el usuario que ejecutó la migración) y admite solo las filas cuyo `(org, namespace)` la tabla `loom.sql_access` concede a `currentUser()`, el usuario que envió la sentencia. Un usuario sin filas concedidas no lee nada, y lo mismo ocurre con cualquier otro usuario de ClickHouse, incluido el propio de Loom.

```bash
loom sql-access grant --user acme_bi --org acme --namespaces support,research
loom sql-access grant --user acme_bi --org acme --namespaces '*'   # every namespace of acme
loom sql-access grant --user ops_bi --all                          # the whole deployment
loom sql-access grant --user acme_bi --org acme --namespaces support --rotate
loom sql-access list
loom sql-access revoke --user acme_bi
```

- `grant` crea el usuario si no existe e imprime su contraseña una sola vez; Loom no guarda ninguna copia. `--org` y `--all` se excluyen entre sí, y `--all` no admite `--namespaces`. Si se ejecuta de nuevo, fija el alcance del usuario exactamente a lo que nombra: los espacios de nombres que ya no nombra dejan de verse cuando las nuevas filas llegan a la réplica que atiende la lectura; `revoke` termina el acceso de inmediato. `--rotate` imprime una contraseña nueva. Cuando falla un paso posterior a la contraseña, `grant` lo indica; ejecútalo de nuevo con `--rotate`.
- `--all` es para un despliegue cuyos datos pertenecen a una sola org. En un despliegue compartido, concede a cada inquilino su propia org.
- `grant` rechaza un alcance que nombre un espacio de nombres restringido, y un comodín que llegaría a uno que solo clasifica la configuración del repositorio de código: las vistas no pueden leer esa configuración, así que ejecuta el comando con la del servidor. Un espacio de nombres restringido ahí después de una concesión que lo alcanza queda dentro de las vistas de esa concesión: `list` nombra cada una de esas concesiones, para reemplazarlas por los otros espacios de nombres por su nombre, y termina con código 2.
- `revoke` elimina primero las filas de alcance y luego borra el usuario. Un espacio de nombres que una concesión nombra cuenta como en uso hasta que `revoke` elimina esa fila: su nombre no se entrega a un nuevo propietario, y una primera escritura en él lo registra sin propietario. Una concesión `*` no nombra ningún espacio de nombres: lee todos los de la org, incluidos los creados después, así que nunca concedas una en una org que comparten los registros de nuevos usuarios (`LOOM_SELF_SIGNUP_ORG`).
- El comando se ejecuta como `LOOM_CH_USER`, que necesita gestión de accesos: `CREATE USER`, `ALTER USER`, `DROP USER`, `CREATE SETTINGS PROFILE` y `ALTER SETTINGS PROFILE`, `SELECT ... WITH GRANT OPTION` sobre las cinco vistas, y `SHOW USERS` con lecturas de `system.users` y `system.settings_profile_elements`. Rechaza un nombre ya tomado por un usuario de ClickHouse que no esté en el perfil `loom_sql_surface`, y los usuarios propios de Loom (`LOOM_CH_USER`, el usuario lector, `loom_analyst`, `default`).
- Elimina un usuario con `revoke`, no con `DROP USER`: sus filas de alcance siguen concedidas hasta que `revoke` las reescribe, y un usuario posterior con el mismo nombre y `SELECT` sobre las vistas las leería.

<a id="querying"></a>

## Consultas

Desde el Postgres de la propia organización, consulta [Consultar Loom desde Postgres](https://github.com/ClickHouse/loom/blob/main/docs/guides/query-from-postgres.md).

```sql
SELECT memory_key, value, updated_at
FROM loom.sql_v1_memories
WHERE namespace = 'support' AND status = 'active'
ORDER BY updated_at DESC
LIMIT 20;

SELECT day, sum(upstream_cost_usd) AS spend
FROM loom.sql_v1_usage_daily
WHERE day >= toDate(now(), 'UTC') - 30
GROUP BY day ORDER BY day;
```
