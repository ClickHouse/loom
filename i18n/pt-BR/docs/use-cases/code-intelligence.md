<!-- i18n: source=docs/use-cases/code-intelligence.md blob=9f10f6e9bc93b39e97d3aee93cedeeb7260a7297 model=claude-sonnet-5-5 -->
> Esta página é uma tradução automática da [página em inglês](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/code-intelligence.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/code-intelligence.md), which is authoritative.

<a id="a-code-index-for-coding-agents"></a>

# Um índice de código para agentes de programação

O Loom oferece aos agentes de programação um índice compartilhado dos seus repositórios git, servido via MCP, de modo que um
agente faz uma pergunta sobre estrutura e recebe as linhas que a respondem, em vez de ler arquivos
inteiros. Exige um repositório adicionado e `LOOM_CODE_INGEST_ENABLED=true`. Como o índice é
construído e mantido atualizado está em [a visão geral do índice de código](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md);
o que ele oferece em cada linguagem está em [índice de código por linguagem](code-intelligence-by-language.md).

<a id="what-you-get"></a>

## O que você recebe

- **Definições, chamadores e impacto.** `code.symbol`, `code.callers` e `code.impact`
  localizam código com referências de arquivo e linha. `code.index_status` informa o commit
  indexado de cada camada. [Operações de código](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/operations.md)
- **Busca por significado e por string exata.** `code.search` encontra onde um mecanismo vive;
  `code.grep` encontra uma mensagem ou um identificador exato.
- **Histórico e pull requests.** Histórico de PRs mesclados com comentários de revisão, uma verificação
  de duplicidade antes do PR e candidatos de bisect para uma regressão. Esses recursos exigem o histórico de PRs ativado
  (`LOOM_CODE_PR_HISTORY_ENABLED`); comentários de revisão e PRs abertos também exigem um token do GitHub.
  [Histórico, PRs e releases](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/history-and-prs.md) informa o que cada um
  exige.
- **Lições sobre o código.** `code.symbol` retorna as memórias vinculadas a uma definição, e
  `code.search`, `code.callers`, `code.callees`, `code.impact`, `code.outline` e o
  `code.context_pack` de uma tarefa anexam as lições dos arquivos que retornam, cada uma indicando se
  o código dela foi movido, alterado ou removido. [Memória vinculada ao código](memory-linked-to-code.md)
- **Atualização verificável.** Cada camada pode ficar atrás do branch. Inspecione `as_of` quando
  retornado, ou use `code.index_status` para ver a marca d'água da camada relevante. A ausência de
  metadados de atualização significa desconhecido. [Atualização](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/freshness.md)
- **Plugins e clientes MCP.** O plugin do Claude Code se conecta no toolset `coding` e
  o plugin do Codex em `core,coding,tracker,tasks`; ambos listam as ferramentas de código. Cursor, GitHub
  Copilot, Gemini CLI, Cline, Kilo Code, OpenCode e outros clientes MCP as listam com
  `?toolset=coding` na URL (`loom install` adiciona isso para o Cursor; `--toolset coding` adiciona
  para os demais que ele grava).
  [Conectar um agente de programação](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md) ·
  [Clientes MCP](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/mcp-clients.md)

<a id="measured"></a>

## Medições

No repositório do ClickHouse, pediu-se ao Claude Code que encerrasse cada resposta com até três
arquivos. Com o Loom, ele indicou o arquivo certo em 96.4% de 36 perguntas conceituais (10 execuções
pareadas), contra 91.4% sem o Loom, e as execuções com o Loom custaram 21% menos (a conta de modelo
do agente; chamadas de modelo do próprio Loom excluídas).
Em 200 issues do SWE-bench Verified, `code.search` classificou um arquivo alterado entre os 10 primeiros
resultados em 82% dos casos (localização de arquivo, não resolução da issue), contra 57% do BM25 sobre todos os arquivos
indexados e 79% do BM25 apenas sobre arquivos `.py`; a diferença para o `.py` isolado está dentro do ruído.
[Com e sem o Loom](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#with-and-without-loom) ·
[Localização de arquivo no SWE-bench Verified](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#swe-bench-verified-file-localization)

<a id="a-coding-agent-that-reads-the-definition-it-needs"></a>

## Um agente de programação que lê a definição de que precisa

Comece com um repositório em que você confia. Na stack lançada, siga os
[passos do índice configurado pelo operador](../getting-started/quickstart.md#optional-index-a-repository-on-the-released-stack);
o fluxo de adicionar repositório do painel precisa de um sandbox que essa stack não habilita. Conecte
o Claude Code ou o Codex com o plugin dele e inicie uma sessão dentro do checkout desse repositório
([correspondência de repositório](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md#when-the-code-index-is-used)).

1. Peça ao agente que chame `loom_op` com `op: "code.index_status"` e
   `arguments: {"namespace": "<indexed namespace>", "brief": true}`. Anote `git_head`,
   o commit indexado da camada rápida; um valor vazio significa que essa camada não tem marca d'água.
2. Escolha uma função cuja definição você conheça. Peça ao agente que chame `loom_code_symbol`
   nesse namespace com o nome da função e `include_body: true`. Verifique
   `path`, `start_line`, `end_line`, `body` e `tier` retornados.
3. Continue esta verificação apenas para uma definição inequívoca com `tier: "treesitter"`
   e um corpo completo, sem trechos omitidos. Compare o caminho, o intervalo e o corpo com o código-fonte no
   `git_head` anotado antes de confiar nele. Use uma leitura local se a marca d'água ou o
   corpo estiver ausente, se a definição for diferente ou se outra camada for retornada. As camadas do compilador
   avançam de forma independente; suas marcas d'água exigem uma verificação de revisão separada
   ([atualização](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/freshness.md#the-mirrors-own-age)).
4. Inicie uma nova sessão no mesmo checkout e repita as duas chamadas. O índice fornece
   a definição novamente; o agente não precisa carregar a conversa anterior nem o arquivo inteiro.

Isso verifica uma definição da camada rápida. Edições não commitadas ainda exigem leituras locais, e uma
consulta bem-sucedida não é uma medição de precisão de tarefa nem de economia de custo. Veja
[operações de código](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/operations.md) para busca, chamadores e impacto.

<a id="start"></a>

## Começar

[Adicionando repositórios](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/repositories.md) ativa o índice para um
repositório; [a visão geral do índice de código](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md) explica as
camadas.

Algumas operações desta página são marcadas como experimentais na [página de status](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md).
