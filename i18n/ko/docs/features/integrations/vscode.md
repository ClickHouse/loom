<!-- i18n: source=docs/features/integrations/vscode.md blob=3cffbda831d94714b443b5f9d0a7ffe59bbc0ca5 model=claude-sonnet-5-5 -->
> 이 페이지는 [영어 원문](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/vscode.md)을 기계 번역한 것이며, 영어 원문이 기준입니다.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/vscode.md), which is authoritative.

<a id="vs-code"></a>

# VS Code

VS Code의 에이전트 모드(GitHub Copilot)는 스트리밍 가능한 HTTP를 통해 원격 MCP 서버로 Loom에 연결합니다. VS Code 확장은 없습니다. `loom_*` 도구가 곧 통합이며, 세션 캡처나 코드 shunt는 없습니다. 이러한 기능은 [Claude Code](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/claude-code.md) 및
[Codex](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/codex.md) 플러그인에 있습니다.

<a id="local-single-user-server"></a>

## 로컬 단일 사용자 서버

같은 머신의 단일 사용자 서버는 토큰 없이 MCP 호출을 받습니다.
[VS Code에 Loom 추가](https://vscode.dev/redirect?url=vscode%3Amcp%2Finstall%3F%257B%2522name%2522%253A%2522loom%2522%252C%2522type%2522%253A%2522http%2522%252C%2522url%2522%253A%2522http%253A%252F%252Flocalhost%253A7777%252Fmcp%2522%257D) ·
[VS Code Insiders에 Loom 추가](https://insiders.vscode.dev/redirect?url=vscode-insiders%3Amcp%2Finstall%3F%257B%2522name%2522%253A%2522loom%2522%252C%2522type%2522%253A%2522http%2522%252C%2522url%2522%253A%2522http%253A%252F%252Flocalhost%253A7777%252Fmcp%2522%257D)는 사용자 프로필의 `mcp.json`에 `servers.loom` 항목을 추가하며, `type`과 `url`은 대시보드와 `loom install --client vscode`가 VS Code에 제공하는 값과 같습니다. 링크의 페이로드는 `vscode:mcp/install?<payload>` 형태로 URL 인코딩되어 있으며 다음과 같습니다.

```json
{ "name": "loom", "type": "http", "url": "http://localhost:7777/mcp" }
```

링크를 열면 VS Code의 설치 프롬프트가 나타납니다. 터미널에서는
`code --add-mcp '{"name":"loom","type":"http","url":"http://localhost:7777/mcp"}'`로
같은 작업을 할 수 있습니다. 코드 인덱스를 사용하는 코딩 세션은 `url`에 `?toolset=coding`을 추가합니다
([툴셋 선택](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/mcp-clients.md#choosing-a-toolset)).

<a id="any-other-server"></a>

## 그 외의 서버

토큰과 함께 항목을 작성합니다.

```bash
loom install --client vscode --server-url https://loom.example.com --token "$LOOM_API_KEY" --toolset coding
```

이 명령은 `servers.loom` 항목(`type: "http"`, `url`, `Authorization` 헤더)을 사용자 프로필의 `mcp.json`에 병합하며 `settings.json`은 건드리지 않습니다. 토큰이 파일에 남지 않게 하려면, VS Code가 한 번 묻고 저장하는 입력을 사용해 항목을 직접 작성합니다.

```json
{
  "servers": {
    "loom": {
      "type": "http",
      "url": "https://loom.example.com/mcp?toolset=coding",
      "headers": { "Authorization": "Bearer ${input:loom-token}" }
    }
  },
  "inputs": [
    { "id": "loom-token", "type": "promptString", "password": true, "description": "Loom token" }
  ]
}
```

에이전트 모드 채팅을 열고 `loom_*` 도구가 목록에 있는지 확인합니다.
`401` 오류는 [연결 확인](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/mcp-clients.md#checking-the-connection)에서 다룹니다.

<a id="agent-instructions"></a>

## 에이전트 지침

저장소의 `AGENTS.md`에 이미 포함되어 있지 않다면, 에이전트가 도구를 언제 호출해야 하는지 알 수 있도록 [에이전트 지침](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/agent-instructions.md) 블록을 `.github/copilot-instructions.md`에 추가합니다. 이 블록은 `coding` 툴셋의 도구를 지칭하므로, `url`에 `?toolset=coding`을 붙여 연결합니다. 그렇지 않으면 블록에 명시된 도구 중 목록에 없는 것은 블록의 설명대로 `loom_op`를 통해 실행됩니다.

<a id="related"></a>

## 관련 문서

- [MCP 클라이언트 연결](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/mcp-clients.md): `loom install`, 툴셋 및 다른 에디터.
- [MCP 레퍼런스](https://github.com/ClickHouse/loom/blob/main/docs/reference/mcp.md): 엔드포인트, 인증 모드, 도구 목록.
- [통합](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/index.md): Loom이 연결되는 모든 대상.
