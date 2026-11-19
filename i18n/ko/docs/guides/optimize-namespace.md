<!-- i18n: source=docs/guides/optimize-namespace.md blob=bcf139a5e94cdfb8e192ed26d7bf748075392110 model=claude-sonnet-5-5 -->
> 이 페이지는 [영어 원문](https://github.com/ClickHouse/loom/blob/main/docs/guides/optimize-namespace.md)을 기계 번역한 것이며, 영어 원문이 기준입니다.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/guides/optimize-namespace.md), which is authoritative.

<a id="optimize-a-namespace-from-its-audit"></a>

# 감사 결과로 네임스페이스 최적화하기

`memory.set`을 객체 저장소처럼 사용하는 작성자(해시 키 아래의 JSON blob, 메모리로 저장한 lease, 클레임으로 저장한 문서)는 모든 테이블의 일관성은 유지하지만 recall은 쓸모없게 만듭니다. 이 절차는 그런 네임스페이스를 [조사 에이전트 가이드](https://github.com/ClickHouse/loom/blob/main/docs/guides/investigation-agent-memory.md)에 있는 레이아웃으로 재구성합니다. 모든 이동은 리디렉션을 남기므로, 이전 키를 계속 읽는 실행 중인 작성자도 계속 응답을 받습니다.

[Claude Code](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/claude-code.md) 및 [Codex](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/codex.md) 플러그인의 `loom-optimize` 스킬이 같은 단계를 실행합니다.

<a id="the-two-operations"></a>

## 두 가지 작업

**`namespace.audit`** (`POST /v1/namespace.audit`, MCP `loom_namespace_audit`, CLI
`loom audit-namespace`). 읽기 전용입니다.

| 입력               | 기본값          |
| ------------------ | --------------- |
| `org`, `namespace` | 필수            |
| `window_seconds`   | 604800 (7 days) |
| `sample_limit`     | 5               |

이 작업은 다음을 반환합니다. `summary`(`live_rows`, `live_bytes`, `excerpt_bytes`, `version_rows`,
`version_bytes`, `unembedded_rows`, `gets`, `searches`, `sets`, `get_search_ratio`, 그리고
`inspector_calls`: 대시보드와 어시스턴트 툴셋의 get과 search로, 집계에서는 제외됩니다. 대시보드에서 수행한 쓰기는
`sets`에 집계됩니다), `findings[]`(`code`, `severity`, `count`, `share`, `bytes`, `samples[]`, `detail`,
`remedy`), `skipped`(실행하지 못한 규칙: 검사, 요약 읽기, 증거 읽기. 나머지는 계속 반환됩니다), `coverage`, `healthy`("치명적 발견 사항 없음"), 그리고
`criticals` / `warnings` / `infos` 개수입니다. 샘플에는 키, 행 id, 식별 컬럼, 개수가 들어 있으며 값은 절대 포함되지 않습니다. 각 finding에는 [증거
필드](https://github.com/ClickHouse/loom/blob/main/docs/features/namespaces-and-sharing.md#reading-a-namespaces-health)도 함께 들어 있습니다. 플랜은
`kind`(사용 휴리스틱은 오용의 증거가 아닙니다), `samples_complete`, `evidence[]`, 즉 샘플링된 각 행의 현재 `version`과 `content_sha256`을 읽습니다.

**`memory.move`** (`POST /v1/memory.move`, MCP `loom_memory_move`).

| 입력                                                                         | 기본값                      |
| ---------------------------------------------------------------------------- | --------------------------- |
| `org`, `namespace`, `agent`, `from_key`, `to_key`                            | 필수                        |
| `value`, `memory_type`, `tags`, `entities`, `valid_at`, `source_resource_id` | 원본 행에서 가져옴          |
| `keep_redirect`                                                              | `true`                      |
| `if_match_sha256`                                                            | 없음                        |
| `dry_run`                                                                    | `false`                     |

이 작업은 `{from_key, to_key, redirected, memory_id, version, content_sha256, advisories,
dry_run}`을 반환하며, 드라이 런에서는 `memory_type`과 `value_preview`가 추가됩니다. 이동 후에는 다음과 같이 동작합니다.

- `memory.get(from_key)`는 여전히 이전 값과 `canonical_key`를 함께 반환합니다.
- `memory.get(from_key, follow=true)`는 새 행을 반환합니다.
- `memory.search`와 `memory.list`는 새 행만 보여 줍니다.

<a id="1-audit"></a>

## 1. 감사

```bash
loom audit-namespace --org <org> --namespace <ns> --window-days 7 --samples 5
```

CLI는 서버에서 실행되며 ClickHouse를 직접 읽습니다. 코드, 심각도, 종류, 개수, 비율, 첫 번째 샘플로 구성된 표를 출력하고, `--json`을 지정하면 출력 모델을 출력합니다. 네임스페이스가 정상이 아니면 종료 코드 1로 끝납니다. 다른 환경에서는 HTTP 또는 MCP로 `namespace.audit`을 호출하세요.

먼저 `summary`를 읽습니다.

- `get_search_ratio`가 수백 단위라면 해당 네임스페이스가 키-값 저장소처럼 사용되고 있다는 뜻입니다 (프로시저 읽기는 의도적으로 키로 다시 읽는 것이므로 여기서 제외됩니다).
- `unembedded_rows`가 0보다 크면 검색으로 절대 찾을 수 없는 행이 있다는 뜻입니다.

그다음 발견 사항(finding)을 읽습니다. `healthy`가 true이고 `warning`이나 `critical`이 없으면 멈춥니다. `info` 발견 사항은 작성자를 위한 참고 사항이며 다시 작업해야 할 문제가 아닙니다. `system_state` 발견 사항(인덱싱 적체, 텔레메트리 누락)은 배포 환경에 관한 것이며 작성자와는 무관합니다. `telemetry_unavailable`이 경고인 동안에는 `exact_key_store`와 `intentional_exact_key_reads`가 정상이 아니라 `skipped`에 포함되고, `coordination_as_memory`는 키 세그먼트 검사만 실행합니다 (`samples_complete: false`).

대시보드의 네임스페이스 감사 패널에서도 같은 감사 결과를 볼 수 있습니다. 네임스페이스가 정상이 아닐 때(심각 발견 사항 1건, 또는 `exact_key_store`, `double_encoded_values`, `unembedded_rows`, `coordination_as_memory` 중 경고 2건) `context.pack`에 `namespace_health` 블록이 포함되므로, 에이전트가 세션 시작 시 이를 확인할 수 있습니다.

<a id="2-plan"></a>

## 2. 계획

발견 사항마다 하나의 섹션을 작성합니다. 감사에서 반환된 코드만 섹션으로 작성합니다.

| 발견 사항                                                               | 변경 내용                                                                | 방법                                                                                                                                                   |
| ----------------------------------------------------------------------- | ------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `double_encoded_values`, `json_prefix_excerpts`, `hash_keys`            | 행을 읽을 수 있는 키와 주장 우선 형태의 값으로 옮깁니다                  | 한 번 디코딩합니다. 주장 문장, 출처 줄, 나머지는 짧은 줄로 작성합니다. `memory.move`에 `value`, `tags`, `entities`, `valid_at`, `if_match_sha256`를 전달합니다 |
| `oversized_values`, `episodic_blobs`                                    | 본문은 문서 또는 블롭이 되고, 한 문단짜리 행이 키를 유지합니다           | `knowledge.upsert`(Markdown, `source_uri=loom://<org>/<ns>/<key>`) 또는 `context.offload`를 실행한 뒤, 기존 키를 `memory.move`로 옮기며 해당 문단을 `value`로 지정합니다 |
| `unembedded_rows`                                                       | 임베딩되도록 값을 분할하거나 줄입니다                                    | `oversized_values`와 같습니다. `namespace.validate`는 이 행들을 `unembedded` 행에 나열합니다                                                           |
| `coordination_as_memory`                                                | 해당 블록을 작업 메모리로 옮기고, 기존 키는 계속 응답합니다              | `working_memory.set`을 실행한 뒤 `memory.move`를 `keep_redirect=true`로 실행합니다                                                                      |
| `hand_rolled_index_rows`                                                | 행은 읽을 수 있는 상태로 두고 `tags`를 추가하며, 작성 측이 이런 행을 더 이상 만들지 않게 합니다 | 태그만 추가합니다(`memory.update_by_id`). 이동은 하지 않습니다                                                                                         |
| `exact_key_store`, `version_churn`                                      | 작성 측을 변경합니다                                                     | 소유자를 위해 키를 나열합니다                                                                                                                          |
| `telemetry_unavailable`, `index_backlog`, `intentional_exact_key_reads` | 없음: 배포 환경의 상태이거나 워크플로입니다                              | 재작업하지 않습니다. `telemetry_unavailable`은 원장이 해당 기간을 포괄한 후 다시 실행하고, 정체된 `index_backlog`는 임베딩 워커를 확인합니다           |

멘토(`mentor.brief`)는 데이터 변경에 해당하는 세 가지 형태를 더 제시합니다. 이들은 힌트의
`evidence`를 개수로 삼아 같은 규칙으로 재작업합니다.

| 멘토 힌트                | 변경 내용                                                                                       | 방법                                                                                                |
| ------------------------ | ----------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------- |
| `documents_as_memories`  | `oversized_values`와 같습니다                                                                   | `knowledge.upsert`를 실행한 뒤, 해당 문단을 `value`로 하여 `memory.move`를 실행합니다               |
| `checkpoints_as_memory`  | 실행 덤프는 `context.offload` 블롭이 되고, 실행 id를 키로 하는 짧은 에피소드 행 하나가 이를 대체합니다 | `context.offload`를 실행한 뒤, 한 줄짜리 원장 `value`와 `memory_type=episodic`으로 `memory.move`를 실행합니다 |
| `records_for_identities` | 스칼라 필드를 가진 `<prefix>-<id>` 키의 행들이 스키마 하나와 행당 레코드 하나로 바뀝니다       | `memory_schema.register`를 한 번 실행하고, 행마다 `memory.record.write`를 실행한 뒤, 포인터 행으로 `memory.move`를 실행합니다 |

각 섹션에는 다음을 명시합니다. 개수와 바이트 수, 발견 사항의 `samples`에서 가져온 변경 전 → 후 예시 키
세 개, 인자를 포함한 정확한 호출, 그리고 수행하지 **않을** 작업입니다.

- `memory.delete`를 사용하지 않습니다.
- 값을 제자리에서 변경하지 않습니다. 다시 작성한 값은 새 키 아래에만 저장됩니다.
- 최근 한 시간 안에 작성된 행은 건드리지 않습니다. 작성 측이 작업 중일 수 있기 때문입니다.

계획은 `loom://<org>/<ns>/_optimize/plan-<YYYYMMDD>`에 지식 문서로 저장합니다
(해당 `source_uri`로 `knowledge.upsert`). 이렇게 하면 소유자의 승인과 변경 전/후 수치가 한곳에 남습니다.

<a id="3-get-approval"></a>

## 3. 승인 받기

네임스페이스 소유자가 서면으로 승인합니다. 계획을 보여 주었다고 해서 계획이 승인된 것은 아닙니다. 부분 승인은 명시된 섹션에만 적용됩니다. 소유자의 writer가 실행 중이라면(해당 기간의 감사 `sets`에서 확인할 수 있습니다) 소유자에게 알리십시오. 어느 경우든 최근 한 시간 동안의 행은 범위에 포함되지 않습니다.

<a id="4-apply"></a>

## 4. 적용

네임스페이스가 중요하다면 먼저 백업하세요. `memory.list … fields=["value"]` 결과를 JSONL 파일로 저장하거나, `namespace.snapshot`(HTTP 또는 SDK, MCP로는 제공되지 않음)을 사용하거나, 서버에서 `CREATE TABLE … AS loom.memory`와 `INSERT SELECT`를 실행합니다
([백업 및 복구](https://github.com/ClickHouse/loom/blob/main/docs/operations/backup-and-recovery.md)).

1. 각 섹션의 처음 세 건의 이동은 `dry_run=true`로 실행하고, `to_key`, `redirected`, `advisories`를 다시 읽어 확인합니다.
2. 그다음 `created_at` 순서로 한 번에 최대 50건씩 일괄 이동해, 개정본이 새 키의 버전으로 들어가게 합니다.
3. 감사 샘플 또는 새로 가져온 `memory.get`의 `if_match_sha256`을 전달합니다. 409는 작업 도중 행이 바뀌었다는 뜻입니다. 해당 섹션을 중단하고, 다시 읽은 뒤 확인하세요.
4. `memory_id`, `version`, `content_sha256`이 없는 응답은 이동이 일어나지 않은 것입니다. 성공 건수에 포함하지 마세요.

```bash
curl -s "$LOOM_BASE_URL/v1/memory.move" -H "Authorization: Bearer $LOOM_API_KEY" \
  -H 'Content-Type: application/json' -d '{
    "org": "acme", "namespace": "agent-sre", "agent": "sre-bot",
    "from_key": "3f9a0c1e5b7d2a4c6e8f0a1b3c5d7e9f",
    "to_key": "sre/case/c-42/evidence/e-7",
    "value": "Disk on ingest-3 filled at 02:10 UTC.\nSource: alert A-981.",
    "tags": ["case:c-42", "kind:evidence"],
    "dry_run": true}'
```

<a id="5-measure"></a>

## 5. 측정

같은 기간을 대상으로 감사를 다시 실행하고, 전후 비교 표를 계획 문서에 추가합니다(같은 `source_uri`에 `knowledge.upsert` 사용). 담당자에게 [작성자 규칙](https://github.com/ClickHouse/loom/blob/main/docs/guides/investigation-agent-memory.md#writer-rules)을 전달하세요.

<a id="6-retire-nothing"></a>

## 6. 아무것도 폐기하지 않기

이전 키는 소유자가 원하는 동안 리디렉션을 통해 계속 읽을 수 있습니다. 작성자가 새 키를 사용하기 시작하면 소유자는 `memory.delete`로 이전 키에 삭제 표시(tombstone)를 할 수 있습니다(소프트 삭제: `memory.history`에는 그대로 남습니다). 이 절차는 삭제를 수행하지 않습니다.

> **경고:** 실행 중인 작성자가 여전히 읽고 있는 키를 폐기하면 해당 작성자가 보는 내용이 갈라집니다. `memory.get`이 `found: false`로 응답하고, 클라이언트가 로컬 상태로 대체될 수 있으며, 그러면 두 저장소의 내용이 서로 달라집니다. 리디렉션은 이를 방지하기 위해 존재합니다.

<a id="related"></a>

## 관련 문서

- [조사 에이전트 메모리](https://github.com/ClickHouse/loom/blob/main/docs/guides/investigation-agent-memory.md): 목표 레이아웃
- [에이전트 통합](https://github.com/ClickHouse/loom/blob/main/docs/guides/agent-integration.md#mistakes-to-avoid)
- [기록과 출처](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/history-and-provenance.md)
