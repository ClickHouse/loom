<!-- i18n: source=docs/guides/headless-bots.md blob=7e255a8b2f49ab5ea38bb7947e4bde837a8a6323 model=claude-sonnet-5-5 -->
> Esta página é uma tradução automática da [página em inglês](https://github.com/ClickHouse/loom/blob/main/docs/guides/headless-bots.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/guides/headless-bots.md), which is authoritative.

<a id="headless-bots"></a>

# Bots headless

Um bot headless roda sem uma pessoa para colar um token, perceber uma falha silenciosa ou dizer
"lembre disto". Este guia aborda a identidade do bot, uma verificação de conexão, três runtimes,
o que o bot grava e como diagnosticar um bot que ficou em silêncio.

<a id="1-give-the-bot-an-identity"></a>

## 1. Dê uma identidade ao bot

A CLI `loom` vem na imagem do servidor e é instalada a partir de um clone com `pip install -e .`.
O pacote do SDK (`clickhouse-loom`) não tem CLI.

**Com self-signup.** Em um servidor com `LOOM_SELF_SIGNUP=open`, o bot se cadastra sozinho:

```bash
loom init --agent nightly-triage --server-url https://<loom> --purpose "Files the nightly failures."
```

`loom init`:

1. Cria o namespace `agent-nightly-triage` (`--purpose` vira a descrição dele).
2. Concede a um subject de service account o papel `writer` nele.
3. Grava o token em `~/.loom/token` (modo 0600) e a URL, o namespace e a org em
   `~/.loom/config.json` (`--token-path` e `--config-path` mudam esses locais).
4. Imprime um código de claim e o endereço da página de claim (`/oauth/agent-claim`). Uma pessoa informa
   o código ali uma única vez, com uma senha própria, e a partir de então entra com o nome do
   namespace e essa senha como administradora dele. O código nunca serve para entrar, e um segundo claim
   com ele é recusado. O token do bot continua funcionando.

`--ttl-days` define o tempo de vida do token (padrão 365). `--org` escolhe uma org diferente da
org de self-signup do servidor.

**No CI**, imprima o token em vez de gravar arquivos e guarde-o como um secret. Com
`--no-files`, o token é a primeira linha da saída:

```bash
loom init --agent ci-reviewer --server-url https://<loom> --no-files | head -n 1 > token.txt
```

Com `--json`, o resultado do cadastro é impresso como JSON sem o token; com
`--json --no-files`, o token vai dentro desse JSON, o único lugar em que ele aparece.

**Sem self-signup**, um operador emite o token, ou uma pessoa cria um em
**Settings → Tokens** que lê apenas os namespaces do bot:

```bash
loom mint-token --subject ci-reviewer --org <org> --namespaces agent-ci --roles writer \
  --service-account --ttl-days 30 --save
```

Veja [tokens e concessões](https://github.com/ClickHouse/loom/blob/main/docs/guides/tokens-and-grants.md) para papéis, tempos de vida e rotação.

<a id="2-check-the-connection-before-the-first-run"></a>

## 2. Verifique a conexão antes da primeira execução

```bash
LOOM_BASE_URL=https://<loom> LOOM_API_KEY="$(cat token.txt)" loom doctor --client
```

`loom doctor --client` resolve o servidor e o token da mesma forma que os hooks do plugin e o SDK, e informa de onde cada um veio. Ele consulta `/v1/health` e `namespace.list_mine`, e verifica se o token pode gravar no namespace do bot. Cada problema aparece como uma linha `FAIL` que indica a correção, e o código de saída é diferente de zero, então o comando funciona como uma verificação de CI:

```yaml
- run: loom doctor --client --server-url ${{ vars.LOOM_BASE_URL }} --token ${{ secrets.LOOM_API_KEY }}
```

`--json` emite as verificações e a lista de problemas como JSON, para um pipeline que as processe.

<a id="3-connect-the-runtime"></a>

## 3. Conectar o runtime

<a id="claude-agent-sdk-or-claude--p"></a>

### Claude Agent SDK ou `claude -p`

Monte o Loom como um servidor MCP a partir de um arquivo e permita apenas as suas ferramentas. Uma execução headless não tem OAuth interativo, então o bearer vai no header:

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

| Flag                            | Por quê                                                                                              |
| ------------------------------- | ---------------------------------------------------------------------------------------------------- |
| `--strict-mcp-config`           | Mantém o bot nos servidores que o arquivo nomeia, não na configuração do usuário.                    |
| `--allowedTools "mcp__loom__*"` | Pré-aprova todas as ferramentas do Loom, para que nenhum pedido de permissão bloqueie a execução.    |
| `--output-format stream-json`   | A primeira mensagem é o `system/init` da sessão (veja abaixo). `claude -p` exige `--verbose` com ele. |
| `--bare`                        | Ignora os hooks, plugins e o `CLAUDE.md` do usuário, para um comportamento idêntico.                 |

> **Warning:** faça a verificação pela conexão. A mensagem `system/init` lista `mcp_servers[]`, cada um com um `status`. Confie na execução somente quando `loom` estiver `connected`; caso contrário, o modelo rodou sem memória.

Escolha a lista de ferramentas com `?toolset=`; os toolsets estão listados em
[Toolsets do MCP](https://github.com/ClickHouse/loom/blob/main/docs/reference/mcp.md#toolsets).

Se o plugin do Loom estiver instalado para o usuário que executa `claude -p`, seus hooks também rodam
([plugin do Claude Code](../features/integrations/claude-code.md)). As dicas do mentor seguem apenas
as ferramentas do servidor do próprio plugin (`mcp__plugin_loom_loom__*`), não uma entrada `loom` vinda de
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

O Codex lê o bearer da variável que a entrada nomeia, no ambiente a partir do qual o `codex`
é iniciado. Com o plugin do Codex instalado, seus hooks também rodam aqui
([plugin do Codex](../features/integrations/codex.md)).

<a id="the-sdk-in-the-bots-own-process"></a>

### O SDK, no processo do próprio bot

Para um bot que é código e não um agente de programação, pule o MCP e chame as operações:

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

`Agent` lê o servidor de `base_url=` ou `LOOM_BASE_URL`, e o token de
`api_key=`, `LOOM_API_KEY` e depois `~/.loom/token`. Outros pontos de entrada:

- `agent.tools()` entrega a um modelo os verbos do Loom (`loom_remember`, `loom_recall`, `loom_ask`,
  `loom_offload`, `loom_fetch`, `loom_note`) como definições de ferramenta com um único `execute()`.
- `agent.session(id)` é a unidade que um framework de agentes mantém: retomar, anexar, fazer flush, fechar.
- Um grafo LangGraph passa `LoomStore` (`loom.integrations.langgraph`) para
  `compile(store=...)`; um agente Strands usa `LoomMemoryStore` (`loom.integrations.strands`).
- [`examples/claude_agent_loop.py`](https://github.com/ClickHouse/loom/blob/main/examples/claude_agent_loop.py) é um loop mínimo
  com o SDK da Anthropic (requer `ANTHROPIC_API_KEY`).

Veja a [referência do SDK Python](https://github.com/ClickHouse/loom/blob/main/docs/reference/python-sdk.md) e as
[integrações com frameworks](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/frameworks.md).

<a id="4-decide-what-the-bot-writes"></a>

## 4. Decida o que o bot escreve

Os plugins capturam uma sessão de programação por conta própria. Um bot fora deles decide o que escrever.
Em resumo: registre decisões e falhas, não transcrições; faça o recall uma vez antes de uma tarefa, não uma vez
por etapa; passe a transcrição de uma execução concluída para `memory.set_from_messages`; e informe no que a
execução se baseou com `run.complete(memories_used=[...])`.
A [integração com agentes](https://github.com/ClickHouse/loom/blob/main/docs/guides/agent-integration.md#3-decide-what-to-write) detalha cada escolha.

<a id="5-when-the-bot-is-quiet"></a>

## 5. Quando o bot está em silêncio

Execute `loom doctor --client` na máquina do bot. Ele informa cada uma das causas mais comuns em uma linha própria:

| Sintoma                                      | Causa                                                                                                                       |
| -------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------- |
| Leituras e escritas vão para outro servidor  | `LOOM_BASE_URL` diverge de `~/.loom/config.json`                                                                            |
| 401 em todas as chamadas                     | O token expirou ou foi rotacionado                                                                                          |
| 403 em todas as escritas                     | O token é `viewer` no namespace em que o bot escreve                                                                        |
| As capturas nunca aparecem                   | Um flusher em segundo plano nunca as entregou: arquivos `loom-flush-*.json` abandonados no diretório temporário             |

<a id="related"></a>

## Relacionados

- [Tokens e grants](https://github.com/ClickHouse/loom/blob/main/docs/guides/tokens-and-grants.md)
- [Integração com agentes](https://github.com/ClickHouse/loom/blob/main/docs/guides/agent-integration.md)
- [Clientes MCP](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/mcp-clients.md)
- [Depurando uma requisição](https://github.com/ClickHouse/loom/blob/main/docs/guides/debugging.md)
