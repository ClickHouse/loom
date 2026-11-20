<!-- i18n: source=docs/operations/maintenance.md blob=5df99557fe7d3a4299c8ea5cc297b48e586731cc model=claude-sonnet-5-5 -->
> 이 페이지는 [영어 원문](https://github.com/ClickHouse/loom/blob/main/docs/operations/maintenance.md)을 기계 번역한 것이며, 영어 원문이 기준입니다.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/operations/maintenance.md), which is authoritative.

<a id="maintenance"></a>

# 유지보수

여기에 나오는 모든 명령은 `loom` 하위 명령이며, 배포 환경의 환경 변수가 설정된 컨테이너 안에서 실행합니다(`docker exec loom-server loom …`, 분리 배포에서는 `loom-workers`). 모든 플래그는 [CLI 레퍼런스](https://github.com/ClickHouse/loom/blob/main/docs/reference/cli.md)에서 확인할 수 있습니다. 코드 인덱스에는 별도의 운영 문서가 있습니다: [코드 인덱스 운영](https://github.com/ClickHouse/loom/blob/main/docs/operations/code-index-operations.md).

<a id="before-you-run-a-write"></a>

## 쓰기를 실행하기 전에

아래 명령 대부분은 **기본적으로 드라이 런(dry run)**으로 동작합니다. 출력을 확인하고 `--org`와 `--namespace`로 범위를 지정한 다음, 쓰기 플래그(`--apply`, 삭제의 경우 `--erase`)를 전달하세요. 각 표에 해당 플래그와 예외가 명시되어 있습니다. 변경(`ALTER … UPDATE`)을 제출하는 명령은 ClickHouse가 해당 변경을 적용하기 전에 반환됩니다. `system.mutations`를 확인하세요.

<a id="re-embed-and-re-extract"></a>

## 다시 임베딩 및 다시 추출

- **`loom reembed`** 는 `embedding_model` 이 구성된 임베더와 다른 메모리, 엔터티, 대화 벡터를 다시 임베딩합니다. Loom 은 스스로 다시 임베딩하지 않으므로 `LOOM_EMBEDDING_MODEL` (및 `LOOM_EMBEDDING_DIM`)을 변경한 뒤에 실행하세요. `--org` / `--namespace` 로 범위를 지정하고, `--dry-run` 은 행 수만 셉니다. `--delete-old` 는 이후에 이전 모델의 행을 제거합니다. `--key-only` 는 각 라이브 행의 키 축 벡터만 다시 만듭니다. 단, 가져오기 키(`import:` 와 다이제스트)는 키 축 벡터가 없으므로 예외입니다. 벡터가 없는 행은 건너뛰고, 이전 빌드가 임베딩해 둔 행은 벡터를 비웁니다. `--in-database <named collection>` 은 Python 루프 대신 ClickHouse 안에서 `aiEmbed` (ClickHouse 26.7 이상)로 테이블마다 `INSERT … SELECT` 한 번씩으로 임베딩을 실행합니다. 고객 또는 아이덴티티 헤더가 설정되어 있거나 추론 게이트웨이가 필수인 경우에는 거부됩니다. ClickHouse 자체의 호출은 고객이나 아이덴티티를 지정하지 않으며 게이트웨이의 호스트 검사도 건너뛰기 때문입니다. Python 경로는 정렬 키 순서로 `--batch-size` 행씩 읽고 페이지마다 `resume:` 토큰을 출력하며, `--resume <token>` 으로 중단된 실행을 그 지점부터 이어갑니다. Python 경로는 텍스트가 높음(high)으로 판정되는 대화 턴을 임베더에 보내지 않습니다. 그 행은 검색이 읽지 않는 이전 모델에 남아 있다가 `--delete-old` 가 제거합니다. 메모리 행의 벡터는 민감도 레이블을 따릅니다. `--in-database` 는 텍스트를 검사할 수 없어 그런 턴도 다시 임베딩하지만, 검색은 여전히 그 턴을 제외합니다
  ([민감도](https://github.com/ClickHouse/loom/blob/main/docs/architecture/security.md#sensitivity)).
  - Python 경로는 각 페이지를 삽입하기 전에 해당 페이지의 행을 키로 다시 읽습니다. 페이지를 읽은 이후 삭제되었거나, 톰스톤 처리되었거나, 텍스트나 에이전트 또는 사용자가 바뀐 행은 건너뜁니다. 그 외의 모든 행은 두 번째 읽기에서 확인된 다른 컬럼 값 그대로 기록됩니다.
  - 건너뛴 행이 다른 텍스트나 아이덴티티로 여전히 저장된 이전 모델 행이면, `--delete-old` 는 해당 org 의 이전 모델 행을 다음 실행을 위해 남겨 둡니다. 다음 실행은 그 org 의 이전 모델 행을 모두 다시 임베딩합니다(`--org` 로 범위를 지정하세요). `resume:` 토큰에는 지금까지 남겨 둔 org 가 담겨 있으며, 실행은 삭제하기 전에 그 org 를 출력합니다.
  - 그 읽기와 삽입 사이에 발생한 삭제, 편집(redaction) 또는 삭제 요청(erasure)이나 아직 행에 반영되지 않은 뮤테이션은 행을 새 벡터와 함께 라이브 상태로 남길 수 있습니다. 이런 편집은 실행이 끝난 뒤 다시 수행하고, 이런 대화 턴이나 스레드는 다시 삭제하세요. 고아 벡터 정리 작업은 삭제된 메모리의 남은 벡터를 톰스톤 처리하며, 삭제 요청의 검증 단계는 해당 행을 찾아 다시 시도합니다. `--in-database` 는 문이 시작될 때 각 테이블을 한 번만 읽으므로, 편집이나 삭제 요청이 없을 때 실행하세요.
- **다른 차원의 모델.** ClickHouse 26.8 이상에서 새로 설치하면 `enable_quantized_codec` 을 켤 수 있는 곳에서 벡터 컬럼이 코딩(`Quantized`)되며([ClickHouse](../architecture/clickhouse.md#gated-clickhouse-settings)), 코딩된 컬럼은 생성 시의 차원을 가진 벡터만 받습니다. 다른 차원(8의 배수)의 모델로 바꾸려면 Settings → Models & keys 에서 변경하세요. 그러면 새 차원으로 벡터 테이블을 다시 만듭니다. `loom reembed` 는 테이블이 받지 않는 차원을 거부하며, `LOOM_EMBEDDING_DIM` 만 수정하면 모든 벡터 쓰기가 503 `CODED_WIDTH_MISMATCH` 로 응답하고 `/v1/health` 는 `coded_width_mismatch` 를 보고합니다.
- **`loom rechunk --org ORG --namespace NS`** 는 저장된 콘텐츠로 모든 라이브 문서에 `knowledge.upsert` 를 다시 실행하여 문서가 현재의 청킹, 청크 컨텍스트, 벡터를 반영하게 합니다(문서 하나만 처리하려면 `--source-uri`). `--apply` 를 지정하기 전까지는 드라이 런입니다.
- **`loom lexical-stats-rebuild --org ORG --namespace NS`** 는 라이브 행에서 BM25 문서 빈도를 다시 계산합니다. 용어 빈도 뷰는 삽입만 보기 때문에, 대체(supersession), 톰스톤, 대량 삭제가 일어나면 이 명령을 실행하기 전까지 오래된 어휘가 남습니다(`--no-memory` / `--no-knowledge` 로 한쪽을 건너뜁니다). 즉시 기록합니다.
- **`loom reextract --org ORG --namespace SRC --into DST`** 는 한 네임스페이스에 저장된 `set_from_messages` 대화록을 실행 중인 서버의 현재 추출기로 다시 처리하여 다른 네임스페이스에 넣습니다(`--token` 은 `DST` 의 writer 토큰이며, 없으면 `$LOOM_API_KEY`, 그다음 `~/.loom/token` 을 사용합니다). 원본은 읽기만 하므로 읽기 쪽을 전환하기 전에 두 네임스페이스를 비교할 수 있습니다. `--dry-run` 은 세션 목록을 보여 주고, `--limit-sessions N` 은 일부 샘플만 다시 처리합니다.
- **`loom recalibrate`** 는 임베더를 다시 탐색하고 적응형 유사도 임계값을 즉시 다시 계산합니다. 그렇지 않으면 `recalibration` 워커가 매주 수행합니다.

새 벡터 공간은 모든 유사도 점수를 바꾸기 때문에 다시 임베딩은 수동으로 합니다. 직접 실행하면 전환, 비용, 검색 품질의 기준선 재설정이 운영자의 한 단계로 묶입니다.

<a id="backfills-and-repairs"></a>

## 백필과 복구

| 명령                             | 동작                                                                                                                                                      | 쓰기 플래그 |
| -------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------- |
| `loom integrity-repair`          | 무결성 게이지와 그 근거가 되는 행을 출력합니다. 검색에서 보이도록 `dim0_with_vector` 행을 다시 내보냅니다(드리프트 행과 `valid_to` 행은 목록으로만 표시합니다) | `--apply`   |
| `loom sensitivity-sweep`         | 민감도 탐지기가 레이블을 붙일 라이브 상태의 레이블 없는 행의 수를 셉니다. 쓰기를 실행하면 레이블을 붙이고, `high` 행의 임베딩 행은 삭제 표시(tombstone)합니다 | `--apply`   |
| `loom reconcile-status`          | 상태가 `loom.memory`와 달라진 `memory_embeddings` 행을 다시 내보냅니다                                                                                    | `--apply`   |
| `loom demote-activity-narration` | 지속되는 결과 없이 도구 활동만 서술하는 추출된 사실을 찾아 `memory.apply`를 통해 강등하거나 레이블을 붙입니다. 삭제는 하지 않습니다                       | `--apply`   |
| `loom namespaces-adopt`          | 라이브 메모리는 있지만 레지스트리 행이 없는 네임스페이스에 소유자 행을 부여합니다                                                                         | `--apply`   |
| `loom namespaces-gc`             | 접두사로 유휴 상태의 벤치마크 네임스페이스를 찾거나(`--worker-pass`를 쓰면 `namespace_gc` 패스를 한 번 실행), 해당 네임스페이스의 삭제를 제출합니다        | `--erase`   |
| `loom audit-namespace`           | 네임스페이스 사용 방식(정확한 키 읽기 대비 검색, 크기가 과도하거나 이중 인코딩된 값, 버전 변동)을 보고합니다. 읽기 전용이며, 심각한 문제가 있으면 종료 코드 1을 반환합니다 | 읽기 전용   |

이 복구 명령들이 다루는 무결성 게이지는
[모니터링](https://github.com/ClickHouse/loom/blob/main/docs/operations/monitoring.md#integrity-invariants)에 나와 있습니다. 행의 형태가 잘못된 네임스페이스(문서가 메모리로 저장되었거나 리스가 사실로 저장된 경우)를 정리하는 방법은
[네임스페이스 최적화](https://github.com/ClickHouse/loom/blob/main/docs/guides/optimize-namespace.md)에서 단계별로 안내합니다.

<a id="memory-ingest-drops"></a>

## 메모리 수집 드롭

수집 파이프라인이 데이터를 버리거나, 잘라내거나, 처리를 포기하는 모든 지점에서 드롭이 기록됩니다(`server/memory/app/drops.py`, `record_drop`). 드롭은 프로세스 내에 버퍼링되고, `loom.memory_drops`(30일 보관)로 플러시되며, 카운터 `loom.memory.drops{stage, reason}`에도 반영됩니다. 드롭 기록은 예외를 발생시키지 않으며, 드롭을 유발한 쓰기를 막지도 않습니다.

```bash
docker exec loom-server loom drops --namespace NS [--org ORG] [--hours 24]
```

이 명령은 해당 기간의 `stage`와 `reason`별 건수를 각 조합이 마지막으로 관측된 시각과 함께 출력합니다. `SELECT * FROM loom.memory_drops WHERE namespace = '…' ORDER BY ts DESC`는 개별 항목을 보여줍니다(`item`에는 키 또는 필드 이름이 들어가며, 메트릭 레이블로는 사용되지 않습니다).
stage에는 `extract.parse`, `extract.judge`, `extract.truncate`, `write`, `entity.extract`, `consolidate.merge`, `records.field`, `chunk.split`, `conflict.deferred`가 있습니다.

완전히 실패한 추출은 드롭이 아닙니다. `set_from_messages`는 502 `EXTRACTION_FAILED`로 응답하며, 부분 실패는 응답의 `pipeline_errors`에 담겨 반환됩니다.

<a id="entity-graph-maintenance"></a>

## 엔티티 그래프 유지 관리

`loom.entity_edges`를 기록하는 writer는 어휘적 불용어(대명사, 감탄사, 경동사, 축약형 조각, 두 글자 토큰)를 그래프에서 걸러냅니다.

메모리보다 그래프가 더 오래 남아 있는 네임스페이스는 `loom erasure-re-verify --org ORG --orphan-graph`로 조회하고 `--erase`로 삭제합니다.

<a id="erasure"></a>

## 삭제

| 호출                                                                            | 효과                                                                                                                                       | 사용 목적                                                            |
| ------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------ | -------------------------------------------------------------------- |
| `memory.clear` (`ADMIN`)                                                        | 네임스페이스 내 하나의 `user_id` 또는 `agent`의 메모리에 삭제 표시(tombstone)를 합니다. `confirm_subject`에 범위 값을 동일하게 입력해야 합니다 | 정리, 테스트 초기화; `memory.restore`로 되돌릴 수 있음               |
| `erasure.request` (`scope_type`: `user_id`, `namespace` 또는 `namespace_list`) | 대상의 콘텐츠를 담을 수 있는 모든 Loom 테이블과 설정된 외부 백엔드(Langfuse, ClickStack)에 걸쳐 실행되는, 재개 가능한 saga                 | 사용자 데이터 삭제(감사 추적은 유지됨); 네임스페이스 폐기            |

saga는 각 단계를 `loom.erasure_requests`(`ReplacingMergeTree`, 행은 89일 동안 읽히고 90일에 삭제됨)에 체크포인트로 기록합니다. 단계를 마친 뒤에는 읽기 경로가 접근하는 모든 테이블에 대해 잔여 데이터 조사(residue census)를 실행합니다. 범위 안에 남은 행이 하나라도 있으면 saga는 실패하며, 마지막 행에는 `residue_count`, `residue`, `verified_at`, `census`가 담깁니다. 삭제를 확인하지 못하는 백엔드가 있으면 saga는 실패 처리(fail closed)됩니다. `erasure_recovery` 워커는 `max_attempts`까지 멱등적으로 재시도하고, 그 후에는 `failed` 상태로 남겨 둡니다. `purge: true`는 삭제 표시 대신 완전히 삭제합니다(임시 네임스페이스용이며, 복원 기간이 없습니다). `external: false`는 외부 백엔드를 건너뛰며 `namespace_list`에서만 허용됩니다. 삭제가 보장하는 것과 보장하지 않는 것은 [보존 및 삭제](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/retention-and-erasure.md)에 설명되어 있습니다.

```sql
SELECT id, scope_type, scope_value, status, current_step, attempts, failure_reason, residue_count
FROM loom.erasure_requests FINAL
WHERE status != 'completed' ORDER BY requested_at DESC LIMIT 20;
```

`loom erasure-re-verify --org ORG`는 실패한 각 saga의 마지막 실패 사유와 해당 범위에 대한 새 잔여 데이터 조사 결과를 읽기 전용으로 출력합니다(saga 하나만 보려면 `--id`). 사용자 saga는 요청 시 사용한 `purge` 값으로 조사합니다. `--resume`은 실패한 saga를 같은 방식으로 체크포인트부터 다시 실행하며, 복구 워커의 시도 횟수 한도를 넘어서도 실행합니다. 마지막 조사에서 행이 발견된 테이블을 담당하는 단계는 체크포인트에 완료로 기록되어 있어도 다시 실행됩니다. `--skip-external`(`--resume`과 함께 사용하며, 실패한 `namespace_list` saga에만 적용되고 `--completed-since`와 함께 쓰면 거부됨)은 외부 백엔드가 계속 실패할 때 Loom 테이블만 먼저 마무리합니다. `--completed-since <ISO timestamp>`는 실패한 saga 대신, 잔여 데이터 조사가 해당 시각 이후에 실행된 완료된 saga를 대상으로 합니다(타임스탬프에 오프셋이 없으면 UTC). 롤아웃 후 이전 빌드가 실행한 삭제를 확인할 때 사용합니다. 이 경우 saga 범위 안의 고아 행 링크(메모리에 살아 있는 행이 없고 saga의 조사 이후 기록된 행도 없는 `entity_triple_rows`와 `entity_edge_rows`의 행)만 집계하고, `--resume`을 함께 쓰면 그것만 삭제합니다. saga 단계는 다시 실행하지 않으므로, 삭제 이후에 기록된 데이터는 `memory.delete`가 삭제 표시를 한 뒤에도 남습니다. saga를 복구할 수 없으면 같은 `erasure.request`를 다시 실행하십시오. 이 작업은 멱등적입니다.

<a id="clickstack-erasure-index"></a>

### ClickStack 삭제 인덱스

ClickStack 단계는 속성 값이 일치하는 OTel 행을 삭제합니다. ClickStack 스키마는 모든 OTel 테이블의 속성 맵에 bloom filter 인덱스를 두며, `schemas/otel/clickstack_otel_tables.sql`은 이 인덱스와 함께 테이블을 생성합니다. 다른 설치 도구가 `mapValues(Attributes)` 없이 만든 `otel_metrics_*` 테이블은 삭제할 때마다 전체를 스캔하게 됩니다. `loom doctor`와 `/v1/health`는 누락된 인덱스를 `otel_erasure_index`로 보고합니다. `CREATE TABLE IF NOT EXISTS`는 기존 테이블에 인덱스를 추가하지 않으므로, 테이블마다 한 번씩 직접 추가해야 합니다.

```sql
ALTER TABLE default.otel_metrics_histogram
  ADD INDEX IF NOT EXISTS idx_attr_value mapValues(Attributes) TYPE bloom_filter(0.01) GRANULARITY 1;
ALTER TABLE default.otel_metrics_histogram MATERIALIZE INDEX idx_attr_value;
-- repeat for otel_metrics_sum and otel_metrics_gauge (and the other metrics tables if populated)
```

ClickHouse Cloud에서는 `ADD INDEX` 직후에 실행한 `MATERIALIZE INDEX`가 아무것도 빌드하지 않을 수 있습니다. 해당 테이블의 `system.data_skipping_indices`에서 `data_compressed_bytes`를 확인하고, 값이 0이면 `MATERIALIZE`를 다시 실행하십시오.

<a id="retention"></a>

## 보존

Loom은 작성자가 삭제하기 전까지 모든 행을 보관합니다. 기본적으로 기간에 따라 삭제되는 데이터는 없으며, 보존 게이지(`loom.retention.*`)는 삭제 후보의 수만 셉니다.

- **테이블별.** `audit_log`, `agent_events`, `memory`, `memory_embeddings`, `conversations`, `chunks`, `documents`에는 `LOOM_RETENTION_<TABLE>_DAYS=N`을 사용합니다.
- **네임스페이스별.** 네임스페이스 등록(`namespace.register`)의 `retention_days`를 사용합니다. 관리되는 테이블의 TTL은 이 값을 설정한 모든 네임스페이스에 대한 하나의 `multiIf`가 됩니다.
- 워커의 리더가 부팅 시 `MODIFY TTL`을 적용합니다. 그 이후에는 `retention_reconcile` 워커만 테이블 TTL을 기록하므로, 정책 변경은 `LOOM_RETENTION_RECONCILE_INTERVAL_SECONDS` 안에 테이블에 반영되며, 등록이 한꺼번에 몰려도 테이블을 등록마다 한 번씩 다시 쓰지 않습니다.

핫/웜 티어링은 ClickHouse 스토리지 정책을 사용합니다. `LOOM_TIERING_<TABLE>_WARM_DAYS=W`와 함께, `storage_policies`에 있는 볼륨의 이름인 `LOOM_TIERING_WARM_VOLUME_NAME`을 설정합니다.

일부 운영용 테이블은 메모리가 아니라 진단용이므로 고정된 자체 TTL을 가집니다. `loom.memory_drops`(30일), `loom.worker_status`(30일), `loom.erasure_requests`(90일, 읽기는 89일에서 멈춥니다)가 그렇습니다. 모든 테이블의 TTL은 [스키마 레퍼런스](https://github.com/ClickHouse/loom/blob/main/docs/reference/schema.md)에서 확인할 수 있습니다.

<a id="compaction-and-optimize"></a>

## Compaction과 OPTIMIZE

`loom.memory`와 `loom.shared`는 `ReplacingMergeTree(version)` 테이블입니다. 업데이트, 툼스톤, 복원은 모두 새 버전을 기록하고, 백그라운드 머지가 이 버전들을 하나로 합칩니다. 읽기 쿼리는 `FINAL`을 사용하므로 합쳐지지 않은 버전은 정확성이 아니라 읽기 시간에 비용을 줍니다. `loom.memory`에서 `count() / uniqExact(memory_key)` 비율이 크면 합쳐지지 않은 버전이 많다는 뜻입니다. 대량 삭제 후에는 머지가 따라잡도록 기다리거나, 부하가 낮은 시간에 `OPTIMIZE TABLE loom.memory FINAL`을 실행하세요.

코드 인덱스의 벡터 테이블에는 머지하기 전에 먼저 측정하는 명령이 있습니다.

```bash
docker exec loom-workers loom code compact                     # measure copies per key
docker exec loom-workers loom code compact --table all --apply # OPTIMIZE … FINAL where the ratio is reached
```

이 명령은 키당 물리적 복사본 수가 `--min-ratio`(기본값 `LOOM_CODE_COMPACT_MIN_RATIO`, 1.5)에 도달하면 코드 벡터 테이블(기본값 `code_chunks_prefix`, 그 외 `code_chunks`, `all`, 또는 `LOOM_CODE_VECTOR_TABLES`에 있는 테이블)에 `OPTIMIZE … FINAL`을 실행하고, 머지가 끝나기를 기다립니다(`--timeout-s`). `code_chunks`는 코드 비중이 큰 배포에서 가장 큰 테이블이므로, 사전에 공지한 시간대에 compaction하세요. 대량 처리 작업은 `LOOM_CODE_COMPACT_AFTER_BULK_ROWS` 행 이후 스스로 compaction합니다.

**테이블의 정렬 키, 파티션 키 또는 벡터 코덱을 변경하는 일**은 `ALTER`가 아니라 재구축으로 처리합니다. `scripts/ops/table_rebuild.py`와 `scripts/ops/vector_codec_rebuild.py`는 트윈 테이블로 복사하고, 검사한 뒤 `EXCHANGE TABLES`로 교체하며, 이전 테이블은 `--rollback`을 위해 보관합니다. 두 스크립트 모두 계획을 출력하며 `--yes` 없이는 아무것도 변경하지 않습니다. 정렬 키와 파티션 키 절차는 [마이그레이션](https://github.com/ClickHouse/loom/blob/main/docs/development/migrations.md#changing-a-sort-key-or-a-partition-key)에, 벡터 코덱 절차는 [권장 설정](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#recommended-clickhouse-settings)에 있습니다.

`vector_codec_rebuild.py`는 모든 writer와 삭제 작업을 중지하고 처리 중인 쓰기를 모두 비운 유지보수 시간대가 필요합니다. 이 스크립트는 라이브 테이블을 복구하지 않습니다. 복사하기 전에, 원본 테이블에서 mutation이 아직 실행 중이거나 최근 1분 이내에 생성되었으면 실행을 거부합니다. 이 1분은 레플리카 간 시계 차이를 감안한 값이며, 교체 전 게이트도 이를 포함해 계산합니다. 마지막 mutation이 끝나고 1분 뒤에 다시 실행하세요. `--reuse`를 사용하면 재사용하는 트윈이 생성된 이후의 모든 mutation을 완료된 것까지 포함해 셉니다. 그런 mutation이 있으면 트윈이 오래된 것이 되므로, `--restart --yes`로 다시 실행해 처음부터 복사해야 합니다. 또한 `--reuse`는 이름이 지정된 컬럼, granularity, 정렬 키가 이번 실행이 만들 트윈과 다른 트윈도 거부합니다(다른 `--method`, `--index-granularity`, `--chain`, 또는 다른 도구가 만든 트윈). 게이트는 저장된 벡터와 코드를 읽으므로 이 차이를 알아차리지 못하기 때문입니다. 빈 테이블도 재구축됩니다. 이때 폭은 아래 폭 항목이 정하는 값(코드화된 컬럼 자체의 폭, 아니면 `LOOM_EMBEDDING_DIM`)을 따르며, recall 게이트는 빈 테이블에서 조사할 행이 없습니다. 이 스크립트의 ClickHouse 사용자에게는 테이블에 대한 `SYSTEM FLUSH ASYNC INSERT QUEUE`와 `SYSTEM SYNC REPLICA` 권한이 필요하고, `ReplicatedMergeTree`에서는 `system.zookeeper`와 `system.replication_queue`에 대한 `SELECT`(`loom ch-setup`이 부여하지 않음), 그리고 트윈 `loom.<table>_rebuild`에 대한 `SYSTEM SYNC REPLICA`(게이트가 읽기 전에 동기화함)와, 교체가 만드는 롤백 복사본 `loom.<table>_pre_rebuild`에 대한 같은 권한(교체 후 검사와 `--drop-pre-rebuild`가 동기화함)도 필요합니다. 사전 점검은 앞의 두 권한을 한 번 실행해 보고, 하나라도 실패하면 오류를 명시하며 거부합니다(종료 코드 3). 나머지는 해당 테이블이 존재해야만 실행할 수 있으며, 트윈 동기화가 거부되면 게이트가 실패합니다(종료 코드 5).
`--dry-run`도 앞의 두 가지를 실행하므로, 큐에 대기 중인 행을 테이블에 반영하고 뒤처진 레플리카에서는 동기화를 기다리지만, 그 외에는 아무것도 쓰지 않습니다. 게이트가 실패하면(종료 코드 5) 쓰기와 삭제를 중지하고 `--restart`로 다시 실행하세요. 단, 테이블에 행은 있지만 조사할 실제 벡터가 없는 경우는 예외입니다. 다시 실행해도 같은 방식으로 실패하므로, 실행 결과의 `next`가 안내하는 대로 트윈을 삭제하고 테이블에 임베딩이 채워질 때까지 건너뛰세요. 폭 거부(종료 코드 3)는 아래의 재임베딩이 필요합니다.

재구축이 끝나거나 중단되고 모든 `<table>_rebuild` 트윈과 `<table>_pre_rebuild` 롤백 복사본이 제거될 때까지 writer와 삭제 작업을 중지된 상태로 유지하세요. 교체에 성공하면 로그와 요약 줄의 `next`에 이 안내가 다음 단계로 출력됩니다. 명령을 알려주는 각 다음 단계에는 이번 실행의 `--method`, `--chain`, `--index-granularity`, `--gate-fetch-multiplier`, `--probes`, `--require-patch-free`, `--cluster`, `--statement-timeout-s`, 그리고 기본값이 아닐 때 `--abort-file`(트윈의 기록이 그 옆에 있음)이 포함되므로, 출력된 그대로 실행하면 됩니다. 검증 또는 롤백 후 롤백 복사본을 제거하려면 `--drop-pre-rebuild --yes`를 사용하세요. 버려진 트윈은 해당 테이블 줄의 `--dry-run` 결과가 `not swapped`로 시작하는 다음 단계를 출력할 때에만(검사용으로 남긴 트윈이거나, 교체되지 않고 남은 잔여물일 때) `DROP TABLE loom.<table>_rebuild SYNC`로 제거하세요. 다음 단계가 `RENAME TABLE loom.<table>_rebuild TO loom.<table>_pre_rebuild`를 안내하거나 두 테이블을 비교하라고 하면, 먼저 그렇게 하고 아무것도 삭제하지 마세요. 이름 변경 단계가 나온다는 것은 교환은 실행되었지만 이름 변경은 실행되지 않았다는 뜻이며, 이때 `<table>_rebuild`가 이전 테이블입니다. 이름 변경을 실행하기 전까지는 해당 테이블 줄의 모든 실행(`--yes`, `--reuse`, `--restart`를 붙인 같은 줄 포함)과 `--drop-pre-rebuild`가 종료 코드 3으로 거부됩니다. 도구는 이를 실행 플래그가 아니라 두 테이블의 모든 벡터 컬럼에서 읽어 판단하므로, 다른 `--method`, `--chain`, `--index-granularity`, `--columns`로 다시 실행해도 거부됩니다. 코드화된 벡터 컬럼이 있는 라이브 테이블 아래에서, 잔여물로 확인되지 않은 트윈은 이전 테이블일 수 있기 때문입니다. 라이브 테이블에서 코드화된 각 컬럼이 트윈에서는 일반 컬럼이면, 도구가 이름 변경 명령을 알려줍니다. 트윈에서도 하나가 코드화되어 있으면(`--require-patch-free` 실행이거나 코드화된 테이블의 재구축), 코덱만으로는 교환이 실행되었는지 알 수 없습니다. 이 경우 도구는 거부하고, 이름 변경도 삭제도 하지 않으며, 둘 중 하나를 수동으로 삭제하기 전에 두 테이블의 행을 비교하라고 안내합니다. 정렬 키나 파티션 키가 라이브 테이블과 다른 트윈은, `scripts/ops/table_rebuild.py`가 이름 변경 없이 교환해 내보낸 이전 테이블일 수 있습니다. 이 도구가 트윈과 같은 이름을 공유하기 때문입니다. 이때 모든 줄이 거부하고, 아무것도 삭제하지 않으며, 보고서가 이름 변경을 알려주는 `python -P -m scripts.ops.table_rebuild --table <table> --dry-run`을 안내합니다. 교환된 적 없는 트윈은 재실행이 어떤 레이아웃을 지정하든 잔여물입니다. `--restart --yes`로 다시 복사하거나 삭제하세요. 도구는 트윈이 생성될 때 `$LOOM_REBUILD_DIR/<table>_rebuild.uuid`(abort 파일이 있는 디렉터리)에 기록된 UUID가 트윈과 라이브 테이블의 UUID와 같으면 그 트윈이 교환된 적 없다고 판단하며, 이는 어떤 데이터베이스 엔진에서도 동작합니다. 교환이 한 번이라도 일어나면 라이브 테이블 이름에 다른 UUID가 붙으며, 다른 디렉터리에서 실행한 `--reuse` 교체도 마찬가지이고 이 경우 기록이 남겨집니다. 도구는 자체 교환 직전에 이 기록을 제거하며, 제거하지 못하면 교체를 거부합니다. 일치하는 기록이 없으면, `system.tables.metadata_modification_time`이 라이브 테이블보다 늦은 트윈을 교환된 적 없는 것으로 판단합니다. 이는 `EXCHANGE TABLES`가 각 테이블의 고유한 시간을 유지하는 Atomic 및 Replicated 데이터베이스에서 성립하는데, 라이브 테이블의 메타데이터 시간이 트윈보다 늦으면 교체가 거부되기(종료 코드 3, 트윈 유지) 때문입니다. 게이트가 도는 동안 라이브 테이블에 실행한 `ADD INDEX`나 `MODIFY SETTING`은 mutation을 만들지 않지만, 교체가 중간에 멈추면 이전 테이블이 더 새로운 쪽이 될 수 있습니다. 이런 ALTER 뒤에는 `--restart`로 다시 실행하세요. 시간이 같은 경우(이 컬럼은 1초 정밀도)와 그 외 데이터베이스 엔진에서는 아무것도 알 수 없으므로, 도구는 트윈을 이전 테이블일 수 있다고 간주합니다. 따라서 매번 같은 `LOOM_REBUILD_DIR`에서 도구를 실행하세요.
`--drop-pre-rebuild`는 먼저 사후 조건과 같은 드레인을 거친 뒤 두 테이블을 다시 비교하며, 두 테이블이 모두 대상 레이아웃인 경우(이미 그 레이아웃인 테이블의 `--require-patch-free` 재구축)에도 같습니다. [교체 후 검사가 찾아내는 항목](#what-the-checks-after-a-swap-find)에 해당하는 차이가 하나라도 있으면 거부하고(종료 코드 3) 복사본을 유지합니다. 두 테이블이 모두 대상 레이아웃이면 `--rollback`과 `--drop-pre-rebuild`는 메타데이터 시간이 더 늦은 테이블을 새 테이블로 간주하는데, 이는 Atomic 및 Replicated 데이터베이스에서만 가능합니다. 시간이 같거나 다른 엔진이면 둘 다 거부하고(종료 코드 3) 수동 비교를 위해 두 테이블을 모두 유지합니다.
삭제 saga는 이 복사본들을 처리하지 않습니다.

- **폭.** 일반 컬럼의 대상 폭은 `LOOM_EMBEDDING_DIM`이므로, 배포 환경의 env로 도구를 실행하세요. 코드화된 컬럼은 자체 폭을 지정합니다. `LOOM_EMBEDDING_DIM`은 서빙 중인 모델의 폭과 같아야 합니다. writer는 서버가 부팅 시 모델에서 조사한 폭을 사용하며, `LOOM_EMBEDDING_DIM`이 낡았으면 서버 로그에 `embedding dim mismatch` 줄만 남습니다. 이름이 지정된 일반 컬럼에 다른 폭의 실제 벡터(모두 0이 아닌)가 있으면 `--dry-run`을 포함해 도구는 거부합니다(종료 코드 3). 복사하면 그 벡터가 영벡터로 대체되고, 코드화된 컬럼은 다른 폭의 쓰기를 거부하기 때문입니다. 메시지에는 각 폭과 `LOOM_EMBEDDING_DIM`이 표시됩니다. 모든 실제 벡터가 하나의 다른 폭이고 서버 로그가 그 폭을 모델의 폭으로 밝히면, `LOOM_EMBEDDING_DIM`을 그 폭으로 설정하고 다시 실행하세요. 재임베딩하지 마세요. 그렇지 않으면 `LOOM_EMBEDDING_DIM`으로 재임베딩한 뒤 다시 실행하세요. `memory_embeddings`, `entity_vectors`, `conversation_embeddings`는 `loom reembed --delete-old`를 사용하는데, 이 명령은 다른 `embedding_model`의 행만 재임베딩하므로 같은 모델에서 폭만 바뀐 경우는 Settings → Models & keys에서 처리합니다. `memory_chunks`, `code_chunks`, `code_summaries` 같은 다른 테이블은 각자의 writer가 재임베딩합니다.
- **게이트(종료 코드 5).** 일관성 게이트는 각 키의 최신 버전(버전 컬럼이 없는 테이블에서는 행 수)과 모든 컬럼의 해시를 비교합니다. 이때 원본의 재구축 대상 벡터 컬럼은 복사본이 쓰는 방식 그대로 캐스트하고 패딩합니다. 트윈에 없거나 버전이 더 오래된 키는 키 단위로 다시 복사하며, 그래도 다른 키가 남아 있으면 게이트가 실패합니다. 트윈에만 있는 키가 있어도 실패합니다. 복사 후 원본에서 행이 제거되었다는 뜻이고, 교체하면 그 행이 되살아나기 때문입니다. recall 게이트는 실제 벡터만 순위에 올립니다. 자리표시자의 코사인 거리는 NaN이기 때문입니다.
- **키와 버전이 같은 행.** `FINAL`에서 ReplacingMergeTree는 최신 버전에 있는 한 키의 행들 중 마지막으로 삽입된 행을 유지합니다. 그런데 복사의 병렬 `INSERT`는 그 행들을 다른 순서로 삽입할 수 있어서, 몇 번을 복사해도 트윈이 다른 행을 유지할 수 있습니다(`DateTime`으로 버전을 관리하는 테이블에서 한 키를 같은 초에 두 번 쓰면 충분합니다). 어떤 키가 계속 다르면 게이트는 원본에서 이런 키의 수를 세어 메시지에 표시합니다. 시간대 안에서 원본에 `OPTIMIZE TABLE loom.<table> FINAL`을 실행하면 키당 한 행만 남으니, 그런 다음 `--restart`로 다시 실행하세요.
- **교체 전.** 도구는 원본의 async insert 큐를 flush합니다(복제 엔진에서는 `SYSTEM SYNC REPLICA`도 실행하는데, 이는 응답하는 레플리카만 동기화하며, `LIGHTWEIGHT`를 쓰는 `ReplicatedMergeTree`에서는 머지가 아니라 fetch만 기다립니다). `ReplicatedMergeTree`에서는 실행하는 동안 내내 한 레플리카에 직접 연결하세요. 이후의 일관성 및 폭 읽기는 각각 같은 문장 안에서, 응답하는 레플리카가 읽는 테이블의 복제 로그 전체를 복사했는지, 그리고 큐에 머지나 mutation 외의 항목이 남아 있지 않은지도 함께 읽습니다. 항목이 하나뿐인 로그를 아직 가져오지 못한 레플리카도 빈 로그를 가진 레플리카와 같은 0의 로그 포인터를 보고하므로, 실행은 `system.zookeeper`에서 로그의 항목을 읽어 항목이 없는 로그일 때만 0을 통과시킵니다. 이를 위해 사용자에게 `system.zookeeper`와 `system.replication_queue`에 대한 `SELECT`가 필요하며, 둘 중 하나라도 읽지 못하면 뒤처진 레플리카에서 읽은 것과 똑같이 거부됩니다. 따라잡지 못한 레플리카에서 읽으면 사전 점검에서 실행이 거부되고(종료 코드 3), 교체 전 게이트가 실패하며(종료 코드 5), 교체 후 사후 조건이 실패합니다(종료 코드 7). 이후의 모든 게이트 읽기와 마지막 delta는 `select_sequential_consistency = 1`을 사용합니다. `SharedMergeTree`에서는 다른 레플리카가 읽기를 처리하더라도 확인된 쓰기를 포함합니다. `ReplicatedMergeTree`에서는 이 설정이 `insert_quorum_parallel = 0`인 quorum insert만 포함하며, 레플리카에 직접 연결하는 일을 대체하지 않습니다. 폭 및 일관성 검사는 삭제, 다른 폭의 쓰기, delta가 가져갈 수 없는 쓰기(트윈의 최고 버전보다 낮은 버전, 또는 버전 컬럼이 없는 테이블에서 트윈에 이미 있는 키의 행), 그리고 키와 버전은 그대로이면서 행의 컬럼(재구축 대상 벡터 포함)이 바뀌는 모든 변경(경량 `UPDATE`나 삭제 툼스톤 등, patch part가 아직 남아 있는지와 무관)에 대해 게이트를 실패시킵니다. 그 밖의 삽입은 복사에 반영되고 교체가 진행됩니다. writer를 막는 것은 도구가 아니라 유지보수 시간대입니다. 게이트는 복사가 시작된 이후(`--reuse`에서는 트윈이 생성된 이후)에 실행 중이거나 생성된 원본 mutation이 있어도 실패하며, 레플리카 시계를 감안해 1분은 제외합니다.
- **교체 후(종료 코드 7).** 도구는 롤백 복사본을 원본으로, 새 테이블을 트윈으로 삼아 마지막 게이트 읽기와 같은 읽기 설정 및 레플리카 검사로 일관성 검사를 다시 실행하고, 복사 이후 두 테이블의 mutation을 셉니다. `ReplicatedMergeTree`에서는 먼저 롤백 복사본에 `SYSTEM SYNC REPLICA ... LIGHTWEIGHT`를 실행합니다. 롤백 복사본에는 자체 복제 큐가 있고, 교체 전에 다른 레플리카에 도달한 쓰기는 이름 변경 후 이 복사본에 속하기 때문입니다. `LIGHTWEIGHT`를 쓰는 이유는 교체가 복사본의 머지를 멈추게 하므로 전체 동기화는 머지 항목을 기다리게 되기 때문입니다. 머지는 어느 테이블의 행도 바꾸지 않으므로 통과합니다. 차이가 있으면 사후 조건이 실패하고 롤백 복사본이 유지됩니다. 출력되는 다음 단계는 [교체 후 검사가 찾아내는 항목](#what-the-checks-after-a-swap-find)에 있습니다.

`--statement-timeout-s`(기본값 1800, 클라이언트의 읽기 타임아웃인 7200 미만)는 폭, 일관성, mutation, delta, recall 읽기(교체 후와 `--drop-pre-rebuild`의 읽기 포함)와 교체 전의 각 delta 및 복구에 상한을 둡니다. 테이블의 행 수, 컬럼, 카탈로그 행을 읽는 작은 읽기에는 적용되지 않으며, 복사와 롤백의 catch-up 복사(그 행을 찾는 읽기 포함)는 별도의 7000초 제한으로 실행됩니다. 교체 후에 읽기가 타임아웃되면 롤백 복사본이 유지된 채 사후 조건이 실패하고(종료 코드 7), `--drop-pre-rebuild`에서 타임아웃되면 복사본이 유지됩니다. 각 폭 검사(사전 점검, 교체 전, 교체 후 롤백 복사본)는 이름이 지정된 일반 컬럼의 모든 벡터를 읽고(ReplacingMergeTree에서는 `FINAL` 사용), 각 일관성 검사(복사 또는 복구 후, 교체 전, 교체 후)는 벡터를 포함해 두 테이블의 모든 행을 읽으므로, 타임아웃은 테이블 전체를 한 번 읽을 수 있는 값으로 설정하세요.

<a id="what-the-checks-after-a-swap-find"></a>

### 교체 후 검사가 찾아내는 항목

교체 후의 실행은 아래 각 항목에서 사후 조건이 실패하고(종료 코드 7) `--drop-pre-rebuild`는 거부하며(종료 코드 3), 두 경우 모두 롤백 복사본을 유지합니다. 요약 줄의 `next`에 다음 단계가 나옵니다.

| 검사에서 발견되는 항목                                                                                                                                                               | 출력되는 다음 단계                                                                                                                                          |
| ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 교체 후 롤백 복사본에만 있는 변경: 그쪽에만 있는 키, 그쪽의 더 새로운 버전, 완료되지 않은 `ALTER UPDATE`, 또는 다른 폭의 벡터를 설정하는 `UPDATE`                                    | `--rollback --yes`: 변경을 포함해 이전 테이블을 되돌려 놓습니다                                                                                             |
| 스왑 이후 라이브 테이블에만 있는 키 또는 그 테이블의 더 새로운 버전: 스왑 이후 새 테이블에 쓴 기록, 또는 마지막 확인 이후 이전 테이블에서 수행한 삭제 | 두 테이블을 모두 보존하고 차이를 직접 확인합니다 |
| 한 버전에서 내용이 다른 키: 병합 여부와 관계없이 어느 한쪽 테이블에서 수행한 경량 `UPDATE`, 또는 스왑 이전에 끝난 `ALTER UPDATE` | 두 테이블을 모두 보존하고 차이를 직접 확인합니다 |
| 스왑 이후 복사가 시작된 뒤 라이브 테이블에서 일어난 뮤테이션, 예를 들어 `loom reembed --delete-old`가 실행하는 경량 `DELETE` | 두 테이블을 모두 보존하고 차이를 직접 확인합니다 |
| 두 테이블의 컬럼 이름이 서로 다름 | 두 테이블을 모두 보존하고 차이를 직접 확인합니다 |
| 롤백 이후 새 레이아웃 복사본에만 있는 변경, 또는 생성 이후 그 복사본에서 일어난 뮤테이션 | 두 테이블을 모두 보존하고 차이를 직접 확인합니다 |
| 확인을 실행하지 못함: drain 또는 sync 실패, 타임아웃을 넘긴 읽기, 또는 따라잡지 못한 레플리카 | 권한(grant)을 고치거나, 레플리카 하나에 직접 연결하거나, `--statement-timeout-s`를 늘린 다음, 거부된 동작(`--drop-pre-rebuild` 또는 `--rollback`)을 다시 실행합니다 |

`--rollback`은 이전 테이블에 없는 행만 다시 복사합니다. 버전 컬럼이 있는 테이블에서는 이전 테이블의 최고 버전보다 높은 버전을 복사하고, 버전 컬럼이 없는 `chunks`에서는 이전 테이블에 없는 키를 복사합니다. 라이브 테이블의 경량 `UPDATE` 또는 `DELETE`, 또는 삭제 툼스톤은 키와 버전을 그대로 유지하며, 이전 테이블의 최고 버전보다 낮은 버전으로 기록된 행은 다시 복사되지 않습니다. 따라서 `--rollback`은 이런 변경을 잃게 됩니다. 이 명령은 교환 전에 위의 확인을 실행하고, 다음 단계가 `--rollback`이 아닌 모든 결과에 대해 아무것도 교환하지 않고 거부합니다(종료 코드 3). 이전 테이블의 병합을 다시 시작하므로, 롤백 복사본에서 끝나지 않은 뮤테이션은 그 뒤에 적용되며, 이어서 실행하는 `--drop-pre-rebuild`는 그 뮤테이션이 건드린 키를 한 버전에서 서로 다른 키로 보여 줍니다. 롤백 이후 새 레이아웃 복사본에서 끝나지 않은 `ALTER UPDATE`는 `FINAL`에서 보이지 않으며, 그래서 이 복사본의 뮤테이션도 집계 대상에 포함됩니다. 롤백이 성공하면 요약에 다음 단계가 표시됩니다. 쓰기 작업과 삭제를 계속 중지한 상태로 `--drop-pre-rebuild --yes`를 실행하세요. 이 명령은 새 레이아웃 복사본을 삭제하기 전에 두 테이블을 확인합니다. 두 번째 `--rollback`은 거부되며(종료 코드 3) 같은 단계를 안내합니다. 라이브 테이블이 더 이상 새 레이아웃이 아니기 때문입니다.
라이브 테이블에서 일어난 삭제는 해당 키를 롤백 복사본에만 남기므로, 첫 번째 행은 복사가 시작된 뒤 라이브 테이블에 뮤테이션이 없는 경우에만 `--rollback`을 안내합니다.

차이를 직접 확인하려면 쓰기 작업과 삭제를 중지한 상태로, 두 테이블이 서로 다르게 가진 키를 양쪽의 버전 컬럼과 함께 나열합니다. `<version>`은 테이블의 ReplacingMergeTree 버전 컬럼으로, `version`이며 `memory_chunks`와 `entity_vectors`에서는 `created_at`, `conversation_embeddings`에서는 `ts`입니다. `<key>`는 테이블의 정렬 키입니다.
`<copy_vectors>`와 `<live_vectors>`는 리빌드에서 지정한 벡터 컬럼(`--columns`)만 나열합니다. `<columns>`는 그 밖의 모든 컬럼이며 다른 벡터 컬럼도 포함하고, 각 컬럼은 양쪽에서 같은 이름으로 씁니다. 도구는 이 컬럼들을 저장된 그대로 해시합니다.
리빌드에서 지정한 컬럼은, 새 레이아웃 쪽(스왑 이후에는 라이브 테이블, 롤백 이후에는 복사본)에서는 컬럼 이름 그대로 씁니다. 이전 레이아웃 쪽에서는 리빌드가 쓰는 방식 그대로
`if(length(<column>) = <dim>, CAST(<column> AS Array(BFloat16)), arrayResize(CAST([] AS Array(BFloat16)), <dim>, toBFloat16(0)))`
로 씁니다. 여기서 `<dim>`은 코덱의 너비(새 레이아웃의 `SHOW CREATE TABLE`에 있는 `Quantized(<method>, <dim>, 0)`)이며, 이렇게 하면 두 레이아웃이 같은 해시를 냅니다.

```sql
SELECT <key>, s.<version> AS copy_version, t.<version> AS live_version,
       s.__present AS in_copy, t.__present AS in_live
FROM (SELECT <key>, <version>, cityHash64(tuple(<columns>, <copy_vectors>)) AS __hash,
             1 AS __present FROM loom.<table>_pre_rebuild FINAL) AS s
FULL OUTER JOIN
     (SELECT <key>, <version>, cityHash64(tuple(<columns>, <live_vectors>)) AS __hash,
             1 AS __present FROM loom.<table> FINAL) AS t USING (<key>)
WHERE s.__hash != t.__hash
SETTINGS join_use_nulls = 0
```

버전 컬럼이 없는 테이블에서는 `FINAL`과 `<version>`을 빼고, 양쪽에서 `count() AS n, sum(cityHash64(...)) AS __hash ... GROUP BY <key>`를 읽습니다. 나열된 각 키의 전체 행을 두 테이블에서 읽고, 아직 적용되지 않은 뮤테이션이 있는지 `system.mutations`를 확인합니다. 그런 다음:

- 나열된 모든 키의 변경이 작성자의 의도대로 라이브 테이블에 있고, 확인에서 집계한 뮤테이션이 라이브 테이블에 모두 적용되었다면, `DROP TABLE loom.<table>_pre_rebuild SYNC`로 복사본을 삭제합니다.
- 그렇지 않으면 윈도우 안에서 복사본에만 있는 각 행을 라이브 테이블로 복사합니다
  (`INSERT INTO loom.<table> (<all columns>) SELECT <all columns> FROM
loom.<table>_pre_rebuild FINAL WHERE (<key>) IN (...)`. 한 버전에서는 마지막에 삽입한 행이 우선합니다). 읽기를 다시 실행해 라이브 테이블이 유지해야 할 변경만 나열될 때까지 반복한 다음 복사본을 삭제합니다. `<all columns>`에서는 MATERIALIZED 컬럼과 ALIAS 컬럼을 제외합니다. INSERT가 이를 거부하기 때문입니다. 스왑 이후 라이브 테이블은 코덱이 적용되어 있어 빈 벡터나 다른 너비의 벡터를 거부하므로, `SELECT`에서 리빌드에서 지정한 각 컬럼을 리빌드가 쓰는 방식 그대로 씁니다.
  `if(length(<column>) = <dim>, CAST(<column> AS Array(BFloat16)), arrayResize(CAST([] AS Array(BFloat16)), <dim>, toBFloat16(0))) AS <column>`.

직접 삭제하면 그 테이블에만 있는 내용의 유일한 사본이 사라지므로, 반드시 읽기 확인을 마친 뒤에 수행하세요.

<a id="disk-pressure-hygiene"></a>

## 디스크 압박 관리

Loom 호스트에서:

- **Docker의 JSON 로그 크기를 제한하세요.** 한 줄마다 스택 트레이스를 기록하는 컨테이너는 디스크를 빠르게 채울 수 있습니다. `/etc/docker/daemon.json`에 `log-opts`를 설정하세요(예: `max-size` 100m × `max-file` 5).
- **빌드 캐시와 dangling 이미지를 제거하세요.** `docker builder prune`과 `docker image prune`을 사용합니다.
- **컨테이너는 prune하지 마세요.** `docker container prune`은 중지된 컨테이너를 제거하는데, [무중단 쌍](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#zero-downtime-pair)에서는 중지된 색상이 곧 롤백 대상입니다([백업 및 복구](https://github.com/ClickHouse/loom/blob/main/docs/operations/backup-and-recovery.md#the-rollback-colour)).
- **코드 미러를 주시하세요.** `var/code_repos`에는 인덱싱된 저장소마다 bare clone이 하나씩 들어 있습니다. 저장소를 폐기하면 다른 항목이 해당 미러를 사용하지 않고 미러가 `LOOM_CODE_REPOS_ROOT` 아래에 있는 경우에 한해 미러가 삭제됩니다. 운영자가 다른 위치에 둔 미러는 그대로 남습니다.
- **진단 출력은 작게 유지하세요.** 디스크가 가득 차면 임시 파일을 쓰는 명령도 실패합니다. 진단 결과는 `/dev/shm` 같은 메모리 기반 경로에 기록하세요.

ClickHouse에서:

```sql
SELECT table, formatReadableSize(sum(bytes_on_disk)) AS size
FROM system.parts WHERE active AND database = 'loom'
GROUP BY table ORDER BY sum(bytes_on_disk) DESC LIMIT 10;

SELECT name, formatReadableSize(free_space) FROM system.disks;
```

대량 삭제 후에는 머지가 버전을 정리하도록 두거나, 코드 벡터 테이블을 컴팩션하거나, 테이블별 보존 정책을 선택적으로 적용하세요. 증상부터 찾아보는 설명은 [문제 해결](https://github.com/ClickHouse/loom/blob/main/docs/operations/troubleshooting.md#the-host-or-clickhouse-runs-out-of-disk)에 있습니다.

<a id="related"></a>

## 관련 문서

- [모니터링](https://github.com/ClickHouse/loom/blob/main/docs/operations/monitoring.md): 복구가 필요한 시점을 알려 주는 게이지
- [백업 및 복구](https://github.com/ClickHouse/loom/blob/main/docs/operations/backup-and-recovery.md)
- [코드 인덱스 운영](https://github.com/ClickHouse/loom/blob/main/docs/operations/code-index-operations.md)
