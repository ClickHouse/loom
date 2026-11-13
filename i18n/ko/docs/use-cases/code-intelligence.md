<!-- i18n: source=docs/use-cases/code-intelligence.md blob=6fd1469bda94290a1ca6a632c41c8fb026085a22 model=claude-sonnet-5-5 -->
> 이 페이지는 [영어 원문](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/code-intelligence.md)을 기계 번역한 것이며, 영어 원문이 기준입니다.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/code-intelligence.md), which is authoritative.

<a id="a-code-index-for-coding-agents"></a>

# 코딩 에이전트를 위한 코드 인덱스

Loom은 코딩 에이전트에게 git 리포지토리의 공유 인덱스를 MCP로 제공합니다. 에이전트는 파일 전체를 읽는 대신 구조에 관한 질문을 던지고, 그 답이 되는 줄만 받아 볼 수 있습니다. 리포지토리가 추가되어 있어야 하고 `LOOM_CODE_INGEST_ENABLED=true`가 설정되어 있어야 합니다. 인덱스를 만들고 최신 상태로 유지하는 방법은 [코드 인덱스 개요](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md)에서 설명합니다.

<a id="what-you-get"></a>

## 제공하는 기능

- **정의, 호출자, 영향 범위.** `code.symbol`, `code.callers`, `code.impact`는
  파일과 줄 번호를 함께 답하며, 각 답변에는 근거가 된 커밋과 인덱스 티어가 명시됩니다.
  [Code operations](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/operations.md)
- **의미 기반 검색과 정확한 문자열 검색.** `code.search`는 특정 메커니즘이 어디에 있는지 찾고,
  `code.grep`은 정확한 메시지나 식별자를 찾습니다.
- **히스토리와 풀 리퀘스트.** 리뷰 코멘트가 포함된 머지된 PR 히스토리, PR 작성 전 중복 확인,
  회귀 원인을 찾기 위한 bisect 후보를 제공합니다. 이 기능을 쓰려면 PR 히스토리를 켜야 하며
  (`LOOM_CODE_PR_HISTORY_ENABLED`), 리뷰 코멘트와 열려 있는 PR에는 GitHub 토큰도 필요합니다.
  각 기능에 필요한 조건은 [History, PRs and releases](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/history-and-prs.md)에서 확인할 수 있습니다.
- **확인할 수 있는 최신성.** 인덱스는 브랜치보다 머지된 커밋 수만큼 뒤처질 수 있으며, 오래된 답변에는 그 사실이 표시됩니다.
  [Freshness](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/freshness.md)
- **플러그인.** Claude Code 플러그인은 `coding` 툴셋으로, Codex 플러그인은 `core,coding,tracker,tasks`로 연결하며,
  두 플러그인 모두 코드 도구를 나열합니다. Cursor 등 다른 MCP 클라이언트는 `?toolset=coding`을 사용합니다.
  [Connect a coding agent](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md)

<a id="measured"></a>

## 측정 결과

ClickHouse 저장소에서 Claude Code는 각 답변을 파일 세 개로 마무리합니다. Loom을 사용하면 개념 질문 36개(10회 쌍 실행) 중 96.4%에서 정답 파일이 그 세 개 안에 들어 있었고, Loom을 사용하지 않았을 때는 91.4%였으며, 모델 비용은 21% 낮았습니다(에이전트의 모델 비용 기준이며, Loom 자체의 모델 호출은 제외).
SWE-bench Verified 이슈 200개에서 `code.search`는 82%의 이슈에 대해 변경된 파일을 상위 10개 결과 안에 올렸습니다(이슈 해결이 아니라 파일 위치 탐색 기준). 색인된 모든 파일에 대한 BM25는 57%, `.py` 파일만 대상으로 한 BM25는 79%였으며, `.py`만 대상으로 한 경우와의 차이는 오차 범위 안입니다.
[Loom 사용 여부별 비교](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#with-and-without-loom) ·
[SWE-bench Verified 파일 위치 탐색](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#swe-bench-verified-file-localization)

<a id="start"></a>

## 시작하기

[리포지토리 추가](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/repositories.md)를 하면 해당 리포지토리의 인덱스가 켜집니다. [코드 인덱스 개요](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md)에서 티어를 설명합니다.

이 페이지의 일부 op는 [상태 페이지](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md)에서 experimental로 표시되어 있습니다.
