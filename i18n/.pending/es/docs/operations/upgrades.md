<!-- i18n: source=docs/operations/upgrades.md blob=8caf8c789d2fc39ab89d9830d45a7e1229be2051 model=claude-sonnet-5-5 -->
> Esta página es una traducción automática de la [página en inglés](https://github.com/ClickHouse/loom/blob/main/docs/operations/upgrades.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/operations/upgrades.md), which is authoritative.

<a id="upgrades"></a>

# Actualizaciones

Una actualización reemplaza la imagen (y la del extractor de documentos, que no guarda estado) y
aplica las migraciones que incluye la nueva compilación. No se mueve ningún otro estado: todo lo duradero
está en ClickHouse (y en el bucket de documentos, para los originales escritos con
`LOOM_DOCUMENT_OBJECT_STORE=s3`), y los mirrors de código se montan sin cambios. Esta página
describe el lado del operador. Cómo ve un cliente los campos desconocidos y eliminados se explica en la [referencia de la API HTTP](https://github.com/ClickHouse/loom/blob/main/docs/reference/http-api.md#unknown-and-removed-fields). Qué versiones reciben correcciones y qué
SDK funciona con qué servidor: [versiones compatibles](https://github.com/ClickHouse/loom/blob/main/docs/operations/supported-versions.md).

<a id="how-migrations-run"></a>

## Cómo se ejecutan las migraciones

El journal, la numeración, las directivas del runner y las reglas para escribir una migración están en
[migraciones](https://github.com/ClickHouse/loom/blob/main/docs/development/migrations.md). Lo que necesita un operador:

- **Al arrancar.** El servidor aplica los archivos pendientes antes de informar que está listo o de iniciar
  los workers (`LOOM_BOOT_MIGRATE=true`, el valor por defecto). Cada proceso, en cualquier contenedor, toma
  el bloqueo `KeeperMap` en `loom._migration_lock`, de modo que cada archivo se aplica una sola vez. Un servidor
  sin Keeper es rechazado.
- **Reconstrucciones del proveedor.** Un arranque con archivos pendientes primero se inscribe como participante del
  proveedor y luego comprueba, bajo el bloqueo de configuración del proveedor (una clave de Keeper, retenida solo durante
  esa comprobación), que no haya una reconstrucción del proveedor sin terminar, y se niega a migrar si la hay:
  `Finish the provider rebuild before applying schema migrations`. Después los archivos
  se aplican solo bajo el bloqueo de migración. Una reconfiguración iniciada mientras tanto espera hasta que el proceso en arranque
  la reconozca, de modo que una reconstrucción nunca se ejecuta junto a una migración.
- **Un contenedor terminado mientras migra o reconfigura** bloquea a los demás solo durante
  un tiempo. El bloqueo de migración se toma por la fuerza una vez que su titular deja de renovarlo (`ttl_s`).
  El bloqueo del proveedor (TTL de 60 s, renovado cada 20 s) se toma por la fuerza una vez que ha expirado y la fila de participante de
  su titular también ha expirado: sin heartbeat durante 60 s (uno cada 10 s) y sin
  ninguna consulta etiquetada con su id en `system.processes` en ningún nodo. Los contenedores que arrancan juntos
  esperan hasta 120 s por el bloqueo del proveedor en lugar de fallar. Un bloqueo cuyo titular no
  tiene fila de participante se libera con `loom provider-lock clear --holder <holder>` una vez
  confirmado que ese proceso está detenido; el comando se niega mientras el titular renueve,
  envíe heartbeats o tenga una consulta en ejecución.
- **Mutaciones.** Un archivo se registra solo cuando sus mutaciones han terminado. Una mutación fallida,
  o una que supere `LOOM_MIGRATE_MUTATION_WAIT_S` (600 por defecto), hace fallar el archivo y
  detiene el arranque con el archivo indicado en el log.
- **Cambios de clave.** Una migración que cambia una clave de ordenación o de partición reconstruye por sí misma una tabla vacía
  y hace fallar el arranque si la tabla tiene filas. Ejecute `scripts/ops/table_rebuild.py`
  para esa tabla antes del despliegue; el dry run estricto lo detecta.
- **Instalación nueva.** Una instalación nueva y una actualización siguen el mismo camino: el servidor reproduce
  lo que le falte al ledger. En una base de datos vacía eso es todo el journal y tarda
  unos minutos.
- **Ledger varado.** Una base de datos cuyo ledger se detuvo a mitad de los archivos que reemplaza un
  baseline es rechazada (`stranded`): primero migre esa base con una compilación anterior a ese
  baseline.
- **Archivos renumerados.** Una fila del ledger cuyo archivo ya no existe en el repositorio es
  `ledger-only`, y `--strict` termina con código 1 ante ella, salvo cuando su SHA-256 registrado es el de un
  archivo actual: es el mismo archivo con un número nuevo. El runner lo informa como renombrado,
  registra el nombre nuevo sin volver a ejecutarlo si está pendiente, y
  `--strict` pasa. Un archivo editado después de ejecutarse con su nombre anterior tiene otro SHA-256,
  así que sigue como ledger-only: su nombre nuevo se ejecuta como archivo pendiente, y la fila antigua se
  elimina a mano ([solución de problemas](https://github.com/ClickHouse/loom/blob/main/docs/operations/troubleshooting.md#migrations-pending-or-checksum-drift)).
  `/v1/health` lista la fila antigua en `schema.ledger_only` hasta entonces.

La CLI ejecuta el mismo runner a mano:

```bash
loom migrate --status              # applied and pending; changes nothing
loom migrate --dry-run --strict    # pending files; exit 1 on ledger-only rows (not renames) or checksum drift
loom migrate                       # apply pending migrations
loom migrate --target N            # apply through file number N only
```

`make migrate`, `make migrate-status` y `make migrate-dry` ejecutan el mismo módulo desde un
checkout.

<a id="upgrade-procedure"></a>

## Procedimiento de actualización

1. Lee las notas de la versión en [publicación de GitHub](https://github.com/ClickHouse/loom/releases)
   para conocer los pasos del operador (nuevos ajustes, backfills que un despliegue debe ejecutar) y los
   cambios de protocolo visibles para el cliente.
2. Desde el checkout de la nueva build, contra el ClickHouse de producción, ejecuta
   `loom migrate --dry-run --strict`. Muestra lo que aplicará el arranque y se detiene ante problemas
   del registro ([solución de problemas](https://github.com/ClickHouse/loom/blob/main/docs/operations/troubleshooting.md#migrations-pending-or-checksum-drift)).
   Si la versión declara una reconstrucción de tabla, ejecuta ahora `scripts/ops/table_rebuild.py` para esa
   tabla. Un despliegue de la autoridad Postgres que pasa a una build que mantiene los
   encabezados de documentos de conocimiento en Postgres desactiva ahora `LOOM_RETENTION_DOCUMENTS_DAYS`, a
   mano: la build más nueva se niega a arrancar con ese ajuste definido, y bajo esa autoridad ningún
   TTL de retención llega a `loom.documents`, y el `retention_days` de un espacio de nombres tampoco
   llega a `loom.chunks`
   ([actualizaciones continuas](../architecture/rolling-upgrades.md), regla 38). Una
   [celda](../architecture/cells.md) que pasa a una build que marca sus sesiones y
   lleva los triggers de org (regla 47) primero apunta su `LOOM_PG_DSN` directamente a
   Postgres, sin pasar por ningún pooler ([Postgres](https://github.com/ClickHouse/loom/blob/main/docs/operations/postgres.md)), y una celda posterior preparada y
   luego revertida desactivando `LOOM_CELL` se limpia primero
   ([solución de problemas](https://github.com/ClickHouse/loom/blob/main/docs/operations/troubleshooting.md#a-process-with-loom_cell-unset-is-gated)):
   la build más nueva condiciona sus procesos a su estado. En una celda posterior ya preparada,
   el primer arranque con migración de la build más nueva crea los triggers de org, de modo que un pod
   antiguo que siga ejecutándose allí tiene rechazada cada inserción de una fila de org de un tenant
   (un error sin asignar para sus llamadores) hasta que se reemplace: despliega la celda en un solo paso.
3. Despliega la nueva imagen. El primer contenedor en arrancar aplica las migraciones; los demás
   esperan y luego no encuentran nada pendiente.
4. Espera a que `/v1/health` informe `status: ok` y un `schema.pending` vacío antes de
   enrutar tráfico hacia ella. El [par sin tiempo de inactividad](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#zero-downtime-pair)
   hace los pasos 2 a 4 por ti, salvo desactivar el ajuste que nombra el paso 2: `redeploy.sh`
   ejecuta el dry run estricto en su verificación previa y cambia solo después de que el nuevo color esté
   listo.
5. Ejecuta los backfills de la versión, si los hay ([mantenimiento](https://github.com/ClickHouse/loom/blob/main/docs/operations/maintenance.md#backfills-and-repairs)).
6. Actualiza los plugins de cliente cuando la versión mencione un cambio de plugin: los hooks se ejecutan
   en la máquina del cliente y solo cambian mediante el gestor de plugins del cliente
   (`claude plugin update loom@loom` para Claude Code), nunca mediante un despliegue del servidor.
7. Una vez detenida la última réplica antigua, vuelve a verificar los borrados completados durante el
   despliegue: `loom erasure-re-verify --completed-since <when step 3 began> --resume`. La saga de una
   réplica antigua no conoce las tablas que añadió la nueva build
   ([actualizaciones continuas](../architecture/rolling-upgrades.md), regla 14). El comando
   elimina solo los vínculos de fila huérfanos: filas de `entity_triple_rows` y `entity_edge_rows`
   en el alcance de cada borrado cuya memoria no tiene fila viva ni fila escrita desde el censo
   del borrado. Sin `--resume` las cuenta y sale con 1 si queda alguna.
   Los datos escritos después del borrado permanecen.
8. Activa una función que la build anterior no puede respetar solo cuando todas las réplicas ejecuten
   la nueva build: `memory_extract_agent_learnings`, ya que la build anterior no
   borra `loom.learnings_supporters`
   ([actualizaciones continuas](../architecture/rolling-upgrades.md), regla 23);
   `LOOM_DOCUMENT_OBJECT_STORE=s3`, ya que la build anterior lee el `data` vacío de una fila de bucket
   y responde a su descarga con `410 BLOB_GONE` (regla 9); y
   `LOOM_PG_PAYLOAD_CLEAR_ENABLED`, no antes de un horizonte de limpieza después de que la nueva
   build sirva sola, ya que la build anterior no marca una entrega tardía (regla 43).
9. Un despliegue con `LOOM_CODE_ASK_MEMORY_NAMESPACE` definido, ahora o en cualquier momento anterior,
   que pasa de una build cuyas respuestas `code.ask` en caché no nombran la memoria que citaron
   (una anterior a `086_code_answer_memory_scope.sql`) a una que sí lo hace, incluido un avance
   tras un rollback: una vez que la build más nueva sirva sola, sin ninguna reconstrucción del proveedor en curso, vacía la caché de respuestas con
   `TRUNCATE TABLE <LOOM_CH_DATABASE>.code_answers` (`loom.code_answers` por defecto). Las
   respuestas y tombstones de la build antigua no nombran ninguna memoria, y un borrado o redact que ejecutó
   durante el despliegue no conocía la tabla
   ([actualizaciones continuas](../architecture/rolling-upgrades.md), regla 36), de modo que ninguna saga puede encontrar
   lo que contienen. Una reconstrucción publica su copia de la tabla sobre ella, así que una que se ejecute
   durante el truncate devuelve las filas. Un truncate elimina las partes al instante, en cada
   réplica de una tabla replicada, sin mutación que esperar, y el usuario loom tiene
   el permiso. Cada pregunta limpiada cuesta una síntesis en su siguiente consulta.
10. Al pasar a una build cuyo borrado de usuario alcanza lo que alimentó un resumen consolidado de las
    memorias del usuario, incluido un avance tras un rollback: una vez que la build más nueva
    sirva sola, solicita de nuevo cada borrado de usuario sin `purge` solicitado antes (las
    filas `user_id` de `loom.erasure_requests`, conservadas 90 días), incluido uno que la build más nueva
    terminó: la saga de una build antigua dejó el historial del resumen, los chunks, los vectores,
    las filas del grafo y las copias compartidas
    ([actualizaciones continuas](../architecture/rolling-upgrades.md),
    regla 37).
    La nueva saga también borra lo que el usuario escribió desde entonces: solicítala para un usuario sin
    fila de memoria viva, o solo donde se desee. Un purge de usuario que completó una build antigua dejó
    las mismas filas, y ningún borrado de usuario posterior las encuentra: las claves del usuario ya no existen.
    Uno que se ejecutó pasado `shared.tombstone` también dejó las filas de búsqueda de copias compartidas
    de las memorias del propio usuario, sea cual sea la build que lo terminó. Una copia compartida que una build antigua
    promovió con `agent="*"` nombra `*` como su agente de origen, y ningún borrado del
    usuario que copió puede encontrarla. Lístalas y revisa cada una, con `memory.demote` para una
    que contenga texto de un usuario borrado:

    ```sql
    SELECT org, namespace, memory_key FROM loom.shared FINAL
    WHERE tombstoned_at IS NULL AND arrayExists(x -> splitByChar('/', x)[3] = '*', promoted_from)
    ```

11. Un despliegue de la autoridad Postgres que pasa a una build que mantiene los encabezados de documentos
    de conocimiento en Postgres (`002_knowledge_documents.sql`), incluido un avance tras un
    rollback: una vez que la build más nueva sirva sola, ejecuta de nuevo cada borrado
    completado mientras una réplica de una build antigua servía desde que la build más nueva sirvió por primera vez. El borrado de una build antigua
    no elimina ningún encabezado ni entrega: el texto de un documento borrado
    permanece en `loom.knowledge_heads` hasta que el borrado se ejecute de nuevo, y una entrega que dejó
    es publicada de nuevo por el worker de entregas
    ([actualizaciones continuas](../architecture/rolling-upgrades.md), regla 38). Hazlo antes de
    reintentar una entrega aparcada.
12. Un despliegue de la autoridad Postgres que pasa a una build que mantiene los trabajos de ingesta de
    conocimiento (`005_knowledge_jobs.sql`) o el catálogo de datasets
    (`007_knowledge_datasets.sql`) en Postgres, incluido un avance tras un rollback: una vez que la build más nueva sirva sola, ejecuta de nuevo cada borrado completado
    mientras una réplica de una build antigua servía desde que la build más nueva sirvió por primera vez. El
    borrado de una build antigua no elimina ningún trabajo de `loom.knowledge_jobs` ni fila de catálogo
    de `loom.knowledge_datasets`: el worker de la build más nueva proyecta ese trabajo de nuevo
    en `loom.knowledge_ingestions` y lo ejecuta, y las filas del catálogo permanecen hasta que el
    borrado se ejecute de nuevo ([actualizaciones continuas](../architecture/rolling-upgrades.md), reglas
    40 y 42). Las ejecuciones del paso 11 cubren por igual los encabezados, los trabajos y las filas del catálogo; basta una
    ejecución por borrado. Pide de nuevo también cada `knowledge.delete` y
    `knowledge.bulk_delete` que una build antigua respondió en esa ventana: eliminó los datasets de las
    fuentes solo de `loom.datasets`.
13. Al pasar a una build que mantiene fuentes de documentos en cualquier espacio de nombres, incluido un avance
    tras un rollback: una vez que la build más nueva sirva sola, retira las fuentes que un
    `namespace.delete`, `collection.delete` o borrado de espacio de nombres de una build antigua dejó
    activas ([actualizaciones continuas](../architecture/rolling-upgrades.md), regla 44). Las
    fuentes en un espacio de nombres eliminado:

    ```sql
    SELECT s.org, s.namespace, s.name
    FROM (SELECT org, namespace, name FROM loom.knowledge_sources FINAL WHERE active) AS s
    INNER JOIN (SELECT org, namespace FROM loom.namespaces FINAL WHERE NOT active) AS n
        ON s.org = n.org AND s.namespace = n.namespace
    ```

    y las fuentes registradas antes de que se completara un borrado de espacio de nombres que este no
    retiró (una que ejecutó una build antigua, o una que una build antigua añadió mientras se ejecutaba el borrado de una build más nueva):

    ```sql
    SELECT s.org, s.namespace, s.name, e.id
    FROM (
        SELECT org, namespace, name, active, created_at, updated_at
        FROM loom.knowledge_sources FINAL
    ) AS s
    INNER JOIN (
        SELECT id, org, completed_at,
               arrayJoin(if(scope_type = 'namespace', [scope_value],
                            if(JSONType(scope_value) = 'Array',
                               JSONExtract(scope_value, 'Array(String)'),
                               JSONExtract(scope_value, 'namespaces', 'Array(String)')))) AS namespace
        FROM loom.erasure_requests FINAL
        WHERE scope_type IN ('namespace', 'namespace_list') AND status = 'completed'
    ) AS e ON s.org = e.org AND s.namespace = e.namespace
    WHERE s.created_at <= e.completed_at AND (s.active OR s.updated_at > e.completed_at)
    ```

    Para la primera consulta, elimina cada fuente listada (`knowledge.source.remove`), o
    borra de nuevo su espacio de nombres con la build más nueva, que la retira. Para la segunda,
    elimina cada fuente listada con `knowledge.source.remove` y
    `delete_documents=true`, que retira los documentos que escribió (acepta una
    fuente ya retirada, por un borrado posterior de su espacio de nombres en un caso), y borra con
    `memory.delete` las filas de decisiones extraídas de ellos. Un borrado de espacio de nombres con la
    build más nueva hace ambas cosas, pero también borra todo lo escrito en el espacio de nombres desde
    el primer borrado; solicítalo de nuevo solo para un espacio de nombres en el que nadie haya escrito
    desde entonces. Una fuente registrada después del borrado no aparece en la lista: el borrado nunca
    la cubrió. La segunda lista es una lista de trabajo que se ejecuta una sola vez: una fuente eliminada como se indicó
    sigue apareciendo, ya que su fila de eliminación es más nueva que el borrado.

    Un borrado de espacio de nombres que inició una build antigua y terminó la build más nueva no necesita
    nada aquí: su censo final encuentra los documentos y decisiones que su fuente
    escribió y reabre sus pasos, de modo que la saga se completa solo cuando ya no existen.

14. Al pasar a una build cuyo borrado de espacio de nombres alcanza `loom.shared`, incluido un avance tras un
    rollback: una vez que la build más nueva sirva sola, limpia las filas promovidas que el borrado de espacio de nombres de una build
    antigua dejó ([actualizaciones continuas](../architecture/rolling-upgrades.md),
    regla 45). La consulta siguiente lista cada espacio de nombres que un borrado `namespace` o `namespace_list`
    del registro (conservado 90 días) alcanzó y que contiene una fila promovida, con la
    hora en que ejecutó su censo el último sin purge (`erased_at`) y con purge (`purged_at`).
    Las sentencias siguientes toman las filas promovidas antes de ese segundo, de modo que una fila promovida
    después del censo permanece, y también puede permanecer una promovida antes dentro del propio segundo del censo,
    que los segundos enteros de `promoted_at` no pueden distinguir; el paso de la build más nueva ya tomó esa fila en un borrado que ejecutó.
    Solo un `namespace_list` hace purge, y la fila `namespace` de una build antigua puede contener el indicador
    que se le pidió.

    ```sql
    SELECT org, namespace,
           maxIf(counted_at, NOT purged) AS erased_at,
           maxIf(counted_at, purged) AS purged_at
    FROM (
        -- The census's time: a row promoted after it was never in the erasure's reach.
        SELECT org, ifNull(verified_at, completed_at) AS counted_at,
               scope_type = 'namespace_list' AND purge AS purged,
               arrayJoin(if(scope_type = 'namespace', [scope_value],
                            if(JSONType(scope_value) = 'Array',
                               JSONExtract(scope_value, 'Array(String)'),
                               JSONExtract(scope_value, 'namespaces', 'Array(String)')))) AS namespace
        FROM loom.erasure_requests FINAL
        WHERE scope_type IN ('namespace', 'namespace_list') AND status = 'completed'
    )
    -- Only a namespace that holds a promoted row has anything to clear.
    WHERE (org, namespace) IN (SELECT org, namespace FROM loom.shared)
    GROUP BY org, namespace
    ```

    Para cada uno, ejecuta lo que ejecuta el paso de la build más nueva, sobre cada versión promovida antes del
    censo del borrado, incluida una versión que una promoción posterior de la misma clave oculta a `FINAL`.
    Donde `purged_at` esté definido, elimínalas:

    ```sql
    ALTER TABLE loom.shared DELETE
    WHERE org = '<org>' AND namespace = '<namespace>' AND promoted_at < '<purged_at>'
    SETTINGS mutations_sync = 2
    ```

    y donde `erased_at` esté definido, marca con tombstone las vivas que queden:

    ```sql
    ALTER TABLE loom.shared UPDATE tombstoned_at = toDateTime('<the time you run it>')
    WHERE org = '<org>' AND namespace = '<namespace>' AND tombstoned_at IS NULL
      AND promoted_at < '<erased_at>'
    SETTINGS mutations_sync = 2
    ```

    El ajuste espera cada sentencia en todas las réplicas. La hora se escribe como
    literal, como la enlaza el paso, porque una tabla replicada rechaza una mutación que llama
    a una función no determinista como `now()`. Un borrado de espacio de nombres solicitado
    antes de los 90 días del registro no aparece en la lista, y las filas promovidas que dejó no se pueden distinguir
    de las de un espacio de nombres vivo; permanecen como las dejó la build antigua.

15. Al pasar a una build donde cada comando de la CLI que declara una org (un comando del índice de código, una
    importación) comprueba su despliegue: bajo cualquiera de las autoridades su usuario de ClickHouse necesita
    `SELECT` sobre `loom.storage_authority`, y el comando se niega contra un despliegue que registra
    otra autoridad. Bajo la autoridad Postgres también necesita `LOOM_PG_DSN`
    y lee el estado de la celda (con `LOOM_CELL` sin definir, en una sesión propia). Con
    `LOOM_CELL` definido, un comando que no escribe para ninguna org pero cambia los almacenes de la celda
    (`loom settings set`, `loom secrets reseal`, `loom code compact` y el resto) o
    revoca el acceso comprueba su despliegue del mismo modo y lee la fila `first` de la celda en una
    sesión propia. Concede al rol de Postgres con el que se conecta:
    - con `LOOM_CELL` sin definir, `USAGE` sobre el esquema `loom`, `SELECT` sobre `loom.cell_first`,
      `loom.tenancy_mode` y `loom.authority`, `SELECT (updated_at)` sobre `loom.cell_self`
      y `SELECT (since)` sobre `loom.tenancy` (los permisos por columna mantienen las huellas y
      las orgs fuera de su alcance), y un `CONNECTION LIMIT` de al menos `LOOM_PG_POOL_MAX_SIZE`
      veces el número de comandos de ese tipo que se ejecutan a la vez, ya que cada uno abre su propio pool para
      sus escrituras (la lectura de estado reintenta brevemente con el límite lleno, luego se niega);
    - con `LOOM_CELL` definido, lo mismo con `SELECT` sobre `loom.cell_self` y `loom.tenancy`
      completos y, para registrar una nueva org mientras la primera celda está en `recording`, `INSERT` sobre
      `loom.tenancy` y `UPDATE (since)` sobre `loom.tenancy_mode`; revoca ambos con
      `LOOM_CELL_ROUTER_TOKEN` cuando la celda se adjunte. Los ajustes de su bucket de documentos
      (`LOOM_DOCUMENT_S3_BUCKET`, `LOOM_DOCUMENT_S3_PREFIX`) son iguales a los de los servidores.

    Una escritura que un rol con solo los permisos por columna hace en una celda que ya pasó de `recording` es
    rechazada como un error de permisos (SQLSTATE 42501) en lugar de `LT001`.

16. Antes del attach de una primera celda, cada imagen de CLI y de CI ejecuta una compilación que lee ese estado, y cada CLI que escribe para una organización tiene `LOOM_CELL` definido. Ningún CLI envía un heartbeat, así que el attach tampoco puede comprobar ninguna de las dos cosas, y un CLI de una compilación anterior no es rechazado en una celda `enforced` ([celdas](../architecture/cells.md#failure-modes)).

17. Paso a una compilación cuyos procesos de la primera celda leen el estado de su celda desde el router de forma periódica: todas las réplicas del router (`loom router serve`) ejecutan esta compilación antes que los procesos de cualquier celda. Cada proceso de una primera celda preparada consulta la ruta de estado del router desde su primera lectura del estado de la celda hasta que lee `serving`, y un router de una compilación anterior lee el directorio en cada una de esas llamadas: aproximadamente una lectura por proceso de servidor cada `min(LOOM_INSTANCE_HEARTBEAT_SECONDS, LOOM_TENANCY_MODE_CACHE_S)`, es decir, 1,600 lecturas por segundo para mil pods de ocho workers con los valores por defecto; un router de esta compilación las responde con una lectura por réplica cada `LOOM_ROUTER_DIRECTORY_CACHE_S` ([celdas](../architecture/cells.md#the-heartbeats-keys)).

Mientras se aplica una migración, la compilación anterior sigue sirviendo: toda migración es aditiva. Las reglas que permiten que las dos compilaciones sirvan en paralelo están en [actualizaciones progresivas](../architecture/rolling-upgrades.md).

<a id="rollback"></a>

## Reversión

Vuelva a desplegar la imagen anterior; con el par, `./redeploy.sh --rollback`. Antes de hacer rollback
a una build sin aprendizajes de turnos del asistente, desactive `memory_extract_agent_learnings`
y elimine las filas de `loom.learnings_supporters` y las filas `assistant_turn` de
`loom.learnings_queue`
([extracción](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/extraction.md#rules-the-assistant-states-about-itself)).
La build anterior entrega una regla de bot promovida a todos los agentes del espacio de nombres, no solo a
su bot. Para evitarlo, elimine con `knowledge.delete` los documentos etiquetados `source-kind:assistant_turn`
antes del rollback; de lo contrario, acepte la entrega más amplia.
Un borrado de espacio de nombres que ejecute la build anterior una vez que existan guías de espacio de nombres deja la guía de ese
espacio de nombres; ejecute el borrado de nuevo cuando la build más reciente vuelva a servir
([actualizaciones progresivas](../architecture/rolling-upgrades.md), regla 24). Un borrado de espacio de nombres
que ejecute la build anterior deja activas las filas promovidas del espacio de nombres; el paso 14 del procedimiento
de actualización las enumera cuando la build más reciente vuelve a servir (regla 45).
Tras un rollback a una build sin máximos de token para bots, los tokens de cada bot duran una hora,
y un bot que guarde esa build conserva 3600 como máximo; vuelva a aplicar uno más corto o más largo
cuando la build más reciente vuelva a servir
([actualizaciones progresivas](../architecture/rolling-upgrades.md), regla 26).
Un borrado de usuario que ejecute la build anterior deja lo que alimentó un resumen de las memorias del usuario;
solicite de nuevo cada uno de los que se ejecutaron sin `purge` cuando la build más reciente vuelva a servir, como en el paso
10; una purga que se completó no se vuelve a encontrar (regla 37).
No haga rollback más allá de la build que añade el bucket de documentos mientras alguna fila del catálogo
nombre un objeto en él: una build anterior responde esas descargas con `410 BLOB_GONE`, su
borrado elimina las filas y no puede alcanzar sus objetos, y su barrido nunca recorre el
bucket ([dónde se almacenan los originales](https://github.com/ClickHouse/loom/blob/main/docs/features/knowledge.md#where-originals-are-stored)).
Tras un rollback a una build anterior a `086_code_answer_memory_scope.sql` con un espacio de nombres de memoria
vinculado a `code.ask`, limpie la caché de respuestas como en el paso 9 dos veces: una cuando la build más antigua
sirva sola, y otra cuando la build más reciente vuelva a servir. La migración ya está
en el registro, así que el roll forward no aplica nada que provoque la segunda.
Desactive `LOOM_PG_PAYLOAD_CLEAR_ENABLED` antes del rollback, y vuelva a activarlo no antes de
un horizonte de limpieza después de que la build más reciente sirva sola de nuevo; una vez que una pasada de limpieza
se haya ejecutado, ejecute `loom reconcile` solo con una build que incluya
`010_revision_delivered_at.sql`, inicie el despliegue con esa build después, y haga
rollback solo cuando la build más reciente no deba ninguna entrega reclamable de una revisión limpiada, lo cual
esta consulta devuelve como 0 (regla 43):

```sql
SELECT count(*) FROM loom.memory_deliveries d
JOIN loom.memory_revisions r USING (org, namespace, seq)
WHERE r.row IS NULL AND NOT d.settled AND d.lease_until <> 'infinity';
```

Hasta entonces, deje que la build más reciente las entregue. La build anterior nunca reclama una
entrega aparcada, así que una aparcada puede quedarse. Mientras la build anterior sirve, un reintento
omite las revisiones limpiadas
([operaciones de Postgres](https://github.com/ClickHouse/loom/blob/main/docs/operations/postgres.md#what-the-boot-checks)), y no se ejecuta `loom reconcile`:
liberaría las retenciones de reconcile a la build anterior.
Antes de hacer rollback de una celda preparada (una con una fila `loom.cell_first`) a una build
sin los triggers de org de [celda](../architecture/cells.md), detenga todos los pods de la build más reciente,
y luego ejecute solo la eliminación de triggers de
[Un proceso con LOOM_CELL sin definir queda bloqueado](https://github.com/ClickHouse/loom/blob/main/docs/operations/troubleshooting.md#a-process-with-loom_cell-unset-is-gated),
no su transacción de estado (la celda sigue preparada), y vuelva a ejecutarla si un pod más reciente
arranca con migraciones activadas antes de que termine el rollback: las sesiones de la build más antigua no envían
marcador de tenencia, así que una vez que la build más reciente haya creado los triggers y la guarda `loom.tenancy`,
se rechaza cada una de sus escrituras para una org de la que la celda no tiene fila de tenencia (en una celda posterior antes de attach, toda org de tenant), igual que su fila de tenencia
para cada org colocada de nuevo, y sus demás escrituras pagan la lectura de estado de los triggers.
No hay ninguna migración que deshacer: la build anterior lee y escribe el esquema más reciente.
Sus escrituras de versión en `loom.memory` y en las demás tablas de memoria `ReplacingMergeTree`
conservan las columnas que no conoce
([actualizaciones progresivas](../architecture/rolling-upgrades.md)).

No hay migraciones inversas. Para volver a un esquema anterior, restaure una copia de seguridad hecha
antes de la actualización ([copia de seguridad y recuperación](https://github.com/ClickHouse/loom/blob/main/docs/operations/backup-and-recovery.md)).

<a id="downgrading-an-installer-install"></a>

### Volver a una versión anterior de una instalación con el instalador

Cada bundle del instalador indica la versión de ClickHouse con la que se construyó, así que instalar
una versión anterior de Loom también ejecuta un ClickHouse anterior sobre un volumen que escribió el más reciente,
y puede que no pueda leerlo. Para hacer rollback solo de Loom, conserve el ClickHouse más reciente: antes de ejecutar
el instalador anterior (inicia la pila de inmediato), fije la imagen que ClickHouse ejecuta ahora
en el `.env` del directorio de instalación. Esto imprime la línea que debe poner allí, reemplazando cualquier
línea `LOOM_CLICKHOUSE_IMAGE` anterior:

```bash
dir=${LOOM_INSTALL_DIR:-$HOME/.loom}
echo "LOOM_CLICKHOUSE_IMAGE=$(docker inspect -f '{{.Config.Image}}' \
  "$(docker compose --project-directory "$dir" ps -q clickhouse)")"
```

Luego ejecute el instalador de la versión anterior indicando su versión, ya que un instalador más antiguo sobre una
instalación más reciente se niega en caso contrario:

```bash
curl -fsSL https://github.com/ClickHouse/loom/releases/download/vX.Y.Z/install.sh -o loom-install.sh && LOOM_VERSION=X.Y.Z sh loom-install.sh && rm loom-install.sh
```

Ejecútelo desde su directorio personal, o desde otro
[directorio en el que solo usted pueda escribir](../getting-started/quickstart.md#fast-path-released-image-no-clone).

El instalador conserva `.env`, así que la fijación mantiene ClickHouse también en actualizaciones posteriores:
elimine la línea `LOOM_CLICKHOUSE_IMAGE` antes de instalar una versión más reciente de Loom, y no
deje `LOOM_CLICKHOUSE_IMAGE` exportada en el shell, ya que el instalador vuelve a escribir en `.env` un valor que
se le da. Para volver
en ambos, restaure una copia de seguridad hecha antes de la actualización.

<a id="what-stays-the-same-across-an-upgrade"></a>

## Qué se mantiene igual en una actualización

| En una actualización                                | Qué se garantiza                                                                                                                         | Lo garantiza                              |
| --------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------- |
| La compilación anterior con el nuevo esquema        | sigue leyendo y escribiendo todas las tablas que conocía, por lo que un rollback no requiere ningún paso de esquema                      | `tests/unit/test_migrations_additive.py`  |
| Un archivo de migración ya aplicado                 | nunca se edita ni se elimina; una corrección es un archivo nuevo                                                                         | el checksum del registro; `--dry-run --strict` |
| Un cliente que envía un campo que el servidor eliminó | se atiende; el campo aparece en `ignored_args`                                                                                         | `server/transport/arg_check.py`           |
| La compilación anterior y la nueva sirviendo a la vez | cada una lee lo que escribió la otra, incluidas las columnas nuevas, salvo una funcionalidad que se activa solo después del despliegue (paso 8) | `make test-mixed-version`                 |

Una actualización puede cambiar o eliminar operaciones y campos. Consulta qué sirve un despliegue
con el catálogo de capacidades
([API HTTP](https://github.com/ClickHouse/loom/blob/main/docs/reference/http-api.md#discovering-what-a-deployment-serves)) y lista lo que
cambió entre dos compilaciones con `scripts/codegen/wire_changelog.py <previous> <new>`
([generación de código](https://github.com/ClickHouse/loom/blob/main/docs/development/code-generation.md#wire-changelog)).

<a id="related"></a>

## Documentación relacionada

- [Despliegue](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#zero-downtime-pair): el par que convierte una actualización en un intercambio
- [Migraciones](https://github.com/ClickHouse/loom/blob/main/docs/development/migrations.md): las reglas que sigue una nueva migración
- [Actualizaciones progresivas](../architecture/rolling-upgrades.md): las reglas para las compilaciones N-1 y N
- [Solución de problemas](https://github.com/ClickHouse/loom/blob/main/docs/operations/troubleshooting.md#migrations-pending-or-checksum-drift)
