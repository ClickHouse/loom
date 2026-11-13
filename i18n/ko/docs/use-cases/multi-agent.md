<!-- i18n: source=docs/use-cases/multi-agent.md blob=a7e1e817573b25059be35ee63c5f9ec80b03af73 model=claude-sonnet-5-5 -->
> 이 페이지는 [영어 원문](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/multi-agent.md)을 기계 번역한 것이며, 영어 원문이 기준입니다.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/multi-agent.md), which is authoritative.

<a id="shared-context-for-multi-agent-workflows"></a>

# 멀티 에이전트 워크플로를 위한 공유 컨텍스트

Loom은 멀티 에이전트 워크플로의 에이전트들이 컨텍스트를 공유할 수 있는 하나의 공간을 제공합니다. 네임스페이스 안의 모든 에이전트가 읽는 메모리, 한 에이전트에서 다른 에이전트로 작업을 넘기는 작업 인박스, 그리고 결정 기록이 그것입니다. 워크플로는 사용자의 에이전트와 오케스트레이터가 실행하고, Loom은 그들이 배운 내용과 넘겨준 내용을 보관합니다.

<a id="what-you-get"></a>

## 제공 기능

- **출처가 기록되는 공유 메모리.** 모든 행에는 어떤 에이전트가 작성했는지가 기록되며, 기본 `auto_reinforce` 정책에서는 두 에이전트 간의 모순이 조용히 덮어쓰이지 않고 검토 대상으로 표시됩니다. [네임스페이스와 공유](https://github.com/ClickHouse/loom/blob/main/docs/features/namespaces-and-sharing.md) ·
  [네임스페이스 충돌 정책](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/conflicts-and-refusal.md#namespace-conflict-policy)
- **작업 인박스.** 각 에이전트는 리스(lease), 재시도, 검토 규칙을 갖춘 영속 큐를 가집니다. 가져간 작업은 한 번에 한 명의 소유자만 가집니다. [작업, 결정, 정책](https://github.com/ClickHouse/loom/blob/main/docs/features/tasks-and-decisions.md)
- **결정.** 이와 같은 경우에 무엇을 결정했는지, 그 이유는 무엇인지, 그리고 효과가 있었는지를 기록합니다.
  [결정](https://github.com/ClickHouse/loom/blob/main/docs/features/tasks-and-decisions.md#decisions)
- **워킹 메모리.** 검색으로 찾는 대신 에이전트에 매번 제공되는 텍스트이며, 장기 메모리와는 별도로 관리됩니다. [워킹 메모리와 프로필](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/working-memory-and-profiles.md)
- **컨텍스트 팩.** `context.pack`은 에이전트가 작업을 시작할 때 로드하는, 크기가 제한된 번들을 만듭니다. [통합 검색과 컨텍스트 팩](https://github.com/ClickHouse/loom/blob/main/docs/features/unified-search.md)
- **프레임워크 어댑터.** SDK는 에이전트 프레임워크 자체의 메모리 인터페이스를 통해 Loom을 제공합니다. [에이전트 프레임워크](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/frameworks.md)

<a id="start"></a>

## 시작하기

[에이전트에 Loom 통합하기](https://github.com/ClickHouse/loom/blob/main/docs/guides/agent-integration.md)에서는 각 에이전트가 보내는 ID와 기록할 내용을 다룹니다. [토큰과 권한 부여](https://github.com/ClickHouse/loom/blob/main/docs/guides/tokens-and-grants.md)에서는 각 에이전트에 고유한 토큰을 발급하는 방법을 설명합니다.

이 페이지의 일부 op는 [상태 페이지](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md)에서 experimental로 표시되어 있습니다.
