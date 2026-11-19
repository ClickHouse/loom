<!-- i18n: source=docs/features/code-index/operations.md blob=0fe0101809c6f7360e6abcbf26ce81e8828343d5 model=claude-sonnet-5-5 -->
> 이 페이지는 [영어 원문](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/operations.md)을 기계 번역한 것이며, 영어 원문이 기준입니다.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/operations.md), which is authoritative.

<a id="code-operations"></a>

# 코드 작업

이 페이지는 질문을 `code.*` op에 대응시켜 보여 줍니다. 모든 인자와 출력은 자동 생성된 [작업 레퍼런스](https://github.com/ClickHouse/loom/blob/main/docs/reference/operations.md#code)와 각 op의 MCP 설명에서 확인할 수 있습니다.

<a id="calling-a-code-op"></a>

## 코드 op 호출하기

모든 읽기 op는 `org` / `namespace`를 받습니다. 호출에서 이 값을 생략하면 MCP 전송 계층은 코딩 에이전트 플러그인이 현재 체크아웃에 대해 보내는 `X-Loom-Code-Scope: <org>/<namespace>` 헤더를 사용하고, 헤더도 없으면 배포의 기본 코드 네임스페이스(`LOOM_CODE_ORG` / `LOOM_CODE_NAMESPACE`)를 사용합니다. 명시적 인수가 우선하며, 어느 쪽이든 권한 검사는 지정된 네임스페이스에 대해 실행됩니다. 토큰이 읽을 수 없는 네임스페이스를 지정한 헤더는 범위가 넓어지지 않고 거부됩니다. 기본값이 설정되어 있지 않을 때 네임스페이스를 지정하지 않은 호출은 검증 오류와 함께 거부됩니다.

일부 op는 `namespaces=[…]`(최대 8개)도 받아 여러 브랜치 인덱스를 한 번에 조회할 수 있습니다. 결과를 반환하는 모든 응답에는 `trace_run_id`가 포함됩니다. 검색 결과로 무엇을 했는지 `code.outcome`을 통해 보고하면 랭킹이 이를 학습합니다.

각 방식으로 동일한 호출을 하는 예시입니다.

```bash
# HTTP
curl -fsS -X POST "$LOOM_BASE_URL/v1/code.search" \
  -H "Authorization: Bearer $LOOM_API_KEY" -H 'Content-Type: application/json' \
  -d '{"org":"acme","namespace":"code-myrepo","query":"where are request quotas enforced?","top_k":5}'
```

MCP, `loom_code_search` 도구:

```json
{ "query": "where are request quotas enforced?", "namespace": "code-myrepo", "top_k": 5 }
```

```python
# Python SDK
from loom import Agent

async with Agent("acme", "support", "review-bot") as agent:
    # agent.code.* sends no namespace: the server's default code namespace answers
    hits = await agent.code.search("where are request quotas enforced?")
    # any code namespace, through the generic call (fields win over the Agent's identity)
    hits = await agent.call(
        "code.search", namespace="code-myrepo", query="where are request quotas enforced?"
    )
    for u in hits["results"]:
        print(u["path"], u["start_line"], u["symbol"], u["tier"])
    await agent.code.outcome(hits["trace_run_id"], "used_in_answer")
```

**MCP 목록.** `coding` 툴셋에는 `code.search`, `grep`, `enclosing`, `ask`, `symbol`, `outline`, `callers`, `context_pack`, `impact`, `pr_check`, `bisect_candidates`가 포함됩니다. `core`는 `LOOM_MCP_CODE_TOOLS=true`(기본값 꺼짐)일 때만 앞의 여덟 개를 포함하며, `tracker`에는 `impact`, `pr_check`, `bisect_candidates`가 포함됩니다. 그 밖의 모든 코드 op는 이름으로 직접 호출하거나 `loom_op`를 통해 호출할 수 있습니다.
`loom_op {"op": "code.index_status", "arguments": {"namespace": "code-myrepo"}}`. 다음 op는 HTTP와 SDK로만 제공됩니다: `code.outcome`, `code.refresh`, `code.reindex`, `code.calibrate`, `code.context_packs`, `code.coverage.watch`, `code.coverage.unwatch`, `code.scip_ingest`, `code.scip_status`. op의 MCP 도구 이름은 `loom_` 뒤에 op 이름의 점을 밑줄로 바꾼 것을 붙인 형태입니다. 툴셋: [MCP 레퍼런스](https://github.com/ClickHouse/loom/blob/main/docs/reference/mcp.md#toolsets).

**CLI.** 읽기 op용 클라이언트 CLI는 없습니다. `loom code …`(`init`, `backfill`, `reconcile`, `compact`, `reembed`, `backfill-prs`, `scip-full`, `scip-delta`, `warm`, `describe`, `stats`, …)는 서버의 관리자용 CLI입니다
([CLI 레퍼런스](https://github.com/ClickHouse/loom/blob/main/docs/reference/cli.md),
[코드 인덱스 운영](https://github.com/ClickHouse/loom/blob/main/docs/operations/code-index-operations.md)).

<a id="find"></a>

## 찾기

| Op               | 사용하는 경우                                                                                                                                                                                     |
| ---------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `code.grep`      | 정확한 문자열을 알고 있을 때. 기본은 리터럴 검색이며, RE2를 쓰려면 `regex=true`를 지정합니다. `mode`는 `matches`, `files`, `presence`(`namespaces` 전체에 걸쳐) 또는 `message`(출력된 메시지 → 그 메시지를 출력하는 구문)입니다 |
| `code.symbol`    | 이름을 알고 있을 때. 시그니처와 범위가 있는 정의를 반환하며, `uses=true`를 지정하면 그 이름을 언급하는 모든 줄이 추가되고, `near_paths`는 현재 작업 중인 파일을 기준으로 여러 정의의 순위를 매깁니다 |
| `code.search`    | 질문을 말로 풀어 쓸 수 있을 때. 심볼 + 어휘 + 벡터를 결합한 하이브리드 검색입니다. 알고 있는 식별자는 `pattern=`에 넣으세요                                                                      |
| `code.ask`       | 인용이 포함된 종합 답변이 필요할 때 (질문별로 캐시되며, 인용된 파일이 변경되면 무효화됩니다)                                                                                                     |
| `code.files`     | 이름, glob 또는 basename stem으로 파일을 찾고 싶을 때                                                                                                                                             |
| `code.enclosing` | `path:line` 위치(최대 200개)가 있고 각 위치를 둘러싼 함수를 알고 싶을 때                                                                                                                          |
| `code.outline`   | 파일에 무엇이 들어 있는지 보려고 파일 전체를 읽어야 하는 상황을 피하고 싶을 때                                                                                                                    |
| `code.map`       | 저장소 모듈의 전체 구조를 파악하는 지도가 필요할 때                                                                                                                                               |
| `code.trace`     | 스택 트레이스가 있을 때: 각 프레임을 해당 단위로 해석합니다                                                                                                                                       |

자체 시계를 가진 호출자는 `code.grep` 또는 `code.search`에 `deadline_ms`를 전달할 수 있습니다
(200–60000으로 제한되며, 0은 제한 없음을 뜻하고, 설정하지 않으면 `LOOM_CODE_SEARCH_DEADLINE_MS`를 사용합니다).
예산이 소진되면 요청은 새 작업을 시작하지 않고, 타임아웃되는 대신 지금까지의 결과를 반환합니다.
이때 `partial: true`와 함께, 끝나지 않은 작업을 알려 주는 `partial_reason`(ClickHouse의 break-on-timeout으로 중단된 grep 스캔, 검색 leg, rerank, HyDE)이 포함됩니다.
`mode: "message"`에서 어떤 구문 템플릿에도 일치하지 않는 패턴(렌더링된 메시지가 아니라 코드 줄)은 리터럴로 한 번 더 grep되며, 응답에는 `fallback: "literal"`이 표시됩니다.

**예시.**

```bash
# Which release branches contain this string? One call, one row per namespace.
curl -fsS -X POST "$LOOM_BASE_URL/v1/code.grep" \
  -H "Authorization: Bearer $LOOM_API_KEY" -H 'Content-Type: application/json' \
  -d '{"org":"acme","pattern":"retry budget exhausted","mode":"presence","namespaces":["code-myrepo","code-myrepo-2-4"]}'
```

```python
d = await agent.code.symbol("QuotaLimiter::check", uses_brief=True, include_body=True)
located = await agent.code.enclosing(["src/quota/limiter.py:88", "src/api/routes.py:140"])
```

알아 두면 좋은 `code.search` 필드: `path_prefix`, `path_globs` / `exclude_globs`
(rg `-g`), `langs` (rg `-t`), `pattern`(두 leg 모두를 해당 패턴을 포함하는 청크로 제한),
`bodies: "none"`(범위와 시그니처만 반환하는 가장 저렴한 응답), `token_budget`,
`expand`(표현을 식별자와 연결하기 위한 LLM 1회 호출), 그리고 다음 페이지를 위한 `cursor`.

<a id="structure-and-change"></a>

## 구조와 변경

| Op                      | 사용하는 경우                                                                                                                                 |
| ----------------------- | --------------------------------------------------------------------------------------------------------------------------------------------- |
| `code.callers`          | 이 심볼을 누가 호출하는지 `depth` 홉까지 조회. 오버라이드의 경우 기반 선언의 호출자도 함께 보고                                               |
| `code.callees`          | 이 심볼이 무엇을 호출하는지 `depth` 홉까지 조회                                                                                               |
| `code.impact`           | 심볼 또는 `files` / `diff`의 영향 범위: 호출자, 변경에 도달하는 테스트, 함께 변경되는 파트너                                                  |
| `code.test_gate`        | 변경에 필요한 테스트와, 어떤 테스트도 도달하지 않는 변경된 함수                                                                               |
| `code.tests_for`        | 특정 대상을 다루는 테스트                                                                                                                     |
| `code.test_home`        | 새 테스트가 들어갈 위치                                                                                                                       |
| `code.coverage_claim`   | "이미 테스트되어 있는가?"                                                                                                                     |
| `code.verify`           | 닫힌 형태의 주장 하나("A가 B를 호출한다", "X가 Y를 오버라이드한다")를 인덱스와 대조해 확인                                                    |
| `code.verify_citations` | 초안을 보내기 전에 초안 속 모든 `path:line` / 심볼 인용을 확인                                                                                |
| `code.path_guards`      | 두 심볼 사이의 호출 경로에 놓인 조건                                                                                                          |
| `code.examples`         | 심볼이 실제로 호출되는 위치                                                                                                                   |
| `code.setting_facts`    | 소스 트리가 ClickHouse 설정 하나에 대해 알려 주는 내용(선언, 변경 이력, CI 무작위화). ClickHouse 소스 트리에서만 의미가 있음                  |

`code.callers` / `code.callees`는 `min_confidence`를 받아 지정한 등급 미만의 파서 티어 엣지를 제외하고(컴파일러 엣지는 항상 1.0), `include_ambiguous`를 받아 리졸버가 해석하지 않은 호출 위치를 별도의 `candidate_sites` 블록으로 추가합니다. 응답의 `epistemic`과 `causes`를 확인하세요([overview.md](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md#reading-an-answer)).

**예시: diff의 영향.**

```python
import subprocess

diff = subprocess.run(["git", "diff", "origin/main"], capture_output=True, text=True).stdout
r = await agent.code.impact(diff=diff, exclude_authors=["me"])
print(r["epistemic"], r.get("causes"))
for row in r.get("co_change_missing", []):
    print("usually changes with this diff:", row)
```

diff 형태는 hunk와 겹치는 심볼, 그 심볼에 도달하는 테스트, `co_change_missing`을 반환합니다. `files`만 지정하면 해당 파일의 모든 함수가 변경된 것으로 간주합니다. 두 형태 모두 `owners`를 반환합니다. 이는 시드 경로에 대해 최근성에 가중치를 둔 PR 작성자이며, `exclude_authors`(보통 변경 자신의 작성자)는 제외됩니다. 인덱스에는 병합된 코드가 들어 있으므로 diff는 브랜치 헤드를 기준으로 해석됩니다. 아직 커밋하지 않은 함수는 이미 그곳에 존재하는 경우에만 해석됩니다.

<a id="history-prs-and-releases"></a>

## 히스토리, PR, 릴리스

이 op들은 `code_pr_history` 워커가 GitHub에서 채우는 PR 플레인(`LOOM_CODE_PR_HISTORY_ENABLED`)을 읽습니다. 릴리스 브랜치 네임스페이스의 히스토리에는 해당 브랜치에 머지된 PR이 담깁니다. PR 플레인이 채워지는 방식과 bisect, 백포트에 대한 자세한 내용은 [history-and-prs.md](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/history-and-prs.md)를 참고하세요.

| Op                       | 사용하는 경우                                                                                                                                       |
| ------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------- |
| `code.context_pack`      | PR(`pr_number`), 앞으로 할 변경(`task`) 또는 저장소에 대한 브리핑입니다. `sections`를 지정하면 실제로 활용할 블록만 남깁니다 |
| `code.history`           | 심볼 또는 경로를 건드린 머지된 PR                                                                                                        |
| `code.symbol_history`    | 두 릴리스 사이에서 하나의 심볼을 변경한 머지된 PR                                                                                                        |
| `code.blame_range`       | 줄 범위의 배경이 된 머지된 PR                                                                                                        |
| `code.review_brief`      | 머지된 PR에서 리뷰어가 확인한 내용                                                                                                        |
| `code.conventions`       | 하나의 PR을 기준으로 본, 저장소에서 반복적으로 나타나는 리뷰 확인 항목                                                                                                        |
| `code.pr_check`          | PR을 열기 전에 사용합니다. 이와 비슷한 PR이 이미 있는지(머지됨, 열림, 닫힘) 확인합니다                                                                                                                  |
| `code.bisect_candidates` | 프레임, 경로, 심볼, 정상/비정상 구간을 바탕으로 버그를 유발했을 수 있는 머지된 PR                                                                                                                  |
| `code.backport_gap`      | 하나의 수정으로 제거된 버그가 아직 남아 있는 릴리스 브랜치                                                                                                                  |
| `code.backport_gaps`     | 백포트 라벨이 붙었지만 일부 브랜치에 없는 수정에 대한 전체 보고서                                                                                                                  |
| `code.first_build`       | 어떤 CI 빌드에 커밋이 처음 포함되었는지                                                                                                                  |

**예시: PR 전 중복 확인.**

```bash
curl -fsS -X POST "$LOOM_BASE_URL/v1/code.pr_check" \
  -H "Authorization: Bearer $LOOM_API_KEY" -H 'Content-Type: application/json' \
  -d '{"org":"acme","namespace":"code-myrepo","title":"Retry quota refresh on 503","paths":["src/quota/limiter.py"],"symbols":["QuotaLimiter::refresh"]}'
```

`verdict`는 `duplicate_of`(열린 PR이 같은 작업을 하고 있거나 머지된 PR이 이미 했음), `related`, `none` 중 하나입니다. 항목에는 상태, 점수, 근거 줄이 포함됩니다. 이 확인은 휴리스틱 방식이며 GitHub를 호출하지 않습니다. 제목만으로는 중복으로 판정되지 않습니다.

<a id="status-and-administration"></a>

## 상태 및 관리

| Op                      | 역할   | 반환값                                                                                                                                                 |
| ----------------------- | ------ | ------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `code.repos`            | viewer | 최신성, 접근 권한, 부트스트랩 단계를 포함한 읽기 가능한 항목. `repo` / `repos`로 하나의 체크아웃으로 좁힐 수 있음 (GitHub 포크는 부모의 인덱스로 해석됨) |
| `code.index_status`     | viewer | 워터마크, 지연, 티어, `commit_coverage` ([freshness.md](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/freshness.md#is-one-commit-in-every-tier))                                                  |
| `code.coverage`         | viewer | 언어별 파일, 청크, 티어별 심볼, 엣지, 그리고 `gaps` — 인덱스가 알지 못하는 부분                                                                        |
| `code.index_errors`     | viewer | 인덱서가 처리에 실패한 파일                                                                                                                            |
| `code.recent_commits`   | viewer | 최근에 수집된 커밋                                                                                                                                     |
| `code.context_packs`    | viewer | 저장된 PR 팩                                                                                                                                           |
| `code.outcome`          | viewer | 결과로 무엇을 했는지 기록 (`opened_file`, `used_in_answer`, `fell_back_grep`, `ignored`)                                                               |
| `code.coverage.watch`   | viewer | 커밋이 포함되면 받은 편지함에 `code.coverage.ready` 작업 이벤트를 전달. 받은 편지함 소유자 네임스페이스에 대한 writer 권한 필요                          |
| `code.coverage.unwatch` | viewer | 대기 중인 watch를 해제                                                                                                                                 |
| `code.scip_status`      | viewer | 업로드된 컴파일러 티어 델타의 상태                                                                                                                     |
| `code.refresh`          | writer | 수집 과정을 즉시 실행 ([freshness.md](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/freshness.md#pushing-freshness-from-ci))                                                                    |
| `code.scip_ingest`      | writer | 외부 생성기에서 컴파일러 티어 델타를 업로드 (`code_scip_apply`가 적용)                                                                                 |
| `code.reindex`          | admin  | 수집 워터마크를 특정 커밋으로 이동                                                                                                                     |
| `code.calibrate`        | admin  | 검색의 노이즈 하한과 보류 임계값을 다시 맞춤                                                                                                           |

리포지토리 추가, 브랜치 추가, 접근 제한, 제거(`code.repo.add`,
`code.repo.remote_branches`, `code.repo.add_branch`, `code.repo.set_access`,
`code.repo.set_source`, `code.repo.remove`)는
[repositories.md](repositories.md)에서 설명합니다.

<a id="related"></a>

## 관련 문서

- [overview.md](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md) — 티어, 언어 지원, 답변 읽는 방법
- [freshness.md](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/freshness.md) — `as_of`, 커밋 커버리지, CI 갱신, 커버리지 감시
- [history-and-prs.md](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/history-and-prs.md) — PR 플레인, bisect, 백포트
- [Python SDK 레퍼런스](https://github.com/ClickHouse/loom/blob/main/docs/reference/python-sdk.md), [MCP 레퍼런스](https://github.com/ClickHouse/loom/blob/main/docs/reference/mcp.md)
- [오퍼레이션 레퍼런스](https://github.com/ClickHouse/loom/blob/main/docs/reference/operations.md#code) — 자동 생성된 인자
