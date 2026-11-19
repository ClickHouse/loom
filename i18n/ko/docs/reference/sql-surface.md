<!-- i18n: source=docs/reference/sql-surface.md blob=cbe1b629ce7bff1c19c5d51c3a461f058ad2fd2c model=claude-sonnet-5-5 -->
> 이 페이지는 [영어 원문](https://github.com/ClickHouse/loom/blob/main/docs/reference/sql-surface.md)을 기계 번역한 것이며, 영어 원문이 기준입니다.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/reference/sql-surface.md), which is authoritative.

<a id="sql-surface"></a>

# SQL 인터페이스

SQL 인터페이스는 테넌트의 메모리, 메모리 기록, 대화, 요청 원장을 읽기 전용으로 보여 주는 ClickHouse 뷰 모음입니다. 조직은 자체 SQL 클라이언트, BI 도구, 웨어하우스 작업에서 `loom sql-access`가 만든 ClickHouse 사용자로 이 뷰를 읽을 수 있습니다. 이 사용자는 뷰만 읽을 수 있고 다른 것은 읽을 수 없으며, 각 뷰에는 해당 사용자에게 권한이 부여된 조직과 네임스페이스만 표시됩니다.

<a id="version-and-compatibility"></a>

## 버전 및 호환성

각 뷰의 이름에는 버전이 포함됩니다. `sql_v1_memories`는 버전 1입니다. 컬럼 이름, 타입, 의미는 공개 계약입니다.

- `v1` 뷰에는 컬럼이 추가될 수 있습니다. 컬럼이 삭제되거나 이름이 바뀌는 일은 없으며, 컬럼의 타입이나 의미도 바뀌지 않습니다.
- 이 규칙을 깨는 변경은 `v1` 옆에 `sql_v2_*` 뷰로 제공됩니다. 두 뷰는 최소 한 번의 릴리스 동안 함께 제공되며, 릴리스 노트에 제거될 `v1` 뷰가 명시됩니다.
- 컬럼은 이름으로 선택하세요. `SELECT *`는 추가된 컬럼까지 가져옵니다.

<a id="views"></a>

## 뷰

모든 뷰는 Loom의 데이터베이스에 있으며(`LOOM_CH_DATABASE`가 다른 이름을 지정하지 않는 한 `loom`) 아이덴티티 컬럼 `org`와 `namespace`를 가집니다. `sql_v1_memories`, `sql_v1_memory_history`, `sql_v1_agent_events`는 `agent`, `user_id`, `session_id`도 가지며, `sql_v1_conversations`는 `agent`와 `user_id`를, `sql_v1_usage_daily`는 `agent`를 가집니다. 조인은 양쪽이 모두 가진 [아이덴티티 튜플](https://github.com/ClickHouse/loom/blob/main/docs/concepts/identities-and-namespaces.md)의 어느 부분과도 일치시킬 수 있습니다.

<a id="sql_v1_memories"></a>

### `sql_v1_memories`

메모리(아이덴티티와 키)당 한 행이며, 현재 버전을 담습니다. 삭제되었거나 지워진 메모리는 없으며, 거부된 메모리도 없습니다. 그 밖의 상태는 `status`와 함께 나열됩니다. `memory.list`는 요청하지 않는 한 `duplicate`, `superseded`, `proposed` 행을 숨기므로, 기본 목록에 표시되는 내용을 보려면 `status = 'active'`로 필터링하세요. `expires_at`이 지난 메모리는 만료 정리 작업이 처리하기 전까지 `memory.list`가 나열하는 것처럼 값과 함께 나열됩니다. `memory.search`가 제공하는 내용만 보려면 `(expires_at IS NULL OR expires_at > now())`를 추가하세요.

| 컬럼                                                               | 의미                                                                                               |
| ------------------------------------------------------------------ | -------------------------------------------------------------------------------------------------- |
| `org`, `namespace`, `agent`, `user_id`, `session_id`, `run_id`     | 메모리의 소유자와 이를 기록한 실행(없으면 `-`)                                                     |
| `memory_key`, `memory_id`, `memory_type`                           | 키, 메모리의 id(`version`과 함께 하나의 버전을 이룸), 유형(`fact`, `decision`, ...)                |
| `value`                                                            | `memory.get`이 제공하는 값이며, 제공이 보류되면 NULL                                               |
| `value_withheld`                                                   | `value`가 제공되면 `''`, `high` 행이면 `sensitive`, 가려진 버전이면 `redacted`                     |
| `content_sha256`                                                   | 값의 SHA-256이며, `value`가 NULL일 때마다 NULL: 짧은 시크릿의 해시는 추측될 수 있기 때문           |
| `version`, `status`, `confidence`, `pinned`, `tags`                | `memory.get`과 동일                                                                                |
| `sensitivity`, `source_class`, `actor`                             | 레이블, 출처 분류, 이를 기록한 토큰 subject                                                        |
| `created_at`, `updated_at`, `expires_at`, `valid_at`, `invalid_at` | 기록된 시각과 마지막으로 변경된 시각, 만료 시각, 사실이 참이 된 시각과 참이 아니게 된 시각         |

<a id="sql_v1_memory_history"></a>

### `sql_v1_memory_history`

모든 메모리의 버전당 한 행이며, 삭제로 생긴 버전도 포함합니다(`status =
'tombstoned'`, `tombstoned_at` 설정됨). 거부된 버전은 없습니다. 컬럼은 `sql_v1_memories`에서 `expires_at`, `valid_at`, `invalid_at`을 뺀 것에 `tombstoned_at`을 더한 것입니다. `value`와 `content_sha256`은 가려진 버전과, 한 번이라도 `high`로 레이블된 키의 모든 버전에서 NULL입니다. 민감도 정리 작업이 행에 레이블을 붙이기 전의 버전은 레이블 없이 같은 값을 담고 있기 때문입니다.

<a id="sql_v1_conversations"></a>

### `sql_v1_conversations`

각 사용자의 저장된 대화 턴당 한 행이며 컬럼은 `org`, `namespace`, `agent`, `user_id`, `thread_id`, `msg_id`, `role`, `content`, `tokens`, `run_id`, `ts`(턴의 가장 최근 기록)입니다. 삭제되었거나 압축되어 사라진 턴(`tombstoned_at` 설정됨)은 없으며, 지워진 턴은 행과 함께 사라집니다. 턴에는 민감도 레이블이 없으므로 `content`는 `conversation.get`이 스레드의 턴을 제공하는 것처럼 기록된 그대로 제공됩니다. 스레드 간 목록에서 `high`로 읽히는 텍스트를 검사하는 작업은 SQL이 아니라 Loom에서 실행됩니다.

<a id="sql_v1_agent_events"></a>

### `sql_v1_agent_events`

요청 원장입니다. op 호출 또는 워커 패스당 한 행이며, 요청이면 `kind`가 `''`입니다.
`ts`, `org`, `namespace`, `agent`, `user_id`, `session_id`, `run_id`, `operation`, `kind`,
`request_id`, `trace_id`, `latency_ms`, `success`, `error_code`, `status_class`,
`memory_key`, `input_tokens`, `output_tokens`, `embedding_tokens`, `llm_calls`,
`upstream_cost_usd`(모델 비용, USD), `bytes_written`, `model`, `consumer`, `subject`
(호출한 토큰 subject), `transport`를 담습니다. 요청 인자는 담기지 않습니다. 지워진 최종 사용자의 행은 `user_id`가 `<erased>`로 읽힙니다.

오늘 이전의 UTC 기준 31일 전체와 오늘 현재까지를 담으며, 각 문은 자체 `now()`를 읽습니다. 윈도우 길이와 UTC 일 단위 정렬은 `v1` 계약의 일부입니다.

<a id="sql_v1_usage_daily"></a>

### `sql_v1_usage_daily`

같은 윈도우에 대해 `sql_v1_agent_events`를 UTC `day`, `org`, `namespace`, `agent`, `operation`, `kind`별로 합산한 뷰입니다: `ops`, `errors`, `input_tokens`, `output_tokens`, `embedding_tokens`, `llm_calls`, `upstream_cost_usd`, `bytes_written`, `latency_sum_ms`(`ops`와 함께 행 전체의 평균을 구하는 데 사용), 그리고 근사값이며 행별인 `latency_p50_ms`와 `latency_p95_ms`로, 이는 여러 행에 걸쳐 합칠 수 없습니다. 오늘을 제외한 모든 `day`는 하루 전체입니다.

<a id="what-a-read-costs"></a>

### 읽기 비용

뷰는 호출자의 필터를 반환하는 행에 대해 실행하므로, `ts`, `day`, `thread_id`, `memory_key`에 대한 필터는 결과를 좁힐 뿐 읽는 양을 줄이지 않습니다. `sql_v1_memories`, `sql_v1_memory_history`, `sql_v1_conversations`를 읽을 때마다 권한(grant)의 전체 범위를 읽습니다. 히스토리는 범위 안의 모든 버전을 정렬하고, 대화 뷰는 범위 안의 모든 턴을 content까지 포함해 그룹화합니다. 이는 범위에 저장된 양에 따라 늘어나며, 넓은 범위는 아래의 읽기 상한을 초과할 수 있습니다. 원장 뷰를 읽을 때마다 윈도우 동안 권한의 전체 범위를 읽는데, 많아도 윈도우가 시작되는 달의 시작부터이며 보통은 윈도우와 거의 같습니다. 더 넓은 권한(`*` 네임스페이스, `--all`)은 더 많이 읽습니다. 이 읽기에는 SQL 사용자의 읽기 상한(`LOOM_CH_ANALYSIS_MAX_ROWS_TO_READ`, `_MAX_EXECUTION_S`, `_MAX_MEMORY_BYTES`, `_MAX_THREADS`)이 적용되므로 바쁜 범위는 이를 초과할 수 있습니다. 이 경우 권한을 좁히거나 `insights.query`가 공유하는 해당 설정을 높인 뒤 `loom sql-access grant`를 다시 실행하세요. 이 명령이 설정을 SQL 사용자의 프로필에 기록합니다. 윈도우보다 오래된 이벤트는 이 인터페이스에 없으며, [분석 내보내기](https://github.com/ClickHouse/loom/blob/main/docs/operations/analytics-export.md)는 일별 사용량 합계를 해시된 이름으로 운영자의 버킷에 기록합니다. `loom.agent_events` 자체에는 `LOOM_RETENTION_AGENT_EVENTS_DAYS`로 설정하지 않는 한 TTL이 없습니다.

<a id="what-the-views-never-show"></a>

## 뷰가 절대 보여 주지 않는 것

메모리 뷰는 Loom 자체 목록의 규칙([민감도](https://github.com/ClickHouse/loom/blob/main/docs/architecture/security.md#sensitivity))을 적용합니다. 기록 뷰는 위의 `conversation.get`과 같은 방식으로 턴을 제공합니다. 어느 뷰도 다음을 보여 주지 않습니다.

- `high` 행의 값과 그 해시. 현재 뷰와 기록 내 해당 키의 모든 버전에 모두 해당합니다.
- 마스킹된 버전의 값과 그 해시
- 거부된 메모리, 삭제되었거나 지워진 메모리, 삭제된 대화 턴
- `loom.namespaces` 또는 `loom.code_repos`에서 `restricted`로 분류된 네임스페이스의 모든 행. 권한 부여와 관계없이, 분류된 시점부터 적용됩니다. Loom은 [restricted 네임스페이스](https://github.com/ClickHouse/loom/blob/main/docs/architecture/security.md#restricted-namespaces)에 대한 모든 읽기를 감사하지만, 뷰는 그렇게 할 수 없습니다.
- 미러(`acl-`과 16진수 24자리로 이루어진 이름의 네임스페이스)의 모든 행. 권한 부여와 관계없습니다. 각 문서는 해당 저장소의 권한 목록이 허용하는 독자에게만 보이며([문서 소스](https://github.com/ClickHouse/loom/blob/main/docs/architecture/document-sources.md#the-mirror)), 뷰는 이를 확인할 수 없습니다.
- 다섯 개의 뷰 밖에 있는 테이블: 토큰, 권한 부여, 시크릿, 감사 로그, 문서, 임베딩, 요청 인자
- 다른 사용자의 쿼리문을 보여 주는 `system` 테이블(예: `system.query_log`, `system.mutations`, `system.processes`). ClickHouse는 `access_control_improvements.select_from_system_db_requires_grant`가 켜져 있을 때(기본값)에만 권한 없는 사용자에게 이를 거부합니다. 그래서 `loom sql-access grant`는 비밀번호를 설정하는 각 사용자로 로그인한 뒤 `system.processes`가 응답하면 해당 사용자를 삭제합니다. Loom 데이터베이스에서 `system.tables`는 다섯 개의 뷰만 나열합니다.

메모리 뷰와 기록 뷰는 [삭제 사가](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/retention-and-erasure.md)가 삭제 대상으로 삼고 잔여 집계가 세는 테이블을 읽으므로, 삭제된 사용자나 네임스페이스의 행은 테이블과 함께 뷰에서도 사라집니다. 원장 뷰는 내용이 없는 행을 그대로 유지합니다. 사용자 삭제는 `loom.agent_events`에서와 마찬가지로 해당 사용자의 `user_id`를 `<erased>`로 덮어씁니다.

<a id="freshness"></a>

## 최신성

뷰는 쿼리 시점에 ClickHouse를 읽으며, 어떤 데이터도 복사하지 않습니다. 라이브 메모리는 Postgres가 보유하고([저장소 권한](https://github.com/ClickHouse/loom/blob/main/docs/architecture/storage-authority.md)), `loom.memory`와 `loom.memory_versions`는 각 커밋 이후 게시되는 프로젝션으로, `LOOM_PG_PUBLISH_INTERVAL_MS`마다(기본값 250 ms) 플러시를 통해 갱신됩니다. 커밋된 메모리 쓰기는 플러시가 완료되면 `sql_v1_memories`와 `sql_v1_memory_history`에 나타나며, 보통 한 주기 안에 반영됩니다. 중단된 writer가 남긴 전달 건은 delivery worker가 게시하며, 이 경우 더 오래 걸릴 수 있습니다. 뷰는 아무것도 기다리지 않습니다. `LOOM_PG_PROJECTION_WAIT_MS`를 넘기면 `PROJECTION_BEHIND`로 응답하는 `memory.search`와 달리, 뷰는 ClickHouse에 있는 데이터를 그대로 제공합니다. 대화와 요청 원장은 ClickHouse에 직접 기록됩니다.

<a id="access"></a>

## 접근

이 서피스의 SQL 사용자는 다섯 개의 뷰에 대한 `SELECT` 권한만 있고 다른 권한은 없는 ClickHouse 사용자이며, 설정 프로필 `loom_sql_surface`를 사용합니다.

- `readonly = 1`: 문은 `final`, `join_use_nulls`, `group_by_use_nulls`, `transform_null_in` 외에는 어떤 설정도 바꾸지 못합니다. 이 네 설정은 각 뷰의 행을 그대로 두며, 클라이언트가 스스로 보내기도 합니다(`pg_clickhouse` 등). 다른 설정은 뷰의 필터를 중간에 끊을 수 있습니다. 예를 들면 overflow mode, `additional_table_filters`, `FINAL` 아래의 PREWHERE가 있습니다.
- 서버의 기본값과 관계없이 프로필이 고정하는 값: `apply_deleted_mask = 1`과 `apply_patch_parts = 1`(삭제된 행은 계속 숨겨집니다), `use_query_cache = 0`과 `use_query_condition_cache = 0`(둘 다 모든 사용자가 공유하는 문의 텍스트를 키로 삼습니다), `use_skip_indexes_if_final_exact_mode = 1`, `do_not_merge_across_partitions_select_final = 0`, `optimize_move_to_prewhere_if_final = 0`(`FINAL`은 각 키의 최신 버전을 읽습니다), 모든 overflow mode는 `'throw'`(read와 timeout 및 그 `_leaf` 형태, set, transfer, join, sort, distinct, group by, result), `send_logs_level = 'none'`, introspection 함수 사용 불가, 그리고 `query_plan_filter_push_down = 0`과 `enable_optimize_predicate_expression = 0`입니다. 따라서 호출자의 필터는 뷰가 반환한 결과에만 적용되며 다른 테넌트의 행에는 적용되지 않습니다(값에 대해 오류를 일으키는 필터는 그 값을 오류 메시지에 인용할 수 있습니다).
- `insights.query`의 한도. 단, SQL 클라이언트는 결과 집합 전체를 읽으므로 결과 상한은 제외합니다: `LOOM_CH_ANALYSIS_MAX_EXECUTION_S`, `_MAX_ROWS_TO_READ`, `_MAX_MEMORY_BYTES`, `_MAX_THREADS`, 그리고 동시에 실행할 문의 수 `_MAX_CONCURRENT`.

각 뷰는 정의자의 권한(`SQL SECURITY DEFINER`, 마이그레이션을 실행한 사용자)으로 실행되며, 테이블 `loom.sql_access`가 `currentUser()`, 즉 문을 보낸 사용자에게 부여한 `(org, namespace)`의 행만 허용합니다. 부여된 행이 없는 사용자는 아무것도 읽지 못하며, Loom 자체를 포함한 다른 모든 ClickHouse 사용자도 마찬가지입니다.

```bash
loom sql-access grant --user acme_bi --org acme --namespaces support,research
loom sql-access grant --user acme_bi --org acme --namespaces '*'   # every namespace of acme
loom sql-access grant --user ops_bi --all                          # the whole deployment
loom sql-access grant --user acme_bi --org acme --namespaces support --rotate
loom sql-access list
loom sql-access revoke --user acme_bi
```

- `grant`는 사용자가 없으면 만들고 비밀번호를 한 번만 출력합니다. Loom은 사본을 보관하지 않습니다. `--org`와 `--all`은 함께 쓸 수 없고, `--all`에는 `--namespaces`를 쓸 수 없습니다. 다시 실행하면 사용자의 범위가 명시한 내용과 정확히 일치하도록 설정됩니다. 더 이상 명시하지 않은 네임스페이스는 새 행이 읽기를 처리하는 레플리카에 도달한 뒤부터 보이지 않습니다. `revoke`는 로그인을 즉시 끝냅니다. `--rotate`는 새 비밀번호를 출력합니다. 비밀번호 이후의 단계가 실패하면 `grant`가 그렇게 알려 주며, 이때는 `--rotate`를 붙여 다시 실행하세요.
- `--all`은 데이터가 하나의 org에 속한 배포를 위한 것입니다. 공유 배포에서는 각 테넌트에 자신의 org를 부여하세요.
- `grant`는 제한된 네임스페이스를 명시한 범위를 거부하며, 와일드카드가 그런 네임스페이스에 닿는 경우도 거부합니다. 다만 이는 코드 저장소 설정만 분류하는 경우에 한합니다. 뷰는 그 설정을 읽을 수 없으므로 서버가 쓰는 설정으로 명령을 실행하세요. 어떤 네임스페이스가 그에 닿는 grant 이후에 제한되었다면 그 grant의 뷰에 포함되어 있습니다. `list`는 이런 grant를 각각 알려 주며, 다른 네임스페이스를 이름으로 지정해 교체해야 하고, 종료 코드 2로 끝납니다.
- `revoke`는 먼저 범위 행을 제거한 다음 사용자를 삭제합니다. grant가 명시한 네임스페이스는 `revoke`가 그 행을 제거할 때까지 사용 중으로 간주됩니다. 그 이름은 새 소유자에게 넘어가지 않으며, 그 네임스페이스에 처음 쓰면 소유자 없이 등록됩니다. `*` grant는 네임스페이스를 명시하지 않습니다. 이후에 만들어지는 것을 포함해 org의 모든 네임스페이스를 읽으므로, 가입자들이 공유하는 org(`LOOM_SELF_SIGNUP_ORG`)에는 절대 부여하지 마세요.
- 명령은 `LOOM_CH_USER`로 실행되며, 이 사용자에게는 접근 관리 권한이 필요합니다: `CREATE USER`, `ALTER USER`, `DROP USER`, `CREATE SETTINGS PROFILE`, `ALTER SETTINGS PROFILE`, 다섯 개 뷰에 대한 `SELECT ... WITH GRANT OPTION`, 그리고 `SHOW USERS`와 `system.users`, `system.settings_profile_elements` 읽기 권한입니다. `loom_sql_surface` 프로필에 속하지 않은 ClickHouse 사용자가 이미 쓰고 있는 이름과 Loom 자체의 사용자(`LOOM_CH_USER`, reader 사용자, `loom_analyst`, `default`)는 거부합니다.
- 사용자는 `DROP USER`가 아니라 `revoke`로 삭제하세요. `revoke`가 범위 행을 다시 쓸 때까지 그 행은 부여된 채로 남으며, 같은 이름의 이후 사용자가 뷰에 대한 `SELECT`를 가지면 그 행을 읽게 됩니다.

<a id="querying"></a>

## 쿼리

조직의 Postgres에서 쿼리하려면 [Postgres에서 Loom 쿼리하기](https://github.com/ClickHouse/loom/blob/main/docs/guides/query-from-postgres.md)를 참고하세요.

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
