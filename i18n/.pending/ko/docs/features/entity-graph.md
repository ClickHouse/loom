<!-- i18n: source=docs/features/entity-graph.md blob=f2344ba078b13ea0d97951af2212b5da3b9af8bb model=claude-sonnet-5-5 -->
> 이 페이지는 [영어 원문](https://github.com/ClickHouse/loom/blob/main/docs/features/entity-graph.md)을 기계 번역한 것이며, 영어 원문이 기준입니다.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/features/entity-graph.md), which is authoritative.

<a id="entity-graph"></a>

# 엔티티 그래프

Loom은 메모리 행 옆에 같은 ClickHouse 데이터베이스로 엔티티 그래프를 유지합니다. 엔티티는 노드입니다. 같은 메모리에서 언급된 두 엔티티는 동시 출현 엣지를 공유하며, 추출 과정에서 유형이 지정된 `(subject, relation, object)` 트리플이 두 번째 엣지 집합으로 추가됩니다. 그래프는 메모리의 투영입니다. 모든 엣지는 그것을 만들어 낸 행까지 추적할 수 있습니다. 벡터 검색으로는 답할 수 없는 질문, 예를 들어 "billing service와 연결된 것은 무엇인가"와 같은 질문에 사용하세요.

엔티티 커뮤니티는 기본적으로 꺼져 있습니다(`LOOM_ENTITY_COMMUNITIES_ENABLED`).

<a id="what-the-graph-holds"></a>

## 그래프가 담고 있는 것

| 테이블                                                                   | 담고 있는 것                                                                                                                            |
| ------------------------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------- |
| `loom.entity_edges` (+ `entity_edge_memories`, `entity_edge_rows`)       | 공출현 관찰(`co_occur = 1`)당 한 행이며, 읽기 시 쌍별로 합산합니다. 쌍별 소스 키와, 그 쌍을 지목한 행과 값을 담습니다. |
| `loom.entity_triples` (+ `entity_triple_memories`, `entity_triple_rows`) | `confidence`와, 추출기 모델을 `adapter_name`으로 가진 타입 지정 트리플. 트리플별 소스 키와, 그 트리플을 진술한 행과 값을 담습니다. |
| `loom.entity_degree`                                                     | 각 엔티티의 전체 공출현 차수(`SummingMergeTree`)이며, 허브 규칙이 이를 읽습니다.                                                   |
| `loom.entity_canonical_map`                                              | 언급 텍스트 → 정규 엔티티.                                                                                                        |
| `loom.entity_communities`                                                | 레벨별 엔티티당 커뮤니티 id.                                                                                                     |
| `loom.entity_vectors`, `loom.entity_summaries`                           | 검색의 엔티티 레그가 읽는 임베딩.                                                                                           |
| `loom.derived_facts`                                                     | 검색에서 계산된 답변에 쓰이는, 단위와 날짜가 있는 숫자 팩트.                                                                       |
| `memory.link` 엣지                                                       | 에이전트나 사람이 단언한 엣지(`supports`, `depends_on`, `caused`, …)이며, 그래프 모드 검색이 읽습니다.                                    |

엣지와 트리플은 `valid_from` / `valid_to`(소스 메모리의 `valid_at` / `valid_to`에서 가져옴)와, 이를 폐기하는 `invalid_at` 스탬프를 가집니다. `as_of`를 지정한 읽기는 해당 시점에 유효한 관찰만 합산합니다.

그래프를 ClickHouse에 두는 이유는 엣지가 해당 행과 같은 요청에서 기록되고, 같은 saga로 삭제되며, 네트워크 홉 없이 메모리와 조인될 수 있기 때문입니다. 그 대가로 탐색은 범위가 제한된 집계 쿼리의 연속이 되므로, 모든 탐색에는 상한이 있습니다.

<a id="how-the-graph-is-built"></a>

## 그래프가 만들어지는 방식

메모리 행이 먼저 기록되고, 그래프 행은 그 행에서 파생된 프로젝션입니다. 프로젝션에 실패하면 `loom.memory_projection_failures`에 기록되고 재시도됩니다. 재시도는 해당 행이 프로젝션에 실패했던 값을 여전히 가지고 있는 동안에만 이루어집니다. 프로젝션 실패가 쓰기를 실패시키는 일은 없습니다. `high` 민감도의 대량 항목은 아무것도 프로젝션하지 않으며, `high` 민감도의 `memory.set`은 작성자가 선언한 엔티티의 엣지만 프로젝션하고 텍스트는 프로젝션하지 않습니다.

1. **엔티티.** 작성자가 `memory.set`에 `entities`를 전달하거나, `memory.set_from_messages`가 엔티티를 추출합니다. `entity_extractor` 워커(`LOOM_ENTITY_EXTRACT_ENABLED`, 기본값 켜짐)는 엔티티 없이 기록된 행에 쓰기 경로와 별개로 태그를 붙이며, 실제 LLM 제공자가 필요합니다. 엔티티 텍스트는 쓰기와 읽기에서 같은 방식으로 정규화됩니다(관사와 소유격 제거, 핵심 명사의 단수화, 1인칭은 `user`로 매핑). 그래서 "The Billing Service's"와 "billing service"는 하나의 노드입니다. `commit:<sha>` 또는 `error:<name>` 형태의 이름은 식별자로 취급되어 같은 방식으로 정규화되지만 단수화는 하지 않습니다. `error:Timeouts`나 백틱 또는 따옴표로 감싼 같은 이름은 노드 `error:timeouts`가 됩니다. 콜론 바로 뒤에 공백이나 마크업이 오면(`Error: the build failed`, ``error:`Timeouts` ``) 일반 문장으로 간주됩니다. 대명사, 감탄사, 경동사, 두 글자 토큰은 제외됩니다. 작성된 그대로이거나 정규화한 뒤(NFKC는 `ﬁ`를 `fi`로 바꿉니다) 256자를 넘는 텍스트도 쓰기와 `entity_filter`에서 제외됩니다. 포인트 쓰기는 이런 팩트의 주어나 목적어를 제외하는 대신 거부합니다.
2. **엣지.** 한 메모리의 엔티티로 만들 수 있는 모든 쌍이 각각 하나의 관측 행이 되며, 메모리당 최대 100쌍입니다.
3. **트리플.** 추출된 팩트의 관계가 트리플이 되며, 메모리당 최대 8개입니다. 동사는 `lower_snake`로 정규화됩니다. `namespace.register_ontology`(관리자)는 선호하는 엔티티 타입, 동사, 속성을 카디널리티와 함께 등록합니다. 이 집합에 없는 이름은 `ontology_warnings`를 생성하며, 거부되는 것은 없습니다.
4. **대체가 트리플을 닫습니다.** 어떤 행이 다른 행을 대체하면, 이전 행의 열린 트리플 중 새 행이 증명하지 않는 것에 `invalid_at`이 설정됩니다. `one` 관계(고용주, 위치)에서는 더 새로운 값이 현재 값으로 유지되고, 더 오래된 값은 새 값이 시작될 때 닫힙니다. 이 닫기가 진행되는 동안 오래된 값을 다시 기술하는 쓰기는 그 값을 열린 상태로 유지합니다.
   - 값은 시작일 기준으로 정렬되고(날짜가 없는 값은 도착한 시점에 시작), 그다음 마이크로초 단위의 도착 순서로 정렬됩니다. 따라서 경합하는 작성자들도 하나의 현재 값에 합의합니다. 하나의 쓰기 안에서는 나중에 기술된 값이 더 새롭습니다.
   - 하나의 쓰기 안에서 과거형(`lived_in`, `worked_at`)의 날짜 없는 값은 순서와 관계없이 날짜가 있는 값보다 오래된 것으로 취급됩니다. 10월 1일부터의 Paris 옆에 "user lived_in london"이 있으면 Paris가 현재 값으로 남습니다.
   - 값은 가장 새로운 행을 기준으로 판단됩니다. "London from Oct 15, Paris from Oct 14, London from Oct 13"이면 London이 현재 값으로 남습니다.
   - 자체 종료 시점이 기술된 값("London until December")은 일찍 닫히지 않습니다. 그 종료 시점까지는 더 새로운 값과 함께 현재 값으로 유지됩니다.
   - 되돌리기(`memory.restore`)는 복원된 값을 복원 시점부터, 또는 팩트 자체의 더 늦은 시작 시점("moving to Berlin on Dec 1")부터 현재 값으로 만듭니다. 다른 값은 닫히며, 그 행은 계속 유효합니다. 더 늦은 날짜의 값이나 되돌리기 이후에 기술된 값이 여전히 우선합니다. 되돌리기를 앞뒤로 반복해도 가중치는 늘어나지 않습니다.
   - 실패한 닫기는 그것을 발생시킨 쓰기 또는 되돌리기 시점을 기준으로 다시 실행되므로, 그 이후에 기술된 값은 현재 값으로 유지됩니다.

   되돌리기가 팩트와 엣지를 어떻게 다시 기술하고, 가중치를 매기고, 다시 실행하는지는 [메모리 아키텍처](../architecture/memory-architecture.md#undo)에 있습니다.

포인트 쓰기(`memory.set`, `memory.update`, `memory.update_by_id`)는 호출자가 `relations`로 전달한 트리플을 최대 8개까지 기록하며, HTTP와 SDK의 타입 지정 연산에서 가능합니다(MCP 목록에는 없음). 포인트 쓰기는 추출을 하지 않습니다. 자동 추출은 대량 경로에서 실행됩니다. 즉 `memory.set_from_messages`나 relations를 제공하는 대량 작성자의 경우입니다. 기본 가짜 제공자를 사용하면 직접 전달한 엔티티만 엣지를 형성합니다.

<a id="operations"></a>

## 작업

| 작업                         | 역할   | 답하는 내용                                                                                                   |
| ---------------------------- | ------ | ------------------------------------------------------------------------------------------------------------- |
| `facts.list`                 | viewer | `subject`, `object`, `relation`(AND) 또는 `entity`(양쪽 끝 중 하나)로 필터링한 트리플.                        |
| `facts.related`              | viewer | 한 엔티티와 함께 등장하는 엔티티를 강한 순서로 보여 주며, 시드가 어느 쪽에 있는지도 알려 줍니다.              |
| `facts.traverse`             | viewer | 시드에서 N홉 떨어진 범위까지의 이웃.                                                                          |
| `facts.path`                 | viewer | `max_hops` 안에서 두 엔티티를 잇는 가장 짧은 엣지 체인.                                                       |
| `facts.expire_by_adapter`    | admin  | 한 추출기가 기록한 트리플을 툼스톤 처리합니다. 메모리는 그대로 남습니다.                                      |
| `memory.search` (그래프 모드) | viewer | `search_mode="graph"`: 쿼리의 엔티티에서 한 홉 떨어진 결과([검색과 질문](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/search-and-ask.md)). |
| `memory.related`             | viewer | 주어진 메모리와 엔티티를 공유하는 메모리.                                                                     |
| `memory.timeline`            | viewer | 한 엔티티의 메모리를 시간순으로 보여 줍니다(`entity=…`).                                                      |
| `memory.backlinks`           | viewer | 한 행의 엔티티를 공유하는 메모리.                                                                             |
| `namespace.taxonomy`         | viewer | 네임스페이스의 상위 엔티티와, 계산된 경우 커뮤니티.                                                           |

필드 목록: [팩트](https://github.com/ClickHouse/loom/blob/main/docs/reference/operations/facts.md), [메모리](https://github.com/ClickHouse/loom/blob/main/docs/reference/operations/memory.md). 그래프 모드가 아닐 때는 `entity_filter`가 지정한 엔티티에 관한 행의 점수를 높이며, 검색의 엔티티 레그는 쿼리가 다른 표현으로 언급한 엔티티에 관한 행을 찾아냅니다([검색과 랭킹](https://github.com/ClickHouse/loom/blob/main/docs/concepts/retrieval-and-ranking.md)).

<a id="factslist"></a>

### `facts.list`

필터가 없으면 `co_occur` 기준 상위 트리플을 반환하며, `sort_by="recency"`는 `last_seen` 순으로 정렬합니다.

| 필드                    | 효과                                                                                                 |
| ----------------------- | ---------------------------------------------------------------------------------------------------- |
| `include_sources`       | 트리플마다 소스 메모리 키를 최대 10개 정도 추가합니다.                                               |
| `include_source_status` | 각 소스 행을 읽어, 트리플은 열려 있지만 그 뒤의 모든 행이 대체된 경우 `stale`로 표시합니다.          |
| `provenance`            | `extracted`(양쪽 끝이 소스 텍스트에 있음), `inferred`, 또는 `facet`(Loom이 만든 탐색용 엣지).        |
| `as_of`                 | 해당 시점에 유효한 트리플만 남깁니다.                                                                |

<a id="factstraverse"></a>

### `facts.traverse`

탐색은 확장하기 전에 먼저 가지치기를 합니다. 각 홉은 프런티어를 하나의 쿼리로 보내고, 유효 기간 안에서 쌍별 관측 횟수를 합산하며, 이미 방문한 엔티티를 제외하고, 각 부모마다 가장 강한 `top_k_per_hop`개의 쌍만 남깁니다. h번째 홉의 프런티어에는 최대 K^h개의 엔티티가 있습니다. 각 엔티티는 처음 도달한 홉에서 가장 강한 부모를 통해 한 번만 보고되므로 결과는 트리 형태로 그려집니다.

모든 것을 확장한 뒤 가지치기하는 단일 재귀 쿼리는 이웃 전체를 구체화하게 되며, 큰 네임스페이스에서는 3홉 탐색이 ClickHouse 메모리 한도를 넘을 수 있습니다. 홉마다 가지치기하면 비용이 결과 크기로 제한됩니다.

| 필드                    | 기본값                                   | 효과                                                                                                           |
| ----------------------- | ---------------------------------------- | -------------------------------------------------------------------------------------------------------------- |
| `max_hops`              | 2 (1–4)                                  | 탐색 깊이.                                                                                                     |
| `top_k_per_hop`         | 20 (최대 100)                            | 홉마다 부모당 유지하는 자식 수.                                                                                |
| `min_co_occur`          | 1                                        | 쌍당 최소 관측 횟수.                                                                                           |
| `max_nodes`             | 400 (최대 5,000)                         | 하드 한도. 도달하면 `truncated=true`.                                                                          |
| `as_of`                 | 미설정                                   | 해당 시점에 유효한 관측만 유지합니다.                                                                          |
| `labeled` / `relations` | false / 미설정                           | 타입이 있는 트리플(전체 또는 지정한 동사만)을 따라갑니다. 노드에는 `via_relation`, `via_direction`, `via_provenance`가 포함됩니다. |
| `hub_degree`            | 0 (꺼짐)                                 | 총 차수가 이 값 이상인 노드는 허브입니다.                                                                      |
| `hub_share`             | `LOOM_FACTS_TRAVERSE_HUB_SHARE` (0.01)   | 노드에 도달한 엣지가 그 노드 차수에서 차지하는 비율이 이 값 이하이면 허브입니다.                               |
| `rank_by`               | `LOOM_FACTS_TRAVERSE_RANK_BY` (`cosine`) | 홉별 가지치기 기준: `co_occur`, `specificity`(`via_co_occur / degree`) 또는 `cosine`(`via_co_occur / sqrt(degree)`). |

각 노드는 `hop`, `cumulative_weight`, `via_co_occur`, `path`(시드에서 이어지는 체인), `valid_from` / `valid_to`, `degree`, 그리고 커뮤니티가 있으면 `community_id`를 보고합니다. 노드는 홉 순, 그다음 가중치 순으로 정렬됩니다.

**허브.** "user", "pr" 같은 일반적인 엔티티는 모든 것과 함께 등장합니다. 허브는 보고되지만(`hub: true`, `hubs`에 나열) 확장되지 않습니다. 시드는 항상 확장되므로, 허브의 이웃을 보려면 허브를 시드로 지정하세요. `hub_share`는 네임스페이스의 차수 규모에 의존하지 않으므로 기본 규칙입니다. `hub_degree`는 네임스페이스마다 다르므로, 설정하기 전에 차수 분포의 꼬리를 확인하세요.

<a id="factspath"></a>

### `facts.path`

같은 엣지에 대해 중간에서 만나는, 범위가 제한된 양방향 너비 우선 탐색입니다. `max_hops`(1–4, 기본값 3)가 전체 길이를 제한합니다. 응답은 `found`, `path`, 그리고 각 쌍의 `co_occur`와 유효 기간을 담은 `edges`입니다. `truncated=true`는 결과가 불완전할 수 있다는 뜻입니다. `found=false`이면서 `truncated=false`이면 `max_hops` 안에 연결이 없다는 뜻입니다.

<a id="example"></a>

## 예시

```python
from loom import Agent

async with Agent("dev", "local", "sre-bot", base_url="http://localhost:7777") as agent:
    await agent.memory.set(
        "billing/depends_on",
        "The billing service reads invoices from the ledger database.",
        entities=["billing service", "ledger database"],
    )
    walk = await agent.facts.traverse("billing service", max_hops=2, top_k_per_hop=10)
    for node in walk["nodes"]:
        print(node["hop"], " > ".join(node["path"]), node["via_co_occur"])

    route = await agent.facts.path("billing service", "ledger outage")
    print(route["found"], route["path"], route["truncated"])
```

HTTP로는 과거 특정 시점의 그래프 상태를 기준으로 탐색할 수 있습니다:

```bash
curl -s localhost:7777/v1/facts.traverse -H "Authorization: Bearer $LOOM_API_KEY" \
  -H 'content-type: application/json' \
  -d '{"org":"dev","namespace":"local","seed_entity":"billing service","max_hops":2,"as_of":"'"$AS_OF"'"}'
```

MCP로는 `op="facts.traverse"`를 지정해 `loom_op`를 호출합니다.

<a id="maintaining-the-graph"></a>

## 그래프 유지 관리

| 작업                                | 방법                                                                                                           |
| ----------------------------------- | -------------------------------------------------------------------------------------------------------------- |
| 잘못된 추출기의 트리플 롤백         | `facts.expire_by_adapter(adapter_name=…, max_confidence=…)`. 메모리는 그대로 유지됩니다.                       |
| 그래프 내보내기                     | `loom entity-export --org O --namespace N --format graphml` (또는 `jsonld`; `--all`은 무효화된 엣지도 포함합니다). |
| 엔티티를 커뮤니티로 클러스터링      | `LOOM_ENTITY_COMMUNITIES_ENABLED=true`를 설정합니다(아래 참고).                                                |
| 그래프 보기                         | 대시보드의 Graph 보기([대시보드](https://github.com/ClickHouse/loom/blob/main/docs/features/dashboard.md)).     |

`entity_communities` 워커는 네임스페이스별로 레이블 전파(label propagation)를 실행하여 그 결과를 `loom.entity_communities`에 기록하며, 이 테이블은 `namespace.taxonomy`, 그래프 모드 확장, `community_id`에서 읽습니다. 통합(consolidation)이 켜져 있는 네임스페이스와, 최근 `LOOM_ENTITY_COMMUNITIES_WALKED_DAYS`(14)일 이내에 탐색된 네임스페이스를 대상으로 합니다. 차수가 가장 높은 엔티티부터 먼저 제외하며(`LOOM_ENTITY_COMMUNITIES_HUB_DROP_FRACTION`), 하나의 커뮤니티가 전체 엔티티의 `LOOM_ENTITY_COMMUNITIES_GIANT_SHARE` 비율을 넘는 동안에는 그 제외 비율을 두 배로 늘립니다. 이렇게 하면 일반적인 토큰 때문에 그래프가 하나의 커뮤니티로 합쳐지는 일을 막을 수 있습니다.

<a id="settings"></a>

## 설정

| 설정                                         | 섹션                                                                                                                         |
| -------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------- |
| `LOOM_FACTS_TRAVERSE_HUB_SHARE`, `…_RANK_BY` | [네임스페이스 상태, 지식 그래프, 커뮤니티](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md#namespace-health-knowledge-graph-communities) |
| `LOOM_ENTITY_COMMUNITIES_*`                  | 같은 섹션.                                                                                                                   |
| `LOOM_ENTITY_EXTRACT_*`                      | [통합](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md#consolidation)                           |
| `LOOM_MEMORY_SEARCH_GRAPH_SEED`              | [메모리 검색](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md#memorysearch)                     |

<a id="limits"></a>

## 제한 사항

- **동시 출현은 관계가 아닙니다.** 한 메모리에 함께 나온 두 엔티티는 문장이 무엇을 말하든 연결됩니다. 관계가 중요하다면 타입이 있는 트리플(`labeled`, `relations`) 또는 `memory.link`를 사용하세요.
- **그래프의 품질은 엔티티의 품질에 좌우됩니다.** 엔티티가 없는 행은 `entity_extractor` 워커가 처리하기 전까지 엣지를 추가하지 않습니다.
- **탐색에는 한계가 있습니다.** 두 엔티티가 연결되어 있지 않다고 결론 내리기 전에 `truncated`를 확인하세요.
- **엣지가 대체된 행보다 늦게 갱신될 수 있습니다.** `facts.list(include_source_status=true)`는 아직 열려 있는 트리플을 보여 줍니다.
- **유효성은 유효 시간(valid time)을 기준으로 합니다.** `as_of`는 사실이 성립했던 시점을 읽으며, Loom이 그 사실을 알게 된 시점을 읽지 않습니다.

<a id="related"></a>

## 관련 문서

- [검색 및 질의](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/search-and-ask.md): 그래프 모드.
- [메모리 쓰기](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/writing.md): `entities` 전달.
- [추출](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/extraction.md): 추출된 엔티티와 트리플의 출처.
- [메모리 아키텍처](../architecture/memory-architecture.md): 프로젝션 라이터.
- [워커 레퍼런스](https://github.com/ClickHouse/loom/blob/main/docs/reference/workers.md).
