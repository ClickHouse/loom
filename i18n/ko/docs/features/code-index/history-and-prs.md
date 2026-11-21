<!-- i18n: source=docs/features/code-index/history-and-prs.md blob=6c133daf27b7ef459055a1cc94a7dd9563aa4255 model=claude-sonnet-5-5 -->
> 이 페이지는 [영어 원문](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/history-and-prs.md)을 기계 번역한 것이며, 영어 원문이 기준입니다.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/history-and-prs.md), which is authoritative.

<a id="history-prs-and-releases"></a>

# 히스토리, PR, 릴리스

이 op들을 사용하려면 병합된 PR 히스토리가 필요합니다. `code_pr_history` 워커는 기본적으로 꺼져 있습니다
(`LOOM_CODE_PR_HISTORY_ENABLED`).

PR 플레인은 "코드가 어떻게 지금의 모습이 되었는가"에 답합니다. 인덱스와 같은 코드 네임스페이스에 있으므로, 히스토리 응답은 병합된 PR의 파일을 현재의 심볼, 호출자, 테스트와 한 번의 읽기로 조인하며, 질문 시점에 GitHub를 호출하지 않습니다. 팀의 이슈 및 인시던트 기록은 연결된 [트래커](https://github.com/ClickHouse/loom/blob/main/docs/features/tracker/overview.md)를 통해 함께 조인됩니다.

<a id="the-pr-plane"></a>

## PR 플레인

| 테이블                    | 내용                                                                                                 | 채우는 주체                                                                     |
| ------------------------- | ---------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------- |
| `loom.code_pr_history`    | 네임스페이스 브랜치에 병합된 PR당 한 행: 번호, 제목, 작성자, 레이블, 파일, 연결된 이슈, 병합 커밋 | `code_pr_history` 워커; 처음 몇 달치는 `loom code backfill-prs`                 |
| `loom.code_pr_reviews`    | 병합된 PR의 인라인 리뷰 댓글                                                                         | 같은 워커 (`LOOM_CODE_PR_REVIEWS_ENABLED`, 기본값 켜짐; 토큰 필요)              |
| `loom.code_gh_items`      | 모든 상태의 이슈와 PR, 열린 PR은 head가 건드리는 파일 포함                                           | 같은 워커 (`LOOM_CODE_GH_ITEMS_ENABLED`, 기본값 켜짐; 익명일 때는 건너뜀)       |
| `loom.code_backport_gaps` | 매일 밤 생성되는 전체 세트 백포트 보고서                                                             | `code_backport_gaps` 워커                                                       |

워커는 `LOOM_CODE_GITHUB_TOKEN` 또는 항목의 `github_token`을 사용하며, 둘 다 없으면 GitHub의
익명 요청 제한을 따릅니다. 릴리스 브랜치 네임스페이스는 해당 브랜치에 병합된 PR을 담으므로,
"2.4에서 무엇이 바뀌었는가"는 2.4 네임스페이스를 읽어 확인합니다. `loom code backfill-prs`는
새 저장소에 대해 `LOOM_CODE_PR_BACKFILL_MONTHS`(12)개월치 기록을 불러옵니다
([코드 인덱스 운영](https://github.com/ClickHouse/loom/blob/main/docs/operations/code-index-operations.md#first-index)).

<a id="which-op-answers-which-question"></a>

## 어떤 op가 어떤 질문에 답하는가

| 질문                                                                      | Op                                                                                       |
| ------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------- |
| 이 PR 또는 앞으로 할 변경에 대해 브리핑해 줘                              | `code.context_pack` (`pr_number` 또는 `task`; `sections`는 실제로 사용할 블록만 남김)    |
| 이 심볼이나 경로를 수정한 머지된 PR은 무엇이고, 리뷰어는 뭐라고 했는가?   | `code.history`, `code.symbol_history`                                                    |
| 릴리스 A와 B 사이에 이 라인들을 마지막으로 수정한 머지된 PR은 무엇인가?   | `code.blame_range`                                                                       |
| 머지된 이 PR에서 리뷰어는 무엇을 확인했는가? 보통 무엇을 확인하는가?      | `code.review_brief`, `code.conventions`                                                  |
| PR을 열기 전에: 비슷한 PR이 이미 있는가?                                  | `code.pr_check`                                                                          |
| 이 버그를 유입했을 수 있는 머지된 PR은 무엇인가?                          | `code.bisect_candidates`                                                                 |
| 수정으로 제거된 버그를 아직 가지고 있는 릴리스 브랜치는 무엇인가?         | `code.backport_gap` (수정 하나), `code.backport_gaps` (레이블이 붙은 모든 수정)          |
| 어떤 CI 빌드가 이 커밋을 처음 포함했는가?                                 | `code.first_build`                                                                       |

인자는 [operations 레퍼런스](https://github.com/ClickHouse/loom/blob/main/docs/reference/operations.md#code)에 있습니다.

<a id="history-of-a-symbol-a-path-or-a-line-range"></a>

### 심볼, 경로, 라인 범위의 이력

`code.history`는 심볼(`name` 또는 `symbol_id`), `path` 또는 지정한 PR `numbers`를 가장 최근에 수정한 머지된 PR을 나열합니다. 번호, 제목, 작성자, 레이블, 그리고 해당 파일에 달린 사람의 리뷰 코멘트를 최대 `reviews_per_pr`개(기본값 3, `0` = 없음) 보여 줍니다.
`code.symbol_history`는 이를 한 함수의 회귀 이력으로 좁힙니다. 해당 함수의 범위를 수정한 PR, 연결된 이슈와 리버트, 그리고 그 범위 안의 리뷰 코멘트를 보여 줍니다.

`code.blame_range`는 `path`, 라인 범위(최대 400라인), 그리고 릴리스 버전(`from_version`, `to_version`) 또는 커밋 sha로 지정한 구간을 받습니다. 이 op는 구간 내 PR 원장의 파일 목록(하한)과 미러에서 실행한 `git blame -w -M -L`을 결합하며, blame된 각 커밋은 해당 PR에 매핑됩니다. 릴리스 브랜치 네임스페이스에서는 호출자가 trunk를 읽을 수 있을 때만 trunk 네임스페이스의 원장도 읽습니다. 읽을 수 없으면 trunk의 행은 제외되고 `legs.ledger.skipped`에 그 사실이 표시됩니다. 릴리스 버전은 호출자가 해당 브랜치 네임스페이스를 읽을 수 있으면 그 네임스페이스의 인덱싱된 head로 해석되고, 그렇지 않으면 미러의 브랜치 또는 `-new` 태그를 통해 해석됩니다.

- `stage: "introduced"`(기본값)는 백포트, 리버트, 공백만 또는 이름만 바꾼 수정의 순위를 낮추고, 해당 함수를 이름으로 언급한 PR, 그 안에 리뷰 코멘트가 달린 PR, 또는 연결된 이슈가 `mentions`의 단어를 포함하는 PR의 순위를 높입니다.
- `stage: "last_touched"`는 blame 순서를 유지합니다.

이 답은 "마지막으로 수정한" PR에 대해서는 정확하지만 "버그를 유입한" PR에 대해서는 그렇지 않으며, `note`에도 그렇게 명시되어 있습니다.

<a id="review-briefs-and-conventions"></a>

### 리뷰 브리프와 컨벤션

`code.review_brief`는 PR이 머지될 때 ingest가 만든 브리프를 읽습니다. 변경된 모든 파일의 분류, 수정된 함수와 그 호출자 및 fan-in, 이를 커버하는 테스트, 그리고 리뷰어가 말한 내용이 들어 있습니다.

`standard`와 `full` 티어에서는 브리프에 `rationale` 섹션도 포함됩니다. 이 섹션은 저장소의 trunk 코드 네임스페이스에서 `files` 또는 코드 링크가 PR의 파일과 닿는 설계 결정과 제약(`decision` 타입의 행, 또는 `decision`이나 `constraint`로 태그된 행이며, 수정 전 블록에 표시되는 기준으로 active 또는 conflicted인 것)을 담습니다. 이 섹션은 호출할 때마다 읽으며, 호출자가 읽을 수 있을 때만 trunk 네임스페이스에서 읽고, 브리프와 함께 저장되지 않습니다. 따라서 머지 후에 기록되거나 변경된 결정도 다음 읽기에 나타납니다. 브리프가 저장한 변경 파일 중 소스 파일을 먼저 두고 처음 `LOOM_CODE_REVIEW_BRIEF_RATIONALE_PATHS`(10)개를 읽으며, 확인할 내용이 있는 행을 우선하여 최대 `LOOM_CODE_REVIEW_BRIEF_RATIONALE_ROWS`(8)개의 행만 유지합니다. 행은 통째로 제공되며 `check` 행이 먼저, 그다음 제약 순이고, 최대 `LOOM_CODE_REVIEW_BRIEF_RATIONALE_TOKENS`(250) 토큰까지이며 첫 행은 항상 포함됩니다. 다만 티어의 예산이 다른 섹션과 마찬가지로 이 섹션을 줄일 수는 있습니다. 각 행에는 수정 전 블록의 해당 행 라인이 값을 라인 길이에서 자른 채(출처, `(pinned)`, 코드 변경 노트는 유지) 포함되며, `pinned`, `code_state`, `verdict`도 함께 제공됩니다. 읽은 변경 파일마다 `memory.for_file` 읽기가 한 번 수행되며, 이는 ClickHouse 읽기 네 번(files 매칭, 코드 링크, 그 링크의 메모리, 연결된 행)입니다. 링크가 많은 파일은 마지막 읽기를 나눕니다. 각 읽기는 최대 50개 행을 유지하며 값은 최대 `LOOM_MEMORY_FOR_FILE_WHOLE_TOKENS` × 48바이트입니다.

검사는 두 종류의 이름을 찾습니다. 하나는 행의 `Rejected:` 절(`Realized:` 절이 나오기 전까지)에 있는 코드 형태의 이름(백틱으로 감싼 이름, `A::b`처럼 한정된 이름, `f()`처럼 호출 형태)이고, 다른 하나는 제약의 값 어디에나 있는 snake_case 이름(백틱 여부와 관계없이, 단독 또는 한정된 형태, 예: `Setting::distinct_spill_ratio`)입니다. 이 검사는 diff가 추가한 라인에 대한 전체 단어 일치이며, 게시된 diff는 최대 200,000자, 미러의 diff는 최대 200,000바이트까지 읽습니다. 따라서 `check`는 "이 행을 diff와 대조해 읽어 보라"는 뜻이지 위반이 발견되었다는 뜻이 아닙니다.

| 행 verdict | 의미                                                                                                                                                                                                                   |
| ---------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `check`    | diff가 행의 확인 대상 이름(`names`) 중 하나를 추가함                                                                                                                                                                   |
| `clear`    | 행에 확인 대상 이름이 있고 diff가 그중 어느 것도 추가하지 않음                                                                                                                                                         |
| `unknown`  | 이름을 찾지 못했지만, 여기서 diff를 읽을 수 없었거나(git 실패, 일부만 읽힌 경우 포함), diff가 잘렸거나, 라인 수가 맞지 않는 hunk가 있거나, 행의 값이 잘림(`diff_unread`, `diff_cut`, `diff_malformed`, `value_cut`) |
| `""`       | 행에 확인할 이름이 없음                                                                                                                                                                                                |

섹션의 verdict는 어느 행이든 그렇다고 하면 `check`이고, 그다음이 `unknown`, 그다음이 `clear`이며, 확인할 내용이 있는 행이 없으면 `none`입니다. `clear`나 `none`은 변경된 모든 경로와 일치하는 모든 행을 확인했을 때만 나타나며, 그렇지 않으면 `unknown`이 되고 원인이 무엇이 남았는지 알려 줍니다. 네임스페이스에 설정된 저장소가 없음(`no_repo`), 브리프에 저장된 변경 경로가 없음(`no_paths`), 호출자가 trunk 네임스페이스를 읽을 수 없음(`trunk_not_readable`), 저장된 변경 경로 목록이 잘렸을 수 있음(`paths_maybe_cut`: 브리프는 처음 50개 변경 파일과 diff hunk의 파일을 최대 200개까지 저장하므로, 50개 이상인 목록은 PR이 실제로 변경한 파일보다 적을 수 있음), 그 목록의 변경 경로 중 읽지 못한 것이 있음(`paths_not_read`), 읽기가 50개 행 한도에 도달한 경로가 있음(`paths_at_row_ceiling`), 코드 링크 읽기에 실패한 경로가 있음(`links_not_read`), 또는 확인할 내용이 있었는데 행 상한 때문에 제외된 행이 있음(`rows_not_read`)입니다.

`code.conventions`는 저장소의 반복되는 메인테이너 검사를 의견이 아니라 한 PR의 diff(`pr_number`, `head_sha`, `files` 또는 `diff`)에 대한 사실로 기술하며, `rules`로 실행할 검사를 좁힐 수 있습니다. 이 op가 읽는 diff는 `LOOM_CODE_CONVENTIONS_DIFF_MAX_BYTES`로 상한이 정해져 있습니다.

<a id="pre-pr-duplicate-check-codepr_check"></a>

## PR 전 중복 확인: `code.pr_check`

풀 리퀘스트를 열려는 에이전트가 초안을 전달하면 세 가지 판정 중 하나를 받습니다:
`duplicate_of`(열린 PR이 이미 같은 작업을 하고 있거나 병합된 PR이 이미 했음),
`related`, `none`.

```python
from loom import Agent

async with Agent(org="acme", namespace="code-widgets", agent="pr-bot") as bot:
    check = await bot.call(
        "code.pr_check",
        title="Retry the upload when the token expires",
        paths=["src/upload/client.py"],
        symbols=["UploadClient.send"],
        issues=[1432],
    )
    if check["verdict"] == "duplicate_of":
        for item in check["items"]:
            print(item["pr_number"], item["state"], item["evidence"])
```

HTTP로 같은 호출은 `POST /v1/code.pr_check`이며, MCP에서는 `loom_code_pr_check` 도구입니다(`coding` 및 `tracker` 툴셋에 포함). 제목, 변경된 경로 또는 이슈 번호 중 하나는 필수입니다.

후보는 GitHub 호출 없이 Loom 자체 원장에서 네 가지 경로를 통해 가져옵니다:

| 경로            | 읽는 대상                                                                                                                            |
| --------------- | ------------------------------------------------------------------------------------------------------------------------------------ |
| 열린 PR         | `code_gh_items`의 모든 열린 PR: 제목과 `refs/pull/N/head`가 변경하는 파일(본문 제외)                                                 |
| 병합 없이 닫힘  | 최근 `LOOM_CODE_PR_CHECK_CLOSED_DAYS`(180)일 이내에 병합 없이 닫힌 PR: "누군가 이미 시도했음"; 최대 `related`까지만 인정             |
| 병합됨, 경로 기준 | 초안의 `paths`를 변경한 병합된 PR                                                                                                  |
| 병합됨, 텍스트 기준 | 텍스트가 초안의 제목 및 심볼과 일치하여 점수를 받는 병합된 PR, 그리고 같은 `issues`를 닫는 병합된 PR                             |

각 후보는 하나의 고정된 규칙으로 점수를 매깁니다. 후보가 가진 초안의 제목 및 심볼 토큰 비율(희소성으로 가중), 병합된 PR에 대한 작은 본문 항, 양방향 경로 중첩, 초안에 명시된 이슈를 닫는지 여부입니다. 중복 PR은 병합되지 않고 닫히기 때문에 가중치를 맞출 레이블된 데이터셋이 없습니다. 응답에는 `epistemic: "heuristic"`이 표시되며, 제목만으로는 `duplicate_of`에 도달하지 않습니다.

| 설정                                     | 기본값 | 효과                                                                  |
| ---------------------------------------- | ------ | --------------------------------------------------------------------- |
| `LOOM_CODE_PR_CHECK_DUPLICATE_THRESHOLD` | 0.55   | 후보가 `duplicate_of`가 되는 점수                                     |
| `LOOM_CODE_PR_CHECK_RELATED_THRESHOLD`   | 0.2    | 후보가 `related`가 되는 점수                                          |
| `LOOM_CODE_PR_CHECK_MERGED_DAYS`         | 365    | 이보다 오래된 병합 PR은 중복으로 투표하지 않음                        |
| `LOOM_CODE_PR_CHECK_MASS_FILES`          | 200    | 이보다 많은 파일을 변경하는 후보는 대량 변경으로 보며, 그 파일은 투표하지 않음 |

`tracker.duplicate_check`는 이슈 쪽의 짝이 되는 기능입니다. 판정이 `none` 또는 `related`일 때, 초안을 이미 다루고 있을 수 있는 `code.pr_check`의 PR을 `related_prs`로 함께 반환합니다
([트래커 개요](https://github.com/ClickHouse/loom/blob/main/docs/features/tracker/overview.md#duplicate-check)).

<a id="bisect-candidates-codebisect_candidates"></a>

## Bisect 후보: `code.bisect_candidates`

버그가 보고되었을 때, 엔지니어는 병합된 PR 중 어떤 것부터 조사해야 할까요?
`code.bisect_candidates`는 후보의 우선순위를 정하며, 자동화된 `git bisect`가 아닙니다.
저장소를 빌드하지 않고, 테스트를 실행하지 않으며, 보고된 문제를 재현하지 않고, 정상 버전과 결함 버전을 찾아내지 않으며, 후보가 버그를 일으켰다는 것을 증명하지도 않습니다. 후보의 점수는 조사 순위일 뿐 원인일 확률이 아닙니다.

회귀 경계는 호출 전에 기록된 증거에서 가져옵니다.

| 윈도우 입력                   | 출처                                                                                                                              | Loom이 하는 일                                                                                                                                      |
| ----------------------------- | --------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------- |
| `bad_version`, `good_version` | 사용자, CI 작업, 지원 워크플로 또는 테스트 에이전트가 결함 릴리스에서 버그를 관찰했고 정상 릴리스에서는 문제가 없음을 관찰한 경우입니다. | 두 관찰 사이의 릴리스를 스캔합니다. Loom은 어느 릴리스도 다시 테스트하지 않습니다.                                                                  |
| `issue_number`                | 연결된 트래커 이슈에 `reproduces_on` 및 `clean_on` 분류(triage) 관찰이 이미 있으며, 트래커 워크플로가 이를 채운 경우입니다.       | 명시적인 버전이 제공되지 않았을 때 이 관찰 값을 읽습니다. 이슈나 분류 값이 존재한다고 해서 보고가 유효하다는 증거는 아닙니다.                       |
| `after`, `before`             | 호출자가 마지막으로 정상이었던 날짜와 처음 발견된 날짜처럼 대략적인 병합 시각 구간만 알고 있는 경우입니다.                        | PR 이력 스캔을 해당 구간으로 좁힙니다. 날짜는 경계일 뿐 재현 결과가 아닙니다.                                                                       |
| `bad_sha`                     | 호출자가 처음 관찰된 실패 시점이거나 그 이전인 커밋을 알고 있는 경우입니다.                                                       | 저장소 미러에서 커밋 시각을 확인하고, `before`가 없으면 이를 `before`로 사용합니다. 해당 커밋을 실행하지는 않습니다.                                 |

그다음 호출자는 가지고 있는 보고 증거를 전달합니다. 크래시 프레임(`symbols`), 소스 `paths`, `patterns`, 자유 형식 `text`, `issue_number`, 그리고 알고 있다면 `fix_pr`입니다.
정확한 릴리스 범위를 알 수 없으면, 저장소 이력 폴백은 이 스캔을 위해 보관된 최근 이력(`LOOM_CODE_BISECT_HISTORY_DAYS`, 기본값 90)을 사용하며, 이는 더 넓고 덜 정밀한 조사 윈도우입니다.

1. 프레임은 코드 인덱스를 통해 파일로 변환됩니다.
2. 후보 스캔은 첫 릴리스가 윈도우 안에 있고 해당 파일을 변경한, 병합된 비파생(non-derivative) PR을 읽고, diff에 이름이 나오는 프레임을 기준으로 순위를 매깁니다. 연결된 트래커가 있으면 트래커의 PR diff를 스캔하며, `issue_number`에 이전에 기록된 분류 컬럼(`reproduces_on`, `clean_on`)이 윈도우를 제공할 수 있습니다. 트래커가 없거나 트래커에 변경된 파일이 없으면 저장소 자체의 PR 이력(`LOOM_CODE_BISECT_HISTORY_DAYS`, 기본값 90)을 읽으며, 응답에는 `candidates_from: "code_history"`가 표시됩니다.
3. 프레임이 없고(잘못된 결과를 보고한 경우) `issue_number`가 있으면, 스캔 범위는 윈도우 diff에서 이슈의 가장 희소한 식별자, 구성 요소의 디렉터리, 수정 PR의 파일로 제한됩니다. `wideners`가 어느 것을 쓸지 선택하고, `widened_by`가 어느 것이 적용되었는지 알려 줍니다.
4. 보고 시점 융합(report-time fusion)은 diff 스캔을 벡터, 어휘, 경로, 하위 트리, 최신성 순위와 융합합니다. 보고에서 버그를 도입한 것으로 지목한 PR이 맨 앞에 옵니다. 융합 결과의 최상위가 약하면(2위와의 차이가 0.1 미만이고 보고에서 지목한 용의 PR이 아닌 경우) 더 넓은 윈도우(12개 릴리스 또는 `LOOM_CODE_BISECT_WIDE_DAYS`)가 추가되고, LLM 호출 한 번이 상위 20개를 다시 정렬합니다(`LOOM_CODE_BISECT_LLM_RERANK_MODEL`, 타임아웃이 발생하면 융합 순서 유지). `rerank: true`로 이를 직접 요청할 수도 있습니다.
5. 수정 PR도 이슈도 지정하지 않은 보고는, 중복 확인이 일치시킨 이슈의 수정 PR을 `LOOM_CODE_BISECT_DUPCHECK_TIMEOUT_S` 안에서 가져와 사용합니다.

각 후보에는 표시(backport, revert, reverted, re-land, 공백 전용 또는 이름 변경 전용, diff의 hunk 헤더에서 얻은 `touches_symbol`)가 붙습니다. `marks: false`는 표시 없이 순위 목록만 반환합니다. 어떤 경로를 소스 코드로 볼지는 트래커의 `bisect.code_prefixes`로 정합니다([레코드 규약](https://github.com/ClickHouse/loom/blob/main/docs/features/tracker/record-contract.md#source-entry-options)).
재정렬에는 실제 LLM 제공자가 필요하며, 나머지 단계는 PR 이력이 로드된 뒤 기본 스택에서 실행됩니다. bisect 설정은 [구성 레퍼런스](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md#code)에 나열되어 있습니다.

아래 예시는 정상 릴리스와 결함 릴리스 사이에 병합되고 크래시가 난 함수의 파일을 변경한 PR의 순위를 매깁니다. `bad_version`은 버그가 처음 관찰된 릴리스이고 `good_version`은 문제가 없던 가장 최신 릴리스이며, Loom은 둘 다 테스트하지 않습니다.

버전 윈도우를 사용하려면 `minor_version`이 포함된 PR 레코드를 가진 저장소에 연결된 트래커 네임스페이스가 필요합니다([레코드 규약](https://github.com/ClickHouse/loom/blob/main/docs/features/tracker/record-contract.md)). 병합된 PR 이력만 있는 경우(`LOOM_CODE_PR_HISTORY_ENABLED` 또는 `loom code backfill-prs`, [첫 인덱스](https://github.com/ClickHouse/loom/blob/main/docs/operations/code-index-operations.md#first-index) 참조) 버전은 스캔 범위를 제한하지 않으며, 대신 이 op는 최근 `LOOM_CODE_BISECT_HISTORY_DAYS`(기본값 90)일을 읽습니다.

```python
from loom import Agent

async with Agent(org="acme", namespace="code-widgets", agent="triage-bot") as bot:
    result = await bot.call(
        "code.bisect_candidates",
        bad_version="26.3",
        good_version="26.2",
        symbols=["UploadClient.send"],
    )
    for candidate in result["candidates"]:
        print(candidate["pr_number"], candidate["score"])
```

후보는 순위대로 반환됩니다. `score`는 어떤 PR부터 조사할지를 알려 줄 뿐, 해당 PR이 버그를 일으켰음을 보여 주지는 않습니다. 버그를 재현하고 후보를 테스트하는 일은 여전히 엔지니어나 CI 작업이 해야 합니다.

최종 검증은 이 작업 밖에서 이루어집니다. 엔지니어, CI 워크플로 또는 테스트 에이전트가 문제를 재현하고 순위가 매겨진 후보를 테스트해야 합니다.

<a id="backport-gaps"></a>

## 백포트 누락

`code.backport_gap`은 병합된 수정(`pr_number`) 또는 그 수정이 닫는 이슈(`issue_number`, 트래커의 `fixes` 에지 또는 복구된 링크로 확인)를 지정하고, 같은 저장소의 릴리스 브랜치 네임스페이스별로 최신 순으로 결과를 반환합니다. `branches`에 네임스페이스나 `26.4` 같은 버전을 지정하면 해당 브랜치만 대상으로 합니다. 세 가지 근거를 종합합니다. 첫째는 백포트 기록으로, 트래커의 `is_backport_pr`, `upstream_pr_number`, `base_branch`와 브랜치 원장에 병합된 백포트를 함께 봅니다. 둘째는 수정이 처음 포함된 릴리스로, 그 시점 이후에 분기된 브랜치에는 수정이 포함됩니다. 셋째는 수정의 hunk가 건드리는 함수가 각 브랜치에 존재하는지 여부입니다.

| 판정           | 의미                                                                                                                                 |
| -------------- | ------------------------------------------------------------------------------------------------------------------------------------ |
| `gap`          | 백포트가 없고, 브랜치가 수정보다 앞서 분기되었으며, 수정이 건드리는 함수(또는 파일)가 존재함. `cherry_pick`은 PR과 병합 sha를 알려 줌 |
| `pending`      | 백포트 PR이 있으며 아직 열려 있음                                                                                                    |
| `covered`      | 백포트가 병합되었거나, 브랜치가 수정이 처음 포함된 릴리스 이후에 분기되었거나, 이슈의 `fix_versions`에 해당 브랜치가 명시됨          |
| `not_affected` | 수정이 건드리는 모든 함수(또는 파일)가 브랜치에 없음                                                                                 |
| `unknown`      | 코드는 존재하지만 기록을 확인할 수 없었거나, 존재 여부 확인이 시간 초과되었거나, 브랜치에 비교할 버전이 없음                         |

심볼 수준의 `gap`은 하한값입니다. 코드가 브랜치에 존재한다고 해서 버그도 존재한다는 것이 증명되지는 않습니다.

- `exact: true`는 패치를 hunk 단위로 검사합니다. 제거된 줄이 아직 있고 추가된 줄이 없으면 `gap`이 확정되고, 추가된 줄이 있고 제거된 줄이 없으며 기록된 백포트가 없으면 `not_affected`가 확정됩니다.
- `history: true`(`exact`를 함의)는 줄 비교로 판정되지 않은 행에 대해 미러에서 브랜치의 트리와 히스토리도 읽습니다.
- `presence.patch`는 모드와 겹침 정보를 근거로 보여 줍니다.

`code.backport_gaps`는 전체 집합 보고서입니다. 최근 90일 동안 메인테이너가 백포트 대상으로 라벨링한 모든 수정을 릴리스 브랜치별로, 누락이 있는 항목을 먼저 보여 줍니다. `code_backport_gaps` 워커가 수정마다 `code.backport_gap(exact, history)`를 실행해 매일 밤 결과를 `loom.code_backport_gaps`에 기록하며(`LOOM_CODE_BACKPORT_GAPS_ENABLED`, 기본값 켜짐, `LOOM_CODE_PR_HISTORY_ENABLED`가 켜져 있을 때만 유효), 이 op는 해당 테이블을 읽습니다. `wanted_only: false`를 지정하면 라벨이 없는 수정도 포함합니다. 모든 브랜치 네임스페이스는 읽기 전에 권한을 확인하며, 실패 시 접근을 거부합니다.

<a id="first-build"></a>

## 첫 빌드

`code.first_build`는 커밋 `sha` 또는 병합된 PR의 `pr_number`를 받아 어떤 CI 빌드가 해당 커밋을 처음 포함했는지 알려 줍니다. 연결된 트래커의 `version_history` 테이블이 필요합니다. 해당 sha를 명시한 실행이 있으면 정확한 결과를 반환하고, 그렇지 않으면 병합 이후 실행들의 base를 미러에서 확인하여(`git merge-base --is-ancestor`, 최대 10개) 가장 이른 후손을 찾으며, 이 경우 결과는 하한값입니다.

<a id="pr-and-tracker-planes"></a>

## PR 및 트래커 플레인

메인테이너는 `code.repo.set_source`로 트래커를 리포지토리에 연결하며, 기본 트래커를 먼저 지정합니다([트래커 개요](https://github.com/ClickHouse/loom/blob/main/docs/features/tracker/overview.md)). 연결하면 다음과 같이 동작합니다.

- `code.bisect_candidates`는 트래커의 PR diff를 스캔하고 의심되는 변경을 알려진 수정 사항과 대조해 확인합니다.
- `code.backport_gap`은 백포트 기록을 읽고, `code.first_build`는 트래커의 CI 실행을 읽습니다.
- `code.history`와 `code.search`, `code.ask`의 히스토리 부분은 바인딩이 해석되고 호출자에게 읽기 권한이 있으면 PR과 함께 트래커 기록도 담습니다.
- `code.context_pack`은 트래커 항목을 포함할 수 있습니다(`sections=["qa"]`).

트래커가 없으면 op는 가능한 범위에서 PR 플레인으로 응답하고, 트래커에만 있는 부분은 누락된 것으로 표시합니다.

<a id="limits-and-failure-modes"></a>

## 한계와 실패 모드

- **기록이 없으면 답도 없습니다.** `LOOM_CODE_PR_HISTORY_ENABLED`가 꺼져 있고 `backfill-prs`도 실행하지 않았다면 기록 테이블이 비어 있어, op는 순위를 매길 대상이 없는 상태로 응답합니다.
- **익명 GitHub 접근**은 공개 속도 제한 범위 내에서 공개 저장소만 다룹니다. 리뷰 댓글과 이슈 미러에는 토큰이 필요합니다.
- **열린 PR은 인덱싱된 코드가 아닙니다.** `code.pr_check`는 열린 PR의 제목과 파일만 볼 수 있으며, 본문이나 심볼은 보지 못합니다.
- **휴리스틱 판정.** `code.pr_check`는 의도적으로 보정되지 않았습니다. `code.blame_range`는 "마지막으로 수정된 시점"에 대해서만 정확합니다. `code.bisect_candidates`는 용의 대상의 순위를 매길 뿐, 원인 제공자를 사실로 단정하지 않습니다.
- **트래커 장애는 성능 저하로 처리되며 실패로 이어지지 않습니다.** 트래커 읽기가 시간 초과되거나 차단기가 작동하면 `tracker_unavailable`이 설정되며, PR 플레인 쪽 처리는 계속 응답합니다.

<a id="related"></a>

## 관련 문서

- [트래커 개요](https://github.com/ClickHouse/loom/blob/main/docs/features/tracker/overview.md) 및 [레코드 계약](https://github.com/ClickHouse/loom/blob/main/docs/features/tracker/record-contract.md)
- [저장소 추가하기](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/repositories.md)
- [질문별 코드 인덱스 작업](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/operations.md)
- [코드 인덱스 아키텍처](../../architecture/code-index-architecture.md)
- [작업 레퍼런스](https://github.com/ClickHouse/loom/blob/main/docs/reference/operations.md#code)
