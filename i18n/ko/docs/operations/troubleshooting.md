<!-- i18n: source=docs/operations/troubleshooting.md blob=3ede5f4702a909ff463f5c011fadcc17a512eb56 model=claude-sonnet-5-5 -->
> 이 페이지는 [영어 원문](https://github.com/ClickHouse/loom/blob/main/docs/operations/troubleshooting.md)을 기계 번역한 것이며, 영어 원문이 기준입니다.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/operations/troubleshooting.md), which is authoritative.

<a id="troubleshooting"></a>

# 문제 해결

각 항목은 증상, 원인을 확인하는 점검 방법, 해결 방법을 차례로 설명합니다. 하나의 요청을 원장(ledger), 쿼리 로그, 트레이스에서 따라가려면 [요청 디버깅](https://github.com/ClickHouse/loom/blob/main/docs/guides/debugging.md)을 참고하세요.

<a id="start-here"></a>

## 시작하기

로그를 읽기 전에 다음 세 가지 명령으로 대부분의 의문을 해소할 수 있습니다.

```bash
curl -s http://localhost:7777/v1/health | jq '{status, ch_ok, ch_error, schema, problems}'
docker exec loom-server loom doctor              # the same problems, plus pre-boot checks
curl -s -XPOST http://localhost:7777/v1/ops.health -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H 'Content-Type: application/json' -d '{"org":"<org>"}' | jq   # ClickHouse parts, workers, stubs (ORG_ADMIN)
```

`problems`의 모든 항목에는 `fix`가 포함되어 있으므로 먼저 읽어 보세요. 클라이언트 머신에서는 `loom doctor --client`가 SDK와 hook이 사용하는 서버 URL과 토큰을 확인합니다. 각 필드의 의미는 [모니터링](https://github.com/ClickHouse/loom/blob/main/docs/operations/monitoring.md#health-checks)에서 확인할 수 있습니다.

<a id="server-and-clickhouse"></a>

## 서버와 ClickHouse

<a id="every-operation-fails-and-v1health-returns-503"></a>

### 모든 작업이 실패하고 /v1/health가 503을 반환함

- **원인:** 서버에서 ClickHouse에 연결할 수 없습니다. 부팅도 첫 번째 마이그레이션에서 멈춥니다.
- **확인:** `curl -s localhost:7777/v1/health | jq .ch_error`를 실행한 뒤
  `docker exec loom-server loom doctor`를 실행합니다(`clickhouse_reachable` 줄).
- **해결:** `LOOM_CH_HOST` / `LOOM_CH_PORT` / `LOOM_CH_USER` / `LOOM_CH_PASSWORD`를 바로잡습니다.
  포트 8443은 TLS를 의미하며, 다른 포트에서 TLS를 쓰려면 `LOOM_CH_SECURE=true`를 설정합니다.
  ClickHouse Cloud에서는 서비스가 실행 중인지(유휴 서비스는 일시 중지됨), 그리고 IP 허용 목록에
  Loom 호스트가 포함되어 있는지 확인합니다.

<a id="loom-up-times-out-or-the-container-exits-at-boot"></a>

### loom up이 시간 초과되거나 컨테이너가 부팅 중 종료됨

- **원인:** 대개 마이그레이션 또는 ClickHouse 오류, 잘못된 `.env` 값, 또는 compose 파일 밖에서
  `LOOM_INSTANCE_LABEL` 없이 실행한 프로세스입니다.
- **확인:** `docker logs loom-server`를 실행하면 실패한 마이그레이션 파일이나 설정이 표시됩니다.
  env 파일은
  `docker compose --env-file .env -f dev/docker-compose.cloud.yml config >/dev/null`
  로 검증합니다(부팅에 사용하는 compose 파일을 지정하세요).
- **해결:** 로그에 나온 오류를 해결합니다. 인증이 켜져 있고 저장소에 포함된 개발용 `LOOM_JWT_SECRET`을 쓰는
  배포(`LOOM_DEPLOYMENT=1`)는 설계상 시작을 거부합니다. 실제 시크릿을 설정하세요
  ([배포](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#required-environment)).

<a id="writes-fail-with-too_many_parts-or-merges-are-failing"></a>

### 쓰기가 TOO_MANY_PARTS로 실패하거나 병합이 실패함

- **증상:** 쓰기가 ClickHouse 오류 252 `TOO_MANY_PARTS`로 실패하거나, `ops.health`가
  `clickhouse_parts: fail`을 보고합니다.
- **확인:** 병합이 실패하고 있는지 확인합니다. 쓰기 속도가 아니라 거의 항상 이것이 원인입니다.
  먼저 `system.part_log`에서 병합 오류를 읽습니다.

  ```sql
  SELECT table, error, count() AS failures, any(exception)
  FROM system.part_log
  WHERE database = 'loom' AND event_type = 'MergeParts' AND error != 0
    AND event_time > now() - INTERVAL 1 HOUR
  GROUP BY table, error;
  ```

- **해결:** 예외 메시지가 서버가 병합할 수 없는 스킵 인덱스를 지목하면, 그 인덱스를 병합 시점
  구체화에서 제외합니다. 이 설정은 메타데이터만 바꾸며 되돌릴 수 있습니다. 삽입된 파트는
  여전히 인덱스를 빌드합니다.

  ```sql
  ALTER TABLE loom.<table> MODIFY SETTING
    exclude_materialize_skip_indexes_on_merge = '<existing list>,<index name>';
  ```

  병합이 성공하면 파트 수는 몇 분 안에 줄어듭니다. 인덱스를 먼저 삭제하지 마세요(나중에 다시 빌드하면
  테이블을 다시 쓰게 됩니다). `parts_to_throw_insert`만 올리는 것도 피하세요. 병합이 멈춘 상태에서는
  파트 수가 계속 늘어납니다. 실패하는 병합이 없다면 문 하나에 행 하나씩 삽입하는 라이터를 찾습니다
  (`system.query_log`를 `log_comment`로 그룹화, [쿼리 귀속](https://github.com/ClickHouse/loom/blob/main/docs/operations/monitoring.md#logs-traces-and-query-attribution)).

<a id="writes-take-seconds-after-a-clickhouse-cloud-replica-restart"></a>

### ClickHouse Cloud 레플리카 재시작 후 쓰기에 몇 초가 걸림

- **증상:** 읽기는 빠른데 쓰기 위주 작업(`memory.set`, `run.*`, `conversation.append`)에
  몇 초가 걸리고 오류는 없습니다. 직전의 배포가 원인이 아닌 경우가 많습니다.
- **확인:** 레플리카 가동 시간과 진행 중인 시스템 로그 재연결을 확인합니다.

  ```sql
  SELECT hostName(), uptime() FROM clusterAllReplicas(default, system.one);
  SELECT count() FROM system.query_log
  WHERE event_time > now() - INTERVAL 15 MINUTE AND query ILIKE 'ATTACH TABLE system.%';
  ```

- **해결:** Loom 쪽에서 할 일은 없습니다. 재시작 후 서비스가 한동안 시스템 테이블을 다시 연결하며,
  이 작업이 끝나면 삽입이 정상으로 돌아옵니다(보통 1시간 이내). 이 때문에 배포를 롤백하지 마세요.
  계속되면 ClickHouse Cloud 지원팀에 문의하세요.

<a id="requests-fail-with-backend_timeout-or-overloaded"></a>

### 요청이 BACKEND_TIMEOUT 또는 OVERLOADED로 실패함

- **원인:** ClickHouse 문이 해당 클래스의 데드라인을 넘겼거나(504 `BACKEND_TIMEOUT`), 프로세스의
  풀링된 ClickHouse 연결이 모두 계속 사용 중이었습니다(503 `OVERLOADED`). 쓰기 속도가 지속되면
  배치 삽입(감사 행, 트랜스크립트 턴, agent_events 플러시)은 최대
  `LOOM_CH_BATCHED_INSERT_WAIT_MAX_MS` 동안 연결을 점유한 뒤 플러시하고, 백그라운드 삽입(사용량
  이벤트 및 쓰기 표시 플러시)은 최대 `LOOM_CH_BATCHED_INSERT_BACKGROUND_WAIT_MAX_MS` 동안
  점유한 뒤 플러시합니다
  ([배치 삽입](../architecture/clickhouse.md#batched-inserts)).
  `OVERLOADED`는 어떤 문이 ClickHouse의 서버 또는 ClickHouse 사용자 메모리가 부족한 상태를 만났다는
  뜻일 수도 있습니다(코드 241, 모든 쿼리가 공유하며, 서버 로그에 ClickHouse의 해당 줄과 함께
  `ClickHouse is short of memory`가 5초에 한 번 남습니다).
  `conversation.delete` 또는 `conversation.compact`에서는 `OVERLOADED`가 스레드 임베딩에 대한 쓰기가
  8번의 조정 라운드 동안 계속 들어왔다는 뜻일 수도 있습니다. 스레드의 쓰기가 가라앉은 뒤 다시 보내세요.
- **확인:** `system.query_log`에서 `log_comment`별로 가장 느린 소스를 봅니다
  ([모니터링](https://github.com/ClickHouse/loom/blob/main/docs/operations/monitoring.md#logs-traces-and-query-attribution)). 파트 부하는 `ops.health`로 확인합니다. 메모리는
  `system.query_log`에서 `exception_code = 241`인 문과 그 옆의 가장 큰 `memory_usage`를 서버의
  `max_server_memory_usage` 및 사용자의 `max_memory_usage_for_user`와 비교합니다.
- **해결:** 지배적인 쿼리나 워커를 해결합니다. 메모리라면 해당 문을 고치거나 ClickHouse 서비스의
  메모리를 늘립니다. ClickHouse와 멀리 떨어진 Loom(WAN)은 지연 시간 예산이 호출마다 초과되지 않도록
  `LOOM_LATENCY_BUDGET_MULTIPLIER`를 20 근처로 설정해야 합니다. 쓰기 속도가 지속되는 상황의
  `OVERLOADED`에서는 배치 삽입이 점유하는 연결에 맞게 풀 크기를 정합니다
  ([배치 삽입](../architecture/clickhouse.md#batched-inserts)). `LOOM_CH_POOL_MAXSIZE`를 올리거나
  (재시작 필요), 전환된 그룹에서 `loom settings set ch_batched_inserts false`로 배치를 끄거나
  (시작 시에는 `LOOM_CH_BATCHED_INSERTS=false`, 대기하는 비동기 삽입은 계속 대기), 삽입 대기 시간을
  `loom settings set ch_batched_insert_wait_max_ms <ms>`(및
  `ch_batched_insert_background_wait_max_ms`)로 실시간으로 낮춥니다.
  [런타임 설정](https://github.com/ClickHouse/loom/blob/main/docs/operations/runtime-settings.md)에서 실시간 재정의를 다룹니다.
  [오류 레퍼런스](https://github.com/ClickHouse/loom/blob/main/docs/reference/errors.md)에 모든 코드가 있습니다.

<a id="providers-and-search-quality"></a>

## 프로바이더와 검색 품질

<a id="searches-fail-with-503-embed_timeout"></a>

### 검색이 503 EMBED_TIMEOUT으로 실패함

- **원인:** 임베딩 프로바이더가 기본적으로 두 번의 시도(재시도 1회, `LOOM_MEMORY_SEARCH_EMBED_BOUND_RETRIES`) 모두에서 `LOOM_MEMORY_SEARCH_EMBED_BOUND_S`(2.5 s) 안에 쿼리 임베딩에 응답하지 않았습니다. 벡터가 임베딩 캐시에 있는 쿼리는 계속 응답합니다.
- **확인:** 서버 로그의 `query embed exceeded …s (attempt n/m)` 경고와 `error_code = 'EMBED_TIMEOUT'`인 `loom.agent_events` 행을 확인하세요. 응답하지 않고 멈춰 있는 프로바이더는 상태 플래그를 설정하지 않으며, `/v1/health`는 프로바이더가 오류도 반환할 때만 `provider_failing:<name>`을 표시합니다. 프로바이더의 상태 페이지도 확인하세요.
- **해결:** 원시 HTTP 또는 MCP 호출자는 한 번 재시도하세요. SDK 호출자는 이미 재시도했으므로 대기 후 다시 시도하세요. 반복되면 프로바이더가 느리거나 중단된 것이므로 복구를 기다리거나, 느리지만 응답은 하는 프로바이더라면 제한 시간을 늘리세요. 실패한 검색은 시도마다 최대 제한 시간만큼 서버를 점유합니다.

<a id="search-returns-arbitrary-results-or-extraction-writes-nothing"></a>

### 검색이 임의의 결과를 반환하거나 추출이 아무것도 기록하지 않음

- **증상:** `set_from_messages`가 팩트를 기록하지 않거나, `memory.ask`가 빈 답변을 반환하거나, 검색이 문자 중복도로 순위를 매깁니다.
- **원인:** 프로세스가 가짜(fake) 프로바이더로 실행 중입니다. 가짜로 설정된 경우(모든 기본 `.env`가 설정하는 `LOOM_LLM_PROVIDER=fake`, 또는 체크아웃의 `.env`나 업그레이드된 설치에 있는 `fake-ngram-*` 임베딩)이거나, 실제 프로바이더로 설정되었지만 키가 없는 경우입니다.
- **확인:** `curl -s localhost:7777/v1/health | jq '.problems[].code'`가 `llm_key_missing`, `embedder_key_missing`, `fake_llm` 또는 `fake_embedder`를 표시합니다(응답 본문에는 `fake_llm: true`, `fake_embedding: true`).
- **해결:** 문제의 `fix`에 명시된 키(`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`)와 `.env`의 프로바이더 블록을 설정한 다음 컨테이너를 다시 생성하세요(`docker compose … up -d --force-recreate`). 키만 설정해서는 `.env`가 `fake`로 고정한 프로바이더가 바뀌지 않습니다. 로컬 스택에서는 테이블을 만들 때 벡터 차원이 고정되므로, 가짜 임베더에서 전환하려면 `make down-all && make dev`가 필요합니다. 가짜 임베더로 임베딩된 행에는 `loom reembed`가 필요합니다. 다음부터는 `LOOM_REQUIRE_REAL_PROVIDERS=true`를 설정하여 이 상황이 `status: degraded`로 표시되게 하세요.

<a id="search-quality-dropped-after-an-embedder-change"></a>

### 임베더를 변경한 후 검색 품질이 떨어짐

- **원인:** 새 임베더는 모든 유사도 점수를 바꿉니다. 이전 모델로 임베딩된 행은 다른 벡터 공간에 있습니다.
- **확인:** `loom reembed --dry-run`이 모델이 다른 행의 수를 세고, `loom.search.top_similarity`가 변화를 보여 줍니다.
- **해결:** `loom reembed`([유지 관리](https://github.com/ClickHouse/loom/blob/main/docs/operations/maintenance.md#re-embed-and-re-extract))를 실행한 다음, `loom.search.top_similarity`에 설정한 알림의 기준값을 다시 잡으세요. `LOOM_ADAPTIVE_THRESHOLDS`가 켜져 있으면(기본값) 부팅 시 프로브 후 유사도 게이트가 새 임베더에 맞춰집니다. `loom recalibrate`는 이를 즉시 다시 실행합니다.

<a id="a-write-is-missing-from-search-or-was-not-stored"></a>

### 기록한 내용이 검색에 없거나 저장되지 않음

- **확인:** 수집 파이프라인이 버리거나 잘라낸 항목은 `loom drops --namespace NS`로, 그리고 쓰기 요청 자체의 응답(`status`, 충돌 판정, `pipeline_errors`)으로 확인하세요. `loom.embed.pending`은 아직 벡터를 기다리는 행의 수를 셉니다.
- **해결:** 폐기 사유에 따라 조치하세요([유지 관리](https://github.com/ClickHouse/loom/blob/main/docs/operations/maintenance.md#memory-ingest-drops)). 충돌한 쓰기는 대시보드의 검토 큐에서 해결하세요([충돌과 거부](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/conflicts-and-refusal.md)).

<a id="auth-and-tokens"></a>

## 인증과 토큰

<a id="requests-fail-with-401-auth_invalid-or-503-auth_unavailable"></a>

### 요청이 401 AUTH_INVALID 또는 503 AUTH_UNAVAILABLE로 실패함

- **증상:** 401 `AUTH_INVALID`, 또는 `Retry-After: 5`와 함께 503 `AUTH_UNAVAILABLE`.
- **확인:** 오류 상세의 `message`(만료됨, 잘못된 서명, 폐기됨, bearer 누락)를 확인합니다. 클라이언트 머신에서는 `loom doctor --client`로 훅과 SDK가 보낼 토큰을 테스트할 수 있습니다.
- **해결:**
  - 서명이 잘못되었다면 토큰이 다른 `LOOM_JWT_SECRET`으로 발급된 것입니다(시크릿이 교체되었거나 다른 배포에 대해 발급됨). `docker exec loom-server loom mint-token …`로 다시 발급하세요.
  - 만료되었거나 폐기된 토큰은 새로 발급해야 합니다. `loom revoke-token --list`로 폐기 목록을 볼 수 있습니다.
  - 부팅 직후 `AUTH_UNAVAILABLE`이 발생하면 폐기 목록이 아직 로드되지 않았다는 뜻입니다(`revocation_not_loaded`). 서버는 목록이 로드될 때까지 토큰을 거부하므로 몇 초 후 다시 시도하고, 계속되면 ClickHouse를 확인하세요.
  - 환경에 오래된 `LOOM_API_KEY`가 있으면 SDK와 플러그인에서 `~/.loom/token`보다 우선합니다.
  - 인증을 처음 켠 뒤에는 모든 호출에 토큰이 필요합니다. 위 방법으로 토큰을 발급하세요.

<a id="an-id-you-know-exists-reads-as-missing"></a>

### 존재하는 것을 아는 id가 없는 것으로 조회됨

- **원인:** `memory.get_by_id`처럼 조직 전체 범위의 id만으로 지정하는 op는, id의 존재를 확인해 주는 403 대신 행이 없는 경우처럼 응답합니다. 보통은 토큰이 해당 행의 네임스페이스에 대한 op의 역할을 갖고 있지 않기 때문입니다. 읽기 전에 id를 해석하는 op도, `namespace.restore` 이후처럼 호출자가 해당 id를 가진 둘 이상의 네임스페이스에 접근할 수 있으면 같은 방식으로 응답합니다.
  [행 범위 작업](https://github.com/ClickHouse/loom/blob/main/docs/architecture/security.md#row-scoped-operations)에 규칙과 예외가 있습니다.
- **해결:** 행의 네임스페이스에 대한 op의 역할을 가진 토큰을 사용하세요
  ([토큰과 권한 부여](https://github.com/ClickHouse/loom/blob/main/docs/guides/tokens-and-grants.md)). id가 여러 네임스페이스에 있는 경우에는 해당 네임스페이스의 키로 행을 지정하세요(`memory.get`, 쓰기라면 `memory.update`).

<a id="schema-and-upgrades"></a>

## 스키마와 업그레이드

<a id="migrations-pending-or-checksum-drift"></a>

### 대기 중인 마이그레이션 또는 체크섬 불일치

- **증상:** `/v1/health`가 `pending_migrations`를 보고하거나, `loom migrate --dry-run --strict`가 1로 종료되거나(대기 중인 파일만 있을 때가 아니라 원장에만 있는 행이나 체크섬 불일치가 있을 때 종료됩니다), `redeploy.sh`의 사전 점검이 중단됩니다.
- **확인:** `loom migrate --dry-run --strict`는 대기 중인 파일을 출력한 다음, `ledger-only` 행(저장소에는 더 이상 없는 원장 파일 이름)과 `checksum drift`(적용된 파일의 내용이 기록된 SHA-256과 더 이상 일치하지 않는 경우)를 출력합니다.
- **해결:**
  - 대기 중인 파일: `loom migrate`를 실행하거나 `LOOM_BOOT_MIGRATE=true`로 다시 시작합니다.
  - 체크섬 불일치 또는 원장에만 있는 행은 어떤 서버가 마이그레이션 파일을 적용한 뒤에 그 파일이 변경되었다는 뜻입니다(이름이 바뀌고 변경된 경우도 포함). 대개 병합되지 않은 브랜치에서 실행한 서버가 원인입니다. 적용된 DDL을 저장소의 파일과 비교합니다. 결과가 동일하면 현재 파일을 기록합니다(가장 최근 행이 우선합니다):

    ```sql
    INSERT INTO loom._migrations (filename, checksum, applied_by)
    VALUES ('<file>', '<sha256 of the file>', 'operator');
    ```

    DDL이 새 이름으로 존재하는 원장 전용 행은 `DELETE FROM loom._migrations WHERE filename = '<old name>'`으로 제거할 수 있습니다.

  - 결과가 다르면 스키마를 맞추는 새 마이그레이션을 작성합니다. 이미 적용된 파일은 절대 수정하지 마십시오 ([마이그레이션](https://github.com/ClickHouse/loom/blob/main/docs/development/migrations.md)).
  - "이전 레이아웃의 행이 있는" 테이블 때문에 부팅이 실패하면 먼저 해당 테이블에 대해 `scripts/ops/table_rebuild.py`를 실행해야 합니다
    ([업그레이드](https://github.com/ClickHouse/loom/blob/main/docs/operations/upgrades.md#how-migrations-run)).

<a id="requests-fail-with-503-schema_pending"></a>

### 요청이 503 SCHEMA_PENDING으로 실패함

- **원인:** 구문이 이 빌드에 포함된 마이그레이션이 생성할 테이블을 지정하고 있는데, 서비스가 해당 마이그레이션을 적용하지 않은 경우입니다(`LOOM_BOOT_MIGRATE=false`로 부팅된 프로세스이거나 마이그레이션이 실패한 경우).
- **해결:** 위의 대기 중인 마이그레이션과 같습니다.

<a id="cells"></a>

## 셀

<a id="a-process-with-loom_cell-unset-is-gated"></a>

### LOOM_CELL이 설정되지 않은 프로세스는 차단된다

- **증상:** 상태 확인 프로브를 제외한 모든 요청이 503으로 응답하고, `/v1/health/ready`가 `cell` 검사에서 실패하며, 워커가 실행되지 않습니다. 사유로는 배포가 "is a cell in enforced mode and LOOM_CELL is unset", "holds a partial cell state (... present)" 또는 "cell state is not read yet"라고 표시됩니다. 조직을 지정하는 CLI 명령은 "this deployment is a cell in enforced mode (cell ...) and LOOM_CELL is unset", "holds a partial cell state and LOOM_CELL is unset" 또는 "cell state could not be read (...)"라는 메시지와 함께 거부됩니다. 첫 번째 경우에는 CLI에 `LOOM_CELL`을 설정하고, 마지막 경우에는 메시지가 가리키는 사유에 따라 조치합니다. 사유는 누락된 권한([업그레이드 15단계](https://github.com/ClickHouse/loom/blob/main/docs/operations/upgrades.md#upgrade-procedure))이거나 응답하지 않는 Postgres입니다.
- **원인:** Postgres가 권한 저장소(authority)인 구성에서는 모든 서버 프로세스가 배포의 셀 상태(`loom.cell_first`, `loom.tenancy_mode`, `loom.cell_self`, `loom.tenancy`)를 읽습니다. 여기서 권한 저장소는 배포가 선택한, 기준이 되는 저장소 백엔드(source of truth)를 뜻합니다. `LOOM_CELL`이 설정되지 않은 프로세스는 이 테이블에 행이 하나도 없거나, 첫 번째 셀의 전체 상태가 `recording`으로 들어 있는 동안에만 요청을 처리합니다
  ([셀](https://github.com/ClickHouse/loom/blob/main/docs/architecture/cells.md#the-tenancy-check-and-placement)).
- **확인:** 라우터의 디렉터리에 이 배포의 행이 있는지 확인합니다. 라우터가 없는 배포에서는 이 단계를 건너뜁니다. `LOOM_PG_DSN`에 연결한 psql 세션에서 시작합니다:

  ```sql
  SELECT deployment_id AS id FROM loom.authority \gset
  \connect '<router DSN>'
  SELECT cell, is_first, state, placed FROM loom_router.cells WHERE deployment_id = :'id';
  ```

- **해결**, "not read yet"인 경우: 프로세스가 Postgres 마이그레이션이 보류된 상태(`LOOM_BOOT_MIGRATE` 꺼짐)로 부팅했거나 그 이후 Postgres에 도달하지 못한 것입니다. 마이그레이션을 적용하거나(`/v1/health`에 목록이 표시됩니다) 연결을 복구하면, 1초 이내의 다음 읽기에서 아래 설명대로 프로세스가 열리거나 차단됩니다.
- **해결**, 그 외의 경우에는 확인 결과에 따라 조치합니다:
  - **배포가 셀인 경우**(디렉터리에 해당 행이 있고, 아래의 연결되지 않은 후속 셀도 아니고 철회 중인 첫 번째 셀도 아닌 경우): `LOOM_CELL`을 셀 이름으로 설정합니다. 부분 상태라면 `loom cell prepare`를 다시 실행하면 상태가 완성됩니다.
  - **배포가 셀이 되도록 의도된 것이 아니거나**(디렉터리 행이 없거나 라우터가 없는 경우), **연결되지 않은 후속 셀**(행이 `is_first = false`, `state = 'attaching'`, `placed = 0`으로 표시됨)을 단일 배포로 되돌리는 경우: 하나의 트랜잭션으로 상태를 지웁니다. 확인 후 `LOOM_PG_DSN`으로 다시 `\connect`한 psql 세션에서, 스키마 `loom`의 테이블을 소유한 역할(마이그레이션을 수행하는 역할이며, 트리거를 변경하려면 테이블 소유자여야 합니다)로 실행합니다. 그러면 배포는 `open` 상태가 됩니다. 연결되지 않은 후속 셀의 디렉터리 행은 `loom cell remove --cell <cell>`로 제거하기 전까지 유효하게 남으며, 이 배포를 새로 prepare하려면 제거가 필요합니다. 잠금 실패 시 psql이 아직 열려 있으면 `ROLLBACK`한 뒤 이 단계 전체를 다시 실행합니다.

    ```sql
    \set ON_ERROR_STOP on
    BEGIN;
    SET LOCAL lock_timeout = '100ms';
    -- the attach lock: held by a running attach, prepare or update
    SELECT pg_advisory_xact_lock(7813586394088235880);
    SELECT pg_advisory_xact_lock(7813586394288842610);  -- the migration lock
    ALTER TABLE loom.cell_first DISABLE TRIGGER cell_first_immutable;
    DELETE FROM loom.cell_first;
    DELETE FROM loom.tenancy_mode;
    DELETE FROM loom.cell_self;
    DELETE FROM loom.tenancy;
    ALTER TABLE loom.cell_first ENABLE ALWAYS TRIGGER cell_first_immutable;
    ALTER TABLE loom.cell_first ENABLE ALWAYS TRIGGER cell_first_no_truncate;
    COMMIT;
    ```

    그런 다음 같은 세션에서 같은 역할로, 상태 트랜잭션 이후에 조직 트리거와 `loom.tenancy` 가드를 삭제합니다(마이그레이션을 수행하는 부팅의 `ensure`가 이들을 다시 만들지 않도록 하기 위함입니다). 잠금 실패 시 psql이 아직 열려 있으면 `ROLLBACK`한 뒤 이 단계를 다시 실행합니다:

    ```sql
    \set ON_ERROR_STOP on
    BEGIN;
    SET LOCAL lock_timeout = '100ms';
    SELECT pg_advisory_xact_lock(7813586394288842610);  -- the migration lock
    SELECT format('DROP TRIGGER %I ON loom.%I', t.tgname, c.relname)
    FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'loom'
      AND t.tgname IN ('tenancy_insert', 'tenancy_update', 'tenancy_record') \gexec
    COMMIT;
    ```

    상태 트랜잭션의 attach 잠금에서 잠금 실패가 발생했다면, 이 Postgres에 대해 attach, prepare 또는 update가 실행 중이라는 뜻이며 몇 시간이 걸릴 수 있습니다. 그동안 이 단계를 다시 실행해도 같은 방식으로 실패합니다. 다음 쿼리는 잠금을 잡고 있는 세션을 알려 줍니다:

    ```sql
    SELECT a.pid, a.usename, a.application_name, a.client_addr, a.backend_start
    FROM pg_locks l JOIN pg_stat_activity a ON a.pid = l.pid
    WHERE l.locktype = 'advisory' AND l.granted
      AND l.classid = (7813586394088235880 >> 32)::oid
      AND l.objid = (7813586394088235880 & 4294967295)::oid AND l.objsubid = 1
      AND l.database = (SELECT oid FROM pg_database WHERE datname = current_database());
    ```

  - **철회 중인 첫 번째 셀**(행이 `is_first = true`, `state = 'attaching'`, `placed = 0`으로 표시되며 `mode`는 무엇이든 상관없습니다. 3단계 잠금에서 거부된 attach는 `enforced`로 남기 때문입니다. 또한 해당 Postgres에서 `SELECT mode FROM loom.tenancy_mode`가 `recording`을 반환합니다): 다음 순서로 철회합니다. 모든 프로세스에서 `LOOM_CELL`을 해제하면 각 프로세스는 `recording`일 때처럼 요청을 처리합니다. 위의 상태 트랜잭션과 삭제 단계를 실행합니다. 그런 다음 라우터의 운영자 자격 증명과 셀의 `LOOM_PG_DSN`으로 `loom cell remove --cell <cell>`을 실행합니다. 이 명령은 해당 Postgres가 `open`으로 읽힐 때까지 거부되며, 행의 배포 id를 기록합니다. 행을 먼저 제거하면 모든 새 조직에 503이 응답되고, 상태를 먼저 지우면 `LOOM_CELL`이 설정된 모든 프로세스가 차단됩니다. 라우터는 셀의 Postgres를 읽을 수 없으므로, 라우터에서 행을 직접 `DELETE`하면 이 검사를 건너뛰게 되고, 위 단계를 실행하기 전까지는 아직 prepare된 셀이 새 조직에 503을 응답하며 다시 등록할 수도 없게 됩니다. 비워진 저장소에는 셀 상태가 남지 않으며, 배포는 `loom cell prepare`부터 다시 prepare합니다. 행이 `placed > 0`인 첫 번째 셀은 되돌리지 않고 앞으로 진행합니다. 이 경우 attach를 다시 실행합니다(제거 명령은 이를 거부합니다).

  - **다른 배포에 복원된 셀의 Postgres 사본**은 원본 셀의 배포 id를 그대로 가지므로, 확인 쿼리가 활성 상태인 원본 셀의 행을 반환합니다. 이 상태를 지우지 말고, 원본과 나란히 서비스하지도 마십시오.

<a id="a-cli-with-loom_cell-set-refuses-a-command-that-writes-for-no-org"></a>

### LOOM_CELL이 설정된 CLI가 조직 없이 쓰는 명령을 거부한다

- **증상:** 조직 없이 쓰거나 폐기하는 명령(`loom secrets reseal`, `loom settings set`, `loom ch-setup`, `loom revoke-token` 등)이 "refusing: this Postgres's first row names cell ..., not LOOM_CELL=...", "refusing: this cell's state could not be read (...)" 또는 "refusing: this ClickHouse could not be read for the cell check (...)"라는 메시지와 함께 종료됩니다.
- **원인:** `LOOM_CELL`이 설정되어 있으면 이런 명령은 무언가를 변경하기 전에 `LOOM_PG_DSN`에서 셀의 `first` 행을 읽고, 그 Postgres를 기준으로 ClickHouse를 검사합니다([셀](https://github.com/ClickHouse/loom/blob/main/docs/architecture/cells.md#the-tenancy-check-and-placement)). 첫 번째 행이 다른 셀을 가리킨다면 CLI가 그 셀의 저장소를 향하고 있다는 뜻입니다. 접근 권한을 되돌려 줄 수 없는 폐기는 대신 Postgres 장애 중에도 실행되며, 셀을 검사하지 않았다고 출력합니다.
- **해결:** 거부 메시지가 가리키는 상태는 다른 셀의 것이므로 절대 지우지 마십시오. `LOOM_PG_DSN`과 ClickHouse 설정을 이 셀 자체의 저장소로 지정하거나, 의도한 셀이 첫 번째 행이 가리키는 셀이라면 `LOOM_CELL`을 그 셀로 설정합니다. 상태를 읽을 수 없는 경우에는 메시지가 가리키는 사유에 따라 조치합니다. 사유는 누락된 권한([업그레이드 15단계](https://github.com/ClickHouse/loom/blob/main/docs/operations/upgrades.md#upgrade-procedure))이거나 응답하지 않는 Postgres 또는 ClickHouse입니다.

<a id="a-boot-logs-cell-triggers-not-applied"></a>

### 부팅 시 "cell triggers: not applied"가 로그에 남는다

- **증상:** `open`이 아닌 상태의 셀이 부팅할 때 `cell triggers: not applied on loom.<table> (...)`를 로그에 남기고, `loom doctor`가 해당 테이블에서 누락되었거나 비활성화된 트리거를 알려 줍니다(`loom.cell_first`의 트리거는 이런 모든 상태에서, 조직 테이블의 트리거나 `loom.tenancy` 가드는 `enforced`에서).
- **원인:** 부팅은 셀의 조직 트리거와 가드를 만들고 `loom.cell_first`의 트리거를 다시 always로 설정합니다. 이때 한 번에 100 ms의 잠금 대기를 1초 간격으로 세 번 시도하며, 두 가지 경우에는 해당 테이블을 건너뛰고 서비스를 계속합니다. 첫 번째는 테이블이 여전히 사용 중인 경우로, 충돌하는 잠금을 잡고 있거나 대기열에 있던 세션이 로그에 표시됩니다. 긴 트랜잭션, 대량 쓰기, 또는 autovacuum이 원인일 수 있는데, 짧은 시도로는 autovacuum이 양보하지 않으며 끝날 때까지 그대로 둡니다(취소할 만큼 오래 기다리면 그 테이블의 모든 쓰기가 부팅 뒤에 대기하게 됩니다). 두 번째는 마이그레이션 역할이 테이블을 소유하지 않아 `this role cannot alter it`가 표시되는 경우로, `loom doctor`는 이를 `owner` 결함으로 알려 줍니다. 이후의 부팅이 해당 테이블을 완성하기 전까지는 테넌시 검사를 하지 않는 쓰기 주체가 그 테이블에서 거부되지 않습니다. Loom 자체의 쓰기는 영향을 받지 않습니다.
- **해결:** 사용 중인 테이블이라면, 로그에 표시된 트랜잭션이 더 이상 필요 없는 것이면 종료하거나 autovacuum이 끝나기를 기다립니다(`pg_stat_progress_vacuum`). 역할이 변경할 수 없는 테이블이라면, 스키마 `loom`의 테이블을 소유한 역할로 부팅하거나 `ALTER TABLE loom.<table> OWNER TO <role>`로 테이블을 그 역할에 넘깁니다. 그런 다음 `LOOM_BOOT_MIGRATE`를 켠 상태로 파드 하나를 재시작하고, `loom doctor`가 누락되었거나 비활성화된 트리거를 더 이상 알려 주지 않을 때까지 반복합니다.

<a id="the-cell_marker-readiness-check-names-the-tenancy-marker"></a>

### cell_marker 준비 상태 검사가 테넌시 마커를 언급한다

- **증상:** `LOOM_CELL`이 설정된 프로세스의 `/v1/health/ready`가 "pooled Postgres sessions do not keep the tenancy marker"라는 사유와 함께 `cell_marker` 검사를 보고합니다. `enforced` 또는 `partial` 셀에서는 실패하고, 아직 `recording`인 첫 번째 셀에서는 같은 사유로 통과하며, 프로세스의 셀 하트비트는 `marked`를 `false`로 보고합니다.
- **원인:** 프로세스와 Postgres 사이에 커넥션 풀러가 있는 경우입니다. 풀러는 자체 시작 매개변수로 서버 연결을 열기 때문에 `loom.tenancy_checked`가 없습니다. 클라이언트 매개변수를 `SET`으로 전달하는 풀러도 마커를 읽기 전에 갱신 과정에서 `RESET ALL`을 실행하므로 마커가 없는 것으로 읽힙니다. 셀에 조직 트리거가 생기면, 이런 세션이 셀이 보유하지 않은 조직에 대해 하는 모든 쓰기는 Loom 자체의 쓰기를 포함해 트리거에 의해 거부됩니다.
- **해결:** 셀이 요구하는 대로([Postgres](https://github.com/ClickHouse/loom/blob/main/docs/operations/postgres.md)) 어떤 모드의 풀러도 거치지 않고 `LOOM_PG_DSN`이 Postgres를 직접 가리키게 한 다음 프로세스를 재시작합니다. `LOOM_PG_DSN`을 변경하려면 재시작이 필요합니다. 이 검사는 셀 갱신(`LOOM_TENANCY_MODE_CACHE_S`)마다 다시 읽히며, 재시작 후 첫 갱신에서 해소됩니다.

<a id="a-boot-fails-with-function-loomtenancy_refuse_-does-not-exist"></a>

### 부팅이 "function loom.tenancy_refuse_… does not exist"로 실패한다

- **증상:** `enforced` 셀의 마이그레이션 부팅이 셀 트리거를 만들다가 `function loom.tenancy_refuse_unheld() does not exist`(또는 `…_unmarked()`)로 실패하고, `loom doctor`는 어떤 상태에서든 해당 함수가 없다고 알려 줍니다.
- **원인:** 마이그레이션이 실행된 뒤 함수가 수동으로 삭제되었습니다(조직 트리거는 011, `loom.tenancy` 가드는 012. `DROP FUNCTION … CASCADE`는 해당 트리거도 함께 삭제합니다). 마이그레이션 원장에는 이미 해당 파일이 기록되어 있어 어떤 부팅도 함수를 다시 만들지 않으며, 함수가 복구되기 전까지는 검사를 거치지 않는 쓰기 주체가 거부되지 않습니다. 새 릴리스의 `loom doctor`가 그 릴리스의 첫 마이그레이션 부팅 전에 함수가 없다고 알려 주는 경우에는 조치가 필요 없습니다. 해당 부팅이 파일을 실행하기 때문입니다.
- **해결:** 스키마 `loom`의 테이블을 소유한 역할로 `psql`을 사용해 마이그레이션 파일(`schemas/pg_migrations/011_tenancy_triggers.sql` 또는 `012_tenancy_guard.sql`)을 실행합니다. 그런 다음 `enforced` 셀에서는 `LOOM_BOOT_MIGRATE`를 켠 상태로 파드 하나를 재시작합니다. 그 부팅이 트리거를 만듭니다(다른 셀에서는 attach가 나중에 만듭니다).

<a id="writes-are-slow-or-a-script-is-refused-on-a-deployment-that-is-not-a-cell"></a>

### 셀이 아닌 배포에서 쓰기가 느리거나 스크립트가 거부된다

- **증상:** 셀 상태가 `open`인 배포(모든 프로세스가 요청을 처리함)에서 Loom 자체의 쓰기와 스크립트 및 대량 로더의 쓰기가 행당 더 오래 걸리거나, `loom.cell_first`에 대한 `SELECT` 권한이 없는 역할이 "permission denied for table cell_first"를 받거나, 스크립트의 `loom.tenancy` 삽입이 SQLSTATE `LT001`로 실패합니다.
- **원인:** 조직 트리거가 테이블에 아직 남아 있습니다. prepare된 셀을 스키마만 복원했거나, 정리 과정에서 트리거를 삭제하는 단계를 건너뛴 경우입니다. `open`에서는 트리거가 모든 행을 통과시키지만, 마커가 없는 쓰기는 매번 상태를 읽습니다. `loom.tenancy` 가드인 `tenancy_record`는 스크립트의 테넌시 행을 SQLSTATE `LT001`로 거부하며, 그 행 자체가 상태를 `partial`로 만듭니다.
- **확인:**

  ```sql
  SELECT c.relname, t.tgname FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid
  JOIN pg_namespace n ON n.oid = c.relnamespace
  WHERE n.nspname = 'loom'
    AND t.tgname IN ('tenancy_insert', 'tenancy_update', 'tenancy_record');
  ```

- **해결:** 스키마 `loom`의 테이블을 소유한 역할로 [LOOM_CELL이 설정되지 않은 프로세스는 차단된다](#a-process-with-loom_cell-unset-is-gated)의 삭제 단계를 실행합니다.

<a id="workers"></a>

## 워커

<a id="a-worker-fails-every-pass"></a>

### 워커가 매 패스마다 실패하는 경우

- **증상:** `loom.worker.consecutive_failures`가 3 이상으로 유지됩니다. 워커 로그에 `<name>: pass failed` 또는 `pass exceeded its deadline`이 반복된 뒤 `DEGRADED`가 나타납니다.
- **확인:** 워커의 마지막 오류는 `GET /v1/admin/workers.status`로, 트레이스백은 `docker logs loom-workers 2>&1 | grep -E '<name>: (pass|DEGRADED)'`로 확인합니다.
- **해결:** 예외 내용을 따라 조치합니다. 데드라인 취소가 반복된다면 간격에 비해 패스가 너무 크다는 뜻입니다(백로그가 크거나 프로바이더가 느린 경우). 프로바이더 오류는 대개 키가 없거나 할당량이 부족한 경우입니다. fake 프로바이더를 사용하는 LLM 워커라면 해당 워커를 비활성화하거나 실제 프로바이더를 설정합니다. 원인을 조사하는 동안 워커 하나만 멈추려면 해당 플래그를 false로 설정하고 workers 컨테이너를 다시 생성합니다. 다른 워커는 계속 실행됩니다.

<a id="no-worker-runs-at-all"></a>

### 어떤 워커도 실행되지 않는 경우

- **확인:** `/v1/health?include=workers` → 각 프로세스의 `workers_leader`를 확인합니다. 한 프로세스가 리스를 보유하고 있어야 합니다. `workers_lease_absent`가 warn이면 유효한 리스를 보유한 프로세스가 없다는 뜻입니다. `workers_lease_unreachable` 문제는 프로세스가 ClickHouse에 요청하지 못한 이유를 알려 주고, `workers_lease_given_up`은 해당 프로세스의 workers 자식 프로세스가 계속 시작에 실패했음을 뜻합니다.
- **해결:** 모든 레플리카를 `LOOM_BACKGROUND_WORKERS=auto`와 `LOOM_DEPLOYMENT=1`로 실행합니다. `off`인 프로세스는 워커를 실행하지 않으며, 배포 밖의 프로세스는 `auto`에서 워커를 실행하지 않습니다. `on`은 어떤 배포도 사용하지 않는 ClickHouse에서만 설정합니다. 이 경우 자체 사본을 실행하며 리스를 확보하지 않습니다. 업그레이드 중에는 이전 빌드가 리스를 유효한 새 빌드에 양보합니다.

<a id="host"></a>

## 호스트

<a id="the-host-or-clickhouse-runs-out-of-disk"></a>

### 호스트 또는 ClickHouse의 디스크가 가득 찬 경우

- **증상:** Loom 호스트의 디스크가 가득 차서(컨테이너가 쓰기에 실패하거나 이미지 빌드가 실패함)
  또는 ClickHouse가 남은 공간 부족을 보고합니다.
- **확인:** 호스트에서 `docker system df`와 `var/code_repos`의 크기를 확인합니다. ClickHouse에서는
  [유지 관리](https://github.com/ClickHouse/loom/blob/main/docs/operations/maintenance.md#disk-pressure-hygiene)에 있는 테이블별 크기 쿼리와 `system.disks` 쿼리를 사용합니다. 디스크가 가득 찬 경우에는
  진단 출력을 `/dev/shm`에 기록하십시오.
- **해결:** Docker의 JSON 로그 크기를 제한하고, 빌드 캐시와 사용되지 않는 이미지(dangling image)를 제거합니다
  (`docker builder prune`, `docker image prune`). 중지된 컨테이너가 롤백용이라면 해당 컨테이너는 정리하지 마십시오.
  ClickHouse에서는 대량 삭제 후 머지가 버전을 병합하도록 두거나, `loom code compact --apply`로 코드 벡터 테이블을 압축하거나,
  테이블별 보존 정책을 사용하도록 설정합니다.

<a id="local-stack"></a>

## 로컬 스택

- **대시보드가 503을 반환합니다.** 서버를 소스에서(`make server`) 프런트엔드를 빌드하지 않은 채 실행한 경우입니다. `make frontend-build`를 실행하세요. `/v1/admin/*`는 어느 쪽이든 응답합니다.
- **스택이 `.env`를 무시합니다.** `--env-file .env` 없이 `docker compose` 명령을 실행하면 compose 파일의 기본값(인증 켜짐, 키가 없는 OpenAI 프로바이더)으로 부팅됩니다. `make dev`를 사용하거나 해당 플래그를 전달하세요.
- **소스에서 시작한 서버가 다른 코드를 제공합니다.** [로컬 개발](https://github.com/ClickHouse/loom/blob/main/docs/development/local-development.md#running-the-server-from-source)을 참고하세요.

<a id="clients-plugins-and-mcp"></a>

## 클라이언트, 플러그인, MCP

- **코딩 에이전트 플러그인**에는 각각 고유한 증상 표가 있습니다(Node를 찾지 못하는 hook, 프라이머 없음, 캡처가 도착하지 않음, 도구가 두 번 나열됨):
  [Claude Code](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/claude-code.md) 및
  [Codex](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/codex.md). 플러그인의 `loom-status` 스킬은 Loom 체크아웃 없이도
  클라이언트 점검을 실행합니다.
- **원장에 해당 요청의 행이 없습니다.** 요청이 도착하지 않은 것입니다.
  `loom doctor --client`는 SDK와 hook이 확인하는 서버 URL과 토큰, 그리고
  각각의 출처를 알려 줍니다. 환경에 남아 있는 오래된 `LOOM_API_KEY` 또는 `LOOM_BASE_URL`이
  `~/.loom/`보다 우선합니다. 토큰으로 `GET /v1/whoami`를 호출하면 서버가 검증한 주체, 조직,
  네임스페이스, 역할을 볼 수 있습니다. 토큰에 명시되지 않은 네임스페이스에서의 403, 또는
  본문에 네임스페이스가 없는 범위 지정 토큰이 반환하는 `IDENTITY_INCOMPLETE`는
  버그가 아니라 권한 부여 문제입니다([토큰과 권한 부여](https://github.com/ClickHouse/loom/blob/main/docs/guides/tokens-and-grants.md#namespace-access)).
  임시 디렉터리에 남아 있는 `loom-flush-*.json` 파일은 서버에 도달하지 못한 플러그인 캡처입니다
  ([헤드리스 봇](https://github.com/ClickHouse/loom/blob/main/docs/guides/headless-bots.md#5-when-the-bot-is-quiet)).
- **MCP 도구가 목록에 없습니다.** 목록은 호출자가 아니라 MCP URL의
  `?toolset=`에 따라 달라집니다. `all`에서는 core나
  atomic 로스터 어디에도 없는 실험적 op가 목록에서 제외되지만, 이름으로 또는
  `loom_op`를 통해 여전히 호출할 수 있습니다. `code.*` 도구는 기본 toolset에서
  `LOOM_MCP_CODE_TOOLS=true`일 때만 나타납니다([MCP toolset](https://github.com/ClickHouse/loom/blob/main/docs/reference/mcp.md#toolsets)).
- **MCP 호출이 `isError: true`를 반환합니다.** 401이 아닌 거부(`LOCKED`,
  `PINNED_CONFLICT`, `BLACKLISTED`, `RATE_LIMITED`, `UNKNOWN_ARGUMENT`, …)는
  `isError`가 설정된 도구 결과이며 HTTP 오류 상세가 `structuredContent`에 담깁니다. 401 또는 5xx는
  JSON-RPC 오류입니다. `code`로 판별하세요.
- **보낸 필드가 무시되었습니다.** 응답의 `ignored_args`에 해당 필드가 나타나며, 알려진 필드와 비슷하면
  `did_you_mean`도 함께 제공됩니다. 쓰기 작업은 철자가 비슷하게 틀린 경우 422 `UNKNOWN_ARGUMENT`로
  거부합니다
  ([알 수 없는 필드와 제거된 필드](https://github.com/ClickHouse/loom/blob/main/docs/reference/http-api.md#unknown-and-removed-fields)).
- **429 `RATE_LIMITED`.** 버킷은
  [모니터링](https://github.com/ClickHouse/loom/blob/main/docs/operations/monitoring.md#rate-limits-and-traffic-classes)을 참고하세요. 재시도 시점은 `Retry-After`에 나와 있습니다.

<a id="filing-a-bug"></a>

## 버그 제보하기

`loom report`는 `loom doctor` 진단 결과(존재 여부를 나타내는 불리언 값만 포함하며, 비밀 값은 절대 포함하지 않음)를 버그 양식의 Environment 필드에 출력합니다. `--open`을 사용하면 버전 정보가 채워진 상태로 버그 양식이 열립니다([이슈 제출하기](https://github.com/ClickHouse/loom/blob/main/CONTRIBUTING.md#filing-an-issue)).
