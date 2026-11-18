<!-- i18n: source=docs/glossary.md blob=8dd4bdacf4b7cf0e994f5fcf1af3353f19f26540 model=claude-sonnet-5-5 -->
> Esta página é uma tradução automática da [página em inglês](https://github.com/ClickHouse/loom/blob/main/docs/glossary.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/glossary.md), which is authoritative.

<a id="glossary"></a>

# Glossário

Os termos do Loom, em ordem alfabética, cada um definido uma única vez. Os identificadores de código estão entre crases.

**Abstain** — `memory.search` retornando `abstain=true` com um código de motivo em vez do
resultado fraco mais próximo, quando o melhor resultado está abaixo do limite de abstenção calibrado para o
embedder.
Ele separa "nada foi armazenado" de "algo não relacionado obteve pontuação 0.3".
Veja [recuperação e ranqueamento](concepts/retrieval-and-ranking.md).

**Actor** — a coluna carimbada pelo servidor que indica quem escreveu uma linha: `human:<subject>` ou
`process:<subject>`, com o tipo extraído do token. Nunca é lida do corpo da requisição.
Veja [identidades e namespaces](concepts/identities-and-namespaces.md).

**Agente** — o terceiro elemento da tupla de identidade: o nome do programa que escreve e
lê (`claude-code`, `support-bot`). Ele registra a autoria; não é um limite de leitura:
todo membro de um namespace lê as linhas de todos os agentes.

**Attestation** — as colunas `verified_*` que registram que uma linha foi verificada. Uma edição
as mantém no lugar e as marca como obsoletas; quem lê decide a confiança comparando o
hash de conteúdo atestado. Veja [histórico e proveniência](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/history-and-provenance.md).

**Audit log** — `loom.audit_log`: uma linha com hash de conteúdo por operação auditada, agrupada
em checkpoints horários encadeados por hash. As linhas carregam hashes, não conteúdo.
Veja [auditoria](https://github.com/ClickHouse/loom/blob/main/docs/features/observability/audit.md).

**Belief revision** — a lente de design para o ciclo de vida da memória: Expand (`memory.set`),
Revise (`memory.update_by_id`), Contract (`memory.delete`), Reinforce (`memory.pin`),
Refuse (`blacklist.add`). Veja [como uma escrita é avaliada](concepts/memory.md#how-a-write-is-judged).

**Blacklist** — substrings ou regexes registradas que recusam escritas correspondentes
(`422 BLACKLISTED`); `sweep=true` também marca com tombstone as correspondências existentes.

**Chunk** — um trecho de um documento de conhecimento ou de um arquivo-fonte, armazenado e com embedding
separadamente para que a recuperação retorne a passagem em vez do documento inteiro.

**Code link** — o vínculo de uma memória com uma definição ou arquivo que ela nomeia, acompanhado por
renomeações e edições. Seu estado é `fresh`, `moved`, `changed`, `unresolved` ou `missing`.
Um nome de código que ainda não está no trunk aguarda até `memory_code_links_pending_days` (14 dias).
Veja [memórias vinculadas a código](concepts/memory.md#memories-linked-to-code).

**Code namespace** — um namespace do tipo `code` que contém um repositório git indexado:
seus chunks, símbolos, arestas, commits e histórico de PRs.
Veja [índice de código](features/code-index/overview.md).

**Commit coverage** — se todas as camadas do índice processaram um determinado commit;
`code.index_status(commit=...)` responde isso, e `code.coverage.watch` notifica quando isso
se torna verdadeiro. Veja [atualidade](features/code-index/freshness.md).

**Camada do compilador** — fatos do índice de código derivados de um indexador de nível de compilador (SCIP) em vez
da camada do parser; arestas de chamada e referências resolvidas por informações de tipo.

**Conflict** — uma escrita semanticamente próxima de uma memória existente (cosseno ≥
`LOOM_CONFLICT_THRESHOLD`). Ambas as linhas são marcadas como `conflicted` e vinculadas entre si; uma pessoa
ou agente as resolve. Veja [conflitos e recusa](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/conflicts-and-refusal.md).

**Consolidation** — a passagem em segundo plano que mescla memórias fragmentadas sobre o mesmo
assunto e registra a linhagem (`memory_belief_sources`, `support_type='derived_from'`).

**Content hash** — `content_sha256` do valor de uma memória. Ele deduplica repetições exatas,
sustenta a pré-condição `if_match_sha256` e ancora as linhas de auditoria.

**Context pack** — `context.pack`: um pacote limitado de memória de trabalho, perfil, memórias
relevantes, procedimentos e decisões, carregado uma vez no início de uma sessão.
Veja [busca unificada](https://github.com/ClickHouse/loom/blob/main/docs/features/unified-search.md#contextpack).

**Painel** — a aplicação web na raiz do servidor, para pessoas: memória, atividade, fontes, fila de
revisão, tarefas, repositórios e configurações. Veja [painel](https://github.com/ClickHouse/loom/blob/main/docs/features/dashboard.md).

**Decision** — um registro de primeira classe de uma escolha (`decision.record`) com justificativa,
alternativas rejeitadas e, depois, seu resultado (`decision.outcome`).
Veja [tarefas e decisões](https://github.com/ClickHouse/loom/blob/main/docs/features/tasks-and-decisions.md).

**Entity edge** — um vínculo ponderado de coocorrência entre duas entidades em
`loom.entity_edges`, com um intervalo de validade; o grafo que `facts.traverse` percorre.
Veja [grafo de entidades](features/entity-graph.md).

**Erasure saga** — a sequência retomável e com falha fechada de etapas que `erasure.request` executa para
remover os dados de um sujeito de todas as tabelas (e dos backends externos configurados), terminando em um
censo de resíduos. Veja [retenção e apagamento](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/retention-and-erasure.md).

**Extraction** — transformar mensagens de conversa em memórias candidatas
(`memory.set_from_messages`), com um LLM e filtros de qualidade.
Veja [extração](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/extraction.md).

**Extractor stamp** — um hash dos tokens de código dos módulos extratores do índice de código. Uma mudança
nele refaz o chunking de todo repositório indexado; comentários e docstrings não contam.
Veja [arquitetura do índice de código](architecture/code-index-architecture.md).

**FINAL** — o modificador do ClickHouse que reduz uma tabela `ReplacingMergeTree` à
versão mais recente por chave no momento da leitura. O Loom lê `loom.memory` com ele.
Veja [ClickHouse](https://github.com/ClickHouse/loom/blob/main/docs/architecture/clickhouse.md).

**Fence** — o token que `task.claim` retorna junto com um lease; as chamadas posteriores a `task.progress`,
`task.heartbeat`, `task.complete` e `task.fail` devem apresentá-lo, para que um reivindicante expirado não possa escrever.

**Grant** — uma linha que dá a um sujeito um papel em um namespace (`grants.*`).
Veja [tokens e grants](https://github.com/ClickHouse/loom/blob/main/docs/guides/tokens-and-grants.md).

**Tupla de identidade** — `(org, namespace, agent, user_id, session_id)` em toda linha. Os
três primeiros são obrigatórios; os dois últimos têm `'-'` como padrão.
Veja [identidades e namespaces](concepts/identities-and-namespaces.md).

**`ignored_args`** — o campo de resposta que nomeia os campos da requisição que o servidor não usou
(desconhecidos ou descontinuados), para que um cliente perceba o desvio sem uma falha.

**Inbox** — uma fila nomeada de tarefas pertencente a um namespace (`task.*`, `inbox.*`).
Veja [tarefas e decisões](https://github.com/ClickHouse/loom/blob/main/docs/features/tasks-and-decisions.md).

**Documento de conhecimento** — um documento adicionado com `knowledge.add`, dividido em chunks e com embedding para
recuperação. Distinto de uma memória: é material de referência, não uma crença.
Veja [documentos de conhecimento](https://github.com/ClickHouse/loom/blob/main/docs/features/knowledge.md).

**Learning** — um candidato aguardando em `loom.learnings_queue` pela decisão de uma pessoa
(`learnings.list`, `learnings.review`): um fato respaldado por vários escritores, promovido à camada
compartilhada; um fato originado de uma conversa, promovido a uma linha de memória do namespace; ou uma regra que um bot afirmou
sobre si mesmo, promovida ao conhecimento do namespace.

**`log_comment`** — a configuração de consulta do ClickHouse que o Loom preenche com a op, o worker
ou o comando de CLI chamador, de modo que `system.query_log` atribua cada instrução.

**`maturity`** — o campo `stable`/`experimental` declarado por uma op. Uma op experimental que
nem o core nem o toolset atomic listam fica fora da listagem `all`; ela
ainda pode ser chamada pelo nome. Veja [toolsets do MCP](https://github.com/ClickHouse/loom/blob/main/docs/reference/mcp.md#toolsets).

**Ferramenta MCP** — a projeção de uma op no Model Context Protocol, chamada `loom_<op>` com
os pontos substituídos por sublinhados (`loom_memory_set`), mais algumas ferramentas sintéticas como
`loom_search` e `loom_op`. Veja [MCP](https://github.com/ClickHouse/loom/blob/main/docs/reference/mcp.md).

**Memória** — uma linha em `loom.memory`: uma chave, um valor, sua tupla de identidade, tipo, tags,
validade e proveniência. Veja [memória](concepts/memory.md).

**Chave de memória** — o identificador legível de uma memória dentro do seu namespace e agente,
por convenção `<scope>/<kind>/<id>`. Uma escrita em uma chave existente a substitui.

**Tipo de memória** — a espécie de uma memória: `semantic`, `episodic`, `procedural`,
`preference`, `profile` e outros; o tipo decide se ela recebe embedding e como é
buscada.

**Mentor hint** — uma curta sugestão de uso que o Loom anexa aos resultados ou serve por meio de
`mentor.brief`, indicando a um agente um plano ou uma op que ele não está usando bem.

**Namespace** — a unidade de armazenamento, compartilhamento e acesso: um workspace nomeado dentro de uma org.
Os tipos são `memory`, `code` e `bench`.
Veja [namespaces e compartilhamento](https://github.com/ClickHouse/loom/blob/main/docs/features/namespaces-and-sharing.md).

**Operação (op)** — uma entrada declarada no registro (`server.operations.registry.ALL_OPERATIONS`),
servida em `POST /v1/<op>`, como uma ferramenta MCP e pelos SDKs.
Veja [referência de operações](https://github.com/ClickHouse/loom/blob/main/docs/reference/operations.md).

**Org** — o tenant de nível mais alto; o primeiro elemento da tupla de identidade.

**Pinned** — uma memória marcada por `memory.pin`. Uma escrita conflitante com uma memória fixada
é rejeitada com `409 PINNED_CONFLICT`.

**Plane** — uma área do produto com suas próprias ops e tabelas: memória, conhecimento, conversa,
grafo, código, tracker, runs e tarefas, auditoria.

**Procedure** — um passo a passo reutilizável (`procedure.add` / `match`), armazenado como uma memória do tipo
`procedural`. Veja [procedimentos e learnings](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/procedures-and-learnings.md).

**Profile** — o contexto permanente de um agente ou usuário (`profile.*`), carregado primeiro por
`context.pack`.

**Projection** — uma linha derivada de uma escrita (um embedding, uma aresta de grafo, um resumo).
Uma projeção que falha é registrada e tentada novamente (`ops.projection_failures.*`).

**Read scope** — `read_scope`, que limita uma leitura dentro de um namespace: `namespace`
(todas as linhas, o padrão), `own_user` ou `own_session`. A claim de um token é um teto que uma
requisição pode reduzir, mas não ampliar.
Veja [identidades e namespaces](concepts/identities-and-namespaces.md#read-scope-inside-a-namespace).

**Record** — uma memória estruturada validada contra um schema de record registrado
(`memory_schema.*`). Não confundir com um record do tracker.

**ReplacingMergeTree (RMT)** — o engine do ClickHouse por trás da maioria das tabelas do Loom: uma nova
linha de versão substitui a antiga no merge ou na leitura com `FINAL`, de modo que todo caminho de escrita precisa
carregar todas as colunas adiante. Veja [ClickHouse](https://github.com/ClickHouse/loom/blob/main/docs/architecture/clickhouse.md).

**Residue census** — a última etapa de uma erasure saga: uma contagem de linhas vivas no escopo em
todas as tabelas que os caminhos de leitura tocam. Qualquer resíduo faz a saga falhar.

**Papel** — `viewer`, `writer`, `admin`, `org_admin`, `platform_admin`, em ordem crescente de
privilégio. Cada op declara o mínimo de que precisa.

**Run** — uma execução de agente, delimitada por `run.begin` e `run.complete`, registrando o que
ela recuperou e escreveu. Veja [execuções](https://github.com/ClickHouse/loom/blob/main/docs/features/runs.md).

**Sessão** — uma conversa ou período de trabalho, o quinto elemento da tupla de identidade;
um **session brief** (`session.brief_*`) é o resumo salvo dela para retomar depois.

**Camada compartilhada** — uma linha canônica por `(org, namespace, memory_key)`, promovida a partir
das linhas que os agentes escrevem (`memory.promote` / `memory.demote`, tabela `loom.shared`).
"Privada" para a outra camada significa não promovida, não oculta.

**Source class** — a proveniência de uma linha carimbada pelo servidor: `asserted`, `extracted`,
`observed`, `derived`, `imported` ou `external`. Uma requisição só pode rebaixá-la.

**Camada** — qual extrator produziu um fato do índice de código (parser ou compilador); as respostas dizem
qual camada as sustenta.

**Tombstone** — uma linha de versão mais alta que marca uma memória como excluída. `memory.restore` pode desfazer
isso dentro do período de carência; `memory.delete` não apaga nada fisicamente.

**Toolset** — a listagem MCP que um cliente solicita (`core`, `coding`, `atomic`, `tasks`,
`tracker`, `readonly`, `all`); ela muda o que é listado, não o que o token pode chamar.

**Tracker** — o plane que lê os registros de um issue tracker (issues, PRs, execuções de CI)
de uma fonte configurada para verificações de duplicatas, bisect e histórico de testes.
Veja [rastreador](https://github.com/ClickHouse/loom/blob/main/docs/features/tracker/overview.md).

**Trust mode** — a política de aceitação que o dono de um inbox define para quem posta; o Loom a armazena
e nunca a altera por conta própria.

**Valid time** — `valid_at` / `valid_to` nas memórias (`valid_from` / `valid_to` nas
entity edges): quando um fato era verdadeiro no mundo. O Loom registra apenas o valid time; não é bitemporal.

**Versão** — a coluna monotonicamente crescente da qual um `ReplacingMergeTree` mantém o
maior valor; toda escrita de uma memória (inclusive uma exclusão) é uma nova versão.

**Worker** — um loop de segundo plano supervisionado, registrado como uma linha em
`server/workers/registry.py` (`_WORKERS`). Veja [processos em segundo plano](https://github.com/ClickHouse/loom/blob/main/docs/reference/workers.md).

**Memória de trabalho** — blocos de rascunho (`working_memory.*`) para a tarefa atual: sem embedding,
sem ranqueamento, carregados antes da memória durável por `context.pack`.
Veja [memória de trabalho e perfis](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/working-memory-and-profiles.md).
