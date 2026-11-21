<!-- i18n: source=docs/development/migrations.md blob=bbea754e8750b1abe4dfb51af0636ead2246778b model=claude-sonnet-5-5 -->
> Esta página es una traducción automática de la [página en inglés](https://github.com/ClickHouse/loom/blob/main/docs/development/migrations.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/development/migrations.md), which is authoritative.

<a id="migrations"></a>

# Migraciones

Esta página contiene las reglas para escribir una migración. La forma en que un despliegue las aplica, y los pasos de actualización y reversión, están en [actualizaciones](https://github.com/ClickHouse/loom/blob/main/docs/operations/upgrades.md#how-migrations-run). [`schemas/README.md`](https://github.com/ClickHouse/loom/blob/main/schemas/README.md) describe el directorio `schemas/` y enumera las herramientas del operador.

<a id="the-journal-and-the-ledger"></a>

## El journal y el ledger

- El esquema es un journal de archivos SQL numerados en `schemas/migrations/`, aplicados en
  orden de prefijo numérico por `server/core/schema/migrate.py`. `000_migrations.sql` crea el ledger;
  `016_baseline.sql` es el esquema inicial; cada archivo posterior es un delta.
- Cada archivo aplicado se registra en `loom._migrations` con el SHA-256 de su contenido.
  Una nueva ejecución aplica solo los archivos nuevos. Un archivo modificado es **checksum drift** (deriva de checksum) y un archivo que el
  repositorio ya no tiene es una **fila solo del ledger**; `loom migrate --dry-run --strict`
  (un paso de CI y la verificación previa al despliegue) falla en ambos casos. Un archivo renombrado con su texto
  sin cambios no es una fila solo del ledger: su SHA-256 lo identifica, y se registra con el nuevo
  nombre sin ejecutarse de nuevo.
- Cada archivo usa `loom.<table>` y `loom_telemetry.<table>`. El runner los asigna
  a `LOOM_CH_DATABASE` y `LOOM_CH_TELEMETRY_DATABASE` al enviar cada sentencia
  (`server/core/ch_names.py`), de modo que el checksum se calcula sobre el archivo tal como está escrito. Una base de datos
  se compara por valor como `currentDatabase()` o un nombre enlazado, nunca como un literal `'loom'`
  (`tests/unit/test_ch_names.py`). Un archivo que cambia la clave o el TTL de una tabla cuyo
  nombre empieza con `_`, o que renombra o intercambia una, también actualiza su fila en [disposición del
  almacenamiento](https://github.com/ClickHouse/loom/blob/main/docs/architecture/storage-layout.md) y la protección de remodelado de
  `tests/unit/test_storage_layout_rows.py`.

<a id="numbering"></a>

## Numeración

Un archivo se llama `NNN_description.sql`. El runner ordena por el prefijo entero, no por el
nombre (`server/core/schema/journal.py`, `migration_order`), de modo que `1030_x.sql` se ejecuta después de `220_y.sql`. Toma el siguiente número libre, revisando `main`
y los pull requests abiertos, y consérvalo: dos archivos con el mismo número se aplican igualmente, pero en el
orden léxico de sus sufijos, que no es el orden que nadie pretendía, y un rebase
que renombra uno no vuelve a aplicar nada. `tests/unit/test_migration_numeric_order.py` falla si
se comparte un número.

<a id="rules-for-writing-a-migration"></a>

## Reglas para escribir una migración

- **Nunca edites ni elimines una migración ya fusionada.** Los registros de los despliegues guardan su hash. Una corrección es un archivo nuevo.
- **Solo cambios aditivos.** Añade tablas, columnas, índices, projections y vistas; elimina un índice de omisión (skip index) o una projection que ningún código bajo `server/`, `scripts/`, `bench/` o `dev/` nombre (la compilación anterior pierde una optimización, nunca una columna); cambia codecs, valores por defecto o ajustes de tabla; amplía tipos; extiende un enum. Nunca elimines ni renombres una tabla o una columna, y nunca intercambies tablas. `tests/unit/test_migrations_additive.py` lo hace cumplir. De las vistas y las tablas, solo permite `DROP VIEW IF EXISTS` de una vista materializada que actúa al insertar y `DROP TABLE IF EXISTS` de una tabla reemplazada, en cada caso cuando ningún archivo bajo `server/`, `scripts/`, `bench/` o `dev/` la nombra: ni una compilación en servicio ni una herramienta de operador (que se ejecutan contra el servicio de producción) la lee. Restringir un tipo queda como cuestión de revisión, ya que una expresión regular no puede distinguirlo de una ampliación.
- **Retira una columna en dos versiones.** Primero publica una versión cuyo código ya no la lea ni la escriba; elimínala solo cuando ninguna compilación en servicio la nombre.
- **Todo escritor lleva una columna nueva.** Añadir una columna a `loom.memory` o `loom.shared` implica actualizar los constructores de filas en `server/memory/store/rows.py` (`build_memory_version_row`, `build_embedding_row`) y cada projection que los alimenta en el mismo cambio: son tablas `ReplacingMergeTree(version)` que se leen con `FINAL`, y una fila de versión que omite una columna la pone a cero en silencio para todos los lectores. Una columna nueva de `loom.memory` también debe añadirse a `loom.memory_versions` y al `SELECT` de la vista `memory_versions_mv` en la misma migración; de lo contrario, el historial deja de registrarla (`tests/unit/test_memory_versions_schema.py` compara las listas de columnas).
- **Una tabla que pueda contener contenido de memoria** necesita además un paso en la saga de borrado y una fila en el censo de residuos (`server/erasure/store/census.py`, `_CENSUS`); `tests/unit/test_every_table_is_classified.py` falla ante una tabla nueva que no figure en ninguna lista ([añadir una tabla](https://github.com/ClickHouse/loom/blob/main/docs/development/adding-a-table.md)).
- **DDL portable entre motores.** Declara las columnas vectoriales como `Array(BFloat16) CODEC(NONE)`; donde el codec `Quantized` pueda estar activo, el runner las crea ya codificadas ([ajustes restringidos](../architecture/clickhouse.md#gated-clickhouse-settings)). Usa nombres de motor `MergeTree` simples, que ClickHouse Cloud ejecuta como `SharedMergeTree`.

**Por qué aditivo.** La compilación que estaba en servicio antes de ejecutar una migración debe poder seguir leyendo y escribiendo después todas las tablas que conocía. Eso convierte un rollback blue/green en un cambio de proxy en lugar de un rollback de esquema, y permite que el color anterior siga sirviendo mientras el nuevo aplica las migraciones al arrancar. El precio se paga aquí: dos versiones para retirar una columna y reconstrucciones en lugar de `ALTER` para los cambios de clave.

<a id="runner-directives"></a>

## Directivas del runner

Un archivo de migración puede llevar directivas en comentarios que el runner lee:

| Directiva                           | Efecto                                                                                                   |
| ----------------------------------- | -------------------------------------------------------------------------------------------------------- |
| `-- loom: tolerate-error <code>`    | el archivo sobrevive a un error del servidor con nombre (por ejemplo, un setting que solo aceptan algunos servidores) |
| `-- loom: rebuild-if-empty <table>` | declara una clave de ordenación o de partición objetivo para `<table>` (abajo)                           |
| `-- loom: baseline covers N`        | el archivo reemplaza a todos los archivos numerados N o menos que el repositorio ya no incluye           |

**Baselines.** Un baseline lleva el número N, posterior al del archivo más reciente que reemplaza. Una base de datos nueva lo ejecuta. Una base de datos cuyo ledger contiene el archivo N lo registra sin ejecutarlo y conserva las filas reemplazadas, que el modo estricto informa como reemplazadas (superseded) y no como errores. Así, una compactación (squash) no requiere editar a mano ningún ledger, y una compilación anterior a ella sigue encontrando aplicados todos sus propios archivos. Una base de datos cuyo ledger contiene algunos archivos reemplazados pero no el archivo N se detuvo a medio camino: `loom migrate` la rechaza (`stranded`) y no cambia nada, ya que ejecutar el baseline omitiría los archivos que le faltan y registrarlo daría por hecho su esquema. Aplica los archivos que faltan con una compilación anterior a la compactación y vuelve a migrar. Compacta en una versión, no en cada cambio; la compactación construye el baseline a partir de `schemas/schema.sql` (el cuerpo del volcado sin los objetos que crean los archivos que conserva) y lo comprueba en un servidor nuevo, en un ledger totalmente migrado y en uno a medio migrar.

El runner también adapta sentencias al aplicarlas sin cambiar los archivos ni sus checksums: elimina el flag experimental de NLP donde el servidor lo prohíbe, crea las columnas vectoriales con el códec `Quantized` donde puede estar activo, quitando un códec general encadenado después de él donde `allow_suspicious_codecs` no puede estarlo (`server/core/schema/vector_codec_ddl.py`), crea cada vista con el usuario que ejecuta la migración (`DEFINER = CURRENT_USER`: el volcado nombra a su propio usuario, que otro servidor puede no tener) y espera a que termine cada mutación que emite un archivo antes de registrarlo (`LOOM_MIGRATE_MUTATION_WAIT_S`).

**Verificaciones previas.** Un archivo que no debe ejecutarse mientras los datos contengan algo que el archivo por sí solo no puede ver registra una verificación en `server/core/schema/prechecks.py`, indexada por su nombre sin el número, de modo que un archivo renumerado la conserva. El runner ejecuta la verificación antes del archivo, también en una ejecución de prueba (dry run); una verificación que rechaza detiene la ejecución con su mensaje y no ejecuta nada del archivo. `097_mirror_names_reserved.sql` usa una: rechaza mientras un espacio de nombres con la forma reservada de espejo esté registrado o contenga filas vivas.

<a id="changing-a-sort-key-or-a-partition-key"></a>

## Cambiar una clave de ordenación o una clave de partición

ClickHouse no cambia ninguna de las dos en el lugar, así que una migración declara el destino y la tabla se reconstruye, nunca se altera:

```sql
-- loom: rebuild-if-empty code_test_facets
CREATE TABLE loom.code_test_facets ( ...every column, unchanged... ) ... ORDER BY (...);
```

Esa sentencia es la única copia del DDL de destino. El runner (`server/core/schema/table_rebuild.py`) compara el `sorting_key` y el `partition_key` de la tabla en vivo con ella: si coinciden, registra el archivo; una tabla vacía (una instalación nueva) se intercambia al instante por una gemela con el DDL de destino; y una tabla con filas hace fallar el arranque con un mensaje que nombra la herramienta. Un arranque nunca copia filas. En un despliegue con datos, el operador ejecuta primero la herramienta, una tabla a la vez:

```bash
python -P -m scripts.ops.table_rebuild --table code_test_facets --dry-run
python -P -m scripts.ops.table_rebuild --table code_test_facets --yes
```

Toda acción que modifica algo (`--yes` solo, `--rollback`, `--drop-pre-rebuild`) imprime el ClickHouse con el que se comunica y su plan, y no cambia nada sin `--yes`. La herramienta copia todas las filas (sin FINAL) a `loom.<t>_rebuild` en lotes acotados, copia las partes escritas entretanto, comprueba el número de filas bajo FINAL y la versión más reciente de una muestra de claves, intercambia con `EXCHANGE TABLES` y conserva la tabla antigua como `loom.<t>_pre_rebuild` para `--rollback` hasta `--drop-pre-rebuild`. Se niega a intercambiar, conservando la gemela, cuando un `ALTER` cambió la tabla en vivo durante la copia: sale con 6 si ClickHouse lo ejecuta como mutación (eliminar, renombrar o cambiar el tipo de una columna, eliminar un índice, un TTL) y con 3 si solo mueve el tiempo de sus metadatos (un índice, un ajuste, el valor por defecto o el códec de una columna). Una columna añadida hace fallar la copia (salida 1, sin intercambio), o la ejecución sale con 3 tras la última pasada de verificación. Una reconstrucción no arrastra un `ALTER`: vuelve a expresarlo en la sentencia de destino y ejecuta de nuevo con `--restart`. Además, rechaza una tabla que ninguna migración declara.
`loom migrate --dry-run --strict` falla mientras una tabla declarada siga teniendo filas en la disposición antigua, de modo que la comprobación previa al despliegue detecta un paso omitido.

El destino conserva las columnas de la tabla (así el intercambio es aditivo), y su clave de ordenación conserva las columnas de la clave antigua, reordenadas: esa clave es también la que usa un `ReplacingMergeTree` para fusionar. Una clave con otras columnas fusiona otras filas y se lista con su motivo en `tests/unit/test_migrations_additive.py`. Nombra la reconstrucción en las notas de la versión como un paso del operador ([proceso de publicación](https://github.com/ClickHouse/loom/blob/main/docs/development/release-process.md#release-notes)).

<a id="postgres-migrations"></a>

## Migraciones de Postgres

Los archivos `schemas/pg_migrations/NNN_*.sql` siguen las mismas reglas: están numerados, son aditivos, nunca se editan una vez fusionados, y cada `CREATE`, `ALTER` o `DROP` de una tabla nombra `loom.<table>`, salvo el nombre nuevo de un renombrado, donde Postgres no admite esquema (`tests/unit/test_storage_layout_rows.py`). Un arranque los aplica bajo un bloqueo consultivo (advisory lock) de Postgres, cada archivo en una transacción junto con su fila del registro. Una migración espera como máximo 5 s a un bloqueo de tabla (`lock_timeout`, y luego reintenta), de modo que un `ALTER` nunca deja en cola todas las consultas de la tabla detrás de una transacción larga. Una compilación sirve con un registro que contiene archivos de una compilación posterior, con una advertencia que los nombra; un archivo del registro que no incluye, numerado igual o por debajo de su propio último archivo, o uno cuyo checksum cambió, rechaza el arranque.

Cada archivo se ejecuta en una transacción, por lo que una migración no crea índices en una tabla grande: la creación bloquearía las escrituras de la tabla, y cada commit respondería 503, hasta que termine. Un índice así necesita `CREATE INDEX CONCURRENTLY` fuera de una transacción, y un arranque que espera el bloqueo consultivo entra en interbloqueo con él (una sesión en espera mantiene un snapshot que la creación debe esperar a que termine): un cambio que añade un archivo de ese tipo también toma el bloqueo reintentando `pg_try_advisory_lock`. Cuando la primera versión que escribe en una tabla incluye el archivo, la tabla es pequeña dondequiera que se ejecute el archivo, y su índice puede crearse en la transacción del archivo (`010_revision_delivered_at.sql`).

<a id="schema-dump-and-check"></a>

## Volcado y verificación del esquema

`schemas/schema.sql` es el resultado volcado de aplicar todas las migraciones: léelo para
entender la forma actual, nunca lo edites a mano. Después de cambiar una migración, regénéralo
contra un ClickHouse nuevo de la versión que prueba CI (`clickhouse/clickhouse-server:latest`):

```bash
bash dev/ci_start_clickhouse.sh     # that image with Loom's server config
make migrate
make schema-dump                    # rewrites schemas/schema.sql only when the dump differs
make schema-check                   # exit 1 with the head of the diff; writes nothing
```

Si el volcado falla, el archivo queda como estaba. Otra versión de ClickHouse, un servidor donde el
códec `Quantized` o su cadena `LZ4` no se pueden activar, o una base de datos que contiene gemelas de reconstrucción producen un volcado
distinto, y por eso importa el contenedor fijado. Las tablas internas (`_migrations`, nombres que empiezan por `_` o `.`)
se excluyen. El job `schema-check` de CI reproduce todas las migraciones en un servidor nuevo, ejecuta
la prueba en seco estricta y después `make schema-check`.

CI sigue `latest`, así que una nueva versión de ClickHouse que imprima el DDL de otra forma pone
`schema-check` en rojo sin ningún cambio en Loom. Lee la diferencia que imprime el job: cuando solo es
de formato, ejecuta los cuatro comandos anteriores (descargan la nueva versión) y confirma
`schemas/schema.sql` por separado. Un cambio de significado (un tipo, un valor por defecto, un ajuste
del motor) es un cambio de comportamiento de ClickHouse que conviene investigar antes de volcar encima;
`LOOM_CLICKHOUSE_IMAGE=clickhouse/clickhouse-server:A.B.C.D` ejecuta los comandos anteriores sobre la
última versión que se volcó sin diferencias, para comparar.

<a id="checklist"></a>

## Lista de verificación

1. Toma el siguiente número libre; escribe el archivo de forma aditiva.
2. Actualiza los constructores de filas, `loom.memory_versions`, el censo de borrado y los casos del analizador si el cambio los afecta.
3. Ejecuta `make migrate` contra un ClickHouse local, luego `make schema-dump` y confirma `schemas/schema.sql` junto con la migración.
4. Ejecuta `make test-unit` (las pruebas de aditividad, numeración y clasificación del esquema) y la suite de integración para las rutas que leen la nueva forma.
5. Anota cualquier paso del operador (una reconstrucción, un backfill) para las notas de la versión.

<a id="related"></a>

## Relacionado

- [Actualizaciones](https://github.com/ClickHouse/loom/blob/main/docs/operations/upgrades.md): cómo el runner aplica los archivos en un despliegue
- [ClickHouse](../architecture/clickhouse.md)
- [Referencia del esquema](https://github.com/ClickHouse/loom/blob/main/docs/reference/schema.md)
