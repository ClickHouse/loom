<!-- i18n: source=docs/features/code-index/freshness.md blob=9dcf44236c7be8f6d338e452c10b5a0be2e98f93 model=claude-sonnet-5-5 -->
> 이 페이지는 [영어 원문](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/freshness.md)을 기계 번역한 것이며, 영어 원문이 기준입니다.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/freshness.md), which is authoritative.

<a id="code-index-freshness"></a>

# 코드 인덱스 최신성

인덱스는 저장소보다 늦게 반영됩니다. 인덱스는 설정된 브랜치에 병합된 커밋을 패스 단위로 색인하며, 각 티어는 자체 워터마크를 따로 진행합니다. 색인된 코드를 신뢰하기 전에 답변에 최신성 메타데이터가 있으면 그것을 확인하고, 해당 티어의 상태도 확인하세요.

<a id="the-as_of-block"></a>

## `as_of` 블록

`as_of`를 반환하는 오퍼레이션은 다음 최신성 필드를 노출합니다. 일반적인
`code.symbol` 정의에는 이 블록이나 커밋이 포함되지 않습니다. 대신
`code.index_status`를 확인하고, 반환된 정의를 해당 소스 리비전과 대조해 검증하세요.

| 필드            | 의미                                                                                                                                                 |
| --------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------- |
| `commit`        | 이 네임스페이스에 대한 빠른 티어의 커밋(`git_head`)이며, 모든 컴파일러 티어의 리비전은 아닙니다                                                      |
| `branch`        | 네임스페이스가 추적하는 브랜치                                                                                                                       |
| `indexed_at`    | 해당 커밋이 반영된 시각                                                                                                                              |
| `lag_commits`   | `commit`과 미러가 가져온 최신 지점 사이의 first-parent 커밋 수. `-1`은 알 수 없음                                                                    |
| `stale`         | `lag_commits` > `LOOM_CODE_STALE_AFTER_COMMITS`(기본값 5)이거나 미러 fetch가 지연된 경우                                                             |
| `status`        | `fresh`(최신 지점), `behind`(오래됨 임계값 미만), `stale`, `unknown`(lag `-1`)                                                                       |
| `stale_reason`  | fetch를 멈춘 미러를 기준으로 lag를 계산한 경우 `mirror_not_fetched`                                                                                  |
| `fetched_at`    | `stale_reason`과 함께 표시: 미러가 마지막으로 fetch한 시각                                                                                           |
| `failing_tiers` | 항목에서 티어가 실패하는 동안 존재: `tier`, `kind`, `failing_since`, `failed_passes`, `fix`, 워터마크가 있는 티어의 경우 `lag_commits`               |

이 블록은 네임스페이스별로 30초 동안 캐시되며, `git_head`를 옮기는 패스가 있으면
캐시가 무효화됩니다. fail-open 방식이므로 최신성을 읽을 수 없으면 블록이 비어
있어도 응답은 그대로 반환됩니다. 비어 있거나 없는 `as_of`는 알 수 없음으로
취급하세요.

`stale`이 false이더라도 실패한 티어의 사실 정보는 head보다 뒤처져 있을 수
있습니다. `failing_tiers`가 컴파일러 티어를 가리키면, 컴파일러 수준의 답변(정확한
호출 그래프, 참조)이 뒤처졌을 수 있다고 보고 의존하는 코드를 직접 읽으세요.

**빌드 상태만이 아니라 최신성을 확인하세요.** 첫 빌드가 끝난 네임스페이스도
여전히 여러 커밋 뒤처져 있을 수 있습니다. `as_of`가 반환되면 `stale`을 확인하고,
반환되지 않으면 `code.index_status`로 관련 워터마크를 확인하세요. 최신성이 stale이거나
알 수 없음이면, 편집하려는 파일은 체크아웃에서 직접 읽으세요.

<a id="the-mirrors-own-age"></a>

## 미러 자체의 경과 시간

지연(lag)은 Loom의 로컬 미러를 기준으로 계산하므로, 미러가 가져오기를 멈춘 상태에서도 값이 0으로 표시됩니다. 따라서 가져오기가 기한을 넘기면 `stale`도 true가 되며, 상태 op는 미러의 경과 시간을 보고합니다.

