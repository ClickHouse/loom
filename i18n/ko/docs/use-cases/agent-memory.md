<!-- i18n: source=docs/use-cases/agent-memory.md blob=e9322c4d7b4b3395943f3caead94d6436cd0954b model=claude-sonnet-5-5 -->
> 이 페이지는 [영어 원문](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/agent-memory.md)을 기계 번역한 것이며, 영어 원문이 기준입니다.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/agent-memory.md), which is authoritative.

<a id="long-term-and-shared-memory-for-ai-agents"></a>

# AI 에이전트를 위한 장기 및 공유 메모리

Loom은 AI 에이전트에 서로 공유하는 장기 메모리를 제공하는 자체 호스팅 MCP 서버이자 HTTP API입니다. 한 에이전트가 세션에서 저장한 사실은 다음 세션, 다른 에이전트, 또는 대시보드를 보는 사람이 찾을 수 있습니다. 이 페이지에서는 그 동작 방식과 시작 위치를 설명합니다.

<a id="what-you-get"></a>

## 제공하는 기능

- **네임스페이스 안에서 키마다 하나의 주장.** `memory.set`은 사실을 저장하고, `memory.get`은 키로 다시 읽으며, `memory.search`와 `loom_search`는 의미로 찾습니다. 네임스페이스는 공유의 단위이며, 기본적으로 모든 멤버가 그 안의 모든 행을 읽습니다.
  [메모리](https://github.com/ClickHouse/loom/blob/main/docs/concepts/memory.md)
- **수정할 수 있는 메모리.** 변경하면 이력이 있는 새 버전이 만들어집니다. 정규화 후 같은 사실은 저장된 행에 병합되고, 표현만 다른 같은 사실은 중복으로 저장되며, 모순은 검토 대상으로 표시되고, 고정된 사실에 대한 쓰기는 거부됩니다.
  [충돌과 거부](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/conflicts-and-refusal.md) ·
  [이력과 출처](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/history-and-provenance.md)
- **답을 보류하는 검색.** 유사도 하한을 넘는 결과가 없으면 `memory.search`는 사유 코드와 함께 `abstain=true`로 응답하고 약한 결과마다 `below_threshold` 라벨을 붙입니다. `loom_search`는 그런 결과를 제외합니다. [검색과 질의](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/search-and-ask.md)
- **에이전트 간에 공유되는 코딩 에이전트용 장기 메모리.** Claude Code와 Codex 플러그인은 세션을 시작할 때 관련 컨텍스트를 불러오고 세션이 진행되는 동안 내용을 캡처합니다. Cursor, GitHub Copilot, Gemini CLI, Cline, Kilo Code, OpenCode, goose, Zed 및 기타 MCP 클라이언트도 같은 서버에 연결되며, 네임스페이스에 속한 에이전트는 다른 에이전트가 그곳에 저장한 사실을 찾을 수 있습니다.
  [코딩 에이전트 연결](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md) ·
  [MCP 클라이언트](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/mcp-clients.md)
- **관련된 코드에 연결된 메모리.** 코딩 에이전트가 얻은 교훈은 이름 변경과 수정을 거쳐도 해당 정의를 따라가며, 에이전트가 그 코드를 읽을 때 나타납니다. [코드에 연결된 메모리](memory-linked-to-code.md)
- **자체 에이전트를 위한 사용자별 메모리.** Python 및 TypeScript SDK는 최종 사용자별로 메모리를 쓰고 읽습니다. [첫 에이전트 만들기](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/first-agent.md)

<a id="measured"></a>

## 측정 결과

같은 `gpt-5` 리더를 사용한 100개의 짝지은 LongMemEval-S 질문에서, Loom이 검색한 메모리로 만든 답변은 전체 기록으로 만든 답변의 90%보다 높은 94%를 기록했고, 리더 프롬프트 토큰은 91% 적었습니다(메모리를 구축하는 일회성 비용 제외). 정확도 차이는 통계적으로 입증되지 않았습니다.
[Loom 사용 여부 비교](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#with-and-without-loom)

<a id="a-research-assistant-that-remembers-your-reading-preferences"></a>

## 읽기 선호도를 기억하는 리서치 어시스턴트

[빠른 시작](../getting-started/quickstart.md)을 마친 후 대시보드의 [클라이언트 설정](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md#how-do-i-give-cursor-or-another-mcp-client-shared-memory)을 통해 Claude Desktop 또는 Cursor를 연결합니다. 이 도구들은 기본 LLM으로 동작하며, 도구를 호출하는 모델은 채팅 앱이 제공합니다.

1. 어시스턴트에게 이렇게 말합니다. “`loom_memory_set`을 사용해 내 리서치 선호도를 `example/research/source-policy` 아래에 저장해 줘: 내 읽기 목록에 동료 검토를 거친 논문을 포함할 것.”
2. 도구 응답에 `memory_id`가 포함되어 있는지 확인합니다. 대시보드에서 **Memories**를 열어 값과 작성자를 확인합니다. 도구가 쓰기를 거부하면 보고된 문제를 해결한 후 계속합니다.
3. 새 채팅을 시작합니다. 어시스턴트에게 이렇게 말합니다. “`loom_memory_get`을 사용해 `example/research/source-policy`를 읽고, 내 읽기 목록에 어떤 출처가 들어가야 하는지 알려줘.”
   도구가 `found: true`와 저장한 선호도를 반환하는지 확인합니다.
4. 마치면 어시스턴트에게 해당 예제 키를 삭제하도록 요청하고, 대시보드에 더 이상 활성 메모리로 표시되지 않는지 확인합니다.

두 번째 채팅은 첫 번째 채팅의 대화 내용 없이 저장된 선호도를 읽습니다. 이는 지속성과 클라이언트 연결을 확인하는 것이며, 어시스턴트가 논문을 판단하거나 리서치 질문에 답할 수 있는지를 시험하는 것은 아닙니다. 논문 자체를 검색하려면 [문서 검색](document-search.md)을 사용하세요.

<a id="start"></a>

## 시작하기

[빠른 시작](../getting-started/quickstart.md)에서는 로컬 스택을 띄우고 HTTP, Python, MCP로 첫 메모리를 저장합니다. [Loom이 맞지 않는 경우](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/overview.md#when-it-does-not-fit)에서는 인프로세스 메모리 라이브러리가 더 적합한 경우를 다룹니다.

이 페이지의 일부 op는 [상태 페이지](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md)에서 실험적으로 표시되어 있습니다.

[Microsoft Agent Framework 컨텍스트 프로바이더](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/frameworks.md#microsoft-agent-framework)를 사용해 실행 전에 사용자 사실을 불러오고 실행 후에 대화를 기록하세요.

[Claude API 메모리 도구](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/frameworks.md#claude-api-memory-tool)의 파일 트리를 Loom에 저장하면 네임스페이스의 에이전트가 그 내용을 공유할 수 있습니다.

[OpenAI Agents 세션 기록](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/frameworks.md#openai-agents-sdk)을 에이전트 실행과 핸드오프 전반에 걸쳐 Loom에 보관하세요.

시각적 워크플로에는 [n8n 노드](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/frameworks.md#n8n)가 AI Agent를 Loom의 대화 기록, 영구 메모리, 지식 검색에 연결합니다.
