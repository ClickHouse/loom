<!-- i18n: source=docs/architecture/failure-and-recovery.md blob=40f8e4b57e275dada1e2090b1f563fcdaafd8095 model=claude-sonnet-5-5 -->
> 이 페이지는 [영어 원문](https://github.com/ClickHouse/loom/blob/main/docs/architecture/failure-and-recovery.md)을 기계 번역한 것이며, 영어 원문이 기준입니다.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/architecture/failure-and-recovery.md), which is authoritative.

<a id="failure-and-recovery"></a>

# 장애와 복구

기본 쓰기와 모든 접근 결정은 실패 시 닫힌 상태(fail closed)로 동작합니다. 파생 데이터와 텔레메트리는 실패 시 열린 상태(fail open)로 동작하며 이후 복구됩니다. 성공을 반환한 요청은 약속한 내용을 영구적으로 저장한 것이며, 그 옆에 투영되는 데이터는 워커가 따라잡아 반영합니다. 마지막 섹션인 [용량](#capacity)에서는 무엇이 어떻게 확장되는지, 그리고 한계가 어디에 있는지를 다룹니다. 증상별 런북은 [문제 해결](https://github.com/ClickHouse/loom/blob/main/docs/operations/troubleshooting.md)에 있습니다.

<a id="errors-retries-and-idempotency"></a>

## 오류, 재시도, 멱등성

| 경계                 | 동작                                                                                                                                                                                                                                                                                                                                                                                                |
| -------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| ClickHouse 클라이언트 | 멱등한 구문에서 일시적인 네트워크 장애가 발생하면 새 연결에서 한 번 재시도합니다. 레플리카에 묶인 서버 오류(코드 10, 242, 999)는 최대 두 개의 새 연결에서 재시도합니다. 그 밖의 오류는 그대로 발생시킵니다.                                                                                                                                                                                      |
| 구문 데드라인        | 클래스별 데드라인(`LOOM_CH_TIMEOUT_READ_S`, `_WRITE_S`, `_DDL_S`)을 넘기면 요청은 재시도 없이 504 `BACKEND_TIMEOUT`으로 실패합니다.                                                                                                                                                                                                                                                                   |
| 풀 진입              | `LOOM_CH_POOL_WAIT_MS` 안에 빈 슬롯이 없으면 `Retry-After: 1`과 함께 503 `OVERLOADED`를 반환합니다.                                                                                                                                                                                                                                                                                                 |
| 삽입                 | 재시도된 INSERT는 동일한 `insert_deduplication_token`을 담고 있으므로, 이미 커밋된 배치는 두 번 기록되지 않습니다. 토큰이 없으면 삽입을 재시도하지 않습니다. 배치 삽입은 같은 테이블에 대한 동시 삽입과 ClickHouse의 flush를 공유하며, flush가 전체적으로 실패하면 함께 묶인 삽입이 모두 실패하고, 각 재시도는 자신의 토큰으로 한 번씩 기록됩니다([배치 삽입](https://github.com/ClickHouse/loom/blob/main/docs/architecture/clickhouse.md#batched-inserts)). |
| 지원되지 않는 설정   | 양자화 코드 읽기 설정을 거부하는 레플리카를 만나면, 클라이언트는 해당 프로세스에서 그 설정을 버리고 재시도합니다.                                                                                                                                                                                                                                                                                         |
| 포인트 읽기          | 호출자 자신의 쓰기 이후의 읽기는 순차 일관성을 사용합니다. 쓰기 직후에 조회가 실패하면 한 번 다시 읽습니다(`LOOM_CH_READ_YOUR_WRITES`). 20초 이내에 쓰기가 있었던 네임스페이스에 대한 검색은 순차적으로 읽습니다.                                                                                                                                                                                               |
| 스키마 불일치        | 보류 중인 마이그레이션이 생성할 테이블을 지정하는 구문은 503 `SCHEMA_PENDING`으로 거부됩니다(아래 참조).                                                                                                                                                                                                                                                                                           |
| 작업 멱등성          | `idempotent`로 선언된 op는 `Idempotency-Key`(HTTP) 또는 `_idempotency_key`(MCP)를 받습니다. 응답은 조직, 주체, op, 키를 기준으로 `loom.idempotency`에 24시간 동안 보관됩니다. 첫 요청이 실행 중일 때 들어온 중복 요청은 409를 받습니다.                                                                                                                                                                       |
| 기본 쓰기            | fact 행과 그 벡터 행은 커밋이 완료될 때까지 기다리는 삽입입니다. 하나라도 실패하면 요청이 실패합니다.                                                                                                                                                                                                                                                                                                         |
| Postgres 기준 스토리지 | `LOOM_STORAGE_AUTHORITY=postgres`에서는 쓰기가 곧 Postgres 커밋이며, ClickHouse 행은 그로부터 게시되고 `pg_delivery` 워커가 복구합니다. Postgres가 중단되면 쓰기와 포인트 읽기는 닫힌 상태로 실패합니다(503 `POSTGRES_UNAVAILABLE`)([기준 스토리지](https://github.com/ClickHouse/loom/blob/main/docs/architecture/storage-authority.md#failure)).                                                                                                   |
| 프로젝션             | 그래프, 청크, 증거, 대체(supersession) 보조 테이블은 실패해도 열린 상태로 동작하며 워커가 복구합니다.                                                                                                                                                                                                                                                                                                         |
| 텔레메트리           | `agent_events`와 이벤트 스트림은 프로세스 내부에서 큐에 쌓입니다. `agent_events` flush는 배치 삽입이고, 이벤트 스트림 flush는 커밋 삽입입니다. 어느 쪽도 요청을 실패시키지 않으며, 크래시가 나면 flush되지 않은 큐는 유실될 수 있습니다.                                                                                                                                                                                     |
| 워커                 | 실패한 패스는 백오프를 두고 재시도합니다. 작업은 절대값 또는 새 버전으로 기록되므로, 패스를 반복해도 결과가 수렴합니다.                                                                                                                                                                                                                                                                           |
| 삭제                 | `erasure.request`는 체크포인트가 있는 saga입니다. `erasure_recovery`가 이를 재개하며, 모든 테이블에 대한 잔여 데이터 조사 결과가 성공 여부를 결정합니다.                                                                                                                                                                                                                                                                                     |

**재시도 가능한 응답.** `OVERLOADED`, `AUTH_UNAVAILABLE`, `RATE_LIMITED`는
`Retry-After`(및 `retry_after_s`)를 포함합니다. 안내된 시간이 지난 뒤 재시도하십시오.
`SCHEMA_PENDING`은 운영자가 해당 마이그레이션을 적용하면 해소됩니다. `EMBED_TIMEOUT`의 경우,
원시 HTTP 또는 MCP 호출자는 한 번 재시도합니다. SDK 호출자는 이미 재시도했으므로(멱등한 op) 백오프를 적용합니다.
`BACKEND_TIMEOUT`은 `idempotent` op에 대해서만 재시도할 수 있으며, 쓰기에는 멱등성 키가 필요합니다.
전체 목록은 [오류 레퍼런스](https://github.com/ClickHouse/loom/blob/main/docs/reference/errors.md)에 있습니다.

**멱등성 캐시가 최선 노력(best effort) 방식인 이유:** 클러스터 전체에서 일관된 캐시는 요청마다 락이 필요합니다.
이 캐시를 사용하는 op는 중복 실행 시 동일한 내용을 `ReplacingMergeTree`에 기록하므로, 경합이 발생해도 핸들러가 한 번 더 실행될 뿐 효과가 두 번 발생하지는 않습니다. 반복하면 효과가 두 배가 되는 op(`memory.set_from_messages`)는 멱등하지 않은 op로 분류되어 이 캐시를 거치지 않습니다.

<a id="consistency-boundaries"></a>

## 일관성 경계

테이블 간 쓰기는 트랜잭션으로 처리되지 않습니다. 사실(fact)과 그 벡터는 함께 저장되고, 파생 데이터는 나중에 따라잡습니다.

| 파생 데이터                   | 따라잡는 방식                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| ----------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| BM25 통계                     | 매분 갱신되는 refreshable view를 사용합니다. 검색은 라이브 테일을 읽으므로 순위는 정확하게 유지됩니다(`LOOM_MEMORY_SEARCH_BM25_LIVE_TAIL`).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| 사용 카운터, 강화             | `usage_fold` 한 번의 실행(300초마다).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                |
| 엔터티 그래프 프로젝션        | 응답 직후에 반영됩니다. 응답 이후에 시작되는 같은 네임스페이스의 `facts.traverse`는 배포의 모든 프로세스에서 프로젝션이 끝나기를 최대 3초까지 기다립니다. 진행 중인 각 프로젝션은 `loom.graph_projections`에 기록되고(서버에 Keeper가 있으면 Keeper 키에 기록), 프로젝션이 끝나면 서버 시계를 기준으로 해당 네임스페이스의 그래프 쓰기를 기록한 뒤 이를 지웁니다. 프로젝션 도중에 종료된 프로세스의 기록은 최대 15초 동안 남습니다. 각 프로세스는 이를 한 번(3초) 기다리며, 기록이 오래된 것으로 처리되기 전까지는 그 네임스페이스의 탐색(walk)을 항상 새로 읽고 메모에서 제공하거나 메모에 저장하지 않습니다. 한 네임스페이스에서 동시에 진행되는 프로젝션이 32개를 넘으면 초과분은 기다리지 않으며, 각 초과분은 자신의 `memory.set`이 응답하기 전에 그래프 쓰기로 한 번, 끝날 때 다시 한 번 계산됩니다. |

**트랜잭션을 쓰지 않는 이유:** ClickHouse는 테이블 간 트랜잭션을 지원하지 않으며, 이를 흉내 내면 모든 쓰기에 왕복 한 번과 실패 모드가 추가됩니다. 내구성 있는 기본 쌍과 멱등 복구를 사용하면 쓰기를 왕복 한 번으로 유지할 수 있습니다.

<a id="backpressure"></a>

## 백프레셔

어떤 요청도 공유 자원을 무한정 기다리지 않습니다.

- **ClickHouse 풀:** 제한된 시간 동안 대기한 뒤 `OVERLOADED`를 반환합니다 ([ClickHouse 클라이언트](#clickhouse-client)).
- **문서 버킷:** 프로세스당 전송 8개와 스트림 4개이며, 각각 제한된 시간 동안 대기한(`LOOM_CH_POOL_WAIT_MS`) 뒤 `OVERLOADED`를 반환합니다. 목록 조회와 삭제(삭제 요청 처리, 스윕)는 별도의 슬롯 4개 중 하나를 기다립니다.
- **속도 제한:** `Retry-After`와 함께 429 `RATE_LIMITED`를 반환합니다. MCP에서는 전송 계층이 짧은 힌트(`LOOM_MCP_RATELIMIT_RETRY_MAX_WAIT_S`)만큼 한 번 기다린 뒤 다시 확인합니다.
- **쿼리 임베딩:** 시도당 `LOOM_MEMORY_SEARCH_EMBED_BOUND_S`(2.5초)이며, `LOOM_MEMORY_SEARCH_EMBED_BOUND_RETRIES`(1)회 재시도한 뒤 503 `EMBED_TIMEOUT`을 반환합니다.
- **텔레메트리 큐:** `agent_events` emitter(10,000행)와 이벤트 스트림(20,000행)은 가득 차면 행을 버리고 그 수를 집계하며(`dropped_queue_full`, `dropped_flush_failed`), 첫 번째 누락과 1000번째마다 로그를 남깁니다. 전달은 최대 한 번(at most once)입니다.
- **워커:** `usage_fold`는 ClickHouse에 대기 중인 mutation이 너무 많으면 해당 패스를 건너뜁니다. 모든 패스는 배치 크기와 데드라인으로 제한됩니다.
- **예산:** `budget_p99_ms`를 넘긴 op도 응답은 그대로 반환하며 `budget.violated` 행을 남깁니다(op당 `LOOM_BUDGET_VIOLATION_AUDIT_INTERVAL_S`, 즉 60초에 최대 한 번).

<a id="schema_pending"></a>

## `SCHEMA_PENDING`

스키마보다 더 새로운 커밋에서 빌드된 서버(`LOOM_BOOT_MIGRATE=false`인 브랜치 서버, 또는 마이그레이션이 적용되기 전에 부팅된 배포 인스턴스)는 아직 존재하지 않는 테이블을 참조할 수 있습니다. `server/core/ch_schema_guard.py`(`guard_pending_schema`)는 이러한 문장을 503 `SCHEMA_PENDING`으로 거부하며, 이때 테이블과 마이그레이션을 함께 알려 줍니다. 실패한 읽기를 선택 사항으로 취급하는 검색 단계는 해당 읽기 없이 계속 진행하고, ledger 행에는 그 테이블이 기록됩니다. DDL은 거부되지 않습니다.

대기 중인 집합은 마이그레이션 ledger(`server/core/schema/status.py`)에서 가져오며, 프로세스마다 60초 동안 캐시됩니다. ledger가 적용된 항목이 없다고 보고하거나 대기 중인 파일이 25개를 넘으면, guard는 경고를 한 번 남기고 유휴 상태가 됩니다. `/v1/health`는 `schema.pending`을 보고하며, blue/green 전환은 이 값이 빌 때까지 기다립니다.

<a id="worker-supervision"></a>

## 워커 감독

`server/workers/supervisor.py`는 활성화된 각 워커를 독립된 asyncio 태스크로 실행합니다.

- **시차 시작.** 첫 번째 패스는 `crc32(name) % interval`만큼 기다립니다. 다만 출력 테이블이 비어 있는 워커는 즉시 실행되고, 마지막 패스가 interval보다 오래된 워커는 부팅 후 300 s 이내에 시작됩니다.
- **마감 시간.** `min(4 × interval, 900 s)`를 넘긴 패스는 최소 60 s를 하한으로 하여(워커가 자체 값을 선언한 경우는 제외) 취소되며 실패로 집계됩니다.
- **재시도 대기.** 실패한 패스는 `min(60 s, 2^(n-1) s)`만큼 기다립니다. 마감 시간에서 잘린 패스, 또는 interval이 마감 시간보다 긴 워커의 실패한 패스는 interval만큼 기다립니다.
- **성능 저하.** 연속 실패가 세 번 발생하면 `DEGRADED`를 로그에 남깁니다. 연속 실패 횟수는 `loom.worker.consecutive_failures{worker}`로 확인할 수 있으며, 일부 구간(leg)이 실패한 패스는 `loom.worker.partial_failures`에 집계됩니다.
- **상태.** 각 패스는 `loom.worker_status` 행을 기록합니다(`GET /v1/admin/workers.status`). 재개 커서 덕분에 새로운 배포 인스턴스가 긴 스캔을 이어서 진행할 수 있습니다.
- **종료.** `stop_all`은 태스크를 취소하고 최대 10 s까지 기다린 뒤 상태 행을 flush합니다.

워커의 예외는 해당 태스크 안에서만 처리됩니다. 워커는 절대값이나 새 버전을 기록하므로, 두 번 실행된 패스도 같은 결과로 수렴합니다.

<a id="repair-workers"></a>

## 복구 워커

| 워커                      | 복구 대상                                                                                                                                             | 보장 사항                                                                          |
| ------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------- |
| `projection_repair`       | `loom.memory_projection_failures`에 기록된 실패한 프로젝션(증거 링크, 청크, 그래프 보조 테이블, 만료 정리 작업의 검색 미러)을 다시 실행합니다。 | 지원되지 않는 항목은 검사할 수 있도록 그대로 남습니다.                             |
| `entity_extractor`        | 엔터티가 없는 행에서 엔터티를 추출하고, `entities`가 `memory`와 다른 `memory_embeddings` 행을 다시 내보냅니다.                                        | 검색 가능한 모든 행은 결국 자신의 엔터티를 갖게 됩니다.                            |
| `orphan_vector_reconcile` | 메모리가 폐기되었거나 사라진 임베딩 및 엔터티 벡터 행을 유예 기간이 지난 후 폐기 처리합니다.                                                          | 어떤 벡터도 해당 사실보다 오래 남지 않습니다.                                      |
| `embedding_worker`        | `embedding_dim = 0`인 스텁을 최신 순으로, 그리고 임베딩 행이 없는 메모리를 임베딩하며, 이전 모델의 스텁은 다시 임베딩합니다.                          | 5번 실패한 행은 `embed_failed_at`으로 표시되고 건너뜁니다.                         |
| `erasure_recovery`        | `in_progress` 또는 `failed` 상태에 멈춘 사가를 `steps_completed`부터 재개합니다.                                                                      | `max_attempts`(5) 이후: 최종 `failed` 상태가 되고 `erasure.abandoned` 감사 행이 남습니다. |
| `task_lease_sweep`        | 만료된 리스를 `todo`로 되돌리며, 시도 횟수를 초과하면 `blocked`로 전환하고, 보류된 작업을 재개합니다.                                                 | 중단된 에이전트의 작업은 유실되지 않습니다.                                        |
| `audit_verifier`          | 감사 해시를 다시 계산하고 체크포인트 체인을 검증합니다.                                                                                               | 변조는 보고만 하고 복구하지 않습니다.                                              |

<a id="fail-closed-choices"></a>

## 실패 시 차단하는 선택

다음 항목은 성능을 낮춰 계속 동작하는 대신 요청을 거부합니다. 성능을 낮추면 접근 범위가 넓어지거나 삭제에 대해 거짓 응답을 하게 되기 때문입니다.

- **폐기.** 프로세스가 폐기된 토큰 집합을 한 번 로드하기 전까지는 모든 토큰이 503 `AUTH_UNAVAILABLE`을 받습니다. 그 이후에는 ClickHouse 장애가 발생해도 마지막 스냅샷을 유지합니다.
- **권한 부여.** 주체의 grant map 읽기가 실패하면 503으로 응답합니다. 단, 프로세스가 `LOOM_JWT_REVOCATION_MAX_STALE_SECONDS`(600초)보다 오래되지 않았고 이후 grant 변경으로 대체되지 않은 캐시 항목을 가지고 있다면 그 항목을 사용해 응답합니다([토큰](https://github.com/ClickHouse/loom/blob/main/docs/architecture/security.md#tokens)). 단일 grant 조회(`authz.grant_role_for`)는 읽기 실패를 grant 없음으로 간주합니다. 분류를 읽을 수 없는 제한된 네임스페이스는 거부됩니다.
- **부팅 시 마이그레이션.** 마이그레이션이 실패하면 워커가 시작되기 전에 프로세스가 중단됩니다.
- **삭제.** 삭제를 확인할 수 없는 연합 백엔드는 해당 단계를 실패 처리하며, 범위 내 어디든 잔여 데이터가 있으면 saga 전체가 실패합니다.
- **개발용 서명 시크릿.** 인증이 필요한 배포는 저장소에 포함된 시크릿으로는 시작을 거부합니다.

다음 항목은 실패해도 계속 동작하며, 횟수를 집계하거나 로그를 남깁니다. 거부하면 가용성을 위한 제어 때문에 서비스 전체가 중단되기 때문입니다. 트래픽 분류, 속도 제한기, 멱등성 캐시 읽기, 감사 삽입([데이터 손실 경계](#data-loss-boundaries)의 두 가지 복원은 제외), 부팅 시 보정, 임베딩 캐시, 그리고 모든 텔레메트리 쓰기가 여기에 해당합니다. 자세한 내용은 [보안](https://github.com/ClickHouse/loom/blob/main/docs/architecture/security.md)을 참조하세요.

<a id="when-a-dependency-is-down"></a>

## 의존성이 중단되었을 때

| 중단                  | 동작                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| --------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| ClickHouse            | `/v1/health`와 `/v1/health/ready`는 503을 반환하고, `/v1/health/live`는 계속 200을 반환합니다. 요청은 `OVERLOADED`, `BACKEND_TIMEOUT` 또는 요청 id가 포함된 500으로 실패합니다. 토큰은 마지막 폐기 스냅샷을 기준으로 검증됩니다. 워커는 작업 패스에 실패하고 백오프합니다.                                                                                                                                                                                                                                                                                                                                                                                                                         |
| 임베딩 제공자         | `memory.set`과 `memory.bulk_set`은 실패합니다. 벡터 없이 저장되는 팩트는 없습니다. 기다릴 수 있는 경로(`memory.promote`, 청크 임베딩)는 `embedding_worker`가 처리하도록 스텁을 저장합니다. 대화 턴은 임베딩 오류가 기록된 채로 저장되며 임베딩되지 않은 상태로 남습니다(재시도하지 않음). `conversation.compact`는 요약을 쓰지 않고 어떤 턴도 툼스톤 처리하지 않습니다(`embed-failed`). `memory.search`는 쿼리의 벡터가 임베딩 캐시에 없으면 503 `EMBED_TIMEOUT`으로 실패합니다. 응답하지 않고 멈춘 제공자는 상태 플래그를 설정하지 않습니다(로그에 `query embed exceeded`가 표시됨). 오류로 응답하는 제공자는 `/v1/health`에 `provider_failing:<name>`으로 표시됩니다. |
| 임베딩 키 누락        | 프로세스는 가짜 임베더로 동작하며, 오류 로그와 `problems` 항목을 남깁니다. 검색은 가짜 모델로 기록된 행만 일치시킵니다.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| LLM 제공자            | 각 호출은 재시도합니다(네 번 시도). fail-open 단계(HyDE, 리랭크, 추출 검증기, 제목 생성)는 건너뜁니다. 추출과 `memory.ask` 합성에는 대체 수단이 없습니다. `LOOM_LLM_FALLBACK_MODEL`을 설정하면 장애 시 두 번째 모델이 이어받습니다.                                                                                                                                                                                                                                                                                                                                                                                                                                                              |
| 문서 버킷             | 버킷에 보관된 원본의 업로드와 다운로드는 503 `BACKEND_UNAVAILABLE`로 응답하고, 이미 사라진 원본의 다운로드는 410으로 응답합니다. 버킷에 접근하는 삭제 단계는 fail-closed로 실패하며 `erasure_recovery`가 재시도합니다. 원본 스윕은 해당 패스의 실패를 기록하고, 버킷 순회는 다음 패스에서 커서부터 재개됩니다.                                                                                                                                                                                                                                                                                                                                                                                                         |
| Langfuse, ClickStack  | 트레이싱과 내보내기는 중단되지만 요청에는 영향이 없습니다. 설정된 삭제 백엔드가 중단되면 해당 saga 단계가 실패하고, 복구 작업이 재시도합니다.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  |

**`memory.set`이 스텁을 저장하는 대신 실패하는 이유:** 스텁은 임베딩되기 전까지 모든 검색 경로에서 보이지 않으므로, "저장됨"이라는 응답을 받은 호출자가 자신이 쓴 내용을 찾지 못하게 됩니다. 실패하도록 두면 호출자가 멱등성 키로 재시도할 수 있습니다.

<a id="bluegreen-rollback"></a>

## Blue/green 롤백

`./redeploy.sh`는 유휴 상태의 컬러를 시작하고, 해당 컬러의 `/v1/health`가 `status: ok`를 보고하고 ClickHouse에 접근할 수 있으며 `schema.pending`이 비어 있고 성능이 저하된 provider가 없으며 임베딩 차원 불일치가 없을 때에만 전환합니다. 이 확인은 `LOOM_REDEPLOY_READY_TIMEOUT_S`(스크립트 변수, 180 s) 안에 완료되어야 합니다.
이전 컬러는 제거되지 않고 중지되며, 해당 이미지에는 `loom:prev` 태그가 지정됩니다.
`./redeploy.sh --rollback`은 이전 컬러를 시작하고 다시 전환합니다. 모든 마이그레이션은 추가 방식(additive)으로만 이루어지므로(`tests/unit/test_migrations_additive.py`), 이전 빌드도 더 새로운 스키마에서 실행됩니다. [업그레이드](https://github.com/ClickHouse/loom/blob/main/docs/operations/upgrades.md)를 참고하세요.

<a id="a-replica-that-dies-mid-migration-or-mid-reconfiguration"></a>

## 마이그레이션 또는 재구성 도중에 replica가 죽는 경우

모든 프로세스는 provider 참여자입니다. 프로세스마다 `loom._provider_participants`에 행이 하나 있으며, 이 행은 별도 스레드에서 10 s마다 기록됩니다. 또한 프로세스가 ClickHouse로 보내는 모든 쿼리에는 해당 id를 명시한 User-Agent가 붙습니다. 참여자는 종료(retired)되었거나, 마지막 heartbeat가 60 s(서버 시계 기준)보다 오래되었고 `system.processes`에 어느 노드에서도 해당 참여자 자신의 쿼리가 보이지 않을 때 중지된 것으로 간주됩니다. 그 외에는 중지로 간주되지 않습니다. 연결할 수 없는 프로세스가 곧 중지된 프로세스는 아닙니다.

- **마이그레이션 도중 종료된 경우.** 마이그레이션은 마이그레이션 잠금만 잡습니다(`ttl_s`, 적용하는 동안 갱신됨). provider 잠금은 잡지 않으며, provider 잠금은 부팅 시 미완료 재빌드가 없는지 확인하는 용도로만 잡습니다. 두 번째 컨테이너는 마이그레이션 잠금이 만료되면 이를 넘겨받아 남은 작업을 적용합니다.
- **provider 잠금을 잡은 채 종료된 경우**(재빌드 단계, 저장된 구성). 키의 TTL은 60 s이며 20 s마다 갱신됩니다. 잠금이 만료되고 보유자의 참여자가 중지된 것으로 간주되면 경합 중인 쪽이 이를 넘겨받습니다. 보유자에게 참여자 행이 없는 잠금은 중지된 것으로 간주되지 않으므로, `loom provider-lock clear --holder <holder>`로 해제합니다.
- **재구성 도중 종료된 경우.** 일시 중지된 프로세스가 이미 수락한 작업을 가지고 재개될 수 있으므로, 전환은 heartbeat가 만료된 참여자를 기다립니다. 같은 호스트의 프로세스가 해당 참여자가 사라졌음을 증명하고(컨테이너가 재시작되었거나 pid가 종료됨) 이를 종료 처리할 때까지 기다립니다.
  workers 리더는 하루 전에 종료된 참여자의 행을 삭제하며, 전환이 기다리게 될 참여자를 1분마다 `/v1/health`의 `provider_participants_stale`로 나열합니다. 프로세스가 사라진 것을 확인했다면 `loom participant retire --participant <id> --yes`로 직접 종료 처리할 수 있습니다. 이 명령은 heartbeat가 최근이거나 쿼리가 아직 실행 중인 참여자는 거부합니다.
- **등록 후 실패한 부팅**은 자신의 행을 종료 처리하므로, 계속 크래시를 반복하는 컨테이너가 행을 남기지 않습니다.

heartbeat 시간을 넘겨 멈춘 프로세스(중지된 컨테이너, VM 일시 중지)는 재개되어 작업을 마무리하거나 운영자가 종료 처리할 때까지 전환을 붙잡아 둡니다.

<a id="data-loss-boundaries"></a>

## 데이터 손실 경계

- **확인된 쓰기.** 성공한 `memory.set`은 두 행을 모두 커밋한 것입니다.
- **텔레메트리와 감사 행.** 장애가 발생하면 아직 플러시되지 않은 `agent_events` 큐(100 ms마다 플러시)와 이벤트 스트림 큐(`LOOM_EVENTS_STREAM_FLUSH_INTERVAL_S`마다, 1 s)가 손실됩니다.
  이벤트 스트림은 예산 위반만 담습니다. 감사, 노출, 검색 추적 행은 직접 삽입됩니다. 감사 삽입이 실패하면 로그에 기록되며, 해당 기록에 의존하는 두 가지 op만 실패합니다. 하나는 폐기를 되돌리는 `memory.restore`이고 다른 하나는 `namespace.restore`입니다([감사의 한계](https://github.com/ClickHouse/loom/blob/main/docs/features/observability/audit.md#limits-and-failure-modes)).
  검색은 이 행들을 전혀 읽지 않습니다.
- **응답 이후 프로젝션**이 장애로 손실되면 복구 워커가 다시 만들어 냅니다.
- **ClickHouse OSS 단일 노드.** 복제와 특정 시점 복구가 없습니다. 프로덕션에서는 ClickHouse Cloud 또는 복제된 OSS 클러스터를 사용해야 합니다. ClickHouse `BACKUP` 또는 네이티브 백업으로 백업하세요. `dev/backup_loom.py`는 배포, 조직 또는 네임스페이스를 옮기는 도구이며 재해 복구용이 아닙니다. [백업 및 복구](https://github.com/ClickHouse/loom/blob/main/docs/operations/backup-and-recovery.md)를 참고하세요.
- **ClickHouse 외부의 파일.** 문서 원본은 ClickHouse(`loom.document_blobs`)에 있습니다. `LOOM_DOCUMENT_OBJECT_STORE=s3`로 기록된 문서는 S3 호환 버킷에 있으며, 이는 버킷 자체의 버전 관리나 복제로 백업합니다. 수명 주기 규칙이 삭제된 원본의 사본이 보관되는 기간을 제한합니다. 코드 미러는 볼륨에 있으며, 미러가 없는 호스트는 ClickHouse에 기록된 원격 등록 정보가 가리키는 Git 저장소 원격에서 다시 클론합니다. ClickHouse는 이 원격 등록 정보를 저장할 뿐이며, 클론의 원본은 ClickHouse 레코드가 아니라 Git 저장소 원격입니다. 프로바이더 재구성은 호스트에 아무것도 남기지 않습니다
  ([프로바이더 재구성](https://github.com/ClickHouse/loom/blob/main/docs/architecture/provider-reconfiguration.md)).
- **다른 호스트나 ClickHouse로의 이전.** ClickHouse, env 설정, `LOOM_JWT_SECRET`을 옮기고, 행이 문서 버킷의 객체를 가리키는 동안에는 그 버킷도 옮깁니다. 나머지는 모두 복원됩니다
  ([배포 이전](https://github.com/ClickHouse/loom/blob/main/docs/operations/backup-and-recovery.md#moving-a-deployment)).

<a id="capacity"></a>

## 용량

서버 프로세스는 지속되는 상태를 보관하지 않으므로, 웹 용량은 프로세스를 더 실행하는 방식으로 확장합니다.
검색 작업(벡터 스캔, 텍스트 인덱스, 그래프 탐색)은 ClickHouse가 수행하므로, Python 티어를 넘어서는 용량은 곧 ClickHouse 용량입니다. 워커는 복제로 확장되지 않습니다. 하나의 supervisor가 워커를 실행합니다. 절차는 [배포](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md)와
[성능 튜닝](https://github.com/ClickHouse/loom/blob/main/docs/guides/performance-tuning.md)에 있습니다.

| 티어                       | 확장 방법                                                  | 제한 요소                                                                 |
| -------------------------- | ---------------------------------------------------------- | ------------------------------------------------------------------------- |
| 웹 (HTTP, MCP, 대시보드) | 로드 밸런서 뒤의 컨테이너 추가, `LOOM_WEB_WORKERS`         | 프로세스당 Python CPU, 프로세스별 풀, 레플리카별 속도 제한                |
| 백그라운드 워커         | 배포당 supervisor 하나, 더 큰 워커 컨테이너                | 간격 대비 패스 소요 시간, LLM 및 임베딩 작업의 제공자 할당량              |
| 검색 및 스토리지      | ClickHouse 컴퓨트, 메모리, 스토리지                        | 테넌트 읽기당 스캔되는 행 수, 수집 중 병합 부하                           |
| 제공자                  | 제공자 할당량과 배치 크기                                  | 제공자 속도 제한, `loom.embed.pending`이 임베딩 백로그를 보여 줍니다      |

<a id="web-tier"></a>

### 웹 티어

| 설정                             | 효과                                                                                                                                                                                                                                       |
| -------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| 웹 레플리카 추가                | 로드 밸런서 뒤에 동일한 레플리카 N개를 두며, 모두 `LOOM_BACKGROUND_WORKERS=auto`를 사용합니다.                                                                                                                                              |
| `LOOM_WEB_WORKERS`               | 컨테이너당 uvicorn 프로세스 수. 설정하지 않으면 이미지는 컨테이너가 사용할 수 있는 CPU마다 하나씩, 메모리 GiB당 최대 하나, 최대 8개를 시작합니다. 각 프로세스는 Python을 코어 하나에서 실행하며, 로컬 임베더는 각 프로세스에 CPU를 동일하게 나눠 줍니다. |
| `LOOM_REPLICA_COUNT`             | 각 속도 제한 버킷을 나누는 레플리카 수의 하한(아래의 서비스 중인 `loom.instances` 행).                                                                                                                                                     |
| `LOOM_LATENCY_BUDGET_MULTIPLIER` | 예산 위반이 발생하기 전에 모든 op의 `budget_p99_ms`에 곱하는 배율(기본값 1.0).                                                                                                                                                             |

새 레플리카는 `/v1/health/ready`가 200으로 응답하면 서비스를 시작합니다. 조건은 ClickHouse가 응답하고, 대기 중인 마이그레이션이 없고, 폐기 스냅샷과 서명 키링이 로드되어 최신 상태이고, 트래커 레지스트리와 런타임 오버라이드가 한 번 로드되었고(부팅 시 둘 다 `LOOM_BOOT_WARMUP_TIMEOUT_S`까지 대기), 유사도 보정이 적용되었고, 그 설정이 배포의 설정과 일치하고, 샌드박스가 필요한 곳에서 실행 중인 것입니다. SIGTERM을 받으면 해당 엔드포인트가 503으로 응답하고, 포트를 닫기 전에 `LOOM_SHUTDOWN_READY_DELAY_S` 동안 계속 서비스합니다. 프로세스 간 read-your-writes는 프로세스 친화성이 아니라 ClickHouse(대기하는 insert, 커밋 또는 배치, 순차 재읽기)가 처리합니다.

<a id="clickhouse-client"></a>

### ClickHouse 클라이언트

`server/core/ch.py`는 프로세스마다 풀 하나와 같은 크기의 승인 세마포어 하나를 제공합니다.

| 설정                                            | 기본값 | 효과                                                                                                                   |
| ----------------------------------------------- | ------ | ---------------------------------------------------------------------------------------------------------------------- |
| `LOOM_CH_POOL_MAXSIZE`                          | 64     | 프로세스당 연결 수이자 동시 구문 수. 배치 insert는 대기와 플러시 동안 연결 하나를 점유합니다.                          |
| `LOOM_CH_POOL_WAIT_MS`                          | 500    | 슬롯 대기 시간. 초과하면 `Retry-After: 1`과 함께 503 `OVERLOADED`. 0이면 게이트를 끕니다.                              |
| `LOOM_CH_TIMEOUT_READ_S`                        | 30     | 읽기 데드라인.                                                                                                         |
| `LOOM_CH_TIMEOUT_WRITE_S`                       | 60     | `INSERT` / `UPDATE` 데드라인.                                                                                          |
| `LOOM_CH_TIMEOUT_DDL_S`                         | 300    | DDL, `OPTIMIZE`, `SYSTEM` 데드라인.                                                                                    |
| `LOOM_CH_BATCHED_INSERTS`                       | true   | 모든 요청에서 쓰는 insert를 배치로 처리하며, 각 insert는 대기하는 동안 연결을 점유합니다. false이면 파트 하나씩 커밋합니다. |
| `LOOM_CH_BATCHED_INSERT_WAIT_MIN_MS`            | 10     | 배치 또는 대기형 비동기 insert의 최소 대기 시간으로, 단독 insert가 대기하는 시간입니다. 대기 동안 연결을 점유합니다.   |
| `LOOM_CH_BATCHED_INSERT_WAIT_MAX_MS`            | 50     | 요청이 기다리는 배치 또는 대기형 비동기 insert의 최대 대기 시간. 그동안 연결을 점유합니다.                             |
| `LOOM_CH_BATCHED_INSERT_BACKGROUND_WAIT_MAX_MS` | 200    | 사용 이벤트 플러셔의 틱과 드레인, 쓰기 마크 플러시에 대한 같은 값.                                                     |

**대기 시간을 제한하는 이유:** ClickHouse가 느려질 때 제한 없는 큐는 어차피 작업이 큐에 들어간 뒤 모든 요청을 클라이언트 타임아웃으로 만듭니다. 짧은 대기는 재시도 가능한 503으로 빠르게 실패합니다. 게이지 `loom.ch.pool.in_use`, `waiters`, `overloaded_total`, `timeouts_total`로 어느 쪽이 포화되었는지 알 수 있습니다.

<a id="traffic-classes"></a>

### 트래픽 클래스

모든 호출에는 클래스가 있습니다. `production`, `interactive`(기본값), `bench`, `background`(supervisor가 설정하며 요청이 설정하지 않음) 중 하나입니다. 클래스는 토큰 클레임에서 가져오고, 없으면 네임스페이스 레지스트리, 그다음 요청 힌트에서 가져옵니다. 클래스는 스케줄링만 바꾸며 결과는 바꾸지 않습니다.

- 클래스별 ClickHouse 읽기 설정(`LOOM_CH_SETTINGS_BY_TRAFFIC_CLASS`): `production`
  우선순위 1, `interactive` 2, `background` 5(스레드 3개), `bench` 10(스레드 4개).
- 속도 제한 버킷(`LOOM_RATELIMIT_CLASS_FRACTIONS`, `bench`는 절반).
- 클래스별 LLM 정책(`LOOM_LLM_POLICY_BY_TRAFFIC_CLASS`).

새로 만든 `bench-…` 네임스페이스는 `bench`로 분류되므로, 부하 테스트가 production을 고갈시키지 않습니다.

<a id="rate-limits"></a>

### 속도 제한

`server/core/ratelimit.py`는 (네임스페이스, op 클래스)별, 그리고 (org, op
클래스)별로 토큰 버킷을 유지합니다. org 티어는 네임스페이스 티어의 세 배입니다. 선택 사항인 actor별 티어는 네임스페이스 버킷의 일부를 사용합니다(`LOOM_RATELIMIT_ACTOR_FRACTION`, 1.0 = 끔). 호출은 모든 버킷을 통과해야 하며, 거부되면 어느 버킷도 차감하지 않습니다.

| op 클래스          | 대상                                                                                                          | 용량     | 초당 충전  |
| ------------------ | ------------------------------------------------------------------------------------------------------------- | -------- | ---------- |
| `memory`           | `memory.*`, `conversation.*` (정확한 키 읽기와 `memory.import` 제외)                                          | 100      | 17         |
| `memory.read`      | 정확한 키 읽기: `memory.get`, `bulk_get`, `history`, `working_memory.get`, …                                  | 300      | 50         |
| `memory.import`    | `memory.import`                                                                                               | 10       | 2          |
| `knowledge.search` | `knowledge.search`                                                                                            | 20       | 3          |
| `knowledge.query`  | `knowledge.query`                                                                                             | 20       | 3          |
| `knowledge.read`   | `knowledge.inspect`를 제외한 나머지 `read_only` `knowledge.*` op: `knowledge.get`, `knowledge.list`, …        | 50       | 10         |
| `knowledge.add`    | 그 외 모든 `knowledge.*` op. `knowledge.inspect`, `knowledge.synthesize`, `knowledge.dedup_list` 포함         | 10       | 2          |
| `code`             | `code.*` (`memory`에서 차감되는 `code.outcome` 제외)                                                          | 60       | 10         |
| `insights.query`   | `insights.query`                                                                                              | 20       | 3          |
| `assistant.ask`    | `assistant.ask`와 어시스턴트의 채팅 라우트                                                                    | 10       | 1          |
| `default`          | 그 밖의 모든 것                                                                                               | 50       | 10         |

이 값은 네임스페이스 티어의 기본값입니다(`DEFAULT_LIMITS`, `op_class_for`).

- **레플리카별.** 버킷은 프로세스 또는 호스트 안에 있습니다. 각 레플리카는 서비스 중인 `loom.instances` 행이 있는 호스트 수로 제한을 나눈 값을 갖게 되어, 레플리카들의 합이 제한에 근접합니다. `LOOM_REPLICA_COUNT`는 그 수의 하한이며, 행을 읽을 수 없는 동안에는 그 수로 사용됩니다.
- **호스트 내부.** `LOOM_WEB_WORKERS > 1`이면 프로세스들이 메모리 매핑 파일의 버킷 테이블 하나를 공유합니다(`LOOM_RATELIMIT_SHARED=auto`, `LOOM_RATELIMIT_SHARED_PATH`,
  `LOOM_RATELIMIT_SHARED_SLOTS`). `/v1/health`가 `ratelimit.backend`를 보고합니다.
- **오류 시 허용(fail open).** 리미터 오류가 나면 호출을 허용하고 그 횟수를 집계합니다.

**로컬 버킷을 쓰는 이유:** 승인마다 ClickHouse 왕복을 하면 데이터베이스를 보호하려고 모든 요청의 경로에 데이터베이스를 두게 됩니다.

<a id="background-workers"></a>

### 백그라운드 워커

배포당 supervisor 하나가 실행됩니다. `auto` 모드에서는 배포(`LOOM_DEPLOYMENT=1`)의 모든 프로세스가 ClickHouse의 리스 하나를 두고 경합합니다. 키는 Keeper 락 테이블 `loom._migration_lock`(strict KeeperMap)의 `workers_supervisor`입니다. 배포 밖(같은 ClickHouse를 쓰는 iso 또는 브랜치 서버)에서는 `auto`가 워커를 실행하지 않으며 부팅 로그에 그렇게 남습니다. `on`은 그곳에서도 워커를 실행하지만, 리더의 작업(보존 ALTER, 트래커 네임스페이스 행, 게시된 설정, 제공자 참여자 스윕)은 배포의 holder만 수행합니다.
만료는 ClickHouse 서버의 시계로 판단합니다. T = `LOOM_SUPERVISOR_LEASE_TTL_S`
(기본값 30초, 최소 24)일 때:

| 단계   | 시점                                                                         | 이유                                                                                        |
| ------ | ---------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------- |
| 갱신   | T/6마다. 갱신에 실패하면 T/12마다 재시도                                     | holder가 포기하기 전에 여러 번의 실패를 허용합니다                                          |
| 상실   | 반영된 마지막 갱신이 전송된 시점으로부터 T/2 후                              | 전송 시점부터 측정하므로 서버의 스탬프가 그보다 앞서지 않습니다                             |
| 중지   | 갱신 호출이 아직 블록된 중이더라도 그 데드라인에                             | 멈춘 호출이 워커를 계속 실행시킬 수 없습니다. 각 호출은 한 번 전송되며 타임아웃은 max(1, T/24)초입니다 |
| 드레인 | 최대 T/6, 따라서 워커는 2T/3까지 중지됨                                      | 그때까지 실행 중인 워커 자식 프로세스는 강제 종료됩니다                                     |
| 만료   | 서버가 마지막 갱신에 스탬프를 찍은 뒤 T 후. 이후 다른 프로세스가 인계받음    | 드레인 이후 T/3의 여유                                                                      |

갱신을 놓친 holder는 갱신에 성공할 때까지 새 패스를 시작하지 않으며(일시 중지되었다가 재개된 프로세스가 새 holder 옆에서 패스를 실행하지 않음), Keeper 키가 여전히 자신을 가리키는 holder는 TTL이 끝나길 기다리지 않고 즉시 리스를 되찾습니다. 리스가 다른 프로세스에 넘어간 것을 발견한 holder는 워커를 중지하고, holder가 죽으면 다른 프로세스가 T 후에 인계받습니다. 부팅 이후 holder가 된 프로세스도 리더의 부팅 작업(트래커 네임스페이스 행, 게시된 설정, `LOOM_RETENTION_*_DAYS`)을 수행합니다.

워커를 중지하면 각 패스는 다음 `await`에서 취소됩니다. 취소가 닿지 않는 작업(PageRank나 커뮤니티 계산 같은 스레드, 일시 중지 뒤 리스 루프가 실행되기 전에 재개되는 패스)은 패스의 다음 `pass_context.check_stop()`에서 멈추며, 긴 루프가 배치 사이에 이를 호출합니다. 해당 루프는 수집 단계, SCIP 델타 라운드, `code_pr_history`, `code_graph_stats`, `entity_communities`입니다. 패스가 중지되었거나 리스가 더 이상 최신이 아니면 예외를 발생시키므로, 패스는 더 이상 쓰기를 보내지 않습니다. 펜싱 토큰은 없으므로 이미 전송된 구문은 그대로 반영됩니다.

프로세스는 다음 경우에 비켜섭니다(holder는 워커를 중지하고 키를 내려놓으며, 경합자는 요청하지 않음).

- SIGTERM 이후 드레인하는 동안. 따라서 다른 프로세스가 그 ready 지연 동안 인계받습니다.
- 설정이 배포의 게시된 설정과 다른 경우(준비 상태가 아니며, 잘못된 공간에 벡터를 쓰거나 잘못된 루트로 서명할 수 있음).
- 더 새로운 빌드의 서비스 중인 `loom.instances` 행이 같은 리스를 두고 경합하는 경우.

마지막은 롤링 업그레이드 인계입니다. 각 행은 해당 빌드의 스키마 버전(마지막 마이그레이션 번호)과 현재 경합 중인 리스를 담고 있습니다. 해당 프로세스가 드레인 중이거나, 설정이 달라졌거나, 리스를 내려놓았다면 리스는 없음으로 표시되므로 그런 행 때문에 더 오래된 빌드가 양보하는 일은 없습니다. 인계는 스키마 버전이 다를 때만 일어나므로 코드만 바뀐 릴리스는 인계하지 않으며, 이전 holder가 멈출 때까지 워커를 유지합니다. 더 새로운 빌드가 더 이상 살아 있지 않으면(롤백) 오래된 빌드가 다시 경합합니다. 워커 자식 프로세스가 계속 시작에 실패하는 프로세스는 리스를 5분 동안 내려놓습니다(아래 참조).

따라서:

- 여러 호스트가 `auto`로 실행될 수 있으며, 그중 하나가 워커를 실행합니다. `on`은 선출을 건너뜁니다.
- 유지보수 워커를 하나 더 실행하면 ClickHouse 작업이 낭비되고, LLM 워커를 하나 더 실행하면 스캔과 비용이 두 배가 됩니다.
- holder는 워커를 자식 프로세스(`LOOM_WORKERS_PROCESS=child`)에서 실행하므로, 긴 패스가 요청을 처리하는 이벤트 루프를 공유하지 않습니다(서빙 루프에서 패스를 실행하면 요청 p90이 대략 두 배가 되었습니다. `code_ingest` 패스 중 `memory.get`이 74–79 ms 대비 148 ms). 자식 프로세스는 supervisor와 워커가 사용하는 것(ClickHouse 클라이언트, 제공자 설정, 이벤트 emitter, 트래커 및 리포지토리 레지스트리)을 실행하며, 서빙 lifespan은 실행하지 않습니다.
  자식은 부모로부터 리스 데드라인을 매초 읽으며, 리스가 최신이 아니면 데드라인은 더 이상 움직이지 않습니다. 자식은 그 데드라인에 T/6을 더한 시점까지 사라집니다. 그때 스스로 종료하고, 부모가 그때 강제 종료하며, 부모가 죽으면 커널이 종료합니다(`PR_SET_PDEATHSIG`). 자식의 이벤트 루프는 매초 부모에게 비트를 보내고, `/v1/health`는 15초 동안 비트가 없는 자식을 살아 있지 않은 것으로 표시합니다. 드레인 중인 프로세스(SIGTERM)는 인라인이든 자식이든 워커를 중지하고, 계속 서비스하면서 리스를 내려놓습니다. 시작 후 60초 이내에 종료하는 일이 연속 다섯 번 일어나면 해당 프로세스는 리스를 내려놓고 `workers_lease_given_up`을 보고합니다. 다른 프로세스가 워커를 인계받고, 이 프로세스는 5분 뒤에 다시 요청합니다.

워커 작업은 프로세스 간에 분할되지 않습니다. 워커는 제한된 패스로 따라갑니다. 패스마다 배치 또는 시간 예산이 있으며(예: `embedding_worker`는 스텁을 최대 100개 임베딩하고, `usage_fold`는 120초 안에 메모리를 최대 1000개 폴딩하며 대기 중인 뮤테이션이 20개이면 물러남), 위의 데드라인이 있습니다. 패스가 일상적으로 데드라인에 걸린다면 워커 컨테이너에 CPU가 더 필요하거나 제공자에 할당량이 더 필요하다는 뜻입니다.

<a id="batch-sizes-and-queues"></a>

### 배치 크기와 큐

| 경로                              | 제한                                                                                                       |
| --------------------------------- | ---------------------------------------------------------------------------------------------------------- |
| 요청 본문                         | `LOOM_MAX_REQUEST_BYTES`(32 MiB). 초과하면 413                                                             |
| `memory.bulk_set`                 | 한 번의 `embed_many`에 최대 1000개 항목. 충돌 검사는 `LOOM_BULK_CONFLICT_CHECK_MAX_ENTRIES`(200)까지        |
| `memory.bulk_get`                 | 최대 1000개 키                                                                                             |
| `memory.set_from_messages`        | 최대 400개 메시지                                                                                          |
| 임베딩 요청                       | 제공자 요청당 최대 100개 입력(OpenAI, Gemini)                                                              |
| `agent_events` emitter            | 10,000행 큐, 0.1초 또는 500행마다 플러시                                                                   |
| 이벤트 스트림(예산 위반)          | 20,000 큐, `LOOM_EVENTS_STREAM_FLUSH_INTERVAL_S` 또는 `_FLUSH_BATCH` 행마다 플러시                         |
| 워커 상태 행                      | 15초 또는 200행마다 플러시                                                                                 |

<a id="vector-cost"></a>

### 벡터 비용

메모리는 `Array(BFloat16)` 벡터를 최대 두 개(값과 키) 저장합니다. 1536차원에서는 6 KiB, 3072차원에서는 12 KiB입니다. 스토리지와 스캔 바이트는 `LOOM_EMBEDDING_DIM`에 비례해 늘어납니다.
HNSW 인덱스가 없으므로 읽기는 테넌트의 네임스페이스에 있는 모든 벡터를 스캔하며, 지연 시간은 네임스페이스 크기에 따라 늘어납니다. 코덱을 켤 수 있는 곳에서는 양자화된 코드가 스캔을 줄입니다
([게이트된 설정](https://github.com/ClickHouse/loom/blob/main/docs/architecture/clickhouse.md#gated-clickhouse-settings)).
자세한 내용은 [ClickHouse](https://github.com/ClickHouse/loom/blob/main/docs/architecture/clickhouse.md#vector-storage)를 참조하세요.

<a id="measuring"></a>

### 측정

이 페이지는 지연 시간이나 처리량 수치를 제시하지 않습니다. 이 값은 ClickHouse 배포, 네트워크 거리, 네임스페이스 크기에 따라 달라집니다. 격리된 서버에서 직접 측정하세요.

| 명령                                                        | 측정 대상                                                                                              | 출력                |
| ----------------------------------------------------------- | ------------------------------------------------------------------------------------------------------ | ------------------- |
| `make bench OP=memory.set C=10 D=30`                        | 동시성 C로 D초 동안 실행한 op 하나의 p50 / p95 / p99와 RPS(`scripts/ops/benchmark.py`)                 | stdout 또는 `--json` |
| `python bench/search_latency.py --namespaces … --queries …` | 네임스페이스 크기 구간별 `memory.search` p50 / p95와 `retrieval_work.stage_ms`의 단계별 시간            | `--out` CSV         |
| `make bench-quick`                                          | 30개 질문 샘플에 대한 검색 품질(실제 제공자 필요)                                                      | `/tmp` 아래 CSV     |

`scripts/ops/benchmark.py`는 새 `bench-…` 네임스페이스에 쓰므로 `bench` 클래스로 실행됩니다. `memory.search` 실행에는 `memory.set` 실행으로 채워진 네임스페이스가 필요합니다.
[벤치마크 실행](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/running.md)을 참조하세요.

<a id="what-to-scale"></a>

### 무엇을 확장할 것인가

| 신호                                                            | 확장 대상                                                 |
| --------------------------------------------------------------- | --------------------------------------------------------- |
| 웹 CPU가 높고 요청 p99가 상승하며 ClickHouse는 빠르게 응답      | 웹 레플리카 또는 `LOOM_WEB_WORKERS`                       |
| `loom.ch.pool.waiters`와 `OVERLOADED`가 증가하고 ClickHouse는 빠름 | `LOOM_CH_POOL_MAXSIZE` 또는 레플리카 추가                 |
| `BACKEND_TIMEOUT`과 느린 `system.query_log` 항목                | ClickHouse 컴퓨트, 또는 읽기당 더 작은 네임스페이스       |
| `loom.embed.pending` 증가                                       | 임베더 처리량(제공자 할당량)                              |
| `loom.worker.consecutive_failures` 또는 패스 소요 시간 증가     | 워커 컨테이너의 CPU와 메모리                              |
| 수집 중 병합 부하, 메모리 오류                                  | ClickHouse 컴퓨트와 메모리, 또는 더 낮은 쓰기 동시성      |

메트릭은 [모니터링](https://github.com/ClickHouse/loom/blob/main/docs/operations/monitoring.md)에 설명되어 있습니다.

<a id="known-limits"></a>

### 알려진 제한

- **배포당 워커 리더 하나.** 워커 패스는 호스트 간에 분산되지 않습니다.
- **레플리카별 속도 제한**은 근사치입니다. 버킷 충전은 호스트별로 이루어집니다. 로그인 및 가입 시도 제한은 `loom.auth_attempts`에서 모든 프로세스에 걸쳐 집계됩니다.
- **레플리카 간 check-then-insert**(가입 시의 이름과 이메일, 개인 네임스페이스, OAuth 코드 또는 리프레시 토큰, 진행 중인 멱등성 키)는 배포 전체 클레임, 즉 `loom._migration_lock`의 strict KeeperMap 키 아래에서 실행되며 확인은 순차 읽기로 합니다. 두 번째 경합자는 409를 받고, 응답할 수 없는 클레임 저장소는 503입니다(이 경우 멱등성 키는 일반적인 미스로 실행됩니다).
- **이벤트 스트림 텔레메트리**(예산 위반)는 `SIGTERM` 시 드레인됩니다. `SIGKILL`이나 OOM이 발생하면 최대 한 번의 플러시 간격만큼을 잃으며, 감사 행이나 메모리 행은 잃지 않습니다.
- **네임스페이스 크기가 읽기 지연 시간을 좌우합니다.** 벡터 읽기는 벡터 인덱스 없이 네임스페이스의 행을 스캔하기 때문입니다.
- **단일 노드 ClickHouse OSS**에는 복제와 장애 조치가 없습니다.

<a id="knowledge-restore"></a>

## 지식 복원

`knowledge.restore` ([삭제된 문서 복원](https://github.com/ClickHouse/loom/blob/main/docs/features/knowledge.md#restoring-a-deleted-document))는
삭제 요청(erasure)이 미친 범위의 데이터를 절대로 되살려서는 안 됩니다. erasure는 자체 표식을 남깁니다. 범위 안의 모든 청크에 표식을 찍으며, 삭제된 문서의 청크도 포함합니다. 또한 라이브 행은 제자리에서 대체(supersede)합니다.
restore는 문서가 기록될 당시 아직 끝나지 않았고 해당 문서에 영향을 줄 수 있는 erasure가 있는지도 erasure 원장에서 읽습니다. 대상은 문서의 네임스페이스에 대한 erasure, 해당 네임스페이스를 지정한 네임스페이스 목록에 대한 erasure, 복원되는 리비전을 작성한 사용자에 대한 erasure입니다. 중간에 끊긴 restore가 남긴 라이브 잔여 행은 어떤 서명도 작성자와 연결하지 못하므로, 이 경우에는 모든 사용자의 erasure가 대상입니다.

restore는 행을 쓰기 전에 원장을 읽고 쓴 뒤에 다시 읽습니다. 두 번째 읽기에서 erasure가 발견되면 쓴 내용을 되돌리며, 쓰기를 시작한 뒤 어떤 단계가 실패해도 마찬가지로 되돌립니다. purge가 진행 중이면 restore는 자신의 행을 삭제하므로 내용의 사본이 남지 않습니다. 병합(merge)이 삭제 행을 그 행에 합쳤다면 이 삭제로 소스에는 행이 하나도 남지 않습니다. 문서를 포함하는 purge라면 어차피 행이 남지 않으며, 포함할 가능성만 있는 purge(읽을 수 없는 목록, 또는 중간에 끊긴 restore의 잔여 행에 대한 임의 사용자의 purge)에서는 문서의 폐기된(retired) 행이 사라집니다. 이는 fail-closed 선택입니다.
그 외의 경우와 원장을 읽을 수 없는 경우에는, 그 사이에 erasure가 행에 남긴 표식을 유지한 채 행을 폐기된 상태로 다시 씁니다. 행을 삭제하면 병합이 언제든 삭제 행을 그 행에 합칠 수 있어 소스에 행이 하나도 남지 않을 수 있기 때문입니다. 소스의 행이 하나도 남지 않았다면(purge가 모두 가져간 경우) 아무것도 다시 쓰지 않습니다.

Postgres authority에서는 restore가 문서 헤드를 통해 씁니다([스토리지 authority](https://github.com/ClickHouse/loom/blob/main/docs/architecture/storage-authority.md#knowledge-documents)). 소스의 게시(publication)가 아직 ClickHouse에 반영될 수 있는 동안에는 재시도 가능한 오류로 거부합니다. 해당 게시는 아직 처리해야 할 것이거나, 전달 워커(delivery worker)가 넘겨받아 리스(lease)의 삽입 한도 안에 마무리한 것입니다. 그렇지 않으면 삭제가 청크를 폐기하는 작업이 restore가 청크를 되살린 뒤에 반영될 수 있기 때문입니다. 다시 쓰기는 읽어 둔 버전을 기준으로 커밋하므로, 그 사이에 같은 소스에 텍스트 쓰기가 커밋되면 restore가 거부됩니다(409). 커밋되었지만 게시하지 못한 경우에는 되돌린 뒤 503으로 응답합니다. purge 되돌리기는 복원된 헤드와 그 전달도 함께 되돌리며, ClickHouse 행을 삭제하기 전에 해당 전달의 리스가 끝나기를 기다립니다.

응답하기 전에 호출자가 사라진 경우(클라이언트 연결 끊김, 요청 취소)에도, restore는 claim을 유지한 채 끝까지 실행된 뒤 스스로를 되돌립니다. 이를 기록할 감사 행이 없기 때문입니다. 되돌리기는 claim을 놓기 전에 끝납니다. restore가 응답한 뒤 전송 계층이 감사 행을 쓰는 동안 호출자가 사라지면, 모든 감사 대상 쓰기와 마찬가지로 감사 행 없이 restore가 그대로 남을 수 있습니다.

되돌릴 수 없는 경우:

- 되돌리기 전에 중단된 restore는 폐기된 행 아래에 청크를 검색 가능한 상태로 남깁니다. 다시 호출하면 마무리됩니다. 먼저 실행된 삭제는 해당 청크를 문서 자체의 삭제 시점 표식으로 폐기하므로, restore가 여전히 청크를 찾을 수 있습니다.
- 두 번의 원장 읽기 사이에 erasure가 등록되고 완료된 뒤 되돌리기가 실패한 경우(또는 두 번째 읽기와 되돌리기가 모두 실패하거나, 그 사이에 프로세스가 종료된 경우)입니다. 행은 라이브로 남으며, 이후 호출은 `live`로 응답합니다.
- 되돌리기가 원장을 읽을 수 없는 purge는 행을 폐기된 상태로 남깁니다. 모든 행을 세는 purge의 잔여 census는 되돌리기 이후에 실행된 경우에만 이를 보고합니다.
- 원본 sweep은 라이브 행이 가리키지 않는 원본을 충분히 오래되면 삭제하며, 삭제 시점에는 나이만 확인합니다. 문서가 삭제된 동안 네임스페이스를 읽은 sweep 패스는 restore가 원본을 찾은 뒤에 그 원본을 삭제할 수 있습니다. 그러면 복원된 행은 사라진 원본을 가리키게 되고(`kept`로 응답), 다운로드는 실패합니다. 기존 원본을 다시 가리키는 모든 쓰기에 이 구간이 똑같이 존재합니다.

동시 쓰기: 텍스트 쓰기(`knowledge.add`, `knowledge.upsert`)와 라이브 문서의 삭제는 claim을 사용하지 않습니다. ClickHouse authority에서는 restore 중에 같은 소스에 텍스트 쓰기가 반영되면, 삭제 중과 마찬가지로 다음에 해당 소스에 쓸 때까지 두 버전의 청크가 모두 검색 가능한 상태로 남을 수 있습니다. restore보다 먼저 시작된 쓰기는 더 낮은 버전으로 행을 저장하므로 복원된 행이 이를 가립니다. 삭제 행이 삭제보다 먼저 시작된 쓰기를 가리는 것과 같으며, 이 경우 다시 쓰세요. Postgres authority에서는 둘 중 나중에 커밋하는 쪽이 409로 응답하고 자신의 청크를 폐기합니다. restore가 스스로를 되돌릴 때 이후 쓰기의 행이 우선했다면, 그 행은 그대로 두고 자신의 청크만 폐기합니다. 이미 삭제된 소스를 다시 삭제하면 소스의 claim 아래에서 남은 청크를 폐기하며, 그 아래에서 순차 일관성(sequential consistency)으로 문서를 다시 읽습니다. 그 사이 restore가 되살린 문서는 이때 라이브이므로 청크가 유지됩니다. 삭제는 tombstone까지의 청크만 폐기하고 집계합니다. 아직 커밋되지 않은 더 새로운 쓰기의 청크는 남으며, 그런 청크만 발견한 삭제는 삭제된 것이 없다고 응답합니다.

<a id="knowledge-move"></a>

## 지식 이동

`knowledge.move`([문서 이동](https://github.com/ClickHouse/loom/blob/main/docs/features/knowledge.md#moving-documents))는 각 문서를 이동할 네임스페이스에 먼저 쓴 다음 원래 있던 곳에서 삭제합니다. 따라서 중간에 끊긴 이동은 문서를 양쪽에 남길 뿐, 어느 쪽에도 없는 상태를 만들지 않습니다. 문서마다 대상 위치를 읽고, 무언가를 쓰기 전에 그곳에 다른 유효한 문서가 있으면 `exists`로 응답합니다. 클레임을 잡은 상태에서는 양쪽을 다시 읽습니다. 그런 다음 원본과 추출 아티팩트를 클레임 밖에서 새 네임스페이스에 저장합니다(어떤 행도 아직 가리키지 않는 원본은, 행이 반영되지 않은 업로드와 마찬가지로 스윕의 대상입니다). ClickHouse 행으로 보관된 원본은 ClickHouse 안에서 복사합니다. 버킷에 있는 원본, 또는 그 사이에 버킷으로 옮겨진 원본은 읽은 뒤 새 네임스페이스의 버킷 위치에 다시 씁니다. 삭제(erasure)로 제거된 원본(문서의 `blob_erased_at`)은, 해당 sha의 바이트가 다른 업로드를 위해 여전히 보관되어 있더라도 복사하지 않습니다.

그다음 양쪽 네임스페이스에서 소스의 클레임을 정렬된 순서로 잡습니다. 이때 대기하지 않으며, 이미 잡혀 있는 소스는 즉시 `busy`로 응답합니다. 다음 경우에는 `busy`로 거부합니다. 읽은 이후 어느 한쪽 위치가 변경된 경우, 어느 한쪽 위치의 게시(publication)가 아직 반영될 수 있는 경우, 또는 어느 한쪽의 인제스트 작업이 열려 있는 동안입니다(그 게시가 문서를 원래 위치에 다시 쓰거나 복사본을 덮어쓸 수 있기 때문입니다). 문서 리비전의 청크를 벡터와 함께 ClickHouse 안에서 복사하고(`INSERT … SELECT`, 행이 서비스하는 모든 버전을 대상으로 하며, 메타데이터 편집은 청크를 자체 버전에 그대로 둡니다), 이어서 테이블, 행 순서로 복사한 뒤, 읽은 버전을 기준으로 소스를 삭제합니다. 삭제 전에 멈춘 이전 이동이 남긴 복사본(대상 위치에 텍스트, 제목, 설명, 태그, 메타데이터, 작성자, 인제스트 시각이 같은 경우)은, 소스와 같은 수의 유효한 청크와 같은 테이블을 서비스하고 있으면 다시 쓰지 않으며 소스만 삭제합니다. 청크가 더 적거나 소스에는 테이블이 있는데 테이블이 없는 복사본(취소된 행 삽입은 되돌리기 이후에 반영될 수 있으며, 이때 행은 건드리지 않습니다)은, 삭제된 문서와 마찬가지로 소스를 삭제하기 전에 덮어씁니다. 청크가 더 많은 복사본은 온전한 것으로 봅니다. 소스의 청크를 폐기하는 단계와 톰스톤을 쓰는 단계 사이에서 삭제가 끊기면(청크를 먼저 폐기합니다) 소스 행은 청크 없이 유효한 상태로 남고 삭제만 남게 됩니다. 소스보다 많은 청크를 서비스하는 복사본은 절대 덮어쓰지 않으며, 테이블도 없는 경우에는 `exists`로 응답합니다. 그 외에 테이블이 다른 경우는 그 뒤 한쪽에서 변경된 것(승인, 명확화)인데, 다른 쪽의 변경과 구분할 수 없습니다. 이런 복사본은 텍스트나 메타데이터가 편집된 복사본과 마찬가지로 다른 문서이며 `exists`로 응답합니다.

온전하게 반영되지 않은 작업이 있거나, 삭제 전에 어느 한쪽 위치를 다른 쓰기가 차지했다면, 이동이 대상에 쓴 내용을 되돌리고 해당 소스는 `busy`로 응답합니다. 되돌리기는 요청이 그 사이에 몇 번 취소되더라도 클레임을 반납하기 전에 끝까지 실행됩니다(취소는 끝난 뒤에 발생시킵니다). 첫 번째 문장은 모든 임베딩 모델에 대해 복사본의 청크를 한꺼번에 폐기합니다. 되돌리기는 클레임의 컷오프에서 시작하거나, Postgres가 권한 저장소(authority)인 경우 Postgres가 거부한 앞선 삭제 직후에 시작하며, 그 커밋은 `pg_commit_lock_timeout_ms`와 `pg_statement_timeout_ms`(기본값 2초와 5초) 안에 응답합니다. 문장 자체의 제한(TTL의 15%, 9초)을 더하면 폐기는 약 40초에 끝나며, 이는 클레임의 60초 TTL 안입니다. 나머지 작업은 TTL을 넘길 수 있지만, 그 뒤 클레임을 잡는 이동은 복사본이 부족한 것으로 읽습니다. 클레임을 잡은 상태에서 이전 이동의 복사본이 온전한지 다시 확인하고, 온전하지 않으면 `busy`로 응답하며, 이후 호출에서 복사본을 다시 씁니다. 같은 호출의 다른 소스는 그대로 이동합니다. 폐기 후 되돌리기는 (Postgres가 권한 저장소인 경우) 자신의 커밋을 지우고, 행을 삭제한 다음 청크와 테이블을 삭제합니다. 따라서 중간에 멈춘 되돌리기가 행 없이 서비스되는 청크를 남기지 않으며, 청크 삭제 후에 반영되는 취소된 `INSERT … SELECT`도 폐기됩니다. 클레임 아래의 작업은 클레임 TTL 60초의 40%(24초)에서 중단되며, 복사가 그 시간을 넘기는 문서는 되돌려지고 `busy`로 응답합니다. 삭제는 요청이나 클레임이 그 사이에 어떻게 끝나든 끝까지 실행되며, 톰스톤이 기록되는 순간 문서는 이동된 것으로 집계됩니다. 따라서 톰스톤이 전송되는 동안 컷오프나 취소가 발생해도 이동됨으로 응답하고 감사 기록을 남기며, 그 뒤 Postgres 권한 저장소가 거부한 톰스톤은 복사본을 되돌립니다. 삭제 자체의 실패(호출을 실패시키는 응답 유실)는 톰스톤 뒤에 올 수 있으므로 복사본은 그대로 남습니다. 이 경우 문서는 양쪽에 있거나 대상에만 있으며, 재시도하면 복사본을 찾아 소스만 삭제하거나 `not_found`로 응답합니다. 톰스톤이 기록되면 이동은 완료된 것입니다. 소스의 테이블은 클레임을 반납한 뒤 삭제되는데, 유효한 문서의 삭제는 클레임을 잡지 않으므로 어떤 컷오프도 여기에 영향을 주지 않습니다. 여기서 실패하면 로그에 남으며, 소스에 대한 이후 삭제가 이를 정리합니다. 문서는 클레임 아래에서 톰스톤이 기록되는 순간 이동된 것으로 집계되며, 그 이후 호출을 끝내는 어떤 일이 생겨도(클레임을 반납하는 중이나 정리 중의 취소) 감사 행과 테이블 정리가 이어집니다. 한 소스에 국한되지 않는 실패(응답하지 않는 저장소)는 호출의 모든 소스가 응답한 뒤, 이동된 소스의 감사 행 다음에 발생시킵니다.

삭제(erasure) 원장은 `knowledge.restore`가 읽는 방식(문서의 네임스페이스와 작성자에 대한 `unfinished_reaching`을 `may_cover`로 필터링)과 같게 문서마다 두 번 읽습니다. 첫 번째는 원본을 저장하기 전에, 현재 위치에 닿을 수 있는 삭제(텍스트가 그곳에 기록된 시점에 미완료인 경우: 첫 청크의 `created_at`이 기준이며, 이동은 청크를 복사한 시각으로 이를 기록하는 반면 행은 인제스트 시각을 유지합니다)나 이동할 위치에 닿을 수 있는 삭제(이동이 시작된 시점에 미완료인 경우)를 확인합니다. 두 번째는 클레임을 잡은 상태에서 삭제 직전에, 그 사이에 등록된 삭제를 확인합니다. 그런 삭제는 이번 이동의 복사본이나 이전 이동의 복사본을 지나쳤을 수 있습니다. 이번 이동이 쓴 복사본은 되돌리고, 이전 이동의 복사본은 삭제 작업에 맡기며, 소스를 삭제하지 않은 채 `erased`로 응답합니다. 따라서 `erased` 문서는 항상 원래 위치에 남습니다. Postgres가 권한 저장소인 경우, 그 사이 삭제로 제거된 문서에 대한 delete도 `erased`로 응답합니다. 첫 번째 읽기는 원본을 새 위치에 저장하기 직전에 이루어지므로, 원본 단계가 놓칠 수 있는 삭제는 그 읽기와 저장 사이의 순간에 등록되어야 합니다. 그러면 삭제 전의 재확인이 `erased`로 응답하고, 어떤 행도 가리키지 않는 그 원본은 행이 반영되지 않은 원본과 마찬가지로 `document_blob_min_age_seconds` 이후 스윕의 대상이 됩니다. 이동은 원본 자체를 삭제하지 않습니다. 원본은 바이트를 키로 하며, 행이 아직 반영되지 않은 같은 바이트의 동시 쓰기가 이를 필요로 할 수 있기 때문입니다.

동시 쓰기: 텍스트 쓰기는 클레임을 잡지 않습니다. Postgres가 권한 저장소인 경우 복사본의 행과 삭제가 각각 읽은 버전을 기준으로 커밋되므로, 소스나 대상 위치를 먼저 차지한 쓰기가 이를 거부하며(409) 복사본은 되돌려집니다. ClickHouse가 권한 저장소인 경우 이동은 복사 전에 클레임을 잡은 상태에서 대상의 헤드를 읽고 행을 쓴 뒤 다시 읽습니다. 더 나중 버전의 쓰기가 이기면 복사본은 되돌려지고, 첫 번째 읽기와 행 사이(청크와 테이블 복사 전체)에 반영된 더 이전 버전의 쓰기는, 한 소스에 대한 두 텍스트 쓰기와 마찬가지로 교체됩니다.

<a id="invariants"></a>

## 불변 조건

- 쓰기가 성공했다면 기본 행(primary rows)이 커밋된 것입니다. 둘 중 하나라도 실패하면 요청은 실패합니다.
- 재시도하는 INSERT는 중복 제거 토큰을 포함하거나, 재시도하지 않습니다.
- `OVERLOADED`와 `AUTH_UNAVAILABLE`에는 재시도 힌트가 포함됩니다. `SCHEMA_PENDING`은 운영자의 조치를 기다립니다.
- 워커 패스는 멱등입니다. 두 번 실행해도 같은 상태로 수렴합니다.
- 접근 검사와 삭제는 실패 시 닫힙니다(fail closed). 가용성 제어와 텔레메트리는 실패 시 열리며(fail open), 그 횟수를 집계합니다.
- 어떤 요청도 풀, 제공자 또는 큐에서 무한정 대기하지 않습니다. 모든 구문은 슬롯을 얻기 위해 최대 `LOOM_CH_POOL_WAIT_MS`만큼 대기하고, 최대 해당 클래스의 데드라인까지만 실행됩니다.
- 트래픽 클래스는 스케줄링과 버킷만 바꾸며, 반환되는 행은 바꾸지 않습니다.
- 모든 마이그레이션은 추가형(additive)이므로, 이전 빌드도 현재 스키마에서 실행됩니다.

<a id="source-entry-points"></a>

## 소스 진입점

| 항목                     | 위치                                                                                                                                                                                                                                        |
| ------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 클라이언트 재시도 및 게이트 | `server/core/ch.py` (`_with_retry`, `_acquire_slot`, `_bounded`, `statement_timeout`), `server/core/ch_schema_guard.py`                                                                                                                     |
| 오류 코드                | `server/core/errors.py`                                                                                                                                                                                                                     |
| 멱등성                   | `server/core/idempotency.py`                                                                                                                                                                                                                |
| 슈퍼바이저 및 리스       | `server/workers/supervisor.py`, `server/workers/leader.py`, `server/workers/registry.py` (`default_max_pass_seconds`)                                                                                                                       |
| 복구 워커                | `server/memory/workers/projection_repair.py`, `orphan_vector_reconcile.py`, `embedding.py`, `entity_extractor.py`; `server/erasure/workers/recovery.py`; `server/tasks/workers/lease_sweep.py`; `server/insights/workers/audit_verifier.py` |
| 해지 스냅샷              | `server/auth/revocation.py` (`load_at_boot`)                                                                                                                                                                                                |
| 쿼리 임베딩 제한         | `server/memory/app/search/embed.py`                                                                                                                                                                                                         |
| 쓰기 후 프로젝션         | `server/memory/app/after_write.py`, `server/memory/app/projection_failures.py`                                                                                                                                                              |
| 텔레메트리 큐            | `server/observability/events.py`, `server/observability/events_stream.py`, `server/workers/status_store.py`                                                                                                                                 |
| 트래픽 클래스            | `server/observability/traffic_class.py`, `server/identity/app/traffic_class_registry.py`                                                                                                                                                    |
| 속도 제한기              | `server/core/ratelimit.py` (`DEFAULT_LIMITS`, `op_class_for`), `server/core/ratelimit_shared.py`, `server/auth/attempts.py`                                                                                                                 |
| 스키마 상태              | `server/core/schema/status.py`                                                                                                                                                                                                              |
| 블루/그린                | `redeploy.sh`, `dev/docker-compose.colour.yml`                                                                                                                                                                                              |
| 부하 벤치마크            | `scripts/ops/benchmark.py`, `bench/search_latency.py`                                                                                                                                                                                       |

<a id="related"></a>

## 관련 문서

- [문제 해결](https://github.com/ClickHouse/loom/blob/main/docs/operations/troubleshooting.md): 증상별 런북입니다.
- [모니터링](https://github.com/ClickHouse/loom/blob/main/docs/operations/monitoring.md): 여기에 언급된 메트릭을 설명합니다.
- [보안](https://github.com/ClickHouse/loom/blob/main/docs/architecture/security.md): 실패 시 차단(fail-closed) 방식의 접근 검사를 설명합니다.
- [ClickHouse](https://github.com/ClickHouse/loom/blob/main/docs/architecture/clickhouse.md): 테이블, 벡터 컬럼, 쿼리 귀속을 설명합니다.
