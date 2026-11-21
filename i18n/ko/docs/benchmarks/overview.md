<!-- i18n: source=docs/benchmarks/overview.md blob=f3e3a30fcb2c5a330a968c35eb380c8c210ad12f model=claude-sonnet-5-5 -->
> 이 페이지는 [영어 원문](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/overview.md)을 기계 번역한 것이며, 영어 원문이 기준입니다.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/overview.md), which is authoritative.

<a id="benchmarks-overview"></a>

# 벤치마크 개요

랭킹 레버, 유사도 게이트, 캐시 임계값, 추출 프롬프트는 단위 테스트로 판단할 수 없는 동작을 바꿉니다. 이러한 항목은 각각 `bench/` 아래의 하니스가 결정하며, 하니스는 실제 프로바이더를 사용해 실행 중인 서버에 HTTP로 접속하여 동작합니다. 이 섹션에서는 [방법론](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/methodology.md), 스위트를 [실행하는 방법](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/running.md)과 데이터의 출처, [커버리지 매트릭스](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/coverage.md), 저장소에서 재현하는 [결과](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md), [케이스를 추가하는 방법](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/adding-a-case.md), 그리고 [스킬 평가](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/skill-eval.md)를 다룹니다.

[`bench/README.md`](https://github.com/ClickHouse/loom/blob/main/bench/README.md)에는 모든 하니스가 나열되어 있습니다. 각 스크립트의 모듈 docstring이 곧 해당 스크립트의 매뉴얼이며, 프로토콜, 플래그, 실행 명령이 담겨 있습니다.

<a id="what-the-suite-measures"></a>

## 스위트가 측정하는 것

하니스는 독립 실행형 스크립트입니다. argparse와 HTTP 클라이언트로 구성되며, 공유 픽스처나 테스트 러너는 없습니다.

| 영역                     | 질문                                                                                           | 주요 하니스                                                                                                                      |
| ------------------------ | ---------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------- |
| 검색 재현율              | 올바른 메모리가 상위 K개 안에 들어왔는가?                                                      | `quality.py`, `retrieval_recall.py`, `k_curve.py`, `plane_ablation.py`, `hard_distractors.py`                                    |
| 답변 정확도              | 고정된 리더와 심사자 아래에서, 검색된 메모리로 만든 답변이 올바른가?                            | `longmemeval.py`, `locomo_qa.py`, `memoryagentbench.py`, `membench.py`, `beam.py`, `musique.py`, `msc_dmr.py`, …                 |
| 쓰기 측 품질             | 추출 단계가 턴에 담긴 내용을, 그리고 그 내용만 기록했는가?                                     | `extraction_eval.py`, `graph_eval.py`, `memory_integrity.py`, `supersession_audit.py`                                            |
| 시간과 개정              | 대체된 사실이 구간이 끝난 뒤에는 숨겨지고 구간 안에서는 보이는가?                              | `fama_forgetting.py`, `mutation_sequence.py`, `fact_consolidation.py`, `pi_llm.py`                                               |
| 에이전트와 연산          | 에이전트가 올바른 op를 골랐고 저장소를 올바른 형태로 남겼는가?                                 | `op_planning.py`, `skill_eval.py`, `op_trajectories.py`, `procedure_replay.py`                                                   |
| 비용                     | 답변 하나 또는 세션 하나에 드는 비용은 얼마인가?                                               | `ask_cost_rollup.py`, `session_ab.py`, `route_replay.py`, `ch_cost_by_op.py`                                                     |
| 안전 및 개인정보 보호    | 삭제가 모든 티어에 반영되는가? 프롬프트만 쓰는 공격자가 메모리를 추출하거나 오염시킬 수 있는가? | `erasure_compliance.py`, `memory_extraction_redteam.py`, `memory_poison.py`, `sensitivity_leakage.py`                            |
| 코드 인덱스              | 코드 인덱스가 정의, 호출자, 개념적 답변을 찾고, 나머지에는 답을 보류하는가?                    | `code_intel_qa.py`, `code_plane_ablation.py`, `code_shunt_tokens.py`, `code_ask_cache_threshold/`                                |
| 문서 파싱                | 추출기가 문서의 텍스트, 제목, 표, 페이지를 읽고 아무것도 잃지 않는가?                          | `docparse/` (오프라인)                                                                                                           |
| 트래커                   | `tracker.similar`가 이슈가 종료될 때 기준이 된 이전 중복 이슈를 찾는가?                        | `tracker_similar_bench.py`                                                                                                       |
| 지연 시간과 인덱스 상태  | 검색 시간은 어디에 쓰이는가? 벡터 인덱스가 전수 검색과 같은 결과를 반환하는가?                 | `search_latency.py`, `vector_tenant_recall.py`, `ann_final_staleness.py`, `vector_index_vs_exact.py`, `scripts/ops/benchmark.py` |
| 쓰기 부하                | 지속적인 쓰기 속도는 어느 정도의 지연 시간과 ClickHouse 파트 수를 요구하는가?                  | `write_rate.py`                                                                                                                  |
| 규모                     | 네임스페이스, 조직, 키가 늘어날 때 메모리는 어떤 한계에 먼저 도달하는가?                       | `scale_load.py`                                                                                                                  |
| 소비자 인수 테스트       | 한 종류의 배포 환경이 허용 가능한 비용으로 정확한 답변을 얻는가?                               | `acceptance/{code_agent,memory_app,plugin,harness}.py`                                                                           |

[`bench/memoryarena/`](https://github.com/ClickHouse/loom/blob/main/bench/memoryarena/README.md)(서로 의존하는 작업의 이후 세션을 채점하는 에이전트 짐)와 [`bench/memorydata/`](https://github.com/ClickHouse/loom/blob/main/bench/memorydata/README.md)(Loom을 다른 방법과 나란히 실행하는 테스트베드)는 Loom을 연결해 사용하는 외부 하니스입니다. 실행 방법은 각 README를 참고하세요.

랭킹 코드를 변경하는 것이 아니라 Loom을 평가하는 중이라면 [Loom 평가하기](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/evaluate.md)에서 시작하세요. 이 문서는 하니스 목록을 제품 연동, 자체 메모리 워크로드, 코드 인텔리전스, 공개된 결과 재현을 위한 짧은 의사결정 경로로 정리해 줍니다.

<a id="entry-points"></a>

## 진입점

| 진입점                       | 실행 내용                                                                  | 필요 조건                                                         |
| ---------------------------- | -------------------------------------------------------------------------- | ----------------------------------------------------------------- |
| `make bench-quality-gate`    | 커밋된 합성 데이터셋에 대한 `quality.py`. recall 0.80 미만이면 종료 코드 1  | 실제 임베더와 추출 LLM이 있는 서버                                 |
| `make bench-algorithm-gate`  | 회귀 픽스처에 대한 `extraction_eval.py`와 `graph_eval.py`                  | 실제 추출 LLM이 있는 서버                                          |
| `make bench-quick`           | 고정된 reader와 judge로 셔플된 30개 항목에 대한 `longmemeval.py`           | 다운로드한 LongMemEval 파일, `LOOM_API_KEY`, `OPENAI_API_KEY`     |
| `make bench-matrix`          | `matrix.py`: LongMemEval에서 검색 모드 × rerank × 풀 너비                  | `bench-quick`과 동일                                              |
| `make bench-code-intel-dry`  | `code_intel_qa.py --dry`: 완벽한 합성 검색 결과에 대한 채점기              | 없음 (오프라인)                                                   |
| `make bench-code-intel`      | 롤아웃 하한선을 적용한 `code_intel_qa.py`                                  | 인덱싱된 ClickHouse 저장소가 있는 서버, 실제 임베더               |
| `make bench-code-legs`       | 세 가지 leg 제거 실험에 대한 `code_intel_qa.py --compare-extra`, 각각 쌍으로 비교 | `bench-code-intel`과 동일                                  |
| `make bench-clickgap-replay` | `clickgap_code_replay.py`: PR 리뷰 봇의 호출 순서를 재생                   | 해당 소비자의 PR이 담긴 재생용 JSONL                              |
| `make bench-clickgap-golden` | 해당 소비자의 골든 픽스처에 대한 `clickgap_golden_replay.py`               | `GOLDEN_DIR`, 이 저장소에 없는 비공개 픽스처                      |
| `make bench`                 | `scripts/ops/benchmark.py`: 하나의 op에 대한 동시 부하, p50/p95/p99 및 RPS | 서버                                                              |
| `make test-unit`             | `pytest bench -q`도 실행: 채점 코어와 데이터셋 리더                        | 없음 (오프라인)                                                   |

그 외의 모든 하네스는 `python bench/<name>.py`로 실행합니다 ([실행 방법](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/running.md)).

<a id="how-a-bench-differs-from-a-production-caller"></a>

## 벤치가 프로덕션 호출자와 다른 점

벤치는 동일한 operation을 HTTP로 호출하지만, 세 가지 차이가 있습니다.

1. **평가용 읽기.** 크레딧을 부여하는 읽기는 다음 패스가 순위를 매길 때 기준이 되는 행을 강화하므로, 벤치가 자신의 결과를 바꿔 버립니다. 실행에 `--credit`을 지정하지 않으면 하니스는 `memory.search`에 `auto_touch: false`를, `memory.ask`에 `reinforce: false`를 보냅니다.
2. **일회용 벤치 네임스페이스.** 각 네임스페이스는 `traffic_class`와 `kind`를 `bench`로 하여 등록됩니다. 이에 따라 벤치 속도 제한 버킷(기본적으로 기본 한도의 절반), 벤치용 ClickHouse 우선순위, 별도의 비용 장부 열이 적용되며, 실행이 끝나면 삭제됩니다([벤치 네임스페이스](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/methodology.md#bench-namespaces)).
3. **고정된 측정 도구.** 리더와 심사 모델은 하니스별로 고정되어 있으며 `LOOM_LLM_MODEL`을 따르지 않습니다. 서버의 추출 모델, 임베더, 설정이 변수입니다. `--answerer loom-ask`(LongMemEval) 또는 `--reader ask`(LoCoMo)는 `memory.ask` 자체를 평가합니다.

서버의 기본값은 이러한 벤치가 선택한 값입니다. `LOOM_LLM_MODEL` 또는 `LOOM_EMBEDDING_MODEL` 기본값을 바꿀 때는 `make bench-quick`으로 시작하며, 새 임베더에는 [기준선 재측정](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/running.md#re-baselining-after-an-embedder-change)이 필요합니다.

<a id="coverage-and-what-it-leaves-out"></a>

## 커버리지와 다루지 않는 범위

- **CI에는 품질 신호가 없습니다.** CI는 가짜 provider로 실행되므로 검색이 아니라 scorer를 테스트합니다
  ([이유](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/methodology.md#fake-providers-make-scores-meaningless)). 누군가 실제 배포 환경에서 게이트를 실행하지 않으면
  랭킹 회귀가 눈에 띄지 않은 채 병합될 수 있습니다.
- **커밋된 측정값이 적습니다.** [결과](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md)에는 커밋된 각 측정값이 `bench/results/` 아래의 아티팩트, 실행에 사용한 도구,
  재현 명령과 함께 나열되어 있습니다. 여러분의 데이터와 provider에 대한 수치는
  직접 배포한 환경에서 생성해야 합니다.
- **대부분의 하네스는 저장소에 없는 데이터가 필요합니다.** 서드파티 코퍼스는 다운로드해야 하며
  ([데이터셋](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/running.md#datasets)), 리플레이, relied-on 세트, 트래커 정답, 트레이스 기반 지연 시간은
  한 배포 환경의 비공개 데이터를 읽습니다.
- **코드 픽스처는 특정 저장소에 한정됩니다.** ClickHouse 레인에는 ClickHouse/ClickHouse 인덱스가,
  Langfuse 레인에는 해당 저장소의 인덱스가 필요합니다. 이 수치는 다른 코드베이스에 대해서는
  거의 알려 주지 않습니다.
- **이름이 붙은 시스템 간 점수표는 없습니다.** 공개 데이터셋을 쓰는 하네스는 Loom의 수치와,
  이를 공개된 표 옆에 놓고 비교하는 데 필요한 열을 보고합니다. 저장소는 Loom을
  자체 빌드 및 arm과만 비교합니다.
- **하드웨어 기록이나 용량 모델이 없습니다.** 호스트를 기록하는 하네스는 없습니다.
  `scripts/ops/benchmark.py`는 하나의 동시성에서 하나의 op를 측정하고, `search_latency.py`는
  `memory.search`를 네임스페이스 크기별 단계로 나누어 측정합니다. 둘 다 실제 에이전트 혼합 부하나
  멀티 테넌트 경합은 모델링하지 않습니다.
- **스킬 평가는 회상이 아니라 형태를 채점합니다**
  ([스킬 평가](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/skill-eval.md#what-this-does-not-measure)).

<a id="what-a-pass-does-not-prove"></a>

### 통과가 증명하지 않는 것

- **게이트 통과는 하한선입니다.** 빌드가 하나의 임베더로 작은 세트에서 기록된 임계값을 넘었다는 뜻입니다.
  여러분의 데이터에서 회상이 좋다는 의미는 아닙니다.
- **회상은 답변 품질이 아닙니다.** 메모리가 1위로 랭크되어도 잘못 해석되거나 오래된 내용일 수 있습니다.
  판정 축과 실패 단계를 확인하세요.
- **답변 정확도는 에이전트의 성공이 아닙니다.** 대부분의 하네스는 한 번에 질문 하나만 채점합니다.
  에이전트가 메모리로 무엇을 했는지 측정하는 것은 `code_plane_ablation.py`와 에이전트 gym뿐입니다.
- **안전성 벤치는 지정된 공격만 측정합니다.** `memory_poison.py`와
  `memory_extraction_redteam.py`는 각자의 공격 형태에 대해서만 보고합니다. `erasure_compliance.py`는
  모든 `loom.*` 테이블의 모든 String 형 컬럼에서 카나리를 검색하며, 외부 백엔드는 자체 삭제 범위만큼만
  완전합니다.

<a id="related"></a>

## 관련 문서

- [검색 및 랭킹](https://github.com/ClickHouse/loom/blob/main/docs/concepts/retrieval-and-ranking.md): 검색 벤치마크가 측정하는 대상을 설명합니다.
- [코드 인덱스](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md): 코드 벤치마크가 측정하는 대상을 설명합니다.
