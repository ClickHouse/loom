<!-- i18n: source=docs/development/release-process.md blob=c00327d2be768494a50aa34f8828ea5bff991faf model=claude-sonnet-5-5 -->
> Esta página é uma tradução automática da [página em inglês](https://github.com/ClickHouse/loom/blob/main/docs/development/release-process.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/development/release-process.md), which is authoritative.

<a id="release-process"></a>

# Processo de release

O Loom segue SemVer: o servidor, os dois SDKs, o web app, as skills da raiz (`loom-code`, `loom`), o plugin do Cursor e o pacote Agent Plugins que as distribuem, e a extensão do Gemini CLI compartilham uma única versão `X.Y.Z`. Os plugins do Claude Code e do Codex compartilham uma versão própria, incrementada quando há mudança de comportamento em qualquer arquivo que o plugin distribui (`hooks/`, `lib/`, `bin/`, `skills/`, `.mcp.json`, o `plugin-defaults.json` do Codex): os clientes instalados só recebem uma mudança de hook por meio de uma nova versão do plugin. `MIN_SERVER_VERSION` em `lib/server-version.mjs` dos plugins indica o servidor mais antigo que os hooks suportam: quando o `GET /v1/health` do servidor informa uma `version` mais antiga, o SessionStart imprime uma linha `loom:` pedindo a atualização do servidor, e não diz nada quando a versão não pode ser lida. Ela sobe a cada release ([como fazer o release](#cutting-the-release), passo 2).

<a id="where-the-version-lives"></a>

## Onde a versão fica

`.venv/bin/python scripts/release/prepare.py bump X.Y.Z` atualiza cada um destes itens no commit de release, exceto os plugins do Claude Code e do Codex, e escreve as [notas de release](#release-notes):

| Componente                    | Arquivos                                                                                                                                                                                                                                                                                                        |
| ----------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Servidor                      | `server/__init__.py` (`__version__`), `pyproject.toml`, `uv.lock` e `server.json`                                                                                                                                                                                                                               |
| SDK Python                    | `sdk/python/pyproject.toml` e `sdk/python/loom/_version.py`                                                                                                                                                                                                                                                     |
| SDK TypeScript                | `sdk/typescript/package.json`, `sdk/typescript/package-lock.json`, `sdk/typescript/src/version.ts`                                                                                                                                                                                                              |
| Aplicativo web                | `frontend/package.json` e `frontend/package-lock.json`                                                                                                                                                                                                                                                          |
| Skills da raiz                | `.claude-plugin/plugin.json`, `.cursor-plugin/plugin.json`, `plugin.json` e o `metadata.version` de `skills/loom-code/SKILL.md` e `skills/loom/SKILL.md`                                                                                                                                                        |
| Extensão do Gemini CLI        | `gemini-extension.json`                                                                                                                                                                                                                                                                                         |
| Helm chart                    | `deploy/helm/loom/Chart.yaml`: `appVersion` (a tag padrão das imagens do servidor e do extrator, e o label `app.kubernetes.io/version` do chart) e o próprio `version` do chart: uma minor para um release major ou minor, um patch para um release patch                                                       |
| Plugins do Claude Code e Codex | `integrations/claude-code-plugin/.claude-plugin/plugin.json`, `integrations/codex-plugin/.codex-plugin/plugin.json` (versão própria; atualize-a quando houver mudança de comportamento em qualquer arquivo que o plugin distribui: `hooks/`, `lib/`, `bin/`, `skills/`, `.mcp.json`, o `plugin-defaults.json` do Codex) |

O servidor lê sua versão somente de `server/__init__.py`; o documento OpenAPI, o `serverInfo` do MCP, `loom report` e `GET /v1/health` (verificado por `tests/unit/test_health_problems.py`) a reportam. Estas verificações mantêm os números consistentes:

| Verificação                                    | Garante                                                                                                                                  |
| ---------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------- |
| `tests/unit/test_release_version.py`           | toda superfície reporta a versão do servidor, e `server.json` mantém o formato da listagem do registry                                   |
| `npm run check:version` (SDK TypeScript)       | suas quatro fontes de versão concordam                                                                                                   |
| `tests/unit/test_codex_plugin.py`              | os dois manifestos de plugin têm a mesma versão, e `MIN_SERVER_VERSION` não é mais novo que a versão do servidor                         |
| `tests/unit/test_release_prepare.py`           | todo arquivo que o `prepare.py` atualiza indica a versão do servidor                                                                     |
| `prepare.py check`, `check-tag`, `check-chart` | o job `version` do release ([etapa 1](#what-the-release-workflow-does)): uma única versão em todo lugar, a ordem dos candidatos, o chart atualizado |

Dois pull requests de plugin que atualizam para o mesmo número são mesclados sem conflito, e o segundo não entrega nada aos clientes que o primeiro já atualizou. Confira a versão do plugin na `main` antes de atualizar.

<a id="release-notes"></a>

## Notas de versão

A execução que cria o GitHub Release gera as notas a partir dos commits desde a tag da versão final anterior (`prepare.py previous`), de modo que uma versão final no commit do seu candidato também as lista; uma nova execução ou o dispatch posterior para o registry as mantém como estão. O que os commits não conseguem dizer vai em uma seção "Release notes" na descrição do pull request da versão, para que seja revisada antes da tag: as entradas de changelog dos pull requests, agrupadas por categoria de changelog (pull requests em uma categoria que não exige entrada ficam de fora), depois as etapas do operador (novas configurações, backfills, reconstruções de tabelas) e as mudanças de wire visíveis ao cliente, incluindo todo campo de requisição removido e toda flag descontinuada. Não existe um registro de campos removidos, então as notas de versão são o lugar onde um cliente fica sabendo de um deles. O `prepare.py` grava a seção em `release-notes.md` (não versionado), a partir dos squash merges desde a tag final mais alta abaixo da versão:

- a entrada de changelog de cada pull request sob sua categoria, na ordem do template, ou o título dele quando a entrada está vazia; aquele cuja descrição não indica uma única categoria é listado em "Unclassified" para o autor posicionar;
- cada item de "Operator steps", como foi escrito, sob o número do seu pull request, de todos os pull requests, inclusive um em uma categoria que não exige entrada; um item que diz que não há nenhum (`none`, `N/A`, `No operator steps.`) ou que é o próprio texto do template é omitido, e o autor remove o restante que não indica nenhuma etapa;
- o diff de wire ([geração de código](code-generation.md#wire-changelog)).

Uma correção que um patch release de uma linha mais antiga já entregou é listada novamente, assim como um pull request revertido no mesmo intervalo, ao lado do seu revert; o autor os remove.

Ele busca as tags do repositório upstream por conta própria, lê os pull requests com `gh` e o diff de wire por meio do registry do servidor, portanto exige um `gh` autenticado e o ambiente do projeto (`uv sync`). `.venv/bin/python scripts/release/prepare.py notes X.Y.Z` imprime a seção novamente sem alterar nenhuma versão: execute-o depois de fazer rebase do pull request da versão logo antes de ele ser mesclado, e de novo no commit do candidato de maior número antes da tag final, para que as notas citem todos os pull requests que a tag entrega.

<a id="cutting-the-release"></a>

## Fazendo o corte da release

1. Verifique se uma execução agendada de `.github/workflows/docker-smoke.yml` em `main` na última semana
   passou:

   ```bash
   gh run list -R ClickHouse/loom --workflow docker-smoke.yml --event schedule --branch main -L 1 --json conclusion,createdAt,headSha
   ```

   Ou inicie uma nova com `gh workflow run docker-smoke.yml -R ClickHouse/loom --ref main` e leia
   o resultado com `--event workflow_dispatch` no lugar de `--event schedule`. Ela escaneia as duas imagens como a
   release faz, e a falha de uma execução agendada notifica apenas o último editor do seu agendamento. Um
   scan vermelho indica o pacote: mova primeiro, em `main`, o pin que o traz (um `ARG` do `Dockerfile` ou o digest da imagem base,
   ou um lock file). Os pacotes da própria imagem base só mudam com um novo digest
   da imagem base; um pacote adicionado por uma camada de `apt-get install` muda em qualquer build sem cache.

2. Em um branch a partir de `main`, execute `.venv/bin/python scripts/release/prepare.py bump X.Y.Z` e abra um
   pull request com as alterações dele e com `release-notes.md` na seção [notas da release](#release-notes)
   da descrição; edite as notas onde alguma entrada precisar. O CI deve passar, incluindo
   `make schema-check` e o dry run estrito de migração.
   Quando a entrada do changelog de um pull request já mergeado disser que ele precisa da próxima release do servidor, o mesmo
   pull request eleva `MIN_SERVER_VERSION` em
   `integrations/claude-code-plugin/lib/server-version.mjs` para esta versão, sincroniza-o com o
   plugin do Codex e incrementa as versões dos dois plugins: os clientes só o recebem por uma nova versão do plugin.
3. Depois que o pull request da release for mergeado, crie a tag no commit de merge e envie a tag. Para fazer antes o soak de
   uma release candidate, crie no lugar a tag `vX.Y.Z-rc.1` nesse commit, e a final depois, no commit da
   candidata de maior número (abaixo):

   ```bash
   git tag vX.Y.Z <merge commit>
   git push origin vX.Y.Z
   ```

4. O push da tag executa `.github/workflows/release.yml` (abaixo), publica as imagens do servidor e do
   extractor e só então cria a GitHub Release, depois verifica o instalador publicado
   por fora (`release-check`). Leia essa execução antes do passo 5.
5. Para publicar os SDKs e a listagem no MCP Registry, execute o mesmo workflow manualmente na tag:

   ```bash
   gh workflow run release.yml --ref vX.Y.Z
   ```

   Quando os jobs `pypi` e `npm` dessa execução passarem, verifique os SDKs como um usuário os instala, com a
   atualização a partir da release anterior:
   `gh workflow run release-check.yml -f version=X.Y.Z -f previous_version=<previous X.Y.Z>`.
   `previous_version` deve ser uma release publicada anterior a `version` que contenha
   `install.sh` e `loom-stack.tar.gz`; omita-a quando não houver nenhuma.

6. Cole as notas da release geradas no commit da tag final ([notas da release](#release-notes)) na
   GitHub Release, acima das notas geradas.

Uma release candidate é uma tag `vX.Y.Z-rc.N` e gera uma pré-release no GitHub; a release recusa qualquer
outra tag de pré-release. A primeira candidata vai no commit de merge do pull request da release, cujos
arquivos indicam `X.Y.Z`, de modo que o servidor da candidata já reporta `X.Y.Z`; uma posterior, após uma correção,
vai em um commit posterior de `main`. A tag final `vX.Y.Z` vai no commit da candidata de maior número
após o soak. O bundle da final mantém a release do ClickHouse da candidata de maior número
nesse commit que tenha um bundle publicado; as imagens dela são construídas de novo a partir do mesmo
commit, sem cache, então um pacote do SO pode estar mais novo que na imagem do soak. A execução de uma tag
de candidata depois da final é recusada. Uma no commit da final recebe a orientação de executar a final. Uma
de número inferior ao da candidata de maior número no commit da final, como uma candidata mais antiga
disparada para publicar seus SDKs, não tem nada a executar, e a tag dela permanece. Qualquer outra é
solta: ela também faz as execuções posteriores da final falharem até ser excluída. O `appVersion` do chart também indica `X.Y.Z`,
então um soak no Kubernetes define `image.tag` e `extractor.image.tag` como `X.Y.Z-rc.N`.

Apenas a maior versão final se torna a Latest release do repositório (`releases/latest` serve o
instalador) e move `:latest`: uma pré-release ou um patch de uma linha mais antiga deixa ambos onde estão.
As versões são lidas das tags do repositório, então exclua uma tag final que não será lançada, ou
a Latest permanece abaixo dela.

<a id="rehearsing-a-release"></a>

## Ensaiando um release

Um dry run compila, verifica e executa smoke tests em tudo o que um release faria, em qualquer branch, e não publica nada: nenhum push de imagem, GitHub Release ou upload para registry.

```bash
gh workflow run release.yml --ref <branch> -f dry_run=true
```

Ele roda na versão que `server/__init__.py` indica e mantém seus artefatos por um dia (um release os mantém por 30 dias, enquanto uma execução ainda puder ser repetida). Suas imagens vão para os artefatos da execução em vez do ghcr.io, sem as atestações de proveniência e de SBOM que só um registry mantém, e o `docker-smoke` as carrega de lá. O job `preflight` informa cada [pré-requisito](#before-the-first-release) ausente como um aviso, em vez de falhar. O que só um release publicado exercita, um dry run não exercita: `docker-tag` (a junção dos dois digests de cada imagem, a decisão sobre o Latest, `:latest`), `sign`, o GitHub Release e as assinaturas de seus arquivos, as publicações nos registries, `release-check`, a verificação das imagens publicadas (`scripts/release/published_images.sh`) com o caminho de build ignorado que ela seleciona, a verificação da ordem dos candidatos (`prepare.py check-tag`, que um dry run de uma branch ignora com um aviso) e a verificação do chart quando a versão já está marcada com tag em outro commit.

<a id="before-the-first-release"></a>

## Antes da primeira release

Uma release publica pacotes públicos, e uma versão de registry nunca pode ser reutilizada, então o workflow
recusa uma release feita a partir de um repositório privado. O job `preflight` verifica, em segundos e
antes de qualquer build, o que somente um proprietário pode conceder, e indica a correção para cada item
que encontrar faltando:

| Pré-requisito                                                                                | Verificado por                                           |
| -------------------------------------------------------------------------------------------- | -------------------------------------------------------- |
| O repositório é público                                                                      | `preflight`                                              |
| `ghcr.io/clickhouse/loom` e `loom-extractor` dão acesso de escrita (Write) a este repositório | `preflight` (uma sessão de upload que ele abre e cancela) |
| Ambos os pacotes são públicos, já que uma instalação baixa sem fazer login                   | `preflight` (um token de pull anônimo)                   |
| Um environment `pypi` e um trusted publisher do PyPI para `release.yml`                      | o job `pypi`                                             |
| Um environment `npm` e um trusted publisher do npm para `release.yml`                        | o job `npm`                                              |
| Um environment `mcp-registry` com as mesmas regras de proteção de `pypi` e `npm`             | o job `mcp-registry`                                     |
| A retenção de artefatos do repositório é de pelo menos 30 dias (Settings > Actions)          | nada                                                     |
| Branches `release/**` protegidas como `main` (checks obrigatórios, sem pushes diretos)       | nada                                                     |

Somente um push cria um pacote no ghcr.io, e ele o cria como privado. Para uma imagem que nenhuma release
enviou ainda, o `preflight` emite um aviso em vez de falhar, e os jobs `docker-smoke` dessa execução
falham ao baixá-la: defina a visibilidade do pacote como pública nas configurações dele e, em seguida,
execute novamente os jobs que falharam.

Até que uma release tenha sido assinada, faça da próxima tag uma release candidate (`vX.Y.Z-rc.1`). A
execução dela assina as imagens e as verifica de fora, e uma candidate nunca move `:latest`, então uma
falha ali custa um número de candidate, não uma versão final.

<a id="what-the-release-workflow-does"></a>

## O que o workflow de release faz

`.github/workflows/release.yml` compila uma vez, verifica os arquivos exatos e publica esses mesmos arquivos.

**Em um push de tag (`v*`)**:

1. `version` obtém a versão da tag (`vX.Y.Z` → `X.Y.Z`) e recusa uma ref que
   não seja uma tag `v*` (a menos que a execução seja um [ensaio](#rehearsing-a-release)) ou que não seja `X.Y.Z` ou
   `X.Y.Z-rc.N`. Falha quando qualquer uma destas condições é falsa (para `vX.Y.Z-rc.N`, em relação a `X.Y.Z`):
   - `prepare.py check`: `server/__init__.py` contém `__version__ = "X.Y.Z"`, e todo outro
     arquivo de versão, incluindo `server.json`, indica essa versão;
   - `prepare.py check-tag`: um candidato `vX.Y.Z-rc.N` antes de `vX.Y.Z` ser tagueado, e um
     `vX.Y.Z` final no commit do seu candidato de maior número;
   - `prepare.py check-chart`: a versão do chart é maior que a do release anterior;
2. `preflight` faz a execução falhar quando falta um [pré-requisito](#before-the-first-release);
3. `clickhouse` indica a versão do ClickHouse que o bundle do Compose executa
   (`scripts/release/release_clickhouse.sh`), migra um servidor novo dessa versão e executa
   `make schema-check` antes que `build` possa fixá-la, e recusa um downgrade do ClickHouse (veja o passo 8),
   de modo que essa execução para antes de compilar e enviar imagens
   ([quando ele para](#when-the-clickhouse-job-stops)). A versão é a primeira entre:
   - a que o bundle já publicado desta versão indica, de modo que executar o comando de instalação de novo
     para uma versão nunca move o ClickHouse (uma versão sem bundle é resolvida novamente, pois
     `github-release` nunca substitui um, então ninguém pode tê-la instalado);
   - o `clickhouse_image` de um dispatch, que para uma versão final é usado no lugar de qualquer versão
     publicada por seus candidatos no mesmo commit;
   - para uma versão final, aquela com a qual o candidato de maior número no mesmo commit, com bundle
     publicado, foi testado em soak;
   - a versão que `clickhouse/clickhouse-server:latest` indica agora (`dev/clickhouse_release.sh`);
4. `build` grava a versão nos fontes do SDK Python (na forma PEP 440, `X.Y.ZrcN`
   para um candidato) e nos fontes do SDK TypeScript, e compila o wheel e o
   sdist do SDK Python, o tarball do npm (depois do `check:dist` do SDK TypeScript) e zips dos dois
   plugins (`loom-claude-code-plugin-vX.Y.Z.zip`, `loom-codex-plugin-vX.Y.Z.zip`); também
   compila o bundle do Compose sem código-fonte e seu arquivo SHA-256 (indicando essa versão do
   ClickHouse), o `install.sh` do release, que instala esta versão, e
   `agent-setup.txt`, o [contrato de configuração do agente](../getting-started/agent-setup.md) com
   esta versão e este commit preenchidos (`scripts/release/agent_setup.py`);
5. `verify` verifica esses arquivos exatos: seus nomes e versões embutidas, os manifestos dos
   plugins, a ausência de testes no zip do Claude Code, os bits de execução que o git registra, e
   que o bundle indica a versão do ClickHouse que o job `clickhouse` verificou,
   que `install.sh` instala esta versão e que `agent-setup.txt` a fixa, e executa um smoke test
   do wheel em um venv limpo (`scripts/gates/sdk_wheel_smoke.py`);
6. `docker` compila `ghcr.io/clickhouse/loom` (com o commit da tag como `LOOM_BUILD_COMMIT`,
   o que `/v1/health` informa como `build_commit`) e `ghcr.io/clickhouse/loom-extractor`
   (o serviço extrator de documentos), cada uma para linux/amd64 e linux/arm64 no runner
   próprio de cada arquitetura e sem cache de build (ou seja, a partir dos pacotes como
   estão naquele dia), com proveniência e um SBOM, envia cada uma por digest e a analisa;
   `docker-tag` junta os dois digests de cada imagem sob suas tags, e não aplica tag a nenhuma das imagens até
   que as quatro tenham passado (uma falha nesse ponto pode deixar uma delas com tag, e executar de novo aplica tag às duas);
   `sign` então assina, por digest, o índice que `docker-tag` criou para cada imagem, com uma assinatura
   keyless (cosign, com a identidade GitHub da execução, de modo que o certificado indica `release.yml`
   e a tag); uma execução em uma tag cujas imagens já estão publicadas não assina nada. Toda execução então verifica
   as assinaturas das duas imagens como os usuários fazem, de modo que o GitHub Release, o PyPI, o npm e o MCP Registry
   esperam por uma assinatura que seja válida ([uma tag ou assinatura com falha](#a-failed-tag-or-signature));
7. `docker-smoke` instala o bundle com essas imagens, na versão do ClickHouse que ele
   indica, em amd64 e arm64, verifica que a imagem do servidor indica o commit da tag, e então executa
   o `install.sh` e o bundle do próprio release como o one-liner em containers limpos de Debian, Ubuntu
   e Alpine (instalação, ida e volta, nova execução, um downgrade recusado sob o `sh` e o `awk` de
   cada um, e uma atualização sobre o instalador do release final anterior,
   ou, em um ensaio de um commit posterior ao release de sua versão, sobre o instalador
   desse próprio release; `scripts/gates/installer_rehearsal.py`);
8. `github-release` cria o GitHub Release quando as duas imagens existem e foram verificadas, com notas
   geradas a partir dos commits desde o release final anterior (`prepare.py previous`, uma
   saída de `version`; as notas de um candidato também começam aí), ou o atualiza em uma nova execução, mantendo
   suas notas como estão, e anexa os arquivos verificados. Um bundle que o release já
   contém é mantido, assim como seu checksum (um que o release perdeu é gravado de novo a partir
   dele): substituir um asset o exclui primeiro, e um upload com falha deixaria uma
   versão instalada sem seu bundle. Um novo bundle nunca indica uma versão do ClickHouse mais antiga que a
   dos bundles dos 30 releases mais recentes (incluindo pré-releases), pois uma instalação
   que atualizasse para ele iniciaria um servidor mais antigo sobre dados que um mais novo gravou; o job falha
   em vez disso, a menos que o dispatch adicione `-f allow_clickhouse_downgrade=true`. Este job é executado
   para uma tag por vez, de modo que duas tags liberadas juntas não podem passar ambas na verificação contra
   o mesmo bundle mais antigo. Apenas mais um desses jobs espera: a publicação de uma terceira tag cancela
   o que está esperando, que então deve ser executado de novo manualmente (Re-run failed jobs). Uma recusa aqui
   ocorre depois que `docker-tag` enviou as tags de imagem da tag (e `:latest` quando é a maior
   versão final). Uma nova execução mantém as entradas da execução e falha de novo, então faça um dispatch da tag, o que
   também publica os SDKs e a listagem no MCP Registry. Faça o dispatch com a versão
   mais nova que o erro indica, quando essa versão extrai o `schemas/schema.sql` da tag:
   `gh workflow run release.yml --ref vX.Y.Z -f clickhouse_image=clickhouse/clickhouse-server:A.B.C.D`;
   ou aceitando o downgrade:
   `gh workflow run release.yml --ref vX.Y.Z -f allow_clickhouse_downgrade=true`.
   Quando nenhuma das duas for adotada, mova `:latest` (se ele se moveu) e, para uma versão final, `:X.Y` de volta para o
   release anterior nas duas imagens, como em [uma tag ou assinatura com falha](#a-failed-tag-or-signature);
9. `sign-files` baixa todos os arquivos que o release agora serve, como os usuários fazem
   (`scripts/release/download_release_files.sh`), e assina cada um que seja idêntico byte a byte ao
   build desta execução com uma assinatura keyless como a das imagens, em um bundle Sigstore
   `<file>.sigstore.json` (`scripts/release/sign_release_files.sh`). O bundle do stack e
   o checksum que o release manteve de uma execução anterior mantêm seus bundles Sigstore quando estes são válidos;
   qualquer outro arquivo que não seja o build desta execução faz o job falhar, sem assinar nada
   ([uma tag ou assinatura com falha](#a-failed-tag-or-signature)). Em seguida verifica cada arquivo como
   os usuários fazem (`scripts/release/verify_release_files.sh`). Ele mantém o token OIDC da execução em um job
   próprio, com contents somente leitura; `attach-signatures` então envia os bundles Sigstore que ele criou
   (um já mantido permanece como está, pois substituir um asset o exclui primeiro). Um dispatch compila
   os arquivos de novo, e `github-release` os substitui, de modo que, até que seus próprios `sign-files` e
   `attach-signatures` terminem, os bundles Sigstore da execução da tag não verificam os arquivos substituídos;
10. `release-check` (em um push de tag) executa `.github/workflows/release-check.yml` contra o que
    acabou de ser publicado, sem autenticação em nada: o one-liner a partir de github.com nos mesmos containers
    limpos, baixando as duas imagens anonimamente, e verifica as assinaturas das duas imagens e
    de todos os arquivos do release, que devem vir de `release.yml` naquela tag, como um usuário faz
    ([imagens de container](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#container-images),
    [arquivos do release](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#release-files)); um arquivo sem seu
    `.sigstore.json` falha. Um release publicado antes de o workflow de release assinar suas imagens
    ou seus arquivos não tem essas assinaturas, de modo que uma verificação dele falha no job `signatures`.

As imagens do servidor e do extrator são publicadas antes do GitHub Release, de modo que um instalador
exibido na página do release nunca aponta para uma imagem ausente.

**Em dispatch manual** (`workflow_dispatch` em uma ref de tag), os mesmos jobs são executados, exceto
`release-check`, sem compilar imagem quando as imagens já estão publicadas (abaixo). `build` também
executa o `npm run check` do SDK TypeScript (lint, tipos, versão, exemplos do README, testes). Então
três jobs de publicação são executados:

| Job            | Publica                                                          | Como                                                                                                                                                                                                                                  |
| -------------- | ---------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `pypi`         | `clickhouse-loom` (apenas o SDK Python) no PyPI                  | o wheel e o sdist verificados; trusted publishing (OIDC); arquivos que o PyPI já tem são ignorados                                                                                                                                    |
| `npm`          | `@clickhouse/loom` no npm com proveniência                       | o tarball verificado; uma versão publicada é ignorada; `latest` apenas para a maior versão final, um pré-release vai para `next`, um patch de uma linha mais antiga vai para `release-X.Y`; trusted publishing (OIDC)                  |
| `mcp-registry` | `server.json` no MCP Registry como `io.github.ClickHouse/loom`   | o arquivo commitado, cuja versão o job `version` verifica contra a tag; login GitHub OIDC com um `mcp-publisher` fixado; uma versão já listada e um pré-release são ignorados; `preflight` recusa um repositório privado antes de qualquer build |

As imagens já foram publicadas pela execução da tag: um dispatch em uma tag cujas imagens contêm a
versão não compila nem aplica tag a nenhuma (`scripts/release/published_images.sh`), executa smoke test das
imagens publicadas e segue para os registries. As imagens de release são compiladas sem cache, de modo que uma recompilação
moveria as tags da versão para outros bytes.

Nunca mova uma tag `X.Y.Z` publicada: `published_images.sh` falha quando uma imagem publicada foi compilada
a partir de um commit diferente do da execução, e uma correção exige uma nova versão.

Uma nova execução após uma publicação parcial é segura: cada job de publicação ignora o que já existe. A única
exceção é uma execução de tag cuja assinatura de arquivos falhou, que [uma tag ou
assinatura com falha](#a-failed-tag-or-signature) recupera.

**Por que publicar os arquivos verificados.** Um job que recompilasse seu próprio artefato poderia enviar bytes não verificados. Publicar o que `verify` aprovou significa que o GitHub Release e os registries contêm os mesmos bytes.

**Por que o servidor não está no PyPI.** O servidor é implantado junto ao ClickHouse, não importado como biblioteca; ele é distribuído como imagem. O `pyproject.toml` da raiz é marcado com `Private :: Do Not Upload`, e o PyPI contém apenas o SDK. Os jobs `pypi` e `npm` precisam de trusted publishers registrados para este repositório (seus ambientes do GitHub).

**Por que a publicação dos SDKs é manual.** Uma publicação em registry não pode ser desfeita: o PyPI nunca aceita
uma versão duas vezes. Publicar os SDKs apenas em dispatch permite que os arquivos do release sejam verificados antes.
A imagem do servidor é automática porque é um pré-requisito do instalador do GitHub Release,
e criar a tag de release assinada é seu portão de revisão.

<a id="a-failed-tag-or-signature"></a>

### Uma tag ou assinatura com falha

Um `docker-tag` com falha (uma imagem com tag, a outra não) ou um `sign` com falha (Sigstore fora do ar, por exemplo):

- Use "Re-run failed jobs" na execução que aplicou tag às imagens (o push da tag, ou o dispatch que
  as compilou e aplicou tag), dentro de 30 dias dela; os artefatos da execução são mantidos pelo mesmo período, a menos que o
  limite de retenção de artefatos do repositório seja menor. Essa tentativa mantém o resultado de `preflight` e
  os digests de `docker-tag`, de modo que aplica tag ao mesmo build e assina o que foi tagueado.
- Quando a execução de tag já tiver assinado, um dispatch ou um segundo push com falha na tag se recupera com seu
  próprio "Re-run failed jobs", ou com um novo dispatch.
- Nunca execute de novo uma execução que falhou antes de aplicar tag depois que outra execução já aplicou tag: seu `preflight` não viu
  imagens, então ela recompila e moveria as tags publicadas.
- Depois que "Re-run all jobs" substituir a tentativa da execução de tag, ou que os 30 dias tiverem passado, nenhuma
  execução pode aplicar tag ou assinar esse build: uma execução em uma versão parcialmente tagueada falha em seu `preflight`, e uma execução
  em uma tag publicada verifica assinaturas, mas não pode criá-las. Libere uma nova versão.

Até que essa versão seja publicada, as tags móveis do build ainda a indicam. Aponte `:latest` (se ele se moveu) e,
para uma versão final, `:X.Y` de volta para o release anterior que continha cada uma, nas duas imagens. Um `X.Y.0` não tem
release anterior com `:X.Y`, que indica este build até que `X.Y.1` seja publicado. Com
`<previous>` sendo a versão desse release, a tag só se move depois que a assinatura desse release for verificada:

```bash
digest=$(docker buildx imagetools inspect ghcr.io/clickhouse/<image>:<previous> \
  --format '{{json .Manifest}}' | jq -re .digest)
cosign verify "ghcr.io/clickhouse/<image>@$digest" \
  --certificate-identity https://github.com/ClickHouse/loom/.github/workflows/release.yml@refs/tags/v<previous> \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com >/dev/null \
  && docker buildx imagetools create -t ghcr.io/clickhouse/<image>:<tag> "ghcr.io/clickhouse/<image>@$digest"
```

Para um `<previous>` publicado antes de o workflow de release assinar, que não tem assinatura, execute
a linha `digest=`, depois o comando `docker buildx imagetools create` sem o
`cosign verify … &&` antes dele. Em qualquer caso, verifique que a tag indica o digest:

```bash
[ -n "$digest" ] && [ "$(docker buildx imagetools inspect ghcr.io/clickhouse/<image>:<tag> --format '{{json .Manifest}}' | jq -re .digest)" = "$digest" ] \
  || { echo "<tag> does not name $digest" >&2; false; }
```

Um `sign-files` ou `attach-signatures` com falha (Sigstore ou a API do GitHub fora do ar, por exemplo) deixa arquivos no
release sem um bundle Sigstore que os verifique. Qualquer execução posterior os assina de novo enquanto o
bundle do stack e o checksum no release tiverem bundles Sigstore válidos: "Re-run failed jobs" na
execução que falhou. Quando eles não têm nenhum, porque a própria assinatura da execução de tag falhou, apenas "Re-run
failed jobs" na execução de tag os assina, enquanto seus artefatos são mantidos (30 dias, a menos que o
limite de retenção de artefatos do repositório seja menor) e antes de qualquer dispatch: um dispatch, "Re-run all
jobs" ou um segundo push da tag recompila o bundle do stack e o checksum, mas mantém os que o
release contém, que então nenhuma execução pode assinar. Passado esse prazo, libere uma nova versão.

Um `release-check` que falha em uma listagem ou em um download: "Re-run failed jobs". Um que encontra um
bundle Sigstore que não é válido depois que `sign-files` e
`attach-signatures` da última execução de release passaram significa que o release mudou depois de assinado: um incidente de segurança
([SECURITY.md](https://github.com/ClickHouse/loom/blob/main/SECURITY.md)). Uma execução cuja assinatura falhou é recuperada como acima.

`sign-files` recusa qualquer arquivo que não seja o build desta execução, exceto um bundle de stack ou checksum mantido cujo bundle do Sigstore seja verificado. Um arquivo anexado manualmente à release: exclua-o da release quando o workflow não o gerar e depois use "Re-run failed jobs". Um bundle de stack ou checksum mantido sem bundle do Sigstore nunca foi assinado (acima). Qualquer outra recusa, e um arquivo mantido cujo bundle do Sigstore não seja verificado fora de uma indisponibilidade do Sigstore, significa que o arquivo foi alterado fora do workflow: um incidente de segurança, não uma falha de assinatura.

<a id="when-the-clickhouse-job-stops"></a>

### Quando o job clickhouse para

Uma execução que parou em `clickhouse` não publicou nada.

- **`latest` aponta para uma release ruim**: dispare a tag com uma release exata,
  `gh workflow run release.yml --ref vX.Y.Z -f clickhouse_image=clickhouse/clickhouse-server:A.B.C.D`.
  Um bundle já publicado mantém a sua própria, e a execução avisa que ignorou o input.
- **A release não gera o dump do `schemas/schema.sql` da tag**: regenere-o nessa release (os comandos do [dump do schema](migrations.md#schema-dump-and-check) com
  `LOOM_CLICKHOUSE_IMAGE=clickhouse/clickhouse-server:A.B.C.D`) e faça o commit. Para um candidato, crie a tag do próximo `-rc.N` no commit da correção. Para uma versão final (uma sem candidato no seu commit que tenha publicado um bundle, pois só esse resolve `latest`), exclua a tag
  (`git push origin :refs/tags/vX.Y.Z`, e `git tag -d vX.Y.Z` em cada clone que a tenha buscado, pois um fetch nunca remove uma tag) e depois crie a tag de um novo candidato no commit da correção. Nos dois casos, dispare o novo candidato com o mesmo `-f clickhouse_image=...`, pois o push da sua tag resolve `latest` de novo; deixe-o em soak e crie a tag da versão final nele, o que mantém essa release.
- **Um downgrade durante o soak**: uma versão final que carrega a release do seu candidato também é verificada, então um patch para uma linha mais antiga publicado durante o soak com uma release mais nova a interrompe. Há duas saídas:
  - exclua a tag da versão final como acima (ela não publicou nada), crie a tag de um novo candidato nesse commit, deixe-o em soak e crie a tag da versão final nele de novo. O push da tag do candidato resolve `latest`; quando ela for mais antiga que a release citada no erro, dispare o candidato com essa release:
    `gh workflow run release.yml --ref vX.Y.Z-rc.N -f clickhouse_image=clickhouse/clickhouse-server:A.B.C.D`;
  - ou dispare a versão final com `-f allow_clickhouse_downgrade=true` e aceite o downgrade para as instalações desse patch.

Cada disparo acima também executa os jobs `pypi` e `npm`, então um candidato disparado publica seus SDKs assim que seus ambientes forem aprovados; recuse essas aprovações para publicar apenas as imagens e a GitHub Release.

<a id="installing-from-a-github-release"></a>

## Instalando a partir de um GitHub Release

Quando os registries ainda não tiverem sido publicados, instale a partir dos assets do release:

```bash
gh release download vX.Y.Z --repo ClickHouse/loom --pattern 'clickhouse_loom-*.whl'
pip install ./clickhouse_loom-*.whl

gh release download vX.Y.Z --repo ClickHouse/loom --pattern 'clickhouse-loom-*.tgz'
npm install ./clickhouse-loom-*.tgz
```

Os plugins para agentes de código são instalados a partir dos marketplaces do repositório (`claude plugin marketplace add ClickHouse/loom`, `codex plugin marketplace add ClickHouse/loom`). Os arquivos zip são para máquinas sem acesso ao repositório.

<a id="listings"></a>

## Listagens

Agentes e pessoas encontram o Loom por meio destas listagens. Cada uma lê arquivos deste repositório:

| Listagem                                       | Lê                                                                                                                                                                                                 | Como é atualizada                                                                                                                                                                                                                                                  |
| ---------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| MCP Registry e os diretórios que o copiam      | `server.json`: seus metadados e um remote em `https://{loom_host}/mcp` com um header `Authorization` secreto opcional                                                                              | o job `mcp-registry` em cada versão final disparada                                                                                                                                                                                                                    |
| Página do `clickhouse-loom` no PyPI            | os metadados `[project]` de `sdk/python/pyproject.toml` e `sdk/python/README.md` como corpo da página                                                                                              | o job `pypi` em cada versão disparada; a página mostra a maior versão final, ou o maior candidato antes da primeira versão final                                                                                                                                       |
| Página do `@clickhouse/loom` no npm            | os metadados de `sdk/typescript/package.json` e `sdk/typescript/README.md` como corpo da página                                                                                                   | o job `npm` em cada versão disparada; a página mostra a versão marcada com `latest`, que o job define apenas para a maior versão final                                                                                                                                 |
| Diretório de plugins do Claude                 | `integrations/claude-code-plugin/.claude-plugin/plugin.json` (`displayName`, `description`, `keywords`, `icon`, `documentationUrl`, `supportUrl`, `privacyPolicyUrl`) e o README                    | enviada uma vez no portal do desenvolvedor; as versões seguintes são obtidas da branch acompanhada. O ícone é lido apenas no primeiro envio                                                                                                                            |
| Sugestões de plugins do Claude Code            | o bloco `relevance` da entrada `loom` em `.claude-plugin/marketplace.json`: o comando `loom`, os arquivos de configuração do Loom, uma dependência do SDK em um manifesto                           | lida do marketplace na branch padrão; exibida apenas onde um administrador inclui o marketplace na lista de permissões (`pluginSuggestionMarketplaces`)                                                                                                               |
| Cursor Marketplace                             | `.cursor-plugin/plugin.json` (`displayName`, `description`, `keywords`, `logo`, `variables`), `.cursor-plugin/marketplace.json`                                                                    | não enviada; um envio em cursor.com/marketplace/publish é revisado manualmente, assim como cada atualização                                                                                                                                                            |
| Marketplace Awesome Copilot                    | o `plugin.json` e o `mcp.json` da raiz (o pacote Agent Plugins), na tag de versão e no commit que o envio indica                                                                                   | não enviada; um envio passa pelo formulário de issue de plugin externo do `github/awesome-copilot`, que verifica o pacote e o instala com o Copilot CLI; uma nova versão é um pull request para o `plugins/external.json` desse repositório                           |
| Diretório de plugins do Codex                  | `integrations/codex-plugin/.codex-plugin/plugin.json` (`interface`)                                                                                                                                | enviada no portal de plugins; um plugin com hooks de ciclo de vida não é elegível ali, então a listagem pública exige um build sem eles                                                                                                                                |
| Índice de docs para agentes de código          | `context7.json`                                                                                                                                                                                    | atualizado a partir da branch padrão depois que o repositório é adicionado uma vez                                                                                                                                                                                     |
| Índice de Agent Skills                         | `skills/loom-code`, `skills/loom` e as skills do plugin do Claude Code; as skills de contribuidores são marcadas como internas no frontmatter e não são oferecidas                                  | classificado pelas instalações de `npx skills add ClickHouse/loom`; nada a enviar                                                                                                                                                                                      |

O job `mcp-registry` é executado no ambiente `mcp-registry`. Dê a ele as mesmas regras de proteção de `pypi` e `npm` antes do primeiro disparo: o GitHub cria um ambiente ausente sem nenhuma. O login dele não precisa de segredo.

Depois de alterar o manifesto do Claude ou a entrada do marketplace, e antes de fazer push, execute `claude plugin validate integrations/claude-code-plugin --profile directory --strict` para o plugin e `claude plugin validate .` para o marketplace: eles executam as verificações estruturais do diretório, e a do plugin deve passar sem nenhum aviso.

<a id="after-the-release"></a>

## Após o release

- Faça o deploy ([atualizações](https://github.com/ClickHouse/loom/blob/main/docs/operations/upgrades.md)). Os passos para o operador nas notas de release são o checklist de deploy.
- Os plugins de cliente são atualizados pelo gerenciador de plugins de cada cliente, não por um deploy do servidor.

<a id="related"></a>

## Relacionados

- [Campos desconhecidos e removidos](https://github.com/ClickHouse/loom/blob/main/docs/reference/http-api.md#unknown-and-removed-fields): como um
  cliente enxerga um campo que uma versão removeu
- [Contribuindo](https://github.com/ClickHouse/loom/blob/main/CONTRIBUTING.md)
