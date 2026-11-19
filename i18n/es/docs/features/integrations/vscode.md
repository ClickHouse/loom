<!-- i18n: source=docs/features/integrations/vscode.md blob=3cffbda831d94714b443b5f9d0a7ffe59bbc0ca5 model=claude-sonnet-5-5 -->
> Esta página es una traducción automática de la [página en inglés](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/vscode.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/vscode.md), which is authoritative.

<a id="vs-code"></a>

# VS Code

El modo agente de VS Code (GitHub Copilot) se conecta a Loom como un servidor MCP remoto mediante HTTP streamable. No hay ninguna extensión de VS Code: las herramientas `loom_*` son la integración, sin captura de sesión ni desvío de código; los plugins de [Claude Code](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/claude-code.md) y
[Codex](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/codex.md) sí los incluyen.

<a id="local-single-user-server"></a>

## Servidor local de un solo usuario

Un servidor de un solo usuario en la misma máquina acepta llamadas MCP sin token.
[Añadir Loom a VS Code](https://vscode.dev/redirect?url=vscode%3Amcp%2Finstall%3F%257B%2522name%2522%253A%2522loom%2522%252C%2522type%2522%253A%2522http%2522%252C%2522url%2522%253A%2522http%253A%252F%252Flocalhost%253A7777%252Fmcp%2522%257D) ·
[Añadir Loom a VS Code Insiders](https://insiders.vscode.dev/redirect?url=vscode-insiders%3Amcp%2Finstall%3F%257B%2522name%2522%253A%2522loom%2522%252C%2522type%2522%253A%2522http%2522%252C%2522url%2522%253A%2522http%253A%252F%252Flocalhost%253A7777%252Fmcp%2522%257D) añade una entrada `servers.loom`
al `mcp.json` del perfil de usuario, con el `type` y la `url` que el dashboard y
`loom install --client vscode` le dan a VS Code. El contenido de los enlaces, codificado como URL en
`vscode:mcp/install?<payload>`, es:

```json
{ "name": "loom", "type": "http", "url": "http://localhost:7777/mcp" }
```

Los enlaces abren el aviso de instalación de VS Code. Desde una terminal,
`code --add-mcp '{"name":"loom","type":"http","url":"http://localhost:7777/mcp"}'` hace
lo mismo. Las sesiones de programación que usan el índice de código añaden `?toolset=coding` a `url`
([elegir un toolset](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/mcp-clients.md#choosing-a-toolset)).

<a id="any-other-server"></a>

## Cualquier otro servidor

Escribe la entrada con su token:

```bash
loom install --client vscode --server-url https://loom.example.com --token "$LOOM_API_KEY" --toolset coding
```

Esto combina una entrada `servers.loom` (`type: "http"`, `url` y una cabecera `Authorization`)
en el `mcp.json` del perfil de usuario, sin tocar `settings.json`. Para mantener el token fuera
del archivo, escribe la entrada a mano con un input que VS Code solicita una vez y almacena:

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

Abre el chat en modo agente y comprueba que las herramientas `loom_*` aparecen en la lista;
[comprobar la conexión](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/mcp-clients.md#checking-the-connection) explica cómo tratar un `401`.

<a id="agent-instructions"></a>

## Instrucciones para el agente

Añade el bloque de [instrucciones para el agente](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/agent-instructions.md) a
`.github/copilot-instructions.md` para que el agente sepa cuándo llamar a las herramientas, a menos que el
`AGENTS.md` del repositorio ya lo incluya. El bloque nombra las herramientas del toolset `coding`:
conéctate con `?toolset=coding` en `url`; de lo contrario, las herramientas que nombra y que el
listado omite se ejecutan mediante `loom_op`, como indica el bloque.

<a id="related"></a>

## Relacionado

- [Conectar un cliente MCP](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/mcp-clients.md): `loom install`, toolsets y los demás editores.
- [Referencia de MCP](https://github.com/ClickHouse/loom/blob/main/docs/reference/mcp.md): endpoint, modos de autenticación y lista de herramientas.
- [Integraciones](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/index.md): todo lo que Loom conecta.
