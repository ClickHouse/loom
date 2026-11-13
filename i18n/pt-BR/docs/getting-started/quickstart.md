<!-- i18n: source=docs/getting-started/quickstart.md blob=2dcc795de559021866a2affe24bef8e4e8f41f81 model=claude-sonnet-5-5 -->
> Esta página é uma tradução automática da [página em inglês](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/quickstart.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/quickstart.md), which is authoritative.

<a id="quickstart"></a>

# Quickstart

Esta página inicia o Loom em um laptop e salva uma primeira memória via HTTP, Python e MCP. Não
precisa de chave de API nem de conta. Uma instalação nova da stack publicada gera embeddings com um pequeno
modelo local que a imagem já inclui, então a busca retorna resultados relevantes sem rede (uma instalação
atualizada mantém o embedder indicado no `.env`); o LLM é falso, então a extração e as respostas de
`memory.ask` não produzem nada. O `.env` de um checkout usa provedores falsos para ambos.

**A instalação padrão do Docker é de usuário único:** sem login, com acesso apenas via localhost e
um namespace padrão `dev/local`. Uma pessoa pode conectar vários agentes sem ativar o modo
multiusuário. **Implantações em equipe também são suportadas no OSS**, com contas autenticadas
e controles de acesso por namespace; [comece no modo multiusuário](#start-in-multi-user-mode)
ou siga [Deployment → Identity modes](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#identity-modes).
Os dois modos oferecem os mesmos recursos do Loom; LLMs e embeddings reais exigem configuração
de provedor em qualquer um dos modos.

<a id="prerequisites"></a>

## Pré-requisitos

| Necessário                     | Para                                                                                |
| ------------------------------ | ----------------------------------------------------------------------------------- |
| Docker com o plugin compose    | A stack: ClickHouse e o servidor Loom.                                              |
| Python 3.12 ou mais recente    | CLI `loom`, apenas para contribuidores. O SDK é compatível com 3.11+.               |
| Node.js 20 ou mais recente     | O SDK TypeScript (opcional).                                                        |
| Porta livre `7777`             | Loom. O desenvolvimento a partir do checkout também expõe o ClickHouse em `8123` e `9000`. |
| `jq`                           | Ler a saída do `curl` no passo 4 (opcional).                                        |

<a id="1-boot-the-stack"></a>

## 1. Suba a stack

<a id="fast-path-released-image-no-clone"></a>

### Caminho rápido: imagem publicada, sem clonar

Pré-requisito: Docker Desktop ou Docker Engine com Compose 2.20 ou posterior.

```bash
curl -fsSL https://github.com/ClickHouse/loom/releases/latest/download/install.sh -o loom-install.sh && sh loom-install.sh && rm loom-install.sh
```

O comando salva o instalador no diretório atual e depois executa esse arquivo. Portanto, execute-o
a partir do seu diretório home, ou de outro diretório em que só você possa escrever: em um diretório compartilhado,
outra pessoa pode substituir `loom-install.sh`, ou apontá-lo para outro arquivo, antes de ele ser executado. Um
download com falha termina com o erro do curl e um código de saída diferente de zero; depois de uma instalação com falha, corrija
o que ela relatar e execute o comando inteiro novamente. O instalador baixa
`loom-stack.tar.gz` da última GitHub Release, verifica o SHA-256, instala a configuração do
Compose e do ClickHouse em `~/.loom`, baixa as imagens correspondentes
`ghcr.io/clickhouse/loom:X.Y.Z` e `ghcr.io/clickhouse/loom-extractor:X.Y.Z` (o
serviço extrator de documentos) e aguarda `http://localhost:7777/v1/health`. A
imagem do servidor contém a API, os workers, as migrations e o painel React compilado na
raiz do servidor; não é necessário instalar Python nem Node. Os indexadores de compilador de Python, TypeScript e Go
estão incluídos e habilitados. Um repositório indexado com o indexador de TypeScript tem
suas dependências instaladas automaticamente. As duas imagens suportam Linux `amd64` e `arm64`,
incluindo o Docker Desktop em Apple Silicon.

A stack executa seu próprio contêiner do ClickHouse. Para usar um ClickHouse que você já executa, um
servidor autogerenciado ou um serviço do ClickHouse Cloud (26.4 ou posterior, com um Keeper), passe a URL dele;
o instalador pede a senha quando a URL não tem uma, cria o banco de dados `loom` e um usuário leitor somente leitura,
ou imprime o SQL que um administrador precisa executar
([escolhendo o ClickHouse](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#choosing-the-clickhouse)):

```bash
curl -fsSL https://github.com/ClickHouse/loom/releases/latest/download/install.sh -o loom-install.sh \
  && sh loom-install.sh --clickhouse https://loom@abc123.us-east-1.aws.clickhouse.cloud:8443 && rm loom-install.sh
```

O `install.sh` de cada release instala essa release, então a cópia que `latest` serve instala a
última release, e a URL própria de uma release, abaixo, instala essa release sempre. Sobre uma instalação
mais nova, a cópia de uma release mais antiga se recusa a instalar, em vez de iniciar um Loom e um
ClickHouse mais antigos sobre dados mais novos; para fazer downgrade de propósito, siga
[downgrade de uma instalação feita pelo instalador](https://github.com/ClickHouse/loom/blob/main/docs/operations/upgrades.md#downgrading-an-installer-install).
Para fixar uma release, use a URL própria dela:

```bash
curl -fsSL https://github.com/ClickHouse/loom/releases/download/v1.2.3/install.sh -o loom-install.sh && sh loom-install.sh && rm loom-install.sh
```

Para inspecionar tudo antes de iniciar, baixe os arquivos e verifique-os:

```bash
curl -fLO https://github.com/ClickHouse/loom/releases/latest/download/loom-stack.tar.gz
curl -fLO https://github.com/ClickHouse/loom/releases/latest/download/loom-stack.tar.gz.sha256
curl -fL https://github.com/ClickHouse/loom/releases/latest/download/install.sh -o loom-install.sh
sha256sum -c loom-stack.tar.gz.sha256
tar -tzf loom-stack.tar.gz
```

Depois, quando o que eles imprimirem parecer correto, instale a partir do bundle verificado:

```bash
LOOM_INSTALL_BUNDLE="$PWD/loom-stack.tar.gz" \
  LOOM_INSTALL_CHECKSUM="$PWD/loom-stack.tar.gz.sha256" sh loom-install.sh && rm loom-install.sh
```

`LOOM_PORT`, `LOOM_BIND_HOST`, `LOOM_IMAGE_REPOSITORY`, `LOOM_EXTRACTOR_IMAGE_REPOSITORY`
e `LOOM_CLICKHOUSE_IMAGE` passados ao instalador são gravados no `.env` do diretório de instalação,
de modo que os comandos `docker compose` posteriores ali e as execuções posteriores do instalador usem a mesma
porta e as mesmas imagens. Um `LOOM_BIND_HOST` diferente de loopback publica o Loom além deste host,
e o servidor se recusa a iniciar assim sem `LOOM_PUBLIC_BASE_URL` e login:
instale com `LOOM_SKIP_START=1`, defina-os no `.env` (abaixo) e execute o comando de instalação
novamente sem `LOOM_SKIP_START=1`.

Cada release executa o ClickHouse que era o mais recente quando ela foi compilada, então executar o
comando de instalação novamente para a mesma versão do Loom mantém o ClickHouse dela; instalar uma versão
mais nova do Loom o atualiza, a menos que o `.env` fixe `LOOM_CLICKHOUSE_IMAGE` (apague essa linha para
voltar a acompanhar as releases). Executar o comando de instalação novamente atualiza os arquivos gerenciados de
Compose/configuração e reconcilia os contêineres. Ele preserva `~/.loom/.env`,
acrescentando a URL do extrator e um token a um arquivo que não os tenha, e preserva os volumes nomeados do Docker. Para
parar sem apagar dados:

```bash
docker compose --project-directory "$HOME/.loom" down
```

Adicione `--volumes` somente para uma redefinição intencional e irreversível.

<a id="try-it-in-github-codespaces"></a>

### Experimente no GitHub Codespaces

[![Open in GitHub Codespaces](https://github.com/codespaces/badge.svg)](https://codespaces.new/ClickHouse/loom?quickstart=1)

O dev container do repositório executa a última release em um codespace, sem nada na
sua máquina: ele instala o Loom com o instalador acima, o inicia e conecta o Claude
Code a ele. A primeira inicialização baixa cerca de 3 GB de imagens e leva alguns minutos. Abra
o painel no endereço que as últimas linhas do log de criação imprimem (`Loom is running in the
codespace. Dashboard: https://<codespace name>-7777.app.github.dev`, o endereço
encaminhado na aba **Ports** para a porta 7777); o `http://127.0.0.1:7777/` do instalador, acima dele, é o
endereço do próprio codespace. No terminal do codespace, execute `cd ~ && claude` (fora do
checkout, cujas instruções são para trabalhar no próprio Loom), faça login e diga "Remember
that I deploy only on Tuesdays"; em uma nova sessão do `claude`, pergunte "When do I deploy?".

O teste roda em modo de usuário único, sem login. No navegador, a porta 7777 é encaminhada
de forma privada: o endereço dela responde somente à sua conta do GitHub. Mantenha-a privada: pública, ou
privada para uma organização, abre um servidor sem login para todos com quem ela for compartilhada.
Os agentes dentro do codespace, como o `claude` dele, usam `http://localhost:7777`; as linhas de conexão
do painel mostram o endereço encaminhado, que exige um login do GitHub que os agentes no codespace
não têm. Aberto no VS Code desktop, o codespace também encaminha a porta para o `localhost:7777` do seu laptop, onde ela fica tão aberta quanto uma instalação local para todos os programas do laptop, e os agentes no laptop podem alcançá-la ali; o encaminhamento precisa dessa porta local livre, então pare antes um Loom local. Use o painel pelo endereço do
navegador: a partir do `localhost:7777` do laptop a página abre, mas o servidor recusa
as requisições dela, então ela não mostra dados; somente o endereço encaminhado é a página do próprio servidor.

A instalação fica em `/workspaces/.loom`, então um codespace reconstruído mantém seu `.env` e
projeto Compose, embora suas memórias fiquem em volumes do Docker que uma reconstrução pode não manter; uma
reconstrução também executa o instalador da última release sobre ela, o que a move para essa release.
Onde quer que esta documentação cite `~/.loom` ou `"$HOME/.loom"`, um codespace usa
`/workspaces/.loom`. O Codespaces oferece abrir a porta 7777 assim que ela começa a escutar; o
painel responde depois que o log de criação imprime "Loom is running in the codespace". Se o
log terminar com um erro, execute `sh .devcontainer/start-loom.sh` no terminal do
codespace: ele mantém o que a primeira execução configurou.

<a id="start-in-multi-user-mode"></a>

### Inicie no modo multiusuário

Para uma instalação nova da imagem publicada com contas e login desde a primeira inicialização,
prepare os arquivos da stack sem iniciar os contêineres:

```bash
curl -fsSL https://github.com/ClickHouse/loom/releases/latest/download/install.sh -o loom-install.sh \
  && LOOM_SKIP_START=1 sh loom-install.sh && rm loom-install.sh
```

Edite `~/.loom/.env` antes de iniciar o Docker. Substitua as entradas existentes de identidade e autenticação,
e adicione a configuração de cadastro:

```dotenv
LOOM_IDENTITY_PROVIDER=trust-network
LOOM_AUTH_REQUIRED=true
LOOM_SELF_SIGNUP=open
```

Mantenha o `COMPOSE_PROJECT_NAME` e o `LOOM_JWT_SECRET` gerados. Depois baixe as imagens e inicie
a stack (com o seu próprio ClickHouse, execute antes `ch-setup`, como o instalador indicou):

```bash
docker compose --project-directory "$HOME/.loom" pull
docker compose --project-directory "$HOME/.loom" run --rm loom loom ch-setup  # your own ClickHouse only
docker compose --project-directory "$HOME/.loom" up -d
```

Depois que as [verificações de saúde](#check-the-stack) passarem, abra `http://localhost:7777/` e
cadastre-se. Clientes SDK e MCP precisam de tokens; use a org e os namespaces concedidos à sua
conta, não os padrões de usuário único `dev/local` dos exemplos abaixo.

Isso habilita a autenticação multiusuário, não a exposição pública na rede: a stack continua vinculada
ao localhost. Antes de compartilhá-la com uma equipe, configure o [acesso de operador](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#identity-modes)
e o [TLS](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#proxy-and-tls). Configure também [modelos reais e chaves de API](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/models-and-keys.md);
selecionar o modo multiusuário não substitui o LLM falso.

Para uma stack de usuário único já em execução, siga as [instruções de troca](#2-open-the-dashboard);
a recriação dos contêineres preserva os volumes de dados existentes.

<a id="contributor-path-build-a-checkout"></a>

### Caminho para contribuidores: compile um checkout

Pré-requisito: Docker Compose 2.34 ou posterior, de que o arquivo compose do checkout precisa para
baixar o ClickHouse diariamente.

```bash
git clone https://github.com/ClickHouse/loom && cd loom
python3 -m venv .venv && . .venv/bin/activate
pip install -e .
loom up
```

`loom up` faz três coisas:

1. Cria os dois arquivos que o git não rastreia: `.env` a partir de `.env.example` e
   `deploy/clickhouse-config/named_collections.xml` a partir do template. Ele nunca
   sobrescreve nenhum dos dois; a um `.env` existente sem token do extrator, ele adiciona um.
2. Executa a stack compose de desenvolvimento com `--build`.
3. Consulta `http://localhost:7777/v1/health` até o servidor ficar pronto, imprimindo a
   contagem de migrations pendentes. Ele desiste após `--timeout` (300 s), com o último corpo de
   health e as últimas linhas de log.

A primeira inicialização de contribuidor compila a imagem e aplica todas as migrations de schema, então reserve
alguns minutos. Em seguida, `loom up` imprime a URL do painel (`http://localhost:7777/`), uma chamada `curl`
e um snippet Python que salvam uma primeira memória, e uma linha que conecta o Claude Code.
Quando a autenticação está ativa, ele também gera um token de escrita e o coloca nesses snippets.

| Flag         | Faz                                  |
| ------------ | ------------------------------------ |
| `--rebuild`  | Recompila após uma mudança de código. |
| `--no-build` | Reutiliza a imagem em cache.         |
| `--open`     | Abre o painel.                       |
| `--port`     | Usa outra porta do Loom.             |
| `--env-file` | Aponta para outro arquivo env.       |

Executar `loom up` novamente em uma stack em execução apenas refaz a verificação de saúde. `make dev` faz o mesmo
sem a CLI e executa:

```bash
docker compose --env-file .env -f compose.yaml -f dev/docker-compose.dev.yml --profile bundled up -d --build
```

Com `compose.yaml` primeiro, o Compose lê o `.env` da raiz do repositório para as
substituições `${VAR:-default}`; `--env-file .env` apenas diz isso explicitamente. `loom down`
para a stack e preserva os volumes dela. `loom down --delete-data` apaga permanentemente
somente os volumes nomeados desta instalação, depois que você digita o nome do projeto Compose
gerado; `--yes` é a confirmação não interativa. Os alvos do Makefile
fazem o mesmo: `make down` preserva os dados e `make down-all` os apaga.

<a id="what-the-stock-env-boots"></a>

### O que o `.env` padrão inicia

Os templates de `.env` do checkout e do bundle de release selecionam o modo de usuário único.
O bundle de release define `LOOM_IDENTITY_PROVIDER=single-user` explicitamente; o checkout
o deixa vazio e infere o mesmo modo a partir de `LOOM_AUTH_REQUIRED=false`.

| Configuração                                                                 | Valor         | Efeito                                                                              |
| ---------------------------------------------------------------------------- | ------------- | ----------------------------------------------------------------------------------- |
| `LOOM_AUTH_REQUIRED=false`, `LOOM_IDENTITY_PROVIDER=single-user` (ou vazio)  | `single-user` | Sem login. Namespace padrão `dev/local`. Chamadas SDK e MCP não precisam de token.  |
| `LOOM_LLM_PROVIDER=fake`, `LOOM_LLM_MODEL=fake-echo-1`                       | LLM falso     | Extração, marcação de entidades e síntese de `memory.ask` não produzem nada.        |
| `LOOM_EMBEDDING_MODEL=local`, `LOOM_EMBEDDING_DIM=384` (bundle de release)   | bge-small     | Busca semântica na CPU, a partir dos arquivos de modelo que a imagem traz.          |
| `LOOM_EMBEDDING_MODEL=fake-ngram-3072`, `LOOM_EMBEDDING_DIM=3072` (checkout) | embedder falso | A busca ranqueia por n-gramas de caracteres. Basta para verificar a ligação, não a recuperação. |

Os dois instaladores geram um `COMPOSE_PROJECT_NAME` único, um segredo aleatório de assinatura de JWT, um
token aleatório do extrator e `LOOM_BIND_HOST=127.0.0.1`. Assim, clones separados não
compartilham volumes do ClickHouse silenciosamente, e o Loom e o ClickHouse só são acessíveis a partir deste
computador.

Para provedores reais, abra **Settings → Models & keys**, escolha seus provedores e modelos,
informe as chaves de API deles e clique em **Test & apply**. Uma única chave de API da OpenAI pode atender o
LLM e os embeddings. Não é preciso reiniciar o Docker; mudanças de embedding disparam uma
reconstrução verificada, com progresso e nova tentativa exibidos no painel.

Para uma implantação baseada em variáveis de ambiente, substitua as linhas de provedor em `~/.loom/.env`
(stack publicada) ou `.env` (checkout). Definir apenas `OPENAI_API_KEY` não muda nada:
as linhas de provedor padrão nomeiam seus provedores explicitamente. Sair do embedder local ou falso
reprocessa os embeddings do que está armazenado.

```bash
LOOM_EMBEDDING_MODEL=text-embedding-3-large
LOOM_EMBEDDING_DIM=3072
LOOM_LLM_PROVIDER=openai
LOOM_LLM_MODEL=gpt-6-luna
OPENAI_API_KEY=sk-...
```

Para Claude, OpenAI, embeddings hospedados, clientes somente com assinatura e implantações com
um único provedor de LLM, siga [Configure modelos e chaves de API](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/models-and-keys.md).
Somente depois de editar o `.env` da stack publicada, aplique-o com
`docker compose --project-directory "$HOME/.loom" up -d --force-recreate loom`;
um simples restart não recarrega o ambiente do contêiner. As configurações avançadas estão em
[Deployment → Providers](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#providers).

<a id="check-the-stack"></a>

### Verifique a stack

```bash
curl -fsS http://localhost:7777/v1/health | jq '{status, fake_embedding, fake_llm, providers_degraded, problems}'
docker compose --project-directory "$HOME/.loom" exec loom loom doctor
```

- `/v1/health` retorna `status: ok` quando o ClickHouse está acessível e nenhum problema além
  de `code_tier_failing` (a camada de índice de código de um repositório com falha) é um erro.
  `fake_embedding`, `fake_llm` e `providers_degraded` indicam em que modo você está;
  `problems` lista cada problema com a correção; `schema.pending` lista as migrations não aplicadas.
- `loom doctor` imprime uma linha por verificação do lado do servidor. `--json` gera JSON. Em uma stack de checkout do código-fonte, execute `docker compose --env-file .env -f compose.yaml -f dev/docker-compose.dev.yml exec loom loom doctor`.

<a id="2-open-the-dashboard"></a>

## 2. Abra o painel

`http://localhost:7777/` abre direto no painel no modo `single-user`;
no modo multiusuário, cadastre-se ou entre primeiro. No modo `single-user`,
o painel e os padrões do agente usam `dev/local` (`LOOM_DEFAULT_NAMESPACE` renomeia o
namespace). Este é um modo de conveniência, não uma barreira de isolamento entre usuários: qualquer pessoa que
consiga acessar o servidor pode acessar seus dados e a administração. Mantenha-o acessível apenas via localhost.

Para testar o cadastro (`trust-network`: e-mail e senha, e a primeira pessoa a
reivindicar o nome de um namespace se torna seu admin), substitua estas entradas em `~/.loom/.env`
(stack publicada) ou `.env` (checkout):

```dotenv
LOOM_IDENTITY_PROVIDER=trust-network
LOOM_AUTH_REQUIRED=true
LOOM_SELF_SIGNUP=open
```

Definir apenas `LOOM_AUTH_REQUIRED=true` não é suficiente quando `single-user` é selecionado explicitamente:
esse modo mantém a autenticação desativada. Mantenha o `LOOM_JWT_SECRET` gerado pelo instalador
e os volumes existentes, e depois recrie o servidor para carregar o ambiente:

```bash
# Released stack:
docker compose --project-directory "$HOME/.loom" up -d --force-recreate loom
# Checkout stack (run from the repository root):
docker compose --env-file .env -f compose.yaml -f dev/docker-compose.dev.yml --profile bundled up -d --force-recreate loom
```

O painel então abre na página de login, que tem uma aba **Sign up**, e toda chamada de SDK
e MCP precisa de um token. Os modos de identidade estão em [Identities and
namespaces](https://github.com/ClickHouse/loom/blob/main/docs/concepts/identities-and-namespaces.md#identity-modes). Indicar
`trust-network` também é o que permite que um `LOOM_BIND_HOST` fora do loopback inicie com o cadastro aberto,
de modo que qualquer pessoa que alcance a porta pode se cadastrar. Antes de tornar o servidor acessível a uma
equipe, siga o [guia de implantação](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#identity-modes) para
acesso de operador e [TLS](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#proxy-and-tls).

<a id="3-get-a-token"></a>

## 3. Obter um token

No modo de usuário único, o token é opcional. Mesmo assim, gerar um mantém seus scripts funcionando
quando a autenticação for ativada, desde que eles informem `org` (ou a enviem em branco), `namespace`
e `agent`: uma chamada que envia uma credencial, como faz o SDK quando encontra alguma ([Python
SDK → Credentials](https://github.com/ClickHouse/loom/blob/main/docs/reference/python-sdk.md#credentials)), não tem nenhum desses campos preenchido automaticamente,
e um `org` em branco é o do próprio token.

- **Painel:** **Settings → Tokens → New token**. Por padrão, o token lê todos os
  namespaces e repositórios aos quais você pertence (um repositório restrito somente quando você o adiciona),
  mas nenhuma coleção, e salva no namespace em que você está. _Only what you choose_ o limita
  aos namespaces, coleções e repositórios que você marcar (nenhum no início); os marcados
  são pesquisados na ordem listada, e você a altera com as setas de uma linha ou, em
  um desktop, arrastando a alça da linha. O resultado mostra o token uma única vez, com a configuração para
  seu cliente MCP ou para um script.
- **CLI, como operador:** execute dentro do container do servidor, que guarda o
  segredo de assinatura:

  ```bash
  docker compose --project-directory "$HOME/.loom" exec loom loom mint-token --subject my-bot --org dev --namespaces local --roles writer
  ```

  Adicione `--ttl-days 30` para qualquer token que saia da sua máquina. `loom mint-token --help`
  lista as opções de escopo (`--namespace-role`, `--read-scope`, `--traffic-class`).

- **Um bot que se cadastra sozinho** (somente em rede confiável):
  `loom init --agent nightly-triage --server-url http://localhost:7777` cria o
  namespace `agent-nightly-triage`, salva um token writer em `~/.loom/token` e a URL
  em `~/.loom/config.json`, e imprime um código de reivindicação. Uma pessoa o informa uma vez em
  `/oauth/agent-claim`, com uma senha própria, para assumir a propriedade.

Salve o token onde o SDK, a CLI e os plugins o procuram:

```bash
mkdir -p ~/.loom && (umask 077; printf '%s' "<token>" > ~/.loom/token)
export LOOM_API_KEY="<token>"
```

Claims, roles e grants estão em [Tokens and grants](https://github.com/ClickHouse/loom/blob/main/docs/guides/tokens-and-grants.md).

<a id="4-first-memory-over-http"></a>

## 4. Primeira memória via HTTP

Toda operação é um `POST /v1/<op>` com um corpo JSON. Uma instalação nova com o `.env` padrão, feita pelo instalador ou a partir de um checkout, é de usuário único: sem token, e uma chamada comum de memória ou de busca (`memory.set`, `memory.get`, `memory.search` e mais algumas) que não informa `org` nem `namespace` é executada nos da própria instalação, org `dev` e namespace `local`, os mesmos que o MCP e o painel usam; um `memory.set` que não informa `agent` é registrado como agente `http`, e uma busca lê todos os agentes. Assim, as chamadas abaixo precisam apenas dos seus próprios campos ([quais ops](https://github.com/ClickHouse/loom/blob/main/docs/reference/http-api.md#request)). Se uma chamada sem token responder 401 `AUTH_INVALID`, a autenticação está ativada: adicione `-H "Authorization: Bearer $LOOM_API_KEY"` e informe `org`, `namespace` e `agent` da sua conta; um header com token vazio é recusado.

```bash
curl -sX POST http://localhost:7777/v1/memory.set -H 'Content-Type: application/json' \
  -d '{"memory_key":"deploy_window","value":"Tue and Thu only; never Friday."}'

curl -sX POST http://localhost:7777/v1/memory.get -H 'Content-Type: application/json' \
  -d '{"memory_key":"deploy_window"}' | jq '{found, value}'

curl -sX POST http://localhost:7777/v1/memory.search -H 'Content-Type: application/json' \
  -d '{"query":"when can we deploy?"}' | jq '.results[0].content_excerpt'
```

A resposta de `memory.set` traz `memory_id`, `content_sha256` e `status`.
`memory.get` lê de volta uma chave: `found` indica se a chave contém um valor ativo, e `value` é o valor como foi gravado. A resposta da busca traz `results` e `abstain`, que indica se o melhor resultado é forte o bastante para responder a partir dele. A busca lê as memórias de todos os agentes no namespace, incluindo as adicionadas no painel; informe um `agent` para ler apenas as desse agente.
`GET /v1/operations` lista todas as operações, e <http://localhost:7777/docs> permite testar cada uma pelo navegador. Os formatos estão na [referência da HTTP API](https://github.com/ClickHouse/loom/blob/main/docs/reference/http-api.md).

<a id="5-first-memory-from-python"></a>

## 5. Primeira memória com Python

```bash
pip install clickhouse-loom    # or, from a clone: pip install ./sdk/python
```

Uma versão que ainda não está no PyPI é instalada a partir dos seus assets
([instalando a partir de um GitHub Release](https://github.com/ClickHouse/loom/blob/main/docs/development/release-process.md#installing-from-a-github-release)).

O exemplo informa `org` e `namespace`, então funciona com ou sem o token do passo 3, que o SDK envia quando encontra um. Em um servidor de usuário único, uma chamada sem token pode omiti-los: `Agent(agent="quickstart", base_url=...)` também roda em `dev`/`local`.

```python
import asyncio

from loom import Agent


async def main():
    async with Agent(
        org="dev",
        namespace="local",
        agent="quickstart",  # names the writer in the audit log
        base_url="http://localhost:7777",
    ) as a:  # sends a credential it finds; none needed in single-user mode
        await a.memory.set("standup_time", "Daily standup is at 9:30.")
        row = await a.memory.get("standup_time")  # None when the key is unset
        print(row["value"] if row else "unset")
        for hit in await a.memory.search("when is standup?"):
            print(hit.content_excerpt)


asyncio.run(main())
```

O local onde o SDK encontra seu servidor e sua credencial está em
[Python SDK → Credentials](https://github.com/ClickHouse/loom/blob/main/docs/reference/python-sdk.md#credentials).
O SDK de TypeScript é instalado com `npm install @clickhouse/loom`
([a partir de um clone](https://github.com/ClickHouse/loom/blob/main/docs/reference/typescript-sdk.md#install)).

As interfaces de cliente estão nas referências do [Python SDK](https://github.com/ClickHouse/loom/blob/main/docs/reference/python-sdk.md) e do
[TypeScript SDK](https://github.com/ClickHouse/loom/blob/main/docs/reference/typescript-sdk.md).

<a id="6-first-memory-from-a-coding-agent"></a>

## 6. Primeira memória a partir de um agente de código

Para o Claude Code, instale o plugin do Loom. Ele adiciona hooks (contexto no início da sessão,
captura no final) e skills sobre as ferramentas MCP:

```bash
claude plugin marketplace add ClickHouse/loom
claude plugin install loom@loom
```

Em um servidor de usuário único nesta máquina, mantenha a URL padrão do servidor e deixe o token
vazio quando o Claude Code pedir as configurações do plugin.

Sem o plugin, adicione a entrada MCP diretamente:

```bash
claude mcp add --transport http loom "http://localhost:7777/mcp?toolset=coding" -s user
```

Ambos acessam um servidor de usuário único nesta máquina sem um token seu.

Com a autenticação ativada, crie um token em **Settings → Tokens** e escolha **Claude Code** em
**Connect**: o diálogo mostra a configuração para o plugin e para uma entrada `claude mcp add`,
com o token incluído. Depois:

1. Sobrescreva `~/.loom/config.json` com o bloco `config.json` do diálogo mais o
   namespace em que o token grava (`"namespace": "<namespace>"`): um `org` ou `namespace` que uma
   sessão de usuário único deixou no arquivo tem precedência sobre o token, e, sem um deles, o plugin
   grava no primeiro namespace do token diferente de `*`, ou em `coding`. Adicione de volta qualquer
   `code_repo` ou `agent` que você tenha definido.
2. Deixe `LOOM_API_KEY` (o passo 3 pode ter exportado uma: `unset LOOM_API_KEY`) e a opção
   `token` do plugin vazias, ou defina ambas com o novo token: o plugin lê as duas
   antes desse arquivo
   ([configuração](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/claude-code.md#configuration)).
3. Remova uma entrada `claude mcp add` criada sem token (`claude mcp remove loom -s user`)
   antes de executar o comando do diálogo, que recusa um nome já existente.

`loom install --client <name>` grava a entrada MCP para Claude Desktop, Claude Code,
Codex, Cursor, VS Code, Cline, Windsurf e Zed; sem a flag, ele configura todos os clientes que
encontrar. [Conectar um agente de código](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md) cobre Codex e Cursor.

Inicie uma nova sessão e diga _"Lembre que eu nunca faço deploy na sexta-feira."_ O agente chama
`loom_memory_set`. Em uma sessão nova, pergunte _"Quando posso fazer deploy?"_ O agente chama
`loom_search` e responde a partir do fato armazenado. O modelo decide quando salvar e
buscar com base nas descrições das ferramentas; não há palavras de gatilho.

<a id="7-see-it-in-the-dashboard"></a>

## 7. Veja no painel

| Página            | Mostra                                                       |
| ----------------- | ------------------------------------------------------------ |
| `/memories`       | As memórias do namespace, com autor da escrita e horário.    |
| `/admin/activity` | O log de auditoria: qual agente escreveu qual chave e quando. |
| `/admin/usage`    | Custo por operação e por agente.                             |

O restante está em [Painel](https://github.com/ClickHouse/loom/blob/main/docs/features/dashboard.md).

<a id="optional-index-a-repository-on-the-released-stack"></a>

## Opcional: indexar um repositório na stack publicada

Adicionar um repositório pelo painel é recusado nesta stack com `SANDBOX_UNAVAILABLE`:
isso exige o [sandbox de jobs de repositório](https://github.com/ClickHouse/loom/blob/main/docs/operations/code-index-operations.md#repository-job-sandbox),
que esta stack deixa desativado porque o confinamento padrão do Docker o bloqueia. Liste
apenas repositórios em que você confia: as etapas de build deles (instalações do TypeScript, downloads de módulos do Go,
CMake) rodam sem o sandbox, no contêiner `loom`, cujo ambiente contém o segredo JWT
e as chaves dos provedores.

`LOOM_CODE_REPOS_JSON` é uma única linha que contém todos os repositórios como um array JSON
([Configure the entry](https://github.com/ClickHouse/loom/blob/main/docs/operations/code-index-operations.md#configure-the-entry)).
Em `~/.loom/.env`, descomente essa linha (adicione-a se ela não existir) e edite-a.
`dev` é a org sob a qual uma stack de usuário único roda e a org em que as contas multiusuário se cadastram
(`LOOM_SELF_SIGNUP_ORG`); se você mudou isso, use a org da sua conta aqui e em
`--org` abaixo:

```dotenv
LOOM_CODE_REPOS_JSON=[{"org":"dev","namespace":"code-myrepo","repo":"acme/myrepo","git_branch":"main","public":true}]
```

- O Loom escolhe os indexadores da camada de compilador a partir dos arquivos de build do repositório (`go.mod`,
  `package.json`, `pyproject.toml`, um `CMakeLists.txt` na raiz, …); `scip_producer` indica
  um indexador explicitamente ([Compiler tier](https://github.com/ClickHouse/loom/blob/main/docs/operations/code-index-operations.md#compiler-tier)).
  C/C++ também precisa de uma árvore de build configurada, então sem ela um repositório C/C++ recebe apenas
  a camada de parser.
- Para um repositório privado, adicione um token do GitHub (`LOOM_CODE_GITHUB_TOKEN` no `.env`, ou
  `github_token` na entrada) e remova `"public":true`: essa opção é para repositórios públicos em
  seu host e permite que qualquer pessoa da org entre no índice como leitora imediatamente em
  **Repositories**. Decida antes do primeiro `loom code init`: ele define a política de entrada do namespace
  a partir de `public`, e editar `public` depois não a altera (um mantenedor
  a altera com `code.repo.set_access`).

Em seguida, recrie o contêiner `loom` e execute a primeira indexação:

```bash
docker compose --project-directory "$HOME/.loom" up -d --force-recreate loom
docker compose --project-directory "$HOME/.loom" exec loom loom code init --namespace code-myrepo --org dev
docker compose --project-directory "$HOME/.loom" exec loom loom code backfill --namespace code-myrepo --org dev --embed
```

`backfill --embed` roda no embedder local da stack publicada. Com um embedder fake (o
`.env` de um checkout, ou uma instalação atualizada que ainda usa `fake-ngram-*`) ele se recusa e
encerra; para testar a integração mesmo assim, adicione `--allow-fake-embedder`. Escolher um modelo depois em
**Settings → Models & keys** reembeda o índice de código; após uma mudança no `.env`, ele precisa de
um reembedding próprio, como descrito em [Models and keys](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/models-and-keys.md).

No modo multiusuário, um repositório listado não tem mantenedor, então sem `"public":true` o pedido de entrada
de um leitor fica esperando sem ninguém para aprová-lo. Depois de `loom code init`, dê acesso a
alguém como operador, com um token gerado no contêiner: `admin` torna a pessoa
mantenedora, que aprova os leitores a partir de então, e `viewer` permite apenas ler. O
subject é o namespace com o qual a conta se cadastrou (seu identificador de login, o subject
dos seus tokens); o email de um autocadastro nunca é verificado, então uma concessão a ele não admite ninguém:

```bash
ADMIN_TOKEN=$(docker compose --project-directory "$HOME/.loom" exec -T loom loom mint-token --subject operator --org dev --namespaces code-myrepo --roles admin --ttl-days 1)
curl -s http://localhost:7777/v1/grants.create -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"org":"dev","namespace":"code-myrepo","subject":"your-namespace","role":"admin"}'
```

<a id="optional-ask-a-question-about-a-document"></a>

## Opcional: faça uma pergunta sobre um documento

```bash
loom demo
```

`loom demo` envia o `AGENTS.md` deste repositório, ingere-o com `knowledge.ingest` (o extrator de referência da stack faz o parsing) e faz uma pergunta sobre ele com `knowledge.synthesize`, exibindo
a resposta e os chunks citados. Com o LLM falso, ele exibe os principais resultados de
`knowledge.search`. `--file` e `--question` apontam o comando para o seu próprio documento.

<a id="common-problems"></a>

## Problemas comuns

| Sintoma                                           | Causa e solução                                                                                                                                                                                  |
| ------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `set_from_messages` não grava nenhum fato         | O LLM é o falso. Siga [Models and keys](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/models-and-keys.md) para configurar um.                                                                                                      |
| Os resultados da busca parecem arbitrários        | O embedder é um falso (`fake-ngram-*` no `.env`). Troque-o conforme [Models and keys](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/models-and-keys.md) indica, o que gera novamente os embeddings do que está armazenado. Não apague seus volumes para trocar de modelo. |
| 401 `AUTH_INVALID` após ativar a autenticação     | Não há token, ou ele foi assinado com outro `LOOM_JWT_SECRET`. Gere um novo com o comando do Compose em [Get a token](#3-get-a-token).                                                                     |
| O painel retorna 503                              | O servidor roda a partir do código-fonte (`make server`) sem um frontend compilado. Execute `make frontend-build`.                                                                                               |
| A stack publicada não fica saudável               | Execute `docker compose --project-directory "$HOME/.loom" logs --tail 100 loom clickhouse`. Veja [Troubleshooting](https://github.com/ClickHouse/loom/blob/main/docs/operations/troubleshooting.md).                                               |
| Adicionar um repositório responde `SANDBOX_UNAVAILABLE` | A stack roda sem o sandbox de jobs de repositório. Indexe o repositório por meio de `LOOM_CODE_REPOS_JSON` ([Index a repository](#optional-index-a-repository-on-the-released-stack)).             |
| O instalador diz que a porta `7777` está em uso   | Outro programa, ou outro Loom, está escutando nela. Pare-o, ou execute o comando de instalação novamente com `LOOM_PORT=7778` antes de `sh loom-install.sh`; o `.env` mantém a porta.                      |
| O instalador diz que a porta `7777` não responde  | Um programa a ocupa sem responder, ou `LOOM_BIND_HOST`, `LOOM_PORT` ou um firewall impede este host de alcançá-la.                                                                         |
| O instalador não conseguiu verificar a porta `7777` | O curl falhou pelo motivo indicado em seu código de saída; verifique `LOOM_BIND_HOST` e `LOOM_PORT`.                                                                                                        |
| A porta `8123` ou `9000` de um clone está ocupada | Remapeie as portas do host do ClickHouse ([local stack](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#local-stack-from-a-clone)).                                                                                         |
| O instalador não conseguiu baixar as imagens      | O próprio Docker acessa o registry: atrás de um proxy, forneça ao daemon do Docker as [configurações de proxy](https://docs.docker.com/engine/daemon/proxy/).                                                 |

<a id="next"></a>

## Próximos passos

- [Avalie o Loom](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/evaluate.md): transforme este smoke test em uma avaliação de qualidade,
  latência e custo específica para a sua carga de trabalho.
- [Conecte um agente de código](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md): Claude Code, Codex ou Cursor
  usando esta stack.
- [Seu primeiro agente](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/first-agent.md): identidade, tratamento de abstain, sessões, context packs
  e runs a partir do SDK.
- [Deployment](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md): como executar o Loom para uma equipe.
