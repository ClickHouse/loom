<!-- i18n: source=docs/operations/code-index-operations.md blob=386d854cb14cf44fb989df236eec8a7a0d02d1ee model=claude-sonnet-5-5 -->
> Esta página é uma tradução automática da [página em inglês](https://github.com/ClickHouse/loom/blob/main/docs/operations/code-index-operations.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/operations/code-index-operations.md), which is authoritative.

<a id="code-index-operations"></a>

# Operações do índice de código

Esta página explica como habilitar os workers do índice de código, configurar repositórios e manter o índice atualizado; o índice de código só roda quando um operador habilita seus workers e configura repositórios. Todo worker `code_*`, exceto `code_coverage_notify` e `code_backport_gaps`, vem desativado por padrão. O que as operações `code.*` respondem está na [visão geral do índice de código](../features/code-index/overview.md); como o índice é construído está na [arquitetura do índice de código](../architecture/code-index-architecture.md).

Um namespace de código indexa um branch de um repositório. Há duas formas de adicionar um:

| Forma              | Como                                              | Camadas                          | Página                                                 |
| ------------------ | ------------------------------------------------- | -------------------------------- | ------------------------------------------------------ |
| Autoatendimento    | painel **Repositories → Add repository**          | parser (`code_ingest`)           | [repositórios](../features/code-index/repositories.md) |
| Configuração do operador | `LOOM_CODE_REPOS_JSON`                      | parser + camada do compilador    | esta página                                            |

Uma entrada do operador para um namespace que um membro adicionou pela página é ignorada ([adicionando repositórios](../features/code-index/repositories.md)).

Execute `loom code` dentro do contêiner que roda os workers (`loom-workers` em `dev/docker-compose.cloud.yml`, `loom-server` em stacks de contêiner único), para que ele use o `.env`, os git mirrors e o ClickHouse do deployment:

```bash
docker exec loom-workers loom code stats --namespace code-myrepo
```

`--org` / `--namespace` assumem por padrão `LOOM_CODE_ORG` / `LOOM_CODE_NAMESPACE`. Informe os dois quando o deployment indexar mais de um repositório.

<a id="prerequisites"></a>

## Pré-requisitos

- **A imagem.** Ela inclui as gramáticas do parser (o extra `code`), Go com scip-go,
  Node com scip-typescript e scip-python, além de npm, pnpm, Yarn e Bun para a
  preparação automática de dependências de TypeScript. Quem usa Docker não instala
  nada disso no host. ClickHouse é o único serviço em execução obrigatório.
- **Toolchains do servidor nativo.** Instale Python 3.12+, Git e Node.js 22+ com npm
  e, em seguida, execute estes comandos a partir do checkout do Loom antes de iniciar
  o servidor:

  ```bash
  pip install -e ".[code]"
  sh scripts/install-code-toolchains.sh
  export PATH="$PWD/var/code-toolchains/bin:$PATH"
  ```

  O script instala indexadores e gerenciadores de pacotes de TypeScript/Python com
  versões fixas, sem exigir root. Mantenha esse `PATH` no ambiente do servidor Loom,
  inclusive no do seu gerenciador de serviços. A indexação nativa de Go exige, além
  disso, Go e scip-go:
  `go install github.com/scip-code/scip-go/cmd/scip-go@v0.2.7` (coloque o diretório bin
  do Go no `PATH` do servidor). C/C++ precisa de scip-clang e da configuração de build
  do projeto.

- **Um embedder.** Os chunks são gerados como embeddings com o `LOOM_EMBEDDING_MODEL`
  do servidor. `loom code backfill --embed` recusa o embedder fake, a menos que você
  passe `--allow-fake-embedder` (somente para desenvolvimento).
- **Mirrors git graváveis.** Os workers rodam como uid 999 e fazem fetch nos clones
  bare em `var/code_repos` (montado por bind mount nos contêineres). O operador do
  host compartilha esse diretório com eles por meio de um grupo, aqui o gid 999.
  Prepare-o uma única vez:

  ```bash
  sudo chgrp -R 999 var/code_repos && sudo chmod -R g+rwX,o-rwx var/code_repos
  sudo find var/code_repos -type d -exec chmod g+s {} +
  ```

  O bit setgid mantém as novas entradas no grupo. Depois, adicione o operador do host
  ao grupo e defina `LOOM_CODE_REPOS_SHARED_GID=999` no `.env`, que tanto os
  contêineres quanto os comandos `loom code` do host leem. Um diretório de mirror ou
  de checkout que o Loom cria nesse grupo tem modo 2770; todos os demais têm modo
  0700, abertos apenas ao usuário do servidor, de modo que nada é compartilhado até
  que a configuração indique o grupo. Com a configuração, o servidor e todo comando
  `loom code` rodam com umask 007: os diretórios e arquivos de lock que eles criam
  são graváveis pelo grupo, os arquivos do git dão ao grupo o que dão ao dono (seus
  objetos continuam somente leitura), e os demais usuários não recebem nada. Execute
  um harness manualmente em um checkout sob a raiz por meio de
  `loom code entry-lock`, que o executa com a mesma umask. O Loom altera o modo de um
  diretório existente apenas para uma entrada restrita: execute os comandos novamente
  para arquivos criados por uma build mais antiga ou por outra ferramenta.

  O grupo lê todos os mirrors, inclusive os restritos, então deve conter apenas os
  usuários que executam o Loom: o operador do host e o usuário dos contêineres
  (`getent group 999`). Quando o gid 999 já é um grupo do sistema com outros membros
  (`systemd-journal` em muitas distribuições), execute os contêineres com um grupo
  dedicado (`user: "999:<gid>"` no Compose) e use esse gid nos comandos e na
  configuração. Mantenha todo caminho que o host e os contêineres compartilham
  (`code_repos_root`, `code_scip_checkout_dir` e o `git_dir` e o `scip_checkout_dir`
  de cada entrada) dentro do diretório preparado: um diretório ou arquivo de lock que
  o Loom cria em outro lugar assume o grupo do pai, se o pai tiver setgid, e o grupo
  do processo que o cria, caso contrário; com a umask, esse grupo poderia gravá-lo.

  A umask cobre todo arquivo que esses processos gravam, inclusive fora da raiz, onde
  o arquivo assume o grupo primário de quem o grava: o grupo primário do operador do
  host deve ser só dele (um grupo privado do usuário, o padrão na maioria das
  distribuições), e não um compartilhado com outros, como `users`.

  Atualize o checkout do host que executa os jobs de `loom code` junto com a imagem e
  execute os comandos acima novamente depois que a cor anterior de um deployment por
  cores for interrompida.

- **Um token do GitHub** (`LOOM_CODE_GITHUB_TOKEN`, ou `github_token` por entrada)
  para o histórico de PRs mesclados e para remotes privados.

<a id="configure-the-entry"></a>

## Configurar a entrada

`LOOM_CODE_REPOS_JSON` é um array JSON com um objeto por branch indexado. Os campos que uma entrada omite recorrem às configurações escalares `LOOM_CODE_*`; uma entrada para um repositório diferente do `LOOM_CODE_REPO` escalar deriva de `repo` o seu `git_remote`
(`https://github.com/<owner>/<name>.git`) e o seu `git_dir` (`<name>.git` ao lado do clone escalar, ou em `var/code_repos` quando não há repositório escalar).

```bash
LOOM_CODE_REPOS_JSON='[
  {"org": "acme", "namespace": "code-myrepo", "repo": "acme/myrepo",
   "git_branch": "main", "public": true},
  {"org": "acme", "namespace": "code-myrepo-release-2", "repo": "acme/myrepo",
   "git_branch": "release-2", "embed_reuse_from": "code-myrepo"}
]'
```

| Campo                                | Significado                                                                                                                                  |
| ------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------- |
| `namespace`, `org`                   | onde o índice fica                                                                                                                           |
| `repo`                               | `owner/name` no GitHub                                                                                                                       |
| `git_dir`, `git_remote`              | o clone bare e seu remoto; branches de um mesmo repositório compartilham um clone                                                            |
| `git_branch`                         | o branch que este namespace acompanha                                                                                                        |
| `path_prefixes`                      | prefixos separados por vírgula a indexar; uma entrada sem ele indexa a árvore inteira, menos a lista de exclusão embutida                    |
| `exclude`                            | globs que nunca são indexados (lista ou separados por vírgula)                                                                               |
| `exclude_generated`                  | recusa arquivos cujo conteúdo os marca como gerados (`Code generated … DO NOT EDIT.`, `@generated`)                                          |
| `include_contrib_paths`              | caminhos `contrib/` vendorizados a indexar mesmo assim, para que suas definições se liguem aos pontos de chamada do repositório              |
| `depends_on`                         | outros namespaces de código que este código chama; uma definição ausente aqui é procurada lá                                                 |
| `embed_reuse_from`                   | um namespace cujos embeddings são reaproveitados quando o texto do chunk é idêntico (um branch de release do mesmo repositório)              |
| `public`                             | o repositório é público no seu host: qualquer pessoa da org pode entrar de imediato no índice como leitora                                   |
| `classification`                     | `restricted` para um repositório privado: um mantenedor aprova cada leitor, e ele nunca é legível pela org                                   |
| `scip_producer`, `scip_checkout_dir` | o indexador da camada do compilador (`clang`, `typescript`, `python`, `go`; se não definido, os arquivos de build da árvore decidem) e sua árvore de trabalho |
| `scip_extra_producers`               | outros produtores da camada do compilador sobre a mesma árvore (um repositório Python com frontend TypeScript), cada um em seu próprio checkout |
| `git_extra_branches`                 | globs de outros branches remotos que o espelho compartilhado deve carregar                                                                   |
| `github_token`                       | substituição por entrada de `LOOM_CODE_GITHUB_TOKEN`                                                                                         |

Escreva o host de `git_remote` em ASCII, como punycode para um nome internacionalizado: a adição de um repositório pela página recusa qualquer outro, para que os dois escrevam o host da mesma forma.

Uma entrada cujo `git_remote` é um caminho local ou um espelho `file://` é conhecida pelo seu `repo`, com o qual a adição do mesmo repositório pela página deve coincidir: nomeie em `repo` um espelho de um repositório hospedado fora do GitHub como `host/owner/name`.

`public` é lido quando `loom code init` registra o namespace pela primeira vez; editá-lo depois não altera a política de ingresso, que um mantenedor altera com `code.repo.set_access`.
Uma entrada configurada começa sem mantenedor. `POST /v1/admin/namespace.adopt` recusa um namespace restricted, e nenhuma rota ou comando instala a primeira concessão direta em um namespace restricted configurado, então um repositório privado é configurado nesta ordem:

1. Configure a entrada sem `classification`, com `public` false (o padrão) e sem `org_readable`, e deixe `LOOM_CODE_ORG_READABLE` no padrão, `false`. Até o passo 3 o namespace é standard: uma concessão nele, ou um token cuja claim o cubra (inclusive uma claim `*`, e não só uma que o nomeie), o lê.
2. Execute `loom code init` para ele, o que grava a linha de diretório do namespace (adopt responde `NAMESPACE_NOT_FOUND` antes disso), e adote-o para o seu mantenedor. Não execute ainda `loom code backfill` (nem qualquer comando `loom code backfill-*`): uma entrada configurada não recebe primeiro índice, histórico de PRs, vereditos de backport nem branches extras (`code.repo.add_branch` responde `REPO_NOT_INDEXED`) até que o backfill grave sua marca d'água `git_head`, então nada do repositório é indexado até você executá-lo. O espelho git da entrada é clonado nesse meio tempo, e as leituras de seu histórico passam pelo mesmo controle, então conclua o passo 3 antes de distribuir tokens `*` da org.
3. O mantenedor o restringe com `code.repo.set_access` (`classification: "restricted"` e `join_policy: "approval"` ou `"closed"`; ambos são obrigatórios); depois defina também `classification: "restricted"` na entrada, para que qualquer uma das entradas restrinja, e execute `loom code backfill`. Espelhos e checkouts da camada do compilador são criados com o modo 2770 quando pertencem a `code_repos_shared_gid`, abertos apenas ao usuário do servidor e a esse grupo, e 0700 nos demais casos (veja "Espelhos git graváveis"); um criado por uma build mais antiga, ou cujo modo foi alterado manualmente, é ajustado para esse modo imediatamente no host que atende a chamada, e em todo outro host que o mantenha na próxima rodada de espelhamento desse host, assim que o host lê a linha de diretório do namespace como restricted (em até meio minuto). Só o diretório em si muda: o que uma build mais antiga criou dentro dele precisa dos comandos descritos em "Espelhos git graváveis".

A restrição é por branch: o índice de cada branch é seu próprio namespace. Uma pessoa cujo próprio token lê o repositório no seu host ainda pode adicionar outro de seus branches com `code.repo.add`, em um novo namespace standard; essa pessoa já pode ler o repositório. Configure cada branch configurado de um repositório privado na ordem acima.

Uma entrada que não nomeia `scip_producer` mantém `scip_checkout_dir` para C/C++ (veja
[Camada do compilador](#compiler-tier)), então nomeie o produtor ao preparar essa árvore para outra linguagem.

Os padrões `linguist-generated` / `linguist-vendored` do `.gitattributes` do repositório e um arquivo `.loomignore` no commit indexado são respeitados como exclusões. Uma dependência vendorizada como submódulo git não está no espelho bare; dê a ela sua própria entrada e nomeie-a em `depends_on`. O gauge `loom.code.repos_config_errors` informa as entradas que o servidor não pôde usar (JSON inválido, uma entrada sem nome, um namespace com `.` ou `/`, um namespace duplicado); 0 significa que o valor foi interpretado sem erros. Valide o JSON antes de reiniciar com `docker compose --env-file .env -f dev/docker-compose.cloud.yml config`.

Quem pode ler um índice é definido pela política de ingresso e pelas concessões do namespace
([tokens e concessões](https://github.com/ClickHouse/loom/blob/main/docs/guides/tokens-and-grants.md)). `LOOM_CODE_ORG_READABLE=true`
torna todo namespace de código legível por todo principal da org sem concessão; o padrão é desativado.

<a id="first-index"></a>

## Primeiro índice

```bash
docker exec loom-workers loom code init     --namespace code-myrepo --org acme   # optional: bare clone + index state
docker exec loom-workers loom code backfill --namespace code-myrepo --org acme --embed
docker exec loom-workers loom code backfill-prs --namespace code-myrepo --org acme --months 12
```

- `init` é opcional: a passagem de ingestão clona em segundo plano um mirror ausente, inclusive para uma entrada configurada pelo operador, e registra no log o caminho em que clona (a linha `code_mirrors` do `loom doctor` indica um `git_dir` do operador que não existe, de modo que um caminho digitado errado não vira silenciosamente um clone de vários GB). O mirror é um cache dos registros remotos do ClickHouse: cada host de serviço clona os mirrors das entradas indexadas na inicialização (`LOOM_CODE_MIRROR_WARMUP`) e, a cada `LOOM_CODE_MIRROR_FETCH_INTERVAL_S`, busca as refs que a passagem do líder registrou no ClickHouse e que ele não possui (sem `ls-remote` e sem chamada de rede quando já está atualizado). Até que um host tenha o mirror, suas leituras de histórico respondem `no_git_mirror: cloning <entry>`; `/v1/health?include=mirrors` conta os mirrors do host em `mirrors`. Disco: cada host guarda uma cópia de cada mirror (N réplicas, N cópias), e um `git_dir` do operador deve ser gravável pelo usuário `loom`. Rede: um host com disco vazio faz um clone completo por repositório indexado; mantenha `LOOM_CODE_REPOS_ROOT` em um volume persistente para que um rollout não reclone todos os mirrors em cada pod.
- `backfill` faz o parsing, divide em chunks, extrai símbolos e arestas e gera embeddings de cada arquivo. É retomável: execute-o novamente após uma interrupção. `--phase index` seguido de `--phase embed` divide o trabalho, já que as linhas sem embedding formam a fila da fase de embed. `--commit <sha>` fixa a árvore (e implica `--no-fetch`); `--concurrency N` paraleliza os arquivos; `--limit N` indexa uma amostra.
- O primeiro backfill completo de um namespace também rederiva as arestas de cada arquivo depois que a tabela de nomes está completa, de modo que as referências entre arquivos sejam resolvidas (`--no-edges-second-pass` o pula). `--dialect-folder-pass` permite que arquivos SQL de dialeto indefinido assumam o dialeto com o qual o diretório concorda.
- `backfill-prs` carrega o histórico de PRs mesclados do GitHub, é retomável por cursor e limitado pelo limite de taxa do GitHub (`LOOM_CODE_PR_BACKFILL_MONTHS`, padrão 12). `backfill-reviews` adiciona comentários de revisão inline, `backfill-assignees` os revisores solicitados e `backfill-items` issues e PRs em qualquer estado.
- **O worker não inicia um primeiro índice para uma entrada configurada.** Uma entrada sem marca-d'água `git_head` é ignorada com a nota "run `loom code backfill` first". (Ele constrói primeiros índices de autoatendimento, `LOOM_CODE_BOOTSTRAP_FILES_PER_PASS` arquivos por passagem.)

Depois:

- `POST /v1/code.calibrate` (`ADMIN`) deriva os limiares de abstenção do índice de código a partir de `bench/fixtures/code_calibration_probes.jsonl` (incluído na imagem).
- `loom code describe --top-pct 20` (opcional, com custo de LLM) escreve descrições de uma linha para os chunks mais centrais.
- `loom code stats` exibe as marcas-d'água (`git_head`, `scip_commit`, …) e as contagens de linhas em tempo real; `POST /v1/code.index_status` responde o mesmo pela API.

`scripts/ops/code_intel_bootstrap.sh` executa essa sequência para `LOOM_CODE_ORG` / `LOOM_CODE_NAMESPACE` configurados no host do Loom (init, as duas fases de backfill, histórico de PRs, calibrate e, em seguida, os gates de `bench/code_intel_qa.py`), recusando um embedder falso ou um servidor cujo `/v1/health` não responda; `--dry-run` executa apenas essa verificação prévia somente leitura e imprime os comandos. Ele lê `LOOM_BASE_URL` e `LOOM_ADMIN_TOKEN`.

<a id="steady-state"></a>

## Estado estável

Habilite os workers no `.env` no container de workers e recrie-o. A configuração de cadência e o valor padrão de cada worker estão na [referência de workers](https://github.com/ClickHouse/loom/blob/main/docs/reference/workers.md).

| Worker                 | Flag                                     | Função                                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| ---------------------- | ---------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `code_ingest`          | `LOOM_CODE_INGEST_ENABLED`               | a cada `LOOM_CODE_INGEST_INTERVAL_SECONDS`: faz o fetch e, para cada commit de first-parent, refaz o chunking apenas do conteúdo alterado, atualiza símbolos e arestas, marca exclusões com tombstone, monta o pacote de contexto do PR mergeado e avança `git_head`. A cada hora, também encontra arquivos indexados cujos chunks são anteriores ao conteúdo no head e corrige até `LOOM_CODE_RECONCILE_HEAL_PER_PASS` por passagem. Constrói os primeiros índices de autosserviço. |
| `code_embed_recovery`  | `LOOM_CODE_EMBED_RECOVERY_ENABLED`       | gera embeddings dos chunks que ficaram sem vetor (queda do provedor, `--phase index`)                                                                                                                                                                                                                                                                                                                                                                         |
| `code_pr_history`      | `LOOM_CODE_PR_HISTORY_ENABLED`           | sincroniza do GitHub os PRs recém-mergeados                                                                                                                                                                                                                                                                                                                                                                                                                   |
| `code_graph_stats`     | `LOOM_CODE_GRAPH_STATS_ENABLED`          | semanalmente: PageRank sobre o grafo de includes, graus por arquivo, o mapa teste ↔ código-fonte                                                                                                                                                                                                                                                                                                                                                              |
| `code_summary`         | `LOOM_CODE_SUMMARY_ENABLED`              | semanalmente: cards de arquivo e de módulo gerados por LLM para `code.map` (com limite de custo por namespace)                                                                                                                                                                                                                                                                                                                                                |
| `code_scip_apply`      | `LOOM_CODE_SCIP_APPLY_ENABLED`           | aplica os deltas da camada do compilador enviados por `code.scip_ingest`                                                                                                                                                                                                                                                                                                                                                                                      |
| `code_coverage_notify` | `LOOM_CODE_COVERAGE_NOTIFY_ENABLED` (on) | um evento de tarefa quando a cobertura de um commit monitorado está pronta em todas as camadas                                                                                                                                                                                                                                                                                                                                                                |
| `code_backport_gaps`   | `LOOM_CODE_BACKPORT_GAPS_ENABLED` (on)   | o relatório noturno de backport; só roda junto com `code_pr_history`                                                                                                                                                                                                                                                                                                                                                                                          |

Os workers da camada do compilador por linguagem estão na próxima seção.

**Atualização acionada por push.** Um job de CI pode chamar `POST /v1/code.refresh` (`WRITER`) logo após um merge. Ele executa imediatamente a mesma passagem de ingestão para esse namespace, na réplica que a recebeu, sob o lease do namespace (`code_refresh:<org>/<namespace>`), de modo que gatilhos que chegam a réplicas diferentes se agrupam em uma única passagem. Responde 404 para um namespace que nenhuma entrada configura, então quem envia o push nunca decide o que é indexado. Funciona com o poller desabilitado.

Pull requests abertos não são indexados; a árvore de trabalho continua sendo do próprio cliente.

<a id="compiler-tier"></a>

## Camada do compilador

A camada do parser responde a partir do tree-sitter. Onde existe um índice de nível de compilador (SCIP), seus
símbolos e referências prevalecem. Cada linguagem tem um worker que indexa os commits entre
sua própria marca d'água e o head do branch no mirror da entrada (obtido por `code_ingest`)
e aplica o delta no próprio processo, em qualquer processo que detenha o lease dos workers. Uma
camada do compilador não espera o `git_head` da camada do parser; somente o primeiro
índice de uma entrada espera.

| Linguagem  | `scip_producer` | Flag do worker                                                | O host precisa de                                                                                                                  |
| ---------- | --------------- | ------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------- |
| C / C++    | `clang`         | `LOOM_CODE_SCIP_DELTA_ENABLED`, `LOOM_CODE_SCIP_FULL_ENABLED` | clang 21, CMake e ninja (na imagem) e scip-clang (na imagem amd64; em outras, `LOOM_CODE_SCIP_CLANG_PATH`)                         |
| TypeScript | `typescript`    | `LOOM_CODE_SCIP_TS_DELTA_ENABLED`                             | Node e o indexador/gerenciadores de pacotes fixados (incluídos na imagem); as dependências são preparadas automaticamente          |
| Python     | `python`        | `LOOM_CODE_SCIP_PY_DELTA_ENABLED`                             | scip-python (na imagem)                                                                                                            |
| Go         | `go`            | `LOOM_CODE_SCIP_GO_DELTA_ENABLED`                             | Go e scip-go (na imagem)                                                                                                           |

Uma entrada que não informa `scip_producer` recebe todas as camadas cujo arquivo de build
sua árvore contém: `go.mod` (em qualquer profundidade) para Go; `tsconfig.json`, `jsconfig.json` ou
`package.json` na raiz para TypeScript; um `CMakeLists.txt` na raiz para C/C++;
`pyproject.toml`, `setup.py` ou `requirements.txt` na raiz para Python.
`code.index_status` as lista como `compiler_candidates`. Informe o producer quando um arquivo
de build for incidental, como um repositório C++ cujo `pyproject.toml` serve apenas às suas
ferramentas de teste; `scip_extra_producers` adiciona camadas ao lado de uma informada. Cada camada trabalha na própria
árvore: o producer informado (C/C++ quando a entrada não informa nenhum) em `scip_checkout_dir`, todos
os outros em `<scip_checkout_dir>.<producer>`.

Todos os workers vêm habilitados por padrão, e a imagem Docker traz todos os indexadores:
scip-python, scip-typescript, scip-go e, para C/C++, clang 21, lld, CMake, ninja e
scip-clang. O upstream compila o scip-clang apenas para Linux x86_64, então a imagem arm64 tem
o resto da toolchain C/C++, mas não o scip-clang: aponte `LOOM_CODE_SCIP_CLANG_PATH` para
seu próprio build, ou a camada C/C++ registra `scip-clang missing` como um skip. Cada worker
cria uma árvore de trabalho ausente como um repositório vazio e busca cada commit nela
a partir do mirror da entrada, então um novo líder começa do zero. Um worker cujos
pré-requisitos de projeto estão ausentes registra um skip em `workers.status`; a camada Tree-sitter
continua disponível. Os projetos TypeScript têm suas dependências instaladas
automaticamente no checkout.

O harness C/C++ executa o configure do CMake do repositório (apenas banco de dados de compilação, sem
build) na primeira execução em uma árvore, ou quando seu `CMakeCache.txt` está ausente, foi criado em
outro diretório, ou nomeia um compilador que o host não tem mais. Ele executa o configure
novamente sobre o cache existente antes de um índice completo e para um delta cuja janela adiciona um
arquivo `.cpp`, `.cc` ou `.cxx` ou adiciona, altera ou exclui um arquivo `CMakeLists.txt` ou `.cmake`,
porque o banco de dados de compilação lista apenas as unidades de tradução da árvore em que
foi configurado. Um reconfigure que falha faz a execução falhar com as últimas linhas do CMake, e o
registro `clang` da entrada (`clang_full` para um índice completo) indica falha com o tipo `cmake`
(`disk` quando o disco está cheio). Um delta com falha não move nenhuma marca d'água, então sua janela, com
as unidades de tradução que adiciona, é tentada novamente a cada passagem; um índice completo é tentado novamente
6 horas depois, como qualquer execução completa com falha.

Uma árvore C++ grande é o caso pesado: um checkout do porte do ClickHouse, com seus submódulos
e árvore de build, ocupa cerca de 30 GB além do mirror, e o scip-clang mantém a AST de uma unidade de
tradução por job (alguns GB cada), então `LOOM_CODE_SCIP_CLANG_JOBS` deve caber na
memória que o processo dos workers tem. O índice completo de uma árvore assim leva cerca de uma hora.

- **Limite de delta C/C++.** O worker indexa as unidades de tradução alteradas, incluindo as
  que incluem um header alterado, até `LOOM_CODE_SCIP_DELTA_MAX_TUS` (200); acima do
  limite, indexa apenas os arquivos `.cpp` alterados. Um delta que reconfigura o CMake executa o
  configure dentro do mesmo `LOOM_CODE_SCIP_DELTA_TIMEOUT_SECONDS` (3600) do índice;
  um delta que estoura o tempo deixa `scip_commit` onde estava, então a próxima passagem tenta uma janela
  maior.
- **Limites do delta.** O servidor lê o delta de uma camada do compilador (qualquer camada, execução delta ou
  completa) somente quando é um arquivo regular, não um link, e o aplica uma rodada de arquivos por
  vez (`server/code/app/index/delta_file.py`). Primeiro conta os bytes `{`, `[`
  e `,` do delta, depois faz o parse de cada valor uma vez e verifica cada entrada de arquivo, e só
  então o reabre e lê os arquivos novamente, uma rodada por vez, para a aplicação. Assim, um
  delta acima de um limite, ou que não faz parse, faz a passagem da entrada falhar antes que qualquer coisa
  seja aplicada. Os limites valem como lidos quando o delta é aberto; um valor alterado vale
  na próxima passagem.
  - `LOOM_CODE_SCIP_DELTA_FILE_MAX_MB` (180 MiB) limita os bytes do arquivo e
    `LOOM_CODE_SCIP_DELTA_MAX_NODES` (6,000,000) seus bytes contados, que limitam os valores JSON
    que os parses constroem. Eles limitam o trabalho de uma passagem. Sua memória é a rodada
    abaixo mais um registro de cada entrada de arquivo e caminho excluído: cerca de 70 B cada, tantos
    quanto o limite de nós permitir (uma entrada mínima custa 2 bytes contados, um caminho excluído 1:
    cerca de 3 milhões de entradas, uns 200 MiB, ou 6 milhões de caminhos excluídos, uns 400 MiB,
    no padrão), mais o texto dos caminhos (no máximo 4,096 bytes cada), que o limite de bytes
    restringe. A execução completa de uma árvore C++ grande grava mais de 500 MiB em entradas de dezenas de
    KB, alguns MiB de controle: aumente ambos para ela.
  - `LOOM_CODE_SCIP_DELTA_ROUND_MAX_NODES` (3,000,000) limita a memória de uma rodada: os
    bytes contados que uma rodada de aplicação processa. Uma rodada contém no máximo 100 arquivos e é
    liberada antes que a próxima seja processada; uma única entrada de arquivo pode preenchê-la, e uma entrada de arquivo
    acima dela faz a passagem falhar, nomeando a entrada. Qualquer outro valor desse tamanho (um
    caminho excluído, um valor de header, uma chave ignorada) resulta em `delta invalid`, pois o
    harness nunca grava um; os caminhos excluídos são lidos um por vez. Um limite fixo de
    64 milhões de caracteres, que não é uma configuração, limita da mesma forma o texto de um valor e de uma rodada; uma entrada real tem cerca de 32 caracteres por byte contado, então para ela o limite vale
    perto de 2,000,000 de bytes contados. A maior entrada de arquivo da execução completa de uma árvore C++ grande
    tem cerca de 600,000 bytes contados (uns 100,000 edges), cerca de um quinto do
    orçamento da rodada e um terço do limite de caracteres. A rodada mais densa que o código de um repositório pode
    gerar (símbolos mínimos) atinge o pico de cerca de 1.8 GiB residentes enquanto é lida no
    orçamento padrão (medido com `ru_maxrss`; cerca de 330 B por byte contado de objetos Python,
    o resto do alocador); o texto de um valor próximo ao limite de caracteres (até 4 bytes por
    caractere) pode ser mantido até três vezes enquanto o buffer de leitura cresce, então a leitura
    da pior rodada é de cerca de 2.5 GiB, qualquer que seja o tamanho do delta. Sua aplicação mantém as
    linhas dessa rodada além disso (as linhas ativas lidas para seus caminhos e as novas linhas a inserir),
    cerca de outro tanto para uma rodada de símbolos novos, então dimensione o worker para cerca do dobro
    da leitura. Uma entrada de arquivo real de um delta C++ tem um byte contado por cerca de 32 bytes:
    uma rodada atinge seus 100 arquivos muito antes de chegar ao orçamento. Para medir a leitura
    da rodada padrão mais densa a partir de um checkout do código-fonte:

    ```bash
    .venv/bin/python -c "import resource, tempfile, pathlib; from server.settings import settings; from server.code.app.index.delta_file import open_delta; p = pathlib.Path(tempfile.mkdtemp()) / 'delta.json'; n = settings.code_scip_delta_round_max_nodes // 2 - 10; p.write_text('{\"commit\":\"c\",\"files\":[{\"path\":\"a\",\"symbols\":[' + ','.join(['{\"qualified_name\":\"\"}'] * n) + ']}]}'); list(open_delta(p).rounds(100, lambda x: True)); print(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss >> 10, 'MiB peak RSS')"
    ```

  Um delta inválido faz a passagem falhar com um breve `delta invalid: <reason>` que nomeia seu
  primeiro problema (para uma entrada, `file entry <i>: …`), não a lista do pydantic com todos os itens
  inválidos. `--keep-delta` mantém o delta como o harness o gravou, antes de ser processado
  (o limite de bytes ainda se aplica), como um arquivo que só o usuário que executou o comando pode ler
  (modo 0600).

- **Índice completo C/C++.** `code_scip_full` reindexa uma entrada por passagem: uma entrada cujo
  `scip_commit` ainda não foi semeado (o delta a ignora com "watermarks not seeded"), e
  toda entrada cuja última execução completa aplicada tenha `LOOM_CODE_SCIP_FULL_EVERY_HOURS` (168)
  horas, o que corrige deltas limitados. Uma execução que falha é tentada novamente 6 horas após a falha,
  e uma interrompida junto com seu pod (um rollout) 6 horas após seu início. Um
  delta de base desconhecida limpa `scip_commit`, então a entrada recebe um índice completo em seguida. A passagem
  roda sob o lock da entrada com `LOOM_CODE_SCIP_FULL_TIMEOUT_SECONDS` (8 horas) para
  o harness e mais seis para a movimentação do checkout, a aplicação e o repack.
  `loom code scip-full` executa o mesmo índice manualmente (`--keep-delta` salva o delta para que uma
  aplicação interrompida possa ser reexecutada com `--apply-delta`), e `loom code scip-delta` uma
  passagem de delta, que imprime uma linha `RESULT:`.
- **Submódulos C/C++.** A camada busca os pins dos submódulos com o token de acesso
  da entrada, então um submódulo privado cuja URL em `.gitmodules` é uma URL https absoluta sob o
  owner do remote não precisa de outra credencial. O owner é comparado como escrito, com
  maiúsculas e minúsculas, então um owner escrito em outra caixa é buscado sem o token. Uma
  URL relativa resolve para um caminho local no checkout, que não tem remote; a camada
  recusa o transporte `file`, então a atualização desse submódulo falha. Quem pode fazer commit em
  `.gitmodules` do repositório pode apontar um submódulo para qualquer repositório desse owner
  que o token leia, então dê a uma entrada um token restrito aos repositórios de que seus submódulos
  precisam.
- **Crossref SQL ↔ C++.** `code_crossref` reconstrói as linhas de referência de conteúdo de `code_test_map`
  (consultas de teste para os arquivos que registram as funções e configurações que usam)
  para cada entrada C/C++ cujo `git_head` mudou desde sua última execução. Ele lê apenas
  os chunks da camada do parser.
- **Manutenção do checkout.** A passagem completa C/C++ (sob o lock da entrada) e as passagens de
  Python, TypeScript e Go fazem o repack do checkout de sua entrada com
  `git gc --no-prune` a cada `LOOM_CODE_MIRROR_GC_INTERVAL_H`, sob as mesmas regras dos
  mirrors (abaixo). Todo comando git que uma camada executa em um checkout tem auto-gc e
  manutenção em segundo plano desligados, então nenhum fetch inicia um repack.
- **Locks de entrada.** Uma passagem completa e um delta nunca trabalham na mesma entrada ao mesmo tempo: cada
  um pega o arquivo de lock da entrada em `LOOM_CODE_SCIP_LOCK_DIR` (`var/cron/locks`),
  `scip.<org>.<namespace>.lock` com ambos os nomes codificados em percent-encoding
  (`loom code scip-lock-path --org ORG --namespace NS` o imprime). Uma passagem completa espera
  um delta que já esteja em sua entrada; um delta ignora uma entrada mantida por uma passagem completa e a nomeia
  em `skipped` ("entry lock held (full pass running)"). Entradas diferentes rodam
  lado a lado. Toda passagem também pega a chave da entrada no ClickHouse (`scip_entry:<org>/<namespace>`
  em `loom._migration_lock`), de modo que
  passagens em dois hosts também se excluem mutuamente; a chave de um detentor morto expira após 120 s.
  Uma passagem que não consegue pegar a chave (ClickHouse ou Keeper inacessível) falha em vez de
  rodar apenas com o arquivo do host. Um detentor considera sua chave perdida após 60 s sem
  renovação (uma pausa incluída), antes que outro host possa pegá-la, e não grava mais nenhuma linha de
  índice nem marca d'água: toda gravação desse tipo verifica a chave antes, e a passagem termina com
  `EntryLockLost`. Uma execução manual do harness vai sob
  `loom code entry-lock --org ORG --namespace NS -- <command>`, que pega ambos os locks,
  sai com 75 quando outra passagem detém a entrada e mata o comando (saída 3) se a chave
  for perdida. Ele entrega ao comando o detentor da chave (`LOOM_ENTRY_LEASE_HOLDER`), que os
  uploads do harness carregam: `code.scip_ingest` recusa uma parte (409 `ENTRY_LEASE_NOT_HELD`)
  quando esse detentor não tem mais a chave, então uma passagem que perdeu sua entrada não completa nenhum
  lote para `scip_apply`. Com `--tier <producer>` ele também registra a saída do comando como
  a saúde dessa camada do compilador na entrada (`tier_health` em `code.repos`) e repassa a
  saída do comando, stderr incluído, para stdout.
- **As dependências do TypeScript são automáticas.** Checkouts novos instalam as dependências
  antes da indexação do compilador, incluindo dependências de desenvolvimento que contêm definições
  de tipos. O Loom usa npm, pnpm, Yarn (clássico ou moderno) ou Bun conforme o
  lockfile, respeita lockfiles existentes e desabilita scripts de ciclo de vida e hooks executáveis
  do gerenciador de pacotes. Scripts simples sem manifesto de pacote não precisam de instalação.
  Mudanças no manifesto/lockfile de dependências disparam uma atualização antes do próximo índice; uma instalação com falha
  ou interrompida é tentada novamente sem avançar a marca d'água do compilador.
  Os downloads precisam de acesso de rede ao registry de pacotes do repositório. Um registry de pacotes
  privado, um lockfile incompatível ou tipos gerados que exigem um build ainda podem precisar
  de configuração específica do projeto. `workers.status` nomeia o instalador que falhou e seu
  código de saída, e o registro de saúde da camada da entrada (`code.index_status`) guarda o final
  da saída do instalador. As credenciais do servidor não são passadas ao ambiente do instalador.
  O servidor lê o `package.json` do checkout (até 1 MiB) e o `yarn.lock` de cada raiz de instalação
  (até 64 MiB) apenas como arquivos regulares, nunca por meio de um link; um deles que seja symlink
  faz a passagem do TypeScript falhar mesmo quando o link aponta para dentro do repositório.
- **Os arquivos do repositório têm o tamanho verificado antes de serem lidos.** A ingestão pergunta ao mirror
  o tamanho do arquivo primeiro: um arquivo acima de `LOOM_CODE_INGEST_MAX_FILE_BYTES` não é lido, e
  sua linha em `code_files` registra o motivo (`code.index_errors`); 0 remove o limite. Todo
  manifesto que o resolvedor de imports lê (os arquivos de projeto e workspace de TypeScript, Python e Go)
  é lido do mirror até 1 MiB, um `go.mod` até 64 KiB e no máximo 200
  deles; um maior é ignorado uma vez por commit com um aviso que nomeia o org e o
  namespace, e os imports que ele teria resolvido permanecem não resolvidos. Fora de uma passagem do worker, o resolvedor lê um arquivo a partir de seus chunks indexados, que contêm apenas arquivos
  dentro de `LOOM_CODE_INGEST_MAX_FILE_BYTES` (qualquer tamanho com 0). Os re-exports de um barrel são lidos
  sob o mesmo limite de arquivo. Um arquivo binário ou da denylist acima do limite recebe tombstone como
  um abaixo dele, por seu caminho e seus primeiros 8 KiB; um arquivo gerado recebe a linha de
  skipped, sem declarações.
- **As exclusões do próprio repositório são limitadas e falham de forma aberta.** `.loomignore` e
  `.gitattributes` são lidos até 1 MiB e dão no máximo 1,000 padrões entre os dois. Um
  arquivo maior não dá nenhum padrão, e os padrões além do 1,000º são descartados, então os caminhos
  que eles excluiriam são indexados; o log nomeia o arquivo ou a contagem. Os globs `exclude`
  da própria entrada nunca são cortados, então um caminho que deve ficar de fora pertence
  a eles.
- **Uma aplicação com falha** deixa seu lote como `failed` com o erro;
  `loom code scip-retry <batch_id>` o redefine para pending.
- **Producers manuais e de CI** ficam em `scripts/scip_harness/` (`run_delta.sh`,
  `run_typescript_delta.sh`, `run_python_delta.sh`, `run_go_monorepo_delta.sh`,
  `scip_to_delta.py`; veja o README).

<a id="repository-job-sandbox"></a>

### Sandbox de jobs do repositório

**Camadas baseadas em build executam código do repositório.** A camada de C/C++ executa o próprio
`cmake` configure do repositório (um `CMakeLists.txt` é um programa), a camada de Go executa `go mod download`
sobre o grafo de módulos do repositório, e a camada de TypeScript instala os pacotes que o
lockfile dele nomeia. Trate toda execução de uma camada do compilador como a execução de código
escrito pelos autores do repositório, no host que a executa.

O Loom executa cada job de repositório (clone, fetch e `ls-remote`, `git submodule update`,
instalações de dependências e os harnesses da camada do compilador) dentro de um único limite construído sobre
[bolha de proteção (bubblewrap)](https://github.com/containers/bubblewrap)
(`server/code/app/index/sandbox.py`) quando o host consegue executá-lo:

- namespaces próprios de user, pid, network, mount, ipc e uts, sem capabilities, sem novos user
  namespaces, e um filtro seccomp que recusa `ptrace`, `mount`, `unshare`, `setns`,
  um `clone` com `CLONE_NEWUSER`, `bpf`, `perf_event_open`, keyrings do kernel, carregamento de módulos e
  kexec, `userfaultfd`, `open_by_handle_at` e `process_vm_readv`/`writev`
  (`clone3` responde `ENOSYS`, de modo que a biblioteca C recorre a `clone`);
- apenas os diretórios do sistema, as toolchains e os caminhos do próprio job são montados. Uma
  toolchain só é montada onde não expõe nenhum dado do Loom: nunca um diretório que
  seja ou contenha `var/`, os mirrors e checkouts, o arquivo env, a
  raiz de instalação ou `HOME`, nem um dentro de `var/` ou de `HOME` (o
  `var/bin/scip-clang` padrão é montado como esse único arquivo). Quando um desses caminhos está
  dentro de qualquer outro diretório montado (um mirror dentro do prefixo Python do
  servidor, `var/` sob um caminho do sistema), uma montagem vazia o cobre dentro do sandbox.
  A raiz de instalação é a exceção, pois o Python do servidor pode executar a partir dela. Um host
  cujo prefixo Python ou um caminho do sistema é, ou está dentro de, um dos caminhos de dados do Loom
  não executa nenhum job em sandbox: nenhuma montagem vazia poderia ocultar esses dados, e `/v1/health` e
  `loom doctor` reportam `repo_sandbox_layout_refused`. O checkout (ou o
  mirror bare, no caso de um fetch) e o diretório de saída são graváveis, o `.git` do checkout
  e os scripts do harness são somente leitura, e `/tmp` e `HOME` são um diretório
  novo removido após o job. Sem ambiente do servidor, credenciais, `HOME` do host, socket do Docker
  ou outros caminhos do host;
- rlimits, definidos por `prlimit`: `LOOM_CODE_SANDBOX_CPU_SECONDS` (7200),
  `LOOM_CODE_SANDBOX_MEMORY_MB` (16384) e `LOOM_CODE_SANDBOX_MAX_FILE_MB` (32768),
  definidos antes de o bwrap iniciar, limitam **cada processo** de um job (tempo de CPU, memória de dados, tamanho
  de arquivo), não o total do job. `LOOM_CODE_SANDBOX_MAX_PIDS` (2048) limita os processos
  e threads de **um job**: ele é definido dentro do user namespace do job, onde o
  kernel conta apenas as tarefas desse job (Linux 5.14 e posteriores; kernels mais antigos contam as
  tarefas do uid em todo o host). Fora dele, RLIMIT_NPROC contaria todas as tarefas do uid no
  host, inclusive as de outros contêineres. O sandbox executa o `prlimit`
  encontrado no `PATH` do servidor, portanto ele deve estar sob um caminho do sistema montado, como
  `/usr`. O Loom não define cgroup; dê ao contêiner seus próprios limites de memória e pids para um
  total rígido. As cores definem `pids_limit: 8192` em cada contêiner como essa salvaguarda.
  Cada job também tem um timeout de relógio que mata toda a sua árvore; o pid namespace do job
  garante que nenhum processo iniciado por ele sobreviva a ele;
- saída limitada no servidor: o stderr de um job, e o stdout de um build, instalação ou
  clone, mantêm apenas o último 1 MiB; um comando cujo stdout o Loom analisa (`ls-remote`,
  fetch) pode imprimir até 64 MiB, e acima disso o job inteiro é morto e falha. Um arquivo
  que o servidor lê de volta de um job ou de um checkout é um arquivo regular, nunca um link: o
  delta de uma camada do compilador até `LOOM_CODE_SCIP_DELTA_FILE_MAX_MB` e
  `LOOM_CODE_SCIP_DELTA_MAX_NODES`, o `package.json` do checkout até 1 MiB e o `yarn.lock` de cada
  raiz de instalação até 64 MiB;
- sem rede, exceto para clones, fetches, submodules e downloads de dependências, e então
  apenas por meio de um proxy de egress fora do sandbox. Ele aceita `CONNECT` para as portas 443,
  22 e a porta do próprio remote, resolve o nome (apenas a partir de `/etc/resolv.conf`: não
  `/etc/hosts` ou nsswitch, e sem search domains, de modo que um remote precisa do nome DNS completo),
  recusa a conexão a menos que **todos** os endereços
  sejam públicos (loopback, faixas privadas, link-local e o endereço de metadados da nuvem
  são recusados, inclusive quando alcançados por um redirect ou por uma URL de `.gitmodules`) e
  conecta ao endereço que verificou. HTTP simples não tem rota. Um job mantém no máximo 32
  túneis por vez. Um cliente dentro do sandbox alcança o proxy por um relay local
  (seu proxy HTTPS), que faz ponte de no máximo 32 conexões; mais uma espera até que uma ponte
  termine. Qualquer outra conexão acima do limite no proxy é recusada após
  `LOOM_CODE_EGRESS_SLOT_WAIT_S` (5 s por padrão), e o mesmo vale para uma conexão do relay enquanto
  túneis ssh ocupam alguns dos slots. O proxy registra no log a primeira recusa, e o total
  quando o job termina, se houve mais de uma.

  Um túnel sem nenhum byte em qualquer direção por `LOOM_CODE_EGRESS_IDLE_TIMEOUT_S` (5 minutos por
  padrão) é fechado. Depois que seu cliente envia EOF, ele é fechado após
  `LOOM_CODE_EGRESS_HALF_CLOSE_TIMEOUT_S` (10 s por padrão) sem um byte do
  servidor; enquanto ainda há bytes aguardando um cliente lento, vale o timeout de inatividade. Um
  túnel cujo cliente enviou EOF antes de qualquer byte trafegar (uma conexão que ele abandonou) é
  fechado imediatamente. Depois que o lado do proxy de um túnel terminou (o servidor o fechou ou
  o reiniciou, ou o proxy o fechou), o relay encerra sua conexão após pelo menos um
  segundo sem um byte do cliente. Um cliente que nunca lê o que lhe foi enviado
  mantém sua conexão de relay, e portanto seu slot de relay, por até o timeout de inatividade.

Todo git que um job de repositório executa não lê nenhuma configuração git de sistema ou global e permite apenas
os transportes `https` e `ssh` (`protocol.allow=never`); `ext::` e `file://` são
recusados, assim como reescritas de URL. O token de acesso da entrada chega apenas ao seu clone, aos seus
fetches e ao `git submodule update` da camada de C/C++, nunca a um build ou a `go mod
download`, e o git o envia apenas a URLs sob o proprietário do remote
(`https://host/owner/`; o host inteiro para um remote sem caminho de proprietário). Dentro do
sandbox, um remote `ssh` não tem chave SSH do host: use um remote HTTPS com um token de acesso.

**Quais stacks executam o sandbox.** As cores blue/green que o `redeploy.sh` implanta
(`dev/docker-compose.colour.yml`) definem as opções de contêiner abaixo. Os outros arquivos Compose
(`compose.yaml`, `dev/docker-compose.cloud.yml`) não as definem: um
perfil AppArmor que o host não carregou impede o contêiner de iniciar, então eles mantêm
os padrões do Docker, a sonda falha e todo job de repositório executa como um processo comum (veja
_Sem o sandbox_). Para executá-lo nesses stacks, depois que o host tiver o perfil:

- copie o bloco `security_opt` do arquivo das cores e seu `pids_limit` (a salvaguarda
  do contêiner). O caminho `seccomp=` é relativo ao diretório do projeto (a pasta do arquivo
  compose, a menos que `--project-directory` diga outra coisa): `./seccomp/…` funciona
  para os arquivos em `dev/`, e o `compose.yaml` precisa de
  `./dev/seccomp/loom-repo-sandbox.json`;
- o bundle standalone da release (`loom-stack.tar.gz`) não inclui nem o arquivo seccomp
  nem o perfil AppArmor: pegue ambos da árvore de código-fonte da mesma release e aponte
  `seccomp=` para onde você colocou o JSON;
- `redeploy.sh` verifica apenas o compose das cores. Execute `dev/apparmor_check.sh <compose file>`
  manualmente antes de iniciar outro stack, e também a verificação com contêiner descartável abaixo.

Uma execução a partir do código-fonte (`make server`) tem o sandbox apenas onde o host atende aos mesmos
requisitos.

**Quais hosts podem carregar o perfil.** O perfil distribuído tem como alvo o AppArmor 4.0 ou posterior
(`abi <abi/4.0>`). Apenas o Ubuntu 24.04 é testado, pelo job `sandbox-profile` do CI com
`kernel.apparmor_restrict_unprivileged_userns=1`; o parser 4.x do Debian 13 aceita a
ABI, mas o perfil não é testado nele. Hosts sem AppArmor, como o Docker Desktop
(em qualquer SO) ou distribuições com SELinux, como Fedora e RHEL, não podem usar com segurança as
opções das cores: o Docker ignora `apparmor=` nesses hosts, então nada substitui as máscaras de `/proc`
que `systempaths=unconfined` remove (veja _`/proc`_ abaixo). Parsers mais antigos (Ubuntu 22.04,
Debian 12) rejeitam o perfil; esses hosts só podem executar o sandbox com
`seccomp=unconfined` e `apparmor=unconfined` (o parágrafo antes de _Sem o
sandbox_), o que perde a mesma proteção de `/proc`. O stack da release executa em todos
esses hosts sem o sandbox.

A menos que `LOOM_CODE_SANDBOX_REQUIRED` esteja definido, todo host ainda indexa os repositórios
listados em `LOOM_CODE_REPOS_JSON`; apenas repositórios adicionados pela página precisam do
sandbox. _Sem o sandbox_ diz o que isso executa, e o
[guia de início rápido](../getting-started/quickstart.md#optional-index-a-repository-on-the-released-stack)
tem os comandos do stack da release.

**Requisitos.** `bwrap` e `prlimit` (util-linux) no `PATH` (a imagem tem ambos) e
user namespaces sem privilégios. Os padrões do Docker bloqueiam o bwrap de três formas, e o compose das
cores relaxa cada uma da maneira mais restrita possível:

```yaml
security_opt:
  - seccomp=./seccomp/loom-repo-sandbox.json
  - apparmor=loom-repo-sandbox
  - systempaths=unconfined
```

- **seccomp.** O perfil padrão do Docker permite `clone` com flags de namespace, `unshare`,
  `mount` e `umount2` apenas a um contêiner com `CAP_SYS_ADMIN`, e `pivot_root` a nenhum
  contêiner. `dev/seccomp/loom-repo-sandbox.json` é o perfil padrão do Docker
  (moby/profiles `seccomp/v0.2.3`, o que o Docker 29 distribui) mais uma regra que permite essas
  cinco a todo processo do contêiner; o bwrap precisa de todas elas. Dentro do sandbox,
  o filtro próprio do Loom recusa ao job `unshare`, `mount`, `umount2`, `pivot_root` e um
  `clone` com `CLONE_NEWUSER`. Um `clone` que peça qualquer outro novo namespace passa pelo
  filtro e falha por falta de capability, pois o bwrap executa o job com
  `--cap-drop ALL`.
- **AppArmor.** O `docker-default` tem `deny mount`. `apparmor=unconfined` não ajuda
  em hosts que restringem user namespaces sem privilégios (Ubuntu 23.10 e posteriores,
  `kernel.apparmor_restrict_unprivileged_userns=1`): ali um processo unconfined que
  cria um user namespace é movido para o perfil `unprivileged_userns`, que
  recusa as montagens. Em `deploy/apparmor/loom-repo-sandbox`, todo processo do
  contêiner executa sob `docker-default`, renomeado, com `deny mount` mantido e, na ABI
  4.0, sem regra `userns`, de modo que user namespaces são recusados. Apenas um exec de
  `/usr/bin/bwrap` passa para o perfil filho `loom-repo-sandbox//bwrap`, que é
  o mesmo mais `userns, mount, pivot_root`. O bwrap e tudo o que ele inicia permanecem no
  perfil filho, então um job de repositório tem essas permissões pelo AppArmor e as vê recusadas pelo
  filtro seccomp do Loom acima. O limite é o exec do bwrap: o servidor e as
  ferramentas que ele executa fora do sandbox não podem fazer unshare nem mount, mas qualquer processo no
  contêiner que possa executar `/usr/bin/bwrap` pode criar user namespaces e montar dentro
  deles, sem o filtro do Loom.
- **`/proc`.** O Docker mascara caminhos sob `/proc` e `/sys`, e o kernel então recusa
  a montagem de um procfs novo que o sandbox faz; `systempaths=unconfined` remove as máscaras. Isso
  também remove as montagens somente leitura sobre `/proc/sys`, `/proc/sysrq-trigger`, `/proc/irq`,
  `/proc/bus` e `/proc/fs`, e as máscaras sobre `/proc/kcore`, `/proc/keys`,
  `/sys/firmware` e o restante. Em ambos os perfis, as regras do `docker-default` ainda recusam
  gravações em `/proc/sys`, `sysrq-trigger` e nos outros arquivos de sistema sob `/proc` e
  `/sys`, e qualquer acesso a `kcore` e `/sys/firmware`. Em um host sem AppArmor,
  o Docker ignora a opção `apparmor=` e nada toma o lugar das montagens, de modo que
  o `redeploy.sh` recusa esse host.

**`--disable-userns`.** O perfil filho mantém as negações de gravação em `/proc/sys` do `docker-default`,
que recusam a gravação de sysctl que o `--disable-userns` do bwrap faz. Uma regra de permissão
não pode sobrepor uma negação, e restringir os globs de negação para as duas formas que o caminho pode assumir
os tornaria difíceis de revisar. Portanto, sob este perfil, inclusive em produção, espera-se que a sonda
veja o bwrap citar `max_user_namespaces`, registre isso em INFO e execute o bwrap sem
a flag; o filtro do Loom recusa novos user namespaces por conta própria. Qualquer outra falha da sonda
é registrada em WARNING e deixa o sandbox desligado.

**Carregando o perfil.** Carregue-o uma vez no host Docker; `/etc/apparmor.d` é carregado
novamente a cada boot:

```bash
sudo install -m644 deploy/apparmor/loom-repo-sandbox /etc/apparmor.d/loom-repo-sandbox
sudo apparmor_parser -r -W /etc/apparmor.d/loom-repo-sandbox
```

**Verificando.** `apparmor_parser -Q -K` aceita o perfil, mas só se sabe se o bwrap executa
sob ele depois que um host o carrega. O CI é onde isso acontece primeiro: o
job `sandbox-profile` em `.github/workflows/docker-smoke.yml` carrega o perfil no
Ubuntu 24.04 com `kernel.apparmor_restrict_unprivileged_userns=1` mantido ligado e executa a
sonda e a suíte hostil na imagem de produção com as opções das cores. Em um host de deploy, verifique em um contêiner descartável a partir do build prestes a ser implantado, não
do que está servindo (`True` significa que o sandbox executa):

```bash
docker build -q -t loom:sandbox-check .
docker run --rm --network none --user loom -w /app --entrypoint python \
  --security-opt seccomp=dev/seccomp/loom-repo-sandbox.json \
  --security-opt apparmor=loom-repo-sandbox --security-opt systempaths=unconfined \
  loom:sandbox-check -c 'from server.code.app.index import sandbox as s; print(s.sandbox_available())'
docker image rm loom:sandbox-check
```

**O que o `redeploy.sh` verifica.** Antes de construir uma cor, ele para quando o perfil não
está carregado, e em um host sem AppArmor. Depois que a nova cor inicia, e antes de o
proxy ser trocado para ela, `dev/sandbox_probe.sh` executa a mesma sonda em cada um dos
contêineres da cor que executam sob o perfil (`loom-<colour>` e
`loom-<colour>-workers`). Quando uma falha, ou não pode ser inspecionada, ele imprime o motivo, remove
os contêineres da nova cor e sai com a cor antiga ainda servindo. Removidos, não
parados: o build já substituiu o build anterior nessa cor, então ele não é alvo de
rollback, e seus workers não poderiam obter o lease e executar jobs sem sandbox. Essa é
a primeira verificação do perfil real em um deploy que pulou a verificação acima.

**Exigindo o sandbox.** A sonda controla a troca, mas os novos workers iniciam antes
dela e podem obter o lease do supervisor (em um primeiro deploy, ou quando o lease antigo está livre)
e iniciar jobs de repositório. `LOOM_CODE_SANDBOX_REQUIRED=true`, que as cores definem,
fecha essa janela: onde o sandbox não pode executar, nenhum job de repositório (clone, fetch,
instalação de dependências, camada do compilador, harness do `loom code scip-full`) executa como um processo
comum. A passagem de ingestão e as camadas do compilador ignoram toda entrada, inclusive as do
operador, e nomeiam o motivo na nota da passagem; `loom code scip-full` sai com 1; qualquer job
que ainda alcance o runner falha com `SandboxRequiredError`. `/v1/health` reporta
`repo_jobs_unsandboxed` como erro. O primeiro clone bare do `loom code init` é o
clone do worker, um job de repositório como os demais: ele também sai com 1 com a recusa.

**Desligando.** Para executar as cores sem o sandbox novamente, remova tanto o bloco
`security_opt` quanto `LOOM_CODE_SANDBOX_REQUIRED` dos dois serviços em
`dev/docker-compose.colour.yml`, e então faça o redeploy. Remover apenas `security_opt` deixa
todo job de repositório recusado e `/v1/health` degradado.

Sem `no-new-privileges`: sob ele o kernel recusa a transição de exec para o perfil filho,
e o bwrap não iniciaria. Os binários setuid da imagem (`su`, `mount`,
`passwd` e semelhantes) portanto ainda funcionam no contêiner, como sob os padrões do Docker;
as regras do perfil, e não as montagens somente leitura, os mantêm longe de `/proc/sys`.

Essas opções ainda enfraquecem o confinamento do próprio contêiner: o seccomp permite que todo processo do contêiner faça as cinco chamadas (fora do bwrap, o AppArmor e a ausência de `CAP_SYS_ADMIN` ainda as recusam); o AppArmor permite que o bwrap e o que ele executa criem namespaces de usuário e montem sistemas de arquivos dentro deles, e qualquer processo que consiga executar o bwrap recebe essa permissão, exceto um job de repositório (o filtro do Loom); e `/proc` e `/sys` passam a depender das regras do perfil em vez das máscaras do Docker. Pese isso contra executar código de repositório sem o sandbox. `seccomp=unconfined` e `apparmor=unconfined` também funcionam onde o host não restringe namespaces de usuário, mas descartam o perfil inteiro.

**Sem o sandbox**, o Loom faz um teste uma vez por processo, registra um aviso no log e informa `repo_jobs_unsandboxed` em `/v1/health` e em `loom doctor`. Com `LOOM_CODE_SANDBOX_REQUIRED=true`, todo job de repositório é recusado (acima). Caso contrário:

- os repositórios do operador (`LOOM_CODE_REPOS_JSON`) mantêm todas as camadas, e seus passos de build rodam como processos comuns no host, como antes;
- os repositórios autorregistrados cujo remoto é uma URL de rede não são clonados nem buscados e não recebem a camada do compilador: sem o proxy de saída, nada validaria os hosts e as portas aos quais um job se conecta. A nota da passagem de ingestão e `workers.status` informam o que foi ignorado; a entrada é retomada quando o sandbox estiver disponível;
- os repositórios autorregistrados em um caminho local mantêm a camada do parser e as camadas estáticas do compilador, mas não recebem nenhuma camada baseada em build (C/C++);
- adicionar um repositório ou um branch pelo painel é recusado com `SANDBOX_UNAVAILABLE`.

Cada entrada Go tem seu próprio cache de módulos em `LOOM_CODE_SCIP_GO_MODCACHE` (`<root>/<org>/<namespace>`): o `go` não verifica de novo as somas dos módulos que já estão no cache, então um cache gravado pelo job de um repositório nunca é usado pelo job de outro.

<a id="mirror-housekeeping"></a>

### Manutenção dos espelhos

Cada processo servidor executa `git gc --no-prune` nos espelhos do seu host a cada `LOOM_CODE_MIRROR_GC_INTERVAL_H` (com baixa prioridade de CPU e de IO, e somente se houver espaço para uma segunda cópia mais `LOOM_CODE_MIRROR_GC_MARGIN_GB`, 5 GB por padrão; sem esse espaço, o gc espera outro intervalo e registra um aviso no log). Também varre `LOOM_CODE_REPOS_ROOT`, removendo os espelhos, as árvores `checkout-*` e os arquivos de lock de espelho que nenhuma entrada ativa nomeia, de modo que um repositório removido sai de todos os hosts, e não apenas daquele que `code.repo.remove` alcançou. Os gauges `loom.host.disk.free_bytes` / `loom.host.disk.total_bytes` são a entrada do alarme de disco.

<a id="measured-coverage"></a>

## Cobertura medida

Um repositório cujo CI executa um build instrumentado com cobertura de linhas por teste pode alimentar o mapa de testes com o que cada teste executou, e não apenas com o que ele nomeia. A importação lê a execução completa mais recente do servidor ClickHouse que contém as tabelas `checks_coverage_lines` e `checks` do CI, em `LOOM_CODE_COVERAGE_SHARDS` fatias por hash de arquivo. Uma fatia precisa caber na memória por consulta desse servidor. Um servidor com cota por hora para cada formato de instrução também precisa comportar uma importação inteira (duas instruções por fatia) mais uma nova tentativa. Um playground público permite 100 instruções por formato por hora; as 16 fatias padrão usam 16 de cada, então uma execução de teste (dry run) e uma execução real cabem na mesma hora.

Ela grava:

- uma linha de `code_test_map` por (arquivo-fonte, teste), com `method = coverage`, quando o teste executou uma linha alcançada por no máximo `LOOM_CODE_COVERAGE_SPECIFIC_MAX_TESTS` testes (uma linha que toda consulta executa não nomeia nenhum teste); `confidence` diminui conforme o número de testes que compartilham a linha mais específica do par, e `evidence` guarda o commit da execução e os intervalos de linhas específicas;
- uma linha de `code_coverage_files` por arquivo-fonte, com todos os intervalos executados por qualquer teste, de modo que `code.coverage_claim` consiga distinguir "executado por muitos testes" de "executado por nenhum".

As linhas que a execução mais recente não tem mais recebem marca de exclusão (tombstone), e isso só ocorre depois que todas as fatias foram lidas. `code.coverage_claim` classifica uma linha medida como `executed`, acima de todas as outras camadas, quando seus intervalos encontram o trecho do assunto; um assunto cujo arquivo a execução cobriu, mas cujas linhas nenhum teste executou, é `uncovered`, com a medição em `measured`.

```bash
LOOM_CODE_COVERAGE_SOURCE_URL=https://<coverage server>
LOOM_CODE_COVERAGE_SOURCE_USER=<reader>
LOOM_CODE_COVERAGE_NAMESPACES=<namespace>[,<namespace>]
loom code coverage-import --namespace <namespace> --dry-run   # counts only, writes nothing
loom code coverage-import --namespace <namespace>
LOOM_CODE_COVERAGE_IMPORT_ENABLED=true                         # then daily, in the workers
```

<a id="another-branch"></a>

## Outra branch

Um namespace acompanha uma branch, e uma branch só é indexada quando uma entrada a nomeia. Adicione uma entrada com o mesmo `repo` e um `namespace` e `git_branch` diferentes; ela compartilha o clone bare. Defina `embed_reuse_from` com o namespace da branch principal para que chunks idênticos reutilizem os embeddings. Em seguida, execute `loom code init` e `loom code backfill` para o novo namespace, como no primeiro índice.

<a id="repairs"></a>

## Reparos

`loom code reconcile --namespace NS` lista os caminhos indexados cujos chunks são anteriores à última
alteração no head do branch; `--heal` os reingere sem mover a marca d'água.

**Após uma mudança no extrator.** O código dos módulos em `EXTRACTOR_MODULES`
(`server/code/domain/extract/extractor_stamp.py`) e as versões das gramáticas formam o
carimbo de versão do índice. O carimbo faz hash apenas dos tokens de código; edições em comentários e docstrings
não o alteram. Quando um deploy move o carimbo, `code_ingest` rederiva os arquivos ativos de
cada namespace nas famílias de linguagens que a mudança afeta,
`LOOM_CODE_EXTRACTOR_RECHUNK_PER_PASS` arquivos por passagem, e executa apenas as etapas (chunk,
symbol, edge, guard) cujo carimbo mudou. `code.index_status` mostra o progresso nas
chaves de estado `extractor_stamp*`. Em um índice grande, isso leva horas.

**Após uma mudança fora do carimbo** (uma faixa de linguagem, uma faceta, um novo tipo de arquivo),
as linhas existentes não são corrigidas automaticamente. Reingira os caminhos afetados na
marca d'água do próprio namespace:

```bash
HEAD=$(docker exec loom-workers loom code stats --namespace "$NS" | awk '$1 == "git_head" {print $2}')
git --git-dir var/code_repos/<name>.git ls-tree -r --name-only "$HEAD" | grep -E '\.(sql|sh)$' > /tmp/$NS-paths.txt
docker cp /tmp/$NS-paths.txt loom-workers:/tmp/
docker exec loom-workers loom code backfill --namespace "$NS" --commit "$HEAD" --no-fetch --embed \
  --paths-file /tmp/$NS-paths.txt
```

`--paths-file` é o modo de reparo: reingere apenas os caminhos listados e deixa a
marca d'água intacta. Arquivos cujo conteúdo não mudou mantêm seus chunks e embeddings;
símbolos, facetas e arestas são rederivados. Adicione `--edges-second-pass` quando muitos arquivos ganharem
símbolos de uma só vez.

As faixas opcionais são ativadas por configurações; depois de ativar uma, execute o reparo nos
caminhos correspondentes:

| Configuração                      | Padrão  | Faixa                                                                               |
| --------------------------------- | ------- | ----------------------------------------------------------------------------------- |
| `LOOM_CODE_SQL_STATEMENT_RECORDS` | off     | arquivos `.sql` como um símbolo `statement` por instrução, com facetas de dialeto e tabela |
| `LOOM_CODE_SHELL_ISLANDS`         | off     | SQL emitido por scripts de shell como registros de instrução                        |
| `LOOM_CODE_PY_SQL_ISLANDS`        | off     | strings SQL em chamadas Python como registros de instrução                          |
| `LOOM_CODE_HOST_ISLANDS`          | off     | workflows de CI, Dockerfiles e Makefiles: jobs, estágios e alvos como símbolos      |

`scripts/ops/code_edge_audit.py` é um censo somente leitura dos dois buracos silenciosos do grafo de chamadas
da camada do parser (pontos de chamada recusados cujo callee agora tem uma definição, e arestas
de baixa qualidade cujo destino não existe mais); execute-o com `LOOM_BACKGROUND_WORKERS=off` em uma cópia
de staging.

<a id="maintenance-commands"></a>

## Comandos de manutenção

| Comando                                      | O que faz                                                                                                                                                                                                                                                                                                               |
| -------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `loom code compact [--table T] [--apply]`    | mede e, com `--apply`, executa `OPTIMIZE … FINAL` em uma tabela de vetores de código quando suas cópias físicas por chave atingem `LOOM_CODE_COMPACT_MIN_RATIO` (1.5), e aguarda o merge; as passagens em lote fazem isso sozinhas depois de `LOOM_CODE_COMPACT_AFTER_BULK_ROWS` linhas ([manutenção](maintenance.md#compaction-and-optimize)) |
| `loom code warm <questions.txt>`             | faz cada pergunta uma vez para que as respostas de `code.ask` fiquem em cache para todos (linhas de texto ou JSONL com `question`); os acertos são gratuitos, então executar novamente é seguro                                                                                                                         |
| `loom code answers-purge`                    | aposenta as respostas de `code.ask` em cache após uma mudança no modelo de síntese (caso contrário, o cache só é invalidado quando um arquivo citado muda); simulação até usar `--apply`                                                                                                                                |
| `loom code repack`                           | reconstrói os pacotes de contexto dos PRs mesclados recentemente no formato atual                                                                                                                                                                                                                                       |
| `loom code crossref`                         | a passagem de referência entre teste SQL/shell e o C++ que o implementa (idempotente)                                                                                                                                                                                                                                   |
| `loom code coverage-import [--dry-run]`      | importa a execução mais recente de cobertura de linhas por teste da fonte configurada para o mapa de testes ([cobertura medida](#measured-coverage))                                                                                                                                                                    |
| `loom code reembed`, `loom code vector-twin` | gravam os chunks de um namespace sob o rótulo de um segundo modelo de embedding, ou preenchem um gêmeo de prefixo Matryoshka dos vetores, dentro do ClickHouse; retomável                                                                                                                                                |
| `loom code lint-instructions`                | lista caminhos e referências `Qualified::symbol` em `CLAUDE.md`, `AGENTS.md`, `.claude/**` e skills que não resolvem mais no head; `--fail-on-stale` para CI                                                                                                                                                             |
| `loom code export-matrix`                    | grava, para um arquivo de instruções, uma tabela "quando este arquivo muda, estes geralmente mudam também", a partir da mudança conjunta em PRs mesclados                                                                                                                                                                |
| `loom code export-pairs`                     | exporta pares de recuperação (consulta → unidade utilizada, negativos difíceis) a partir de tráfego real, para ajuste de embedder ou reranker                                                                                                                                                                           |

`make bench-code-intel LOOM_BASE_URL=…` executa o portão de qualidade do índice de código
(`bench/code_intel_qa.py`) contra um servidor com provedores reais, para um repositório que
tenha um conjunto de perguntas de referência; ele nunca é executado no CI, cujo embedder é falso
([testes de desempenho](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/running.md)).

<a id="rollout-and-shutdown"></a>

## Implantação e desligamento

- `LOOM_MCP_CODE_TOOLS` (desativado por padrão) lista as ferramentas `code.*` no conjunto de ferramentas MCP padrão. O conjunto de ferramentas `coding` as lista independentemente dessa variável, e o HTTP e os SDKs sempre podem chamá-las, de modo que você pode avaliar um índice antes de expô-lo a clientes de chat.
- `LOOM_CODE_INTEL_ENABLED=false` faz com que toda operação `code.*`, exceto `code.repo.remove`, retorne um erro de pré-condição. Também interrompe os workers de importação de cobertura e de notificação e o loop do host de mirror; cada um dos outros workers de código tem seu próprio interruptor ([workers de segundo plano](https://github.com/ClickHouse/loom/blob/main/docs/reference/workers.md)).
- **Desativar um repositório.** Um mantenedor usa **Retire…** na aba Access do repositório (`code.repo.remove` com `confirm` definido como o namespace). A entrada fica inativa, uma saga de eliminação apaga todas as tabelas `loom.code_*` do namespace, e o mirror bare é excluído quando nenhuma outra branch o usa. A telemetria de busca é mantida. Uma entrada configurada pelo operador é recusada com `REPO_FROM_CONFIG`: remova-a de `LOOM_CODE_REPOS_JSON`. Com `LOOM_CODE_INTEL_ENABLED=false`, a página não consegue listar repositórios, mas `code.repo.remove` via HTTP, MCP ou SDK continua sendo atendido; nesse caso, as outras réplicas só limpam a própria cópia do mirror quando a inteligência de código for ativada novamente.
- **Apagar um índice.** `erasure.request` com `scope_type = namespace` em um namespace de código também apaga as tabelas `loom.code_*`. Uma entrada que ainda está configurada é reindexada do zero na próxima passagem, portanto desative-a primeiro.

<a id="when-a-tier-fails"></a>

## Quando uma camada falha

Cada camada que mantém uma entrada atualizada (o fetch do mirror, o ingest do parser e toda camada do compilador) registra seu último resultado na entrada: quando falha, o erro com as credenciais ocultadas, um tipo de erro, a única correção para esse tipo, `failing_since` e o número de execuções consecutivas que falharam. A próxima execução que for bem-sucedida limpa o registro. Uma camada executada a partir do cron registra da mesma forma quando roda sob `loom code entry-lock --tier <tier>`; `loom code scip-delta` e `scip-full` registram por conta própria.

Um módulo Go cujas dependências este host não consegue buscar (o Go informa um repositório privado, muitas vezes os próprios módulos do repositório por meio de um caminho de importação vanity) não é uma falha: a camada Go indexa o restante e registra a camada como `unavailable`, tipo `dependency_unavailable`, com a correção (defina `LOOM_CODE_SCIP_GO_PRIVATE` e forneça ao usuário da camada uma credencial git para esse host; o Go diz o mesmo sobre um caminho ou uma versão que está errada ou não existe mais). A nota é escrita uma única vez, não conta execuções, nunca é um problema `code_tier_failing` e não aparece em `as_of.failing_tiers`, de modo que a resposta de código de um agente não sinaliza esses módulos. Ela reflete a última execução que compilou módulos: uma execução que não compila nada a mantém, e a próxima execução que compilar sem essa falha a limpa, de modo que ela volta quando um módulo que ainda não consegue buscar sua dependência muda novamente. Sob `entry-lock --tier go`, que enxerga apenas o código de saída e a saída do seu comando, uma saída limpa sem essa falha a limpa. Um módulo que falha por qualquer outro motivo ainda faz a camada falhar, e o registro dela nomeia também os inacessíveis.

O registro é lido onde cada leitor já olha:

- as respostas de código de um agente o trazem em `as_of.failing_tiers`
  ([atualização dos dados](../features/code-index/freshness.md#the-as_of-block));
- `code.index_status` e `code.repos` o retornam em `tier_health`, e o cartão do repositório no painel mostra a camada com falha, desde quando, quão atrasada está e a correção, além de qualquer camada `unavailable` com sua correção;
- quando uma camada falha por `LOOM_CODE_TIER_FAILING_WARN_PASSES` execuções consecutivas ou por `LOOM_CODE_TIER_FAILING_WARN_HOURS`, ela se torna o problema `code_tier_failing` em `/v1/health`, no banner do painel e em `loom doctor` ([monitoramento](monitoring.md#v1health)): uma linha por entrada e camada com falha, até cinco, com o motivo, e cada correção distinta uma vez. `loom doctor`, que lê com as credenciais do próprio deployment, também nomeia cada entrada que lista, até cinco, como repositório@branch (org/namespace) com a linha de erro que explica o motivo; `code.repos` nomeia as demais. O token de um administrador da plataforma lê o mesmo para toda entrada que não seja [restrita](https://github.com/ClickHouse/loom/blob/main/docs/architecture/security.md#restricted-namespaces); uma restrita aparece como "a restricted entry", assim como uma cuja classificação não pode ser lida. Qualquer outro chamador não lê nenhuma entrada, já que `/v1/health` responde sem token;
- para alertas por push, os gauges `loom.code.tier_failing` e `loom.code.tier_lag_commits` trazem cada camada que o problema `code_tier_failing` reporta ([telemetria](../features/observability/telemetry.md)), com um alerta em `deploy/dashboards/code_plane.json` e, para o Prometheus, a regra do chart ([regras de alerta](kubernetes.md#alert-rules)).

Os tipos (`_KINDS`) e suas correções (`fix_for`) estão em
[`server/code/domain/index/tier_health.py`](https://github.com/ClickHouse/loom/blob/main/server/code/domain/index/tier_health.py).
Uma falha que não corresponde a nenhum tipo aparece como "unclassified error" e traz o final da saída para leitura.

<a id="failure-model"></a>

## Modelo de falhas

| Falha                                                                                                               | Comportamento                                                                                                                                                                                                                                                                            | Recuperação                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| ------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `git fetch` falha                                                                                                   | a marca d'água não se move; a camada `fetch` da entrada registra o erro, seu tipo e a correção (`tier_health`)                                                                                                                                                                           | a próxima passagem (backoff do supervisor)                                                                                                                                                                                                                                                                                                                                                                                                                                                     |
| falha no meio de um commit                                                                                          | o commit é executado de novo                                                                                                                                                                                                                                                             | upserts idempotentes                                                                                                                                                                                                                                                                                                                                                                                                                                                                           |
| um arquivo falha ao ser analisado                                                                                   | chunks sem símbolos, com `code_files.index_error` definido (inclusive uma análise além de `code_parse_timeout_s` ou `code_parse_memory_mb`, e uma que não pôde ser executada em seu processo)                                                                                             | `code.index_errors` lista esses arquivos; corrigido pela próxima alteração ou por um reparo (aumentar qualquer um dos limites não releitura nenhum)                                                                                                                                                                                                                                                                                                                                            |
| todo arquivo falha ao ser analisado com `LookupError` "... is loaded from outside the interpreter's site-packages"  | chunks sem símbolos para todos os arquivos: o tree-sitter ou uma gramática está instalado onde o processo de análise não o enxerga                                                                                                                                                       | instale os pacotes do tree-sitter no ambiente do próprio interpretador do servidor, não por meio de `PYTHONPATH` nem do site do usuário                                                                                                                                                                                                                                                                                                                                                        |
| provedor de embeddings fora do ar                                                                                   | chunks armazenados sem vetores; a busca lexical continua funcionando                                                                                                                                                                                                                     | `code_embed_recovery`                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| falha ao aplicar a camada do compilador                                                                             | lote `failed` com o erro                                                                                                                                                                                                                                                                 | `loom code scip-retry <batch_id>`                                                                                                                                                                                                                                                                                                                                                                                                                                                              |
| uma passagem da camada do compilador falha ou trava                                                                 | o timeout do harness encerra uma trava, e o prazo da passagem (esse timeout mais folga para as etapas do git e para a aplicação) cancela qualquer outro caso; o lock da entrada é liberado; uma falha que a passagem reporta (inclusive o próprio timeout do harness) é registrada com seu tipo e correção em `tier_health` | a próxima passagem; `workers.status` informa o timeout                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| base da janela da camada do compilador desconhecida (histórico reescrito, espelho podado)                           | toda camada de `scripts/scip_harness/` sai com 3, nunca com um delta vazio; os workers de Python, TypeScript e Go limpam a marca d'água da camada                                                                                                                                        | a próxima execução é uma indexação completa dessa camada (`code_scip_full` para C/C++)                                                                                                                                                                                                                                                                                                                                                                                                         |
| um delta da camada do compilador é um link, não um arquivo regular, ou é grande demais, ou o harness do Go grava mais deltas do que módulos | o delta é recusado (`FileRefused`, ou `Go harness wrote more deltas than its N module(s)`) e a passagem da entrada falha; a marca d'água permanece                                                                                                                                       | grande demais: aumente `LOOM_CODE_SCIP_DELTA_FILE_MAX_MB` e `LOOM_CODE_SCIP_DELTA_MAX_NODES` (eles limitam o trabalho e o texto de caminho mantido); para uma entrada de arquivo acima do orçamento da rodada, aumente `LOOM_CODE_SCIP_DELTA_ROUND_MAX_NODES`, dentro da memória do worker; uma entrada de arquivo acima do limite fixo de caracteres é nomeada, e nenhuma configuração altera esse limite: o que se deve dividir é o arquivo que ela indexa; um link, um arquivo não regular ou deltas extras se repetem a cada passagem até o repositório mudar ou a entrada ser removida |
| limite de taxa do GitHub                                                                                            | a passagem termina, cursor não se move                                                                                                                                                                                                                                                   | a próxima passagem                                                                                                                                                                                                                                                                                                                                                                                                                                                                             |
| branch com force-push (a marca d'água não é mais um ancestral)                                                      | marca d'água redefinida para o novo head, com registro em log                                                                                                                                                                                                                           | `loom code backfill` para derivar de novo                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| falha nas estatísticas semanais do grafo                                                                            | os rankings da semana anterior continuam sendo servidos                                                                                                                                                                                                                                  | a próxima passagem semanal                                                                                                                                                                                                                                                                                                                                                                                                                                                                     |

Os indicadores do índice de código (`loom.code.index_lag_commits`, `loom.code.stale_files`,
`loom.code.tier_lag_seconds`, `loom.code.embed_backlog`, `loom.code.files_errored`) estão
catalogados em [telemetria](../features/observability/telemetry.md); o que merece alerta está em
[monitoramento](monitoring.md#what-to-alert-on), e o painel "Loom — Code Plane" está
definido em `deploy/dashboards/code_plane.json`.

<a id="related"></a>

## Relacionados

- [Repositórios](../features/code-index/repositories.md): repositórios de autoatendimento,
  mantenedores e leitores
- [Atualização](../features/code-index/freshness.md): como um cliente lê o atraso do índice
- [Arquitetura do índice de código](../architecture/code-index-architecture.md)
