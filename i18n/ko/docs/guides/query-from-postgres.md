<!-- i18n: source=docs/guides/query-from-postgres.md blob=c49542f7fffa2ee2ced68d49d8ab0829854132f8 model=claude-sonnet-5-5 -->
> 이 페이지는 [영어 원문](https://github.com/ClickHouse/loom/blob/main/docs/guides/query-from-postgres.md)을 기계 번역한 것이며, 영어 원문이 기준입니다.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/guides/query-from-postgres.md), which is authoritative.

<a id="query-loom-from-postgres"></a>

# Postgres에서 Loom 쿼리하기

이 가이드는 테넌트의 메모리, 대화, 사용량을 조직 자체의 Postgres에서 읽는 방법을 설명합니다. 이렇게 읽으면 애플리케이션의 테이블과 조인할 수 있습니다. 여기에서는 오픈 소스 [`pg_clickhouse`](https://github.com/ClickHouse/pg_clickhouse) 확장을 사용합니다. 이 확장은 ClickHouse 테이블과 뷰를 Postgres 외부 테이블로 가져오고, 쿼리를 ClickHouse로 푸시다운합니다. 뷰에 어떤 내용이 담기고 어떤 내용이 제외되는지는 [SQL 표면 레퍼런스](https://github.com/ClickHouse/loom/blob/main/docs/reference/sql-surface.md)에서 확인할 수 있습니다.

데이터는 ClickHouse에 그대로 남습니다. Postgres에서 보낸 쿼리는 쿼리 시점에 뷰를 읽으며, 다른 SQL 클라이언트와 동일한 범위, 보류, 최신성 규칙이 적용됩니다. 이 Postgres는 애플리케이션의 것이며 Loom의 [메모리 권한 저장소](https://github.com/ClickHouse/loom/blob/main/docs/operations/postgres.md)가 아닙니다. 확장은 읽기만 하고 Loom은 확장을 통해 쓰지 않으며, Loom 자체의 데이터베이스에는 확장이 필요하지 않습니다. Loom의 `LOOM_PG_DSN`이 가리키는 데이터베이스나 서비스와는 별도의 것을 사용하세요.

CI는 `.github/workflows/ci.yml`에서 `LOOM_PGCH_VERSION`이 지정한 릴리스를 Postgres 16과 18에서 테스트합니다 (`tests/integration/test_pg_clickhouse_live_ch.py`).

<a id="1-make-a-sql-user-for-the-tenant"></a>

## 1. 테넌트용 SQL 사용자 만들기

Loom 쪽에서 운영자로서 실행합니다 ([접근](https://github.com/ClickHouse/loom/blob/main/docs/reference/sql-surface.md#access)):

```bash
loom sql-access grant --user acme_bi --org acme --namespaces support,research
```

사용자의 비밀번호는 한 번만 출력됩니다.

<a id="2-install-the-extension"></a>

## 2. 확장 설치하기

확장은 `ghcr.io/clickhouse/pg_clickhouse:<postgres major>-<version>` 이미지와 [PGXN 저장소](https://pgxn.org/dist/pg_clickhouse/)에 있으며, 자체 관리형 Postgres에 빌드해 넣을 수 있습니다 (`pgxn install pg_clickhouse`). 관리형 Postgres 서비스는 이 확장을 제공하고 허용해야 합니다. 세 가지 역할이 관여합니다:

- 이 단계를 위한 슈퍼유저. 확장이 trusted로 표시되어 있지 않기 때문입니다 (관리형 서비스에서는 제공자가 허용된 확장을 생성하도록 해 주는 역할).
- 3단계와 4단계를 위한 소유자: `CREATEROLE`, 데이터베이스에 대한 `CREATE`, 외부 데이터 래퍼에 대한 `USAGE`를 가진 역할로, 리더 역할을 생성하므로 그 기본값을 설정할 수 있습니다.
- 5단계를 위한 애플리케이션의 리더 역할 (여기서는 `app_reader`). 3단계의 사용자 매핑과 4단계 끝의 권한 부여가 필요합니다.

슈퍼유저로 실행합니다:

```sql
CREATE EXTENSION pg_clickhouse;
CREATE ROLE app_owner LOGIN CREATEROLE PASSWORD '<password>';
GRANT CREATE ON DATABASE app TO app_owner;
GRANT USAGE ON FOREIGN DATA WRAPPER clickhouse_fdw TO app_owner;
```

소유자로 실행합니다:

```sql
CREATE ROLE app_reader LOGIN PASSWORD '<password>';
```

<a id="3-point-it-at-the-clickhouse-service"></a>

## 3. ClickHouse 서비스 지정하기

```sql
CREATE SERVER loom_ch FOREIGN DATA WRAPPER clickhouse_fdw
  OPTIONS (driver 'http', host 'abc123.us-east-1.aws.clickhouse.cloud', dbname 'loom');

-- The import in step 4 connects as the role that runs it; the queries in step 5 as theirs.
CREATE USER MAPPING FOR CURRENT_USER SERVER loom_ch
  OPTIONS (user 'acme_bi', password '<password from step 1>');
CREATE USER MAPPING FOR app_reader SERVER loom_ch
  OPTIONS (user 'acme_bi', password '<password from step 1>');
```

- `dbname`은 Loom의 데이터베이스입니다 (`LOOM_CH_DATABASE`, 기본값은 `loom`).
- ClickHouse Cloud 호스트에서는 확장의 `secure 'auto'` 기본값이 TLS를 켜고 `http` 드라이버의 포트 기본값은 8443입니다. 이는 확장 문서에 나와 있는 내용이며, 여기의 테스트는 로컬 서버에 일반 HTTP로 실행됩니다. 자체 관리형 서버에서는 서버의 TLS 포트에 맞게 `port`와 `secure 'on'`을 설정하세요.
- 테넌트의 데이터를 읽어도 되는 Postgres 역할만 매핑하세요. 사용자 매핑에는 ClickHouse 비밀번호가 들어 있으며, 이 서버를 통한 모든 쿼리는 해당 SQL 사용자로 실행됩니다.

역할에 확장의 세션 설정을 지정하면, 모든 세션이 이 표면의 SQL 사용자가 변경할 수 있는 네 가지 설정만 보내고 다른 설정은 보내지 않습니다:

```sql
ALTER ROLE app_reader SET pg_clickhouse.session_settings =
  'join_use_nulls 1, group_by_use_nulls 1, final 1, transform_null_in 0';
```

이 값들은 확장의 기본값이며, 이후 릴리스의 기본값에 설정이 추가되지 않도록 명시해 둔 것입니다. 다른 설정을 지정하는 세션은 ClickHouse에서 `READONLY`로 실패합니다. 역할에 어떻게 지정했든 SQL 사용자의 프로필이 이를 거부하기 때문입니다 ([접근](https://github.com/ClickHouse/loom/blob/main/docs/reference/sql-surface.md#access)).

<a id="4-import-the-views"></a>

## 4. 뷰 가져오기

```sql
CREATE SCHEMA loom;
IMPORT FOREIGN SCHEMA loom
  LIMIT TO (sql_v1_memories, sql_v1_memory_history, sql_v1_conversations,
            sql_v1_agent_events, sql_v1_usage_daily)
  FROM SERVER loom_ch INTO loom;
```

첫 번째 `loom`은 ClickHouse 데이터베이스이고 두 번째는 Postgres 스키마입니다. 뷰를 `LIMIT TO`로 명시하면 가져오기가 `v1` 집합에 고정되어, 이후의 `sql_v2_*` 뷰가 요청하지 않았는데 들어오는 일이 없습니다. 그다음 리더 역할이 읽을 수 있게 합니다:

```sql
GRANT USAGE ON SCHEMA loom TO app_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA loom TO app_reader;
```

`v1` 뷰는 Loom 업그레이드 때 컬럼이 추가될 수 있습니다. 외부 테이블은 가져온 시점의 컬럼을 그대로 유지하므로, 새 컬럼을 반영하려면 스키마(와 그 테이블 위에 만든 Postgres 뷰)를 삭제하고 가져오기와 권한 부여를 다시 실행하세요:
`DROP SCHEMA loom CASCADE; CREATE SCHEMA loom;`.

<a id="5-query-and-join"></a>

## 5. 쿼리와 조인

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

Loom이 값을 보류한 경우 `value`는 NULL이며, 그 이유는 `value_withheld`에 나옵니다.

<a id="cost-and-scale"></a>

## 비용과 규모

- `EXPLAIN (VERBOSE)`는 쿼리의 어느 부분이 ClickHouse에서 실행되는지 보여 줍니다. 외부 테이블과 로컬 테이블 간의 조인은 Postgres에서 실행되며, Postgres는 푸시다운된 필터를 거치고 남은 뷰의 모든 행을 가져옵니다. 따라서 쿼리가 허용하는 한 각 뷰를 좁게 필터링하세요. 네임스페이스, 시간, 리터럴 ID 목록 등으로 필터링할 수 있습니다.
- 푸시다운된 필터는 ClickHouse가 Postgres로 보내는 양을 줄일 뿐, ClickHouse가 읽는 양을 줄이지는 않습니다. 각 뷰는 권한이 부여된 전체 범위를 읽고, 원장 뷰는 약 31일 윈도우를 읽습니다 ([읽기 비용](https://github.com/ClickHouse/loom/blob/main/docs/reference/sql-surface.md#what-a-read-costs)).
- SQL 사용자의 상한(`LOOM_CH_ANALYSIS_MAX_EXECUTION_S`, `_MAX_ROWS_TO_READ`, `_MAX_MEMORY_BYTES`)을 넘는 구문은 느려지지 않고 ClickHouse에서 실패하며, Postgres가 그 오류를 반환합니다.
- 매핑을 통해 쿼리하는 모든 Postgres 세션은 같은 SQL 사용자로 로그인하며, 이 사용자는 한 번에 최대 `LOOM_CH_ANALYSIS_MAX_CONCURRENT`개의 구문을 실행합니다. 애플리케이션의 리더 세션 풀 크기를 이 값 이하로 맞추세요. 이를 넘는 구문은 실패합니다.

<a id="copies-and-erasure"></a>

## 복사본과 삭제

외부 테이블에는 데이터가 저장되지 않습니다. 모든 쿼리가 ClickHouse를 읽으므로 Loom에서 수행한 삭제가 즉시 반영됩니다. 이 Postgres에서 만든 복사본(외부 테이블에 대한 `CREATE TABLE … AS SELECT`, 구체화된 뷰, 덤프)은 조직 자체의 데이터이며 Loom의 삭제 대상이 아닙니다.

<a id="revoking"></a>

## 권한 해제

`loom sql-access revoke --user acme_bi`를 실행하면 사용자 매핑을 통한 모든 쿼리가 즉시 중단됩니다. 다음 쿼리는 로그인에 실패합니다. 그다음 소유자로서 `DROP SERVER loom_ch CASCADE`를 실행하면 두 사용자 매핑과 가져온 외부 테이블이 삭제됩니다.
