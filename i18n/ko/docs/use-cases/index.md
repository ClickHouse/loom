<!-- i18n: source=docs/use-cases/index.md blob=f08bc10d9096342a15700a4d8e519f3f11a3f2e8 model=claude-sonnet-5-5 -->
> 이 페이지는 [영어 원문](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/index.md)을 기계 번역한 것이며, 영어 원문이 기준입니다.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/index.md), which is authoritative.

<a id="use-cases"></a>

# 사용 사례

각 페이지는 사람과 에이전트가 도구를 찾을 때 던지는 질문 하나에 답합니다. 해당 요구에 Loom이 무엇을 해 주는지, 어디에서 시작하면 되는지를 다룹니다.

| 요구 사항                                                                        | 페이지                                                     |
| -------------------------------------------------------------------------------- | ---------------------------------------------------------- |
| AI 에이전트를 위한 장기 메모리와 공유 메모리 (MCP 메모리 서버)                   | [에이전트 메모리](agent-memory.md)                         |
| 에이전트를 위한 지식 및 문서 검색 (RAG)                                          | [문서 검색](document-search.md)                            |
| 코딩 에이전트를 위한 코드 인덱스와 코드 인텔리전스                               | [코드 인덱스](code-intelligence.md)                        |
| 언어별 코드 인덱스 제공 범위 (C++, Python, TypeScript, Go, Rust, Java)           | [언어별 코드 인덱스](code-intelligence-by-language.md)     |
| 코딩 에이전트의 모델 비용 절감                                                   | [코딩 에이전트 비용](coding-agent-cost.md)                 |
| 멀티 에이전트 워크플로에서 에이전트 간 컨텍스트 공유                             | [멀티 에이전트](multi-agent.md)                            |
| 자율 에이전트를 위한 메모리, 회상, 감사, 삭제                                    | [자율 에이전트](autonomous-agents.md)                      |
| 에이전트 실행, 트레이스, 트래커, PR 이력                                         | [실행 및 PR 이력](runs-and-pr-history.md)                  |
| Docker 또는 Kubernetes로 구성하는 ClickHouse 기반 셀프 호스팅 에이전트 메모리    | [ClickHouse 셀프 호스팅](self-hosted-clickhouse.md)        |

[Loom이 맞지 않는 경우](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/overview.md#when-it-does-not-fit)에서는 다른 도구가 더 적합한 요구 사항을 정리합니다.
