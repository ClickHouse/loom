<!-- i18n: source=docs/use-cases/code-intelligence.md blob=42cc2c3e3e4af87c1d93ff61f15c3e5f7c8f6117 model=claude-sonnet-5-5 -->
> Esta página é uma tradução automática da [página em inglês](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/code-intelligence.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/code-intelligence.md), which is authoritative.

<a id="a-code-index-for-coding-agents"></a>

# Um índice de código para agentes de programação

O Loom oferece aos agentes de programação um índice compartilhado dos seus repositórios git, servido via MCP. Assim, o agente faz uma pergunta sobre a estrutura do código e recebe as linhas que a respondem, em vez de ler arquivos inteiros. Requer um repositório adicionado e `LOOM_CODE_INGEST_ENABLED=true`. Como o índice é construído e mantido atualizado está na [visão geral do índice de código](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md).

<a id="what-you-get"></a>

## O que você recebe

- **Definições, chamadores e impacto.** `code.symbol`, `code.callers` e `code.impact`
  respondem com o arquivo e a linha, e cada resposta indica o commit e o nível de índice
  por trás dela. [Code operations](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/operations.md)
- **Busca por significado e por string exata.** `code.search` encontra onde um mecanismo vive;
  `code.grep` encontra uma mensagem ou um identificador exato.
- **Histórico e pull requests.** Histórico de PRs mesclados com comentários de revisão, uma verificação
  de duplicidade antes do PR e candidatos de bisect para uma regressão. Esses recursos exigem que o histórico de PRs esteja ativado
  (`LOOM_CODE_PR_HISTORY_ENABLED`); comentários de revisão e PRs abertos também exigem um token do GitHub.
  [History, PRs and releases](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/history-and-prs.md) explica o que cada um
  exige.
- **Atualização visível.** O índice fica atrás do branch pelos commits mesclados, e uma
  resposta desatualizada avisa isso. [Freshness](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/freshness.md)
- **Plugins.** O plugin do Claude Code se conecta no toolset `coding` e o plugin do Codex
  em `core,coding,tracker,tasks`; ambos listam as ferramentas de código. O Cursor e outros clientes MCP usam
  `?toolset=coding`.
  [Connect a coding agent](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md)

<a id="measured"></a>

## Medições

No repositório do ClickHouse, pediu-se ao Claude Code que terminasse cada resposta com até três
arquivos. Com o Loom, ele indicou o arquivo correto em 96.4% de 36 perguntas conceituais (10 execuções
pareadas), contra 91.4% sem o Loom, e as execuções com o Loom custaram 21% menos (a conta do modelo
do agente; as chamadas de modelo do próprio Loom não estão incluídas).
Em 200 issues do SWE-bench Verified, `code.search` classificou um arquivo alterado entre os 10 primeiros
resultados em 82% dos casos (localização de arquivos, não resolução de issues), contra 57% do BM25 sobre todos os arquivos
indexados e 79% do BM25 apenas sobre arquivos `.py`; a diferença para o caso só com `.py` está dentro do ruído.
[Com e sem o Loom](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#with-and-without-loom) ·
[Localização de arquivos no SWE-bench Verified](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#swe-bench-verified-file-localization)

<a id="start"></a>

## Início

[Adicionar repositórios](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/repositories.md) ativa o índice para um
repositório; [a visão geral do índice de código](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md) explica os
níveis.

Algumas ops desta página estão marcadas como experimentais na [página de status](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md).
