<!-- i18n: source=docs/guides/query-from-postgres.md blob=c49542f7fffa2ee2ced68d49d8ab0829854132f8 model=claude-sonnet-5-5 -->
> Esta página es una traducción automática de la [página en inglés](https://github.com/ClickHouse/loom/blob/main/docs/guides/query-from-postgres.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/guides/query-from-postgres.md), which is authoritative.

<a id="query-loom-from-postgres"></a>

# Consultar Loom desde Postgres

Esta guía lee las memorias, conversaciones y el uso de un tenant desde el Postgres propio de la organización, donde se pueden unir con las tablas de la aplicación. Usa la extensión de código abierto [`pg_clickhouse`](https://github.com/ClickHouse/pg_clickhouse), que importa tablas y vistas de ClickHouse como tablas foráneas de Postgres y delega las consultas a ClickHouse. Lo que contienen las vistas y lo que dejan fuera está en la [referencia de la superficie SQL](https://github.com/ClickHouse/loom/blob/main/docs/reference/sql-surface.md).

Los datos permanecen en ClickHouse: una consulta desde Postgres lee las vistas en el momento de la consulta, con las mismas reglas de alcance, de retención de valores y de actualidad que cualquier otro cliente SQL. Este Postgres es el de la aplicación, no la [autoridad de memoria](https://github.com/ClickHouse/loom/blob/main/docs/operations/postgres.md) de Loom: la extensión lee, Loom nunca escribe a través de ella, y la base de datos propia de Loom no necesita la extensión. Usa una base de datos distinta, o un servicio distinto, de la que indica `LOOM_PG_DSN` de Loom.

CI prueba la versión que indica `LOOM_PGCH_VERSION` en `.github/workflows/ci.yml` en Postgres 16 y 18 (`tests/integration/test_pg_clickhouse_live_ch.py`).

<a id="1-make-a-sql-user-for-the-tenant"></a>

## 1. Crear un usuario SQL para el tenant

Del lado de Loom, como operador ([acceso](https://github.com/ClickHouse/loom/blob/main/docs/reference/sql-surface.md#access)):

```bash
loom sql-access grant --user acme_bi --org acme --namespaces support,research
```

Imprime la contraseña del usuario una sola vez.

<a id="2-install-the-extension"></a>

## 2. Instalar la extensión

La extensión está en las imágenes `ghcr.io/clickhouse/pg_clickhouse:<postgres major>-<version>` y en el [catálogo PGXN](https://pgxn.org/dist/pg_clickhouse/), para compilarla en un Postgres autogestionado (`pgxn install pg_clickhouse`). Un servicio de Postgres gestionado debe incluirla y permitirla. Intervienen tres roles:

- un superusuario para este paso, ya que la extensión no está marcada como trusted (en un servicio gestionado, el rol que el proveedor deja crear sus extensiones permitidas);
- un propietario para los pasos 3 y 4: un rol con `CREATEROLE`, `CREATE` sobre la base de datos y `USAGE` sobre el foreign data wrapper, que crea el rol lector y por ello puede establecer sus valores por defecto;
- el rol lector de la aplicación (aquí `app_reader`) para el paso 5, que necesita su user mapping del paso 3 y los permisos del final del paso 4.

Como superusuario:

```sql
CREATE EXTENSION pg_clickhouse;
CREATE ROLE app_owner LOGIN CREATEROLE PASSWORD '<password>';
GRANT CREATE ON DATABASE app TO app_owner;
GRANT USAGE ON FOREIGN DATA WRAPPER clickhouse_fdw TO app_owner;
```

Como propietario:

```sql
CREATE ROLE app_reader LOGIN PASSWORD '<password>';
```

<a id="3-point-it-at-the-clickhouse-service"></a>

## 3. Apuntarla al servicio de ClickHouse

```sql
CREATE SERVER loom_ch FOREIGN DATA WRAPPER clickhouse_fdw
  OPTIONS (driver 'http', host 'abc123.us-east-1.aws.clickhouse.cloud', dbname 'loom');

-- The import in step 4 connects as the role that runs it; the queries in step 5 as theirs.
CREATE USER MAPPING FOR CURRENT_USER SERVER loom_ch
  OPTIONS (user 'acme_bi', password '<password from step 1>');
CREATE USER MAPPING FOR app_reader SERVER loom_ch
  OPTIONS (user 'acme_bi', password '<password from step 1>');
```

- `dbname` es la base de datos de Loom (`LOOM_CH_DATABASE`, `loom` por defecto).
- En un host de ClickHouse Cloud, el valor por defecto `secure 'auto'` de la extensión activa TLS y el puerto del driver `http` es 8443 por defecto, como indica su documentación; la prueba de aquí usa HTTP sin cifrar contra un servidor local. En un servidor autogestionado, establece `port` y `secure 'on'` para que coincidan con el puerto TLS del servidor.
- Mapea solo los roles de Postgres que pueden leer los datos del tenant: el user mapping contiene la contraseña de ClickHouse, y toda consulta a través del servidor se ejecuta como ese usuario SQL.

Establece los ajustes de sesión de la extensión en el rol, de modo que cada sesión envíe los cuatro que un usuario SQL de la superficie puede cambiar y nada más:

```sql
ALTER ROLE app_reader SET pg_clickhouse.session_settings =
  'join_use_nulls 1, group_by_use_nulls 1, final 1, transform_null_in 0';
```

Son los valores por defecto de la extensión, escritos explícitamente para que los valores por defecto de una versión posterior no puedan añadir otro. Una sesión que establece otro ajuste falla en ClickHouse con `READONLY`: el perfil del usuario SQL lo rechaza ([acceso](https://github.com/ClickHouse/loom/blob/main/docs/reference/sql-surface.md#access)), diga lo que diga el rol.

<a id="4-import-the-views"></a>

## 4. Importar las vistas

```sql
CREATE SCHEMA loom;
IMPORT FOREIGN SCHEMA loom
  LIMIT TO (sql_v1_memories, sql_v1_memory_history, sql_v1_conversations,
            sql_v1_agent_events, sql_v1_usage_daily)
  FROM SERVER loom_ch INTO loom;
```

El primer `loom` es la base de datos de ClickHouse y el segundo el esquema de Postgres. Nombra las vistas con `LIMIT TO`, de modo que la importación fije el conjunto `v1` y una vista `sql_v2_*` posterior no llegue sin pedirla. Después permite que el rol lector las lea:

```sql
GRANT USAGE ON SCHEMA loom TO app_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA loom TO app_reader;
```

Una vista `v1` puede ganar columnas en una actualización de Loom. Una tabla foránea conserva las columnas con las que se importó, así que para obtener las nuevas elimina el esquema (y cualquier vista de Postgres construida sobre sus tablas) y ejecuta de nuevo la importación y los permisos: `DROP SCHEMA loom CASCADE; CREATE SCHEMA loom;`.

<a id="5-query-and-join"></a>

## 5. Consultar y unir

```sql
-- The facts Loom holds about each account on the support team
SELECT a.email, m.memory_key, m.value, m.updated_at
FROM accounts AS a
JOIN loom.sql_v1_memories AS m ON m.user_id = a.loom_user_id
WHERE a.team = 'support' AND m.status = 'active'
ORDER BY m.updated_at DESC;

-- Model spend per day for the last month
SELECT day, sum(upstream_cost_usd) AS spend
FROM loom.sql_v1_usage_daily
WHERE day >= (now() AT TIME ZONE 'UTC')::date - 30
GROUP BY day ORDER BY day;
```

`value` es NULL cuando Loom lo retiene; `value_withheld` indica el motivo.

<a id="cost-and-scale"></a>

## Costo y escala

- `EXPLAIN (VERBOSE)` muestra qué parte de una consulta se ejecuta en ClickHouse. Una unión entre una tabla foránea y una local se ejecuta en Postgres, que obtiene todas las filas de la vista que dejan los filtros delegados, así que filtra cada vista tan estrictamente como la consulta lo permita: por espacio de nombres, por tiempo, por una lista literal de ids.
- Un filtro delegado reduce lo que ClickHouse envía a Postgres, no lo que lee: cada vista lee todo el alcance del permiso, y las vistas del registro de uso, su ventana de unos 31 días ([qué cuesta una lectura](https://github.com/ClickHouse/loom/blob/main/docs/reference/sql-surface.md#what-a-read-costs)).
- Una sentencia que supera los límites del usuario SQL (`LOOM_CH_ANALYSIS_MAX_EXECUTION_S`, `_MAX_ROWS_TO_READ`, `_MAX_MEMORY_BYTES`) falla en ClickHouse en lugar de ralentizarse, y Postgres devuelve su error.
- Toda sesión de Postgres que consulta a través de los mappings inicia sesión como el mismo usuario SQL, y ese usuario ejecuta como máximo `LOOM_CH_ANALYSIS_MAX_CONCURRENT` sentencias a la vez: dimensiona el pool de sesiones lectoras de la aplicación por debajo de ese valor, o las sentencias que lo superen fallarán.

<a id="copies-and-erasure"></a>

## Copias y borrado

Las tablas foráneas no guardan nada: cada consulta lee ClickHouse, así que un borrado en Loom les llega de inmediato. Una copia tomada en este Postgres (`CREATE TABLE … AS SELECT` desde una tabla foránea, una vista materializada, un volcado) son datos propios de la organización y quedan fuera del borrado de Loom.

<a id="revoking"></a>

## Revocar

`loom sql-access revoke --user acme_bi` detiene de inmediato todas las consultas a través de los user mappings: la siguiente falla al iniciar sesión. Después, como propietario, `DROP SERVER loom_ch CASCADE` elimina ambos user mappings y las tablas foráneas importadas.
