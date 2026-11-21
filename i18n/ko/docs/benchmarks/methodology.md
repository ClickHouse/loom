<!-- i18n: source=docs/benchmarks/methodology.md blob=0e0708fc624ae94fe8f285a72bac00e00924c567 model=claude-sonnet-5-5 -->
> 이 페이지는 [영어 원문](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/methodology.md)을 기계 번역한 것이며, 영어 원문이 기준입니다.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/methodology.md), which is authoritative.

<a id="benchmark-methodology"></a>

# 벤치마크 방법론

이 페이지에서는 하네스가 무엇을 계산하는지, 변화량(delta)을 노이즈와 비교해 어떻게 판단하는지, 두 수치를 언제 비교할 수 있는지, 그리고 수치에 어디서 편향이 생기는지를 다룹니다. 규칙은 코드에 있습니다:
`bench/_gate.py`(공유 게이트), `bench/bootstrap.py`, `bench/judge_calibration.py`,
`bench/bench_namespaces.py`, `bench/failure_taxonomy.py`
([소스 진입점](#source-entry-points)).

<a id="recall-and-answer-accuracy-are-separate-axes"></a>

## 재현율과 답변 정확도는 별개의 축입니다

**Recall@K**는 관련 메모리가 상위 K개 안에 들어왔는지를 묻습니다. **답변 정확도**는 합성된 답변이 올바른지를 묻습니다. 한쪽이 포화 상태에 이르러도 다른 쪽은 낮게 유지될 수 있습니다. `longmemeval.py`는 recall@K와 평가된 정확도를 별도의 지표로 출력하고, `coding_agent_qa.py`는 R@5 / R@10 / MRR을 평가된 QA 정확도와 따로 출력합니다. 리더(reader)와 평가자(judge) 없이 임베더만으로 얻은 대표 수치는 재현율 수치입니다. 이를 정확도 수치와 나란히 놓고 비교하지 마세요.

<a id="task-categories"></a>

## 작업 카테고리

각 하네스는 원본 데이터의 카테고리 레이블을 그대로 유지하므로, 결과를 카테고리별로 읽을 수 있습니다.

| 하네스                | 카테고리                                                                                                                                                        |
| --------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `locomo_qa.py`        | 1 멀티홉, 2 시간, 3 오픈 도메인, 4 싱글홉, 5 적대적. `overall_comparable`은 1–4를 포함하며(공개된 열), `overall_all`은 5를 기권으로 채점합니다. |
| `longmemeval.py`      | 데이터셋의 `question_type`(`--question-type`). `--stale-check`는 대체된 이전 값을 반환한 잘못된 knowledge-update 답변에 레이블을 붙입니다.                    |
| `memoryagentbench.py` | 정확한 검색, 테스트 시점 학습, 장거리 이해, 충돌 해결.                                                                                                          |
| `beam.py`             | 열 가지 메모리 능력이며, 데이터셋의 루브릭 판정기로 채점합니다. 이벤트 순서 항목에는 Kendall τ_b × F1이 추가됩니다.                                              |
| `membench.py`         | 사실 수준과 성찰 수준의 참여 및 관찰 시나리오.                                                                                                                  |
| `code_intel_qa.py`    | 행의 `type`: `symbol_def`, `callers`, `conceptual`, `hub`, `history`, `test_set`, `abstain_probe`. 여기에 `lane`과 `facets`가 추가됩니다.                      |
| `coding_agent_qa.py`  | 버그 수정 반영, 인프라 수정, 리팩터링 위치, 불안정한 테스트, 성능 누수, 스키마 마이그레이션, 다중 세션 인과, 시간, 팀 컨벤션.                                    |
| `op_planning.py`      | 각 작업의 `class`: `instruction_type` × `structure`.                                                                                                            |

<a id="ground-truth"></a>

## 정답 기준

| 종류                            | 위치                                                                                                 | 구성 방식                                                                                                                          | 약점                                                                           |
| ------------------------------- | ---------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------ |
| 부분 문자열 정답 키             | `bench/datasets/synthetic_memory_qa.json`, `extraction_regressions.json`, `graph_regressions.json`   | 직접 작성한다. 반환된 `memory_key` 또는 발췌문에 정답 문자열이 하나라도 있으면 적중이다(대소문자 구분 없음, `_`는 공백으로 취급)  | 의도적으로 느슨하다. 장황한 발췌문이 우연히 일치할 수 있다                     |
| 공개된 정답                     | 서드파티 데이터셋 ([데이터셋](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/running.md#datasets))                                                | 데이터셋의 정답과 근거 레이블                                                                                                      | 데이터셋의 레이블 오류와 포화 상태를 그대로 물려받는다                         |
| 직접 레이블링한 쿼리 → 키 쌍    | `retrieval_recall.py`, `plane_ablation.py`, `hard_distractors.py` `--dataset`                        | 사용자가 기존 코퍼스를 대상으로 쿼리에 레이블을 단다                                                                               | 규모가 작다. 레이블은 저장소에 포함하지 않는다                                 |
| 사용 기반 레이블                | `relied_on_set.py` → `bench/datasets/relied_on/`                                                     | 검색이 반환한 키를 에이전트가 짧은 시간 창 안에 이후 활용한 경우(`used` / `used_cited` 이벤트)                                     | id가 아닌 시간으로 조인한다. 현재 순위가 이미 상위에 올린 항목에 유리하다      |
| 병합된 PR에서 추출              | `bench/fixtures/code_intel_mined_*.jsonl`, `code_intel_history.jsonl`, `code_plane_ablation.py mine` | 쿼리 = 이슈 또는 PR 제목, 정답 = 변경된 `src/` 파일. 텍스트에 정답 경로의 파일명(stem)이 나오는 행은 제외하고 그 비율을 보고한다 | 인덱스에 모든 수정이 들어 있다. 절댓값은 상한이므로 짝지은 차이를 읽어야 한다 |
| 모델이 작성하고 모델이 검증     | `bench/fixtures/code_intel_oss.jsonl`                                                                | 쿼리와 경로를 모델이 작성하고 모델 판정기가 확인한 서드파티 코퍼스                                                                 | 미적중이 레이블 탓일 수 있다. `secondary` 경로는 인정하지 않는다               |

구조적 코드 정답(`symbol_def`, `callers`)은 정확하지만 업스트림 저장소에 대한 지식을 바탕으로 작성했다. 새로 백필할 때마다 게이트 없이 한 번 실행하고, 불일치를 읽고 정답을 고친다. 잘못된 정답은 코퍼스 버그다.

판정기 보정은 자체 프로브를 만든다(`build_probes`). 항목의 정답을 독자가 답하는 방식으로 표현한 것은 정답 프로브이고, 같은 카테고리의 다른 항목의 정답은 오답 프로브다. 적대적 항목은 제외한다.

<a id="judges"></a>

## 판정 모델

리더와 판정 모델은 평가 대상 시스템이 아니라 측정 도구의 일부입니다.

- **하네스별 고정.** `longmemeval.py`와 `coding_agent_qa.py`는 `gpt-4o-mini`로 읽고 `gpt-4o-2024-08-06`(LongMemEval 판정 스냅샷)으로 판정합니다.
  `locomo_qa.py`는 `gpt-4.1-mini`로 읽고 `gpt-4.1`로 판정합니다.
  `BENCH_READER_MODEL` / `BENCH_JUDGE_MODEL`로 이를 덮어쓸 수 있으며, 이 경우 비교 가능성 경고가 출력됩니다. `OPENAI_BASE_URL`은 호환되는 게이트웨이를 가리킵니다.
- **Loom의 답변 채점.** `longmemeval.py --answerer loom-ask`와
  `locomo_qa.py --reader ask`는 `memory.ask`를 채점합니다. `--ask-arms`는 하나의 수집 결과에서 `memory.ask` 구성들을 비교합니다.
- **루브릭은 파일입니다.** `bench/rubrics/entailment.md`는
  `dialogue_consistency.py`가 불러오는 NLI 프롬프트입니다. `beam.py`는 데이터셋의 루브릭 판정 모델을 사용합니다.
- **일치도.** `locomo_qa.py --second-judge-model`은 표본을 다시 채점하고
  일치도와 Cohen's kappa를 보고합니다.

어휘 기반 점수는 공개된 어휘 기반 표와 나란히 놓기 위해서만 존재합니다.
`longmemeval.py --lexical-scores`는 **단순화된** 답변에 대해 토큰 F1, BLEU-1, ROUGE-L을 추가합니다(`--simplify-model`, 기본값은 판정 모델이며, 그렇지 않으면 레이블이 붙은 휴리스틱을 사용합니다). 단순화하지 않으면 이 점수들은 장황함을 채점하게 되어, 틀린 날짜가 올바른 문장보다 높은 점수를 받을 수 있습니다
([참고 값](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#lexical-scoring-reference-values)). 이 점수가 판정 모델을 대체하는 일은 없습니다.

<a id="metrics"></a>

## 지표

| 지표                       | 정의                                                                                                                                                        | 계산 위치                                        |
| -------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------ |
| 재현율@K                   | 상위 K개 안에 정답 항목이 있는 질문의 비율                                                                                                                  | `quality.py` `summarise`, `retrieval_recall.py`  |
| MRR                        | 첫 번째 정답 항목 순위의 역수(1 / 순위)의 평균(정답이 없으면 0)                                                                                             | 위와 같음                                        |
| rank@1                     | 정답이 1위인 질문의 비율                                                                                                                                    | 위와 같음                                        |
| 정밀도@K                   | 반환된 항목 중 정답 항목의 비율의 평균                                                                                                                      | `quality.py`                                     |
| `best_cos_rank`            | 코사인 유사도가 가장 높은 항목의 순위. 1위와 차이가 나면 융합 과정에서 더 약한 일치 항목이 앞에 놓였다는 뜻                                                 | `retrieval_recall.py`                            |
| 정확도@1, 호출자 집합 F1   | 코드 구조의 정확도                                                                                                                                          | `code_intel_qa.py`                               |
| 답변 보류 정밀도 / 재현율  | 재현율 = 답변을 보류한 프로브 수 / 전체 프로브 수, 정밀도 = 답변을 보류한 프로브 수 / 전체 보류 수                                                           | `code_intel_qa.py`                               |
| `placebo@k`                | 질문마다, 시스템이 모든 개념 질문에 대해 반환한 경로 풀에서 (시드를 고정해) 같은 개수의 경로를 뽑은 값. 이보다 낮은 재현율은 순위가 아니라 풀 커버리지의 문제다 | `code_intel_qa.py`                               |
| 답변당 토큰 수             | 답변한 질문당 `tokens_returned`의 평균 / p90                                                                                                                | `code_intel_qa.py`, 인수 벤치마크                |
| 판정 정확도                | 반복 실행에서 다수 레이블이 CORRECT인 비율                                                                                                                  | `_gate.py`                                       |
| 뒤바뀜 비율, 불일치율      | 그룹 내에서는 반복된 항목 중 레이블이 일치하지 않은 항목의 비율, 그룹 간에는 다수 레이블이 서로 다른 쌍의 비율                                              | `_gate.py` `flip_rate`, `GateResult.discordance` |
| 실패 단계                  | `extraction`, `graph_resolution`, `retrieval_ranking`, `compression_loss`, `reader`, `not_verbatim`                                                         | `failure_taxonomy.py`                            |

실패 단계는 놓친 항목마다 파이프라인 순서대로 계산한다. 회수되지 않았으면 `retrieval_ranking`, 정답이 회수된 발췌문에 있는데 답이 틀렸으면 `reader`, 정답이 회수된 메모리의 원본 근거에만 있으면 `compression_loss`, 정답이 어디에도 원문 그대로 없으면 `not_verbatim`이다. `lme_diagnose.py`는 LongMemEval에서 놓친 항목을 이 레이블과 함께 덤프한다. 검색이 바뀌지 않았는데 `reader`가 퇴보했다면 원인은 reader에 있다.

요청 실패는 비율 계산에서 제외하며, 놓친 항목으로 세지 않는다(`code_intel_qa.py`는 `errors(excluded)`를 출력한다). `quality.py`는 HTTP 실패가 하나라도 발생하면 중단된다.

<a id="baselines"></a>

## 기준선

Loom은 동일한 인제스트에 대해 동일한 측정 도구로, 다른 기법 및 자체 구성과 비교합니다.

| 기준선                                                            | 위치                                                                               |
| ----------------------------------------------------------------- | ---------------------------------------------------------------------------------- |
| 어휘 검색 단독, 어휘 검색 + 쿼리 확장, 벡터 검색 단독             | `make bench-code-legs`: 각각 하이브리드 기본값에 대한 쌍체 B 구성                  |
| 요약 전용 메모리                                                  | `msc_dmr.py`의 `digest` 구성과 `ask`, `search`, `search_recall`, `search_reader`   |
| 원문 그대로 저장 vs 사실 추출                                     | `memoryarena/` 및 `memorydata/`의 `verbatim` / `extract` 모드                      |
| 롱 컨텍스트, BM25, 일반 밀집 검색                                 | `memoryarena/memory_server.py` 구성                                                |
| 플러그인을 끈 코딩 에이전트                                       | `code_plane_ablation.py`의 A 구성                                                  |
| 파일 일괄 읽기                                                    | `code_shunt_tokens.py`의 "cold" 구성                                               |
| 브루트 포스 최근접 이웃 검색                                      | `vector_tenant_recall.py`                                                          |
| 강력한 합성 모델 하나 또는 저렴한 합성 모델 하나                  | 고정된 검색 결과에 대한 `route_replay.py`의 strong, cheap, cascade 구성            |
| 원본 시스템의 기본 랭킹                                           | `clickgap_migration_validate.py`: 내보내기 파일의 `recall_count` 순서              |
| 플레인별 제거 실험                                                | `plane_ablation.py`의 `execution_planes` 마스크                                    |

<a id="statistics"></a>

## 통계

<a id="paired-clustered-bootstrap"></a>

### 쌍체 클러스터 부트스트랩

랭킹 변경은 평균이 아니라 쌍체 델타의 **하한**을 기준으로 배포합니다.
`paired_bootstrap`은 항목별 델타(B에서 A를 뺀 값)를 받아 클러스터 단위로 복원 추출하고(10,000회 리샘플, `seed=7`), 2.5번째와 97.5번째 백분위수를 읽습니다. 하한이 0보다 클 때만 개선으로 인정합니다. 같은 메커니즘에 관한 항목은 함께 움직이므로, 하니스는 클러스터 키를 전달합니다. 클러스터 키는 대화, 질문 유형이며, 코드의 경우 경로의 첫 두 세그먼트(`cluster_of_path`)입니다. 클러스터 수가 너무 적으면 구간이 지나치게 좁아지므로, 하한과 함께 `clusters`를 확인하세요. `code_plane_ablation.py`는 시드 간 작업별 평균에 대해 쌍체 Wilcoxon 부호 순위 검정을 추가로 수행합니다.

<a id="repeats-and-the-majority-label"></a>

### 반복 실행과 다수결 레이블

리더는 temperature 0에서도 결과가 달라질 수 있으며, 합성 단계의 검색은 쿼리를 다시 분해합니다. `--repeats N`은 동일한 검색 결과에 대해 각 항목을 N번 질의하고 판정한 뒤 **다수결** 레이블을 유지합니다. 동률은 WRONG으로 처리하며, 모든 실행이 오류일 때만 항목을 ERROR로 처리합니다. 게이트 실행은 세 번 반복합니다(`GATE_REPEATS`). 반복 실행의 `top_keys`에 대한 `identical_share`를 보면 결과가 뒤집힌 원인이 리더인지 검색인지 알 수 있습니다. 반복은 레이블 노이즈를 줄여 주며([시뮬레이션](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#majority-voting-on-simulated-reader-noise)), 샘플링 노이즈는 항목 수를 늘려야만 줄어듭니다.

<a id="the-verdict"></a>

### 판정

`paired_gate`는 각 비교군의 다수결 레이블에 대해 부트스트랩을 실행합니다.

| 판정         | 조건                                                                                                                                                             |
| ------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `pass`       | 95% 하한이 `--gate-min-delta`를 넘음(기본값 0, 음수 값은 비열등성 게이트)                                                                                        |
| `fail`       | 95% 상한이 임계값보다 낮음                                                                                                                                       |
| `unresolved` | 구간이 임계값에 걸쳐 있음. 귀무 밴드(`--gate-band`, 기본값 `DEFAULT_BAND_PP` = 4.0 pp) 안이면 노이즈 수준이고, 밴드 밖이면 검정력이 부족한 실행임               |

어느 한쪽 비교군의 반복 실행에서 불안정했던 항목은 안정 항목만 모은 쌍으로도 따로 보고합니다.

**자신의 밴드를 측정하세요.** 기본값은 하나의 하니스와 하나의 리더에서 얻은 것입니다. 같은 구성을 비교군 A와 B로 두고 `--repeats 3 --gate`로 실행한 다음, 결과에서 더 넓은 쪽(`band_from_null`)을 실제 비교의 `--gate-band`로 전달하세요.

<a id="how-wide-the-interval-is"></a>

### 구간의 폭

폭은 n뿐만 아니라 비교군이 서로 다르게 판정한 쌍체 항목의 수에 따라 달라집니다. 아래 행은 고정된 델타에 대한 `paired_bootstrap` 결과입니다(+1 = B 정답·A 오답, −1 = 그 반대, 0 = 일치).

| n                    | 승 / 무 / 패                     | 평균 델타  | 95% 구간          | 해석                                           |
| -------------------- | -------------------------------- | ---------- | ----------------- | ---------------------------------------------- |
| 60                   | 1 / 59 / 0                       | +1.7 pp    | +0.0 .. +5.0 pp   | 질문 하나. 하한이 0을 넘지 못함                |
| 30                   | 2 / 26 / 2                       | 0          | −13.3 .. +13.3 pp | 같은 구성의 쌍: 13포인트 변동이 구간 안에 있음 |
| 60                   | 5 / 50 / 5                       | 0          | −10.0 .. +10.0 pp |                                                |
| 164                  | 13 / 138 / 13                    | 0          | −6.1 .. +6.1 pp   |                                                |
| 500                  | 40 / 420 / 40                    | 0          | −3.6 .. +3.6 pp   |                                                |
| 60                   | 6 / 52 / 2                       | +6.7 pp    | −1.7 .. +16.7 pp  | 미결(unresolved)인 "7포인트 개선"              |
| 60                   | 8 / 50 / 2                       | +10.0 pp   | +0.0 .. +20.0 pp  | 하한이 0에 닿을 뿐이므로 pass가 아님           |
| 500                  | 40 / 440 / 20                    | +4.0 pp    | +1.0 .. +7.0 pp   | n이 커서 통과하는 더 작은 개선                 |
| 60, 10개씩 6개 클러스터 | 8 / 50 / 2, 두 클러스터에 승이 집중 | +10.0 pp   | +0.0 .. +30.0 pp  | 상관된 항목이 구간을 넓힘                      |

```bash
.venv/bin/python - <<'EOF'
from bench.bootstrap import paired_bootstrap
def row(w, t, l, clusters=None):
    d = [1.0] * w + [-1.0] * l + [0.0] * t
    print(paired_bootstrap(d, clusters).line(unit="pp", scale=100))
row(1, 59, 0); row(2, 26, 2); row(5, 50, 5); row(13, 138, 13); row(40, 420, 40)
row(6, 52, 2); row(8, 50, 2); row(40, 440, 20)
row(8, 50, 2, [f"c{i // 10}" for i in range(60)])
EOF
```

<a id="set-sizes"></a>

### 세트 크기

작은 세트로는 비슷한 비교군의 순위를 매길 수 없습니다.

| 세트                                   | 크기                         | 항목 하나의 비중                  |
| -------------------------------------- | ---------------------------- | --------------------------------- |
| `synthetic_memory_qa.json`             | 질문 15개                    | 6.7포인트: 붕괴만 잡아냄          |
| `code_intel_qa.jsonl` 개념 슬라이스    | 60개 중 36개 질문            | 2.8포인트                         |
| 알고리즘 게이트 픽스처                 | 추출 19개, 그래프 케이스 2개 | 스모크 테스트                     |
| `code.ask` 캐시 쌍                     | 유사 표현 42쌍, 상이 43쌍    | 2포인트 이상                      |

<a id="judge-calibration"></a>

### 판정자 보정

`bench/judge_calibration.py`는 프로브에 대해 판정자의 참양성률과 위양성률을 측정합니다. 관측 정확도는 `TPR·p + FPR·(1−p)`이므로, 쌍체 델타는 Youden의 `J = TPR − FPR`만큼 줄어듭니다. `--judge-calibration <json>`을 사용하면 게이트가 `Δ/J`를 보고하고, 보정 세트 자체의 분산(델타 방법)만큼 구간을 넓힙니다. `J ≤ 0`인 판정자는 정보를 전혀 제공하지 않으며, 이 경우 입력은 그대로 전달됩니다.

<a id="when-two-numbers-compare"></a>

## 두 숫자를 비교할 수 있는 경우

다음 항목이 모두 일치해야 합니다. 하나라도 다르면 해당 비교를 비교 불가로 표시합니다.

| 일치해야 하는 항목        | 이유                                                                                        |
| ------------------------- | ------------------------------------------------------------------------------------------- |
| 축                        | Recall@K와 judged accuracy는 서로 다른 지표입니다                                           |
| 리더와 판정자             | 더 강한 리더는 추론 비중이 큰 범주의 결과를 바꾸고, 다른 판정자는 레이블을 바꿉니다         |
| 데이터셋 분할과 슬라이스  | LongMemEval oracle 대 S, LoCoMo 범주 1–4 대 1–5, 한 레인 대 전체 파일                       |
| 검색 모드                 | 고정하지 않은 기본값은 `LOOM_MEMORY_SEARCH_DEFAULT_MODE`이며, 다른 모드는 ablation입니다    |
| 임베더와 서버 빌드        | 임베더를 바꾸면 유사도 척도 전체가 달라지며, `quality.py`의 `.meta.json`에 둘 다 기록됩니다 |
| 인덱스 리비전(코드)       | `code_intel_qa.py`는 답변을 제공한 `as_of.commit`과 오래된 항목 수를 명시합니다             |
| 크레딧 부여               | `--credit` 실행은 읽은 내용을 강화합니다                                                    |
| 정확도 분모               | 기권을 제외하면 기권을 더 많이 하는 쪽이 유리하게 보입니다                                  |

**기권.** 판정 게이트는 기권을 오답으로 계산합니다. `route_replay.py`는 두 값을 모두 보고합니다. `accuracy` = 정답 / (정답 + 오답)이고, `accuracy_abstain_wrong` = 정답 / (정답 + 오답 + 기권)이며 후자가 게이트의 분모입니다. 오류가 난 질문은 두 값 모두에서 제외합니다. 판정이 참조하는 값을 인용하고, 어느 값인지 명시합니다.

**상한과 플라시보.** 마이닝한 코드 레인은 모든 수정이 포함된 인덱스를 대상으로 실행됩니다(각 ablation 작업의 `fix_in_index`). 절대값이 아니라 쌍을 이룬 차이를 인용합니다. `placebo@5` / `placebo@10`을 넘지 못하는 recall은 풀 커버리지입니다. 세트가 아주 작으면 플라시보가 지나치게 강해집니다. 대부분의 arm이 상한에 가까운 점수를 내는 세트로는 arm을 구분할 수 없으므로, 범주별 결과와 stable-only 쌍을 확인합니다.

<a id="fake-providers-make-scores-meaningless"></a>

## 가짜 프로바이더는 점수를 무의미하게 만듭니다

체크아웃의 `.env`는 `LOOM_EMBEDDING_MODEL=fake-ngram-3072`를 사용하고, CI는 `fake-ngram-1024`를 사용하며, 둘 다 `LOOM_LLM_PROVIDER=fake`로 실행됩니다(릴리스된 스택은 `local`로 임베딩하고 가짜 LLM은 그대로 유지합니다). 가짜 임베더(`server/embedding/fake.py`)는 문자 트라이그램을 버킷으로 해시하므로, 유사도는 의미가 아니라 철자를 따릅니다. 가짜 LLM(`server/llm/fake.py`)은 앵커 문구를 기준으로 정해진 형태의 JSON을 반환하므로, 실제로 아무것도 추출하지 않고도 파이프라인이 실행됩니다. 이 둘을 사용하면 다음과 같습니다.

- 리콜은 질문과 저장된 텍스트 사이의 문자열 중복을 측정합니다.
- 추출 벤치는 프롬프트가 아니라 가짜 LLM의 출력을 측정합니다.
- 적응형 보정은 가짜 임베더를 건너뛰기 때문에, 유사도 게이트는 스칼라 기본값을 사용합니다.

가짜 임베딩에서 통과한 게이트는 게이트가 없는 것보다 나쁩니다. 측정된 적 없는 안전성을 보고하기 때문입니다. 그래서 `make bench-quality-gate`와 `make bench-code-intel`은 CI에 포함되어 있지 않으며(이를 강제하려면 유료 프로바이더 시크릿이 필요합니다), CI는 순수 점수 계산 코드(`pytest bench`)와 게이트 함수의 단위 테스트(`quality.gate`: 경계값은 통과하고, 0/0은 실패)만 실행합니다. 벤치 서버에는 `LOOM_REQUIRE_REAL_PROVIDERS=true`를 설정하세요. 그러면 가짜 프로바이더이거나 키가 없는 프로바이더일 때 `/v1/health`가 `degraded`를 보고하므로, 실행에 시간을 쓰기 전에 알 수 있습니다.

<a id="reproducibility-controls"></a>

## 재현성 제어

| 제어                       | 메커니즘                                                                                                                                                                           |
| -------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 결정적 리샘플링            | `paired_bootstrap`과 `paired_gate`는 `seed`에 대해 결정적입니다. 게이트는 `resamples`와 `seed`를 기록합니다                                                                       |
| 결정적 샘플                | 샘플 플래그는 시드를 받습니다(`musique.py --seed`, `judge_calibration.py`, mined-lane 샘플)                                                                                         |
| 기록된 서버 계측 정보      | `quality.py`는 `/v1/health`에서 가져온 `embedding_model`, `embedding_dim`, `llm_model`, `llm_provider`, `build_commit`을 담은 `.meta.json`을 씁니다. `code_intel_qa.py`는 `as_of.commit`을 명시합니다 |
| 고정된 범위                | 검색 하니스는 `namespace_scope: "this"`를 전송하므로 읽기 가능한 다른 네임스페이스의 행이 윈도우에 들어오지 않습니다. 임시 프로브도 마찬가지로 지정해야 합니다                                             |
| 평가 읽기                  | `measurement_fields()`는 `auto_touch: false` / `reinforce: false`를 전송합니다. `--credit`로 옵트인할 수 있습니다. 이전에 읽은 적이 있는 네임스페이스에서는 `reinforcement_weight: 0`을 전달하거나 새 행을 사용하세요            |
| 새 네임스페이스            | 항목 또는 대화마다 일회용 네임스페이스를 하나씩 만들고 종료 시 삭제합니다                                                                                                                |
| 워밍업                     | `search_latency.py --warm 1`은 쿼리마다 첫 번째 호출을 버리므로, 임베딩 캐시 미스가 검색 시간에 포함되지 않습니다                                                                |
| 토큰 수명                  | 실행이 끝난 뒤에도 유효한 토큰을 발급하세요([실행](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/running.md#2-mint-a-token-that-outlives-the-run))                                                                                  |

<a id="bench-namespaces"></a>

### 벤치 네임스페이스

데이터를 시딩하는 하니스는 일회용 네임스페이스(`lme-…`, `locomo-…`, `mab-…`,
`qabench-…`, 새 하니스는 `bench-`)를 만듭니다. `bench/bench_namespaces.py`는 다음과 같이 동작합니다.

- 첫 쓰기 전에 각 네임스페이스를 `traffic_class = "bench"`, `kind = "bench"`,
  `capture_generation_io = True`(기본값)로 등록합니다.
- 작업을 `erase_on_exit` 안에서 실행합니다. 이는 실행이 끝나거나 실패하거나 중단될 때 생성한 모든 이름을 한 번의 영구 삭제(purging erasure)로 지웁니다. 본문은 `external: false`를 설정하므로 외부 백엔드에는 접근하지 않습니다.
- `--keep-namespaces`를 사용하면 나중에 다시 채점(`--namespaces-json`)할 수 있도록 네임스페이스를 보존하며, 유휴 수집기가 건드리지 않도록 `kind = "memory"`로 등록합니다.

삭제에는 org-admin 토큰이 필요합니다. 거부되면 하니스가 `loom namespaces-gc` 명령을 로그에 남깁니다. `namespace_gc` 워커는 `LOOM_NAMESPACE_GC_IDLE_HOURS`가 지난 유휴 `kind = bench` 네임스페이스를 삭제하며, 생성된 이름은 `LOOM_NAMESPACE_BENCH_PREFIXES`에 있는 접두사로 시작하므로 중단된 실행의 네임스페이스도 찾아냅니다.

<a id="http-policy"></a>

### HTTP 정책

하니스는 `bench/_http.py`를 통해 요청을 보냅니다(`post` / `apost`는 응답을, `post_json` / `apost_json`은 본문을 반환합니다).

| 응답                                          | 정책                                                                                                           |
| --------------------------------------------- | -------------------------------------------------------------------------------------------------------------- |
| 429                                           | `Retry-After`만큼 기다린 뒤 재시도합니다(없으면 1 s, 0.2–60 s로 제한). 대기는 5xx 예산을 소모하지 않습니다 |
| 5xx                                           | `attempts`개의 요청을 보낼 때까지 `0.5 × 2^n` s 백오프로 재시도합니다                                          |
| 그 외 상태 ≥ 400, 또는 마지막 429 / 5xx       | 본문의 앞부분과 함께 `httpx.HTTPStatusError`를 발생시킵니다                                                    |
| 전송 오류                                     | 그대로 전파됩니다. 재시작을 견디는 하니스는 자체 루프를 유지합니다                                             |

<a id="sources-of-bias"></a>

## 편향의 원인

| 원인                     | 영향                                                                               | 코드상의 완화 방법                                                   |
| ------------------------ | ---------------------------------------------------------------------------------- | -------------------------------------------------------------------- |
| 작성자가 만든 세트       | 시스템을 튜닝하는 사람이 합성 세트를 작성하고 그 위에서 임계값을 정했습니다        | 스모크 게이트로만 사용하고, 순위는 공개 세트나 사용 기반으로 라벨링한 세트로 결정합니다 |
| 느슨한 부분 문자열 정답  | 긴 발췌문에 정답 단어가 우연히 포함될 수 있습니다                                  | recall과 함께 MRR, rank@1, precision@K를 사용합니다                  |
| 상한선 성격의 코드 레인  | 인덱스에 이미 모든 수정 사항이 들어 있습니다                                       | 헤더 경고, `fix_in_index`를 사용하고, 짝지은 차이값만 인용합니다     |
| 모델이 작성한 정답       | 서드파티 코드 검색 라벨을 모델이 작성하고 검증했습니다                             | `secondary` 경로는 인정하지 않으며, 짝지은 비교로만 해석합니다       |
| 사용 기반 라벨           | 신뢰하는 세트에는 현재 순위가 보여 준 항목만 들어 있습니다                         | 부트스트랩 경계에서 짝지은 스윕을 수행하고, 여러 출처 중 하나로만 사용합니다 |
| 판정자의 관대함          | LLM 판정자가 주제는 맞지만 틀린 답을 일정 비율로 통과시킵니다                      | `judge_calibration.py`의 참양성률/위양성률, `--second-judge-model`   |
| 판정자와 리더의 변동     | 고정된 이름 뒤에서 제공자가 리비전을 바꾸면 라벨이 달라집니다                      | 데이터셋이 지정하면 고정된 스냅샷을 사용하고, 두 비교군을 함께 실행합니다 |
| 자기 강화                | 읽기를 인정하면 다음 단계에서 매기는 순위가 강화됩니다                             | 기본적으로 평가용 읽기를 사용합니다                                  |
| 제외된 오류              | 실패가 어려운 항목에 몰리면 비율이 높아집니다                                      | 오류 수를 출력하며, `quality.py`는 실패가 하나라도 있으면 중단합니다 |
| 에이전트 벤치마크의 유출 | 에이전트가 네트워크나 git 히스토리를 통해 수정 내용을 찾아냅니다                   | ablation의 제외 원장이 해당 셀을 제외하며, 실패 시 닫힌 방식으로 동작합니다 |
| 포화                     | 대부분의 시스템이 만점을 받는 카테고리로는 비교군의 순위를 가릴 수 없습니다        | 카테고리별 결과, 안정 항목만 비교하는 쌍                             |
| 하네스 동등성            | 외부 테스트베드는 모든 방법에 대해 모든 쿼리를 자체 템플릿으로 감쌉니다            | 어댑터가 이 동등성을 유지합니다                                      |

<a id="source-entry-points"></a>

## 소스 진입점

- `bench/_gate.py`: `paired_gate`, `verdict_for`, `majority_label`, `flip_rate`,
  `JudgeCalibration`, `measurement_fields`, `add_gate_arguments`.
- `bench/bootstrap.py`: `paired_bootstrap`, `cluster_of_path`.
- `bench/judge_calibration.py`: `build_probes`.
- `bench/bench_namespaces.py`: `register_body`, `erase_on_exit`, `erase_on_exit_sync`.
- `bench/failure_taxonomy.py`: `classify_answer_failure`.
- `bench/answer_scoring.py`: `token_f1`, `bleu1`, `rouge_l`, `cohen_kappa`.
- 테스트: `tests/unit/test_bench_gate.py`, `test_bench_bootstrap.py`,
  `test_bench_judge_calibration.py`, `test_bench_namespaces.py`,
  `test_bench_failure_taxonomy.py`, `test_answer_scoring.py`,
  `test_bench_gate_help_renders.py`.

<a id="related"></a>

## 관련 문서

- [실행](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/running.md) 및 [결과](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md).
- [검색과 순위 지정](https://github.com/ClickHouse/loom/blob/main/docs/concepts/retrieval-and-ranking.md): 순위를 변화시키는 요소.
