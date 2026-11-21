<!-- i18n: source=docs/use-cases/code-intelligence.md blob=9f10f6e9bc93b39e97d3aee93cedeeb7260a7297 model=claude-sonnet-5-5 -->
> 이 페이지는 [영어 원문](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/code-intelligence.md)을 기계 번역한 것이며, 영어 원문이 기준입니다.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/code-intelligence.md), which is authoritative.

<a id="a-code-index-for-coding-agents"></a>

# 코딩 에이전트를 위한 코드 인덱스

Loom은 코딩 에이전트에게 git 리포지토리의 공유 인덱스를 MCP로 제공합니다. 에이전트는 파일 전체를 읽는 대신 구조에 관한 질문을 하고, 그 답이 되는 줄만 받습니다. 리포지토리가 추가되어 있어야 하고 `LOOM_CODE_INGEST_ENABLED=true`가 필요합니다. 인덱스를 만들고 최신 상태로 유지하는 방법은 [코드 인덱스 개요](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md)에, 언어별로 제공되는 기능은 [언어별 코드 인덱스](code-intelligence-by-language.md)에 있습니다.

<a id="what-you-get"></a>

## 제공 기능

- **정의, 호출자, 영향 범위.** `code.symbol`, `code.callers`, `code.impact`는 파일과 줄 번호 참조와 함께 코드의 위치를 찾습니다. `code.index_status`는 각 티어의 인덱싱된 커밋을 보고합니다. [코드 작업](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/operations.md)
- **의미 기반 검색과 정확한 문자열 검색.** `code.search`는 특정 메커니즘이 있는 위치를 찾고, `code.grep`은 정확한 메시지나 식별자를 찾습니다.
- **기록과 풀 리퀘스트.** 리뷰 코멘트가 포함된 병합된 PR 기록, PR 작성 전 중복 확인, 회귀를 일으킨 bisect 후보를 제공합니다. 이 기능들은 PR 기록을 켜야 합니다(`LOOM_CODE_PR_HISTORY_ENABLED`). 리뷰 코멘트와 열린 PR에는 GitHub 토큰도 필요합니다. [기록, PR, 릴리스](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/history-and-prs.md)에서 각 기능에 필요한 조건을 설명합니다.
- **코드에 연결된 교훈.** `code.symbol`은 정의에 연결된 메모리를 반환합니다. `code.search`, `code.callers`, `code.callees`, `code.impact`, `code.outline`, 그리고 작업의 `code.context_pack`은 반환하는 파일에 대한 교훈을 함께 붙이며, 각 교훈에는 해당 코드가 이동했는지, 변경되었는지, 사라졌는지가 표시됩니다. [코드에 연결된 메모리](memory-linked-to-code.md)
- **확인할 수 있는 최신성.** 각 티어는 브랜치보다 뒤처질 수 있습니다. `as_of`가 반환되면 이를 확인하고, 그렇지 않으면 `code.index_status`로 해당 티어의 워터마크를 확인하십시오. 최신성 메타데이터가 없으면 알 수 없는 상태입니다. [최신성](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/freshness.md)
- **플러그인과 MCP 클라이언트.** Claude Code 플러그인은 `coding` 툴셋으로, Codex 플러그인은 `core,coding,tracker,tasks`로 연결되며, 둘 다 코드 도구를 나열합니다. Cursor, GitHub Copilot, Gemini CLI, Cline, Kilo Code, OpenCode 및 기타 MCP 클라이언트는 URL에 `?toolset=coding`을 붙이면 코드 도구를 나열합니다(`loom install`은 Cursor에 이를 추가하고, `--toolset coding`은 그 외에 설정 파일을 작성하는 클라이언트에 추가합니다).
  [코딩 에이전트 연결](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md) ·
  [MCP 클라이언트](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/mcp-clients.md)

<a id="measured"></a>

## 측정 결과

