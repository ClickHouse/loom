<!-- i18n: source=docs/use-cases/agent-memory.md blob=27f44a06830b04a8e513cbab521e16aef4bb7034 model=claude-sonnet-5-5 -->
> Esta página é uma tradução automática da [página em inglês](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/agent-memory.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/agent-memory.md), which is authoritative.

<a id="long-term-and-shared-memory-for-ai-agents"></a>

# Memória de longo prazo e compartilhada para agentes de IA

Loom é um servidor MCP e uma API HTTP self-hosted que dá aos agentes de IA uma memória de longo prazo compartilhada: um fato que um agente salva em uma sessão pode ser encontrado pela sessão seguinte, por outro agente ou por uma pessoa no painel. Esta página mostra como isso funciona e por onde começar.

<a id="what-you-get"></a>

## O que você recebe

- **Uma afirmação por chave, em um namespace.** `memory.set` salva um fato; `memory.get` o lê
  de volta pela chave; `memory.search` e `loom_search` o encontram pelo significado. Um namespace é a
  unidade de compartilhamento: por padrão, todo membro lê todas as linhas dele.
  [Memória](https://github.com/ClickHouse/loom/blob/main/docs/concepts/memory.md)
- **Memória que pode ser corrigida.** Uma alteração cria uma nova versão com seu histórico. O
  mesmo fato, após a canonicalização, é mesclado na linha armazenada; o mesmo fato com outras
  palavras é armazenado como duplicata; uma contradição é sinalizada para revisão; e uma escrita
  contra um fato fixado (pinned) é recusada.
  [Conflitos e recusa](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/conflicts-and-refusal.md) ·
  [Histórico e proveniência](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/history-and-provenance.md)
- **Busca que se abstém.** Quando nada atinge o limite mínimo de similaridade, `memory.search`
  responde `abstain=true` com um código de motivo e marca cada resultado fraco como `below_threshold`;
  `loom_search` os descarta. [Busca e pergunta](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/search-and-ask.md)
- **Memória de longo prazo para o seu agente de código, compartilhada entre agentes.** Os plugins do Claude Code e do
  Codex carregam o contexto relevante no início da sessão e capturam a sessão enquanto ela
  é executada. Cursor, GitHub Copilot, Gemini CLI, Cline, Kilo Code, OpenCode, goose, Zed e outros
  clientes MCP se conectam ao mesmo servidor, e um agente em um namespace encontra os fatos
  que outros agentes salvaram ali.
  [Conectar um agente de código](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md) ·
  [Clientes MCP](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/mcp-clients.md)
- **Memória por usuário para o seu próprio agente.** Os SDKs de Python e TypeScript gravam e leem
  a memória por usuário final. [Seu primeiro agente](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/first-agent.md)

<a id="measured"></a>

## Medições

Em 100 perguntas pareadas do LongMemEval-S, com o mesmo leitor `gpt-5`, as respostas baseadas nas memórias recuperadas pelo Loom tiveram 94% de acerto, contra 90% com o histórico completo, e usaram 91% menos tokens no prompt do leitor (excluindo o custo único de construir a memória); a diferença de acurácia não é estatisticamente estabelecida.
[Com e sem Loom](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#with-and-without-loom)

<a id="start"></a>

## Primeiros passos

O [Guia rápido](../getting-started/quickstart.md) sobe a stack local e salva uma primeira
memória via HTTP, Python e MCP. [Quando o Loom não é a melhor opção](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/overview.md#when-it-does-not-fit)
cobre os casos em que uma biblioteca de memória em processo atende melhor.

Algumas ops desta página estão marcadas como experimentais na [página de status](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md).
