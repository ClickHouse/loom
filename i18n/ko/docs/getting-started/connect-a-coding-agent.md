<!-- i18n: source=docs/getting-started/connect-a-coding-agent.md blob=4a72e3770686ed00295619940601d7f23baa7988 model=claude-sonnet-5-5 -->
> 이 페이지는 [영어 원문](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md)을 기계 번역한 것이며, 영어 원문이 기준입니다.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md), which is authoritative.

<a id="shared-memory-for-claude-code-codex-and-cursor"></a>

# Claude Code, Codex, Cursor를 위한 공유 메모리

Loom은 같은 셀프 호스팅 서버에 연결된 플러그인 또는 MCP 도구를 통해 Claude Code, Codex, Cursor에 공유 장기 메모리를 제공합니다. 팀원마다 팀 네임스페이스에 접근할 수 있는 토큰을 발급하면, 각자의 에이전트가 다른 팀원이 저장한 사실을 찾을 수 있습니다. 아래의 플러그인 또는 MCP 항목을 설치하면 다음 세션에서 에이전트가 저장한 내용을 다시 불러올 수 있습니다. Claude Code 및 Codex 플러그인이 Loom으로 보내는 데이터는 [데이터 처리](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/claude-code.md#data-handling)에 설명되어 있습니다. 대화 기록은 기본적으로 꺼져 있습니다. 네임스페이스 소유자는 대시보드의 Conversations 페이지에 있는 **Record conversations** 스위치로, 또는 `conversation.set_logging`으로 이를 켤 수 있습니다. 서버를 직접 실행하려면 [빠른 시작](quickstart.md)부터 시작하세요. 에이전트가 고정된 릴리스를 설치하고 정상 동작을 확인하도록 하려면 최신 릴리스의 `https://github.com/ClickHouse/loom/releases/latest/download/agent-setup.txt`를 에이전트에 전달하세요([코딩 에이전트로 Loom 설정하기](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/agent-setup.md)).

<a id="what-you-need"></a>

## 필요한 것

![설정 → 토큰: 새 토큰과 함께 출력되는 Claude Code 설정](https://github.com/ClickHouse/loom/raw/main/docs/images/dashboard-connect.png)

- **서버 URL**: 예를 들어 `https://loom.example.com`, 또는 `loom up`으로 시작한 스택의 경우 `http://localhost:7777`입니다.
- **네임스페이스용 writer 토큰**: 같은 머신의 단일 사용자 서버에 연결하는 경우는 예외입니다. 이 경우 플러그인과
  [Cursor](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/cursor.md#mcp-tools) 및
  [VS Code](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/vscode.md#local-single-user-server)용 설치 링크가 토큰 없이 연결됩니다.
  토큰은 대시보드의
  `<server>/settings` → **토큰**에서 만들거나, 운영자에게 다음 명령을 실행해 달라고 요청하세요.
  `loom mint-token --subject you --org <org> --namespaces <namespace> --roles writer`
  자체 가입이 열려 있는 서버에서는 Claude Code 플러그인이 첫 세션에서 머신을 직접 가입시킬 수 있습니다. [자격 증명이 없을 때의 동작](#no-token-yet)을 참고하세요.

<a id="claude-code"></a>

<a id="how-do-i-give-claude-code-shared-memory"></a>

## Claude Code에 공유 메모리를 제공하려면 어떻게 하나요?

공유하려는 네임스페이스의 서버 URL과 writer 토큰을 지정해 Loom 플러그인을 설치하세요. 플러그인은 세션이 시작될 때 관련 메모리를 불러오고, 에이전트에게 사실을 저장하고 검색하는 도구를 제공합니다.

```bash
claude plugin marketplace add ClickHouse/loom
claude plugin install loom@loom \
  --config server_url=https://loom.example.com \
  --config token=<writer JWT>
```

두 `--config` 값은 모두 선택 사항이며, 지정하지 않으면 Claude Code가 한 번 물어봅니다. 새 세션을 시작하세요. Loom의 세션 프라이머가 맨 위에 표시되고, 그 이후에는 다음과 같이 동작합니다.

- 훅은 [데이터 처리](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/claude-code.md#data-handling)에 나열된 항목을 Loom으로 전송합니다.
- `loom-memory`, `loom-code`, `loom-session` 등의 스킬은 Claude에게 사실을 저장할 시점, 검색할 시점, 코드 인덱스를 사용하는 방법을 알려 줍니다.
- `loom_search`, `loom_memory_set` 및 코드 도구는 MCP 도구로 사용할 수 있습니다.

플러그인이 설치하는 모든 항목과 설정 방법은 [Claude Code](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/claude-code.md) 페이지에서 확인할 수 있습니다.

<a id="codex"></a>

<a id="how-do-i-give-codex-shared-memory"></a>

## Codex에 공유 메모리를 제공하려면 어떻게 하나요?

Loom 플러그인을 설치하고, 플러그인의 훅과 MCP 항목을 같은 서버에 연결한 다음, 공유 네임스페이스에 접근할 수 있는 토큰을 사용하세요. 플러그인은 세션이 시작될 때 메모리 프라이머를 로드합니다. 플러그인의 훅은 아래에 설명된 승인이 필요합니다.

```bash
codex plugin marketplace add ClickHouse/loom
codex plugin add loom@loom
```

그런 다음 `codex`를 대화형으로 한 번 실행하고 [플러그인의 훅을 승인](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/codex.md#approve-the-hooks)하세요.
승인하기 전에는 도구는 동작하지만 아무것도 캡처되지 않습니다.

플러그인의 MCP 항목은 `http://localhost:7777`을 가리킵니다. 다른 서버를 사용하려면 해당 서버의 URL과 토큰을 `~/.loom/config.json`에 작성하고, [Codex: 서버](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/codex.md#the-server)에 나온 대로 `~/.codex/config.toml`의 MCP 항목을 재정의하세요.

Codex 플러그인은 동일한 훅과 스킬을 제공하며, 차이점은 [Codex](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/codex.md) 페이지에 있습니다. 플러그인이 설치된 상태에서 `codex mcp add loom`을 함께 실행하지 마세요. 같은 이름의 항목이 플러그인의 항목을 대체합니다.

<a id="cursor-and-other-mcp-clients"></a>

<a id="how-do-i-give-cursor-or-another-mcp-client-shared-memory"></a>

## Cursor나 다른 MCP 클라이언트에 공유 메모리를 어떻게 제공하나요?

공유 네임스페이스용 토큰으로 클라이언트를 Loom의 MCP 엔드포인트에 연결한 다음, 아래에 링크된 에이전트 지침을 추가합니다. 도구는 사실을 저장하고 불러옵니다. 세션 대화 기록을 캡처하려면 클라이언트의 hook 통합도 필요합니다.

Cursor Teams 또는 Enterprise 플랜에서는 팀 관리자가 [Cursor 플러그인](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/cursor.md#plugin)(Cursor에서 테스트되지 않음)을 가져올 수 있으며, 이 플러그인에는 MCP 항목과 Loom의 skill이 들어 있습니다.

같은 머신의 단일 사용자 서버에 Cursor나 VS Code를 연결하는 경우에는 토큰이 필요 없습니다. [Cursor](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/cursor.md#mcp-tools) 또는
[VS Code](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/vscode.md#local-single-user-server) 설치 링크를 사용하세요. 그렇지 않은 경우 대시보드에서
**Settings → Tokens**를 열고 **New token** 양식을 작성한 뒤(토큰이 이미 있으면 먼저 **New token**을 누릅니다) **Create token**을 누릅니다. 그런 다음 **Connect**에서 클라이언트(Cursor, VS Code, Cline, Windsurf, Zed 또는 Claude Desktop)를 선택하면, 대화 상자에 해당 설정 파일에 넣을 항목이 토큰과 함께 표시됩니다. 그 밖의 MCP 클라이언트는 [MCP 클라이언트](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/mcp-clients.md)에서 항목을 복사하세요. `loom` CLI가 설치되어 있다면(저장소 클론에서 `pip install -e .`), 명령 하나로 항목을 대신 작성할 수 있습니다.

```bash
loom install --client cursor --server-url https://loom.example.com --token "$LOOM_API_KEY"
```

이후 클라이언트를 다시 시작하세요. `--client`는 `claude-desktop`, `vscode`, `cline`, `windsurf`, `zed`도 받습니다. 지정하지 않으면 `loom install`은 찾은 모든 클라이언트를 설정합니다. 이 클라이언트들은 도구를 사용할 수 있습니다. 대화 기록 캡처에는 hook이 필요한데, [Cursor](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/cursor.md)에는 hook이 있고 다른 클라이언트는 [대화 기록 템플릿](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/index.md#transcript-capture-hooks)을 사용합니다.
[MCP 클라이언트](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/mcp-clients.md)에서 툴셋과 명령이 작성하는 항목을 다룹니다. 그런 다음 코딩 클라이언트의 규칙 파일에 [에이전트 지침](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/agent-instructions.md) 블록을 추가해, 에이전트가 도구를 언제 호출할지 알도록 하세요. Cursor 이외의 클라이언트에서는 항목의 URL에 `?toolset=coding`을 추가해야(`loom install`에서는 `--toolset coding`) 블록에 명시된 도구가 목록에 표시됩니다.

<a id="check-that-it-works"></a>

## 정상 작동 확인

1. Claude Code에서 `/loom:loom-status`를 실행합니다. 플러그인이 호출하는 서버, 토큰이 수락되는지와 네임스페이스에 쓸 수 있는지, 그리고 서버에 도달하지 못한 채 남아 있는 캡처된 배치가 있는지를 보고합니다.
2. 에이전트에게 오래 유지되어야 할 내용을 알려 줍니다. 예: "our deploy window is Tuesday and Thursday only". 새 세션에서 "when can we deploy?"라고 물어봅니다. 답변은 Loom에서 가져옵니다.
3. 대시보드(`<server>/`)를 엽니다. 저장된 사실이 표시되며, 대화 기록이 켜져 있는 동안에는 해당 세션의 대화도 함께 표시됩니다. 둘 다 수정하거나 삭제할 수 있습니다.

CLI가 있다면 `loom doctor --client`로 로컬 연결 상태 점검을 추가로 수행할 수 있습니다.

<a id="when-the-code-index-is-used"></a>

## 코드 인덱스가 사용되는 경우

플러그인은 세션이 인덱싱된 리포지토리의 git 체크아웃 안에서 시작될 때만 코드 인덱스를 연결합니다.

- 체크아웃의 리모트(`origin`, 그다음 `upstream`, 그다음 나머지)를 읽고, 서버가 인덱싱하는 리포지토리 중에서 찾습니다. 자체 인덱스가 없는 포크는 부모 리포지토리의 인덱스를 사용합니다.
- 해당 리포지토리의 코드 네임스페이스에 대한 읽기 권한이 필요합니다. 대시보드의 **Repositories** 페이지에서 읽을 수 있는 리포지토리를 확인하고 참여할 수 있습니다.
- 일치하는 리포지토리가 있으면 세션의 코드 관련 질문은 해당 리포지토리로 범위가 한정되며, 인덱싱된 경로에 대한 grep과 전체 파일 읽기는 마지막으로 인덱싱된 커밋 기준의 인덱스에서 응답합니다.
- 일치하는 리포지토리가 없으면(git 밖의 디렉터리, 아무도 인덱싱하지 않은 리포지토리, 또는 읽을 수 없는 리포지토리) 아무것도 바뀌지 않습니다. 에이전트는 로컬 파일을 읽고, 메모리는 평소처럼 동작합니다.

그래도 특정 리포지토리의 인덱스를 사용하려면 이름을 지정하세요. `LOOM_CODE_REPO=<owner>/<name>`(예: `ClickHouse/loom`, 또는 읽을 수 있는 리포지토리 중 이름이 같은 것이 하나뿐이면 `loom`), Claude Code 플러그인의 `code_repo` 옵션, 또는 `~/.loom/config.json`의 `"code_repo"`(Codex 세션이 읽는 설정)를 사용할 수 있습니다. 이는 대체 수단입니다. git 체크아웃 밖에 있거나, 리포지토리에 인덱스가 없는 체크아웃(URL이 다른 미러 등)에서는 프라이머가 해당 리포지토리의 네임스페이스를 명시하고, 모든 `loom_code_*` 호출이 기본적으로 그 네임스페이스를 사용합니다. 인덱싱된 체크아웃 안에서는 체크아웃 자체의 인덱스가 우선합니다. 다른 리포지토리를 검색하려면 호출에 해당 네임스페이스를 전달하세요. `local_first`(`LOOM_CODE_LOCAL_FIRST=on`)는 grep과 파일 읽기에서 인덱스를 사용하지 않게 합니다. 예를 들어 커밋하지 않은 변경 사항을 작업할 때 유용합니다.
자세한 내용: [Claude Code](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/claude-code.md#the-code-shunt).

<a id="no-token-yet"></a>

## 아직 토큰이 없는 경우

`LOOM_SELF_SIGNUP=open`으로 시작한 서버에서는 토큰을 찾지 못한 첫 Claude Code 세션이 해당 머신을 자동으로 가입시킵니다. 이때 토큰은 `~/.loom/config.json`(모드 0600)에 기록하고, 클레임 코드는 `~/.loom/claim-<namespace>.txt`에 기록합니다. 프라이머는 네임스페이스를 클레임하는 방법을 안내합니다. 이 코드는 서버의 `/oauth/agent-claim` 페이지에서 직접 정한 비밀번호와 함께 한 번만 사용할 수 있습니다. `LOOM_AUTO_SIGNUP=off`로 설정하면 이 동작을 끌 수 있습니다. 셀프 가입을 지원하지 않는 서버에서는 세션이 토큰 없이 시작되며, 프라이머가 토큰을 얻는 방법을 안내합니다.

<a id="next"></a>

## 다음 단계

- [첫 번째 에이전트](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/first-agent.md): SDK를 사용해 직접 작성한 코드에서 동일한 메모리를 사용합니다.
- [에이전트 통합](https://github.com/ClickHouse/loom/blob/main/docs/guides/agent-integration.md): 무엇을 저장하고, 언제 검색해야 하는지, 그리고
  피해야 할 실수를 설명합니다.
- [코드 인덱스](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md): 서버가 인덱싱한 리포지토리에 대해 Loom에 질문합니다.
- [문제 해결](https://github.com/ClickHouse/loom/blob/main/docs/operations/troubleshooting.md): `/loom:loom-status`부터 시작합니다.
