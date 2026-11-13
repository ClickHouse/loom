<!-- i18n: source=docs/use-cases/code-intelligence.md blob=fac5260cc9d02070903874ade5298d5ecff95137 model=claude-sonnet-5-5 -->
> 이 페이지는 [영어 원문](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/code-intelligence.md)을 기계 번역한 것이며, 영어 원문이 기준입니다.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/code-intelligence.md), which is authoritative.

<a id="a-code-index-for-coding-agents"></a>

# 코딩 에이전트를 위한 코드 인덱스

Loom은 코딩 에이전트에게 git 저장소의 공유 인덱스를 MCP로 제공합니다. 에이전트는 파일 전체를 읽는 대신 구조에 대한 질문을 던지고, 그 답이 되는 라인만 받아 옵니다. 저장소를 추가해 두어야 하며 `LOOM_CODE_INGEST_ENABLED=true`가 필요합니다. 인덱스를 구축하고 최신 상태로 유지하는 방식은 [코드 인덱스 개요](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md)에서 설명합니다.

<a id="what-you-get"></a>

## 제공 기능

- **정의, 호출자, 영향 범위.** `code.symbol`, `code.callers`, `code.impact`는
  파일과 줄 번호를 함께 답하며, 각 답변에는 근거가 된 커밋과 인덱스 티어가 명시됩니다.
  [Code operations](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/operations.md)
- **의미 기반 검색과 정확한 문자열 검색.** `code.search`는 특정 메커니즘이 어디에 있는지 찾고,
  `code.grep`은 정확한 메시지나 식별자를 찾습니다.
- **히스토리와 pull request.** 리뷰 코멘트가 포함된 병합된 PR 히스토리, PR 작성 전 중복 확인,
  회귀를 일으킨 커밋 후보(bisect candidates)를 제공합니다. 이 기능을 쓰려면 PR 히스토리를 켜야 하며
  (`LOOM_CODE_PR_HISTORY_ENABLED`), 리뷰 코멘트와 열린 PR을 보려면 GitHub 토큰도 필요합니다.
  각 기능에 무엇이 필요한지는 [History, PRs and releases](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/history-and-prs.md)에서 확인할 수 있습니다.
- **눈으로 확인할 수 있는 최신성.** 인덱스는 병합된 커밋 수만큼 브랜치보다 뒤처질 수 있으며,
  오래된 답변에는 그 사실이 표시됩니다. [Freshness](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/freshness.md)
- **플러그인.** Claude Code 플러그인은 `coding` toolset으로, Codex 플러그인은
  `core,coding,tracker,tasks`로 연결되며, 둘 다 코드 도구를 나열합니다. Cursor와 그 밖의 MCP 클라이언트는
  `?toolset=coding`을 사용합니다.
  [Connect a coding agent](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md)

<a id="measured"></a>

## 측정 결과

ClickHouse 저장소에서 Loom을 함께 사용한 Claude Code는 개념 질문 36개(짝지은 실행 10회) 중 96.4%에서 세 개의 후보 파일 가운데 올바른 파일을 짚었고, Loom 없이 실행했을 때는 91.4%였습니다. 모델 비용은 21% 낮았습니다(에이전트의 모델 비용 기준이며, Loom 자체의 모델 호출은 제외).
SWE-bench Verified 이슈 200건에서 `code.search`는 82%의 이슈에 대해 상위 10개 결과 안에 변경된 파일을 올려놓았습니다(이슈 해결이 아닌 파일 위치 특정 기준). 색인된 모든 파일을 대상으로 한 BM25는 57%, `.py` 파일만 대상으로 한 BM25는 79%였으며, `.py` 파일만 대상으로 한 경우와의 차이는 오차 범위 안입니다.
[Loom 사용 여부별 비교](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#with-and-without-loom) ·
[SWE-bench Verified 파일 위치 특정](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#swe-bench-verified-file-localization)

<a id="start"></a>

## 시작하기

[저장소 추가](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/repositories.md)는 저장소에 대해 인덱스를 활성화합니다. 티어에 대한 설명은 [코드 인덱스 개요](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md)를 참고하세요.

이 페이지의 일부 op는 [상태 페이지](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md)에서 실험적(experimental) 기능으로 표시되어 있습니다.
