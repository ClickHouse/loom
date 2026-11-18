<!-- i18n: source=docs/features/integrations/agent-instructions.md blob=66910fd5b55982ca9d0f8f554d606e94b2c0f60c model=claude-sonnet-5-5 -->
> 이 페이지는 [영어 원문](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/agent-instructions.md)을 기계 번역한 것이며, 영어 원문이 기준입니다.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/agent-instructions.md), which is authoritative.

<a id="agent-instructions"></a>

# 에이전트 지침

에이전트의 지침 파일에 짧은 블록 하나를 넣으면, 에이전트가 Loom의 컨텍스트를 불러올 시점, 사실을 저장할 시점, 그리고 grep이나 파일 읽기 대신 코드 인덱스를 사용할 시점을 알 수 있습니다. 이 페이지는 해당 블록을 한 번만 제시하고, 각 클라이언트가 이 블록을 읽는 파일을 안내합니다. 블록을 붙여 넣고, 두 개의 네임스페이스를 `code.repos`(아래 참고)에서 가져와 채운 다음, 저장소와 함께 커밋하세요.

Claude Code 및 Codex 플러그인은 이미 훅과 스킬을 통해 이 작업을 수행합니다([Claude Code](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/claude-code.md#skills), [Codex](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/codex.md#skills)). 이 블록은 그 외의 모든 클라이언트와, 플러그인 없이 연결한 Claude Code 또는 Codex를 위한 것입니다. 블록은 `coding` 툴셋이 나열하는 도구를 지정하므로 클라이언트를 `?toolset=coding`으로 연결하세요. 지원되는 클라이언트는 `loom install --client <name> --toolset coding`을 사용하고, 그 외에는 URL에 `?toolset=coding`을 붙입니다([MCP 클라이언트](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/mcp-clients.md)).

<a id="the-block"></a>

## 블록

[Cline 훅](https://github.com/ClickHouse/loom/blob/main/integrations/cline/README.md)을 사용하는 경우, 사본에서 블록의 "Every task:"로 시작하는 문단을 "Every task: make these two calls."로 바꾸십시오. 그러면 두 호출이 모두 필수로 유지됩니다. Cline은 훅의 텍스트를 해당 훅이 실행된 도구 뒤에만 모델에 보여 주므로, 프라이머는 작업의 첫 번째 도구 호출 뒤에 도착하고 편집에 대한 파일 블록은 편집 뒤에 도착합니다.

```markdown
## Loom: memory and code index

Loom is connected as an MCP server; its tools are named `loom_*`. A tool you do not see
listed runs through `loom_op` with the op's dotted name: on a connection without the
coding toolset, `loom_code_symbol` is `code.symbol` and `loom_working_memory_set` is
`working_memory.set`. `loom_op` with `{"op": "help", "query": "<words>"}` finds them.

Every task: make these two calls unless a Loom hook already put Loom's context in this
session (the Claude Code and Codex plugins do). Without one they are not optional.

- At the start of each task, before planning: `loom_context_pack` with the task as
  `query`. It returns the decisions, constraints and memories that bear on it.
- Before editing a file: `loom_op` with `op` set to `memory.for_file` and `arguments`
  holding `path` (the file's repo-relative path) and `extra_namespaces`
  (`["<team-namespace>"]`). Follow every constraint and decision it returns.

Memory

- To recall something mid-task, call `loom_search` with the topic, file or error. Treat
  `abstain` as "nothing relevant". A hit whose `code_state` is `changed`, `unresolved`
  or `missing` is about code that changed or went away since it was written: check the
  code before relying on it.
- When the user states a decision, preference, constraint or correction, save it with
  `loom_memory_set` right away: one plain-text fact per call under a readable key
  (`<scope>/<kind>/<id>`), dates as calendar dates. Never save a guess, your reasoning or
  the conversation.
- A rule the user states for the whole team about a file: `loom_memory_set` with
  `namespace` `<team-namespace>`, the file in `files` and the tag `constraint`.
- A fact that changed: `loom_memory_update` on the same key, in the namespace the row
  lives in, never a second copy. A team rule or decision someone else wrote is not yours
  to update: follow the refusal's `hint`.
- Your plan and checklist for this task: `loom_working_memory_set`, not `loom_memory_set`.
- Before reporting done, one pass over what should outlive the session.

Code

- This repository's code namespace is `<code-namespace>`; pass it as `namespace` on
  every `loom_code_*` call.
- Where X is defined: `loom_code_symbol`. Who calls X: `loom_code_callers`. A concept
  with no exact keyword: `loom_code_search`. An exact string: `loom_code_grep`. What is
  in a file: `loom_code_outline`, then read one span. What a change breaks:
  `loom_code_impact`.
- Grep your own uncommitted files and repositories the index does not cover. When a code
  tool answers `abstain: true`, grep instead of retrying with synonyms.
- A code answer's `lessons` are memories about its top files: follow them, and check the
  code of one whose `code_state` is `changed`, `unresolved` or `missing` before relying on
  it.
```

`code.repos`는 토큰이 읽거나 참여를 요청할 수 있는 저장소를 인덱싱된 브랜치당 한 행씩 나열합니다.

```bash
curl -s -X POST https://loom.example.com/v1/code.repos \
  -H "Authorization: Bearer $LOOM_API_KEY" -H 'Content-Type: application/json' \
  -d '{"repo": "<owner>/<name>", "fields": ["branch", "primary_namespace", "readable"]}'
```

`<code-namespace>`는 이 파일의 사본이 커밋된 브랜치에 해당하는 행의 `namespace`로, `<team-namespace>`는 그 행의 `primary_namespace`(팀의 파일 규칙과 결정이 있는 트렁크 브랜치의 네임스페이스)로 바꾸십시오. 트렁크 브랜치에서는 두 값이 같습니다. 행이 없는 저장소(인덱싱되지 않았거나 `code.repos`가 코드 인텔리전스가 꺼져 있다고 응답하는 경우), 또는 행에 `readable: false`가 표시된 저장소(이 토큰이 읽을 수 없어 접근을 요청하거나 가져올 수 있도록만 나열된 저장소)에는 `extra_namespaces`, 팀 규칙 줄, Code 섹션을 뺀 블록을 사용하십시오.

<a id="where-each-client-reads-it"></a>

## 각 클라이언트가 읽는 위치

블록은 어디서나 동일합니다(위에서 설명한 Cline 훅의 한 단락 변경은 제외). 파일과 헤더만 다릅니다. 이 클라이언트 대부분은 `AGENTS.md`도 읽으므로, 한 파일이 여러 클라이언트를 담당할 수 있다면 사본을 하나만 두세요. 블록이 들어 있는 파일 두 개를 읽는 클라이언트는 블록을 두 번 보게 되며, 컨텍스트를 더 쓰는 것 외에 달라지는 것은 없습니다.

| 클라이언트                                                     | 파일                                                                   | 블록 앞의 헤더                                                               |
| -------------------------------------------------------------- | ---------------------------------------------------------------------- | ---------------------------------------------------------------------------- |
| 플러그인을 쓰지 않는 Codex, 그리고 `AGENTS.md`를 읽는 모든 에이전트 | 저장소 루트의 `AGENTS.md`                                              | 없음                                                                         |
| Zed                                                            | 처음 발견되는 지침 파일(아래 참고)                                     | 없음                                                                         |
| 플러그인을 쓰지 않는 Claude Code                               | 저장소 루트의 `CLAUDE.md`(아래 참고)                                   | 없음                                                                         |
| Cursor                                                         | `.cursor/rules/loom.mdc`                                               | `---`, `description: Loom memory and code index`, `alwaysApply: true`, `---` |
| Devin 데스크톱 (Windsurf)                                      | `.devin/rules/loom.md` (Windsurf 빌드에서는 `.windsurf/rules/loom.md`) | `---`, `trigger: always_on`, `---`                                           |
| Cline                                                          | `.clinerules/loom.md`                                                  | 없음                                                                         |
| VS Code (GitHub Copilot)                                       | `.github/copilot-instructions.md`                                      | 없음                                                                         |

Zed는 처음 발견한 지침 파일(`AGENTS.md`보다 앞서는 `.rules`, `.cursorrules` 등)만 읽습니다. 따라서 블록은 그 파일에 넣고, 저장소에 해당 파일이 없으면 `AGENTS.md`에 넣습니다.

Claude Code는 `AGENTS.md`를 읽지 않습니다. 저장소에 이미 하나 있다면 블록을 `AGENTS.md`에 넣고, `CLAUDE.md`에서 `@AGENTS.md` 줄로 이를 가져오게 하세요.

각 헤더 줄은 별도의 줄에 작성합니다. 예를 들어 Cursor 파일은 다음과 같이 시작합니다.

```markdown
---
description: Loom memory and code index
alwaysApply: true
---

## Loom: memory and code index
```

기존 `AGENTS.md`, `CLAUDE.md` 또는 Copilot 파일은 내용을 그대로 유지합니다. 블록은 그 안의 한 섹션으로 추가하세요.

<a id="why-these-rules"></a>

## 이 규칙이 필요한 이유

훅이 없는 세션에서는 메모리를 불러오도록 유도하는 것이 아무것도 없고, 목록에 나열된 메모리 도구는 여러 선택지 중 하나일 뿐입니다. 그래서 이 블록은 플러그인의 훅과 마찬가지로 컨텍스트 로딩을 모든 작업의 일부로 만듭니다.

각 줄은 플러그인이 알려 주는 내용 또는 Loom의 응답에 담긴 내용을 줄여 쓴 것입니다.

- 작업을 시작할 때와 편집하기 전에 컨텍스트를 불러옵니다. 플러그인의 세션 시작 훅과 파일 컨텍스트 훅이 이 일을 하며, 각각 `context.pack`과 `memory.for_file`을 호출합니다.
- 언급된 사실은 나오는 대로 회상하고 저장합니다.
  [`loom-memory`](https://github.com/ClickHouse/loom/blob/main/integrations/claude-code-plugin/skills/loom-memory/SKILL.md)
  스킬이 이를 다룹니다.
- grep보다 코드 인덱스를 먼저 쓰고, 인덱스가 답하지 못하는 곳에서는 grep을 씁니다.
  [`loom-code`](https://github.com/ClickHouse/loom/blob/main/integrations/claude-code-plugin/skills/loom-code/SKILL.md)
  스킬이 이를 다루며, 플러그인의 코드 shunt가 이를 강제합니다.
- 코드 응답에 담긴 메모리를 읽습니다. 이는 응답의 `lessons`
  ([코드에 연결된 메모리](https://github.com/ClickHouse/loom/blob/main/docs/concepts/memory.md#memories-linked-to-code))이며, 플러그인 세션에서는 같은 응답에서 얻을 수 있습니다.

블록의 마지막 메모리 줄은 작업 종료 시 리뷰를 대신합니다. 이를 풀어서 쓰려면 블록 아래에 있는 [에이전트 통합 가이드](https://github.com/ClickHouse/loom/blob/main/docs/guides/agent-integration.md#7-follow-the-memory-skills-review)의 질문을 복사하세요. 스킬에는 블록이 생략한 세부 내용이 담겨 있습니다. 영수증 필드, 문서와 메모리의 차이, 중단 규칙이 그것입니다. 이 세부 내용이 필요하면 블록과 함께 `loom` 스킬과 `loom-code` 스킬을 설치할 수 있습니다([코딩 에이전트](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/index.md#coding-agents)). `loom`은 메모리 루프, 쓰기 형태, 영수증, 작업 종료 시 리뷰를 다룹니다. 이 스킬에는 훅이 없으므로 블록의 두 호출을 대체하지는 않습니다.

<a id="related"></a>

## 관련 문서

- [MCP 클라이언트](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/mcp-clients.md): 각 클라이언트 연결 방법.
- [도구 세트 선택](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/mcp-clients.md#choosing-a-toolset).
- [통합](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/index.md).
