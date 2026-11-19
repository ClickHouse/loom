<!-- i18n: source=docs/benchmarks/skill-eval.md blob=c855282f25bb802cf3b249aa01819ec8e21227b7 model=claude-sonnet-5-5 -->
> 이 페이지는 [영어 원문](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/skill-eval.md)을 기계 번역한 것이며, 영어 원문이 기준입니다.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/skill-eval.md), which is authoritative.

<a id="skill-eval"></a>

# 스킬 평가

스킬 평가는 에이전트가 남긴 네임스페이스를 기준으로 `loom-memory` 스킬을 채점합니다. 스킬은 무엇을 어디에 둘지 규정합니다. 주장(claim)은 읽기 쉬운 키 아래에 일반 텍스트로 `memory.set` 한 번으로 저장하고, 문서는 짧은 포인터 행과 함께 knowledge에 넣고, 고유한 식별자를 가진 항목의 표는 records에 넣고, 리스(lease)는 working memory에 두며, 수정은 같은 키를 다시 씁니다. 평가는 작업을 실행하고, `namespace.audit`으로 네임스페이스를 채점하며, 심각한(critical) 발견 사항이 하나라도 있으면 실패로 처리합니다.

<a id="the-pieces"></a>

## 구성 요소

| 구성 요소         | 위치                                                 | 설명                                                                                                                                                                                                                                                                                                                                    |
| ----------------- | ---------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 작업 브리프       | `bench/skill_eval/tasks/*.md`                        | 안티패턴을 유도하는 브리프 세 개입니다. 긴 로그, 스니펫, 표, 리스, 보관해야 할 큰 모델이 포함된 알림 조사(`sre_alert_investigation`), 구조화된 필드가 있는 PR 묶음(`pr_review_batch`), 두 번 수정된 노트(`research_note_revisions`)입니다. 각 브리프는 자신의 네임스페이스를 명시하고 감사를 받는다고 알립니다. |
| 러너              | `bench/skill_eval.py`                                | 작업을 실행한 뒤(`--runner claude-code`, `codex`, `manual`, `replay`), `namespace.audit`(1시간 윈도우, 샘플 5개)를 실행하고, 이어서 형태 카운터를 실행하여 결과 JSON을 기록합니다.                                                                                                                                                              |
| 기록된 에이전트   | `bench/skill_eval/replays/{bad,good}_agent.jsonl`    | `{op, body}` 스트림입니다. bad 에이전트는 값을 이중 인코딩하고, 해시 키를 사용하며, 메모리로 저장된 리스를 폴링합니다. good 에이전트는 [조사 에이전트 메모리](https://github.com/ClickHouse/loom/blob/main/docs/guides/investigation-agent-memory.md) 가이드를 따릅니다.                                                                                                                   |
| 플러그인 평가 스위트 | `integrations/claude-code-plugin/evals/loom-memory/` | 동일한 브리프를 `claude plugin eval` 케이스로 만든 것으로, 모의 Loom 서버(`evals/mocks/loom/`)를 대상으로 도구 트레이스를 기준으로 채점합니다.                                                                                                                                                                                                           |
| 단위 테스트       | `tests/unit/test_skill_eval.py`                      | 핸들러 자체의 분류기를 사용하는 가짜 Loom을 대상으로 두 리플레이를 러너에 통과시킵니다. bad는 예상된 코드와 함께 실패하고 good은 통과합니다. 또한 작업 목록과 평가 프롬프트의 본문을 고정합니다.                                                                                                                          |

각 브리프의 front matter에는 `task`, `namespace`, `agent`, `traps`(브리프가 유도하는 안티패턴)가 들어 있습니다. 러너는 리플레이 스트림의 `$ORG`, `$NAMESPACE`, `$AGENT`를 치환하므로 하나의 기록을 어떤 조직에서도 실행할 수 있습니다.

<a id="the-score"></a>

## 점수

`pass`는 감사 결과가 **치명적 문제 0건**이고 **경고가 최대 1건**(`MAX_WARNINGS_TO_PASS`)임을 뜻합니다. 보고서는 각 발견 사항을 건수, 검사한 행에서 차지하는 비율, 바이트 수, 작성자 측 해결 방법과 함께 나열한 뒤, 행 자체에서 읽은 형태 카운터를 보여 줍니다. 따라서 등급은 감사 결과만으로 정해지지 않습니다.

| 카운터                   | 의미                                                                                                        |
| ------------------------ | ----------------------------------------------------------------------------------------------------------- |
| `rows`                   | 활성 메모리 행                                                                                              |
| `readable_key_share`     | 마지막 세그먼트가 긴 16진수 문자열이 아니고 sha 길이의 세그먼트도 없는 키(감사의 `hash_keys` 규칙)         |
| `plain_text_value_share` | 문자열이면서 직렬화된 JSON이 아닌 값                                                                        |
| `documents`              | 지식 문서(`knowledge.list`)                                                                                 |
| `records`                | 타입이 지정된 레코드(등록된 스키마별 `memory.record.query`)                                                 |
| `working_memory_blocks`  | 워킹 메모리 블록(`working_memory.render`)                                                                   |

형태 읽기는 감사 **이후에** 실행되므로 에이전트의 읽기로 집계되지 않습니다. 토큰으로 읽을 수 없는 카운터는 0으로 보고되지 않고 `shape.skipped` 아래에 나열됩니다.

<a id="run-it"></a>

## 실행하기

<a id="replay-no-model-is-called"></a>

### 리플레이 (모델을 호출하지 않음)

어떤 Loom이든 사용할 수 있으며, 로컬 스택이면 충분합니다.

```bash
export LOOM_BASE_URL=http://localhost:7777 LOOM_API_KEY=<writer token> LOOM_ORG=<org>
.venv/bin/python bench/skill_eval.py --task sre_alert_investigation --runner replay \
  --replay bench/skill_eval/replays/bad_agent.jsonl            # exits 1: FAIL
.venv/bin/python bench/skill_eval.py --task sre_alert_investigation --runner replay \
  --replay bench/skill_eval/replays/good_agent.jsonl --baseline   # exits 0: PASS
```

실행할 때마다 새 네임스페이스를 사용하세요(`--namespace skill-eval-sre-$(date +%s)`). 감사 윈도우는 1시간이므로, 네임스페이스를 재사용하면 이전 실행에서 읽은 내용이 채점에 포함됩니다.

오프라인에서는 서버 없이 같은 검사를 단위 테스트로 실행할 수 있습니다.

```bash
.venv/bin/python -m pytest tests/unit/test_skill_eval.py -q
```

<a id="a-live-agent-with-the-skill-loaded"></a>

### 스킬을 로드한 실제 에이전트

```bash
.venv/bin/python bench/skill_eval.py --task sre_alert_investigation --runner claude-code --baseline
```

이 실행에는 모델 토큰 비용이 듭니다. 저장소 루트에서 다음을 실행합니다.

```bash
claude -p --plugin-dir integrations/claude-code-plugin \
  --mcp-config <generated file: loom at $LOOM_BASE_URL/mcp?toolset=all, Bearer $LOOM_API_KEY> \
  --strict-mcp-config --allowedTools "mcp__loom__*" "Skill" "Read" \
  --permission-mode bypassPermissions --output-format json --max-turns 60 \
  "<the task body>"
```

- `--plugin-dir`는 체크아웃된 소스에서 스킬을 로드하므로, 이 실행은 작업 트리에 있는 스킬 텍스트를 측정합니다.
- `toolset=all`은 기본 `core` 목록에서 빠지는 `memory_schema.register`와 `memory.record.write`를 목록에 포함합니다. `knowledge.upsert`는 `all`에서도 목록에 없으며(core와 atomic 목록 모두 포함하지 않습니다), 에이전트는 이를 `loom_op`를 통해 호출합니다.
- 생성된 MCP 설정에는 토큰이 들어 있습니다. 이 파일은 `0600` 권한으로 만들어지고 실행이 끝나면 삭제됩니다.
- `--max-turns`(기본값 60)는 실행의 상한을 정합니다.

`--runner codex`는 같은 서버 연결로 `codex exec`를 실행하며, Codex 플러그인이 설치되어 있으면 그 플러그인이 스킬을 제공합니다.

<a id="any-other-agent"></a>

### 그 밖의 에이전트

`--runner manual`은 브리프를 출력합니다. 원하는 에이전트를 브리프에 명시된 네임스페이스에 대해 실행한 다음, 에이전트가 남긴 결과를 채점하세요.

```bash
.venv/bin/python bench/skill_eval.py --task pr_review_batch --runner manual
.venv/bin/python bench/skill_eval.py --task pr_review_batch --runner manual --audit-only
```

같은 `--audit-only`로, 실제 서버를 대상으로 한 `claude plugin eval` 실행이 기록한 네임스페이스도 채점할 수 있습니다.

<a id="compare-with-a-baseline"></a>

### 베이스라인과 비교하기

스킬을 수정하고 다시 실행한 뒤 비교합니다.

```bash
.venv/bin/python bench/skill_eval.py --task sre_alert_investigation --runner claude-code \
  --compare bench/out/skill_eval_sre_alert_investigation_baseline.json
```

델타 표에는 발견 코드(finding code)마다 한 행(심각도와 개수, 이전과 이후)이 있고, 형태 카운터(shape counter)마다 한 행이 있습니다. 실제 에이전트는 결정적이지 않으므로, 스킬 수정은 한 번이 아니라 여러 번 실행해 비교하세요.

<a id="output"></a>

### 출력

실행할 때마다 `bench/out/skill_eval_<task>_<runner>_<ts>.json`이 기록됩니다(`--out-dir`로 위치를 바꿀 수 있습니다). `--baseline`은 `bench/out/skill_eval_<task>_baseline.json`도 기록하며, 이 파일이 `--compare`가 읽는 기준입니다. `bench/out/`는 gitignore 처리되어 있습니다.

<a id="the-plugin-eval-suite"></a>

## 플러그인 평가 스위트

```bash
claude plugin eval integrations/claude-code-plugin --trust-plugin --no-publish \
  --case 'loom-memory/*' --runs 3 --threshold 1.0 --json evals-result.json
```

이 명령은 도구 트레이스를 기준으로 각 실행을 오프라인에서 채점합니다. Loom MCP 서버는 고정된 응답을 반환하는 목(mock)이므로(`evals/mocks/loom/`) 아무것도 기록되지 않습니다. 채점기는 문서에 정의된 채점기 유형만 사용합니다.

- 해시 키, JSON 문자열을 다시 JSON으로 감싼 값, 약 2 KB를 넘는 값, 리스 또는 락 키, 버전이 붙은 리비전 키가 입력에 포함된 메모리 쓰기에 대해 `max: 0`을 지정한 `tool_used`: `namespace.audit`이 critical 또는 warning으로 채점하는 형태입니다.
- `loom-memory`와 일치하는 `tool_used: Skill`: 플러그인이 실행되었음을 나타내는 지표입니다.
- 작업 메모리와 항목별 클레임에 대한 양의 최솟값.

`llm` 채점기는 없습니다. 본문을 루브릭으로 읽어 채점하는 방식은 게이트가 될 수 없기 때문입니다. 케이스는 이름 글롭으로 필터링할 수 있습니다(`--case sre_alert_investigation`). 플러그인이 확인되면 기본값이 되는 `--ablation with-without`는 플러그인 없이 같은 프롬프트를 실행했을 때 같은 채점기에 얼마나 자주 걸리는지 보고합니다.

이 프레임워크에는 스크립트 채점기가 없으므로 네임스페이스 수준의 채점은 별도의 두 번째 명령으로 수행합니다. `--mocks off --allow-real-servers --allow-tools "mcp__plugin_loom_loom__*"`와 `LOOM_API_KEY`에 설정한 쓰기 권한 토큰으로 에이전트가 해당 케이스의 네임스페이스에 쓰고, `bench/skill_eval.py --task <case> --runner manual --audit-only`가 이를 채점합니다.

<a id="reading-a-failure"></a>

## 실패 읽기

각 finding의 해결 방법은 작성자 측 수정입니다. `loom-optimize` 스킬은 같은 코드를 이미 존재하는 행을 다시 정리하는 작업에 대응시킵니다([네임스페이스 최적화](https://github.com/ClickHouse/loom/blob/main/docs/guides/optimize-namespace.md)).

| 발견 항목                                  | 지켜지지 않은 규칙                                            |
| ------------------------------------------ | ------------------------------------------------------------- |
| `double_encoded_values`                    | 값은 한 번만 보내세요. 문자열에 `json.dumps`를 두 번 적용하지 마세요. |
| `hash_keys`                                | 키는 해시가 아니라 읽을 수 있는 `<scope>/<kind>/<id>` 형식이어야 합니다. |
| `coordination_as_memory`                   | 리스와 락은 작업 메모리에 두어야 합니다.                      |
| `exact_key_store`                          | 키로 폴링하는 대신 포인터를 작업 메모리에 유지하세요.         |
| `oversized_values`, `json_prefix_excerpts` | 문서는 짧은 포인터 행과 함께 knowledge로 보내야 합니다.       |

SRE 작업에서 `documents: 0`으로 감사를 통과한 실행은 검색이 닿지 않는 곳에 큰 모델을 두었습니다. 판정만 보지 말고 shape 줄을 읽으세요.

<a id="adding-a-task"></a>

## 작업 추가하기

새 브리프에는 front matter, 본문에 명시된 `skill-eval-…` 네임스페이스, 그리고 해당 네임스페이스가 감사(audit)될 것이라는 마무리 문장이 필요합니다. 또한 본문이 바이트 단위로 동일한 plugin eval 케이스와, `tests/unit/test_skill_eval.py`가 고정해 두는 작업 목록의 업데이트도 필요합니다.
자세한 내용은 [케이스 추가하기](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/adding-a-case.md#skill-eval-tasks)를 참고하세요.

<a id="what-this-does-not-measure"></a>

## 이 벤치마크가 측정하지 않는 것

회상 품질입니다. 에이전트가 작성한 클레임이 다음 주의 질문에 답할 수 있는지는 회상 벤치마크가 다룰 일이며([개요](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/overview.md)), 이 벤치마크는 회상을 불가능하게 만드는 형태를 평가합니다.

<a id="related"></a>

## 관련 문서

- [조사 에이전트 메모리](https://github.com/ClickHouse/loom/blob/main/docs/guides/investigation-agent-memory.md): 좋은 리플레이가
  따르는 방식입니다.
- [네임스페이스 최적화](https://github.com/ClickHouse/loom/blob/main/docs/guides/optimize-namespace.md): 감사에서 표시된 행을 다시 작업하는 방법입니다.
- [Claude Code 플러그인](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/claude-code.md) 및
  [Codex](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/codex.md).
- [`integrations/claude-code-plugin/evals/README.md`](https://github.com/ClickHouse/loom/blob/main/integrations/claude-code-plugin/evals/README.md).
