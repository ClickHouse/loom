<!-- i18n: source=docs/use-cases/agent-memory.md blob=7278a12f3a940bfba3a0dead9a5e37fa445bb68a model=claude-sonnet-5-5 -->
> Esta página é uma tradução automática da [página em inglês](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/agent-memory.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/agent-memory.md), which is authoritative.

<a id="long-term-and-shared-memory-for-ai-agents"></a>

# Memória de longo prazo e compartilhada para agentes de IA

Loom é um servidor MCP e uma API HTTP self-hosted que dá aos agentes de IA uma memória de longo prazo compartilhada: um fato que um agente salva em uma sessão pode ser encontrado pela sessão seguinte, por outro agente ou por uma pessoa no dashboard. Esta página mostra como isso funciona e por onde começar.

<a id="what-you-get"></a>

## O que você recebe

- **Uma afirmação por chave, em um namespace.** `memory.set` salva um fato; `memory.get` o lê
  de volta pela chave; `memory.search` e `loom_search` o encontram pelo significado. Um namespace é a
  unidade de compartilhamento: por padrão, todo membro lê todas as linhas dele.
  [Memory](https://github.com/ClickHouse/loom/blob/main/docs/concepts/memory.md)
- **Memória que pode ser corrigida.** Uma alteração cria uma nova versão com seu histórico. O
  mesmo fato, após a canonicalização, é mesclado na linha armazenada; o mesmo fato com outras
  palavras é armazenado como duplicata; uma contradição é sinalizada para revisão; e uma escrita
  contra um fato fixado (pinned) é recusada.
  [Conflicts and refusal](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/conflicts-and-refusal.md) ·
  [History and provenance](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/history-and-provenance.md)
- **Busca que se abstém.** Quando nada atinge o limite mínimo de similaridade, `memory.search`
  responde `abstain=true` com um código de motivo e marca cada resultado fraco como `below_threshold`;
  `loom_search` os descarta. [Search and ask](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/search-and-ask.md)
- **Memória entre agentes de código.** Os plugins do Claude Code e do Codex carregam o contexto
  relevante no início da sessão e capturam a sessão enquanto ela roda; o Cursor e outros clientes
  MCP se conectam ao mesmo servidor. [Connect a coding agent](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md)
- **Memória por usuário para o seu próprio agente.** Os SDKs de Python e TypeScript gravam e leem
  a memória por usuário final. [Your first agent](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/first-agent.md)

<a id="measured"></a>

## Medido

Em 100 perguntas pareadas do LongMemEval-S, usando o mesmo leitor `gpt-5`, as respostas geradas a partir das memórias recuperadas pelo Loom obtiveram 94% de acerto, contra 90% usando o histórico completo, com 91% menos tokens no prompt do leitor (sem contar o custo único de construir a memória); a diferença de acurácia não é estatisticamente comprovada.
[Com e sem o Loom](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#with-and-without-loom)

<a id="start"></a>

## Início

O [Quickstart](../getting-started/quickstart.md) inicia a stack local e salva uma primeira
memória via HTTP, Python e MCP. [Quando o Loom não é a melhor opção](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/overview.md#when-it-does-not-fit)
descreve os casos em que uma biblioteca de memória em processo atende melhor.

Algumas ops desta página estão marcadas como experimentais na [página de status](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md).
