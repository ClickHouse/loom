<!-- i18n: source=docs/use-cases/coding-agent-cost.md blob=d7a120fc7dc895117d934de1f2dc5ffd14bca474 model=claude-sonnet-5-5 -->
> Esta página é uma tradução automática da [página em inglês](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/coding-agent-cost.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/coding-agent-cost.md), which is authoritative.

<a id="lower-model-cost-for-coding-agents"></a>

# Custo de modelo menor para agentes de código

Com o plugin do Loom e o índice de código, o Claude Code respondeu perguntas sobre a base de código do ClickHouse com um custo de modelo de 6% a 21% menor do que sem ele, nos três conjuntos de perguntas medidos. A economia foi de 20% em 24 perguntas avaliadas por resposta (qualidade da resposta de 0.84 contra 0.78), de 21% em 36 perguntas conceituais e de 6% em 24 perguntas multi-hop, nas quais rastrear um bug até a correção em um branch de release custou o mesmo nos dois casos.

<a id="what-was-measured"></a>

## O que foi medido

Os dois grupos executaram o Claude Code (`claude-sonnet-5`) no mesmo checkout: uma sessão simples e outra com o plugin do Loom e o toolset `coding`. O custo é a fatura de modelo da sessão inteira e não inclui as chamadas de modelo feitas pelo servidor do Loom, a hospedagem nem a indexação. A comparação mede o plugin como um todo; ela não separa o índice de código da memória. Os resultados são específicos das cargas de trabalho testadas. Com o Loom, uma pergunta leva mais tempo no relógio: a busca por prompt do plugin e as idas e vindas do MCP adicionam latência. A forma como o custo por pergunta é comparado com o ruído entre execuções está na página de resultados.
[With and without Loom](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#with-and-without-loom) traz todos os conjuntos, o protocolo e os artefatos, e [evaluate Loom](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/evaluate.md) mostra como medir no seu próprio repositório.

Para a memória lida por um modelo, em vez de todo o histórico de um chat, consulte
[memória de agente](agent-memory.md#measured).

<a id="how-the-plugin-is-designed-to-save"></a>

## Como o plugin foi projetado para economizar

- **Respostas do índice em vez de leituras de arquivos.** `code.symbol`, `code.callers` e `code.search`
  retornam as linhas que respondem a uma pergunta de estrutura. [Índice de código](code-intelligence.md)
- **Contexto salvo em vez de redescoberta.** As decisões e descobertas que uma sessão salva são
  carregadas no início da sessão seguinte. [Memória compartilhada](agent-memory.md)

O Loom também registra, por operação, o que gasta com embeddings e chamadas de LLM.
[Custo e uso](https://github.com/ClickHouse/loom/blob/main/docs/features/observability/cost-and-usage.md)
