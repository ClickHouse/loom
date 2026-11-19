<!-- i18n: source=docs/guides/importing-clickgap-memory.md blob=5fff9080da2e5f36035250de4a491ae273d3484c model=claude-sonnet-5-5 -->
> 이 페이지는 [영어 원문](https://github.com/ClickHouse/loom/blob/main/docs/guides/importing-clickgap-memory.md)을 기계 번역한 것이며, 영어 원문이 기준입니다.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/guides/importing-clickgap-memory.md), which is authoritative.

<a id="import-clickgap-memory"></a>

# ClickGap 메모리 가져오기

`loom import-clickgap`은 SQLite 내보내기 형식 중 하나인 ClickGap `lessons` 저장소를 Loom 네임스페이스로 불러옵니다. 이러한 내보내기 파일이 없다면 이 페이지는 건너뛰어도 됩니다. lesson은 중복이 제거된 짧은 리뷰 메모리이며, 타입이 지정된 엔티티, 카테고리, kind, 행 단위 TTL, `recall_count`를 가집니다.

두 가지 명령이 작업을 수행합니다.

- `loom import-clickgap`: 가져오기 도구(`server/cli/import_clickgap.py`)입니다. `loom` CLI가 ClickHouse에 접근할 수 있는 위치(서버 컨테이너 또는 서버의 `.env`가 있는 클론)에서 실행합니다.
- `python -m bench.clickgap_migration_validate`: 검증 도구(`bench/clickgap_migration_validate.py`)입니다.

<a id="what-the-importer-preserves"></a>

## 임포터가 보존하는 항목

| ClickGap                    | Loom                                                                    | 비고                                                                                                                                                                                                                                                                            |
| --------------------------- | ----------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `lessons.text`              | `loom.memory.value` + 임베딩된 `content_excerpt`                        | 주장(assertion)은 임베딩되며, 근거(reasoning)는 임베딩되지 않습니다                                                                                                                                                                                                                                 |
| `lessons.reasoning`         | `value_json.reasoning` (`.text` = 주장 포함)                       | `get`은 `value_json`을 반환하므로 주장이 근거와 함께 전달되며, 근거에 이어 붙지는 않습니다                                                                                                                                                                             |
| `lessons.kind`              | `memory_type`(대부분 1:1) **+** `kind:<kind>` 태그                      | `summary → episodic`(검색 불가): 요약은 `memory.get` / `memory.list`와 `kind:summary` 태그로 계속 접근할 수 있지만 `memory.search`에서는 순위에 오르지 않습니다. `kind:<kind>` 태그가 원래의 분류 체계를 보존합니다. `_clickgap_memory_type` / `_CLICKGAP_TYPE_REMAP`을 참고하세요. |
| `lessons.source`            | `source_trace='clickgap://<class>/<source>'` + `source:<class>` 태그     | 클래스: pipeline / session / human / reviewer / eval                                                                                                                                                                                                                           |
| `lessons.created_at`        | `created_at`, `valid_at`, `temporal_anchor`                             | 임포트 시각이 아니라 원래의 기록 시각입니다                                                                                                                                                                                                                                |
| `lessons.expires_at`        | `expires_at` **행마다 원본 그대로**                                      | 전역 TTL로 다시 계산하지 않습니다                                                                                                                                                                                                                                              |
| `lessons.recall_count`      | `recall_count` **및** `memory_embeddings.reinforcement_count`          | [회상 신호](#recall-signal)를 참고하세요                                                                                                                                                                                                                                             |
| `lessons.last_recalled_at`  | `last_recalled_at` **및** `last_reinforced_at`                         | 회상 최근성 축입니다                                                                                                                                                                                                                                                         |
| `lessons.evidence_count`    | `evidence_count`                                                        |                                                                                                                                                                                                                                                                                 |
| `lesson_entities`           | `entities`(단순) + `entity:<kind>:<text>` 태그 + `loom.entity_vectors` | 단순 `entities` 배열에는 종류(kind)가 빠지며, 태그가 이를 담습니다                                                                                                                                                                                                                    |
| `lesson_categories` + items | `loom.categories` + `loom.memory_categories`                            | 결정적 `category_id`를 만들기 전에 이름의 대소문자를 통일합니다                                                                                                                                                                                                                        |
| `false_positive` lessons    | `polarity:negative` 태그                                                 | 억제("다시 제기하지 말 것") 메모리                                                                                                                                                                                                                                        |
| 사람이 작성한 출처    | `pii:human-attributed` 태그                                              | 태그 기반 삭제 후보                                                                                                                                                                                                                                                       |

**임포트는 멱등입니다.** `memory_key='lesson_<id>'`와 UUIDv5 `memory_id`(`clickgap://<org>/<namespace>/lesson/<id>`에서 생성)는 결정적이며, 실행할 때마다 새로운 `version` 하나가 부여됩니다. 다시 임포트하면 `ReplacingMergeTree(version)`에 따라 기존 행을 대체하며 행 수는 그대로 유지됩니다.

<a id="1-dry-run"></a>

## 1. 드라이 런

쓰기 작업은 없습니다. 드라이 런은 모든 타임스탬프가 파싱되는지 확인하고, kind → type 및 TTL 교차표와 카테고리 수를 출력하며, 행이 임베딩 없이 저장되지 않도록 임베더를 점검합니다.

```bash
loom import-clickgap \
  --sqlite ~/Downloads/clickgap-memory-YYYYMMDD/memory.db \
  --org <org> --namespace clickgap --dry-run
```

교차표가 원본과 일치하는지, 그리고 임베더에 연결할 수 있는지 확인합니다.

<a id="2-import"></a>

## 2. 가져오기

**전용 네임스페이스**(`clickgap`)를 사용하면 롤백을 `namespace.delete` 한 번으로 끝낼 수 있습니다. 내보낸 데이터에는 벡터가 없으므로, 가져오기 시 모든 행이 현재 `LOOM_EMBEDDING_MODEL`로 임베딩됩니다.

```bash
loom import-clickgap \
  --sqlite .../memory.db --org <org> --namespace clickgap --agent clickgap
```

`--agent`의 기본값은 `clickgap`이며, `--batch-size`는 ClickHouse insert 한 번에 넣을 행 수를 지정합니다(기본값 1000). 마지막에 출력되는 검증 블록은 다음 항목을 확인하며, 어느 하나라도 맞지 않으면 실행을 실패(종료 코드 1)로 처리합니다.

- 소스의 lesson 개수와 비교한 라이브 메모리 행 수
- 잘못된 모델로 임베딩된 행 수(반드시 0이어야 함)
- 영구(TTL 없음) 행 수
- 다시 읽어 온 가장 높은 `recall_count`

`lesson_recalls`(recall별 이벤트 로그)는 기본적으로 가져오지 **않습니다**. 비정규화된 `recall_count` 컬럼에 이미 랭킹 신호가 담겨 있기 때문입니다. 분석을 위해 `loom.memory_usage_events`에 이벤트별 로그가 필요한 경우에만 `--seed-recall-events`를 지정하십시오.

> **경고:** `--seed-recall-events`는 데이터를 추가하는 방식이며 멱등적이지 않습니다. 새 네임스페이스에 한 번만 실행하십시오. Loom 사용 이벤트에는 PR 컬럼이 없으므로 `pr_number`는 **버려집니다**.

<a id="recall-signal"></a>

## 회상 신호

임포터는 `recall_count`를 `memory_embeddings.reinforcement_count`에, `last_recalled_at`을 `last_reinforced_at`에 그대로 반영합니다. `memory.search`에서는 이 카운트가 감쇠 강화(decayed-reinforcement) 순위 항에 사용됩니다. `LOOM_MEMORY_SEARCH_REINFORCEMENT_WEIGHT`가 이 항의 크기를 정하고(0이면 비활성화), `LOOM_MEMORY_SEARCH_REINFORCEMENT_HALF_LIFE_DAYS`가 감쇠 정도를 정하며, 요청에서 `reinforcement_weight` / `reinforcement_half_life_days`로 두 값을 모두 재정의할 수 있습니다
([구성](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md#memorysearch)).

이 항은 관련도보다 낮은 수준으로 상한이 제한되므로, Loom은 ClickGap의 `recall_count DESC` 정렬을 **재현하지 않습니다**. Loom은 주로 유사도를 기준으로 순위를 매기고, 여기에 엔티티, 최신성, 강화 항을 더합니다
([검색과 순위 지정](https://github.com/ClickHouse/loom/blob/main/docs/concepts/retrieval-and-ranking.md)). 워크로드가 회상 빈도 순서에 의존한다면, 전환 전에 결과 순서가 허용 가능한지 검증하세요(검증기는 회상 순서 τ를 보고합니다).

<a id="3-validate-before-cutover"></a>

## 3. 전환 전에 검증하기

검증기는 `server.auth.jwt.mint_token`으로 자체 관리자 토큰을 발급하므로 `LOOM_JWT_SECRET`과 명시적인 `LOOM_BASE_URL`이 필요합니다. 기본 대상은 없습니다. 검증기의 probe는 평가용 읽기(`auto_touch: false`, `bench/_gate.py` 측정 필드)이므로, 실행해도 가져온 lesson에 recall 사용 기록이 남지 않고, 반복 실행이 τ 게이트가 측정하는 강화 신호에 반영되지도 않습니다.

> **경고:** 신뢰할 수 있는 엔드포인트를 대상으로 지정하세요. probe 쿼리는 lesson의 assertion이며, 가져온 데이터가 서드파티 임베더를 통해 사용자의 인프라 밖으로 나가서는 안 됩니다. 같은 이유로 `--show-queries`는 잘린 probe 텍스트를 출력하며 기본적으로 꺼져 있습니다.

```bash
LOOM_BASE_URL=http://localhost:7777 LOOM_JWT_SECRET=... \
python -m bench.clickgap_migration_validate \
  --sqlite .../memory.db --org <org> --namespace clickgap --agent clickgap \
  --probes 200 --top-k 10 \
  --min-recall 0.90 --min-recallw 0.90 --min-tau 0.2
```

각 게이트는 0보다 큰 값으로 설정하면 기준을 충족하지 못할 때 0이 아닌 종료 코드로 실행을 실패 처리합니다(0 = 보고만 함).

| 게이트          | 측정 대상                                                                                                                                                           |
| --------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `--min-recall`  | Known-item recall@k: probe 대상 lesson 자체가 Loom의 상위 k개 안에 있는가? 종류별, 엔터티 유형별 세부 결과로 특정 구간만 무너지는 문제(예: 잘못된 유형 재매핑)를 잡아냅니다. |
| `--min-recallw` | Recall 가중 recall@k: 각 probe에 `recall_count`만큼 가중치를 두므로, 자주 쓰이는 lesson을 놓치면 더 큰 감점이 됩니다.                                              |
| `--min-tau`     | Recall 순서 τ: Loom의 정렬 순서와 `recall_count` 사이의 Kendall 일치도입니다. 진단용 지표입니다. Loom은 `recall_count`로 정렬하지 않으므로 높은 τ를 기대하지 마세요.      |

실패한 검색은 보고되며 비율 계산에서 제외됩니다. 누락으로 채점되지 않습니다. 검색이 하나라도 실패하면 게이트 설정과 관계없이 실행은 0이 아닌 종료 코드로 끝납니다. τ를 계산할 수 없는 실행은 `n/a`를 출력하며 `--min-tau`를 통과하지 못합니다.

요약(`summary → episodic`)은 설계상 검색할 수 없으므로 probe에서 제외됩니다. `--seed`(기본값 42)는 probe 샘플을 재현 가능하게 하며, `--concurrency`(기본값 8)는 동시 검색 수의 상한을 정합니다.

<a id="use-suppression-memories"></a>

## 억제 메모리 사용하기

`false_positive` 레슨("버그처럼 보이지만 다시 제기하지 말 것")이 억제 계층입니다. 이 레슨은 검색할 수 있으며 `polarity:negative` 태그를 가집니다. 후보 발견 사항을 제기할지 판단하는 감지기는 발견 사항의 텍스트에 대해 태그 필터 검색을 실행합니다.

```python
hits = await agent.memory.search(finding_text, tags=["polarity:negative"], top_k=5)
if hits and hits[0].similarity >= SUPPRESS_THRESHOLD:  # calibrate per embedder
    skip_or_downweight(finding)  # a known false positive
```

`tags` 필터는 모든 검색 경로(벡터, 어휘 검색, 그리고 최신성/신뢰도 백필)에 적용되므로 결과에는 억제 메모리만 담깁니다
(`tests/unit/test_memory_search_service.py`의 `test_lexical_fallback_applies_caller_filters`).
긍정 메모리가 섞여 들어가면 실제 발견 사항이 가려질 수 있습니다.

다른 종류도 `kind:<kind>` 태그로 같은 방식으로 필터링할 수 있습니다. `tags=["kind:principle"]`을 쓰거나, 여러 종류를 묶은 제외 목록에는 `tags=["kind:false_positive", "kind:maintainer_preference"]`를 사용합니다(`tags`는 OR 필터입니다). `memory.search`의 `memory_type` 매개변수는 큰 분류(`semantic`, `procedural`, `preference`, `reflection`, `decision`)만 받으므로, 전체 종류 분류 체계는 태그에 담겨 있습니다.

<a id="roll-back"></a>

## 롤백

- **전체 되돌리기:** `clickgap`에 대해 `namespace.delete`를 실행합니다(관리자 권한 필요, MCP에서는 `loom_op`를 통해 호출). 가져오기는 전용 네임스페이스를 대상으로 하므로 다른 데이터는 삭제되지 않습니다.
- **삭제 또는 코호트 정리:** 네임스페이스 범위로 `erasure.request`를 실행합니다(체크포인트가 기록되며 복구 워커가 재개합니다). 또는 유지관리자의 삭제 요청이 있을 때 `pii:human-attributed` 행만 삭제할 수 있습니다([보존 및 삭제](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/retention-and-erasure.md)).
- **매핑 수정:** 가져오기 도구를 다시 실행합니다. 결정적 키와 더 높은 `version`이 잘못된 행을 그 자리에서 대체합니다.

<a id="consolidate-optional-after-validation"></a>

## 통합(선택 사항, 검증 후)

엔티티 집합이 거의 중복되는 항목을 정리하려면 검증 **후에** `memory.consolidate`를 실행하세요.
`server/memory/app/consolidate.py`의 가드는 **고정(pinned)** 행을 제외하며, `memory_type`이나 **극성(polarity)**을 넘나들어 병합하지 않습니다. 따라서 `false_positive` 억제 항목이 긍정 관찰에 흡수될 수 없고, 원칙(principle)이 관찰(observation)을 흡수할 수도 없습니다.

백그라운드 통합 워커가 ClickGap에서 이미 요약된 `summary` 교훈을 다시 한 번 통합하지 않도록, 네임스페이스(`namespace.register`)에서 `consolidation_enabled=false`를 유지하세요.

<a id="related"></a>

## 관련 문서

- [메모리 쓰기](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/writing.md)
- [검색과 순위 지정](https://github.com/ClickHouse/loom/blob/main/docs/concepts/retrieval-and-ranking.md)
- [네임스페이스와 공유](https://github.com/ClickHouse/loom/blob/main/docs/features/namespaces-and-sharing.md)