ClickHouse 리포지토리에서 Claude Code에게 각 답변의 끝에 최대 세 개의 파일을 제시하도록 요청했습니다. Loom을 사용했을 때 개념 질문 36개(짝지은 실행 10회)에서 올바른 파일을 지목한 비율은 96.4%였고, Loom 없이는 91.4%였습니다. 또한 Loom을 사용한 실행의 비용은 21% 낮았습니다(에이전트의 모델 비용 기준이며, Loom 자체의 모델 호출은 제외).
SWE-bench Verified 이슈 200개에서 `code.search`는 82%의 경우 변경된 파일을 상위 10개 결과에 올렸습니다(이슈 해결이 아니라 파일 위치 특정). 인덱싱된 모든 파일에 대한 BM25는 57%, `.py` 파일만 대상으로 한 BM25는 79%였으며, `.py` 전용과의 차이는 오차 범위 안입니다.
[Loom 사용 시와 미사용 시](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#with-and-without-loom) ·
[SWE-bench Verified 파일 위치 특정](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#swe-bench-verified-file-localization)

<a id="a-coding-agent-that-reads-the-definition-it-needs"></a>

## 필요한 정의를 읽는 코딩 에이전트

신뢰할 수 있는 리포지토리로 시작하십시오. 릴리스된 스택에서는 [운영자 구성 인덱스 단계](../getting-started/quickstart.md#optional-index-a-repository-on-the-released-stack)를 따르십시오. 대시보드의 리포지토리 추가 흐름에는 해당 스택에서 활성화되지 않는 샌드박스가 필요합니다. 플러그인으로 Claude Code 또는 Codex를 연결하고 그 리포지토리의 체크아웃 안에서 세션을 시작하십시오([리포지토리 매칭](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md#when-the-code-index-is-used)).

1. 에이전트에게 `op: "code.index_status"`와 `arguments: {"namespace": "<indexed namespace>", "brief": true}`로 `loom_op`를 호출하도록 요청하십시오. 빠른 티어의 인덱싱된 커밋인 `git_head`를 기록하십시오. 값이 비어 있으면 해당 티어에 워터마크가 없다는 뜻입니다.
2. 정의를 알고 있는 함수를 하나 고르십시오. 에이전트에게 해당 네임스페이스에서 함수 이름과 `include_body: true`로 `loom_code_symbol`을 호출하도록 요청하십시오. 반환된 `path`, `start_line`, `end_line`, `body`, `tier`를 확인하십시오.
3. 이 확인은 `tier: "treesitter"`이고 본문이 완전하며 생략되지 않은, 모호하지 않은 정의 하나에 대해서만 계속하십시오. 의존하기 전에 그 경로, 범위, 본문을 기록해 둔 `git_head` 시점의 소스와 비교하십시오. 워터마크나 본문이 없거나, 정의가 다르거나, 다른 티어가 반환되면 로컬에서 읽으십시오. 컴파일러 티어는 각자 독립적으로 진행되므로 워터마크에 대해 별도의 리비전 확인이 필요합니다([최신성](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/freshness.md#the-mirrors-own-age)).
4. 같은 체크아웃에서 새 세션을 시작하고 두 호출을 반복하십시오. 인덱스가 정의를 다시 제공하므로, 에이전트가 이전 대화나 파일 전체를 불러올 필요가 없습니다.

이 절차는 빠른 티어의 정의 하나만 확인합니다. 커밋되지 않은 편집은 여전히 로컬에서 읽어야 하며, 조회에 성공했다고 해서 작업 정확도나 비용 절감을 측정한 것은 아닙니다. 검색, 호출자, 영향 범위는 [코드 작업](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/operations.md)을 참고하십시오.

<a id="start"></a>

## 시작하기

[리포지토리 추가](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/repositories.md)는 리포지토리에 대해 인덱스를 켭니다. [코드 인덱스 개요](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md)에서는 티어를 설명합니다.

이 페이지의 일부 op는 [상태 페이지](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md)에서 실험적으로 표시되어 있습니다.
