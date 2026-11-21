<!-- i18n: source=docs/features/integrations/vscode.md blob=3cffbda831d94714b443b5f9d0a7ffe59bbc0ca5 model=claude-sonnet-5-5 -->
> Esta página é uma tradução automática da [página em inglês](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/vscode.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/vscode.md), which is authoritative.

<a id="vs-code"></a>

# VS Code

O modo agente do VS Code (GitHub Copilot) se conecta ao Loom como um servidor MCP remoto por HTTP com streaming (streamable HTTP). Não há extensão do VS Code: as ferramentas `loom_*` são a integração, sem captura de sessão nem desvio de código; os plugins do [Claude Code](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/claude-code.md) e do
[Codex](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/codex.md) oferecem esses recursos.

<a id="local-single-user-server"></a>

## Servidor local de usuário único

Um servidor de usuário único na mesma máquina aceita chamadas MCP sem token.
[Adicionar o Loom ao VS Code](https://vscode.dev/redirect?url=vscode%3Amcp%2Finstall%3F%257B%2522name%2522%253A%2522loom%2522%252C%2522type%2522%253A%2522http%2522%252C%2522url%2522%253A%2522http%253A%252F%252Flocalhost%253A7777%252Fmcp%2522%257D) ·
[Adicionar o Loom ao VS Code Insiders](https://insiders.vscode.dev/redirect?url=vscode-insiders%3Amcp%2Finstall%3F%257B%2522name%2522%253A%2522loom%2522%252C%2522type%2522%253A%2522http%2522%252C%2522url%2522%253A%2522http%253A%252F%252Flocalhost%253A7777%252Fmcp%2522%257D) adiciona uma entrada `servers.loom`
ao `mcp.json` do perfil do usuário, com o `type` e o `url` que o painel e
`loom install --client vscode` fornecem ao VS Code. O conteúdo dos links, codificado como URL em
`vscode:mcp/install?<payload>`, é:

```json
{ "name": "loom", "type": "http", "url": "http://localhost:7777/mcp" }
```

Os links abrem o prompt de instalação do VS Code. A partir de um terminal,
`code --add-mcp '{"name":"loom","type":"http","url":"http://localhost:7777/mcp"}'` faz
o mesmo. Sessões de programação que usam o índice de código adicionam `?toolset=coding` ao `url`
([escolhendo um toolset](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/mcp-clients.md#choosing-a-toolset)).

<a id="any-other-server"></a>

## Qualquer outro servidor

Grave a entrada com o seu token:

```bash
loom install --client vscode --server-url https://loom.example.com --token "$LOOM_API_KEY" --toolset coding
```

Isso mescla uma entrada `servers.loom` (`type: "http"`, `url` e um cabeçalho `Authorization`)
no `mcp.json` do perfil do usuário, sem alterar o `settings.json`. Para manter o token fora
do arquivo, escreva a entrada manualmente com um input que o VS Code solicita uma vez e armazena:

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

Abra o chat no modo agente e verifique se as ferramentas `loom_*` estão listadas;
[verificando a conexão](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/mcp-clients.md#checking-the-connection) trata de um erro `401`.

<a id="agent-instructions"></a>

## Instruções para o agente

Adicione o bloco de [instruções para o agente](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/agent-instructions.md) ao
`.github/copilot-instructions.md` para que o agente saiba quando chamar as ferramentas, a menos que o
`AGENTS.md` do repositório já o contenha. O bloco cita as ferramentas do toolset `coding`:
conecte com `?toolset=coding` no `url`; caso contrário, as ferramentas citadas que a
listagem omite são executadas por meio de `loom_op`, como o bloco indica.

<a id="related"></a>

## Veja também

- [Conectando um cliente MCP](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/mcp-clients.md): `loom install`, toolsets e os outros editores.
- [Referência do MCP](https://github.com/ClickHouse/loom/blob/main/docs/reference/mcp.md): endpoint, modos de autenticação e a lista de ferramentas.
- [Integrações](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/index.md): tudo ao que o Loom se conecta.
