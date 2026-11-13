<!-- i18n: source=docs/use-cases/agent-memory.md blob=7278a12f3a940bfba3a0dead9a5e37fa445bb68a model=claude-sonnet-5-5 -->
> 이 페이지는 [영어 원문](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/agent-memory.md)을 기계 번역한 것이며, 영어 원문이 기준입니다.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/agent-memory.md), which is authoritative.

<a id="long-term-and-shared-memory-for-ai-agents"></a>

# AI 에이전트를 위한 장기 및 공유 메모리

Loom은 AI 에이전트가 함께 사용하는 장기 메모리를 제공하는 셀프 호스팅 MCP 서버이자 HTTP API입니다. 한 에이전트가 세션에서 저장한 사실은 다음 세션, 다른 에이전트, 또는 대시보드를 보는 사람이 찾을 수 있습니다. 이 페이지에서는 그 동작 방식과 시작할 위치를 설명합니다.

<a id="what-you-get"></a>

## 제공 기능

- **네임스페이스 안에서 키당 하나의 클레임.** `memory.set`은 사실을 저장하고, `memory.get`은 키로 다시 읽어 오며, `memory.search`와 `loom_search`는 의미로 찾습니다. 네임스페이스는 공유의 단위입니다. 기본적으로 모든 멤버가 네임스페이스 안의 모든 행을 읽습니다.
  [Memory](https://github.com/ClickHouse/loom/blob/main/docs/concepts/memory.md)
- **수정할 수 있는 메모리.** 변경하면 이력이 있는 새 버전이 만들어집니다. 정규화(canonicalisation) 후 같은 사실은 저장된 행에 병합되고, 표현만 다른 같은 사실은 중복으로 저장되며, 모순되는 내용은 검토 대상으로 표시되고, 고정(pinned)된 사실에 대한 쓰기는 거부됩니다.
  [Conflicts and refusal](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/conflicts-and-refusal.md) ·
  [History and provenance](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/history-and-provenance.md)
- **답을 보류하는 검색.** 유사도 하한을 넘는 결과가 없으면 `memory.search`는 사유 코드와 함께 `abstain=true`로 응답하고, 약한 결과마다 `below_threshold` 레이블을 붙입니다. `loom_search`는 이런 결과를 제외합니다. [Search and ask](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/search-and-ask.md)
- **코딩 에이전트 간 메모리.** Claude Code와 Codex 플러그인은 세션 시작 시 관련 컨텍스트를 불러오고 세션이 진행되는 동안 내용을 기록합니다. Cursor와 그 밖의 MCP 클라이언트도 같은 서버에 연결됩니다. [Connect a coding agent](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md)
- **직접 만든 에이전트를 위한 사용자별 메모리.** Python 및 TypeScript SDK는 최종 사용자별로 메모리를 쓰고 읽습니다. [Your first agent](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/first-agent.md)

<a id="measured"></a>

## 측정 결과

동일한 `gpt-5` 리더를 사용해 LongMemEval-S 질문 100개를 쌍으로 비교한 결과, Loom이 검색한 메모리로 답변했을 때의 점수는 94%였고 전체 기록을 사용했을 때는 90%였습니다. 리더 프롬프트 토큰은 91% 적게 사용했습니다(메모리를 구축하는 1회성 비용은 제외). 정확도 차이는 통계적으로 입증되지 않았습니다.
[Loom 사용 시와 미사용 시](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#with-and-without-loom)

<a id="start"></a>

## 시작하기

[Quickstart](../getting-started/quickstart.md)에서는 로컬 스택을 띄우고 HTTP, Python, MCP로 첫 메모리를 저장합니다. [Loom이 맞지 않는 경우](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/overview.md#when-it-does-not-fit)에서는 프로세스 내 메모리 라이브러리가 더 적합한 경우를 다룹니다.

이 페이지의 일부 op는 [상태 페이지](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md)에서 experimental로 표시되어 있습니다.
