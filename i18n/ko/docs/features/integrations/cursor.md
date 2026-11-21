<!-- i18n: source=docs/features/integrations/cursor.md blob=ba85fce4ce13c17118f863ce5f4a67693523e037 model=claude-sonnet-5-5 -->
> 이 페이지는 [영어 원문](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/cursor.md)을 기계 번역한 것이며, 영어 원문이 기준입니다.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/cursor.md), which is authoritative.

<a id="cursor"></a>

# Cursor

Cursor는 세 가지 구성 요소로 Loom을 사용합니다.

| 구성 요소          | 제공하는 기능                                      | 방법                                                                                                                                                    |
| ------------------ | -------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------- |
| MCP 도구           | Cursor 에이전트의 `loom_*` 도구                    | [플러그인](#plugin), 로컬 단일 사용자 서버용 [설치 링크](#mcp-tools), 또는 `loom install --client cursor` ([MCP 클라이언트](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/mcp-clients.md)) |
| 스킬               | `loom` 및 `loom-code` 스킬                         | [플러그인](#plugin), 또는 `npx skills add ClickHouse/loom --skill loom loom-code`                                                                       |
| 대화 내용 캡처     | `conversation.append`에 기록되는 각 대화의 턴      | `sessionEnd` 훅 `integrations/cursor/cursor-session-hook.sh`                                                                                            |

Cursor 플러그인에는 MCP 항목과 `loom` 및 `loom-code` 스킬이 포함되어 있으며 훅은 없습니다. 따라서 코드 shunt도 없고, 도구 호출, 실행, 작업 메모리의 캡처도 없습니다. 이러한 기능은 [Claude Code](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/claude-code.md) 및 [Codex](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/codex.md) 플러그인에서 제공합니다.

<a id="plugin"></a>

## 플러그인

저장소 루트의 `.cursor-plugin/`은 저장소를 플러그인이 하나뿐인 Cursor 플러그인 마켓플레이스로 만듭니다. 이 플러그인은 `clickhouse-loom`이며, `loom` MCP 서버와 루트의 `skills/`로 구성됩니다. 이 플러그인은 Cursor의 마켓플레이스에 등록되어 있지 않습니다. 로컬 단일 사용자 연결과 읽기 전용 메모리 검색은 Cursor에서 테스트했습니다. Teams 또는 Enterprise 요금제에서는 팀 관리자가 **Dashboard → Plugins & MCPs → Add Marketplace → Import from Repo**에서 `https://github.com/ClickHouse/loom`을 사용해 추가합니다.

플러그인에는 **Loom MCP URL**이라는 변수가 하나 있습니다. 팀 관리자가 Cursor 대시보드의 **Plugins & MCPs**에서 이 값을 설정하며, 모든 구성원에게 적용됩니다.

| 서버                                       | Loom MCP URL                                             | 로그인              |
| ------------------------------------------ | -------------------------------------------------------- | ------------------- |
| 단일 사용자, 각 구성원의 머신에서 실행     | `http://localhost:7777/mcp?toolset=coding` (기본값)      | 없음                |
| `LOOM_OAUTH_ENABLED=true`인 팀 서버        | `https://<your server>/mcp`                              | 브라우저, 사용자별  |

OAuth가 켜진 서버에는 보호된 리소스 메타데이터가 지정하는 리소스인 기본 URL을 사용합니다. 기본 툴셋에 없는 도구는 `loom_op`를 통해 실행합니다. 이 항목은 `Authorization` 헤더를 보내지 않으므로, OAuth가 없는 팀 서버는 401로 응답합니다. 이런 팀은 플러그인을 사용하지 않고, [아래](#mcp-tools)의 `loom install --client cursor`로 토큰이 포함된 항목을 작성하고 `npx skills add`로 스킬을 추가합니다.
[브라우저로 로그인하는 클라이언트](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/mcp-clients.md#clients-that-sign-in-by-browser)에 OAuth 관련 세부 사항이 있습니다.

<a id="mcp-tools"></a>

## MCP 도구

같은 머신에서 실행하는 단일 사용자 서버는 토큰 없이 MCP 호출을 받습니다.
[Cursor에 Loom 추가](https://cursor.com/en/install-mcp?name=loom&config=eyJ1cmwiOiJodHRwOi8vbG9jYWxob3N0Ojc3NzcvbWNwP3Rvb2xzZXQ9Y29kaW5nIn0%3D)를 사용하면 대시보드가 Cursor에 제공하는 `coding` 툴셋을 사용하는 다음 항목이 `mcpServers.loom` 아래에 추가됩니다. 링크의 `config`는 이 항목을 base64로 인코딩한 값입니다.

```json
{ "url": "http://localhost:7777/mcp?toolset=coding" }
```

이 항목은 기본 포트 7777을 가리킵니다. 다른 포트를 사용한다면 `~/.cursor/mcp.json`의 `url`을 수정하세요. 그 외의 서버에는 토큰을 포함해 항목을 작성합니다.

```bash
loom install --client cursor --server-url https://loom.example.com --token "$LOOM_API_KEY"
```

이 명령은 `mcpServers.loom` 항목(`url`과 `Authorization` 헤더)을 `~/.cursor/mcp.json`에 병합합니다. 이후 Cursor를 다시 시작하세요. 툴셋, 에이전트 레이블, 클라이언트별 파일 표는 [MCP 클라이언트](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/mcp-clients.md#wire-a-client-with-loom-install)에 있습니다.

그런 다음 [에이전트 지침](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/agent-instructions.md)의 `.cursor/rules/loom.mdc`를 추가하세요. 그러면 에이전트가 언제 recall하고, 저장하고, 코드 인덱스를 사용할지 알게 됩니다. 저장소의 `AGENTS.md`에 이미 해당 블록이 있다면 추가하지 않아도 됩니다.

<a id="transcript-capture"></a>

## 기록 캡처

직접 등록하는 훅입니다. `cursor-session-hook.sh`는 끝난 대화의 기록을 `loom ingest-transcript`에 넘기고, 이 명령은 각 턴을 `conversation.append`로 전송합니다. 턴은 네임스페이스에서 대화 로깅이 켜져 있을 때만 저장됩니다.

<a id="install"></a>

### 설치

1. Loom 체크아웃에서(`pip install -e .`) 또는 git에서(`pip install "git+https://github.com/ClickHouse/loom"`) `loom` CLI를 설치하고, `PATH`에 `python3`가 있어야 합니다.
2. 훅을 `~/.cursor/hooks/cursor-session-hook.sh`에 복사하고 실행 가능하게 만듭니다.
3. `~/.cursor/hooks.json`에 등록합니다. 사용자 파일의 경로는 `~/.cursor/` 기준 상대 경로이며, 프로젝트의 `.cursor/hooks.json`은 프로젝트 루트에서 경로를 해석합니다.

   ```json
   {
     "version": 1,
     "hooks": {
       "sessionEnd": [{ "command": "./hooks/cursor-session-hook.sh", "timeout": 150 }]
     }
   }
   ```

4. 훅에 환경을 제공합니다. 독에서 실행한 앱은 셸 프로필을 읽지 못하므로, 토큰은 `~/.loom/token`에 두고(`loom mint-token --save`가 기록합니다) 나머지는 `hooks.json`이 가리키는 래퍼에서 설정합니다.

   ```bash
   #!/usr/bin/env bash
   export LOOM_BASE_URL=https://loom.example.com LOOM_NAMESPACE=my-namespace
   exec "$HOME/.cursor/hooks/cursor-session-hook.sh" "$@"
   ```

5. 네임스페이스의 대화 로깅을 켭니다(대시보드 Conversations 페이지의 **Record conversations** 스위치 또는 `conversation.set_logging`). 그 전까지 서버는 턴을 받아들이지만 아무것도 저장하지 않습니다.

| 변수                         | 기본값                  | 의미                                                                                            |
| ---------------------------- | ----------------------- | ----------------------------------------------------------------------------------------------- |
| `LOOM_NAMESPACE`             | 필수                    | 스레드를 기록할 네임스페이스                                                                    |
| `LOOM_API_KEY`               | `~/.loom/token`         | 토큰. CLI가 환경 변수에서 읽으므로 명령줄에 나타나지 않습니다                                   |
| `LOOM_BASE_URL`              | `http://localhost:7777` | Loom 서버                                                                                       |
| `LOOM_AGENT`                 | `cursor`                | 대화 행의 `agent` 값                                                                            |
| `LOOM_BIN`                   | `loom`                  | 실행할 CLI                                                                                      |
| `LOOM_CAPTURE_CONVERSATIONS` | `true`                  | `false`이면 훅이 아무 동작도 하지 않습니다                                                      |
| `LOOM_INGEST_MAX_BYTES`      | `16777216`              | 이보다 큰 기록은 잘라내지 않고 건너뜁니다                                                       |
| `LOOM_INGEST_TIMEOUT`        | `120`                   | CLI가 실행될 수 있는 시간(초)(`timeout` 또는 `gtimeout` 필요). `hooks.json`의 timeout보다 작게 유지하세요 |

<a id="what-the-hook-reads"></a>

### 훅이 읽는 내용

Cursor는 훅을 프로세스로 시작하고 stdin에 JSON 객체 하나를 씁니다. 훅은 두 필드만 읽고 나머지는 무시합니다.

| 필드              | 용도                                                                             |
| ----------------- | -------------------------------------------------------------------------------- |
| `transcript_path` | 기록 파일. 기록이 꺼져 있으면 `null`이며, 이때는 아무것도 전송하지 않습니다      |
| `conversation_id` | 스레드 id `cursor-<conversation_id>` (`[A-Za-z0-9._:-]`만 허용, 최대 128자)      |

환경 변수 `CURSOR_TRANSCRIPT_PATH`는 경로의 대체 값입니다. 사용할 수 있는 `conversation_id`가 없으면 스레드 id는 `cursor-`에 기록 경로의 해시를 붙인 값이 됩니다. `sessionEnd`를 등록하세요. `stop`도 동작하지만, 에이전트 턴이 끝날 때마다 전체 기록을 다시 전송합니다.

훅은 Cursor가 `~/.cursor/projects/<project>/agent-transcripts/` 아래에 쓰는 JSONL을 읽습니다. 한 줄에 객체 하나이며 `role`과 `message.content`(문자열 또는 블록 목록)를 가집니다. `{role, content}`의 `.json` 배열이나 `# user` / `# assistant` 섹션이 있는 `.md` 파일은 그대로 전달됩니다. 그 밖의 파일은 stderr에 메시지를 남기고 건너뜁니다.

<a id="capture-boundary"></a>

### 캡처 범위

- **전송됨:** `user`와 `assistant` 줄의 텍스트를 파일 순서대로 보냅니다. 텍스트 블록만 해당하며, `tool_use` 블록, 도구 결과, thinking, 그 밖의 모든 role은 해당 머신에서 버려집니다.
- **전송되지 않음:** 훅 입력의 `user_email`, `workspace_roots`, 모델 필드. 작성자는 토큰이 가리키는 주체이며, 행에는 `agent`만 담기고 Cursor 계정의 정보는 담기지 않습니다.
- **마스킹되지 않음:** 턴 텍스트는 작성된 그대로 전송됩니다. 캡처하는 채팅에는 비밀 정보를 넣지 말거나, 네임스페이스의 로깅을 꺼 두세요.

<a id="behaviour"></a>

### 동작

- **Cursor를 막지 않습니다.** 훅 모드에서는 모든 결과가 종료 코드 0으로 끝나고 stdout에 `{}`를 출력하며, CLI의 출력은 stderr로 갑니다.
- **Loom에 연결할 수 없어도 안전합니다.** 훅은 한 줄을 기록하고 0으로 종료하며 재시도하지 않습니다. 다음 실행에서 기록을 다시 보내고, `conversation.append`는 이미 반영된 턴을 건너뜁니다([턴 id](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/index.md#transcript-capture-hooks)).
- **제한이 있습니다.** 훅 입력은 최대 1 MiB, 기록은 최대 `LOOM_INGEST_MAX_BYTES`까지 읽고, CLI는 `LOOM_INGEST_TIMEOUT`초 동안 실행됩니다.
- **조용합니다.** 진단 메시지는 `loom-cursor-hook:` 접두사가 붙은 stderr 한 줄이며, 기록 텍스트나 토큰은 포함하지 않습니다.
- **수동 모드.** `cursor-session-hook.sh <transcript>`는 파일 하나를 수집하고 CLI의 코드로 종료합니다(`2`는 설정 오류, `3`은 기록을 건너뜀, 그 외에는 CLI의 코드).

<a id="limits"></a>

## 제한 사항

- 저장된 턴은 기록일 뿐 추출 소스가 아니며, 여기서 아무것도 추출하지 않습니다. 대화에서 메모리를 작성하려면 해당 턴을 `memory.set_from_messages`로 전송하세요.
- 트랜스크립트 형식은 Cursor의 형식이며 Cursor에서 문서화하지 않았습니다. 형식이 바뀌면 stderr에 "no user or assistant text in the transcript"라고 표시됩니다.

<a id="related"></a>

## 관련 문서

- [통합](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/index.md#transcript-capture-hooks): 다른 대화 기록 템플릿입니다.
- [MCP 클라이언트](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/mcp-clients.md).
- [대화와 세션](https://github.com/ClickHouse/loom/blob/main/docs/features/conversations-and-sessions.md).
- [소스 README](https://github.com/ClickHouse/loom/blob/main/integrations/cursor/README.md).
