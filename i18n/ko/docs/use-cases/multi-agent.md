<!-- i18n: source=docs/use-cases/multi-agent.md blob=d147692b665800cf10e0dfda74f2fdc9f9de8293 model=claude-sonnet-5-5 -->
> 이 페이지는 [영어 원문](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/multi-agent.md)을 기계 번역한 것이며, 영어 원문이 기준입니다.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/multi-agent.md), which is authoritative.

<a id="shared-context-for-multi-agent-workflows"></a>

# 멀티 에이전트 워크플로를 위한 공유 컨텍스트

Loom은 멀티 에이전트 워크플로의 에이전트들이 컨텍스트를 공유할 수 있는 한 곳을 제공합니다. 네임스페이스의 모든 에이전트가 읽는 메모리, 한 에이전트에서 다른 에이전트로 작업을 넘기는 작업 인박스, 그리고 결정 기록입니다. 워크플로는 에이전트와 오케스트레이터가 실행하고, Loom은 에이전트가 알게 된 것과 넘겨준 내용을 보관합니다.

<a id="what-you-get"></a>

## 제공 기능

- **출처가 있는 공유 메모리.** 모든 행에는 어떤 에이전트가 작성했는지 기록되며, 기본 `auto_reinforce` 정책에서는 두 에이전트 사이의 모순이 조용히 덮어써지지 않고 검토 대상으로 표시됩니다. [네임스페이스와 공유](https://github.com/ClickHouse/loom/blob/main/docs/features/namespaces-and-sharing.md) ·
  [네임스페이스 충돌 정책](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/conflicts-and-refusal.md#namespace-conflict-policy)
- **작업 인박스.** 각 에이전트는 리스, 재시도, 검토 규칙을 갖춘 영속 큐를 가집니다. 가져간 작업은 한 번에 한 명의 소유자만 가집니다. [작업, 결정, 정책](https://github.com/ClickHouse/loom/blob/main/docs/features/tasks-and-decisions.md)
- **결정.** 이와 같은 경우에 무엇을 결정했는지, 이유는 무엇인지, 효과가 있었는지를 담습니다.
  [결정](https://github.com/ClickHouse/loom/blob/main/docs/features/tasks-and-decisions.md#decisions)
- **작업 메모리.** 검색으로 찾는 대신 에이전트에 매번 제공되는 텍스트로, 장기 메모리와는 별개입니다. [작업 메모리와 프로필](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/working-memory-and-profiles.md)
- **컨텍스트 팩.** `context.pack`은 에이전트가 작업을 시작할 때 불러오는, 크기가 제한된 번들을 만듭니다. [통합 검색과 컨텍스트 팩](https://github.com/ClickHouse/loom/blob/main/docs/features/unified-search.md)
- **세션 브리프.** 하나의 작업 흐름에서 이어서 진행할 수 있는 상태로, 목표, 현황, 결정, 막다른 길, 완료된 내용을 담습니다. 한 에이전트가 `session.brief_save`로 저장하면 다음 에이전트가 `session.brief_get`으로 불러옵니다. [세션 브리프](https://github.com/ClickHouse/loom/blob/main/docs/features/conversations-and-sessions.md#session-briefs)
- **프레임워크 어댑터.** SDK는 에이전트 프레임워크 고유의 메모리 인터페이스를 통해 Loom을 제공합니다. [에이전트 프레임워크](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/frameworks.md)

<a id="two-agents-sharing-a-research-note"></a>

## 두 에이전트가 리서치 노트를 공유하기

한 사람의 노트북에서는 [단일 사용자 퀵스타트](../getting-started/quickstart.md)로 연결된 두 에이전트가 같은 네임스페이스를 읽도록 할 수 있습니다. [연결 가이드](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md)에 따라 두 클라이언트를 모두 연결하세요. 예를 들어 Claude Code로 조사 결과를 모으고 Cursor로 그 내용을 바탕으로 글을 씁니다.

1. 첫 번째 클라이언트에 `loom_memory_set`을 사용해 `example/research/next-step`을 값 “Check the paper's evaluation dataset before adding it to the reading list.”로 저장하도록 요청합니다. 반환된 `memory_id`를 확인하고 대시보드에서 해당 행을 찾습니다.
2. 다른 클라이언트에 `loom_memory_get`을 사용해 정확히 그 키를 읽도록 요청합니다. 다음 단계 초안을 작성하기 전에 `found`, `value`, `agent`를 포함한 도구 응답을 요구하세요. 이 클라이언트는 첫 번째 클라이언트가 저장한 값을 읽고 작성자를 식별해야 합니다.
3. 끝나면 해당 키를 작성한 클라이언트에서 예시 키를 삭제합니다.

에이전트들은 저장된 사실을 공유할 뿐이며, Loom이 에이전트를 실행하거나 작업을 스케줄링하지는 않습니다. 서로 다른 사람의 머신에서 실행되는 에이전트에는 [인증이 적용된 팀 배포](../getting-started/quickstart.md#start-in-multi-user-mode)를 사용하고, 각 사람의 토큰으로 같은 네임스페이스에 대한 접근 권한을 부여하세요. 로컬 단일 사용자 실행으로는 이러한 접근 제어를 테스트할 수 없습니다.

<a id="start"></a>

## 시작하기

[에이전트에 Loom 통합하기](https://github.com/ClickHouse/loom/blob/main/docs/guides/agent-integration.md)에서는 각 에이전트가 보내는 신원과 기록할 내용을 다룹니다. [토큰과 권한 부여](https://github.com/ClickHouse/loom/blob/main/docs/guides/tokens-and-grants.md)에서는 에이전트마다 고유한 토큰을 부여하는 방법을 설명합니다.

이 페이지의 일부 op는 [상태 페이지](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md)에서 실험적 기능으로 표시되어 있습니다.
