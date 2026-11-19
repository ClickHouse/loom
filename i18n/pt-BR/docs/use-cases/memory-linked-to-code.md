<!-- i18n: source=docs/use-cases/memory-linked-to-code.md blob=fab4f6c6a23848dde1feef736969b204d07adfd8 model=claude-sonnet-5-5 -->
> Esta página é uma tradução automática da [página em inglês](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/memory-linked-to-code.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/memory-linked-to-code.md), which is authoritative.

<a id="memory-linked-to-code-for-coding-agents"></a>

# Memória vinculada ao código para agentes de programação

O Loom vincula as memórias dos agentes de programação às funções, classes e arquivos que elas citam, acompanha esses elementos ao longo de refatorações e sinaliza uma memória quando o código dela muda. Por padrão, a busca deixa de fora uma memória cujo código sumiu por completo, a menos que uma pessoa a tenha fixado, bloqueado ou verificado, e toda leitura que serve uma lição informa quando o código dela mudou ou sumiu. Funciona por MCP, HTTP e pelos SDKs nos repositórios que um Loom auto-hospedado indexa (o
[índice de código](code-intelligence.md), `LOOM_CODE_INGEST_ENABLED=true`), e o worker `memory_code_links`, que mantém os vínculos, vem ativado por padrão.

<a id="what-you-get"></a>

## O que você obtém

