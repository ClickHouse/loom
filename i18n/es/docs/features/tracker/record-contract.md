<!-- i18n: source=docs/features/tracker/record-contract.md blob=88cada3e321cc9c82e54440ec12b93a202fcc39a model=claude-sonnet-5-5 -->
> Esta página es una traducción automática de la [página en inglés](https://github.com/ClickHouse/loom/blob/main/docs/features/tracker/record-contract.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/features/tracker/record-contract.md), which is authoritative.

<a id="tracker-record-contract"></a>

# Contrato de registros del tracker

Las ops `tracker.*`, y los tramos de tracker de `code.*` y `loom_search`, leen en vivo las incidencias, los pull requests y los incidentes de un equipo desde el ClickHouse propio del equipo, con una credencial de solo lectura. Loom no copia el registro. Cada sentencia se escribe contra un único modelo lógico, el **contrato**: las tablas y columnas que se describen a continuación.

Un equipo conecta su esquema de una de dos maneras:

| Modo          | Cómo                                                                                                                                                      |
| ------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Vistas        | El equipo crea vistas con los nombres de tablas y columnas del contrato. Loom las lee tal como están.                                                      |
| Mapeo         | Loom lista las tablas que la credencial puede leer y propone un mapeo. El mapeo confirmado se guarda como datos; no se crea nada de forma remota.          |

Un mapeo es solo una proyección frente al contrato, de modo que las mismas sentencias, pruebas y predicados de privacidad se ejecutan para cualquier fuente. El contrato como código es `server/tracker/domain/contract.py` (`REQUIRED`, `OPTIONAL`, `FEATURES`, `ALIASES`), el mapeo es `server/tracker/domain/mapping.py`, y las columnas que se leen por tabla son `COLUMNS_READ` en `server/tracker/domain/columns.py`.

<a id="adding-a-tracker-source"></a>

## Agregar una fuente de tracker

1. **Crea un usuario de ClickHouse de solo lectura para Loom** con `readonly = 2`. `readonly = 1`
   rechaza los ajustes por consulta que Loom envía con cada lectura, por lo que no se
   ejecutaría ninguna lectura (`CREDENTIAL_READONLY_MODE`).

   ```sql
   CREATE USER loom_ro IDENTIFIED BY '…' SETTINGS readonly = 2;
   GRANT SELECT ON system.columns TO loom_ro;
   -- views mode: the views only
   GRANT SELECT ON loom_tracker.* TO loom_ro;
   -- mapping mode instead: each mapped table
   GRANT SELECT ON tracker_db.tickets TO loom_ro;
   ```

2. **Solo en modo views: crea las vistas.** Una base de datos (por ejemplo `loom_tracker`) con
   `github_issues`, `github_prs` y `edges`, además de las tablas opcionales para las que tengas datos.
   Créalas con `DEFINER = CURRENT_USER SQL SECURITY DEFINER`, de modo que `loom_ro` necesite `SELECT`
   sobre las vistas y nada sobre las tablas que hay detrás. Una vista simple se lee con los permisos
   del propio lector, y cada lectura fallaría con `ACCESS_DENIED`; la comprobación del registro
   detecta esto e indica la solución. Una fuente externa a ClickHouse puede colocarse tras una
   tabla con motor `PostgreSQL`, `MySQL` o `S3`. `tracker.contract` devuelve cada tabla y
   columna con lo que contiene y una plantilla de vista en blanco por tabla, incluida una
   función SQL `loom_uuid5(name)` para el `id` del elemento.

3. **Registra la fuente.** Cualquier miembro de la organización con sesión iniciada puede hacerlo, desde el
   flujo _Add a tracker_ del panel ([resumen del tracker](https://github.com/ClickHouse/loom/blob/main/docs/features/tracker/overview.md#adding-a-tracker-from-the-dashboard))
   o con las ops siguientes; quien registra se convierte en el primer `admin` de los
   espacios de nombres del tracker. Antes de escribir nada, Loom comprueba que el host resuelve a una dirección
   pública (`HOST_REFUSED`), que la credencial es de solo lectura (`CREDENTIAL_NOT_READONLY`,
   `CREDENTIAL_READONLY_MODE`), que puede conectarse (`SOURCE_UNREACHABLE`) y que el esquema
   o el mapeo cumple el contrato (`CONTRACT_FAILED`). Solo lectura significa que todo tipo de acceso
   que listan `SHOW GRANTS` y `SHOW GRANTS FINAL`, incluidos los tipos de los roles concedidos,
   es `SELECT`, un `SHOW` de bases de datos, tablas, columnas o diccionarios, `dictGet` o
   `CREATE TEMPORARY TABLE`, y nada se concede `WITH GRANT OPTION` ni
   `WITH ADMIN OPTION`, tampoco en un rol anidado; cualquier otro tipo se rechaza. Todo rol
   concedido al usuario debe ser un rol predeterminado, para que la comprobación vea toda su cadena. Un
   servidor que no acepta `SHOW GRANTS FINAL` se rechaza (`SOURCE_UNREACHABLE`). Con
   `dry_run: true`, un esquema que falla se devuelve como informe, con un
   borrador de `CREATE OR REPLACE VIEW` por cada tabla que falla.

   | Op                           | Qué hace                                                                                                                                                               |
   | ---------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
   | `tracker.contract`           | El contrato y las plantillas de vista, antes de cualquier conexión.                                                                                                    |
   | `tracker.source.inspect`     | Se conecta y lista cada tabla legible con sus columnas, además de un mapeo propuesto. No escribe nada.                                                                 |
   | `tracker.source.add`         | `name`, `host`, `port` (8443), `secure` (true), `database`, `user`, `password`, `namespaces`, `mapping` opcional; `dry_run: true` ejecuta todas las comprobaciones y no escribe nada. |
   | `tracker.source.set_mapping` | Reemplaza el mapeo de una fuente registrada (también con `dry_run`); `mapping: null` la devuelve al modo views.                                                        |
   | `tracker.source.check`       | Vuelve a ejecutar ahora la comprobación del contrato.                                                                                                                  |
   | `tracker.source.rotate`      | Reemplaza la credencial.                                                                                                                                               |
   | `tracker.source.remove`      | La retira junto con sus espacios de nombres (`confirm`).                                                                                                               |
   | `tracker.source.list`        | Todas las fuentes de la organización: veredicto del contrato, estado del breaker, espacios de nombres y la posición de quien llama en cada uno. Nunca se devuelve ninguna credencial. |

   Cada entrada de `namespaces` es `{namespace, repos, restricted, join_policy}`: `repos` son
   los prefijos `Org/repo` de `source_id` que el espacio de nombres puede leer (un repositorio que no figura
   nunca se lee); `restricted` lo convierte en un registro privado; `join_policy` es `open` (predeterminado) o
   `approval` para un espacio de nombres estándar. El registro por autoservicio requiere `LOOM_TRACKER_ORG`
   y está activado por defecto (`LOOM_TRACKER_SELF_SERVICE_ENABLED`, como máximo
   `LOOM_TRACKER_SELF_SERVICE_MAX_SOURCES` por organización, 10 por defecto;
   `LOOM_TRACKER_SELF_SERVICE_ALLOW_PRIVATE_HOSTS=true` para un ClickHouse local de
   desarrollo). La contraseña se almacena cifrada y nunca se devuelve. `inspect` por
   nombre requiere `admin` en uno de los espacios de nombres de la fuente; `set_mapping`, `rotate` y
   `remove` lo requieren en todos, ya que cada uno cambia lo que lee cada espacio de nombres de la fuente.
   Un espacio de nombres estándar lo decide la puerta en `admin`, y uno restringido
   por las reglas de la puerta restringida, salvo que el token no necesita nombrarlo;
   registrar la fuente no basta una vez revocada la concesión de quien la registró
   ([espacios de nombres restringidos](https://github.com/ClickHouse/loom/blob/main/docs/architecture/security.md#restricted-namespaces)). Una
   sesión del panel de cualquiera que no sea administrador de la plataforma es un token `writer`, por lo que no puede
   modificar ni retirar una fuente con un espacio de nombres privado (restringido): para eso hace falta un token
   con el rol `admin`, que un operador emite con `loom mint-token --roles admin`.
   La página muestra esos botones deshabilitados con el motivo; el servidor decide.

   Un operador puede configurar la misma fuente de otro modo ([configuración](#configuration)); una
   entrada configurada prevalece sobre una registrada del mismo nombre y solo se cambia en
   la configuración (`SOURCE_FROM_CONFIG`).

4. **Loom crea las filas de directorio de los espacios de nombres**: `kind = tracker`, privadas,
   `classification = restricted` para un espacio de nombres restringido, `join_policy` `open` (o
   `approval`) para uno estándar y `approval` para uno restringido.

5. **Los lectores se unen.** `namespace.join` con `role: "viewer"` en un espacio de nombres de tracker abierto
   admite al instante, cuentas de servicio incluidas. Uno restringido espera a su admin, y el
   token del lector debe nombrar el espacio de nombres
   ([identidades y espacios de nombres](https://github.com/ClickHouse/loom/blob/main/docs/concepts/identities-and-namespaces.md)).

6. **Vincula el tracker a un repositorio** para que los agentes que trabajan en ese checkout lo lean:
   `code.repo.set_source` con `tracker_namespaces` (`admin` del repositorio), la tarjeta del
   repositorio en el panel o, solo para los repositorios propios del operador,
   `LOOM_TRACKER_CODE_NAMESPACE_BINDINGS_JSON`. Un repositorio puede vincular varios trackers en
   orden; el primero es el principal, cuyos números son los números de issues y PR del repositorio
   ([vincular un tracker](https://github.com/ClickHouse/loom/blob/main/docs/features/tracker/overview.md#linking-a-tracker-to-a-repository)).
   `tracker.binding` informa a qué se resuelve un espacio de nombres de código para quien llama.

<a id="configuration"></a>

## Configuración

La forma del operador de la misma entrada:

```bash
LOOM_TRACKER_ORG=acme
LOOM_TRACKER_SOURCES_JSON='{
  "sources": {
    "widgets": {"host": "abc.clickhouse.cloud", "port": 8443, "secure": true, "database": "loom_tracker"}
  },
  "namespaces": {
    "tracker-widgets":         {"source": "widgets", "repos": ["acme/widgets"]},
    "tracker-widgets-private": {"source": "widgets", "repos": ["acme/widgets-private"], "restricted": true}
  }
}'
LOOM_TRACKER_CREDENTIALS_JSON='{"widgets": {"user": "loom_ro", "password": "…"}}'
LOOM_TRACKER_CODE_NAMESPACE_BINDINGS_JSON='{"code-widgets*": ["tracker-widgets", "tracker-helpdesk"]}'
```

Las credenciales van en su propia variable, separadas del mapa, de modo que el mapa se pueda mostrar y comparar con diff sin el secreto. El valor de un binding es un espacio de nombres del tracker o una lista ordenada, con el principal primero; las claves son globs sobre los nombres de los espacios de nombres de código de los repositorios del operador (un repositorio de autoservicio lo vincula su maintainer). Los vínculos de `code.repo.set_source` de un maintainer reemplazan el mapa para ese repositorio. Los espacios de nombres del tracker pertenecen a `LOOM_TRACKER_ORG`; si está vacío, ningún espacio de nombres del tracker se sirve.

<a id="source-entry-options"></a>

### Opciones de la entrada de source

Una entrada de source también puede incluir:

- `"mapping"`: el mapeo guardado ([modo de mapeo](#mapping-mode)); si no está, se usa el modo de vistas.
- `"checks": {"from": "<table or table function>", "repo": "Org/repo"}`: dónde viven los resultados de CI del tracker, leídos por `tracker.flaky` y `tracker.test_signal`. Resolución: esta entrada, si no la tabla `checks` propia del source, si no `LOOM_TRACKER_CHECKS_FROM` / `LOOM_TRACKER_CHECKS_REPO` (ambas vacías por defecto: sin checks de CI). Indique una función de tabla en lugar de una vista sobre ella: el filtro de una vista se ejecuta en el lado remoto después del escaneo. Sin `repo`, un espacio de nombres con un único repositorio usa ese.
- `"bisect": {"code_prefixes": ["src/"]}`: qué rutas cuentan como código fuente para bisect; si no está, cualquier ruta que no esté bajo docs, tests, CI, examples o benchmarks. Un repositorio sin tracker igualmente hace bisect sobre el historial de PR propio de Loom (la respuesta entonces indica `candidates_from: "code_history"`).
- `"columns": {"<table>": {"<contract column>": "<your column>"}}`: columnas del contrato que sus tablas tienen con otro nombre, por ejemplo `{"github_issues": {"triage_bisect_outcome": "bisect_result"}}`. La columna real se comprueba igual que la que reemplaza; solo se aceptan identificadores simples.

Reglas:

- Un espacio de nombres que indica un source desconocido no se sirve (una línea de error al arrancar, nunca una suposición).
- Cada source tiene su propia conexión, circuit breaker y presupuesto de tiempo, así que el servicio lento de un equipo nunca dispara el breaker de otro.
- Los sources registrados (`loom.qa_sources`) se vuelven a leer cada `LOOM_TRACKER_SOURCES_REFRESH_SECONDS` (60); una escritura en el mismo proceso los actualiza de inmediato.

Todos los ajustes del tracker están en la [referencia de configuración](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md#tracker).

<a id="public-and-private"></a>

## Público y privado

Un espacio de nombres del tracker es **estándar** o **restringido**, y Loom lo aplica
sentencia por sentencia:

- Cada sentencia de un espacio de nombres estándar lleva `is_private = 0`, y las tablas que
  contienen solo datos privados (`core_incidents`, `support_cases`, `slack_threads`) se rechazan
  antes de cualquier lectura.
- Un espacio de nombres restringido elimina ese predicado, lee únicamente sus propios `repos` y
  queda sujeto a las reglas de los espacios de nombres restringidos: un token que lo nombre y una concesión directa.

Funcionan tres configuraciones:

- un conjunto de vistas con una columna `is_private`, leído por ambos espacios de nombres (una política de filas sobre
  `is_private` para el usuario de solo lectura añade una comprobación en el lado de ClickHouse);
- dos conjuntos de vistas con dos usuarios, uno que contiene solo filas públicas, registrados como dos fuentes;
- un espacio de nombres restringido por sí solo.

<a id="the-contract"></a>

## El contrato

Los tipos se comparan por **familia**: `String`, `LowCardinality(String)`, `Nullable(String)`,
`FixedString` y `Enum` son una; `DateTime`, `DateTime64` y `Date` son una; los tipos `UInt*`
e `Int*` y `Bool` son una; `Array(x)` se compara según `x`. Una vista sobre otro almacén
puede diferir en códec y en nulabilidad, nunca en forma. Las plantillas de vistas escriben
cada familia como su tipo más ancho (`UInt64`, `DateTime64(3)`, …).

<a id="required-tables"></a>

### Tablas obligatorias

Una fuente que no las tenga no se sirve.

**`github_issues`**: una fila por issue.

| Columna                                                                                      | Familia       | Significado                                                                                                                       |
| -------------------------------------------------------------------------------------------- | ------------- | --------------------------------------------------------------------------------------------------------------------------------- |
| `id`                                                                                         | UUID          | `uuid5(NAMESPACE_DNS, 'github_issue:' \|\| source_id)` (`github_issue_private:` para las filas privadas); la clave que usa `edges` |
| `source_id`                                                                                  | String        | `Org/repo#number`; el repositorio debe ser uno de los que lista el espacio de nombres del tracker                                 |
| `number`                                                                                     | UInt          | el número del issue                                                                                                               |
| `url`, `title`, `body`, `author`                                                             | String        | un enlace que una persona pueda abrir; el título que leen todas las búsquedas; el texto que lee la comprobación de duplicados; quién lo abrió |
| `labels`, `assignees`                                                                        | Array(String) |                                                                                                                                   |
| `primary_component`, `severity_level`                                                        | String        | la taxonomía del equipo; `''` si no hay                                                                                           |
| `state_reason`                                                                               | String        | `completed`, `not_planned`, `duplicate` o `''`                                                                                    |
| `affected_versions`, `fix_versions`                                                          | Array(String) | versiones que el issue afecta; versiones que incluyen la corrección                                                               |
| `is_flaky_test`, `is_crash_ci`, `is_performance`                                             | UInt          | indicadores 0/1; `0` cuando se desconoce                                                                                          |
| `has_test`                                                                                   | UInt          | existe una prueba vinculada; NULL cuando se desconoce                                                                             |
| `error_code`                                                                                 | UInt          | el código de error del servidor que nombra el reporte; `0` si no hay                                                              |
| `is_private`                                                                                 | UInt          | `1` para una fila de un repositorio privado                                                                                       |
| `created_at`, `updated_at`, `closed_at`                                                      | DateTime      | `closed_at` es la época 0 mientras está abierto                                                                                   |
| `triage_bisect_outcome`, `triage_introduced_commit`, `triage_component`, `triage_confidence` | String        | el veredicto de un bot de triaje; se sirve como `''` cuando el tracker no tiene ninguno                                           |
| `triage_reproduces_on`, `triage_clean_on`                                                    | Array(String) | igual, se sirve como `[]`                                                                                                         |

**`github_prs`**: una fila por pull request.

| Columna                                                                                      | Familia       | Significado                                                                |
| -------------------------------------------------------------------------------------------- | ------------- | -------------------------------------------------------------------------- |
| `id`                                                                                         | UUID          | `uuid5(NAMESPACE_DNS, 'github_pr:' \|\| source_id)` (`github_pr_private:`) |
| `source_id`, `number`, `url`, `title`, `body`, `author`                                      | como arriba   |                                                                            |
| `pr_number`                                                                                  | UInt          | igual a `number`                                                           |
| `primary_component`, `severity_level`, `is_private`, `created_at`, `updated_at`, `closed_at` | como arriba   |                                                                            |
| `state`                                                                                      | String        | `open`, `closed` o `merged`                                                |
| `is_merged`, `has_test`, `review_requests_test`                                              | UInt          |                                                                            |
| `test_types`, `reviewers`, `src_components`                                                  | Array(String) |                                                                            |
| `ci_result`, `changelog_category`, `minor_version`, `base_branch`                            | String        |                                                                            |
| `is_backport_pr`, `is_cherrypick_pr`, `is_sync_pr`                                           | UInt          | PR derivados, excluidos de las lecturas de actividad                       |
| `upstream_pr_number`                                                                         | UInt          | el PR que repite un backport; `0` si no hay                                |
| `additions`, `deletions`, `time_to_merge_days`                                               | UInt          |                                                                            |

**`edges`**: vínculos entre elementos.

| Columna                | Familia  | Significado                                                                                               |
| ---------------------- | -------- | --------------------------------------------------------------------------------------------------------- |
| `from_id`, `to_id`     | UUID     | ids de elementos como arriba                                                                              |
| `from_type`, `to_type` | String   | `github_issue`, `github_pr`, `github_issue_private`, `github_pr_private`, `core_incident`, `support_case` |
| `rel_type`             | String   | `fixes`, `related_to`, `duplicate_of`, `caused_by`, `backport_of`, `reverts`, …                           |
| `created_at`           | DateTime |                                                                                                           |

Loom deriva localmente el `id` de un elemento y une `edges` por él, así que una vista debe
producir exactamente ese valor. ClickHouse no tiene `uuid5()`; la función `loom_uuid5` que
devuelve `tracker.contract` es la construcción de la versión 5 de la RFC 4122 en SQL, igual
a `uuid.uuid5` de Python.

<a id="optional-tables"></a>

### Tablas opcionales

Si falta una tabla, las ops que la necesitan responden `not_provided`; una tabla presente a
la que le falta una de sus columnas mínimas (`OPTIONAL` en `contract.py`) se reporta como
`incomplete` y se trata como ausente.

| Tabla                                        | Columnas mínimas                                                                                                                                              |
| -------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `core_incidents`                             | `id`, `source_id`, `title`, `body`, `primary_component`, `severity_level`, `stack_functions Array(String)` (coincidencia de frames de pila), `error_code`, `created_at` |
| `support_cases`                              | `id`, `source_id`, `title`                                                                                                                                    |
| `slack_threads`                              | `source_id`, `title`, `body`                                                                                                                                  |
| `component_risk_snapshot`                    | `computed_at`, `component`                                                                                                                                    |
| `release_items`                              | `release_version`, `issue_number`                                                                                                                             |
| `release_health_snapshot`                    | `computed_at`, `release_version`                                                                                                                              |
| `version_history`, `version_history_private` | `commit_sha`, `version`                                                                                                                                       |
| `checks`                                     | `check_name`, `test_name`                                                                                                                                     |
| `cloud_instances`                            | `instance_id`                                                                                                                                                 |
| `rel_instance_mentions`                      | `from_id`, `instance_id`                                                                                                                                      |
| `slack_incidents`                            | `incident_id`                                                                                                                                                 |

`core_incidents`, `support_cases` y `slack_threads` se leen solo a través de un espacio de
nombres restringido.

<a id="feature-columns"></a>

### Columnas de funcionalidad

Columnas opcionales de las tablas obligatorias, reportadas como presentes o ausentes. Una
columna de funcionalidad que la fuente no tiene lee su alias o, si no, un valor por defecto
(consulta [Alias](#aliases)): las sentencias que la leen responden como para una fila sin
ella, y la funcionalidad sigue ausente.

| Funcionalidad | Columnas                                            | Habilita                                                                              |
| ------------- | --------------------------------------------------- | ------------------------------------------------------------------------------------- |
| `bisect`      | `github_prs.diff`, `github_prs.affected_file_paths` | candidatos de bisect y búsquedas del culpable                                         |
| `merge_time`  | `github_prs.merged_at`                              | datos de tiempo de merge                                                              |
| `vector`      | `github_issues.embedding`, `github_prs.embedding`   | solo se reporta; `tracker.similar` usa los embeddings que calcula el propio Loom      |

<a id="aliases"></a>

### Alias

Una fuente a la que le falta una columna del contrato aún cumple el contrato cuando `ALIASES`
en `contract.py` nombra una expresión aceptada para ella sobre columnas que la fuente sí
tiene. La comprobación verifica por familia las columnas del propio alias y lo reporta como
`aliased`; las sentencias siguen leyendo el nombre del contrato, porque la referencia a la
tabla se sustituye por una proyección que añade el alias (el analizador de consultas la
reduce a las columnas que lee una sentencia). Un alias sin columnas propias es un
**valor por defecto** (`defaulted`): cada fila lee `''`, `0`, `[]` o `NULL`, y el reporte lo
indica, de modo que un valor por defecto nunca se confunde con datos.

| Tabla                                            | Columna                                                                                      | Formas aceptadas, en orden                                                         |
| ------------------------------------------------ | -------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------- |
| `github_issues`                                  | `severity_level`                                                                             | `severity`                                                                         |
| `github_issues`                                  | `has_test`                                                                                   | `toUInt8(metadata.has_test.:Bool)` (una columna JSON `metadata`), si no `NULL`     |
| `github_issues`                                  | `triage_bisect_outcome`, `triage_introduced_commit`, `triage_component`, `triage_confidence` | `''`                                                                               |
| `github_issues`                                  | `triage_reproduces_on`, `triage_clean_on`                                                    | `[]`                                                                               |
| `github_prs`                                     | `has_test`                                                                                   | `toUInt8(metadata.has_test.:Bool)`, si no `toUInt8(tests_added > 0)`               |
| `github_prs`                                     | `upstream_pr_number`                                                                         | `ifNull(toUInt32(metadata.original_pr_number.:Int64), 0)`, si no `0`               |
| `github_prs`                                     | `review_requests_test`                                                                       | `NULL`                                                                             |
| `github_prs`                                     | `test_types`, `src_components`                                                               | `[]`                                                                               |
| `github_prs`                                     | `merged_at`                                                                                  | `if(is_merged = 1, closed_at, toDateTime64(0, 3))`                                 |
| `github_prs`, `core_incidents`                   | `severity_level`                                                                             | `''`                                                                               |
| `qa_bugs` (la vista entre fuentes de la ruta de respuesta) | `severity_level`, `has_test`                                                       | `severity`; `toUInt8(metadata.has_test.:Bool)`, si no `NULL`                       |

Un cambio de nombre que el contrato no lista sigue siendo una columna faltante. Decláralo
en el mapa `columns` de la entrada de la fuente ([opciones de la entrada de fuente](#source-entry-options)),
o añade un `Alias` en `contract.py` para aceptar otra grafía en todas las fuentes.

<a id="mapping-mode"></a>

## Modo de mapeo

Un mapeo indica, para cada tabla lógica, cuál de las tablas del equipo lee y, para cada columna lógica, una expresión sobre las columnas de esa tabla:

```json
{
  "version": 1,
  "tables": {
    "github_issues": {
      "source": "tracker_db.tickets",
      "final": true,
      "columns": {
        "source_id": { "concat": [{ "lit": "acme/widgets#" }, "ticket_no"] },
        "number": "ticket_no",
        "title": "summary",
        "body": "description",
        "created_at": "opened_at",
        "state_reason": { "lower": "resolution" }
      }
    }
  }
}
```

- **Las expresiones son datos, nunca SQL**: un nombre de columna que tenga la tabla activa (o `{"col": …}`), `{"lit": …}`, o uno de `concat`, `coalesce`, `eq`, `in`, `has`, `cast` (`[expr, family]`), `lower`, `digits` (el número final de una clave: `PROJ-1234` → `1234`). Loom las renderiza con los identificadores entre comillas invertidas y los literales entre comillas, convierte cada una a la familia de tipos que leen las sentencias y las ejecuta con la credencial de solo lectura.
- `source` es `db.table`, o `table` en la base de datos del tracker. `final: true` lee la tabla con `FINAL` (propuesto para los motores Replacing, Collapsing, Aggregating y Summing).
- Loom deriva el `id` de cada elemento a partir del `source_id` mapeado, y el `from_id` / `to_id` de una arista a partir de `from_ref` / `to_ref` mapeados (`Org/repo#number`). Nunca se mapean.
- Cada tabla mapeada necesita sus columnas principales: `github_issues` `source_id`, `number`, `title`, `created_at`; `github_prs` `source_id`, `pr_number`, `title`, `created_at`; `edges` `from_ref`, `to_ref`, `from_type`, `to_type`, `rel_type`; `core_incidents` `source_id`, `title`, `created_at`. Un mapeo debe mapear `github_issues`. Las columnas sin mapear toman un valor por defecto o se derivan (el `state` de un PR, a partir de sus campos de fusionado y cerrado), y el informe las enumera; una tabla lógica que el mapeo no nombra se lee como una tabla vacía.
- Las capacidades del informe (`items`, `duplicate_check`, `duplicate_calibration`, `pull_requests`, `links`, `severity_board`, `bisect`, `incidents`, `ci_checks`) indican qué puede responder Loom con los datos mapeados.

En el momento de la lectura, cada referencia a una tabla lógica se sustituye por la proyección mapeada, de modo que las mismas sentencias, pruebas y predicados de privacidad se ejecutan para cualquier fuente.

<a id="checking-a-source"></a>

## Comprobar una fuente

```bash
loom tracker-contract-check                           # every configured source
loom tracker-contract-check --source widgets          # one source
loom tracker-contract-check --namespace tracker-widgets
loom tracker-contract-check --json
```

El servidor ejecuta la misma comprobación en la primera lectura del tracker de cada proceso: lee el esquema con la propia credencial de la fuente (`system.columns`, o `DESCRIBE` por tabla cuando está oculta) y guarda el veredicto en caché durante `LOOM_TRACKER_CONTRACT_TTL_S` (3,600 s); si el veredicto es un fallo, lo guarda durante 60 s, de modo que una vista corregida se detecta en menos de un minuto. Una fuente que falla responde a cada lectura con `tracker_unavailable` indicando qué falta, nunca con una respuesta parcial. `tracker.source.check` ejecuta la comprobación bajo demanda.

<a id="related"></a>

## Relacionado

- [Descripción general del tracker](https://github.com/ClickHouse/loom/blob/main/docs/features/tracker/overview.md): qué responden las ops, la vinculación, el flujo del panel
- [Historial y PRs](../code-index/history-and-prs.md): las ops del plano de código que leen un tracker
- [Referencia de configuración](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md#tracker)
- [Referencia de operaciones](https://github.com/ClickHouse/loom/blob/main/docs/reference/operations.md#tracker)
