<!-- i18n: source=docs/guides/headless-bots.md blob=7e255a8b2f49ab5ea38bb7947e4bde837a8a6323 model=claude-sonnet-5-5 -->
> Esta página es una traducción automática de la [página en inglés](https://github.com/ClickHouse/loom/blob/main/docs/guides/headless-bots.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/guides/headless-bots.md), which is authoritative.

<a id="headless-bots"></a>

# Bots sin interfaz

Un bot sin interfaz (headless) se ejecuta sin una persona que pegue un token, note un fallo silencioso o diga
"recuerda esto". Esta guía cubre la identidad del bot, una comprobación de la conexión, tres entornos de ejecución,
lo que escribe el bot y cómo diagnosticar un bot que ha dejado de responder.

<a id="1-give-the-bot-an-identity"></a>

## 1. Dale una identidad al bot

El CLI `loom` viene incluido en la imagen del servidor y se instala desde un clon con `pip install -e .`.
El paquete del SDK (`clickhouse-loom`) no incluye CLI.

**Con autorregistro.** En un servidor con `LOOM_SELF_SIGNUP=open`, el bot se registra por sí mismo:

```bash
loom init --agent nightly-triage --server-url https://<loom> --purpose "Files the nightly failures."
```

`loom init`:

1. Crea el espacio de nombres `agent-nightly-triage` (`--purpose` pasa a ser su descripción).
2. Otorga el rol `writer` sobre él a un sujeto de cuenta de servicio.
3. Escribe el token en `~/.loom/token` (modo 0600) y la URL, el espacio de nombres y la organización en
   `~/.loom/config.json` (`--token-path` y `--config-path` cambian esas rutas).
4. Imprime un código de reclamación y la dirección de la página de reclamación (`/oauth/agent-claim`). Una persona introduce
   el código allí una sola vez, con una contraseña propia, y a partir de entonces inicia sesión con el nombre
   del espacio de nombres y esa contraseña como su administrador. El código nunca inicia sesión y se rechaza una segunda reclamación
   con él. El token del bot sigue funcionando.

`--ttl-days` define la vigencia del token (por defecto 365). `--org` elige una organización distinta de la
organización de autorregistro del servidor.

**En CI**, imprime el token en lugar de escribir archivos y guárdalo como secreto. Con
`--no-files`, el token es la primera línea de la salida:

```bash
loom init --agent ci-reviewer --server-url https://<loom> --no-files | head -n 1 > token.txt
```

Con `--json`, el resultado del registro se imprime como JSON sin el token; con
`--json --no-files`, el token va dentro de ese JSON, el único lugar donde aparece.

**Sin autorregistro**, un operador genera el token, o una persona crea uno en
**Settings → Tokens** que solo lee los espacios de nombres del bot:

```bash
loom mint-token --subject ci-reviewer --org <org> --namespaces agent-ci --roles writer \
  --service-account --ttl-days 30 --save
```

Consulta [tokens y permisos](https://github.com/ClickHouse/loom/blob/main/docs/guides/tokens-and-grants.md) para ver roles, vigencias y rotación.

<a id="2-check-the-connection-before-the-first-run"></a>

## 2. Comprobar la conexión antes de la primera ejecución

```bash
LOOM_BASE_URL=https://<loom> LOOM_API_KEY="$(cat token.txt)" loom doctor --client
```

`loom doctor --client` resuelve el servidor y el token igual que los hooks del plugin y el SDK, e indica de dónde proviene cada uno. Sondea `/v1/health` y `namespace.list_mine`, y comprueba que el token pueda escribir en el espacio de nombres del bot. Cada problema aparece como una línea `FAIL` que indica la solución, y el código de salida es distinto de cero, por lo que el comando sirve como control en CI:

```yaml
- run: loom doctor --client --server-url ${{ vars.LOOM_BASE_URL }} --token ${{ secrets.LOOM_API_KEY }}
```

`--json` emite las comprobaciones y la lista de problemas como JSON para un pipeline que las procese.

<a id="3-connect-the-runtime"></a>

## 3. Conectar el runtime

<a id="claude-agent-sdk-or-claude--p"></a>

### Claude Agent SDK o `claude -p`

Monta Loom como servidor MCP desde un archivo y permite solo sus herramientas. Una ejecución headless no tiene OAuth interactivo, así que el bearer viaja en la cabecera:

```bash
cat > loom.mcp.json <<EOF
{"mcpServers": {"loom": {"type": "http",
  "url": "https://<loom>/mcp?toolset=coding",
  "headers": {"Authorization": "Bearer $LOOM_API_KEY"}}}}
EOF
claude -p --mcp-config loom.mcp.json --strict-mcp-config \
  --allowedTools "mcp__loom__*" \
  --output-format stream-json --verbose \
  "Triage the failures in the last nightly run; record each root cause you find."
```

| Flag                            | Por qué                                                                                              |
| ------------------------------- | ---------------------------------------------------------------------------------------------------- |
| `--strict-mcp-config`           | Mantiene el bot en los servidores que nombra el archivo, no en la configuración del usuario.         |
| `--allowedTools "mcp__loom__*"` | Preaprueba todas las herramientas de Loom, de modo que ningún aviso de permisos bloquee la ejecución. |
| `--output-format stream-json`   | El primer mensaje es el `system/init` de la sesión (ver más abajo). `claude -p` necesita `--verbose` con esta opción. |
| `--bare`                        | Omite los hooks, plugins y `CLAUDE.md` del usuario para un comportamiento idéntico.                  |

> **Advertencia:** condiciona la ejecución a la conexión. El mensaje `system/init` lista `mcp_servers[]` con un
> `status` para cada uno. Confía en la ejecución solo cuando `loom` esté `connected`; de lo contrario, el modelo se ejecutó
> sin memoria.

Elige el listado de herramientas con `?toolset=`; los toolsets se enumeran en
[toolsets de MCP](https://github.com/ClickHouse/loom/blob/main/docs/reference/mcp.md#toolsets).

Si el plugin de Loom está instalado para el usuario que ejecuta `claude -p`, sus hooks también se ejecutan
([plugin de Claude Code](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/claude-code.md)). Sus sugerencias de mentor siguen solo
las herramientas del servidor propio del plugin (`mcp__plugin_loom_loom__*`), no una entrada `loom` de
`--mcp-config`.

<a id="codex-codex-exec"></a>

### Codex `codex exec`

```bash
export LOOM_API_KEY="$(cat token.txt)"
codex exec --json \
  -c 'mcp_servers.loom.url="https://<loom>/mcp?toolset=coding"' \
  -c 'mcp_servers.loom.bearer_token_env_var="LOOM_API_KEY"' \
  "Summarise what we decided about retries; record any decision that is missing."
```

Codex lee el bearer de la variable que nombra la entrada, en el entorno desde el que arranca `codex`.
Con el plugin de Codex instalado, sus hooks también se ejecutan aquí
([plugin de Codex](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/codex.md)).

<a id="the-sdk-in-the-bots-own-process"></a>

### El SDK, en el proceso propio del bot

Para un bot que es código y no un agente de programación, omite MCP y llama a las operaciones:

```python
from loom import Agent

async with Agent(org="acme", namespace="agent-nightly-triage", agent="nightly-triage") as a:
    hits = await a.memory.search("flaky tests in the ingest suite", top_k=5)
    await a.memory.set(
        "flaky/ingest_retry_test",
        "Times out against the S3 mock.",
        tags=["failure", "ingest"],
    )
    ans = await a.memory.ask("which suites flaked this week?")
```

`Agent` lee el servidor de `base_url=` o `LOOM_BASE_URL`, y el token de
`api_key=`, `LOOM_API_KEY` y luego `~/.loom/token`. Otros puntos de entrada:

- `agent.tools()` entrega a un modelo los verbos de Loom (`loom_remember`, `loom_recall`, `loom_ask`,
  `loom_offload`, `loom_fetch`, `loom_note`) como definiciones de herramientas con un único `execute()`.
- `agent.session(id)` es la unidad que mantiene un framework de agentes: reanudar, añadir, vaciar y cerrar.
- Un grafo de LangGraph pasa `LoomStore` (`loom.integrations.langgraph`) a
  `compile(store=...)`; un agente de Strands usa `LoomMemoryStore` (`loom.integrations.strands`).
- [`examples/claude_agent_loop.py`](https://github.com/ClickHouse/loom/blob/main/examples/claude_agent_loop.py) es un bucle mínimo
  con el SDK de Anthropic (requiere `ANTHROPIC_API_KEY`).

Consulta la [referencia del SDK de Python](https://github.com/ClickHouse/loom/blob/main/docs/reference/python-sdk.md) y las
[integraciones con frameworks](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/frameworks.md).

<a id="4-decide-what-the-bot-writes"></a>

## 4. Decide qué escribe el bot

Los plugins capturan una sesión de programación por sí solos. Un bot ajeno a ellos decide qué escribir.
En resumen: registra decisiones y fallos, no transcripciones; consulta la memoria una vez antes de una tarea, no una vez
por paso; pasa la transcripción de una ejecución terminada a `memory.set_from_messages`; e informa de lo que la
ejecución utilizó con `run.complete(memories_used=[...])`.
[Integración de agentes](https://github.com/ClickHouse/loom/blob/main/docs/guides/agent-integration.md#3-decide-what-to-write) explica cada una de estas decisiones.

<a id="5-when-the-bot-is-quiet"></a>

## 5. Cuando el bot está en silencio

Ejecuta `loom doctor --client` en la máquina del bot. Informa de cada una de las causas habituales en su propia línea:

| Síntoma                                         | Causa                                                                                                                     |
| ----------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------- |
| Las lecturas y escrituras van a otro servidor   | `LOOM_BASE_URL` no coincide con `~/.loom/config.json`                                                                     |
| 401 en cada llamada                             | El token expiró o fue rotado                                                                                              |
| 403 en cada escritura                           | El token es `viewer` en el espacio de nombres en el que escribe el bot                                                    |
| Las capturas nunca aparecen                     | Un proceso de vaciado en segundo plano nunca las entregó: quedan archivos `loom-flush-*.json` en el directorio temporal   |

<a id="related"></a>

## Relacionado

- [Tokens y grants](https://github.com/ClickHouse/loom/blob/main/docs/guides/tokens-and-grants.md)
- [Integración de agentes](https://github.com/ClickHouse/loom/blob/main/docs/guides/agent-integration.md)
- [Clientes MCP](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/mcp-clients.md)
- [Depuración de una solicitud](https://github.com/ClickHouse/loom/blob/main/docs/guides/debugging.md)