- **Lições anexadas ao código a que se referem.** Uma memória marcada com `repo:<id>` (os plugins do Claude Code e do Codex marcam o que seus hooks capturam) ou escrita no namespace de código do repositório é vinculada a cada definição (função, classe ou outro símbolo) que ela cita entre crases, ou em qualquer ponto do texto como `A::b` ou uma chamada (`a.b()`, `name()`), desde que resolva para exatamente um lugar (um nome sem qualificação precisa ter quatro ou mais caracteres); às definições nos arquivos que ela lista e que cita por uma palavra simples em camelCase ou snake_case; e a cada arquivo que ela lista e que não contém nenhuma delas. Um nome qualificado que ainda não está no trunk é vinculado se for mesclado em até `LOOM_MEMORY_CODE_LINKS_PENDING_DAYS` (14 dias).
  [Memórias vinculadas ao código](https://github.com/ClickHouse/loom/blob/main/docs/concepts/memory.md#memories-linked-to-code)
- **Vínculos que acompanham refatorações.** A cada commit indexado, cada vínculo fica como `fresh`, `moved`, `changed` (indicando o commit que o alterou), `unresolved` ou `missing`. Um vínculo registra os tokens da definição, então espaços em branco, comentários e a maioria das mudanças de formatador o mantêm como `fresh`. [Estados do vínculo](https://github.com/ClickHouse/loom/blob/main/docs/concepts/memory.md#memories-linked-to-code)
- **Memórias desatualizadas tratadas como você escolher.** A
  [`code_change_policy`](https://github.com/ClickHouse/loom/blob/main/docs/reference/operations/namespace.md#namespaceregister) do namespace decide:
  `retire_removed` (o padrão) deixa de fora da busca uma memória cujo código sumiu por completo e sinaliza código alterado; `flag` apenas sinaliza; `revise` também deixa de fora as memórias com código removido e faz o modelo conferir a afirmação de uma memória alterada contra o código antigo e o novo: se ela ainda vale, é reancorada, e se for contradita, recebe uma sugestão de correção. Uma memória que uma pessoa fixou, bloqueou ou verificou nunca é deixada de fora. Em todas as políticas, a busca costuma classificar uma memória cujo código mudou abaixo dos três resultados seguintes em vez de escondê-la; uma memória retida e uma linha encontrada por nome mantêm sua posição. O Loom nunca reescreve a memória em si. [Políticas](https://github.com/ClickHouse/loom/blob/main/docs/concepts/memory.md#memories-linked-to-code)
- **Lições onde quer que o agente leia código.** `code.symbol` retorna as memórias vinculadas a uma definição como `lessons`; `code.search`, `code.callers`, `code.callees`, `code.impact`, `code.outline` e o `code.context_pack` de uma tarefa anexam as lições dos arquivos que retornam; cada lição traz seu estado de vínculo. Os hooks do Claude Code e do Codex mostram as memórias de um arquivo (`memory.for_file`) antes de um agente editá-lo, e os hooks do Cline fazem isso quando ele lê ou edita um arquivo. [`code.symbol`](https://github.com/ClickHouse/loom/blob/main/docs/reference/operations/code.md#codesymbol) ·
  [Plugin do Claude Code](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/claude-code.md) ·
  [Plugin do Codex](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/codex.md) ·
  [Hooks do Cline](https://github.com/ClickHouse/loom/blob/main/integrations/cline/README.md) (experimental)
- **Compartilhado com a equipe.** As lições são lidas do namespace de código do repositório e dos namespaces que a chamada nomeia (por MCP, o do próprio chamador por padrão), então uma lição salva no namespace de código do repositório chega a todo agente que possa ler esse namespace (um token restrito ao seu próprio usuário ou sessão não lê lições).
  [Memórias vinculadas ao código](https://github.com/ClickHouse/loom/blob/main/docs/concepts/memory.md#memories-linked-to-code)
- **Branches leem o próprio código.** Em outra branch, uma lição é verificada contra a definição daquela branch, então uma branch de release que ainda tem o texto antigo fica como `fresh` (em um host com `LOOM_GIT_HISTORY=none`, que não tem espelho git, uma lição traz o estado do trunk). [Memórias vinculadas ao código](https://github.com/ClickHouse/loom/blob/main/docs/concepts/memory.md#memories-linked-to-code)
- **Documentos ao lado do código.** Documentos de uma fonte registrada no namespace de código do repositório (o do trunk) carregam a tag do repositório, então as buscas no repositório os leem junto com o código (o worker de sincronização precisa de `LOOM_KNOWLEDGE_SOURCE_SYNC_ENABLED`).
  [Fontes de documentos](https://github.com/ClickHouse/loom/blob/main/docs/features/knowledge.md#document-sources)
- **Uma lista de revisão.**
  [`namespace.validate`](https://github.com/ClickHouse/loom/blob/main/docs/reference/operations/namespace.md#namespacevalidate) lista as memórias cujo código mudou, não foi encontrado de novo ou sumiu, e o painel mostra os vínculos de cada memória e o estado deles.

![Detalhe de uma memória no painel: o código vinculado a ela, com uma definição atual e outra alterada desde um commit](https://github.com/ClickHouse/loom/raw/main/docs/assets/screenshots/memory-linked-code.webp)

<a id="start"></a>

## Primeiros passos

O [Início rápido](../getting-started/quickstart.md) inicia o Loom, e
[adicionar repositórios](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/repositories.md) ativa o índice de código para um repositório. Conecte seu agente de programação com o plugin dele ou por MCP
([conectar um agente de programação](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md)). Salve uma lição que cite, entre crases ou como chamada, uma definição de quatro ou mais caracteres (`` `name()` ``), ou uma qualificada como `` `Reader.run` ``. Os plugins do Claude Code e do Codex marcam o que seus hooks capturam com `repo:<id>`, e o primer da sessão informa o id
([como ele é derivado](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/claude-code.md#configuration)). Uma lição que o próprio agente salva com `loom_memory_set` é vinculada quando carrega essa tag (`tags: ["repo:<id>"]`) ou é escrita no namespace de código do repositório; qualquer cliente pode fazer uma das duas coisas. Por HTTP (ou `agent.call` no SDK Python), uma lição salva no seu próprio namespace só é lida quando a chamada nomeia esse namespace em `lesson_namespaces`. Cada passada de vinculação vincula as escritas com pelo menos 60 segundos de idade e as passadas rodam a cada 60 segundos, então, assim que o trunk estiver indexado, `code.symbol` nessa definição retorna a lição em `lessons` em cerca de dois minutos.

Algumas operações desta página estão marcadas como experimentais na [página de status](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md).
