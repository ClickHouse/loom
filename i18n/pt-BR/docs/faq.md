<!-- i18n: source=docs/faq.md blob=d1e8cdcb41e8e749ecd46d194697f615642a3ca8 model=claude-sonnet-5-5 -->
> Esta página é uma tradução automática da [página em inglês](https://github.com/ClickHouse/loom/blob/main/docs/faq.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/faq.md), which is authoritative.

<a id="frequently-asked-questions"></a>

# Perguntas frequentes

Respostas curtas para as primeiras perguntas que as pessoas fazem. Cada uma tem um link para a página que a explica.

<a id="using-it"></a>

## Usando o Loom

<a id="what-is-the-fastest-way-to-try-loom"></a>

### Qual é a maneira mais rápida de experimentar o Loom?

Se já existe um servidor Loom, instale o plugin do Claude Code ou do Codex e aponte-o para o
servidor: [conectar um agente de código](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md). Para rodar um servidor
em um laptop, `loom up` sobe um em poucos minutos: [início rápido](getting-started/quickstart.md).

<a id="how-do-i-give-claude-code-codex-or-cursor-long-term-memory"></a>

### Como dou memória de longo prazo ao Claude Code, ao Codex ou ao Cursor?

Use os plugins do Claude Code ou do Codex para obter contexto e captura no início da sessão, ou conecte qualquer
outro cliente MCP com `loom install --client <name>` (veja [clientes MCP](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/mcp-clients.md)).
Suba um servidor pelo [início rápido](getting-started/quickstart.md). O Cursor recebe a entrada
MCP e as skills `loom` e `loom-code` (sem hooks para contexto no início da sessão; ainda não
testado no Cursor); a captura de transcrições vem do hook separado `sessionEnd`. Veja
[Conectar um agente de código](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md) e
[Cursor](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/cursor.md).

<a id="why-are-my-search-results-nonsense"></a>

### Por que os resultados da minha busca não fazem sentido?

A instalação roda um embedder falso: o `.env` dela (o de um checkout, ou `~/.loom/.env`) nomeia
`fake-ngram-*`, cujos vetores tornam a recuperação sem sentido. Troque o embedder, o que
reprocessa os embeddings do que está armazenado ([Modelos e chaves](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/models-and-keys.md)); uma instalação nova
da stack lançada já usa um modelo local para embeddings. Um embedder hospedado
recupera melhor em perguntas com muitas paráfrases
([provedores](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#providers)).

<a id="why-does-memorysearch-say-abstain-instead-of-returning-something"></a>

### Por que `memory.search` diz `abstain` em vez de retornar algo?

Nenhum resultado superou o piso de similaridade calibrado para o seu embedder, então o Loom diz que não
sabe em vez de devolver a correspondência fraca mais próxima. O motivo está na resposta. Veja
[recuperação e ranking](https://github.com/ClickHouse/loom/blob/main/docs/concepts/retrieval-and-ranking.md).

<a id="why-was-my-write-flagged-as-a-conflict-or-refused"></a>

### Por que minha escrita foi marcada como conflito ou recusada?

Uma escrita de significado próximo ao de uma memória existente é marcada como `conflicted` e vinculada a ela; uma
escrita próxima a uma memória fixada é recusada com `409 PINNED_CONFLICT`; uma escrita que corresponde a um
padrão da lista de bloqueio recebe `422 BLACKLISTED`. Resolva conflitos na fila de Review do painel ou
com `conflicts.resolve`. Veja [conflitos e recusa](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/conflicts-and-refusal.md).

<a id="can-i-use-loom-just-for-myself"></a>

### Posso usar o Loom só para mim?

Sim. Rode a [stack local](getting-started/quickstart.md), que não tem login e tem um só
namespace, e conecte seus agentes com o [plugin ou `loom install`](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md).
Em um servidor com login, cada pessoa recebe um namespace `personal-<slug>` no qual seus clientes MCP
escrevem por padrão e que ninguém mais lê sem uma concessão. A memória salva pelo
Claude Code fica então disponível para o Codex, o Cursor e o painel.

<a id="can-two-agents-see-each-others-memories"></a>

### Dois agentes podem ver as memórias um do outro?

Todo membro de um namespace pode ler suas linhas. Cada agente escreve em sua própria camada, e um
administrador pode copiar uma memória para a camada compartilhada do namespace com `memory.promote`. Um token pode ler vários
namespaces enquanto escreve em um. Veja [identidades e namespaces](https://github.com/ClickHouse/loom/blob/main/docs/concepts/identities-and-namespaces.md).

<a id="when-should-i-use-knowledge-documents-instead-of-memories"></a>

### Quando devo usar documentos de conhecimento em vez de memórias?

Uma memória é uma afirmação curta que pode ser revisada. Um documento, uma página longa ou qualquer coisa acima de
alguns kilobytes vai para o plano de conhecimento com `knowledge.add`, que o divide em chunks e gera os embeddings.
Veja [memória](https://github.com/ClickHouse/loom/blob/main/docs/concepts/memory.md#what-goes-where) e [documentos de conhecimento](https://github.com/ClickHouse/loom/blob/main/docs/features/knowledge.md).

<a id="how-do-i-make-loom-forget-something"></a>

### Como faço o Loom esquecer algo?

`memory.delete` marca uma memória com tombstone (restaurável durante um período de carência);
`erasure.request` remove tudo o que um usuário ou namespace possui, em todas as tabelas, e
verifica que nada restou. O Loom não apaga nada por conta própria, em um cronograma, a menos que um operador defina uma
janela de retenção. Veja [retenção e apagamento](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/retention-and-erasure.md).

<a id="how-do-i-find-out-why-a-result-ranked-where-it-did"></a>

### Como descubro por que um resultado ficou na posição em que ficou?

Chame `memory.search` com `explain=true`: o `score_components` de cada resultado traz os sinais de
ranking e, após um reranker, seu deslocamento (`rerank_from`, `rerank_pos`, `rerank_delta`) e,
quando o reranker mudou a ordem, sua própria pontuação combinada (`rank_score_pre_rerank`).
`memory.search_trace` pontua apenas a etapa vetorial. Veja [busca e
ask](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/search-and-ask.md).

<a id="can-loom-answer-questions-about-my-code"></a>

### O Loom pode responder perguntas sobre o meu código?

Sim, para repositórios que o servidor indexa: `code.search`, `code.symbol`, `code.callers`,
`code.impact` e mais, e cada resposta informa qual commit e qual camada do índice a sustentam. Veja o
[índice de código](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md) e [adicionar um repositório](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/repositories.md).

<a id="why-doesnt-my-coding-agent-use-the-code-index"></a>

### Por que meu agente de código não usa o índice de código?

O plugin só anexa o índice quando a sessão começa dentro de um checkout git cujo
remote é um repositório indexado que você pode ler. Inicie a sessão a partir do checkout do repositório,
confira se o repositório aparece em **Repositories** no painel (`<server>/repositories`) e veja
[quando o índice de código é usado](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md#when-the-code-index-is-used).

<a id="running-it"></a>

## Executando o Loom

<a id="do-i-need-clickhouse-cloud"></a>

### Preciso do ClickHouse Cloud?

Não. O Loom também funciona com o ClickHouse OSS, e a stack local sobe uma instância dele no Docker. O Cloud é
o caminho pretendido para produção, porque o OSS em um único nó não tem replicação nem recuperação para um ponto no tempo. Veja [implantação](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md).

<a id="can-i-use-anthropic-or-gemini-models"></a>

### Posso usar modelos da Anthropic ou do Gemini?

Sim. O provedor de LLM pode ser OpenAI ou Anthropic, e os embeddings podem vir da OpenAI,
do Gemini ou de um modelo local em CPU. Trocar o modelo de embeddings muda o espaço vetorial, então execute
`loom reembed` depois. Veja [LLM e embeddings](https://github.com/ClickHouse/loom/blob/main/docs/architecture/llm-architecture.md).

<a id="how-do-i-give-someone-access"></a>

### Como dou acesso a alguém?

Gere um token (`loom mint-token`, ou Settings → Tokens no painel) e conceda uma função em um
namespace. Veja [tokens e concessões](https://github.com/ClickHouse/loom/blob/main/docs/guides/tokens-and-grants.md).

<a id="something-is-wrong-where-do-i-start"></a>

### Algo está errado. Por onde começo?

`loom doctor` no servidor, `/loom:loom-status` no Claude Code e `/v1/health`. Depois, a página de
[solução de problemas](https://github.com/ClickHouse/loom/blob/main/docs/operations/troubleshooting.md), que é organizada por sintoma.

<a id="the-project"></a>

## O projeto

<a id="why-is-an-operation-missing-from-my-mcp-tool-list"></a>

### Por que uma operação está faltando na minha lista de ferramentas do MCP?

A listagem do MCP depende do toolset (`?toolset=` ou `LOOM_MCP_TOOLSET`, padrão
`core`), e as ferramentas de código só são listadas com `LOOM_MCP_CODE_TOOLS=true`. Em `all`, uma
op que nem a lista core nem a atomic inclui e que está marcada como `experimental` fica
de fora. Toda op continua podendo ser chamada pelo nome por meio de `loom_op` (somente leituras em
`readonly`), por HTTP e pelos SDKs. Veja [toolsets do MCP](https://github.com/ClickHouse/loom/blob/main/docs/reference/mcp.md#toolsets).

<a id="how-good-is-the-retrieval-and-how-do-you-know"></a>

### Qual é a qualidade da recuperação e como vocês sabem?

O Loom inclui sua suíte de benchmarks com o método, os datasets e os comandos exatos. Só são apresentados resultados
reproduzíveis a partir de arquivos commitados. Veja [testes de desempenho](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/overview.md).

<a id="how-is-loom-different-from-putting-a-vector-database-next-to-my-app"></a>

### Em que o Loom é diferente de colocar um banco de dados vetorial ao lado do meu aplicativo?

Com a autoridade ClickHouse padrão, um único banco de dados ClickHouse guarda as linhas, os vetores,
o índice de texto, o grafo, a auditoria e o estado de apagamento, então nada precisa ser mantido em sincronia. A
[autoridade de memória Postgres](architecture/storage-authority.md#why-two-stores) guarda o estado vivo de
memória e conhecimento no Postgres, e o próprio Loom mantém os dois armazenamentos em sincronia.
Cada escrita é verificada em relação ao que está armazenado. A
[visão geral](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/overview.md) lista o custo de cada propriedade e quando o Loom
não é adequado.
