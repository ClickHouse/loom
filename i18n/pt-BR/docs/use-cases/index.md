<!-- i18n: source=docs/use-cases/index.md blob=faab303a28ac13877ddbd45c24561cb50d90c53e model=claude-sonnet-5-5 -->
> Esta página é uma tradução automática da [página em inglês](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/index.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/index.md), which is authoritative.

<a id="use-cases"></a>

# Casos de uso

Cada página responde a uma pergunta que pessoas e agentes fazem ao procurar uma ferramenta: o que o Loom
faz para essa necessidade e por onde começar.

| Necessidade                                                                      | Página                                                     |
| -------------------------------------------------------------------------------- | ---------------------------------------------------------- |
| Memória de longo prazo e compartilhada para agentes de IA (um servidor de memória MCP) | [Memória de agente](agent-memory.md)                 |
| Busca de conhecimento e de documentos para agentes (RAG)                         | [Busca de documentos](document-search.md)                  |
| Um índice de código e inteligência de código para agentes de programação         | [Índice de código](code-intelligence.md)                   |
| O que o índice de código oferece por linguagem (C++, Python, TypeScript, Go, Rust, Java) | [Índice de código por linguagem](code-intelligence-by-language.md) |
| Memória vinculada ao código a que se refere, sinalizada quando o código muda     | [Memória vinculada ao código](memory-linked-to-code.md)    |
| Menor custo de modelo para agentes de programação                                | [Custo de agentes de programação](coding-agent-cost.md)    |
| Contexto compartilhado entre agentes em um fluxo de trabalho com vários agentes  | [Vários agentes](multi-agent.md)                           |
| Memória, recuperação, auditoria e exclusão para agentes autônomos                | [Agentes autônomos](autonomous-agents.md)                  |
| Execuções de agentes, traces, tracker e histórico de PRs                         | [Execuções e histórico de PRs](runs-and-pr-history.md)     |
| Memória de agente auto-hospedada no ClickHouse, com Docker ou Kubernetes         | [Auto-hospedado no ClickHouse](self-hosted-clickhouse.md)  |

[Quando o Loom não se encaixa](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/overview.md#when-it-does-not-fit) lista as
necessidades que outra ferramenta atende melhor.
