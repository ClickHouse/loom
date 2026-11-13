<!-- i18n: source=docs/use-cases/coding-agent-cost.md blob=d7a120fc7dc895117d934de1f2dc5ffd14bca474 model=claude-sonnet-5-5 -->
> 이 페이지는 [영어 원문](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/coding-agent-cost.md)을 기계 번역한 것이며, 영어 원문이 기준입니다.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/coding-agent-cost.md), which is authoritative.

<a id="lower-model-cost-for-coding-agents"></a>

# 코딩 에이전트의 모델 비용 절감

Loom 플러그인과 코드 인덱스를 사용하면, Claude Code가 ClickHouse 코드베이스에 관한 질문에 답할 때 측정한 세 가지 질문 세트 모두에서 사용하지 않았을 때보다 모델 비용이 6%에서 21% 낮았습니다. 절감률은 답변 품질을 채점한 24개 질문에서 20%(답변 품질 0.84 대 0.78), 개념 질문 36개에서 21%, 멀티홉 질문 24개에서 6%였습니다. 멀티홉 질문 중 버그를 추적해 릴리스 브랜치의 수정 사항까지 찾아가는 경우는 두 방식의 비용이 같았습니다.

<a id="what-was-measured"></a>

## 측정 대상

두 그룹 모두 같은 체크아웃에서 Claude Code(`claude-sonnet-5`)를 실행했습니다. 하나는 일반 세션이고, 다른 하나는 Loom 플러그인과 `coding` toolset을 사용한 세션입니다. 비용은 세션 전체의 모델 사용 요금이며, Loom 자체의 서버 측 모델 호출, 호스팅, 인덱싱 비용은 포함하지 않습니다. 이 비교는 플러그인 전체를 대상으로 하며, 코드 인덱스와 메모리를 분리해서 측정하지는 않습니다. 결과는 테스트한 워크로드에만 해당합니다. Loom을 사용하면 질문 하나에 걸리는 실제 시간은 더 길어집니다. 플러그인의 프롬프트별 검색과 MCP 왕복 통신이 지연 시간을 늘리기 때문입니다. 질문별 비용을 실행 간 변동(noise)과 어떻게 비교하는지는 결과 페이지에서 설명합니다.
[With and without Loom](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#with-and-without-loom)에는 모든 세트, 프로토콜, 아티팩트가 있으며, [evaluate Loom](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/evaluate.md)은 자신의 저장소에서 직접 측정하는 방법을 보여 줍니다.

전체 채팅 기록 대신 모델이 읽는 메모리에 대해서는 [agent memory](agent-memory.md#measured)를 참고하세요.

<a id="how-the-plugin-is-designed-to-save"></a>

## 플러그인이 비용을 절감하도록 설계된 방식

- **파일 읽기 대신 인덱스 응답.** `code.symbol`, `code.callers`, `code.search`는 구조에 관한 질문에 답이 되는 줄만 반환합니다. [코드 인덱스](code-intelligence.md)
- **재탐색 대신 저장된 컨텍스트.** 세션에서 저장한 결정 사항과 발견 내용은 다음 세션이 시작될 때 불러옵니다. [공유 메모리](agent-memory.md)

Loom은 또한 작업별로 임베딩과 LLM 호출에 사용한 비용을 기록합니다.
[비용 및 사용량](https://github.com/ClickHouse/loom/blob/main/docs/features/observability/cost-and-usage.md)
