<!-- i18n: source=docs/guides/extraction-pipeline.md blob=a8d7e423185b09c1fa986972aa01b8c1b70d77d8 model=claude-sonnet-5-5 -->
> 이 페이지는 [영어 원문](https://github.com/ClickHouse/loom/blob/main/docs/guides/extraction-pipeline.md)을 기계 번역한 것이며, 영어 원문이 기준입니다.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/guides/extraction-pipeline.md), which is authoritative.

<a id="customize-the-extraction-pipeline"></a>

# 추출 파이프라인 사용자 지정

`memory.set_from_messages`는 대화 기록을 하나의 상태를 공유하는 순서 있는 단계 목록에 통과시킵니다. 요청은 `pipeline_steps`로 이 목록을 대체할 수 있습니다. 단계의 순서를 바꾸거나 건너뛸 수 있고, 운영자가 등록한 프로세스 내 단계를 호출할 수도 있습니다. 이 페이지는 단계 목록만 다룹니다. 추출이 하는 일은 [추출](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/extraction.md)에서 설명합니다.

`pipeline_steps`는 HTTP 본문 필드입니다. MCP 도구 스키마에는 노출되지 않으며 Python SDK의 `memory.set_from_messages` 헬퍼의 매개변수도 아닙니다. HTTP로 보내거나 `call(...)`을 통해 보내십시오. 생성된 op 입력 타입(`sdk/python/loom/ops.py`, `sdk/typescript/src/generated/ops.ts`)에는 이 필드가 포함되어 있습니다.

<a id="the-default-pipeline"></a>

## 기본 파이프라인

`server/memory/app/pipeline.py`의 `DEFAULT_PIPELINE`입니다.

| 단계                              | 역할                                                                                             |
| --------------------------------- | ------------------------------------------------------------------------------------------------ |
| `loom.fetch_namespace_categories` | 추출기에 전달할 네임스페이스의 태그 분류 체계를 불러옵니다.                                      |
| `loom.fetch_existing_memories`    | 가장 가까운 기존 메모리(최대 50개)를 추출기에 보여 줍니다.                                       |
| `loom.llm_extract`                | 추출 호출입니다.                                                                                 |
| `loom.drop_recall_echoes`         | 세션에 제공된 메모리를 다시 서술한 사실을 제거합니다.                                            |
| `loom.drop_session_echoes`        | 세션 자체의 Loom 호출이 담고 있던 내용을 다시 서술한 사실을 제거합니다.                          |
| `loom.drop_activity_narration`    | 지속되는 결과가 없는 도구 활동 서술을 제거합니다.                                                |
| `loom.drop_non_durable`           | 코딩 세션: 상태 스냅샷, 잡무, 계획을 제거합니다.                                                 |
| `loom.verify_durable`             | 코딩 세션: 남은 사실에 대해 검증기 패스를 실행합니다.                                            |
| `loom.hash_dedup`                 | 정규 해시가 이미 저장된 사실을 제거합니다.                                                       |
| `loom.bulk_write`                 | 봇 규칙(`about="agent"`)은 learnings 큐에 보관하고, 나머지는 `memory.bulk_set`으로 기록합니다.   |

코딩 세션이란 `extra_tags`에 코딩 에이전트 소스 태그(`source:claude-code`, `source:codex`, `source:cursor`)가 있는 대화 기록, 또는 궤적 재생을 말합니다. 두 코딩 단계는 다른 대화 기록은 그대로 통과시킵니다.

**`pipeline_steps`가 비어 있으면 이 목록을 실행합니다. 비어 있지 않으면 거기에 명시된 단계만 순서대로** 하나의 공유 상태에 대해 실행합니다. `loom.bulk_write`를 빼면 아무것도 기록되지 않습니다. 응답에는 여전히 추출된 내용이 보고됩니다.

<a id="run-a-shorter-pipeline"></a>

## 더 짧은 파이프라인 실행

새 데이터임을 알고 있는 일회성 가져오기를 위해, 중복 제거 컨텍스트 없이 추출하고 기록합니다.

```bash
curl -s "$LOOM_BASE_URL/v1/memory.set_from_messages" \
  -H "Authorization: Bearer $LOOM_API_KEY" -H 'Content-Type: application/json' -d '{
  "org": "acme",
  "namespace": "support",
  "agent": "importer",
  "messages": [{ "role": "user", "content": "..." }],
  "pipeline_steps": [{ "step_id": "loom.llm_extract" }, { "step_id": "loom.bulk_write" }]
}'
```

이를 위해서는 실제 LLM 제공자(`LOOM_LLM_PROVIDER`와 해당 키)가 필요합니다. 기본 스택의 가짜 제공자는 의미 있는 사실을 추출하지 못합니다.

<a id="step-behaviour"></a>

## 단계 동작

각 디스크립터는 `{"step_id": ..., ...}` 형태이며, 객체 전체가 해당 단계에 설정으로 전달됩니다.

- 요청당 **최대 16개 단계**입니다.
- **`hook.*` 단계는 거부되며** 422 `VALIDATION_FAILED`가 반환됩니다. 서버는 호출자를 대신해 외부로 나가는 호출을 하지 않습니다.
- **알 수 없는 단계 id**는 건너뛰며, 응답의 `pipeline_errors`에 `unknown_step:<id>`로 보고됩니다.
- **Fail-open 단계.** 가져오기 단계가 실패하면 `pipeline_errors`에 오류를 기록하고(예: `fetch_existing_failed:<error>`) 파이프라인은 계속 진행됩니다.
- **`loom.llm_extract`는 fail-closed입니다.** 추출 오류가 나면 요청이 502로 중단되므로, 대화 기록에 대해 조용히 `written=[]`로 응답하는 일이 없습니다. 디스크립터에 `{"judge": true}`를 지정하면 판정 게이트가 켜집니다. 기본 목록에서는 요청의 `judge_extraction=true`가 같은 역할을 합니다.
- **소요 시간.** 응답의 `stage_ms`에는 각 단계의 실제 소요 시간(`step.<step_id>`)이 담깁니다. 또한 각 단계는 단계 id를 속성으로 가진 `pipeline.step`이라는 이름의 OpenTelemetry 스팬 안에서 실행됩니다.

<a id="register-an-in-process-step"></a>

## 프로세스 내 단계 등록

운영자는 서버 프로세스가 시작할 때 import하는 코드에서 단계를 등록합니다.

```python
from server.memory.app.pipeline import WorkflowState, register_step


async def spam_filter(state: WorkflowState, config: dict) -> WorkflowState:
    state.extracted_facts = [
        f for f in state.extracted_facts if "spam" not in str(f.get("value", "")).lower()
    ]
    return state


register_step("operator.spam_filter", spam_filter)
```

이후 요청에서 `{"step_id": "operator.spam_filter"}`를 지정할 수 있으며, 디스크립터의 다른 키는 `config`로 전달됩니다. 이 단계는 프로세스 내에서 실행되며 `WorkflowState` 전체(신원, 메시지, 기존 메모리, 추출된 사실, 카운터)에 대한 읽기 및 쓰기 권한을 가집니다. `register_step`은 같은 id로 이미 등록된 항목을 대체하므로, 운영자 단계가 기본 제공 단계를 대체할 수도 있습니다.

`loom.bulk_write`를 대체하는 단계는 `about="agent"` 사실을 기록 대상에서 제외해야 합니다. 이 사실은 봇의 규칙이며, 기본 제공 단계는 이를 learnings 큐에 보관합니다.

> **경고:** `extracted_facts`를 제거하거나 다시 쓰는 단계는 그 단계를 지정하는 모든 호출자가 저장하는 내용을 바꿉니다. `loom.bulk_write` 뒤에 놓인 단계는 `state.written`에서 기록된 행을 볼 수 있지만 기록을 되돌릴 수는 없습니다.

Loom 자체에 기본 제공 단계를 추가하려면 `server/memory/app/pipeline.py`에 함수와 해당 `register_step` 호출을 추가하고 `DEFAULT_PIPELINE`에 넣은 뒤, 전후로 `make bench-quick`을 실행하십시오. 기본 목록은 모든 추출의 동작을 결정합니다.

<a id="limits"></a>

## 제한 사항

- 단계는 순차적으로 실행되며 분기나 병렬 실행은 없습니다.
- 기본 목록은 전역이며 네임스페이스별 파이프라인은 없습니다.
- 단계 단위 재시도는 없습니다.

<a id="related"></a>

## 관련 문서

- [추출](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/extraction.md)
- [Loom 확장하기](https://github.com/ClickHouse/loom/blob/main/docs/development/extending-loom.md)
- [메모리 기록하기](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/writing.md)
