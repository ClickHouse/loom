<!-- i18n: source=docs/concepts/identities-and-namespaces.md blob=05f85261bcf9106427aff88cb70b9f1b5e978a92 model=claude-sonnet-5-5 -->
> Esta página é uma tradução automática da [página em inglês](https://github.com/ClickHouse/loom/blob/main/docs/concepts/identities-and-namespaces.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/concepts/identities-and-namespaces.md), which is authoritative.

<a id="identities-and-namespaces"></a>

# Identidades e namespaces

O Loom responde a duas perguntas em cada chamada: de quem é esta linha (a tupla de identidade, mais duas colunas que o servidor preenche) e se o chamador pode acessá-la (o token, as concessões do namespace e sua classificação). Esta página explica o modelo. A criação e a revogação de tokens estão em [Tokens e concessões](https://github.com/ClickHouse/loom/blob/main/docs/guides/tokens-and-grants.md); a aplicação das regras está em [Segurança](https://github.com/ClickHouse/loom/blob/main/docs/architecture/security.md).

<a id="where-namespaces-fit"></a>

## Onde os namespaces se encaixam

- Uma **org** é o tenant.
- Um **namespace** é a unidade de compartilhamento e de acesso dentro dela. Uma implantação geralmente tem
  um por equipe ou projeto, um por bot que precisa ficar isolado, um `personal-<slug>`
  por pessoa e um `code-<repo>[-<branch>]` por branch indexada.
- Todo membro de um namespace lê todas as linhas dele.
- `agent` registra quem escreveu uma linha; não é uma fronteira.
- `user_id` / `read_scope=own_user` separa os usuários finais dentro do namespace de um bot.
- O acesso é um papel (viewer/writer/admin) em um namespace, transportado por um token ou adicionado por um
  grant.

<a id="the-identity-tuple"></a>

## A tupla de identidade

Toda linha carrega cinco colunas de identidade:

```
(org, namespace, agent, user_id, session_id)
```

| Parte        | Significado                                                                                          | Padrão                                                                                                |
| ------------ | ---------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------- |
| `org`        | O tenant. Nada cruza o limite de uma org, exceto por meio de um token de operador com curinga.       | (obrigatório; o modo de usuário único o preenche, veja abaixo)                                        |
| `namespace`  | A unidade de isolamento e compartilhamento dentro de uma org. Toda leitura é restrita a `(org, namespace)`. | (obrigatório; o modo de usuário único o preenche, veja abaixo)                                |
| `agent`      | Quem escreveu a linha (`support-bot`, `claude-code`). Autoria, não um limite de leitura.             | (obrigatório; o HTTP de usuário único o preenche em algumas ops, [referência HTTP](https://github.com/ClickHouse/loom/blob/main/docs/reference/http-api.md#request)) |
| `user_id`    | O usuário final a quem a linha se refere.                                                            | `-`                                                                                                   |
| `session_id` | Uma conversa ou uma execução de uma conversa.                                                        | `-`                                                                                                   |

Muitas operações também recebem `run_id` (padrão `-`) para restringir uma escrita ou leitura
a uma execução de tarefa. O token fixa a org e os namespaces que um chamador pode acessar; o
restante da tupla viaja no corpo de cada requisição. Os SDKs vinculam `(org, namespace, agent)`
quando o cliente é construído e passam `user_id`, `session_id` e `run_id` a cada chamada, com `-`
para cada um que a chamada não informa. Quando um desses campos filtra uma leitura (buscas,
asks, o pacote de inicialização, listagens de conversas e execuções, a consulta de chave de
`memory.update`), `-` significa sem filtro: uma busca que não informa nenhuma execução lê as
linhas de todas as execuções. Quando o campo é o endereço da linha (`memory.get`, `memory.list`,
`memory.stack`, memória de trabalho, escritas de perfil, mensagens salvas), `-` é a linha
escrita sem usuário, sessão ou execução.

**As leituras abrangem todos os autores.** Uma leitura tem como padrão `agent="*"`: todas as
linhas do namespace, não importa quem as escreveu. Informar um `agent` apenas restringe.
`user_id` e `session_id` em uma leitura filtram as linhas escritas sob eles.

**Por que a tupla vem primeiro nas tabelas.** As chaves de ordenação de `loom.memory` e
`loom.memory_embeddings` começam com `(org, namespace, agent, user_id, session_id)`, então uma
leitura restrita a um namespace é um intervalo de chave primária no ClickHouse, e não uma
varredura, e informar um agente a restringe ainda mais. Uma leitura entre agentes, o padrão,
filtra `user_id` e `session_id` dentro do intervalo do namespace: a chave só pula granules por
eles até onde a coluna de agente permite, e nenhum índice de salto os cobre. Qualquer leitura
restrita ainda lê pelo menos um granule de cada parte que o seu intervalo abrange
([memória em escala](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#memory-at-scale)).

<a id="what-the-server-stamps"></a>

### O que o servidor carimba

Mais duas colunas são definidas pelo servidor em toda escrita e nunca são lidas do corpo da
requisição, exceto que um chamador pode rebaixar `source_class`
(`server/memory/app/provenance.py` para `actor`, `server/memory/domain/provenance.py` para
`source_class`):

| Coluna         | Valores                                                                | Definido por                                                                                             |
| -------------- | ---------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------- |
| `actor`        | `human:<subject>` ou `process:<subject>` (uma conta de serviço)        | O token verificado (`writer_actor`). Um job na fila ou uma importação pela CLI escreve como o principal que o iniciou. |
| `source_class` | `asserted`, `extracted`, `observed`, `derived`, `imported`, `external` | A operação e a convenção de `source_trace` (`source_class_for`).                                         |

Uma requisição pode declarar apenas um rebaixamento de `source_class`: `external` (conteúdo de
uma ferramenta ou página), `imported` ou `derived`. Pedir qualquer outro valor resulta em 422. O
motivo é que a proveniência alimenta o ranqueamento e a escolha de evidências de `memory.ask`:
se um chamador pudesse rotular texto coletado como `asserted`, o rótulo não significaria nada.
`agent` é fornecido pelo chamador porque nomeia um papel no sistema, enquanto `actor` é o
principal autenticado por trás dele.

<a id="orgs-and-namespaces"></a>

## Orgs e namespaces

Uma **org** é um tenant; um **namespace** é onde as linhas ficam e a unidade sobre a qual o acesso é
concedido. Um namespace passa a existir quando é registrado (`namespace.register`,
que torna o chamador seu `admin`), reivindicado no cadastro, provisionado pelo servidor (o
namespace de usuário único e um namespace `personal-<slug>` para cada pessoa autenticada) ou
criado para um repositório indexado. Sua linha de diretório em `loom.namespaces` guarda a
configuração: descrição, tipo, visibilidade, política de entrada, política de conflito e de propostas,
limite de custo, retenção e padrões de recuperação.

| Tipo de namespace | Contém                                            | Lido por                                                                     |
| ----------------- | ------------------------------------------------- | ---------------------------------------------------------------------------- |
| `memory`          | Fatos, documentos, conversas: o tipo padrão       | operações de memória, conhecimento e conversa, e fan-outs                    |
| `code`            | Um repositório indexado e um branch               | as operações `code.*`, endereçadas por nome; `loom_search` sob um escopo de código |
| `bench`           | Um corpus de benchmark                            | benchmarks; seu tráfego é classificado como `bench`                          |

Uma busca que faz fan-out por todos os namespaces de um chamador abrange apenas o tipo `memory`
(`LOOM_MEMORY_SEARCH_FANOUT_KINDS`): varrer todos os índices de código atrás de um fato pessoal custa
uma instrução por índice e não encontra nada.

**Por que namespaces, e não armazenamentos por agente.** O caso comum são vários agentes e pessoas
que devem ver os mesmos fatos: a janela de deploy de uma equipe, o histórico de um cliente. Tornar o
namespace a fronteira de leitura faz com que o compartilhamento seja o padrão dentro dele e inexistente fora dele,
e uma pessoa ou bot recebe acesso pela concessão de um papel em um namespace, não pela cópia de
linhas.

<a id="visibility-joining-and-restriction"></a>

### Visibilidade, entrada e restrição

| Configuração        | Valores                                         | Efeito                                                                                                                                    |
| ------------------- | ----------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------- |
| `visibility`        | `private` (padrão), `public`                    | Um namespace público é listado em `namespace.browse` e pode receber entradas. A visibilidade não concede leitura por si só.               |
| `join_policy`       | `open`, `approval`, `closed`, `inherit` (não definido) | O que `namespace.join` faz. `inherit` resolve para `open` em um namespace público que não exige aprovação, e para `approval` caso contrário. |
| `requires_approval` | `true`, `false`                                 | Lido apenas sob `inherit`: toda entrada passa a aguardar um admin (`grants.approve`).                                                     |
| `classification`    | `standard` (padrão), `restricted`               | Desativa todos os padrões de ampliação de acesso (abaixo).                                                                                |

Em um namespace `open`, a entrada como `viewer` fica ativa imediatamente e a entrada como `writer` ou `admin`
fica pendente até que um admin aprove; em `approval`, toda entrada fica pendente; `closed`
recusa entradas.

Um namespace **restricted** (`classification=restricted` em sua linha de diretório, ou em sua
entrada em `LOOM_CODE_REPOS_JSON`) admite uma chamada somente quando o token pertence à org do
namespace, nomeia o namespace literalmente (não por meio de `*`), tem ali um papel que alcança o papel
da operação, e o próprio subject do token tem uma concessão direta ativa. Nenhuma regra
de leitura pela org, visibilidade pública, curinga, concessão herdada de conta de serviço,
`org_admin` ou `platform_admin` a contorna, e uma recusa não revela se o
namespace existe. Fan-outs, pacotes de contexto e rollups ignoram namespaces restritos que o
chamador não pode ler, e cada requisição admitida é auditada. A configuração passo a passo está em
[Tokens e concessões](https://github.com/ClickHouse/loom/blob/main/docs/guides/tokens-and-grants.md#restricted-namespaces-classificationrestricted).

<a id="roles"></a>

## Papéis

Os papéis são ordenados, e um papel mais alto satisfaz todas as verificações de um papel mais baixo. Cada
operação declara o papel mínimo de que precisa (`GET /v1/operations` o lista).

| Papel            | Concede                                                                                                             | Como é obtido                                                                                                                                                                                                         |
| ---------------- | ------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `viewer`         | Leitura.                                                                                                            | Claim do token ou concessão no namespace.                                                                                                                                                                             |
| `writer`         | Leitura, e escrita em memória, conhecimento e nos demais planos de dados.                                           | Claim do token ou concessão no namespace.                                                                                                                                                                             |
| `admin`          | Administrar um namespace: concessões, configuração, política, restaurações, promoção para a camada compartilhada.   | Claim do token ou concessão no namespace; registrar um namespace torna você o admin dele.                                                                                                                             |
| `org_admin`      | Visões de toda a organização (por exemplo, custo sem um namespace).                                                 | Uma barreira reconhecida, sem caminho de atribuição dentro do produto.                                                                                                                                                |
| `platform_admin` | Visões de operador entre organizações.                                                                              | Conferido no login aos handles e e-mails verificados em `LOOM_PLATFORM_ADMINS`, e aos tokens de um bot registrado com `--platform-admin`; emitido apenas para uma conta de serviço, com data de expiração.            |

As concessões carregam apenas `viewer`, `writer` ou `admin`, e o mesmo vale para os tokens que o Loom emite, exceto no caso de um
`platform_admin`: o poder em toda a organização é uma decisão do operador, na configuração ou no registro do bot, e não algo que um token possa levar consigo. Os namespaces de código têm leitores
(`viewer`) e mantenedores (`admin`), e não têm `writer`.

<a id="who-may-call-what"></a>

## Quem pode chamar o quê

Uma chamada em `(org, namespace)` é permitida quando o namespace não é restrito (ou passa
pelas verificações de restrição) e quando o papel do próprio token para esse namespace
alcança o papel da operação, ou quando uma concessão ativa o alcança.

- **O token** (`sub`, `org`, `namespaces`, `roles`, `ns_roles` e `ns_write` opcionais)
  informa quais namespaces o chamador lê, em ordem de prioridade, e com qual papel. Um
  token **com escopo** (`ns_roles`, papéis por namespace) é a fonte de autoridade
  exatamente para os namespaces que lista. Um token **simples** aplica seus `roles` a todo
  namespace que ele nomeia. `ns_write` restringe as escritas a um subconjunto, de modo que
  um único token pode ler um namespace de equipe e escrever apenas no seu próprio:

  ```
  namespaces = ["team-shared", "agent-private"]
  ns_write   = ["agent-private"]
  ```

- **Concessões** (`loom.namespace_grants`, uma linha por `(org, namespace, subject)`)
  adicionam um papel em um namespace acima do que o token carrega.
  - **Uma reivindicação não vai além da concessão.** Quando o sujeito tem uma linha de
    concessão em um namespace que o token nomeia, em qualquer status, a reivindicação não
    vai além dessa concessão: uma concessão revogada ou reduzida (`grants.revoke`,
    `grants.create` com `replace=true`, `namespace.leave`, uma reivindicação liberada, uma
    restauração) encerra a reivindicação de todos os tokens mais antigos ali, e uma
    solicitação pendente ou rejeitada a limita a nenhum acesso. Um token com escopo mantém
    seu próprio papel ali, limitado pela concessão ativa.
  - **O que não é afetado.** Um namespace em que o sujeito nunca teve uma concessão mantém
    sua reivindicação; revogue o token para removê-la.
  - **Quando se aplica.** O processo que alterou a concessão a aplica imediatamente; todos
    os outros, em até `LOOM_ACL_WATERMARK_POLL_SECONDS` (padrão 1 s), neste servidor ou em
    outro.
- **Contas de serviço herdam.** Uma conta de serviço sem concessão própria em um namespace
  usa a concessão de seu criador ali, de modo que um token gerado pelo painel acompanha a
  pessoa que o criou, exceto em um namespace restrito.
- **Operações endereçadas por um id válido em toda a organização**, como
  `memory.get_by_id`, autorizam o chamador no escopo que contém a linha. Quando o chamador
  não tem o papel da operação no namespace da linha, elas respondem como para uma linha
  inexistente, de modo que uma recusa nunca confirma que o id existe
  ([operações com escopo de linha](https://github.com/ClickHouse/loom/blob/main/docs/architecture/security.md#row-scoped-operations)).

Por que tokens mais concessões: o token é verificado localmente a cada chamada, sem
consulta, o que mantém o caminho crítico barato; as concessões permitem que um admin dê e
retire acesso sem reemitir tokens. As revogações são compartilhadas por meio do ClickHouse
e chegam a todas as réplicas dentro do intervalo de cache de revogação.

<a id="reading-several-namespaces"></a>

## Lendo vários namespaces

Uma busca pode ler vários namespaces de uma vez: `namespace_scope` é `this` (o namespace
endereçado, o padrão) ou `all_mine` (o namespace endereçado mais os do próprio chamador).
A ferramenta MCP `loom_search` e o `context.pack` usam o mesmo fan-out. A busca de memória lista os
namespaces no escopo em `namespaces_searched` (uma busca em camadas pode parar antes do último
deles) e os que o limite deixou de fora em `namespaces_skipped`.

Os namespaces do próprio chamador dependem do token:

| Token                                                       | Namespaces que `all_mine` adiciona                                                             |
| ----------------------------------------------------------- | ---------------------------------------------------------------------------------------------- |
| Flat (a maioria dos tokens; `loom mint-token`, self-signup)          | Os namespaces que o token nomeia, mais todo namespace em que o subject tem um grant              |
| Com escopo (`ns_roles`; um token para _Only what you choose_)     | Exatamente os namespaces que o token lista, nessa ordem; grants adicionados depois não entram          |
| Delegado com `*` (um token para _Everything you belong to_) | Os grants ativos do criador, de modo que um namespace em que a pessoa entrar depois também é pesquisado               |
| Delegado que nomeia namespaces                                 | Os que ele nomeia e nos quais o criador ainda tem um grant                                        |
| Nenhum, autenticação desligada (`single-user`)                              | Os namespaces em que o usuário único tem um grant: o padrão e todos os que ele registrou |

Em seguida, a lista é reduzida:

- **Tipos.** Só são varridos os namespaces dos tipos em `LOOM_MEMORY_SEARCH_FANOUT_KINDS` (padrão
  `memory`). Os namespaces de código e de benchmark são lidos pelo nome ou com suas próprias
  ferramentas. A exceção é o MCP `loom_search` sob um escopo de código: ele também lê o namespace
  de código do trunk do repositório no escopo, para seus documentos e decisões, quando o chamador pode
  lê-lo ([busca unificada](https://github.com/ClickHouse/loom/blob/main/docs/features/unified-search.md#which-namespaces-it-reads)).
- **Namespaces restritos** só permanecem quando o chamador passa na verificação de restrição.
- **O limite.** Além do namespace endereçado, no máximo
  `LOOM_MEMORY_SEARCH_FANOUT_MAX_NAMESPACES` (padrão 32) são lidos, na ordem da lista do
  chamador: a ordem de um token com escopo, ou a lista nomeada de um token delegado; caso contrário, por nome. Um
  token com escopo mantém os primeiros; para os demais, o corte descarta os namespaces públicos
  antes dos privados. A busca de memória, de conhecimento e de conversas nomeia o restante em
  `namespaces_skipped` (inclusive em cada página de uma busca de memória paginada), e o `loom_search` informa
  isso em seu texto; endereçe um deles pelo nome para lê-lo.

O namespace endereçado é sempre pesquisado, e primeiro: o fan-out apenas amplia uma busca.
A busca de memória então lê em camadas. Para um token com escopo, os outros namespaces vêm em
sua ordem, e a busca para no primeiro com uma correspondência confiável. Para os demais, o
namespace endereçado e os privados são lidos primeiro, e os públicos só quando
essa primeira passagem fica aquém.

Alguns namespaces que um chamador pode ler não são dele e nunca são varridos: os alcançados
por uma claim `*` ou por um papel de org ou de plataforma, os namespaces públicos em que ele não entrou, e
os namespaces que ele criou ao escrever neles (eles recebem uma linha de dono, mas nenhum grant, de modo que um
harness que escreve um namespace por tarefa não amplia toda busca seguinte). Endereçe-os
pelo nome. Uma busca abrange uma org, a do próprio token.

<a id="read-scope-inside-a-namespace"></a>

## Escopo de leitura dentro de um namespace

A associação (membership) é o limite de leitura, o que é adequado para a memória compartilhada de uma equipe e inadequado para um único agente que atende muitos usuários finais por meio de um único token: um prompt de um usuário poderia trazer para a resposta as memórias de outro usuário. `read_scope` limita uma leitura dentro do namespace:

| `read_scope`          | Uma leitura retorna                                           |
| --------------------- | ------------------------------------------------------------- |
| `namespace` (padrão)  | todas as linhas do namespace                                  |
| `own_user`            | linhas gravadas sob o `user_id` que a requisição informa      |
| `own_session`         | linhas gravadas sob o `session_id` que a requisição informa   |

`memory.search`, `memory.search_trace`, `memory.ask`, `memory.recall`, `context.pack`,
`namespace.inventory`, `profile.render` e `profile.structured` aceitam esse parâmetro; `profile.structured` recusa
`own_session`, já que o perfil de um usuário abrange as sessões dele. Um token pode carregar uma
claim `read_scope` (e um `read_user_id` fixado) que funciona como teto: uma requisição pode restringi-lo, mas nunca ampliá-lo, e
uma leitura com escopo sem o `user_id` ou o `session_id` ao qual está vinculada é recusada com
`IDENTITY_INCOMPLETE` em vez de ser ampliada silenciosamente. Emita tokens `own_user` para
agentes com vários usuários.

Os dois transportes resolvem o escopo uma única vez, quando admitem a chamada, e toda leitura de
linhas de memória nessa chamada o carrega, seja qual for a op que fez a leitura: `memory.get`,
`get_by_id` (o id de outro usuário responde `found=false`), `list`, `bulk_get`, `history`,
`timeline`, `related`, `evidence`, `conflicts.list`, `memory.proposals`, resumos de sessão (session briefs),
digests, procedimentos, memória de trabalho e as etapas de `loom_search`. Sob um escopo mais restrito
que `namespace`:

- `user_id="*"` é recusado com `AUTH_INSUFFICIENT`, e o mesmo vale para `session_id="*"` sob
  `own_session`.
- Um token fixado com `read_user_id` age somente como esse usuário, inclusive nas gravações: um corpo que nomeia
  outro `user_id` é recusado com `AUTH_INSUFFICIENT` em toda op, e um corpo que não nomeia
  nenhum (ou `-`) é executado como o usuário fixado.
- A resposta de uma gravação não nomeia nenhuma linha fora do escopo: uma mesclagem com, uma duplicata de ou um
  conflito fixado com a linha de outro usuário informa o veredito sem o id, o hash ou a chave dessa linha,
  e `conflicts.list` remove esses pares de `conflict_with`. Uma linha fixada
  ainda bloqueia uma gravação próxima de qualquer usuário (uma invariante válida em todo o namespace): uma linha fixada de outro usuário
  dentro da distância de conflito recusa a gravação, sem nomeá-la, por mais que linhas do próprio
  autor da gravação estejam mais próximas. A verificação do escritor restrito lê as linhas fixadas do namespace
  (com o tipo tomado da linha de memória) junto com sua própria leitura com escopo, então custa no
  máximo cerca de o dobro do custo de um escritor com escopo de todo o namespace. Um
  `memory.set_from_messages` adiado (`defer=true`, ou `extraction_defer_agents`) armazena o escopo
  da requisição junto com ele e é reexecutado sob esse escopo. `run.begin` com o
  `run_id` de outro usuário inicia uma nova execução sob um id novo, e `run.complete` nele
  responde `NOT_FOUND`. Um begin com id fornecido pelo chamador o reivindica no ClickHouse Keeper
  (`loom.agent_run_claims`) e libera a reivindicação quando a linha da execução é gravada, de modo que, entre
  begins concorrentes de dois usuários no mesmo id, exatamente um o mantém. Um begin que falha antes de gravar
  sua linha mantém a reivindicação, então nenhum begin de outro usuário pode tomar o id. Enquanto o Keeper falha,
  um begin apenas verifica a linha da execução; nesse caso, um token restrito mantém um id fornecido somente para reabrir
  sua própria execução existente, e uma nova recebe um id gerado pelo servidor, portanto um cliente que cria
  o id por conta própria deve usar o `run_id` retornado.
- Os resumos de sessão (session briefs) são mantidos por usuário (por sessão sob `own_session`): dois usuários que salvam
  o mesmo `brief_key` obtêm dois resumos, cada um vê apenas o seu, e um token
  de todo o namespace os lista sob chaves com o prefixo `~<user>~` (ou `~<user>~<session>~`).
- `code.search` desempata com os arquivos recentes do próprio usuário do escopo, seja qual for o
  `user_id` que o corpo nomeie.
- A etapa de termos de `memory.search` serve uma definição governada sob qualquer escopo de leitura: um administrador
  a publicou para todos os leitores do namespace ([definições governadas na
  busca](#governed-definitions-in-search)).
- Uma etapa de `context.pack` que o escopo não consegue limitar (conversas, que não carregam usuário, e
  o manifesto de execuções, que lê todas as sessões de um usuário) não responde nada.
- `namespace.inventory` conta memórias e execuções, além de agentes, autores e sujeitos (subjects) presentes
  nas suas linhas de memória, dentro do escopo; agentes, autores e sujeitos são populações de identidades observadas nessas linhas, e os sujeitos são as identidades dos usuários finais (vindas do user_id), não os assuntos tratados na memória. Documentos e membros são contados em todo o namespace (como `knowledge.*`
  e `grants.list` os leem), e registros de listas (um por chave, de quem gravou por último)
  e integrações (`webhook.list` é recusado) aparecem como não contados (`complete: false`).
- Um padrão de blacklist recusa as gravações de todos os usuários, mas não registra nenhum usuário (seu
  `created_by` é o subject do token, que todos os tokens por usuário de um mesmo backend
  compartilham), então toda op `blacklist.*` é recusada com `AUTH_INSUFFICIENT`: os padrões
  são adicionados, listados, removidos e varridos com um token de todo o namespace. Um padrão ainda
  recusa a gravação de um token restrito, e esse 422 não nomeia id de padrão nem motivo.
- `policy.decisions` lista apenas as decisões do próprio chamador. `memory.unlock` e
  `memory.unpin` (admin) alcançam apenas as linhas do próprio chamador, como `memory.lock`.
- Uma op que não consegue limitar sua resposta a um usuário (o grafo de entidades `facts.*`,
  `conversation.*`, exportações do namespace, ops de administração e curadoria,
  `ops.projection_failures.*`, `erasure.*`, `audit.list`, `cost.agent_summary`,
  `agents.connected`) é recusada com
  `AUTH_INSUFFICIENT`, e o mesmo vale para uma gravação em uma tabela cuja chave não tem usuário, em que
  sobrescreveria a linha de outro usuário: `memory.record.write`, `decision.record`,
  `procedure.record_run` e `context.offload`. `memory.set_from_messages` ignora sua
  projeção `record_schema` em registros e informa isso em `degraded`. Um token cujo
  escopo não se resolve (um teto `own_session` e um corpo que não nomeia sessão, ou um
  token `own_user` não fixado e um corpo que não nomeia usuário) é recusado em toda leitura que ele
  delimita e em toda gravação cujo corpo carrega um usuário ou uma sessão (e em `cost.summary`),
  em vez de ser executado como o usuário `-`. Sob `own_session`, ops sem campo de sessão
  (`cost.summary`, `memory.outcome`) são, portanto, sempre recusadas. As ops de índice de código
  (`code.*`) carregam um usuário apenas para atribuição e são admitidas; uma leitura de memória dentro
  delas é recusada. Regras de todo o namespace (`policy.set`, `policy.delete`,
  `memory_schema.register` / `propose` / `approve`, `blacklist.*`) exigem um token
  com escopo de namespace, e o mesmo vale para `policy.check`, que avalia as regras do chamador
  sobre as chamadas de todos os usuários. A lista está em
  `server/identity/domain/read_scope.py` (`SCOPED_OPS`, `NARROW_SAFE_OPS`), e uma op
  que ela não nomeia é recusada.

<a id="agent-written-rows-and-the-shared-layer"></a>

## Linhas escritas por agentes e a camada compartilhada

Dentro de um namespace, as linhas de `loom.memory` são identificadas pela tupla completa: o mesmo
`memory_key` escrito por dois agentes, ou para dois usuários, são duas linhas. Essa é a camada por agente
("privada"), e é o que `memory.search` lê por padrão (`scope="private"`).
"Privada" aqui significa _não promovida_, não _oculta_: todo leitor do namespace pode ler
essas linhas, a menos que `read_scope` as restrinja.

A **camada compartilhada**, `loom.shared`, guarda uma linha por `(org, namespace, memory_key)`: uma
versão canônica, pertencente ao namespace, de um fato que deixou de pertencer ao agente que
o aprendeu. `memory.promote` copia uma linha para ela, com uma justificativa e um aprovador
opcional, e `memory.demote` a retira (a resposta informa se isso ocorreu
dentro da janela `reversible_for_days` da promoção, padrão 7, até 90). Uma busca com
`scope="shared"` lê essa camada, e `memory.lineage` rastreia uma linha compartilhada até
sua promoção. `memory.promote` e `memory.demote` exigem `admin`. `memory.cross_promote`
copia uma linha para outro namespace, com proveniência, para um chamador que seja admin em ambos.

Uma `definition` só é promovida:

- A partir de sua linha ativa de todo o namespace (agente, usuário e sessão `'-'`, `active`, não retirada
  por uma substituição e não além de seu `expires_at` ou `valid_to`) sob uma chave `definition/`,
  para essa mesma chave; uma linha de outro tipo nunca é promovida sob uma chave `definition/`.
  Caso contrário, 422 `DEFINITION_PROMOTION_REFUSED`.
- Com o hash que ela aprova (`approved_sha256`, o `content_sha256` da linha): é recusada
  sem ele (422 `VALIDATION_FAILED`) e com 409 `PRECONDITION_FAILED` quando o texto
  não o tem mais.
- Com um valor que seja um objeto JSON contendo um `term` do tipo string não vazia e, opcionalmente,
  uma lista de strings `aliases`; caso contrário, 422 `VALIDATION_FAILED`.
- Sem o rótulo `medium` e sem ser lida como `medium` ou `high` (422
  `SENSITIVE_DEFINITION_REFUSED`; um rótulo `high` responde
  `HIGH_SENSITIVITY_PROMOTION_REFUSED` primeiro, como para qualquer linha).
- Dentro de `memory_shared_definitions_max`: uma chave nova no limite, ou qualquer promoção
  enquanto o namespace estiver acima dele, é recusada (409 `DEFINITION_CEILING`).
- Sob um nome que nenhuma outra definição compartilhada ativa tenha como termo ou alias (409
  `DEFINITION_NAME_TAKEN`, indicando as chaves e os nomes), com os nomes comparados após normalização: caracteres de formato removidos,
  NFKC, espaços em branco colapsados, case-fold e NFKC de novo.

As promoções de definição de um namespace, com as verificações de limite e de nome, e seus
rebaixamentos de definição rodam um de cada vez, e o último a rodar decide se a definição é
compartilhada (503 `OVERLOADED` quando outro mantém o namespace por mais de 10 s de espera). Rebaixar uma
definição devolve seu nome e seu lugar. O ator do token é seu `promoted_by`:
`approver` e `promoted_from_candidate_id` são ignorados e listados em `ignored_args`, e
nenhuma linha compartilhada próxima sob outra chave a recusa. `learnings.review` não promove nenhum candidato
do tipo `definition` para a memória.

Uma definição compartilhada é **governada** enquanto foi promovida de exatamente uma linha, a
linha de todo o namespace sob sua chave, e essa linha está `active`, não retirada por uma substituição,
não além de seu `expires_at` ou `valid_to`, nem rotulada nem classificada como `medium` ou `high`,
e contém o texto que a cópia compartilhada contém (o hash servido de ambos, de modo que um armazenamento que ordene
as chaves de um objeto de outra forma não conta como mudança). Uma edição, uma nova rotulagem para `medium` ou `high` ou uma
tombstone da origem encerra a governança; reverter a edição ao texto aprovado a torna governada
de novo, sem nova promoção. `memory.search` omite uma definição compartilhada cuja
origem de todo o namespace sumiu (excluída, ou marcada com tombstone pela varredura de expiração) ou está rotulada como `medium`
ou `high`, já que a camada compartilhada não tem rótulo próprio. Quando uma origem não pode
ser lida, ele omite todas as definições compartilhadas. Uma cópia promovida de uma linha que não seja a
de todo o namespace é julgada pela linha de todo o namespace sob sua chave: retida quando não
há nenhuma, servida conforme o rótulo dessa linha caso contrário. A própria cópia permanece: uma nova rotulagem de volta, ou a
origem escrita novamente, volta a servi-la, e `memory.demote` a retira.
`memory.definitions.list` (admin) lista as definições compartilhadas ativas do namespace, as mais
recentemente promovidas primeiro e no máximo o dobro de `memory_shared_definitions_max`, com `count` de
todas elas. Cada entrada traz sua chave e `promoted_at`; uma dentro do limite
traz também seu termo e aliases, seu `verdict` (`governed` ou `lapsed`), o `lapse_reason`
(`not_namespace_wide`, `no_term`, `gone`, `superseded`, `conflicted`, `duplicate`,
`not_active` para qualquer outro status, `expired`, `ended` após seu `valid_to`, `changed` ou
`sensitive`) e, para uma governada, `lost`: cada nome que ela perde para outra definição
governada que o detém, como `name` e a chave do `winner` (a promoção mais recente vence, com empate
resolvido pelo seu evento de promoção). Uma além do limite é `capped`, e sua origem não é lida.

<a id="governed-definitions-in-search"></a>

### Definições governadas na busca

`memory.search` serve uma definição governada primeiro quando a consulta cita seu termo ou um de
seus aliases, marcada com `governed: true`. Essa etapa de termo roda onde quer que `memory.search` rode:
dentro de `memory.ask` e `context.pack`, e na seção de memória de `loom_search`.

- **O que ela lê.** Apenas os nomes das definições compartilhadas ativas do namespace endereçado,
  qualquer que seja o alcance de `namespace_scope`, no máximo `memory_shared_definitions_max` delas
  (além disso, as mais recentemente promovidas, e `term_leg` é `capped`). Ela lê os valores completos, e lê e classifica as origens, das definições que a consulta cita e das
  que compartilham um nome com uma delas, e de nenhuma outra, sem consistência sequencial: uma promoção em
  outra réplica de instantes atrás pode não aparecer em uma busca. Ela roda ao lado das outras etapas, e depois que
  elas terminam a busca espera no máximo `memory_term_leg_wait_ms` a mais por ela (uma passagem reentrante, não mais que o tempo que lhe resta). Um orçamento fixo de trabalho também limita a normalização,
  as comparações de nomes e a decodificação de corpos para validação. Trabalho excessivo retém
  a etapa inteira como `timed_out`; as definições armazenadas e as regras de promoção não mudam.
- **Correspondência.** A consulta e cada nome perdem seus caracteres de formato e passam por NFKC
  com espaços em branco colapsados; uma marca combinante junta-se à letra ou ao número anterior. Um nome
  corresponde em caracteres inteiros, sem vizinho que continue sua borda: ao lado de uma letra latina, um dígito, `_`, `+` ou `#`, outro desses; ao lado de uma letra de outra escrita
  escrita com espaços (cirílico, grego, devanágari), qualquer letra fora do latim ou qualquer dígito;
  antes de hangul, uma letra hangul, e depois dele nada, já que partículas se seguem; ao lado de han,
  kana, tailandês, lao, khmer, birmanês ou pontuação, nada. Assim `MRR` corresponde em "什么是ＭＲＲ" e
  `ARR` em "ARR은", enquanto `рост` não corresponde dentro de "простой". A diferença entre maiúsculas e minúsculas é decidida por palavra de
  um nome: uma palavra toda em maiúsculas, ou com maiúscula após a primeira letra, corresponde apenas como
  escrita (`IT` não corresponde a "what is it"); qualquer outra corresponde com case-fold.
- **Posicionamento.** As definições citadas lideram a lista servida na ordem em que seus nomes aparecem pela primeira vez
  na consulta, o nome mais longo primeiro quando dois começam juntos, depois a promoção mais recente; a linha calculada de um agregado derivado
  permanece acima delas. Qualquer outro resultado sob a mesma chave no namespace endereçado é omitido. Elas contam dentro de `top_k` e são cortadas
  por último; o que é cortado, um resultado governado ou um que ele empurrou para fora, lidera a próxima página. Um resultado
  governado estacionado é verificado de novo contra a consulta original e a promoção e
  a origem atuais antes de ser servido; uma verificação não lida o retém. Os corpos inteiros contam dentro de
  `body_budget_tokens`; uma definição que não cabe mantém um trecho marcado como truncado.
  Nenhum piso,
  limiar ou etapa posterior as descarta ou reordena: nem `min_similarity`, a calibração,
  os pisos e a regra de resposta primeiro de `loom_search`, o limite de memória do veredito do tracker (dentro
  do qual elas contam), o empacotamento de `context.pack` ou a verificação de valores reapresentados de `memory.ask`.
- **Pontuações.** `similarity` e `distance` são as do embedding compartilhado da definição (`0` e
  `1` sem um), `rank_score` está acima o de todo resultado, exceto de uma linha calculada, e
  `below_threshold` nunca é definido. Uma resposta que serve uma tem `abstain` falso;
  `retrieval_confidence` e `retrieval_risk` descrevem os outros resultados.
- **Quando está desligada.** Um handle de página, e um campo que pede outras linhas: `memory_type`,
  `tags` ou `category` (não com `tags_soft` ou `category_soft`), `as_of`, `min_body_chars`,
  `recent_first` ou `anchor_first` acima de 0, ou `legs` sem `memory`. Uma consulta com Unicode inválido
  a desliga quando a verificação Unicode limitada a lê; uma consulta além do limite de trabalho dessa verificação
  informa `timed_out` antes da validação. Um campo que diz de quem são as linhas a ler
  (`agent`, `user_id`, `session_id`, `session_ids`, `run_id`, `scope`) a mantém ligada, e
  `exclude_keys` descarta suas chaves.
- **`term_leg`** na resposta diz o que ela fez: `complete`, `empty` (sem definições),
  `capped`, `timed_out`, `failed` (uma leitura falhou, então nada foi servido) ou `off`.
- **Escopo.** Ela serve definições governadas sob qualquer escopo de leitura e sob `scope`
  `private`, o padrão: um admin as publicou para todo leitor do namespace.
- **Falhas de busca.** Uma busca que serve uma definição governada, ou cuja etapa não informou
  `complete` ou `empty` (`off` incluído), não registra nenhuma falha de [lacuna de
  conhecimento](https://github.com/ClickHouse/loom/blob/main/docs/architecture/knowledge-gaps.md#recording-misses).
- **Respostas armazenadas.** Uma promoção e um rebaixamento movem a marca de escrita de memória do namespace, de modo que
  `memory.ask` não reutiliza nenhuma resposta anterior a eles depois que a marca é gravada.

Por que duas camadas: os agentes escrevem livremente e, muitas vezes, de forma redundante sob sua própria identidade,
enquanto um fato curado em que todo o namespace deve confiar precisa de uma chave canônica, da decisão de
um responsável e de uma trilha de auditoria. A promoção é essa decisão.

<a id="identity-modes"></a>

## Modos de identidade

A forma como um chamador obtém um token é definida pelo modo de identidade da implantação
(`LOOM_IDENTITY_PROVIDER`):

- **`single-user`**: sem autenticação; uma chamada que não informa nenhum namespace é resolvida para um
  (`LOOM_DEFAULT_NAMESPACE`, padrão `local`, na organização `LOOM_SELF_SIGNUP_ORG`, padrão
  `dev`) via MCP, em todas as ops exceto `code.*`. Via HTTP, uma chamada sem token recebe a organização em
  todas as ops exceto `code.*` e `tracker.*`, e o namespace nas ops cotidianas de memória e
  busca; quais ops são essas, e o agente que cada uma recebe, estão na
  [referência HTTP](https://github.com/ClickHouse/loom/blob/main/docs/reference/http-api.md#request). Uma busca `all_mine` também lê os
  namespaces que o usuário registrou. Apenas para um laptop.
- **`trust-network`**: cadastro próprio com email e senha; o primeiro principal a
  reivindicar um nome de namespace passa a ser o dono dele. Um agente pode se cadastrar sozinho (`loom init --agent`) e uma pessoa o reivindica depois com o código que ele exibiu, que funciona uma única vez e é substituído pela
  senha da própria pessoa.
- **`gateway`**: uma camada de identidade upstream autentica as pessoas e emite tokens curtos do
  Loom com o segredo compartilhado; o Loom apenas verifica. Um subject em
  `LOOM_TRUSTED_CALLERS` pode agir em nome de um usuário final com `X-Loom-On-Behalf-Of`, e cada
  linha do ledger registra o chamador real. Um serviço cujos usuários não têm conta no Loom assina um
  token de curta duração por requisição para um `user:<id>`, com uma chave registrada para isso
  como emissor de usuários finais ([agir em nome dos usuários finais de um serviço](https://github.com/ClickHouse/loom/blob/main/docs/guides/tokens-and-grants.md#act-for-a-services-end-users)).

<a id="collections"></a>

## Coleções

Uma coleção é um namespace chamado `col-…` ao qual só concessões (grants) abrem acesso: nenhuma claim de token nem papel de administrador da organização chega a ela. Uma coleção pode ficar dentro de outra e herdar suas concessões, e uma concessão em uma coleção pode nomear um principal `user:`, `group:` ou `agent:`, ou `everyone` (toda requisição que age na organização), além de uma pessoa ou de uma conta de serviço. As pessoas se referem a uma coleção pelo título, e o servidor deriva dele o nome `col-…`; o nome continua sendo seu endereço para sempre. [Compartilhamento de conhecimento](https://github.com/ClickHouse/loom/blob/main/docs/architecture/sharing.md) descreve o modelo e como um nome é derivado. As pessoas veem uma coleção como coleção, na Biblioteca, pelo título e pelo lugar que ela ocupa ali; o namespace que a armazena é a chave para concessões, escopo de busca, auditoria e exclusão, e ele nunca aparece no seletor de namespace do painel.

Um namespace chamado `acl-` seguido de 24 dígitos hexadecimais é um espelho: ele guarda os documentos de uma fonte de documentos que segue as permissões do seu repositório, cada um exibido apenas a um leitor que o repositório admite ([fontes de documentos](https://github.com/ClickHouse/loom/blob/main/docs/architecture/document-sources.md#the-mirror)). Nenhum token chega a um espelho (apenas a exclusão por um administrador da plataforma o nomeia), então `namespace.register` e `namespace.restore` recusam essa forma.

Quando `LOOM_IDENTITY_PROVIDER` está vazio, o servidor infere o modo: autenticação desativada resulta em `single-user`, autenticação ativada com `LOOM_SELF_SIGNUP=open` resulta em `trust-network`, e autenticação ativada em qualquer outro caso resulta em `gateway`. Um bind não loopback com cadastro aberto também exige `trust-network` definido explicitamente. [Implantação](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#identity-modes) lista as configurações de cada modo.

<a id="related"></a>

## Relacionados

- [Tokens e grants](https://github.com/ClickHouse/loom/blob/main/docs/guides/tokens-and-grants.md): emissão, claims, grants, namespaces
  restritos, política de histórico e revogação, passo a passo.
- [Namespaces e compartilhamento](https://github.com/ClickHouse/loom/blob/main/docs/features/namespaces-and-sharing.md): as operações
  de namespace e as visões de compartilhamento do painel.
- [Segurança](https://github.com/ClickHouse/loom/blob/main/docs/architecture/security.md): como a autenticação e a autorização são
  aplicadas.
- [Memória](https://github.com/ClickHouse/loom/blob/main/docs/concepts/memory.md): a linha que a tupla endereça.