- `code.index_status`(네임스페이스 하나)와 `code.repos`(읽을 수 있는 모든 항목)는 `clone_head`, `clone_fetched_at`, `fetch_overdue`(`LOOM_CODE_FETCH_STALE_AFTER_SECONDS`보다 오래됨, 기본값 3600)를 반환합니다. 함께 `git_head`, `fast_tier_lag`, `scip_lag` / `scip_py_lag`, `compiler_tiers` 목록, `last_refresh_at`, `chunks_total` / `chunks_embedded` / `embedding_coverage`도 반환합니다.
- 두 op 모두 `tier_health`를 반환합니다. 각 티어의 마지막 기록된 결과(가져오기, 수집, 컴파일러 티어, 주간 C++ 전체 실행)를 담고 있으며, 실패 중인 경우에는 오류 종류, `failing_since`, `failed_passes`, 해결 방법이 포함됩니다. 이 호스트에서 의존성을 가져올 수 없는 모듈을 제외한 Go 티어는 `state: "unavailable"`(종류 `dependency_unavailable`, 처음 확인된 시점의 `failing_since`, 실패 횟수 없음)로 표시되며 해결 방법이 함께 제공됩니다. 이 경우 실패가 아니며 `as_of.failing_tiers`에도 포함되지 않습니다.
- `code.repos` 행에는 `searchable`이 있습니다. `embedding_coverage`가 `LOOM_CODE_READY_EMBED_COVERAGE`에 도달했는지를 나타냅니다.

`compiler_tiers`에는 해당 항목이 실행하는 티어마다 카드가 하나씩 있습니다(`producer`, `tool`, `label`, `commit`, `lag`, `workers`). `code.index_status`는 MCP를 통해 호출하거나 `commit`이 설정된 경우 간략하게 응답하며, `brief: false`를 지정하면 전체 응답을 반환합니다.

<a id="is-one-commit-in-every-tier"></a>

## 하나의 커밋이 모든 티어에 있는가?

병합된 PR을 검토하는 사람은 모든 티어가 해당 병합을 반영하고 있는지 알아야 합니다. `code.index_status`에 `commit=<sha>`를 지정하면 `commit_coverage`로 답합니다.

| 필드                    | 의미                                                                                     |
| ----------------------- | ---------------------------------------------------------------------------------------- |
| `ready`                 | 빠른 티어와 워터마크가 있는 모든 컴파일러 티어가 해당 커밋에 도달하면 true                |
| `waiting_on`            | 아직 도달하지 못한 티어                                                                  |
| `in_mirror`             | 미러에 해당 커밋이 있는지 여부                                                           |
| `fast`, `tiers[]`       | 티어별 값: `covered`(미러에 해당 커밋이 없으면 null)와 `commits_since`                    |
| `commits_since`         | 빠른 티어가 해당 커밋을 지나 얼마나 더 진행했는지                                        |
| `paths_changed_overlap` | `paths=[…]`를 지정한 경우, 해당 경로 중 이후 병합에서 변경된 경로                         |

`paths_changed_overlap`의 경로는 인덱스에서 해당 커밋의 경로와 다를 수 있으므로, 체크아웃한 저장소에서 읽으십시오.

```python
st = await agent.code.index_status(namespace="code-myrepo", commit=merge_sha, paths=pr_files)
cov = st["commit_coverage"]
if not cov["ready"]:
    print("waiting on", cov["waiting_on"])
```

<a id="waiting-for-coverage-without-polling"></a>

## 폴링 없이 커버리지 기다리기

`code.coverage.watch`는 커밋이 커버되면 작업 인박스에 이벤트를 게시합니다.

```bash
curl -fsS -X POST "$LOOM_BASE_URL/v1/code.coverage.watch" \
  -H "Authorization: Bearer $LOOM_API_KEY" -H 'Content-Type: application/json' \
  -d '{"org":"<org>","namespace":"code-myrepo","commit":"'"$MERGE_SHA"'","inbox":"<your-inbox>"}'
```

- `commit`은 전체 SHA 또는 미러가 해석할 수 있는 축약형입니다. `inbox`는 작업 인박스(`inbox.register`)입니다. 호출자는 코드 네임스페이스에 대한 viewer 권한과 인박스 소유자 네임스페이스에 대한 writer 권한이 필요합니다.
- `code_coverage_notify` 워커(기본적으로 켜져 있으며 `LOOM_CODE_COVERAGE_NOTIFY_INTERVAL_SECONDS`마다 실행되고, 기본값은 60)는 `patch`가 `{namespace, commit, tiers: [{producer, label, watermark}]}`인 `code.coverage.ready` 이벤트를 한 번 게시합니다. 이 이벤트는 `/v1/task/watch?inbox=…`, `task.updates`, 그리고 인박스의 서명된 웹훅을 통해 도착합니다.
- 이미 커버된 커밋은 `ready=true`, `emitted=true`로 응답하며, 같은 호출 안에서 이벤트를 게시합니다.
- (네임스페이스, 커밋, 인박스)마다 이벤트는 하나입니다. 이벤트의 `task_id`가 응답의 `event_id`이므로 재시도해도 두 번 게시되지 않습니다. 하나의 이벤트에 대한 전달은 해당 id의 리스 아래에서 한 번에 하나씩 실행되므로, 경합하는 두 전달 중 이벤트를 저장한 쪽만 `emitted=true`로 응답합니다. 같은 호출에서 전달을 수행하는 `code.coverage.watch`가 다른 전달을 10초보다 오래 기다리면 409 `PRECONDITION_FAILED`로 응답하고, 리스 저장소(Keeper)가 응답하지 않으면 503으로 응답합니다. 둘 다 재시도하면 됩니다.
- `code.coverage.unwatch`(같은 필드)는 대기 중인 watch를 제거합니다. `removed: false`는 활성 상태의 watch가 없었다는 뜻입니다.

