<!-- i18n: source=docs/benchmarks/adding-a-case.md blob=63bcbeab205c463f07cd6aa007c0f8d4eeabefab model=claude-sonnet-5-5 -->
> 이 페이지는 [영어 원문](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/adding-a-case.md)을 기계 번역한 것이며, 영어 원문이 기준입니다.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/adding-a-case.md), which is authoritative.

<a id="adding-a-benchmark-case-dataset-or-suite"></a>

# 벤치마크 케이스, 데이터셋 또는 스위트 추가하기

커밋된 세트는 규모가 작으므로([세트 크기](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/methodology.md#set-sizes)), 새 케이스를 추가하는 것이 스위트를 개선하는 가장 저렴한 방법입니다. 먼저 파일의 헤더나 `_comment`를 읽어 보세요. 대부분 정답(gold)을 어떻게 만들었는지, 새 행이 어떤 조건을 충족해야 하는지 설명하고 있습니다.

<a id="add-a-case-to-an-existing-set"></a>

## 기존 세트에 케이스 추가

<a id="memory-qa-benchdatasetssynthetic_memory_qajson"></a>

### 메모리 QA (`bench/datasets/synthetic_memory_qa.json`)

대화에 질문을 추가하거나, 대화를 새로 추가합니다.

```json
{
  "conversation_id": "06-billing",
  "conversation": [{ "role": "user", "content": "Our invoices go out on the 3rd of each month." }],
  "questions": [{ "question": "When do invoices go out?", "gold_keys": ["invoice", "3rd"] }]
}
```

반환된 `memory_key` 또는 발췌문 중 하나라도 정답 문자열을 포함하면 적중으로 칩니다(대소문자를 구분하지 않고, 밑줄은 공백으로 취급). 올바른 메모리라면 반드시 포함하고 틀린 메모리에는 들어 있을 가능성이 낮은 단어를 정답으로 고르세요. 데이터는 합성으로 유지하세요. 제3자의 텍스트나 실존 인물은 넣지 않습니다.

**기준선이 달라집니다.** 0.80 게이트 임계값은 이 파일을 기준으로 기록되었습니다. 실제 배포 환경에서 변경 전후에 게이트를 실행하고, 값이 달라지면 `Makefile`의 `MIN_RECALL`을 업데이트하세요. 테스트: `tests/unit/test_bench_quality_gate.py`, `bench/test_quality.py`.

<a id="extraction-and-graph-regressions"></a>

### 추출 및 그래프 회귀

- `bench/fixtures/extraction_regressions.json`의 행은 `{id, messages, expected}` 형식입니다. 어떤 메모리의 키 또는 값에 반드시 나타나야 하는 구문입니다.
- `graph_regressions.json`의 행은 `{id, messages, expected_triples: [{subject, relation, object}], as_of}` 형식입니다.
- `_note` 필드에는 실패 유형을 적습니다. 하네스는 이 필드를 무시합니다.

`tests/unit/test_algorithm_gate_assets.py`는 모든 행에 메시지와 비어 있지 않은 기대값이 있는지 검사합니다. 케이스가 통과하는지 확인하려면 실제 추출용 LLM을 대상으로 `make bench-algorithm-gate`를 실행하세요.

<a id="code-plane-qa-benchfixturescode_intel_qajsonl"></a>

### 코드 플레인 QA (`bench/fixtures/code_intel_qa*.jsonl`)

한 줄에 JSON 객체 하나를 씁니다. `//`로 시작하는 줄은 주석입니다.

```json
{"id": "co037", "type": "conceptual", "query": "where are part merges scheduled", "gold": {"paths": ["src/Storages/MergeTree/MergeTreeBackgroundExecutor"]}}
{"id": "sd011", "type": "symbol_def", "query": "IColumn::insert", "gold": {"qualified_name": "DB::IColumn::insert", "path_prefix": "src/Columns/"}}
{"id": "ab011", "type": "abstain_probe", "query": "kubernetes ingress annotation for sticky sessions", "gold": {}}
```

유형별 정답 형식은 `bench/code_intel_qa.py`의 docstring에 있습니다(`callers`는 `expected_callers`, `hub`는 `path_prefix`와 `symbols`를 사용). 경로는 확장자를 뺀 접두사입니다. 선택 항목인 `lane`, `facets`, `namespace`는 행을 라우팅하고 분할하는 데 쓰입니다.

- **게이트에 적용하기 전에 실행 중인 인덱스로 정답을 검증하세요.** 하한 없이 한 번 실행하고, 불일치를 확인한 뒤 정답을 수정합니다.
- **기권 프로브는 저장소에 없는 코드에 관한 것이어야 합니다.**
- **메인 파일의 유형 집합을 유지하세요.** `bench/test_code_intel_qa.py`(`test_fixture_loads_and_validates`)는 `code_intel_qa.jsonl`이 최소 30개 행을 갖고, 유형이 정확히 `symbol_def`, `callers`, `conceptual`, `abstain_probe`뿐인지 검사합니다. hub, history, test-set 행은 별도의 레인 파일에 넣습니다.
- `make bench-code-intel-dry`는 계속 통과해야 합니다. 완벽한 합성 검색 결과를 가정했을 때 모든 레인이 상한 점수를 받아야 합니다.
- 마이닝된 레인은 직접 수정하지 않고 다시 생성합니다. `code_intel_mine_prs.py`와 `code_intel_ka_slice.py`가 파일과 헤더의 개수를 기록합니다.

<a id="coding-agent-lessons-benchfixturescoding_agent_qajson"></a>

### 코딩 에이전트 교훈 (`bench/fixtures/coding_agent_qa.json`)

`lessons`(`memory_key`를 키로 하는 메모리 행)와 `questions`(`question`, 분류 체계에 속한 `question_type`, `gold_keys`, 선택 항목 `fix_reuse`)에 항목을 추가합니다. 모든 정답 키는 실제 교훈 키를 가리켜야 하며, 그렇지 않으면 `bench/test_coding_agent_qa.py`(`test_load_corpus_rejects_dangling_gold_key`)가 실패합니다. `--dry`는 계속 통과해야 합니다.

<a id="op-planning-tasks-benchfixturesop_planning_tasksjsonl"></a>

### Op-planning 작업 (`bench/fixtures/op_planning_tasks.jsonl`)

작업은 `id`, `class`(`instruction_type`, `structure`), `prompt`, `gold` 플랜(도구, 필수 인자, 리터럴 인자), 저장소 `assertions`를 가집니다. `tests/unit/test_op_planning_bench.py`(`test_every_gold_tool_and_required_arg_exists_on_the_surface`)는 정답 도구나 인자가 MCP 서피스에 더 이상 없으면 실패합니다. op가 폐기된 작업은 삭제하지 말고 `op_planning_tasks_retired.jsonl`로 옮기세요.

<a id="skill-eval-tasks"></a>

### 스킬 평가 작업

`bench/skill_eval/tasks/` 아래의 브리프는 프런트 매터(`task`, `namespace`, `agent`, `traps`)를 갖고, 본문에 `skill-eval-…` 네임스페이스를 명시하며, 마지막에 해당 네임스페이스를 감사할 것이라고 밝히며 끝납니다. 같은 본문이 `integrations/claude-code-plugin/evals/loom-memory/<task>/prompt.md`의 `claude plugin eval` 케이스이기도 하며, 프런트 매터 이후는 바이트 단위로 동일합니다. `tests/unit/test_skill_eval.py`는 이 세 가지 규칙을 모두 검사하고 작업 목록을 고정하므로, 새 작업을 추가할 때는 해당 테스트도 업데이트하세요. [스킬 평가](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/skill-eval.md)를 참고하세요.

<a id="wire-in-a-new-dataset"></a>

## 새 데이터셋 연결하기

1. **커밋하지 마세요.** `bench/datasets/` 아래에 두거나 경로 플래그를 받도록 하세요. 데이터셋을 내려받아야 한다면 `longmemeval.py`처럼 `--fetch` 경로를 추가하거나, 파일이 없을 때 다운로드 명령을 출력하세요.
2. **하니스 docstring에 출처를 기록하세요.** 출처, 파일 이름, 항목 수, 데이터셋이 명시한 라이선스를 적습니다. [데이터셋](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/running.md#datasets)에 행을 추가하세요.
3. **데이터셋 자체의 카테고리를** 열로 유지하고, 공개된 표에서 비교 가능한 부분집합을 사용한다면 그 이름을 명시하세요.
4. **공개 벤치마크 이름은 `bench/` 안에서는 사용해도 됩니다.** 하지만 `server/` 아래 산문에서 어떤 선택의 이유로 쓰는 것은 안 됩니다 (`tests/unit/test_public_copy.py` `BENCHMARK_NAMES`).
5. 임시 네임스페이스는 `LOOM_NAMESPACE_BENCH_PREFIXES`(`server/settings/catalog.py`, 새 하니스는 `bench-` 사용)에 있는 접두사 아래에서 생성하세요. 그러면 `loom namespaces-gc`와 네임스페이스 GC가 중단된 실행이 남긴 것을 찾을 수 있습니다.

<a id="write-a-new-harness"></a>

## 새 하네스 작성하기

모든 하네스는 다음과 같은 형태를 따릅니다.

| 규칙                              | 방법                                                                                                                                                                                                                                                                            |
| --------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 독립 실행형 스크립트              | argparse, `--base-url`, `--token` (기본값 `LOOM_API_KEY`), `--out`; `PYTHONPATH=$PWD python bench/<name>.py` 형태와 모듈로 모두 실행 가능해야 합니다                                                                                                                              |
| 공통 HTTP 정책                    | `bench/_http.py`(`post_json` / `apost_json`)를 통해 전송합니다: Bearer 토큰, 429 대기, 횟수가 제한된 5xx 재시도, 응답 본문을 담은 오류                                                                                                                                           |
| 독스트링이 곧 매뉴얼              | 무엇을 측정하고 무엇을 측정하지 않는지, 데이터셋, 비용, 실행 명령을 적습니다                                                                                                                                                                                                    |
| 순수한 채점 코어                  | 채점 함수는 실제 실행 경로보다 위에 두고, 서버 없이 가져올 수 있어야 하며, 단위 테스트(`bench/test_<name>.py` 또는 `tests/unit/test_bench_<name>.py`)를 갖춥니다                                                                                                                 |
| `--dry` 또는 자체 테스트 모드     | 서버와 비용 지출 없이 합성 입력으로 채점기를 실행합니다                                                                                                                                                                                                                         |
| 평가용 읽기                       | 먼저 `bench/_gate.py`의 `measurement_fields("memory.search")` / `("memory.ask")`를 요청 본문에 병합하고, `--credit`를 제공합니다                                                                                                                                                |
| 임시 등록 네임스페이스            | 첫 쓰기 전에 `register_bench_namespace` + `remember_bench_namespace`를 호출하고, 작업은 `erase_on_exit` 안에서 수행하며, `--keep-namespaces` 플래그를 둡니다(`bench/bench_namespaces.py`)                                                                                         |
| 고정된 범위                       | 읽기에서 `namespace_scope: "this"`를 보냅니다                                                                                                                                                                                                                                   |
| 판정을 쓰나요? 공통 게이트를 사용 | `--repeats`, `--gate*`, `--credit`, `--judge-calibration`에는 `add_gate_arguments(parser)`를, 판정 결과에는 `gate_from_args`를 사용합니다. 리더와 판정 모델을 고정하고 재정의 시 경고합니다. `tests/unit/test_bench_gate_help_renders.py`는 도움말 텍스트의 단독 `%`가 `--help`를 깨뜨리는 이유를 보여 줍니다 |
| 측정 도구 기록                    | 임베더, LLM, 제공자, 그리고 `/v1/health`의 `build_commit`을 수치 옆에 함께 기록합니다(`quality.py`의 `run_metadata` 참고)                                                                                                                                                        |
| 실패와 누락을 구분해 보고         | 요청 오류를 집계해 출력하고, 누락으로 채점하지 않습니다                                                                                                                                                                                                                         |
| 쌍체 비교                         | 클러스터 키를 지정한 `paired_bootstrap`으로 항목별 차이를 구하고, 하한값을 기준으로 반영합니다                                                                                                                                                                                  |

그다음에는 다음을 수행합니다.

- [`bench/README.md`](https://github.com/ClickHouse/loom/blob/main/bench/README.md)의 알맞은 섹션에 항목을 추가하고, 자주 묻는 질문에 답하는 하네스라면 "When to run what" 표에 행을 추가합니다.
- 누군가 정기적으로 실행하는 게이트에만 Makefile 타깃을 추가하고, 기준값은 재정의할 수 있는 변수로 둡니다.
- `make format`(ruff가 `bench/`를 포맷합니다), `pytest bench -q`, 그리고 카피 린트를 실행합니다.

```bash
.venv/bin/python -m pytest tests/unit/test_public_copy.py tests/unit/test_bench_namespaces.py -q
```

카피 린트는 `bench/`를 검사합니다. 코드, 주석, 픽스처에는 제품명, 인명, 내부 호스트나 네임스페이스, 논문 인용, 결정 기록 참조가 없어야 합니다.

<a id="related"></a>

## 관련 문서

- [방법론](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/methodology.md): 게이트와 부트스트랩이 계산하는 내용.
- [실행](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/running.md): 명령과 출력.
- [테스트](https://github.com/ClickHouse/loom/blob/main/docs/development/testing.md): 단위 테스트 및 통합 테스트 모음.
