<!-- i18n: source=docs/getting-started/overview.md blob=6fbe75840c341cccfe02d72afa8d5bbb4dfb7809 model=claude-sonnet-5-5 -->
> Esta página é uma tradução automática da [página em inglês](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/overview.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/overview.md), which is authoritative.

<a id="overview"></a>

# Visão geral

Loom é um servidor que armazena memória, documentos, conversas, um grafo de entidades e um
índice de código para agentes de IA, como linhas no ClickHouse, com
[Postgres](https://github.com/ClickHouse/loom/blob/main/docs/operations/postgres.md) como opção para o estado da memória e do conhecimento em tempo real.
Agentes e pessoas o acessam por MCP, HTTP, pelos SDKs de Python e TypeScript e pelo
painel. Ele é Apache-2.0. Esta página ajuda você a decidir se ele atende às suas necessidades.

<a id="what-it-does"></a>

## O que ele faz

- **Memória que dura.** Os fatos que um agente salva continuam disponíveis em sessões posteriores e em outras
  ferramentas, uma afirmação por chave, com histórico, proveniência e validade. Pode ser a memória de uma pessoa
  ou compartilhada por vários agentes e pessoas.
- **Verificações no momento da escrita.** Um quase-duplicado é mesclado, uma contradição é sinalizada, uma escrita
  contra um fato fixado é recusada.
- **Busca que se abstém.** `memory.search` retorna `abstain=true` com um código de motivo
  quando nada passa por um limiar calibrado para o seu embedder.
- **Auditoria e apagamento.** Toda escrita que altera a memória deixa uma linha de auditoria com hash de conteúdo.
  `erasure.request` termina com um censo de resíduos em todas as tabelas que os caminhos de leitura
  acessam.
- **Um índice de código com o seu histórico.** Busca, símbolos, chamadores, impacto, histórico de PRs com
  comentários de revisão e candidatos a bisect para os seus repositórios, com cada resposta indicando o commit e a camada do índice
  por trás dela. Com uma fonte de tracker conectada, o issue tracker
  também: verificações de duplicatas, vínculos issue↔PR e testes instáveis.

A memória e o índice de código são planos separados em um mesmo servidor. Use um deles isoladamente ou ambos.
A memória precisa de um servidor e de um token de escrita. O índice de código também precisa de um repositório adicionado
(`LOOM_CODE_REPOS_JSON`, ou **Repositories → Add repository** no painel, onde o
[sandbox de jobs de repositório](https://github.com/ClickHouse/loom/blob/main/docs/operations/code-index-operations.md#repository-job-sandbox) está em execução) e
`LOOM_CODE_INGEST_ENABLED=true`. Os [plugins do Claude Code e do Codex](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md)
usam ambos.

<a id="where-namespaces-fit"></a>

## Onde os namespaces se encaixam

- Uma **org** é o tenant.
- Um **namespace** é a unidade de compartilhamento e de acesso dentro dela. Uma implantação geralmente tem
  um por equipe ou projeto, um por bot que precisa permanecer isolado, um `personal-<slug>`
  por pessoa (criado no primeiro uso e usado como padrão para os clientes MCP dessa pessoa) e um `code-<repo>[-<branch>]` por branch indexada.
- Todo membro de um namespace lê todas as linhas dele.
- `agent` registra quem escreveu uma linha; não é uma fronteira.
- `user_id` / `read_scope=own_user` separa os usuários finais dentro do namespace de um bot.
- O acesso é um papel (viewer/writer/admin) em um namespace, carregado por um token ou adicionado por uma
  concessão.

[Identidades e namespaces](../concepts/identities-and-namespaces.md) traz o modelo completo.

<a id="when-loom-fits"></a>

## Quando o Loom é indicado

- Você usa várias ferramentas de IA e quer que elas compartilhem o que você contou a qualquer uma delas, em um namespace pessoal.
- Várias ferramentas e pessoas (Claude Code, Codex, Cursor, um bot de suporte, uma pessoa no painel) precisam da mesma memória.
- Você quer usar SQL sobre o que seus agentes sabem: fatos, traces, custos e linhas de auditoria são tabelas do ClickHouse.
- Você já executa o ClickHouse, ou pode executar. Ele é o único armazenamento obrigatório do Loom;
  o [Postgres](https://github.com/ClickHouse/loom/blob/main/docs/operations/postgres.md) é uma opção para a memória em tempo real e o estado do conhecimento.
- Uma abstenção ou um conflito sinalizado é melhor do que uma resposta errada dada com confiança.
- Você precisa de histórico, proveniência, auditoria ou exclusão verificável.
- Agentes de código precisam de um índice dos seus repositórios.

<a id="when-it-does-not-fit"></a>

## Quando não se encaixa

| Situação                                                              | Por que não                                                           | Considere em vez disso                            |
| --------------------------------------------------------------------- | --------------------------------------------------------------------- | ------------------------------------------------- |
| Um agente, um processo, memória de curta duração                      | Um servidor e um banco de dados são sobrecarga quando nada é compartilhado | Uma biblioteca de memória em processo             |
| Consultas de chave abaixo de um milissegundo em um caminho crítico    | Toda leitura é uma consulta ao banco de dados                         | Um cache na frente do seu próprio armazenamento   |
| Análises do grafo inteiro ou caminhos longos                          | A travessia é uma consulta limitada por salto, com no máximo quatro saltos | Um armazenamento de grafos, alimentado por `loom entity-export` |
| Você precisa de um framework de agentes                               | O Loom não executa o loop do agente, os prompts nem o modelo          | Um framework de agentes, com o Loom como sua memória |
| Uma garantia de conformidade pronta para uso                          | Apagamento e auditoria são primitivas de engenharia                   | Seu processo de conformidade, usando essas primitivas |
| Produção em ClickHouse OSS de nó único sem plano de backup            | Sem replicação nem recuperação para um ponto no tempo                 | ClickHouse Cloud                                  |
| APIs congeladas                                                       | Operações e campos mudam entre versões                                | Fixe uma versão; consulte `ops.capabilities`      |

A validade considera apenas o tempo de validade (`valid_at`, `valid_to`); o que o Loom registrou e quando fica no histórico de versões, não em um segundo eixo de tempo. Algumas partes exigem que um operador as ative: o índice de código, as fontes de trackers, OAuth, a exportação de telemetria e o apagamento no Langfuse e no ClickStack.

<a id="what-each-property-costs"></a>

## O que cada propriedade custa

| Propriedade                       | Mecanismo                                                                                               | Custo                                                                                                                         |
| --------------------------------- | ------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------- |
| Um único motor (autoridade ClickHouse) | Vetor, busca lexical, grafo, filtros, auditoria e exclusão são SQL sobre as mesmas linhas          | Toda leitura é uma consulta. Os leitores pagam `FINAL` em linhas versionadas. As linhas derivadas (entidades, resumos) chegam depois, por meio de workers. |
| Abster-se em vez de uma correspondência fraca | Limites derivados na inicialização a partir da própria distribuição de distância de cosseno do embedder (`LOOM_ADAPTIVE_THRESHOLDS`) | Algumas correspondências fracas, porém corretas, são recusadas; defina `min_similarity` ou `floor_mode` por requisição. |
| Fatos que mudam                   | Veredictos de merge, duplicate, conflict e pinned a cada escrita; `loom.memory_versions` somente para acréscimo | Cada escrita é comparada com os vizinhos. Uma paráfrase pode esperar na fila de revisão até que alguém a resolva.            |
| Ranqueamento explicável           | `memory.search` com `explain=true` detalha a pontuação de cada resultado; os resultados trazem `source_class` e `verified` | Acima de `top_k` 15, onde `explain` vem desativado por padrão, `explain=true` torna cada resultado maior.                     |
| Compartilhamento com controle de acesso | A tupla de identidade em toda linha; tokens e concessões carregam papéis de namespace                | Você emite e rotaciona tokens e concede papéis. Uma suíte de vazamento entre principals protege toda op de leitura no CI.     |
| Exclusão verificável              | Uma saga retomável que termina em um censo de resíduos; backends externos falham de forma fechada       | Uma exclusão é um job de várias etapas. Um backend que não consegue confirmar a exclusão deixa a saga em `failed` para escalonamento manual. |
| Respostas de código com atualidade | Camadas do parser e do compilador (SCIP); as respostas trazem `as_of.commit` e um sinalizador de obsolescência | Um repositório grande leva horas para ser indexado. A camada do compilador exige a toolchain.                                 |

Medir as alegações de qualidade fica a seu cargo. O repositório inclui os harnesses de benchmark e
os resultados brutos por trás dos números publicados ([resultados](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md)). Execute os gates
no seu deployment com provedores reais antes de confiar em um padrão
([execução](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/running.md)).

<a id="decision-checklist"></a>

## Checklist de decisão

Duas ou mais respostas na coluna da direita indicam que o Loom provavelmente não é a ferramenta certa.

| Pergunta                                                                            | Indica o Loom | Indica outra opção           |
| ----------------------------------------------------------------------------------- | ------------- | ---------------------------- |
| Duas ou mais ferramentas ou agentes precisam compartilhar memória?                  | Sim           | Não, um único processo       |
| Você consegue executar o ClickHouse e operar um serviço?                            | Sim           | Não, você quer algo gerenciado |
| Os dados precisam permanecer na sua própria infraestrutura?                         | Sim           | Sem preferência              |
| É melhor uma abstenção ou um conflito sinalizado do que uma resposta errada e confiante? | Sim      | Sempre retornar algo         |
| Você precisa de histórico, proveniência, auditoria ou exclusão verificável?         | Sim           | Não                          |
| Seu caminho crítico é uma busca por chave abaixo de um milissegundo?                | Não           | Sim                          |
| Você precisa de análises do grafo inteiro, e não de alguns saltos?                  | Não           | Sim                          |
| Você consegue absorver mudanças de API entre versões, ou fixar uma versão?          | Sim           | Não                          |
| Você pagará por um embedder e um LLM reais e fará benchmarks com os seus dados?     | Sim           | Não                          |

<a id="prerequisites"></a>

## Pré-requisitos

| Você precisa de                                   | Para                                                                                                                                                                                                                                                           |
| ------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| ClickHouse (OSS para avaliar, Cloud em produção)  | Tudo. Veja [requisitos do ClickHouse](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#clickhouse-requirements).                                                                                                                                                                |
| Postgres 16+ (opcional)                           | Estado ativo de memória e conhecimento sob a [autoridade de memória do Postgres](https://github.com/ClickHouse/loom/blob/main/docs/operations/postgres.md), escolhida no primeiro boot de uma implantação; exige um standby síncrono, a menos que `LOOM_PG_DURABILITY=local` ([requisitos](https://github.com/ClickHouse/loom/blob/main/docs/operations/postgres.md#requirements)). |
| Docker com Compose 2.20 ou posterior              | A stack local publicada (instalador de uma linha; no Windows, [a partir do WSL 2](quickstart.md#fast-path-released-image-no-clone)) e a imagem do servidor.                                                                                                   |
| Python 3.12+                                      | `loom up` exclusivo para contribuidores, desenvolvimento do servidor e trabalho com a CLI de operador.                                                                                                                                                         |
| Um provedor de embeddings                         | Recuperação real: OpenAI, Gemini ou o embedder local em CPU que a stack publicada usa.                                                                                                                                                                         |
| Um provedor de LLM                                | Extração, síntese de `memory.ask`, `code.ask`: OpenAI ou Anthropic.                                                                                                                                                                                            |
| Tokens e namespaces                               | Acesso. Veja [tokens e concessões](https://github.com/ClickHouse/loom/blob/main/docs/guides/tokens-and-grants.md).                                                                                                                                                                                               |
| Para o índice de código                           | Acesso Git aos repositórios, `LOOM_CODE_GITHUB_TOKEN` para o histórico de PRs e tempo para a primeira indexação.                                                                                                                                               |

<a id="next"></a>

## Próximos passos

[Avaliar o Loom](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/evaluate.md) separa um teste de fumaça do produto de uma avaliação real de qualidade. O [Guia de início rápido](quickstart.md) executa a stack local.
A [implantação](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md) o executa para uma equipe. O
[mapa da documentação](https://github.com/ClickHouse/loom/blob/main/docs/index.md) lista todas as páginas em ordem de leitura.
