<!-- i18n: source=docs/operations/postgres.md blob=d087a158c4304a8400459a4a96b0f61030b0fabb model=claude-sonnet-5-5 -->
> Esta página es una traducción automática de la [página en inglés](https://github.com/ClickHouse/loom/blob/main/docs/operations/postgres.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/operations/postgres.md), which is authoritative.

<a id="postgres-memory-authority"></a>

# Autoridad de memoria en Postgres

Esta guía configura un despliegue de Loom cuyas memorias activas y cabeceras de documentos de conocimiento se almacenan en Postgres, ya sea en ClickHouse Managed Postgres o en un Postgres autoalojado, y explica qué comprobar antes de ponerlo en servicio. El funcionamiento interno se describe en [autoridad de almacenamiento](../architecture/storage-authority.md).

La autoridad se elige una sola vez, en el primer arranque de un despliegue. Un ClickHouse que ya contiene memorias conserva la autoridad de ClickHouse; inicie un despliegue con Postgres sobre un ClickHouse vacío.

<a id="requirements"></a>

## Requisitos

- Postgres 16 o superior. No se necesita ninguna extensión.
- Una base de datos y un rol que sea su propietario (Loom crea en ella el esquema `loom`).
- El rol puede ejecutar `pg_logical_emit_message` (por defecto el público puede; de lo contrario, el arranque se rechaza y nombra el `GRANT`).
- Para `LOOM_PG_DURABILITY=synchronous` (el valor por defecto): al menos un standby síncrono
  en otro dominio de fallo, de modo que una escritura confirmada sobreviva a la pérdida del primario.
- `wal_level = logical` si la base de datos también alimentará una herramienta de replicación; Loom en sí
  no lo necesita.

<a id="settings"></a>

## Configuración

| Ajuste                                           | Valor                                                                                                                                          |
| ------------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------- |
| `LOOM_STORAGE_AUTHORITY`                         | `postgres`                                                                                                                                     |
| `LOOM_PG_DSN`                                    | `postgres://loom:<password>@<host>:5432/<database>?sslmode=verify-full&sslrootcert=<CA file>`, el endpoint directo, nunca un pooler de transacciones |
| `LOOM_PG_POOL_MIN_SIZE`, `LOOM_PG_POOL_MAX_SIZE` | conexiones por proceso de Loom (2 y 20); dimensione `max_connections` para cada proceso de cada réplica                                       |
| `LOOM_PG_DURABILITY`                             | `synchronous` (se niega a iniciar sin un standby síncrono) o `local`                                                                           |

Indique la autoridad de certificación del servidor y exija `sslmode=verify-full`:
comprueba el certificado y el nombre del host, de modo que nada en la ruta pueda
hacerse pasar por la autoridad ni leer la memoria o la contraseña. `require` y los
modos inferiores cifran sin comprobar con quién se habla, y son solo para un servidor
de desarrollo local.

Loom gestiona su propio pool de conexiones. Apunte `LOOM_PG_DSN` más allá de cualquier
pooler que entregue al cliente una conexión de servidor distinta por transacción
(transaction pooling): las migraciones de Loom mantienen un bloqueo advisory de sesión
y su driver prepara sentencias, y un pooler de ese tipo no conserva ninguna de las dos
cosas entre transacciones. El `LOOM_PG_DSN` de una [celda](../architecture/cells.md)
apunta directamente a Postgres, sin ningún pooler de ningún modo: sus procesos marcan
cada sesión con el parámetro de inicio `loom.tenancy_checked`, que un pooler de sesión
rechaza o descarta (entonces sus triggers de org rechazan las escrituras del propio
Loom para una org de la que la celda no tiene ninguna fila, y la protección
`loom.tenancy` rechaza su fila de tenencia para cada org recién asignada), y
`loom doctor` señala la sesión que lo perdió. Los scripts y los cargadores masivos
nunca deben establecer `session_replication_role = replica` en una celda: omite los
triggers de org y la protección.

Todas las réplicas del despliegue, y su proceso worker, usan los mismos valores. La
lista completa, con los ajustes de admisión y de entrega, está en la
[referencia de configuración](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md).

<a id="clickhouse-managed-postgres"></a>

## Postgres administrado de ClickHouse

1. Crea el servicio con **dos standbys de HA**. Con un solo standby el servicio replica de forma asíncrona, y una escritura confirmada se puede perder junto con el primario.
2. Crea la base de datos y el rol, concede al rol `pg_read_all_stats` para que Loom pueda ver qué standbys están conectados, y toma la cadena de conexión **directa**, en el puerto 5432, de los detalles de conexión del servicio, no la del pooler: el pooler del servicio funciona en modo de transacción.
3. Define los [ajustes](#settings) e inicia Loom. Comprueba que `GET /v1/health?include=postgres` informe `postgres.standby` `synchronous`: el arranque rechaza un `synchronous_standby_names` vacío y emite una advertencia cuando hay menos standbys conectados de los que nombra ([qué comprueba el arranque](#what-the-boot-checks)).

La conmutación por error, la ubicación de los standbys y el aislamiento del antiguo primario son responsabilidad del servicio. En Kubernetes, los valores `postgres` del chart definen los [ajustes](#settings) ([ClickHouse Cloud y Postgres administrado](https://github.com/ClickHouse/loom/blob/main/docs/operations/clickhouse-cloud.md)).

<a id="self-hosted-postgres"></a>

## Postgres autoalojado

Ejecute Postgres bajo un gestor de HA que mantenga un standby síncrono, aísle (fence) al primario anterior en la promoción y promueva solo un standby que contenga todos los commits confirmados.
Configúrelo en su modo síncrono estricto: se requiere al menos un standby síncrono, y las escrituras se rechazan en lugar de ser confirmadas solo por el primario cuando no hay ninguno conectado. Un gestor que no es estricto vacía `synchronous_standby_names` cuando su standby se desconecta, y entonces el primario confirma los commits por sí solo.

Loom comprueba la configuración del standby al arrancar, y `GET /v1/health?include=postgres` informa `postgres.standby` mientras está en servicio: `synchronous` cuando los standbys que un commit espera (`n` de `FIRST n`, `ANY n` o `n (...)`, y si no, uno) están conectados; `primary_only` cuando no se nombra ninguno, de modo que solo el primario confirma los commits; `waiting` cuando hay menos conectados, de modo que los commits esperan hasta que lo estén; `null` cuando no se pudo leer el estado, porque Postgres no respondió o el rol no puede ver `pg_stat_replication`.

Apunte `LOOM_PG_DSN` al endpoint primario del gestor (el servicio o la dirección que sigue al líder), nunca a un único host, de modo que una promoción mueva a Loom con ella.

Un primario con un standby y sin gestor, como lo inicia `dev/ci_start_postgres.sh` para las pruebas, muestra la configuración:

```text
synchronous_commit = on
synchronous_standby_names = 'FIRST 1 (loom_standby)'
```

<a id="what-the-boot-checks"></a>

## Qué comprueba el arranque

| Comprobación | Se niega a arrancar cuando                                                                                                                                                                                                                                                                      |
| ------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Conexión     | `LOOM_PG_DSN` está vacío o Postgres no responde                                                                                                                                                                                                                                                 |
| Migraciones  | se editó un archivo ya aplicado de `schemas/pg_migrations/`, o falta en esta compilación uno anterior al último aplicado                                                                                                                                                                        |
| Durabilidad  | con `LOOM_PG_DURABILITY=synchronous`, `synchronous_standby_names` está vacío; con cualquier valor, el rol no puede ejecutar `pg_logical_emit_message`                                                                                                                                            |
| Autoridad    | el despliegue registró la autoridad de ClickHouse, este ClickHouse contiene memoria bajo ella, o este Postgres pertenece a otro despliegue                                                                                                                                                      |
| Estado de la celda | falla la primera lectura de las filas de estado de la celda (`loom.cell_first`, `loom.tenancy_mode`, `loom.cell_self`, `loom.tenancy`); con migraciones de Postgres pendientes, el proceso arranca bloqueado y no listo hasta que una lectura se completa ([solución de problemas](https://github.com/ClickHouse/loom/blob/main/docs/operations/troubleshooting.md#a-process-with-loom_cell-unset-is-gated)) |

Una migración espera como máximo 5 s a un bloqueo de tabla y se intenta cinco veces; un
arranque que registra `Postgres migration <file> waited too long for a lock` cuatro
veces y después falla con `LockNotAvailableError` encontró una transacción larga que
retiene la tabla: termínela (`pg_stat_activity` la nombra) y vuelva a arrancar. Un
arranque que encuentra a otro arranque migrando lo espera, sin límite, y arranca cuando
sus migraciones terminan.

Cada conexión de Loom establece `synchronous_commit = on` por sí misma, sea cual sea el
valor por defecto del servidor. El arranque emite una advertencia cuando hay menos
standbys conectados de los que un commit espera, y cuando su rol no puede ver
`pg_stat_replication`. Mientras no estén conectados, un commit los espera, hasta
`LOOM_PG_STATEMENT_TIMEOUT_MS` más un segundo, y luego responde 503
`POSTGRES_UNAVAILABLE` con `outcome_unknown`: la espera cancelada deja el commit solo en
el primario. El worker de entrega lo publica en ClickHouse únicamente cuando los standbys
lo contienen, de modo que una conmutación por error intermedia no deja nada de él en
ClickHouse
([autoridad de almacenamiento](../architecture/storage-authority.md#only-durable-revisions-are-published)).
Un publicador que envía el commit de otra sesión lo vuelve a leer en una transacción que
confirma un mensaje lógico transaccional con el prefijo `loom.durable`, por lo que un
consumidor de captura de cambios de la base de datos recibe uno por cada lectura de ese
tipo; el arranque rechaza un rol que no puede escribir uno.

`GET /v1/health` informa `storage_authority` y, con Postgres, `pg_ok`; con
`?include=postgres` también un bloque `postgres` con `standby`, `parked_deliveries`,
`parked_audits`, `parked_knowledge_deliveries` y `backlog` (las entregas pendientes y
aparcadas de cada fuente, contadas hasta `LOOM_PG_BACKLOG_COUNT_CAP`, `capped` más allá,
la antigüedad de la más antigua y la de la lectura,
[SLO del pipeline](https://github.com/ClickHouse/loom/blob/main/docs/operations/monitoring.md#pipeline-slos)), sentencias que la respuesta por defecto omite en una sonda de
liveness. `pg_ok` es false cuando una consulta en el pool de peticiones no obtuvo
respuesta en dos segundos, incluida la situación en que todas las conexiones del pool
están ocupadas. `/v1/health/ready` no espera a Postgres: una réplica que no puede
alcanzarlo sigue sirviendo los planos que no lo usan. Responde 503
`POSTGRES_UNAVAILABLE` para las operaciones de memoria, para toda operación de knowledge
que lee la fila actual de un documento desde su cabeza o la confirma, para la búsqueda y
las lecturas que comprueban cabezas (`knowledge.search`, `knowledge.outline`,
`knowledge.synthesize`), y para las operaciones de datasets, que allí leen o confirman el
catálogo de datasets (`knowledge.dataset_get`, `knowledge.dataset_approve`,
`knowledge.query`). Las operaciones de knowledge que leen o escriben solo en ClickHouse,
como `knowledge.list` y `knowledge.dedup_resolve`, siguen sirviendo.
`knowledge.ingest`, `knowledge.ingest_status` y `knowledge.ingest_cancel` también
responden 503, ya que cada una lee o confirma su trabajo allí (`loom.knowledge_jobs`); una
ejecución detenida por una caída cuenta ese intento y se ejecuta de nuevo cuando Postgres
responde.

`parked_deliveries` cuenta cada entrega de memoria con un arrendamiento sin fin:
revisiones confirmadas en Postgres que ClickHouse rechazó `LOOM_PG_DELIVERY_MAX_ATTEMPTS`
veces, las versiones reemplazadas cuyo payload borrado no dejó nada que enviar (abajo), y
las entregas que `loom reconcile` retiene (`last_error` `held by loom reconcile`) hasta
que las libera. Una cuyo `targets` no contiene ninguno de 1, 2 y 4 tiene su fila de
memoria en ClickHouse (también su documento de búsqueda cuando le corresponde), su fila de
auditoría allí o en `loom.audit_outbox`, y solo debe retiradas: 8, el registro de su
decisión eliminada, que `decision.chain` sigue sirviendo hasta que se retira; 16, los
vectores de la versión que reemplazó, que la búsqueda sigue encontrando hasta que se
retiran (para una fila `high`, su valla oculta mientras tanto los vectores del modelo en
servicio). Una que agotó sus intentos no se reintenta; su `last_error` en
`loom.memory_deliveries` contiene el tipo del error y su código de ClickHouse, estado HTTP
o SQLSTATE de Postgres, nunca su texto, que podría citar la memoria. Esta sentencia
reintenta una (nunca una retención de reconcile, que `loom reconcile` libera por sí
mismo):

```sql
UPDATE loom.memory_deliveries SET attempts = 0, held = false, lease_until = now() WHERE …
```

Una vez que se ha ejecutado la pasada de [borrado de payloads](../architecture/storage-authority.md#payload-clearing),
una compilación sin `010_revision_delivered_at.sql` no puede enviar una revisión cuyo
payload fue borrado: mientras esa compilación sirve, un reintento las excluye, añadiendo
esto a su `WHERE` (una sonda de clave primaria por entrega):

```sql
AND NOT EXISTS (SELECT 1 FROM loom.memory_revisions r
                WHERE r.org = memory_deliveries.org
                  AND r.namespace = memory_deliveries.namespace
                  AND r.seq = memory_deliveries.seq
                  AND r.row IS NULL)
```

Una publicación que termina el proceso del worker (falta de memoria con un valor enorme)
cuenta como un intento cuando la siguiente pasada toma su reclamo (`last_error`
`stopped mid-publish`): al llegar al límite la revisión se aparca o, cuando lo único que
aún debe es su documento de búsqueda, recibe un stub que el worker de embeddings rellena.

Una publicación que supera el plazo de sentencia de ClickHouse cuenta como fallo propio de
las filas, ya que ClickHouse aún responde: tras una ralentización que aparcó revisiones
sanas, la sentencia de reintento anterior con `WHERE attempts >= …` las reintenta todas.

Una aparcada con `last_error` `payload cleared, nothing to send` es una versión cuyo
payload borró la pasada de [borrado de payloads](../architecture/storage-authority.md#payload-clearing)
y que una versión posterior de su clave ha reemplazado: ClickHouse sirve la cabeza de la
clave, y el contenido de esa versión ya no existe (una restauración que omitió el
[procedimiento operativo](https://github.com/ClickHouse/loom/blob/main/docs/operations/backup-and-recovery.md#the-postgres-journal-and-a-clickhouse-restore)). Su fila de
auditoría tampoco se envía de nuevo: permanece en el `audit` de la revisión en
`loom.memory_revisions`. Reintentar la entrega no puede tener éxito; una vez anotada,
elimine su fila:

```sql
DELETE FROM loom.memory_deliveries
WHERE lease_until = 'infinity' AND last_error = 'payload cleared, nothing to send';
```

El worker de entrega de knowledge publica una entrega a la vez, cada retirada por
separado: hasta dos inserciones por entrega y `LOOM_PG_DELIVERY_BATCH_ROWS` entregas por
pasada, dentro de un arrendamiento de `LOOM_PG_DELIVERY_LEASE_SECONDS`, que con un backlog
suele terminar la pasada antes. Una pasada que el fin del arrendamiento interrumpe libera
el resto sin contarlo, y la siguiente pasada las toma.

`parked_knowledge_deliveries` cuenta los documentos de knowledge confirmados en Postgres
cuya fila de `loom.documents` ClickHouse rechazó `LOOM_PG_DELIVERY_MAX_ATTEMPTS` veces: la
cabeza sirve `knowledge.get` y la búsqueda, que omite los fragmentos de la versión
anterior que ClickHouse lee como vigentes hasta que se ejecuta la retirada de la entrega
([búsqueda de knowledge](../architecture/storage-authority.md#knowledge-search)). La
retirada de un borrado aparcado se ejecuta antes que su fila: `knowledge.get` lo responde
como borrado, la búsqueda no sirve ninguno de sus fragmentos, y `knowledge.restore`
responde 503 mientras una publicación de la fuente aún pueda llegar: hasta que la entrega
aterriza, y durante el límite de inserción después. El barrido de originales conserva el
original que nombra un documento vigente aparcado, y también lo hace el borrado de un
usuario distinto de su autor. Su `last_error` en `loom.knowledge_deliveries` contiene el
tipo y el código del error. Una entrega de un alcance que una compilación anterior borró
durante una actualización progresiva no se reintenta: ejecute primero ese borrado de nuevo
([actualizaciones progresivas](../architecture/rolling-upgrades.md)). Esta sentencia
reintenta una:

```sql
UPDATE loom.knowledge_deliveries SET attempts = 0, lease_until = now() WHERE …
```

`parked_audits` cuenta las filas de `loom.audit_outbox` que ClickHouse rechazó el mismo
número de veces, dejadas allí con su `last_error`: las filas de auditoría de un borrado, y
las que un reclamo aplazó porque el titular al que reemplazó aún puede insertarlas. Esta
sentencia reintenta una:

```sql
UPDATE loom.audit_outbox SET attempts = 0, due_at = now() WHERE …
```

<a id="rolling-upgrades-and-rollback"></a>

## Actualizaciones continuas y rollback

Cada réplica debe ejecutar una compilación que registre la autoridad. Un despliegue de la autoridad de Postgres no puede volver a una compilación que no la incluya: dicha compilación no lee ningún registro y escribe en `loom.memory` directamente ([actualizaciones continuas](../architecture/rolling-upgrades.md)).

<a id="backup"></a>

## Copia de seguridad

Haz la copia de seguridad de Postgres con las herramientas del proveedor o del gestor, y la de ClickHouse con las suyas. Una restauración es de un solo almacén, seguida de `loom reconcile`, que vuelve a ejecutar en él lo que el otro almacén registró después del punto de restauración ([restaurar un solo
almacén](https://github.com/ClickHouse/loom/blob/main/docs/operations/backup-and-recovery.md#restoring-one-store)). Un Postgres de otro despliegue se niega a iniciar.
