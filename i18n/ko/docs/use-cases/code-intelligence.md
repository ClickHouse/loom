<!-- i18n: source=docs/use-cases/code-intelligence.md blob=4f52e242dcb612150541843af58b15f37c31a265 model=claude-sonnet-5-5 -->
> 이 페이지는 [영어 원문](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/code-intelligence.md)을 기계 번역한 것이며, 영어 원문이 기준입니다.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/code-intelligence.md), which is authoritative.

<a id="a-code-index-for-coding-agents"></a>

# 코딩 에이전트를 위한 코드 인덱스

Loom은 코딩 에이전트에게 git 리포지토리의 공유 인덱스를 MCP로 제공합니다. 에이전트는 구조에 관한 질문을 던지고, 파일 전체를 읽는 대신 답이 되는 줄만 받습니다. 리포지토리가 추가되어 있어야 하며 `LOOM_CODE_INGEST_ENABLED=true`가 필요합니다. 인덱스를 만들고 최신 상태로 유지하는 방법은 [코드 인덱스 개요](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md)에, 언어별로 제공하는 기능은 [언어별 코드 인덱스](code-intelligence-by-language.md)에 있습니다.

<a id="what-you-get"></a>

## 제공 기능

- **정의, 호출자, 영향 범위.** `code.symbol`, `code.callers`, `code.impact`는 파일과 줄 번호로 답하며, 각 답변에는 근거가 된 커밋과 인덱스 티어가 함께 표시됩니다. [코드 작업](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/operations.md)
- **의미 기반 검색과 정확한 문자열 검색.** `code.search`는 어떤 동작이 구현된 위치를 찾고, `code.grep`은 정확한 메시지나 식별자를 찾습니다.
- **히스토리와 풀 리퀘스트.** 리뷰 코멘트가 포함된 병합된 PR 히스토리, PR 전 중복 확인, 회귀를 추적하기 위한 bisect 후보를 제공합니다. 이 기능들은 PR 히스토리를 켜야 하며(`LOOM_CODE_PR_HISTORY_ENABLED`), 리뷰 코멘트와 열린 PR에는 GitHub 토큰도 필요합니다. 각 기능에 필요한 조건은 [히스토리, PR, 릴리스](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/history-and-prs.md)에 설명되어 있습니다.
- **눈으로 확인할 수 있는 최신성.** 인덱스는 병합된 커밋 단위로 브랜치를 뒤따르며, 오래된 답변에는 그 사실이 표시됩니다. [최신성](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/freshness.md)
- **플러그인.** Claude Code 플러그인은 `coding` 툴셋으로, Codex 플러그인은 `core,coding,tracker,tasks`로 연결되며 둘 다 코드 도구를 나열합니다. Cursor와 다른 MCP 클라이언트는 `?toolset=coding`을 사용합니다.
  [코딩 에이전트 연결](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md)

<a id="measured"></a>

## 측정 결과

ClickHouse 리포지토리에서 Claude Code에게 각 답변을 최대 세 개의 파일로 마무리하도록 요청했습니다. Loom을 사용했을 때 개념 질문 36개(짝지은 실행 10회)에서 올바른 파일을 지목한 비율은 96.4%였고, Loom 없이는 91.4%였습니다. Loom을 사용한 실행은 비용이 21% 적게 들었습니다(에이전트의 모델 비용 기준이며 Loom 자체의 모델 호출은 제외).
SWE-bench Verified 이슈 200개에서 `code.search`는 82%의 경우 변경된 파일을 상위 10개 결과 안에 올렸습니다(이슈 해결이 아니라 파일 위치 특정 기준). 인덱스된 모든 파일에 대한 BM25는 57%, `.py` 파일만 대상으로 한 BM25는 79%였으며, `.py` 전용과의 차이는 오차 범위 안입니다.
[Loom 사용 여부 비교](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#with-and-without-loom) ·
[SWE-bench Verified 파일 위치 특정](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#swe-bench-verified-file-localization)

<a id="start"></a>

## 시작하기

[리포지토리 추가](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/repositories.md)에서 리포지토리의 인덱스를 켤 수 있으며, 티어에 대한 설명은 [코드 인덱스 개요](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md)에 있습니다.

이 페이지의 일부 작업은 [상태 페이지](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md)에서 실험적 기능으로 표시되어 있습니다.
