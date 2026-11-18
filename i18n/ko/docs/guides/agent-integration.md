<!-- i18n: source=docs/guides/agent-integration.md blob=456e73fdf0db8acbbcc8465c94067cbbf581e556 model=claude-sonnet-5-5 -->
> 이 페이지는 [영어 원문](https://github.com/ClickHouse/loom/blob/main/docs/guides/agent-integration.md)을 기계 번역한 것이며, 영어 원문이 기준입니다.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/guides/agent-integration.md), which is authoritative.

<a id="integrate-loom-into-an-agent"></a>

# 에이전트에 Loom 통합하기

이 가이드는 에이전트가 Loom에 무엇을 보내고 응답을 어떻게 읽는지를 다룹니다. 클라이언트 표면, 아이덴티티, 무엇을 기록할지, 어떻게 읽을지를 설명합니다. [메모리](https://github.com/ClickHouse/loom/blob/main/docs/concepts/memory.md)와
[아이덴티티와 네임스페이스](https://github.com/ClickHouse/loom/blob/main/docs/concepts/identities-and-namespaces.md)를 이해하고 있다고 가정합니다. 플러그인이 제공되는 코딩 에이전트([Claude Code](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/claude-code.md),
[Codex](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/codex.md))는 대부분의 내용을 플러그인에서 얻습니다.

사용자가 아니라 에이전트를 위해 작성된 같은 규칙이 `loom` Agent Skill(`skills/loom/`)입니다. 플러그인이 없는 에이전트는 서버에서 이를 읽습니다. 실험적인 [`mentor.guide`](https://github.com/ClickHouse/loom/blob/main/docs/reference/operations/mentor.md#mentorguide)는 루프, 각 종류의 상태를 어디에 두는지, 응답을 읽는 방법(`part: "skill"`)을 반환하거나, 모든 plane과 해당 op(`part: "reference"`)를 반환합니다. 이를 에이전트의 시스템 프롬프트에 넣으세요.

```python
system_prompt = "You triage support tickets.\n\n" + await agent.guide()
```

MCP에서는 프라이머가 해당 호출을 알려 주며, Agent-Skills 클라이언트는 `npx skills add ClickHouse/loom --skill loom`으로 설치합니다.

<a id="1-choose-the-client-surface"></a>

## 1. 클라이언트 인터페이스 선택

| 인터페이스                                                   | 사용하는 경우                                                                                   | 제공되는 기능                                                                                                                                                                      |
| ------------------------------------------------------------ | ----------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **MCP** (`/mcp`)                                             | 에이전트가 이미 MCP를 사용하는 LLM 클라이언트일 때(Claude Code, Codex, Cursor, 데스크톱 앱). | `?toolset=`별로 선별된 도구 목록, 모델을 위해 작성된 도구 설명, 연결에서 가져오는 기본 ID 값. 호출 시점은 모델이 결정합니다.                                                         |
| **Python SDK** (`clickhouse-loom`, `from loom import Agent`) | 에이전트가 직접 작성한 Python 프로세스일 때.                                                    | 플레인별 타입 클라이언트, 재시도 및 멱등성 키, 모델의 도구 루프용 `agent.tools()`, 실행 스코프, 프레임워크 어댑터(LangGraph `LoomStore`, Strands `LoomMemoryStore`).                  |
| **TypeScript SDK** (`@clickhouse/loom`)                      | 에이전트가 Node 또는 브라우저 측 서비스일 때.                                                   | `Datapack` / `Agent`, 생성된 op 테이블로 타입이 지정되는 `agent.call(op, body)`, `AgentTools`, `Session`.                                                                           |
| **HTTP** (`POST /v1/<op>`)                                   | 그 외의 언어를 쓰거나 일회성 스크립트를 작성할 때.                                              | 모든 op와 동일한 JSON 본문. 재시도와 `Idempotency-Key` 헤더는 직접 처리해야 합니다.                                                                                                 |

네 가지 인터페이스는 모두 하나의 레지스트리에서 같은 op를 제공합니다. 누가 호출을 결정하는지에 따라 선택하세요. 모델이 결정하면 MCP, 코드가 결정하면 SDK입니다. 많은 에이전트가 둘을 함께 사용합니다. 시작할 때 컨텍스트를 불러오고 끝날 때 실행을 기록하는 데는 SDK를, 모델이 무엇을 기억하거나 조회할지 선택하는 데는 MCP 도구를 사용합니다. MCP에서는 `?toolset=`으로 목록을 선택합니다([MCP 툴셋](https://github.com/ClickHouse/loom/blob/main/docs/reference/mcp.md#toolsets)). 목록에서 빠진 op도 `loom_op`를 통해 호출할 수 있습니다.

<a id="2-choose-the-identity"></a>

## 2. 아이덴티티 선택

모든 행은 아이덴티티 튜플 `(org, namespace, agent, user_id, session_id)`와 `run_id`를 가집니다. 토큰은 `org`와 네임스페이스를 고정하고, 나머지는 호출마다 전달됩니다.

| 필드         | 설정 값                                                           | 중요한 이유                                                                                                     |
| ------------ | ----------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------- |
| `namespace`  | 에이전트마다 하나(`agent-<name>`), 또는 에이전트 팀이 공유하는 하나 | **멤버십이 읽기 경계입니다**: 네임스페이스의 리더는 어떤 에이전트가 썼든 그 안의 모든 행을 읽습니다. |
| `agent`      | 쓰기를 수행하는 에이전트의 고정된 이름                            | 격리가 아니라 작성자 표시입니다. MCP에서는 기본값이 클라이언트 이름(`clientInfo.name`, 슬러그화)입니다.            |
| `user_id`    | 에이전트가 여러 사용자를 대상으로 할 때, 에이전트가 응대하는 최종 사용자 | `--read-scope own_user`로 발급한 토큰을 사용하면, 한 사용자의 프롬프트가 다른 사용자의 메모리를 가져올 수 없습니다.             |
| `session_id` | 대화 또는 작업                                                    | 작업 메모리와 `own_session` 읽기의 범위를 정하고, 대화 기록(transcript)을 묶습니다.                                             |
| `run_id`     | 작업의 한 번의 실행                                               | 한 실행의 쓰기, 검색, 체크포인트, 대화 기록을 하나로 연결합니다([실행](https://github.com/ClickHouse/loom/blob/main/docs/features/runs.md)).               |

- **에이전트 하나, 사용자 여러 명, 토큰 하나:** `--read-scope own_user`로 토큰을 발급하거나(또는 `--read-user-id`를 고정하고) 모든 호출에 `user_id`를 전달합니다. `user_id` 없이 범위가 지정된 읽기를 하면 400 `IDENTITY_INCOMPLETE`로 응답하며, 범위가 넓어지지 않습니다.
- **여러 에이전트가 지식을 공유하는 경우:** 모두가 읽는 공유 네임스페이스 하나를 두고 각자 자신의 `agent`로 씁니다. 또는 공유 네임스페이스를 읽고 자신의 네임스페이스에 쓰는 토큰(`ns_write`)을 사용합니다.
  [네임스페이스 접근](https://github.com/ClickHouse/loom/blob/main/docs/guides/tokens-and-grants.md#namespace-access)을 참고하세요.
- **`agent`에 비밀 정보나 개인 식별자를 넣지 마세요:** 이 값은 모든 원장 행에 기록됩니다.

<a id="3-decide-what-to-write"></a>

## 3. 무엇을 기록할지 결정하기

먼저 플레인을 고르세요. 영구 메모리는 임베딩되고, 순위가 매겨지고, 충돌 검사를 거치며, 버전이 관리됩니다. 작업 메모리는 키로 읽으며 순위를 매기지 않습니다. 지식에는 청크로 나뉜 문서가 들어갑니다.

| 에이전트가 가진 것…                                                                                       | 사용할 쓰기 방법                                                                              | 사용하지 말 것                        |
| --------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------- | ------------------------------------- |
| 다음 주에도 유효하고 다른 세션이 필요로 하는 사실 (결정, 제약, 담당자, 선호)                              | `memory.set`: 호출당 사실 하나, 사실당 키 하나                                                | 작업 메모리                           |
| 현재 계획, 아직 확정되지 않은 가정, 이 작업의 체크리스트                                                  | `working_memory.set` / `append`, `context.pack`이 로드                                        | `memory.set`                          |
| 기억해 둘 가치가 있는 대화 기록, 이메일 또는 스레드                                                       | `memory.set_from_messages`: 서버가 짧은 키의 사실을 추출                                      | 거대한 `memory.set` 하나              |
| 문서 (명세, 런북, 보고서 등 약 2 KB를 넘는 모든 것)                                                       | Markdown으로 `knowledge.upsert`, 그리고 같은 키로 `memory.set`에 포인터 문단 하나              | 텍스트 전체를 담은 `memory.set` 하나  |
| 식별자가 있는 사실의 표 (번호별 PR, id별 서비스)                                                          | `memory_schema.register`를 한 번 실행한 뒤 `memory.record.write`                              | 배치당 메모리 행 하나                 |
| 조율 상태 (리스, 동결 플래그, "현재 진행 상황")                                                           | `block_key`로 `working_memory.set`                                                            | 루프 안의 `memory.set` + `memory.get` |
| 이어서 재개할 체크포인트                                                                                  | 작업 메모리 블록, 또는 큰 blob이면 `context.offload`                                          | 단계마다 영구 메모리 하나             |
| 나중에 보여 주고 검색할 대화                                                                              | `conversation.create` / `conversation.append`                                                 | 메모리                                |
| 추론, 인사말, 중간 도구 출력                                                                              | 없음                                                                                          | 어떤 것도 아님                        |

[조사 에이전트 메모리](https://github.com/ClickHouse/loom/blob/main/docs/guides/investigation-agent-memory.md)는 이 표를 사례를 처리하는 에이전트에 적용한 문서이고, [추적과 재개](https://github.com/ClickHouse/loom/blob/main/docs/guides/trace-and-resume.md)는 체크포인트를 다룹니다.

**나중의 세션이 신뢰할 수 있도록 각 사실을 구성하세요.**

- **주장을 먼저 쓰세요.** 값에는 사실을 평문으로 서술하고, 그다음 줄에 출처를 적습니다. 값의 앞부분만 임베딩됩니다 (발췌는 1,200자에서 잘립니다).
- **읽기 쉬운 키**를 `<scope>/<kind>/<id>` 형태로 만드세요. 해시가 아니라 `sre/case/1234/evidence/dns-sidecar` 같은 형태입니다. 패싯은 `tags`(`case:1234`, `kind:evidence`)와 `entities`에 넣습니다.
- **수정은 같은 키를 다시 씁니다.** Loom이 버전을 보관하고(`memory.history`) 현재 버전을 제공합니다. `deploy_window_v2` 같은 키를 새로 만들지 말고, 현재 키를 가리키는 인덱스 행도 두지 마세요.
- **출처를 밝히세요.** 사실을 읽어 온 파일, URL 또는 문서는 `source_resource_id`에 넣습니다.
- **추론을 사실로 둔갑시키지 마세요.** "A와 B로부터 추론: C"라고 쓰고 `confidence`를 1.0 미만으로 설정합니다.
- **날짜를 구체적으로 쓰세요.** "어제"가 아니라 "<YYYY-MM-DD>에"라고 쓰고, `valid_at`은 관찰이 이루어진 시점으로 설정합니다.
- **비밀 정보는 쓰지 마세요.** 사용자가 절대 기억시키고 싶지 않은 종류의 정보는 `blacklist.add`로 패턴을 등록하세요. 일치하는 쓰기는 422 `BLACKLISTED`로 거부됩니다.

사용자의 발언("우리는 화요일에 배포한다")이 기록의 계기입니다. 먼저 검색하지 마세요. 중복과 충돌은 서버가 처리합니다.

<a id="4-read-the-writes-answer"></a>

## 4. 쓰기 응답 읽기

`memory.set` 응답은 영수증입니다. 다음과 같이 읽습니다.

| 필드                                      | 의미                                                                                                                                                |
| ----------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------- |
| `memory_id`, `version`, `content_sha256`  | 쓰기가 저장되었습니다. 이 값이 없으면 저장되지 않은 것이며, 저장되지 않았는데 저장되었다고 사용자에게 말해서는 안 됩니다.                           |
| `conflict_verdict: conflict`, `peers`     | 쓰기가 기존 사실과 충돌합니다(두 행 모두 `conflicted`로 표시됨). 두 값을 모두 보여 주고 어느 쪽이 현재 값인지 물어보세요. 조용히 덮어쓰지 마세요.   |
| 409 `PINNED_CONFLICT`                     | 사용자가 기존 사실을 고정했습니다. 이를 알리고, 새 키로 다시 시도하지 마세요.                                                                       |
| `duplicate_of`, `merged_into_key`, `noop` | 이미 알려진 사실이며, 새로 저장된 것은 없습니다.                                                                                                    |
| `advisories`                              | 형태에 대한 경고입니다. base64, 이중 인코딩된 JSON, `LOOM_MEMORY_VALUE_ADVISORY_CHARS`를 초과하는 값 등이 해당됩니다.                               |

두 작성자가 하나의 키에 동시에 쓸 수 있다면 (`memory.get`에서 얻은) `if_match_sha256`을 전달하세요. 오래된
해시는 덮어쓰지 않고 거부됩니다.

<a id="5-read-with-one-call-per-question"></a>

## 5. 질문당 한 번의 호출로 읽기

1. **작업당 한 번만 컨텍스트를 불러옵니다.** `context.pack`은 토큰 예산에 맞춘 번들을 반환합니다. 번들에는 프로필 섹션, 작업 메모리, `query`와 관련된 메모리, 절차가 들어가며, 선택적으로 지식, 대화, 최근 실행도 포함할 수 있습니다(`include_knowledge`, `include_conversations`, `include_runs`). 플러그인은 세션이 시작될 때 이 함수를 호출합니다.

   ```python
   pack = await agent.context.pack("fix the flaky ingest test", token_budget=2000)
   system_prompt = base_prompt + "\n\n" + pack.rendered
   ```

2. **질문당 한 번만 검색합니다.** MCP에서는 `loom_search`가 메모리, 프로필, 지식, 대화를 한 번의 호출로 모두 다룹니다. SDK에서는 `agent.memory.search(query)`를 사용하거나, 출처가 달린 답변이 필요하면 `agent.memory.ask(question)`을 사용합니다. 답변에 더 많은 행(목록, 개수)이 필요할 때만 `next_page`로 다음 페이지를 가져옵니다.
3. **abstain도 하나의 답으로 취급합니다.** `abstain: true`는 유사도 게이트를 통과한 결과가 없다는 뜻입니다. 가장 가까운 약한 결과를 내놓지 말고 "메모리에 없음"이라고 답하세요. `abstain_code`는 어느 게이트에서 걸렸는지 알려 줍니다([abstain 코드](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/search-and-ask.md)). `pending_extraction`이 0보다 크면 지연된 추출이 아직 실행되지 않은 것이므로, 빈 답변이 부재의 증거는 아닙니다.
4. **출처 정보를 존중합니다.** 결과의 `source_class`는 콘텐츠가 메모리에 들어온 경로를, `verified`는 유효한 증명이 이를 보증하는지를 나타냅니다. `memory.get`은 `verification`을 반환합니다(`human`, `process`, 증명 이후 콘텐츠가 바뀐 경우의 `stale`, 또는 `none`). `memory.ask`는 기본적으로 `external`로 선언되었고 증명되지 않은 행을 종합 결과에서 제외합니다.

```python
hits = await agent.memory.search("refund window for EU customers", top_k=5)
if hits.abstain:
    reply = "I don't have that in memory."
else:
    reply = hits[0].content_excerpt
```

<a id="6-record-runs"></a>

## 6. 실행 기록

실행(run)은 작업을 한 번 수행한 것입니다. 목표, 회상하여 활용한 메모리, 기록한 내용, 그리고 결과를 담습니다. 실행을 기록해 두면 랭킹이 어떤 메모리가 도움이 되는지 학습할 수 있습니다 ([실행](https://github.com/ClickHouse/loom/blob/main/docs/features/runs.md)).

```python
async with agent.run_scope(goal="Fix the flaky CI job") as run:
    answer = await agent.memory.ask("what broke CI last week?")
    run.used("ci/flaky/runner-image")
    run.final_response = "Pinned the runner image."
```

이 스코프는 진입 시 `run.begin`을 호출하고 종료 시 `run.complete`를 호출하며, 상태는 `completed` 또는 `failed`입니다. `memories_used`에는 `memory.ask` 답변이 인용한 키와 `used()`에 전달한 키가 들어갑니다. 블록 안에서 기록하는 내용에는 해당 `run_id`가 붙습니다. SDK를 쓰지 않는 경우에는 `run.begin`과 `run.complete(run_id, status=..., memories_used=[...])`를 직접 호출하세요. `memories_used`는 에이전트가 실제로 활용한 메모리이며, 에이전트에 제공된 모든 메모리가 아닙니다. 회상된 키는 별도로 추적됩니다.

플러그인을 사용하지 않는 에이전트도 완성된 대화 내용을 `memory.set_from_messages`에 전달해야 합니다(또는 `agent.session(...)`을 닫아야 합니다). 그래야 서버가 오래 보존할 내용을 추출할 수 있습니다.

<a id="7-follow-the-memory-skills-review"></a>

## 7. 메모리 스킬의 검토 따르기

플러그인에는 이 가이드를 모델이 따를 수 있게 정리한 `loom-memory` 스킬
(`integrations/claude-code-plugin/skills/loom-memory/`, Codex 플러그인에는 생성되어 포함됨)이 들어 있습니다.
일반 MCP를 사용하는 코딩 에이전트는 [에이전트
지침](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/agent-instructions.md) 블록을 사용하며, 이 블록의 마지막 메모리
줄은 이 검토를 짧게 줄인 형태입니다. 전체 검토를 실행하려면 아래 질문을
해당 블록 아래에 붙여 넣으세요. 다른 에이전트는 이 질문을 자신의 지침에 복사해 사용합니다. 작업 완료를 보고하기 전에, 답이 "예"인 항목만 기록하세요.

1. 이후 세션에 필요한 결정, 제약 조건 또는 담당자를 만들었거나 알게 되었는가?
2. 기존 메모리가 틀렸거나 오래된 것으로 드러났는가? 같은 키로 업데이트하세요.
3. 사용자가 나를 바로잡았는가? 사실을 업데이트하고, 사용자가 그것을 거부했다면 `memory.delete`로 삭제하며, 같은 일이 반복될 수 있다면 `blacklist.add`로 패턴을 추가하세요.
4. 향후 작업에서 알아야 할 산출물(파일, PR, 대시보드)을 만들었는가?
5. 상당한 노력이 들어간 자명하지 않은 사실을 발견했는가?
6. 내가 작성한 추론이 이제 확인되었는가? `confidence`를 높이고 "Inferred" 접두어를 제거하세요.

그런 다음 `namespace.validate`를 실행하고, 작성한 키를 언급하는 결과에 대응하세요. "저장됨"은
영수증에 `memory_id`와 `version`이 있을 때만, "검증됨"은 사람이나
프로세스가 확인했을 때만, 확인을 실행할 수 없었다면 "여기서는 검증되지 않음"이라고 말하세요. 그 외에 제공되는
스킬로는 코드 질문용(`loom-code`, 모든 클라이언트용으로 `skills/loom-code/`에도 있음),
네임스페이스 재구성용(`loom-optimize`), 세션 복원용(`loom-session`), 클라이언트 상태 확인용
(`loom-status`)이 있습니다.

<a id="mistakes-to-avoid"></a>

## 피해야 할 실수

`namespace.audit`는 아래 항목을 각각 탐지하며, [네임스페이스 최적화](https://github.com/ClickHouse/loom/blob/main/docs/guides/optimize-namespace.md)는
그 결과를 수정으로 이어 줍니다.

| 탐지 항목                             | 실수                                                                                    | 대안                                                                                            |
| ------------------------------------- | --------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------- |
| `double_encoded_values`               | `json.dumps(json.dumps(doc))`: 임베딩된 텍스트가 이스케이프된 따옴표로 시작합니다.      | 텍스트 또는 객체 자체를 `value`로 보냅니다.                                                     |
| `json_prefix_excerpts`                | 임베딩 클립보다 긴 JSON 객체: 벡터가 앞부분만으로 만들어집니다.                         | 먼저 일반 텍스트로 클레임하고, 문서는 `knowledge.upsert`로 보냅니다.                            |
| `hash_keys`                           | 행의 식별자로 sha 사용: 읽을 수 없고 그룹화할 수 없으며, 수정본이 새 키가 됩니다.       | `<scope>/<kind>/<id>`를 쓰고, 수정본은 같은 키로 갱신합니다.                                    |
| `exact_key_store`                     | 정확한 키로 읽는 횟수가 검색을 압도합니다: 이 네임스페이스는 객체 저장소입니다.         | `memory.search`로 회상하고, 리스와 포인터는 작업 메모리에 둡니다.                               |
| `coordination_as_memory`              | 리스, 락, 동결 플래그를 메모리로 저장하고 반복해서 읽습니다.                            | `working_memory.set` / `get`: 임베딩, 순위 지정, 충돌 검사가 적용되지 않습니다.                 |
| `hand_rolled_index_rows`              | 에이전트가 자기 행을 찾지 못해 다른 키를 나열하는 행을 만듭니다.                        | `tags` + `memory.list prefix=`를 쓰고, 수정본은 `memory.link supersedes`로 연결합니다.          |
| `oversized_values`, `unembedded_rows` | 문서를 메모리로 저장: 임베더의 한도를 넘으면 값이 임베딩되지 않습니다.                  | `knowledge.upsert`와 한 문단짜리 포인터 행을 함께 사용합니다.                                   |
| `episodic_blobs`, `version_churn`     | 실행 덤프를 에피소드 행으로 저장하고 매 단계마다 다시 씁니다.                           | 블롭은 `context.offload`로 보내고, 실행당 짧은 에피소드 행 하나만 두며, 블롭이 아닌 클레임을 수정합니다. |

감사가 잡아내지 못하는 실수가 두 가지 더 있습니다.

- **턴당 블롭 하나.** 사실 다섯 개는 `memory.set` 호출 다섯 번이며, 각각 자기 키를 사용합니다.
- **실행 중인 에이전트가 아직 읽는 키를 폐기.** 해당 읽기는 `found: false`로 응답하고, 클라이언트가
  로컬 상태로 대체할 수 있으며, 두 저장소의 내용이 어긋납니다. `memory.move`로 옮기고(리디렉션이 남습니다)
  모든 읽기 주체가 옮겨 갈 때까지 아무것도 삭제하지 마세요.

<a id="related"></a>

## 관련 문서

- [헤드리스 봇](https://github.com/ClickHouse/loom/blob/main/docs/guides/headless-bots.md)
- [메모리 쓰기](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/writing.md)
- [통합 검색](https://github.com/ClickHouse/loom/blob/main/docs/features/unified-search.md)
- [프레임워크](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/frameworks.md)
- [MCP 클라이언트](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/mcp-clients.md)
