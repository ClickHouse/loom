<!-- i18n: source=docs/guides/performance-tuning.md blob=285ede1bf84d35f39aa2de165f838e15263c115e model=claude-sonnet-5-5 -->
> 이 페이지는 [영어 원문](https://github.com/ClickHouse/loom/blob/main/docs/guides/performance-tuning.md)을 기계 번역한 것이며, 영어 원문이 기준입니다.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/guides/performance-tuning.md), which is authoritative.

<a id="tune-looms-performance"></a>

# Loom 성능 튜닝

이 페이지의 모든 조정 항목은 설정 또는 요청 필드입니다.
[구성 레퍼런스](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md)에서 각 설정의 기본값과
의미를 확인할 수 있습니다. 트레이드오프를 측정할 수 있는 경우, 이 페이지에 측정에 사용하는 명령을 명시합니다.

<a id="measure-first"></a>

## 먼저 측정하기

| 질문                                               | 명령                                                                                  | 필요한 것                                            |
| -------------------------------------------------- | ------------------------------------------------------------------------------------- | ---------------------------------------------------- |
| 동시성 N에서 한 op의 p50 / p95 / p99 및 RPS        | `make bench OP=memory.search C=5 D=60` (`scripts/ops/benchmark.py`)                   | 실행 중인 서버. 쓰기는 `bench-…` 네임스페이스로 들어감 |
| 랭킹 또는 임베더 변경이 recall을 떨어뜨렸는가?     | `make bench-quick` (답변 판정기를 사용하는 공개 장기 메모리 벤치마크의 일부)          | 실제 provider, `OPENAI_API_KEY`                      |
| 기록된 기준선 대비 recall                          | `make bench-quality-gate MIN_RECALL=…`                                                | 실제 provider                                        |
| 검색 모드 × rerank × 풀 너비                       | `make bench-matrix` (`bench/MATRIX_RESULTS.md`에 기록)                                | 실행 중인 서버                                       |
| 추출 및 그래프 추출 회귀                           | `make bench-algorithm-gate`                                                           | 실행 중인 서버                                       |
| 코드 플레인의 심볼, 호출자, recall, 토큰 비용      | `make bench-code-intel`, `make bench-code-legs`                                       | 실제 임베더                                          |
| 배포 후 무엇이 바뀌었는가                          | `loom deploy-delta` (교체 전후의 ledger와 ClickHouse 구문 형태)                       | `var/deployed.json`                                  |

> **경고:** 가짜 임베더로 측정한 recall 수치는 실제 검색 성능에 대해 아무것도 알려 주지 않습니다. 이 때문에 품질 게이트는 CI에서 제외되어 있습니다. 배포할 provider를 사용하는 서버에서 실행하고, 임베더를 바꾼 뒤에는 기준선을 다시 잡으세요.

느린 요청 하나를 분석할 때는 설정을 건드리기 전에 `Server-Timing` 헤더(`app` 대 `ch`)와 `system.query_log` 행을 먼저 확인하세요([디버깅](https://github.com/ClickHouse/loom/blob/main/docs/guides/debugging.md)). 배포 전체의 연산별 비용은 `log_comment`에 대한 쿼리 하나로 구할 수 있습니다.

```sql
SELECT splitByChar(' ', log_comment)[1] AS source,
       count() AS queries, sum(read_bytes) AS bytes, sum(query_duration_ms) AS ms
FROM system.query_log
WHERE type = 'QueryFinish' AND event_time > now() - INTERVAL 1 HOUR AND log_comment != ''
GROUP BY source ORDER BY ms DESC LIMIT 20;
```

교체 직후의 벤치마크 트래픽은 비어 있는 캐시를 읽습니다. 배포의 지연 시간을 이전 빌드와 비교하려면 잠시 기다리세요.

<a id="ranking-and-thresholds"></a>

## 랭킹과 임계값

`memory.search`는 첫 단계 후보 풀의 순위를 매긴 뒤 랭킹 항목과 게이트를 적용합니다
([검색과 랭킹](https://github.com/ClickHouse/loom/blob/main/docs/concepts/retrieval-and-ranking.md)). 대부분의 항목은 배포 단위로 설정할 수 있고 요청 단위로 덮어쓸 수 있으므로, 변경 사항을 먼저 한 호출자에게만 A/B 테스트할 수 있습니다.

| 조정 항목                          | 설정 (요청 필드)                                                                                                                                                        | 트레이드오프                                                                                                           |
| ---------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------- |
| 유사도 게이트                      | `LOOM_ADAPTIVE_THRESHOLDS` (기본값 on)                                                                                                                                  | On: 부팅 시 임베더 자체의 코사인 분포에서 게이트를 도출합니다. Off: 아래의 스칼라 게이트를 직접 관리해야 합니다.        |
| 스칼라 게이트 (대체)               | `LOOM_MEMORY_SEARCH_MIN_SIMILARITY`, `LOOM_MEMORY_SEARCH_ABSTAIN_THRESHOLD`, `LOOM_MEMORY_SEARCH_HYDE_FALLBACK_THRESHOLD`, `LOOM_MEMORY_SEARCH_QUERY_REWRITE_THRESHOLD` | `text-embedding-3-small`에 맞춰 조정되어 있으며, 가짜 임베더, 프로브 실패, 적응형 기능이 꺼진 경우에 사용됩니다.      |
| 하한 미만 결과                     | `LOOM_MEMORY_SEARCH_FLOOR_MODE` (`floor_mode`)                                                                                                                          | `readmit`은 `top_k`를 채우고 `below_floor`로 태그하며, `ids_only`는 본문이 빈 항목으로 채우고, `drop`은 더 적게 반환합니다. |
| 후보 풀                            | `LOOM_MEMORY_SEARCH_POOL_MULTIPLIER_STANDARD`, `_RERANK`, `_DEEP`, 상한은 `LOOM_MEMORY_SEARCH_MAX_POOL_TOP_K`                                                           | 풀이 넓을수록 재현율과 리랭크 입력이 늘어나는 대신, 읽는 벡터 행과 리랭크 토큰 비용이 듭니다.                          |
| 리랭크 생략                        | `LOOM_LLM_RERANK_SKIP_ABOVE` (적응형 게이트에 프로브가 없을 때의 대체값)                                                                                                | 최상위 벡터 결과가 이미 충분히 강하면 리랭크 호출을 건너뜁니다.                                                        |
| 대체 캐스케이드                    | `LOOM_MEMORY_SEARCH_FALLBACK_MAX_ROUNDS`, `_MAX_WALL_MS`, `_MAX_LLM_CALLS`, `_MAX_VECTOR_QUERIES`                                                                       | 약한 첫 패스가 쓸 수 있는 재시도(쿼리 재작성, HyDE)의 한도를 정합니다.                                                 |
| 강화                               | `LOOM_MEMORY_SEARCH_REINFORCEMENT_WEIGHT` (`reinforcement_weight`), `_HALF_LIFE_DAYS`                                                                                   | 회상과 긍정적 결과가 행의 순위를 얼마나 끌어올릴지 정하며, 0이면 비활성화됩니다. 관련도보다 낮은 값으로 제한됩니다.    |
| 출처                               | `LOOM_MEMORY_SEARCH_TRUST_WEIGHT` (기본값 0)                                                                                                                            | 검증된 출처를 선호하는 제한된 항목입니다.                                                                              |
| 최신성                             | `LOOM_MEMORY_SEARCH_RECENCY_POOL_WEIGHT` (`recency_pool_weight`)                                                                                                        | 최신성 의도가 있는 쿼리에서 풀을 최근 행 쪽으로 다시 정렬하며, 0이면 꺼집니다.                                         |
| 코드 형태 쿼리의 어휘 레그         | `rrf_adaptive_lexical` (요청), `LOOM_MEMORY_RRF_CODE_SHAPE_LO` / `_HI`                                                                                                  | RRF에서 경로나 코드 형태의 쿼리에 대해 어휘 레그의 가중치를 낮춥니다.                                                  |
| 기본 모드                          | `LOOM_MEMORY_SEARCH_DEFAULT_MODE` (`search_mode`)                                                                                                                       | 호출자가 모드나 레시피를 지정하지 않았을 때 사용됩니다.                                                                |
| 키당 중복                          | `LOOM_MEMORY_SEARCH_PER_KEY_CAP`                                                                                                                                        | 하나의 키가 한 페이지에 기여할 수 있는 행 수입니다.                                                                    |

> **경고:** 한 임베더에 맞춰 조정한 임계값은 다음 임베더에서는 맞지 않습니다.
> `LOOM_ADAPTIVE_THRESHOLDS=false`이거나 가짜 임베더를 사용하는 경우, 임베더를 바꿀 때마다
> 벤치마크(`make bench-quick`, `make bench-quality-gate`)를 다시 실행하세요.
> `calibration.get`은 네임스페이스에 적용 중인 게이트를 어떤 티어가 만들었는지 보여 줍니다.

<a id="vector-reads"></a>

## 벡터 읽기

벡터 컬럼은 `Array(BFloat16)`입니다. `enable_quantized_codec`을 켤 수 있는 환경에서는 마이그레이터가 코덱을 적용한 상태로 컬럼을 생성합니다 ([ClickHouse](../architecture/clickhouse.md#gated-clickhouse-settings)).

| 조정 항목                  | 설정                                                                                              | 트레이드오프                                                                                                                                                                                                                                                                                                                                                           |
| -------------------------- | ------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 코드 우선 스캔             | `LOOM_VECTOR_SEARCH_QUANTIZED_CODES` (기본값: 켜짐), `LOOM_MEMORY_SEARCH_FACT_CODES_FIRST` (`auto`) | 양자화된 코드를 먼저 읽은 다음 전체 정밀도로 다시 점수를 계산합니다. `auto`는 `memory_embeddings`에 패치 파트가 있는 동안 이 기능을 끕니다. 이 경우 재점수 계산이 건너뛰어지기 때문입니다.                                                                                                                                                                              |
| 코드 오버페치              | `LOOM_VECTOR_SEARCH_QUANTIZED_FETCH_MULTIPLIER`, `LOOM_MEMORY_SEARCH_FACT_CODES_FETCH_MULTIPLIER` | 재점수 계산 전에 코드 거리 기준으로 남겨 두는 행 수입니다. 값이 낮을수록 비용은 줄지만 상위 결과가 바뀌는 경우가 더 잦아집니다.                                                                                                                                                                                                                                         |
| 팩트 구문당 스레드         | `LOOM_MEMORY_SEARCH_FACT_LEG_MAX_THREADS` (0 = 고정 안 함)                                        | 스레드를 고정하면 구문당 CPU와 최대 메모리 사용량이 제한되지만, 지연 시간이 다소 늘어납니다.                                                                                                                                                                                                                                                                            |
| 대량 쓰기의 피어 읽기      | `LOOM_MEMORY_BULK_PEER_READ` (기본값 `batched`, `per_fact`)                                       | `per_fact`는 `memory.set`처럼 대량 쓰기의 각 팩트에 대한 충돌 피어와 값 업데이트 피어를 코드 우선으로 읽습니다. 배치 피어 구문이 `MEMORY_LIMIT_EXCEEDED`로 실패하고(서버 로그의 `batched conflict candidates failed`) 팩트별 폴백도 실패하면 해당 항목이 검사 없이 기록되는 대규모 네임스페이스에 사용합니다. 거리가 같은 경우 피어가 달라질 수 있습니다. |

기존 테이블에 코덱이나 레이아웃을 적용하려면 `scripts/ops/vector_codec_rebuild.py`(트윈 테이블, 일관성 및 재현율 게이트, `EXCHANGE`. [단계](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#recommended-clickhouse-settings))를 사용하세요. 마이그레이션에서 `ALTER`를 사용해서는 안 됩니다.

<a id="clickhouse-connections-and-deadlines"></a>

## ClickHouse 연결과 데드라인

| 조절 항목                 | 설정                                                                                                                                                                                | 트레이드오프                                                                                                                                                                                                                                                   |
| ------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 풀 크기                   | `LOOM_CH_POOL_MAXSIZE` (기본값: 프로세스당 64)                                                                                                                                      | 예상되는 요청 동시성에 워커 수를 더하고, 배치 insert가 대기하는 동안 점유하는 연결 수까지 더해 크기를 정합니다. 드라이버의 기본값은 대량 쓰기에서 고갈됩니다.                                                                                                  |
| 배치 insert               | `LOOM_CH_BATCHED_INSERTS` (기본값: 켜짐), `LOOM_CH_BATCHED_INSERT_WAIT_MIN_MS` (10), `LOOM_CH_BATCHED_INSERT_WAIT_MAX_MS` (50), `LOOM_CH_BATCHED_INSERT_BACKGROUND_WAIT_MAX_MS` (200) | 동시에 들어온 insert는 하나의 파트를 공유하며, 각각 최대 윈도우 길이만큼의 지연이 생깁니다. 단독 insert는 최소 시간만큼 대기합니다. 대기 중인 insert는 각각 풀 연결을 하나씩 점유하므로 쓰기 빈도에 따라 풀이 커집니다 ([배치 insert](../architecture/clickhouse.md#batched-inserts)). |
| 풀 대기                   | `LOOM_CH_POOL_WAIT_MS` (기본값: 500)                                                                                                                                                | 이 시간을 넘으면 요청은 큐에 쌓이지 않고 503 `OVERLOADED`로 즉시 실패합니다. 0이면 제한이 없습니다.                                                                                                                                                            |
| Keep-alive                | `LOOM_CH_KEEPALIVE_TIMEOUT` (기본값: 5초)                                                                                                                                           | 로드 밸런서의 유휴 종료 시간보다 짧아야 Loom이 풀의 연결을 먼저 닫습니다.                                                                                                                                                                                      |
| 구문 데드라인             | `LOOM_CH_TIMEOUT_READ_S`, `LOOM_CH_TIMEOUT_WRITE_S`, `LOOM_CH_TIMEOUT_DDL_S`                                                                                                        | `max_execution_time`으로 적용되며, 풀 슬롯을 해제하는 클라이언트 측 제한으로도 적용됩니다. 0이면 해당 클래스에는 제한이 없습니다.                                                                                                                               |
| 트래픽 클래스별 우선순위  | `LOOM_CH_SETTINGS_BY_TRAFFIC_CLASS` (JSON)                                                                                                                                          | 클래스(`production`, `interactive`, `bench`, `background`)별 `priority`와 `max_threads`를 지정하여, 벤치와 워커가 프로덕션 읽기에 양보하도록 합니다.                                                                                                            |
| Read-your-writes          | `LOOM_CH_READ_YOUR_WRITES`, `LOOM_CH_READ_YOUR_WRITES_WINDOW_S`                                                                                                                     | 쓰기 이후의 읽기가 순차적 일관성을 갖습니다. 복제 서비스에서는 비용이 들고, 단일 노드에서는 아무 영향이 없습니다.                                                                                                                                              |
| 작업 예산                 | `LOOM_LATENCY_BUDGET_MULTIPLIER`                                                                                                                                                    | 모든 op의 `budget_p99_ms`를 배율로 조정합니다. ClickHouse까지 WAN 구간이 있으면 약 20이 필요합니다. 예산은 `budget.violated` 이벤트를 발생시킬 뿐 요청을 끊지는 않습니다.                                                                                       |

웹 처리 용량은 컨테이너당 `LOOM_WEB_WORKERS`에 컨테이너 수를 곱한 값입니다. 속도 제한기는 `loom.instances`에서 확인한 활성 레플리카 수로 제한값을 나누며, 그 수는 `LOOM_REPLICA_COUNT`보다 작아지지 않습니다 ([레플리카](https://github.com/ClickHouse/loom/blob/main/docs/operations/replicas.md#rate-limits)).

<a id="providers"></a>

## 프로바이더

| 조절 항목                    | 설정                                                                                                                              | 트레이드오프                                                                                                                                                                                              |
| ---------------------------- | --------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 쿼리 임베딩 제한             | `LOOM_MEMORY_SEARCH_EMBED_BOUND_S`, `LOOM_MEMORY_SEARCH_EMBED_BOUND_RETRIES`                                                      | 쿼리 임베딩의 시도당 제한 시간입니다. 재시도 후에도 실패하면 검색은 503 `EMBED_TIMEOUT`으로 응답합니다.                                                                                                    |
| 쿼리 철자                    | `LOOM_MEMORY_SEARCH_QUERY_SPELLING_MAX_WORDS`                                                                                     | 이 단어 수 이하의 쿼리는 임베딩과 나란히 네임스페이스를 한 번 읽어, 소문자 단어를 코퍼스의 철자로 임베딩합니다. 40k 행 네임스페이스 기준 100–300 ms가 걸리며 1분간 재사용됩니다. `0` = 끔.                |
| 쿼리 임베딩 캐시             | `LOOM_EMBEDDING_CACHE_SIZE`, `LOOM_EMBEDDING_CACHE_TTL_S`, `LOOM_EMBEDDING_CACHE_SHARED`, `LOOM_EMBEDDING_CACHE_SHARED_MAX_CHARS` | 공유 `loom.embedding_cache` 앞단에 프로세스별 LRU를 둡니다. 반복되는 쿼리는 프로바이더 호출을 건너뜁니다.                                                                                                 |
| 클래스별 LLM 응답 캐시       | `LOOM_LLM_POLICY_BY_TRAFFIC_CLASS` (JSON)                                                                                         | LLM 응답을 캐시하며(`bench`는 기본값으로 켜짐), 특정 클래스가 강력한 티어나 중간 티어를 사용하지 못하게 막을 수 있습니다.                                                                                   |
| 폴백 모델                    | `LOOM_LLM_FALLBACK_MODEL`                                                                                                         | 재시도 후에도 기본 모델을 사용할 수 없을 때 op를 실패시키는 대신 이 모델이 응답합니다.                                                                                                                    |
| 지출 한도                    | `LOOM_DEFAULT_DAILY_COST_CAP_USD` (네임스페이스별 `daily_cost_cap_usd`)                                                           | 한도를 넘으면 LLM 기반 워커는 해당 네임스페이스를 건너뛰고 `memory.ask`는 `budget_capped`로 응답을 보류합니다. 일반 읽기와 쓰기는 계속됩니다.                                                              |

<a id="embedder-choice"></a>

### 임베더 선택

| 임베더 (`LOOM_EMBEDDING_MODEL`)                              | 트레이드오프                                                                                    |
| ------------------------------------------------------------ | ----------------------------------------------------------------------------------------------- |
| `text-embedding-3-large`, `gemini-embedding-001`             | Loom의 3072차원 기본값입니다. 품질과 비용은 여전히 사용자의 데이터로 측정해야 합니다.           |
| `text-embedding-3-small`                                     | 더 좁은 1536차원 벡터 옵션입니다. 차원을 바꾸려면 다시 임베딩해야 합니다.                       |
| `local`, `local:bge-small`, `local:bge-base`, `local:minilm` | 프로세스 내에서 CPU로 임베딩하며 프로바이더 호출이나 키가 필요 없습니다. 품질을 낮추는 대신 지연 시간과 비용을 줄입니다. |
| `fake-ngram-3072`                                            | 표준 차원에서 문자 n-gram을 사용하며 테스트와 연동 확인용입니다. 재현율 수치는 의미가 없습니다.  |

임베더를 바꾸면 벡터 공간이 바뀝니다. `LOOM_EMBEDDING_DIM`을 그에 맞게 설정하고 `loom reembed`를 실행한 뒤, 적응형 임계값을 켜 두었다면 부팅 프로브가 다시 보정하도록 두세요. 전후로 `make bench-quick`을 실행하세요. 추출 프롬프트가 맞춰져 있는 `LOOM_LLM_MODEL`도 마찬가지입니다.

<a id="workers-and-batches"></a>

## 워커와 배치

모든 워커에는 `*_ENABLED` 스위치와 `*_INTERVAL_SECONDS` 주기가 있으며, 많은 워커에는 아무것도 찾지 못한 패스 이후에 대기하는 `*_IDLE_INTERVAL_SECONDS`도 있습니다
([워커 레퍼런스](https://github.com/ClickHouse/loom/blob/main/docs/reference/workers.md)). 간격을 짧게 하면 백그라운드 작업의 지연은 줄어들지만 스캔이 늘어납니다. 유휴 간격은 빈 큐의 비용을 낮게 유지합니다.

| 워커                  | 조정 항목                                                                                                                                                                                                                            |
| --------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `embedding_worker`    | `LOOM_EMBEDDING_WORKER_INTERVAL_SECONDS`, `_IDLE_INTERVAL_SECONDS`; `LOOM_EMBEDDING_WORKER_ORPHAN_FULL_SCAN_SECONDS` / `_ORPHAN_RECENT_SECONDS`는 고아 항목 anti-join의 범위를 제한합니다.                                            |
| `extraction_queue`    | `LOOM_EXTRACTION_QUEUE_INTERVAL_SECONDS`, `_MAX_BATCHES_PER_PASS`, `_NAMESPACE_CONCURRENCY`, `_LEASE_SECONDS`; `LOOM_EXTRACTION_DEFER_AGENTS`는 나열된 에이전트의 `set_from_messages`를 미루어 추출이 끝나기 전에 요청이 반환되도록 합니다。 |
| `memory_expiry_sweep` | `LOOM_MEMORY_EXPIRY_SWEEP_BATCH_SIZE`.                                                                                                                                                                                               |
| `task_lease_sweep`    | `LOOM_TASK_LEASE_SWEEP_INTERVAL_SECONDS`, `_IDLE_INTERVAL_SECONDS`.                                                                                                                                                                  |

워커는 `background` 트래픽 클래스에서 실행되므로 `LOOM_CH_SETTINGS_BY_TRAFFIC_CLASS`로 ClickHouse 우선순위와 스레드 수도 제한할 수 있습니다. 워커 프로세스는 정확히 하나만 실행하십시오. LLM 기반 워커를 하나 더 실행하면 스캔과 비용이 두 배가 됩니다. 코드 플레인의 수집 워커와 컴파일러 티어 워커에는 별도의 조정 항목이 있습니다([코드 인덱스 운영](https://github.com/ClickHouse/loom/blob/main/docs/operations/code-index-operations.md)).

<a id="client-side-levers"></a>

## 클라이언트 측 조절 방법

가장 큰 효과 중 일부는 요청 자체에서 나옵니다.

- 더 적은 본문 요청하기: `memory.search`의 `bodies`와 `body_budget_tokens`, `context.pack`과 `code.search`의 `token_budget`.
- 단계마다 검색하는 대신 작업당 한 번만 컨텍스트 로드하기(`context.pack`).
- 호출마다 `retrieval_budget`을 사용해 더 저렴하거나 더 깊은 검색 프로필 선택하기.
- 멱등성 있는 op는 `Idempotency-Key` 헤더와 함께 재시도하기(SDK가 헤더를 생성합니다). 재전송하면 op를 다시 실행하지 않고 저장된 응답을 반환합니다.

<a id="related"></a>

## 관련 문서

- [장애 및 복구](../architecture/failure-and-recovery.md): 용량과 확장 대상
- [모니터링](https://github.com/ClickHouse/loom/blob/main/docs/operations/monitoring.md)
- [벤치마크 실행](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/running.md)
- [디버깅](https://github.com/ClickHouse/loom/blob/main/docs/guides/debugging.md)