Python SDK에는 `agent.code.coverage_watch(commit, inbox, namespace=…)`와 `agent.code.coverage_unwatch(...)`가 있습니다. 인박스에 대한 설명은 [작업과 결정](../tasks-and-decisions.md#task-inbox)을 참고하세요.

<a id="pushing-freshness-from-ci"></a>

## CI에서 최신 상태 푸시하기

폴러(`LOOM_CODE_INGEST_INTERVAL_SECONDS`, 120)는 모든 머지를 감지합니다. 머지를 즉시 인덱싱하려면 머지 후 작업에서 코드 네임스페이스로 범위가 지정된 writer 토큰으로 `code.refresh`를 호출합니다.

```bash
curl -fsS -X POST "$LOOM_BASE_URL/v1/code.refresh" \
  -H "Authorization: Bearer $LOOM_CI_TOKEN" -H 'Content-Type: application/json' \
  -d '{"org":"<org>","namespace":"code-myrepo","expected_head":"'"$GITHUB_SHA"'"}'
```

- 이 실행은 지정한 네임스페이스만 대상으로 하며, 해당 네임스페이스는 설정된 항목이어야 합니다(그렇지 않으면 404). 설정된 브랜치를 가져온 최신 커밋까지 인덱싱합니다.
- `expected_head`는 로그에만 기록됩니다. `wait=true`를 지정하면 실행이 끝날 때까지(최대 `wait_timeout_seconds`, 기본값 300) 대기합니다. 응답에는 `triggered`, `already_running`, `head_before`, `head_after`가 포함됩니다.
- 이 호출은 최선 노력(best-effort) 방식이며, 놓친 트리거는 폴러가 복구합니다.

운영자는 `loom.code.tier_lag_seconds{tier}`를 확인합니다. 지연된 티어에 대한 런북은 [코드 인덱스 운영](https://github.com/ClickHouse/loom/blob/main/docs/operations/code-index-operations.md)에 있습니다.

<a id="settings"></a>

## 설정

| 설정                                         | 기본값  | 효과                                                                 |
| -------------------------------------------- | ------- | -------------------------------------------------------------------- |
| `LOOM_CODE_STALE_AFTER_COMMITS`              | 5       | `as_of.stale`이 true가 되기 전까지 뒤처질 수 있는 커밋 수            |
| `LOOM_CODE_FETCH_STALE_AFTER_SECONDS`        | 3600    | `fetch_overdue`와 `stale`이 되기 전까지 미러가 오래될 수 있는 시간 (0이면 검사를 끔) |
| `LOOM_CODE_INGEST_INTERVAL_SECONDS`          | 120     | `code_ingest`가 미러를 폴링하는 주기                                 |
| `LOOM_CODE_READY_EMBED_COVERAGE`             | 0.95    | `code.search`가 벡터 경로를 사용하기 시작하는 임베딩 완료 비율       |
| `LOOM_CODE_COVERAGE_NOTIFY_INTERVAL_SECONDS` | 60      | 커버리지 감시를 확인하는 주기                                        |

<a id="limits"></a>

## 제한 사항

- 최신 상태 여부는 브랜치 네임스페이스 단위로 관리됩니다. `main`에 병합된 수정 사항은 릴리스 브랜치 네임스페이스에 병합되기 전까지는 해당 네임스페이스에 반영되지 않습니다. 여러 네임스페이스를 한 번에 확인하려면 `code.grep mode=presence namespaces=[…]`를 사용하세요.
- 열려 있는 풀 리퀘스트와 작업 트리는 인덱스에 포함되지 않습니다.
- "인덱스가 갱신되었다"는 푸시 알림은 없습니다. `code.coverage.watch`는 커밋마다 알림을 보냅니다.

<a id="related"></a>

## 관련 문서

- [overview.md](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md): 응답 읽는 방법, 문제 해결
- [operations.md](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/operations.md): 상태 op
- [코드 인덱스 아키텍처](https://github.com/ClickHouse/loom/blob/main/docs/architecture/code-index-architecture.md): 워터마크, 조정(reconciliation), extractor 스탬프
