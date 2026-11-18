<!-- i18n: source=docs/getting-started/connect-a-coding-agent.md blob=4a72e3770686ed00295619940601d7f23baa7988 model=claude-sonnet-5-5 -->
> Esta página es una traducción automática de la [página en inglés](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md), which is authoritative.

<a id="shared-memory-for-claude-code-codex-and-cursor"></a>

# Memoria compartida para Claude Code, Codex y Cursor

Loom ofrece a Claude Code, Codex y Cursor una memoria a largo plazo compartida mediante plugins o herramientas MCP conectados al mismo servidor autoalojado. Dale a cada integrante del equipo un token con acceso al espacio de nombres del equipo para que sus agentes puedan encontrar los datos que guardan los demás. Instala el plugin o la entrada de MCP que aparece a continuación; la siguiente sesión podrá recuperar lo que un agente guardó. Lo que los plugins de Claude Code y Codex envían a Loom se describe en [tratamiento de datos](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/claude-code.md#data-handling). El registro de conversaciones está desactivado por defecto; el propietario del espacio de nombres lo activa con el interruptor **Record conversations** en la página Conversations del dashboard, o con `conversation.set_logging`. Para ejecutar un servidor por tu cuenta, empieza con el [inicio rápido](quickstart.md); para que tu agente instale una versión fijada y compruebe que funciona, entrégale la dirección `https://github.com/ClickHouse/loom/releases/latest/download/agent-setup.txt` de la última versión ([Configura Loom con tu agente de programación](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/agent-setup.md)).

<a id="what-you-need"></a>

## Qué necesitas

![Settings → Tokens: un token nuevo y la configuración de Claude Code que imprime](https://github.com/ClickHouse/loom/raw/main/docs/images/dashboard-connect.png)

- **La URL del servidor**, por ejemplo `https://loom.example.com`, o `http://localhost:7777`
  para un stack iniciado con `loom up`.
- **Un token de escritor para tu espacio de nombres**, excepto con un servidor de un solo usuario en la misma
  máquina: en ese caso los plugins y los enlaces de instalación de
  [Cursor](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/cursor.md#mcp-tools) y
  [VS Code](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/vscode.md#local-single-user-server) se conectan sin uno.
  Crea uno en el dashboard en
  `<server>/settings` → **Tokens**, o pide a un operador que ejecute
  `loom mint-token --subject you --org <org> --namespaces <namespace> --roles writer`.
  En un servidor con registro automático abierto, el plugin de Claude Code puede registrar la máquina por
  sí mismo en la primera sesión. Consulta [qué ocurre sin credencial](#no-token-yet).

<a id="claude-code"></a>

<a id="how-do-i-give-claude-code-shared-memory"></a>

## ¿Cómo le doy a Claude Code una memoria compartida?

Instala el plugin de Loom con la URL del servidor y un token de escritor para el espacio de nombres que
quieras compartir. El plugin carga las memorias relevantes al inicio de la sesión y le da al agente
herramientas para guardar y buscar datos.

```bash
claude plugin marketplace add ClickHouse/loom
claude plugin install loom@loom \
  --config server_url=https://loom.example.com \
  --config token=<writer JWT>
```

Ambos valores de `--config` son opcionales; si no los indicas, Claude Code te los pide una sola vez. Inicia una nueva
sesión. El primer contexto de sesión de Loom aparece al principio y, a partir de ahí:

- los hooks envían a Loom lo que indica [el tratamiento de datos](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/claude-code.md#data-handling);
- los skills `loom-memory`, `loom-code`, `loom-session` y otros le indican a Claude cuándo guardar
  un dato, cuándo buscar y cómo usar el índice de código;
- `loom_search`, `loom_memory_set` y las herramientas de código están disponibles como herramientas MCP.

Todo lo que instala el plugin, y cómo configurarlo, está en la página de
[Claude Code](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/claude-code.md).

<a id="codex"></a>

<a id="how-do-i-give-codex-shared-memory"></a>

## ¿Cómo le doy a Codex una memoria compartida?

Instala el plugin de Loom, conecta sus hooks y su entrada MCP al mismo servidor y usa un
token con acceso al espacio de nombres compartido. El plugin carga una introducción de la memoria cuando empieza una sesión; sus hooks necesitan la aprobación que se describe a continuación.

```bash
codex plugin marketplace add ClickHouse/loom
codex plugin add loom@loom
```

Después ejecuta `codex` de forma interactiva una vez y [aprueba los hooks del plugin](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/codex.md#approve-the-hooks);
hasta entonces las herramientas funcionan, pero no se captura nada.

La entrada MCP del plugin apunta a `http://localhost:7777`. Para usar otro servidor, escribe su URL
y tu token en `~/.loom/config.json` y reemplaza la entrada MCP en
`~/.codex/config.toml`, como se muestra en [Codex: el servidor](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/codex.md#the-server).

El plugin de Codex incluye los mismos hooks y skills; las diferencias están en la página de
[Codex](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/codex.md). No ejecutes también `codex mcp add loom` con
el plugin instalado: una entrada con el mismo nombre reemplaza la del plugin.

<a id="cursor-and-other-mcp-clients"></a>

<a id="how-do-i-give-cursor-or-another-mcp-client-shared-memory"></a>

## ¿Cómo le doy memoria compartida a Cursor u otro cliente MCP?

Conecta el cliente al endpoint MCP de Loom con un token del espacio de nombres compartido y luego
añade las instrucciones para agentes enlazadas más abajo. Las herramientas guardan y recuperan datos; para capturar
la transcripción de la sesión también se necesita la integración de hooks del cliente.

En un plan Cursor Teams o Enterprise, un administrador del equipo puede importar el [plugin de
Cursor](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/cursor.md#plugin) (sin probar en Cursor), que incluye la
entrada MCP y las skills de Loom.

Para Cursor o VS Code con un servidor de un solo usuario en la misma máquina, no hace falta ningún token:
usa el enlace de instalación de [Cursor](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/cursor.md#mcp-tools) o de
[VS Code](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/vscode.md#local-single-user-server). De lo contrario, en el dashboard,
abre **Settings → Tokens**, rellena el formulario **New token** (pulsa primero **New token** si ya existe un
token) y pulsa **Create token**; después elige tu cliente (Cursor, VS Code, Cline,
Windsurf, Zed o Claude Desktop) en **Connect**: el diálogo muestra la entrada para su archivo
de configuración, con el token incluido. Para cualquier otro cliente MCP, copia la entrada de
[clientes MCP](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/mcp-clients.md). Con la CLI `loom` instalada
(`pip install -e .` desde un clon del repositorio), un solo comando escribe la entrada:

```bash
loom install --client cursor --server-url https://loom.example.com --token "$LOOM_API_KEY"
```

Reinicia el cliente después. `--client` también acepta `claude-desktop`, `vscode`, `cline`,
`windsurf` y `zed`; sin él, `loom install` configura todos los clientes que encuentre. Estos clientes
obtienen las herramientas; la captura de transcripciones necesita un hook, que [Cursor](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/cursor.md)
tiene y los demás toman de una [plantilla de transcripción](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/index.md#transcript-capture-hooks).
[Clientes MCP](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/mcp-clients.md) explica los conjuntos de herramientas y la entrada que escribe el
comando. Después añade el bloque de [instrucciones para agentes](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/agent-instructions.md)
al archivo de reglas de un cliente de programación, para que el agente sepa cuándo llamar a las herramientas; para un
cliente distinto de Cursor, añade `?toolset=coding` a la URL de la entrada (`--toolset coding`
con `loom install`) para que se listen las herramientas que nombra el bloque.

<a id="check-that-it-works"></a>

## Comprobar que funciona

1. En Claude Code, ejecuta `/loom:loom-status`. Informa del servidor al que llama el plugin, si el token es aceptado y puede escribir en el espacio de nombres, y de cualquier lote capturado que nunca llegó al servidor.
2. Dile al agente algo duradero, por ejemplo "our deploy window is Tuesday and Thursday only". En una sesión nueva, pregunta "when can we deploy?". La respuesta proviene de Loom.
3. Abre el dashboard (`<server>/`). El hecho guardado aparece allí, y también la conversación de la sesión mientras el registro de conversaciones esté activado; puedes corregir o eliminar cualquiera de los dos.

`loom doctor --client` añade las comprobaciones de la configuración local cuando tienes la CLI.

<a id="when-the-code-index-is-used"></a>

## Cuándo se usa el índice de código

El plugin adjunta el índice de código solo cuando la sesión se inicia dentro de un checkout de git de
un repositorio indexado:

- Lee los remotes del checkout (`origin`, luego `upstream`, luego los demás) y los busca
  entre los repositorios que indexa el servidor. Un fork sin índice propio usa
  el índice de su repositorio padre.
- Necesitas acceso de lectura al espacio de nombres de código de ese repositorio. La página **Repositories** del
  dashboard muestra lo que puedes leer y te permite unirte.
- Si hay coincidencia, las preguntas sobre código de la sesión se limitan a ese repositorio, y las búsquedas con grep y las lecturas
  de archivos completos de rutas indexadas se responden desde el índice, tal como está en su último commit indexado.
- Si no hay coincidencia (un directorio fuera de git, un repositorio que nadie indexó o uno que no puedes
  leer), no cambia nada: el agente lee los archivos locales y la memoria funciona como de costumbre.

Para usar de todos modos el índice de un repositorio, nómbralo: `LOOM_CODE_REPO=<owner>/<name>` (por ejemplo
`ClickHouse/loom`, o solo `loom` cuando un único repositorio que puedes leer tiene ese nombre), la
opción `code_repo` del plugin de Claude Code, o `"code_repo"` en `~/.loom/config.json` (el
ajuste que lee una sesión de Codex). Es un valor de reserva: fuera de cualquier checkout de git, o en un
checkout cuyo repositorio no tiene índice (un mirror con una URL distinta), el primer mensaje indica
el espacio de nombres de ese repositorio y cada llamada `loom_code_*` lo usa por defecto. Dentro de un checkout
indexado, el índice del propio checkout sigue prevaleciendo; para buscar en otro repositorio, pasa su
espacio de nombres a la llamada. `local_first` (`LOOM_CODE_LOCAL_FIRST=on`) mantiene
el índice fuera de grep y de las lecturas de archivos, por ejemplo mientras trabajas con cambios sin commit.
Detalles: [Claude Code](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/claude-code.md#the-code-shunt).

<a id="no-token-yet"></a>

## Aún sin token

En un servidor iniciado con `LOOM_SELF_SIGNUP=open`, la primera sesión de Claude Code que no encuentra ningún token registra la máquina. Escribe el token en `~/.loom/config.json` (modo 0600) y un código de reclamación en `~/.loom/claim-<namespace>.txt`, y el primer mensaje de contexto explica cómo reclamar el espacio de nombres: el código funciona una sola vez, en la página `/oauth/agent-claim` del servidor, con una contraseña propia. `LOOM_AUTO_SIGNUP=off` desactiva este comportamiento. Un servidor sin registro automático deja la sesión sin token, y el mensaje de contexto explica cómo obtener uno.

<a id="next"></a>

## Siguiente

- [Tu primer agente](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/first-agent.md): la misma memoria desde tu propio código con el SDK.
- [Integración de agentes](https://github.com/ClickHouse/loom/blob/main/docs/guides/agent-integration.md): qué guardar, cuándo buscar y
  qué errores evitar.
- [Índice de código](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md): pregunta a Loom sobre un repositorio que el servidor
  indexa.
- [Solución de problemas](https://github.com/ClickHouse/loom/blob/main/docs/operations/troubleshooting.md), empezando por `/loom:loom-status`.
