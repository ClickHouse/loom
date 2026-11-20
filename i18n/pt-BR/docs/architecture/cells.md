<!-- i18n: source=docs/architecture/cells.md blob=78e1d4c689a18b65208ce7c9b2d1a0593744a238 model=claude-sonnet-5-5 -->
> Esta página é uma tradução automática da [página em inglês](https://github.com/ClickHouse/loom/blob/main/docs/architecture/cells.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/architecture/cells.md), which is authoritative.

<a id="cells"></a>

# Células

A memória de um deployment pode crescer além de um primary Postgres e de um serviço ClickHouse, e uma organização pode querer sua memória em serviços próprios. Uma **célula** é um deployment completo do Loom, como esta documentação o descreve: suas réplicas e workers, uma unidade Postgres (um primary com seus standbys), um serviço ClickHouse, um prefixo de bucket de documentos, seu próprio host público, e suas próprias chaves de assinatura, concessões, workers e exclusão. Um **router** no host compartilhado do deployment envia uma requisição para a célula que contém o que ela endereça. Uma organização vive em exatamente uma célula, a que a alocou, e nunca sai dela. Uma organização é alocada no attach (as organizações que a célula continha e suas organizações configuradas), por `loom cell update` (uma organização configurada que mudou) ou por `loom cell place`, ou na primeira requisição verificada de um principal com escopo nela. Um deployment com uma célula e sem router funciona como um deployment único.

As células exigem a autoridade Postgres (`LOOM_STORAGE_AUTHORITY=postgres`) e uma autenticação que todos os transportes imponham (`auth_actually_required()`, e `LOOM_MCP_AUTH_MODE` diferente de `open`): `loom cell attach` se recusa a executar sem elas, e um processo com `LOOM_CELL` definido se recusa a iniciar sem elas. [Autoridade de armazenamento](storage-authority.md) descreve o que uma célula contém e como seus dois armazenamentos concordam.

<a id="why-whole-deployments"></a>

## Por que implantações inteiras

Uma célula não compartilha armazenamento, chave, lease nem worker com outra. Toda garantia que uma implantação única oferece (isolamento de tenants, uma revogação que vale imediatamente, cobertura de apagamento, um lease de worker, uma cadeia de auditoria, uma restauração) vale por célula, sem alterações, e os dados, as concessões e o apagamento de uma organização nunca abrangem várias células. O roteador adiciona o diretório e as regras de roteamento abaixo; cada célula adiciona uma verificação de que contém a organização à qual uma requisição se dirige.

O que abrange várias organizações passa a ser por célula:

- um token é válido na célula que o emitiu e em nenhuma outra;
- as linhas de escopo global da implantação são por célula: a lista de bloqueio global (`org='*'`), as configurações e os overrides de runtime, as regras de política, os overrides de limite de taxa e de orçamento, a rotação de chaves de assinatura e a revogação de um subject. `loom cell each -- <command>` executa um comando administrativo sem organização em todas as células, com uma credencial de curta duração por célula, registra o resultado de cada célula, retoma as células que falharam e falha até que todas tenham êxito; `loom doctor` compara as configurações e os fingerprints de configuração das células e indica o que difere;
- as organizações de código e de tracker (`LOOM_CODE_ORG`, `LOOM_TRACKER_ORG`) são próprias de cada célula: cada célula indexa seus próprios repositórios, com o custo de ingestão de cada um;
- a visão de plataforma de um operador e o painel são por célula, no host da célula.

<a id="the-router"></a>

## O router

O router é um proxy HTTP sem estado, executado como várias réplicas atrás do host compartilhado do deployment, lançado de forma independente e compatível com cells das builds N-1 e N. Ele mantém o diretório em seu próprio banco Postgres:

- `cells`: a cell, o host público, os emissores de bearer token que ela aceita exclusivamente (seu próprio `LOOM_JWT_ISSUER`, seu `LOOM_JWT_TRUSTED_ISSUERS`, seus emissores de usuário final), seus stores (o id do deployment e o nome e o prefixo do bucket), seu estado (`attaching`, `serving`), quando o attach começou (`attach_started_at`, que o attach grava sob o lock do diretório somente depois de todas as suas recusas terem passado, antes de gravar qualquer linha na cell além de suas probes e das revogações de `--revoke-unprovisioned`, e que um attach retomado lê), seu modo de tenancy e quando foi definido (o prepare grava o primeiro, e o attach registra cada um que ele eleva ou tenta elevar), se está fechada para novas orgs e sua cota de placement (`placements_max`), o SHA-256 da credencial com a qual ela chama o router (`current` e, durante uma rotação, `next`), a impressão digital de seu segredo raiz (`keys_fp`), seu `probe_url` (o do próprio router para a primeira cell), os nomes e as impressões digitais de chave de seus bots break-glass e a impressão digital de sua configuração (`current` e, durante uma mudança, `next`; os campos citados em "Verificação contínua"), que o attach e `loom cell update` gravam junto com a cópia da própria cell dessa impressão digital (`loom.cell_self`);
- `placements`: org, cell. Uma linha por org, gravada uma vez e nunca excluída;
- `hosts`: o host de cada cell e os aliases do host compartilhado (abaixo).

O router tem sua própria credencial de operador, definida em sua configuração e nunca no diretório; toda alteração do diretório feita pelo operador usa essa credencial, e toda alteração, placements incluídos, é gravada no log de auditoria do router. Anexar uma cell também exige uma credencial de administrador da plataforma da cell que está sendo anexada. O router informa sua própria liveness e readiness, independentes as de qualquer cell.

O router é colocado primeiro na frente do deployment existente, repassando todas as requisições a ele; esse deployment se torna a primeira cell quando é anexado e mantém o host compartilhado como seu próprio host, de modo que seus clientes, tokens, refresh tokens, clientes OAuth e callbacks registrados continuam funcionando. O router responde por conta própria seus próprios caminhos (`/cells/`, `/livez`, `/readyz`) e roteia todo o resto, inclusive os caminhos `/v1/health` de uma cell, tanto para cells `attaching` quanto `serving`, nesta ordem:

1. **Por host.** Uma requisição ao host próprio de outra cell vai para essa cell. O host compartilhado, e qualquer alias dele registrado em `hosts`, são configuração própria do router e são tratados pelas regras 2 a 4 (a cell da regra 4 é o upstream de pass-through, antes e depois de a primeira cell ser registrada, já que o prepare a registra nesse endereço), nunca respondidos com 421; qualquer outro host que nenhuma linha de `cells` nomeie responde 421. Toda URL que uma cell fornece nomeia seu próprio host: seus endpoints OAuth e OIDC, a URI de verificação do device flow, o callback de login (`LOOM_OAUTH_ISSUER`, registrado no provedor de identidade por cell), seu JWKS, as páginas e os assets do painel e os metadados de recurso MCP. Clientes MCP de uma cell que não seja a primeira usam o host da cell, que as linhas de setup da cell exibem.
2. **SCIM** segue o host, como a regra 1: o aplicativo do provedor de identidade de cada cell faz o provisionamento em seu próprio host (abaixo); o SCIM no host compartilhado é o da primeira cell, exceto uma requisição `/scim/v2/Groups` com um JWT, que vai pelo emissor como na regra 3. As rotas Groups têm verificação de tenancy (elas gravam `loom.principal_groups` da org).
3. **Por emissor.** Uma requisição com token ao host compartilhado vai para a única cell que aceita seu claim `iss`, lido sem verificação; a cell verifica. Um emissor que nenhuma cell aceita responde 401. Até a primeira cell estar `serving`, as regras 2 a 4 enviam toda requisição do host compartilhado para a cell da regra 4, que a verifica, de modo que o deployment atrás do router atende as orgs que possui como antes durante o prepare, o rollout de `LOOM_CELL` e o attach; do rollout até `serving`, uma nova org também precisa do diretório em todo processo com `LOOM_CELL` definido (ele responde 503 ali enquanto o diretório estiver ilegível). Isso abrange MCP e toda op, qualquer que seja a org que um corpo nomeie, e os tokens de emissores federados e de usuário final de integrações.
4. **Todo o resto no host compartilhado vai para a primeira cell**: seus fluxos OAuth e device, refreshes, callbacks, requisições de senha e autocadastro, probes de saúde e a página de login; uma revogação de token (`/oauth/revoke`) de um JWT segue o emissor, como na regra 3, e a de um refresh token opaco vai para a primeira cell, já que apenas os clientes da primeira cell usam o host compartilhado (`loom logout` revoga no host em que fez login). Os documentos de descoberta do host compartilhado são os da primeira cell.

O router alcança uma cell no `upstream_url` que a linha do diretório registra, um endereço dentro da rede do deployment (o da primeira cell é `LOOM_ROUTER_UPSTREAM_URL`, gravado pelo prepare, de modo que registrá-la não muda nenhuma rota; o de uma cell posterior é gravado pelo attach), nunca no host público da cell, que nomeia o próprio router; antes de a primeira cell ser registrada, a cell da regra 4 é o deployment diante do qual o router foi colocado (`LOOM_ROUTER_UPSTREAM_URL`). O host compartilhado é `LOOM_ROUTER_SHARED_HOST`, o host próprio da primeira cell e os aliases dessa cell em `hosts`. Ele encaminha cada requisição como o cliente a enviou (seu caminho e query brutos, seu próprio `Host`) com a visão do próprio router sobre endereço, esquema e host do cliente em `X-Forwarded-For`, `-Proto` e `-Host`, substituindo o que o cliente declara ali (por isso cada cell lista os endereços do router em `LOOM_TRUSTED_PROXIES`, ou então conta todo cliente como o router nos limites de login e lê o esquema como o do router), descarta os cabeçalhos que descrevem uma única conexão (e os que `Connection` nomeia), mantém toda linha de cabeçalho da resposta (um segundo `Set-Cookie` incluído), faz streaming de ambos os corpos (um stream de server-sent events incluído), responde 502 quando a cell não pode ser alcançada e nunca encaminha um caminho sob `/cells/` (um que ele não serve responde 404). Uma primeira cell registrada sem endereço de upstream responde 503, como a de qualquer cell. Ele lê as cells, seus emissores e hosts uma vez a cada `LOOM_ROUTER_DIRECTORY_CACHE_S`, respondendo pela última tabela enquanto uma desatualizada é lida de novo em segundo plano, de modo que um diretório que trava ou não pode ser lido não atrasa nenhuma requisição; antes de sua primeira leitura, ele responde 503. Um emissor que duas cells listam (o que o attach recusa) não roteia para nenhuma.

Um cliente encontra a cell de uma org com `GET /cells/lookup?org=<org>`, sem autenticação e com limite de taxa; uma org sem placement responde a primeira cell com o mesmo corpo de uma org colocada ali (apenas a resposta de uma org colocada é armazenada em cache, então o tempo de resposta e uma indisponibilidade ainda podem distingui-las: o nome de uma org não é segredo), e antes de qualquer cell ser registrada o lookup responde 503 com o código `NO_CELL`; um diretório que ele não consegue ler para uma org que não está em cache responde 503 com `BACKEND_UNAVAILABLE`. Um cliente confere o código, nunca a mensagem: em `NO_CELL`, e em um 404 de um deployment sem router, `loom login --org` mantém o host que recebeu (o host compartilhado, servido como antes pela regra 4, ou o deployment único); em um 429 ele espera o `Retry-After` e pergunta de novo, por no máximo `--wait` (padrão 2 minutos), depois recusa como recusa por limite de taxa; em qualquer outra resposta ele recusa, nomeando o router, de modo que nenhum fluxo comece em uma cell que pode não conter a org. A cell de uma org é pública, como a URL de um workspace: o lookup revela que uma org fora da primeira cell existe. `loom login --org`, a página de login da primeira cell (que pergunta a org primeiro) e as linhas de setup iniciam todo fluxo OAuth, device e de login no host da cell da org, de modo que nenhum fluxo cruza cells; um navegador é redirecionado apenas para o início de um fluxo novo.

Somente a primeira cell chega a estar em `recording`: uma cell cuja linha `first` é `(false, …)` trata qualquer modo diferente de `enforced` como `enforced` em seus triggers de org, em seu cache de modo e em sua CLI, de modo que a linha de modo de uma cell posterior definida como `recording` não registra nada. Uma cell em `recording` lê seu próprio estado com `GET /cells/v1/cells/<cell>`, com sua credencial do router, respondido apenas para a cell que o índice único do diretório nomeia primeiro e aceito apenas quando o SHA-256 da credencial é um dos hashes que o diretório registra para `<cell>`, dentro de `LOOM_CELL_ROUTER_TIMEOUT_S` (padrão 5 s, o timeout da chamada de placement); uma resposta recusada, falha ou tardia conta como ilegível.

Com o diretório inacessível, as regras 1 a 4 roteiam a partir das cells em cache do router; um lookup de uma org que ele não tem em cache e um placement respondem 503.

As rotas do operador, cada uma com a credencial de operador do router, ficam em `/cells/v1/operator/cells/<cell>`: `limit`, `credential` e `register` (o do prepare), a própria linha (`GET`, com o nome de qualquer outra linha, que o attach lê para suas recusas, sua verificação de conclusão e sua retomada), o attach da primeira cell (`attach/begin`, `attach/place`, `attach/enforced` e `attach/serving`, nessa ordem) e `DELETE`. Cada uma, exceto `register`, responde a uma cell que o diretório não possui com 404 e o código `NO_SUCH_CELL`, de modo que um 404 sem ele indica uma réplica do router de uma build anterior. Toda chamada bem-sucedida, a leitura incluída, é gravada no log de auditoria (uma repetição de registro, que não muda nada, não grava nenhuma), e toda chamada executa um número fixo de instruções, cada uma limitada pelo statement timeout do diretório (aplicado também no servidor do diretório); a leitura e a remoção carregam os limites derivados que `loom cell` aguarda. Toda alteração toma o lock do diretório e depois o lock da linha da cell (a ordem que o registro, as alterações do operador e a remoção seguem). Toda réplica do router executa uma build com essas rotas antes de o attach da primeira cell começar: o placement de uma build anterior colide com o de um lote de attach como um 503 transitório, e seu registro pode adicionar uma cell posterior durante o attach, o que faz toda chamada de attach recusar até que ela seja removida. Cada chamada de attach recusa, a menos que a linha seja a da primeira cell, esteja `attaching` e registre o id de deployment que a chamada nomeia, e enquanto existir qualquer outra linha (o registro recusa uma cell posterior até a primeira estar `serving`, e uma réplica do router de uma build anterior ainda poderia registrar uma):

- `attach/begin` traz as impressões digitais da cell exatamente como `loom.cell_self` as armazena, a impressão digital de seu segredo raiz, as listas de permissão pelas quais o attach julgou os stores e nonces novos. Ele recusa uma impressão digital cujo id de deployment, bucket, prefixo, host público ou host do emissor difiram dos da linha (um host de emissor diferente do próprio da cell é recusado), e uma cujo corpo de placement real, com um nome de org no comprimento máximo, ultrapassaria o limite de corpo do router, nomeando os maiores campos. Fora de qualquer transação do diretório, ele executa a probe do registro no `probe_url` da linha e pergunta ao `upstream_url` da linha o id de deployment, de modo que os stores que o attach lê sejam aqueles de que a cell em operação serve. Depois, sob os locks, grava `attach_started_at` uma vez, as impressões digitais somente enquanto a linha não tem nenhuma (outras impressões digitais ali respondem 409: elas mudam com `loom cell update`) e `keys_fp` em toda chamada, e audita as listas de permissão. É idempotente. Seu corpo tem limite próprio, 256 KiB, acima do qual responde 413.
- `attach/place` coloca um lote de no máximo 500 orgs (dentro do limite de corpo de 64 KiB) depois que `begin` foi executado e o modo do diretório é `enforced`, além da marca de fechamento e da cota da cell. Ele executa um número fixo de instruções: um insert do lote que ignora uma org já colocada, uma leitura que recusa o lote (com rollback, nomeando a org) quando uma org está colocada em outra cell, uma contagem e uma linha de auditoria por org que colocou e uma para a chamada. Um placement real da mesma org corre ao lado dele quando o modo é `enforced`; ambos seguram o lock da linha da cell, um placement real lê o vencedor de novo quando o obtém, e todo insert ignora uma org já colocada e conta apenas o que gravou, de modo que a org é colocada e contada uma vez.
- `attach/enforced` registra o modo do diretório `enforced`, depois do qual a primeira cell coloca novas orgs em tempo real; é idempotente.
- `attach/serving` executa a probe e a verificação de upstream de novo, depois, sob os locks, calcula os emissores da linha a partir de suas impressões digitais (`bearer_issuers` de `current` e `next`) e marca a cell como `serving`; em uma cell `serving`, responde na hora, com auditoria. `begin`, `place` e `enforced` em uma cell `serving` respondem 409.

`DELETE` remove a linha de uma cell que está `attaching` sem nenhum placement, qualquer que seja o modo do diretório, com suas linhas de host, com auditoria: uma primeira cell é desfeita até seu primeiro placement, e uma cell posterior registrada por uma build anterior deixa de bloquear o primeiro attach. Um placement em andamento ou é confirmado primeiro, e a remoção recusa, ou não encontra linha. O router não consegue ler os stores da cell, então `loom cell remove`, que o chama, verifica antes o Postgres de uma primeira cell ([Operações](#operations)).

<a id="scim-and-sign-in-across-cells"></a>

## SCIM e login entre células

Cada célula é uma aplicação própria no provedor de identidade: um cliente OIDC para o login em seu host e uma aplicação SCIM que provisiona o seu `/scim/v2`, ambos atribuídos ao mesmo grupo de pessoas, as pessoas das orgs dessa célula. Uma pessoa que o provedor de identidade desativa, exclui ou remove da atribuição é desprovisionada em todas as células cuja aplicação lhe estava atribuída; o Loom não consegue ver uma aplicação configurada para admitir pessoas não atribuídas ou para não desativar ao remover a atribuição, então o operador configura cada aplicação para exigir atribuição e para desativar ao remover a atribuição. Cada célula mantém seus usuários SCIM, desativações e tombstones exatamente como uma implantação única ([SSO e SCIM](../operations/sso.md)); o roteador não guarda nenhuma identidade.

Uma célula em `enforced` com SCIM ativado exige SCIM: uma pessoa só age na célula enquanto o SCIM dela mantiver uma linha ativa para ela. A verificação é feita na conta da pessoa, não na forma como um subject é escrito: `scim.refuses_subject(subject)` considera as grafias de email do próprio subject e os emails de contato e verificados das contas ativas que ele possui, incluindo contas de senha, e recusa se qualquer um deles não tiver linha ativa (a linha cujo id é `scim.user_id(email)`; uma linha inativa ou excluída com esse id sempre recusa, e um email sem linha própria só passa como endereço de trabalho de uma linha ativa cujo email principal seja uma das grafias do próprio subject). Um principal fora do SCIM pertence a uma lista fechada: um bot (um subject cujo `sa:<name>`, antes de qualquer `:` adicional, nomeia uma linha de `loom.machine_identities`: o bot e seus agentes, como `check_acted_for` o interpreta), um subject de máquina ou workload, um usuário final (`user:`). Qualquer outro subject `sa:`, uma conta de serviço pessoal, é verificado por meio de seu criador (`service_accounts.sa_creator`) e de todo subject que o `lineage` de sua linha lista, de modo que agir em nome dela é agir em nome da pessoa que a criou; todo outro subject nesses caminhos é uma pessoa e é recusado, a menos que tenha uma grafia de email ou possua uma conta com uma. Uma leitura SCIM que falha gera `SignInUnavailable`, como faz `scim.deactivated`, nunca um `OAuthError`, de modo que um refresh devolve seu token e nenhuma sessão termina por causa de uma indisponibilidade. Todo caminho que admite uma pessoa o utiliza:

- um login SSO, antes de a conta ser encontrada ou criada (JIT), com o email que o provedor de identidade verificou;
- todo refresh de uma sessão (`_refresh_session`), com o email verificado que o grant armazena ou, para um grant sem email, as contas do subject;
- um ato em nome de uma pessoa (`authz.check_acted_for`), qualquer que seja a grafia do subject;
- o subject de um token de gateway, verificado no momento da verificação e lembrado como as outras respostas da verificação;
- o criador de um token delegado ou de uma conta de serviço pessoal (`authz._check_creator`), em cache como a verificação de atuação em nome de alguém, de modo que um token criado por uma pessoa nunca provisionada é recusado quando a célula está `enforced`.

Com SCIM ativado, o login por senha e o autocadastro já ficam fechados (`oauth_login_provider=oidc`), e o fluxo de dispositivo faz login por SSO, portanto passa pela mesma verificação. Principais sem email estão fora do SCIM e são desprovisionados por quem os criou: bots e tokens emitidos por um operador, usuários de emissores de usuário final pelo serviço que assina por eles, a confiança de workload por sua regra.

**Break-glass.** Cada célula possui um bot administrador de plataforma com prova por chave (`loom bot create --platform-admin`), que não tem email e que o SCIM nunca revoga, e um segundo, separado, para attach e `loom cell each`. As chaves ficam no cofre de segredos do operador, lidas a cada uso; o diretório registra apenas o nome e a impressão digital da chave de cada bot, de modo que o roteador não guarda credencial de nenhuma célula, e todo uso do bot break-glass consta no log de auditoria da célula e é apontado por `loom doctor`. Os operadores são atribuídos às aplicações de todas as células para usar o painel e acessam uma célula cuja aplicação no provedor de identidade esteja com defeito por meio do bot dela.

**Os tokens SCIM de uma célula são só dela por construção.** O nome de uma célula corresponde a `[a-z0-9-]{1,32}` (sem `_`, comparado exatamente; verificado em prepare, attach e boot, e único no diretório). Um token que começa com `lsc_` nomeia uma célula e deve corresponder por inteiro a `lsc_[a-z0-9-]{1,32}_[0-9a-f]{32,}`, ou toda célula o recusa; somente um token sem esse prefixo não nomeia nenhuma célula. Com `LOOM_CELL` definido, um token SCIM tem a forma `lsc_<cell>_<secret>`, como `loom cell scim-token new --cell <name>` o gera (roda offline): exatamente três campos separados por `_`, o do meio igual a `LOOM_CELL`, e um segredo de pelo menos 32 caracteres hexadecimais minúsculos (128 bits).

Um parser ao lado de `scim.tokens()` devolve o nome da célula de um token ou nada, e quem o chama compara o resultado com `LOOM_CELL` e com a linha `first` da célula (abaixo). `scim_settings_problem` recebe ambos, de modo que o boot e `loom doctor` recusam por meio de uma única função, e com `LOOM_CELL` definido recusam um terceiro token configurado (dois permitem uma rotação). Uma função, `scim.authorized(presented)`, concentra a comparação em tempo constante para as duas rotas SCIM (`_authorized`, `scim_groups._caller`). Com `LOOM_CELL` indefinido, compara com os tokens configurados como uma implantação única faz e não lê nada. Com ele definido, compara com os tokens que o parser admite para `LOOM_CELL` e com a linha `first`; enquanto essa linha não foi lida, gera erro como `AuthUnavailable` faz, de modo que as rotas respondem 503 com `Retry-After` e o provedor de identidade tenta de novo.

- Um processo se recusa a iniciar com um token SCIM que nomeia outra célula, e `/scim/v2` responde 401 a um token assim.
- Uma célula posterior recusa um token que não nomeia nenhuma célula. A primeira célula, que mantém o host compartilhado, também aceita seus tokens existentes sem nome, somente enquanto sua linha `first` for `(true, LOOM_CELL)`, de modo que seu SCIM continue funcionando durante a transição sem rotação.
- Um token copiado para as configurações de outra célula, ou uma aplicação do provedor de identidade apontada para o host da célula errada, portanto falha de forma fechada, e nenhuma célula jamais compara seus tokens com os de outra.

<a id="preparing-a-cell"></a>

## Preparando uma célula

`loom cell prepare` registra uma célula no diretório e grava as linhas que os processos da célula leem. A verificação de tenancy (abaixo) diz o que ele grava e como retoma; esta seção diz como ele prova que recebeu a célula certa.

**Qual célula é a primeira** nunca é uma configuração nem a ordem de preparação. O prepare só concede isso à célula cujo host é o host compartilhado do roteador e cujo id de deployment é o que o upstream de pass-through do roteador informa no endereço de probe do upstream. Esse endereço é a configuração do próprio roteador (`LOOM_ROUTER_UPSTREAM_PROBE_URL`) e o `probe_url` da primeira célula, nunca um dos argumentos do prepare: o roteador o lê ali a cada prepare e a cada nova execução, e recusa uma resposta com outro id de deployment ou sem id. O roteador também consulta a porta principal do seu upstream de pass-through (`LOOM_ROUTER_UPSTREAM_URL`, que o registro copia para a linha) e recusa uma que informe outro id de deployment, de modo que a linha nunca nomeia dois deployments. O prepare recusa toda outra célula até que a primeira esteja registrada e `serving`, sob o lock do diretório, de modo que nenhuma célula posterior apareça enquanto a primeira é anexada.

**Os stores são os ativos.** Antes de gravar qualquer coisa, o prepare grava um nonce aleatório no Postgres de destino, sob uma chave reservada do `loom._migration_lock` do ClickHouse de destino, e como um objeto de probe sob o prefixo do bucket de destino. A rota de probe da célula deve devolver cada um deles conforme o lê da sua própria conexão com o primário naquela requisição. Os nonces são então apagados; suas linhas não guardam memória nem org e são classificadas assim. Uma cópia com o mesmo id de deployment é recusada. O prepare envia ao roteador os três nonces junto com o registro (`POST /cells/v1/operator/cells/<cell>/register`, com a credencial de operador); o roteador lê ele mesmo o listener de probe e só registra a célula quando este responde o id de deployment da célula, esses nonces, e o bucket e o prefixo que o registro nomeia (assim, um registro que não nomeia bucket não pode passar por uma célula que tem um). O roteador responde como `PROBE_BUSY` um listener acima da sua taxa (429), e como `PROBE_UNREACHABLE` um que ele não consegue alcançar ou que não consegue ler um store, ou a porta principal do upstream de uma primeira célula que ele ainda não alcança, ambos com 503, e o prepare espera por eles dentro de `--wait`. Uma nova execução cujos endereços diferem dos da linha é recusada antes do probe.

**A rota de probe**, `GET /v1/health/deployment`, não recebe credencial e fica fora do gate de fingerprint:

- Na porta principal, ela responde apenas o id de deployment que cada processo verificou na inicialização, mantido em memória, seja qual for o Host da requisição. Não lê nenhum store e não tem limite de taxa, então nenhuma enxurrada na porta principal trava a verificação de host do attach.
- Completa, com os valores sob as chaves de nonce reservadas do prepare, ela é servida apenas em uma porta separada de listener de probe (`LOOM_CELL_PROBE_PORT`, `0` para desligado; exige a autoridade do Postgres e a autenticação ligadas, e escuta em todos os endereços do host): a linha do Postgres `loom.cell_probe` e a chave do KeeperMap, ambas chamadas `cell-probe`, e o objeto `<prefix>/_cell_probe`; ela também nomeia o bucket e o prefixo onde armazena. Nenhum desses valores é secreto. Responde 429 acima de uma taxa fixa por processo nesse listener.
- Somente o endereço de probe da célula (`probe_url`) aponta para o listener: um endereço interno que nenhum ingress expõe e para o qual o roteador nunca roteia, que o deployment envia a todo processo vivo, pronto ou não, de modo que o prepare e o attach alcançam um processo que o gate mantém como não pronto.
- O listener de probe do chart vem desligado por padrão e é ligado, inclusive no deployment existente, antes do primeiro `loom cell prepare`. Ele renderiza um Service nessa porta que publica endereços não prontos e não altera nenhuma NetworkPolicy. Quem pode alcançar a porta é decisão da network policy do operador, como ocorre com a do ClickHouse: com a policy própria do chart ligada, que admite apenas a porta `http`, o operador adiciona uma policy que admita a porta de probe a partir dos pods do roteador (o registro do prepare é sondado pelo roteador) e de onde a CLI roda (as policies se somam), e sem ela qualquer pod pode alcançá-la. Do mesmo modo, o egress da célula para o endereço de placement do roteador e para o Postgres é responsabilidade do operador, por meio de `networkPolicy.extraEgress` quando a policy do chart está ligada.
- O prepare tenta de novo os `PROBE_BUSY` e `PROBE_UNREACHABLE` do roteador (um listener em inicialização fica inalcançável por um tempo), e o attach e o `loom cell update --probe-url` tentam de novo um 429 do listener que sondam, por no máximo `--wait` (padrão 10 minutos), e então o nomeiam.

O `probe_url` de uma célula posterior é entregue ao prepare, normalizado (`http` ou `https`, sem userinfo), e aceito apenas quando informa o id de deployment que o prepare lê dos stores de destino; o attach então verifica que o host da própria célula informa o mesmo id. O prepare recusa um argumento de endereço de probe para a primeira célula. `loom cell update --probe-url` substitui o de uma célula posterior sob o lock do diretório, após a mesma verificação e um novo eco de nonce. Os endereços de probe e de upstream da primeira célula são copiados da configuração do roteador no registro, e `loom cell update` os substitui a partir dela, depois que toda réplica do roteador roda os novos valores, após o mesmo probe e uma verificação de que o novo upstream informa o id de deployment da célula. Uma nova execução do prepare nunca move um endereço: ela recusa enquanto a réplica do roteador que alcança estiver configurada com outros.

**A linha `first`.** O prepare grava uma linha `first` explícita e imutável no Postgres de cada célula (`loom.cell_first`: um trigger de linha recusa seu update e delete e um trigger de statement seu truncate, ambos criados `ENABLE ALWAYS` e verificados pelo attach e pelo `loom doctor` como os triggers de org), `(true, <cell>)` para a primeira célula e `(false, <cell>)` para qualquer outra, na mesma transação do modo de tenancy, de modo que nenhuma exista sem a outra. Lidas em conjunto, as duas linhas dão um de quatro estados. Ambas ausentes, sem linha de tenancy nem própria: aberto, de um deployment único, sem verificação; um processo com `LOOM_CELL` definido permanece não pronto ali (sem linha de modo). Em uma célula que o diretório registra, isso só acontece quando o Postgres dela é restaurado para antes do seu prepare (fechado por `loom cell restore`) ou quando um prepare para entre o registro e a gravação da linha (fechado por uma nova execução do prepare), e os triggers e os processos com `LOOM_CELL` não definido permanecem abertos nesse meio tempo, como em um deployment nunca preparado, já que o Postgres da célula, que guarda o seu estado, não distingue os dois casos (o ClickHouse não carrega estado da célula: adicioná-lo faria o caminho de escrita ler um segundo store para tenancy) e tal processo nunca lê o diretório; onde todo processo tem `LOOM_CELL` não definido, o apontamento do `loom doctor` (que lê o diretório e vem com o attach) é o único sinal. Uma linha `(true, …)` com `recording` e a linha própria: a primeira célula em gravação. Ambas presentes com a linha própria de outro modo: `enforced`, com o modo de uma célula posterior lido como `enforced` seja o que disser sua linha (seus triggers, cache de modo e CLI o tratam assim, e `loom cell prepare` nomeia uma linha que não é). Qualquer das linhas sozinha, ambas sem a linha própria, ou ambas ausentes ao lado de uma linha de tenancy ou própria (os estados `partial`: alguma linha de estado presente, mas não todas entre `first`, modo e própria), que o prepare nunca deixa: os triggers de org, onde a célula os tem, agem como em `enforced` (uma inicialização não cria nenhum nela), todo processo fica sob gate como por uma linha não lida, o `loom doctor` o nomeia, e uma nova execução de `loom cell prepare` grava de volta as linhas ausentes a partir do diretório enquanto a célula roda. Um índice único no diretório permite uma só primeira célula, e o valor não guarda conteúdo de memória e é classificado assim. O attach reverifica o host e o id de deployment. Um processo lê o valor junto com o modo, e "ainda não lido", "ausente" e "falso" são distintos: só uma linha que foi lida deixa um processo pronto, e uma linha cuja célula não é `LOOM_CELL` o mantém sob gate (abaixo) e é reportada por `scim_settings_problem`. A inicialização recusa apenas pelas configurações (`scim_settings_problem` com a linha não lida verifica só a forma do token contra `LOOM_CELL`); a comparação com a linha `first` roda por requisição depois que a linha é lida, e o `loom doctor` lê o valor do Postgres da célula ou o reporta como desconhecido.

<a id="the-cells-own-row"></a>

## A linha própria da célula

**Verificada continuamente, não apenas no attach.** Cada processo calcula o fingerprint de sua configuração: campos nomeados sob uma versão de schema (os hosts de seu `LOOM_PUBLIC_BASE_URL` e do `LOOM_OAUTH_ISSUER` resolvido, seu emissor OIDC e client id, se o SCIM está ativado, os emissores de bearer que ele aceita (`trusted_issuers()` e seus emissores de usuário final), suas orgs configuradas, as `orgs` de seu `jwt_end_user_issuers_json`, o deployment id que ele verificou no boot, e o nome e o prefixo do bucket). Seu Postgres e seu ClickHouse carregam ambos o deployment id (a verificação de pareamento que um deployment único já executa). O bucket, quando há um configurado, é identificado por seu nome e prefixo (uma célula sem bucket não participa de nenhuma verificação de bucket), que duas células não compartilham em nenhum endpoint: o prefixo de nenhuma célula em um bucket de mesmo nome fica aninhado dentro do de outra nem o contém, de modo que um endpoint tomado do ambiente não pode cair no prefixo de outra célula. Nenhum store é identificado por seu endereço, então a troca de um standby, um failover, um endereço tomado do ambiente ou uma restauração para um novo endpoint não altera nada do que a célula verifica. O deployment id, o nome do bucket e o prefixo nunca mudam após o prepare: `loom cell update` recusa a mudança de qualquer um deles, inclusive adicionar um bucket a uma célula preparada sem um, e mover os dados de uma célula para outros stores, ou adicionar um, é um desenho à parte (Operações). Os bots não entram nele: `loom bot create` os adiciona em tempo de execução, cada um com uma org, e a colocação já os limita. A linha própria da célula em `loom.cell_self` contém um fingerprint aceito, `current`, e, durante uma mudança, um segundo, `next`. Suas linhas, e o heartbeat da célula (`loom.cell_heartbeats`, indexado pela linha do processo em `loom.instances` e escrito junto com ela, já que uma tabela KeeperMap não aceita nova coluna: o `LOOM_CELL` com que ele executa, o estado que sua última leitura produz, seu fingerprint e [as chaves ao lado dele](#the-heartbeats-keys)), não contêm conteúdo de memória e são classificadas assim para fins de exclusão. Um processo corresponde a um valor quando todos os campos que ambos conhecem são iguais, e um campo que o valor não tem conta como igual apenas no seu padrão, de modo que os builds N e N+1 com a mesma configuração correspondem a uma única linha. Com `LOOM_CELL` definido, enquanto a linha própria de sua célula, a linha `first` ou a linha de modo não foi lida, as linhas de estado estão em estado `partial` (a regra da linha `first`, acima), a linha própria da célula está ausente ou não corresponde a nenhum dos dois valores, ou a linha `first` nomeia outra célula, toda requisição que ele recebe, exceto seus health probes e a rota de probe, responde 503 `AuthUnavailable`: requisições bearer (a primeira verificação em `verify_token`, antes do ramo de emissor de usuário final), SCIM, e toda rota que faz login, emite, renova ou troca um token, incluindo signup, os endpoints OAuth e de dispositivo e o callback de login. Ele também se reporta como não pronto e se afasta dos workers como faz um processo com drift: interrompe suas passagens, devolve o lease e não nomeia nenhum lease em sua linha de instância, em todos os modos de `LOOM_BACKGROUND_WORKERS` (inclusive `on`: sem lease para manter, o processo `on` de uma célula executa o loop de lease, que inicia e para o supervisor no gate dentro de `LOOM_TENANCY_MODE_CACHE_S` e um período do loop), e sua linha de instância não o conta como servindo, de modo que um build mais antigo ao lado dele mantém os workers em execução. Uma leitura com falha mantém as últimas linhas por no máximo `LOOM_JWT_REVOCATION_MAX_STALE_SECONDS`, como fazem o keyring de assinatura e o snapshot de revogação; passado esse tempo, o processo fica bloqueado até que uma leitura seja concluída. O modo (abaixo) tem o mesmo limite. Uma célula configurada para aceitar o emissor de outra célula, um host com o qual não foi anexada ou um padrão de org que não lhe foi concedido, portanto, não serve nada com isso, onde quer que a requisição chegue a ela. Seu heartbeat reporta o fingerprint, o router recusa uma chamada de colocação cujos campos não correspondem a nenhum dos dois valores da linha do diretório, e `loom doctor` nomeia uma divergência, uma célula cujo cliente OIDC outra célula compartilha, e uma célula com SCIM desativado ao lado de células com ele ativado.

`loom cell prepare` escreve `current` em `loom.cell_self` junto com as linhas `first` e de modo. Até que a linha do diretório contenha um fingerprint (antes do attach), ele escreve, quando a linha própria está ausente, o fingerprint que todo processo vivo reporta em seu heartbeat, recusando enquanto algum processo vivo executar um build anterior ao N, que não reporta nenhum, ou enquanto os fingerprints reportados forem diferentes; uma linha própria presente permanece, e um processo vivo que executa nem seu `current` nem seu `next` recusa; a cada execução, antes de se registrar, ele recusa um processo vivo cujo deployment id, nome de bucket ou prefixo difira da configuração do próprio prepare (um campo que ele não reporta é lido em seu padrão). Quando a linha do diretório já contém um fingerprint, uma nova execução escreve `current` e `next` a partir dele, como faz `loom cell restore`, recusando apenas enquanto o fingerprint de um processo vivo não corresponder a nenhum dos dois. De qualquer forma, a célula permanece pronta durante o rollout de `LOOM_CELL`. Uma mudança posterior de configuração, enquanto a célula está `attaching` ou `serving`, passa por `loom cell update`; antes que o attach tenha escrito o fingerprint da célula na linha do diretório, ele escreve `next` somente em `loom.cell_self`, sem reservar nada (e `--finish` e `--revert` então tocam apenas `loom.cell_self`), e um attach retomado executa suas recusas de sobreposição com, e escreve no diretório, ambos os valores de `loom.cell_self`. Os emissores e padrões de org de uma célula preparada não são reservados em lugar nenhum: o router não roteia nenhum emissor para ela e não aceita nenhuma colocação dela até que o attach tenha executado suas recusas de sobreposição e escrito sua linha no diretório.

Uma mudança no que uma célula aceita passa por `loom cell update --config <file>`, sob o mesmo lock do diretório que o attach; ele calcula `next` a partir dessa configuração com a mesma função que os processos usam, e recusa enquanto um `next` diferente estiver pendente; uma nova execução com a mesma configuração, ou `--finish`, primeiro escreve `next` onde ele estiver ausente. Ele executa novamente as recusas de sobreposição do attach contra a linha de diretório de toda outra célula, contando ambos os valores de cada uma, escreve o novo fingerprint como `next` no diretório e depois em `loom.cell_self`, de modo que processos antigos e novos servem ambos enquanto a nova configuração é distribuída. `loom cell update --finish` espera, no máximo `--wait` (padrão 10 minutos), até que todo processo vivo reporte `next` e o maior prazo de requisição tenha passado; quando a mudança ativa o SCIM ou altera o cliente OIDC da célula, ele então executa a precondição de SCIM do attach (recusando, ou `--revoke-unprovisioned`); só então torna `next` o único valor, primeiro em `loom.cell_self` e por último no diretório. Um emissor ou padrão ampliado é reservado no diretório antes que qualquer processo o aceite, e um reduzido só é liberado depois que nenhum o aceita. Uma recusa ou uma espera que se esgota mantém ambos os valores e imprime os processos que ainda não reportam `next`; executar novamente retoma, e `loom cell update --revert` espera da mesma forma até que nenhum processo vivo corresponda apenas a `next` (recusando quando `--wait` se esgota), depois descarta `next`, primeiro em `loom.cell_self`, sob o mesmo lock; uma org configurada que a atualização colocou permanece colocada na célula (uma colocação nunca é excluída).

Um novo token SCIM não altera nenhum fingerprint e não precisa de `loom cell update`. Uma célula muda a aplicação de seu provedor de identidade como um deployment único ([SSO e SCIM](../operations/sso.md)), com os mesmos limites; nada nisso é particular às células.

<a id="the-heartbeats-keys"></a>

### As chaves do heartbeat

O heartbeat de célula de um processo reporta, ao lado do `LOOM_CELL` com que ele executa, seu modo de tenancy: o estado que sua última leitura das linhas de estado produz (`open`, `recording`, `enforced` ou `partial`, com o `recording` de uma célula posterior lido como `enforced`), e `''` somente antes de uma leitura ser concluída ou em um processo que nunca as lê. Seu JSON `fingerprint` contém a versão e os campos do fingerprint, e ao lado deles chaves que nenhum fingerprint compara: elas nunca estão em `loom.cell_self`, nos fingerprints do diretório nem no corpo de uma colocação, e uma linha KeeperMap não precisa de coluna para elas (`server.core.instances.cell_row`). Sua presença prova o build que as reporta; não há chave de geração.

- `serves`: `LOOM_SERVES_TRAFFIC`.
- `marked`: se as sessões Postgres em pool deste processo mantêm o marcador de tenancy (`loom.tenancy_checked`). O refresh da célula o lê junto com as linhas de estado, após `RESET ALL` na mesma transação: um valor de startup sobrevive ao reset, um `SET` não, de modo que um pooler, que uma célula não deve ter ([Postgres](https://github.com/ClickHouse/loom/blob/main/docs/operations/postgres.md)), é lido como não marcado, quer remova os parâmetros de startup, quer os envie de novo com `SET`, e a leitura é executada na conexão de servidor em que o reset foi executado. Custa três idas e vindas a mais por refresh (`BEGIN`, `RESET ALL` e `COMMIT`), e todo processo reporta um valor novo sem prober. Uma sessão que o pooler removeu, ou um processo com `LOOM_CELL` não definido sob a autoridade Postgres, lê `false`; `null` significa apenas que nenhuma leitura foi concluída (um boot com migrações Postgres ignoradas cuja primeira leitura falhou, ou a autoridade ClickHouse, em que nada é lido). A verificação de readiness `cell_marker` de um processo com `LOOM_CELL` definido lê o mesmo valor: ela falha em `enforced` e `partial`, onde os triggers recusam as escritas de uma sessão não marcada para uma org que a célula não possui e a linha de tenancy de cada org recém-colocada, e passa com um aviso em `recording`, de modo que uma primeira célula atrás de tal pooler não trava seu rollout antes do attach, que recusa o processo ([solução de problemas](https://github.com/ClickHouse/loom/blob/main/docs/operations/troubleshooting.md#the-cell_marker-readiness-check-names-the-tenancy-marker)).
- `router` e `router_why`: com `LOOM_CELL` definido, `router` é `null` e `router_why` diz por quê até que a linha `first` seja lida, e depois ambos estão presentes somente em uma primeira célula (a rota de estado de uma célula posterior responde 404, então ela nem pergunta nem os reporta). `router` indica se a última leitura deste processo de `GET /cells/v1/cells/<cell>` com sua própria credencial respondeu 200 nomeando a célula: `null` até que a primeira leitura responda (ainda em boot), `true` com tal resposta, e `false` para uma configuração que nunca funciona: uma configuração do router não definida (o processo nunca pergunta), uma URL que não pode ser requisitada, um 404, uma resposta para outra célula, ou um 401 que persiste além de um `LOOM_ROUTER_DIRECTORY_CACHE_S`. Essa é a configuração do router, que os processos da célula também carregam: os hashes de credencial em cache de uma réplica do router respondem 401 para um token rotacionado há pouco, então até lá o último valor permanece e `router_why` diz "401, possibly a rotation" (um router executado com um cache maior pode fazer o attach recusar durante uma rotação; uma nova execução resolve). Uma falha transitória (um timeout, um erro de conexão, um 503, um 200 que não é um objeto JSON nomeando a célula) mantém o último valor, e enquanto `router` é `null`, `router_why` nomeia a última causa desse tipo.

Um leitor em segundo plano por processo de servidor consulta a rota a cada `min(LOOM_INSTANCE_HEARTBEAT_SECONDS, LOOM_TENANCY_MODE_CACHE_S)` em uma primeira célula até ler `serving` (`server/core/cell_router.py`); nada espera por ele, então um router lento não atrasa nenhum beat nem nenhuma requisição, e o beat reporta o que leu por último, com no máximo um período mais `LOOM_CELL_ROUTER_TIMEOUT_S` de idade quando um beat o escreve. Todo processo uvicorn executa um, de modo que mil pods de oito workers consultam até 1,600 vezes por segundo. Cada réplica do router responde a rota a partir de sua própria leitura da linha da célula, feita no máximo uma vez por `LOOM_ROUTER_DIRECTORY_CACHE_S`, então o diretório vê uma leitura por réplica por janela; uma leitura com falha responde 503 durante sua janela, de modo que um diretório fora do ar não é consultado com mais frequência e continua respondendo 503. Um processo sem heartbeat (uma CLI, o filho dos workers) consulta sob demanda, no máximo uma vez a cada `LOOM_TENANCY_MODE_CACHE_S`.

O leitor também mantém a última resposta do diretório para a verificação de tenancy, como uma de quatro: `attaching`, `serving`, outro estado, ou desconhecido enquanto nenhuma leitura foi concluída, a última falhou, respondeu sem nenhum estado desta célula (um 401, um 404 ou uma resposta para outra célula), ou está atrasada (mais antiga que o período em que este processo lê, o do leitor em segundo plano ou a janela sob demanda, mais `LOOM_CELL_ROUTER_TIMEOUT_S`). Quando um processo leu `serving`, ele mantém isso durante toda a sua vida e não pergunta mais: apenas `loom cell restore` o rebaixa, e isso reinicia os processos. Uma primeira célula em `recording` registra uma nova org somente em `attaching`. Um principal de plataforma ou de org `*`, ou uma chamada sem principal, não coloca nenhuma org: em uma primeira célula cujo diretório responde `attaching`, ou não pode ser lido antes de ela ter lido `serving`, tal chamada para uma org que a célula não possui responde 503 ("attach in progress" ou diretório ilegível), já que os dados da org podem estar nesta célula até que o attach a coloque; uma vez `serving`, e em uma célula posterior, ela responde 409 `WRONG_CELL`. Um processo que inicia após `serving` com o router fora do ar responde essas chamadas com 503 até que sua primeira leitura seja concluída.

Com `LOOM_CELL` definido, um `LOOM_CELL_ROUTER_URL` não vazio que não seja uma URL http(s) com host, porta de no máximo 65535 e sem `?` nem `#` recusa o boot (`cell.settings_problem`); um vazio é permitido, para pods somente de workers (`LOOM_SERVES_TRAFFIC=false`), cujo heartbeat então reporta `router` como `false` com as configurações não definidas em `router_why`, sem registrar nada em log.

<a id="the-tenancy-check-and-placement"></a>

## A verificação de tenancy e o posicionamento

Um processo com `LOOM_CELL` não definido não executa nenhuma verificação de tenancy. Sob a autoridade Postgres
(`LOOM_STORAGE_AUTHORITY=postgres`, a única que uma célula executa), um processo de servidor ainda lê,
em uma única instrução, a linha de modo, a linha `first` e se existe alguma linha em `loom.tenancy` ou
`loom.cell_self`, com as regras do cache de modo (abaixo). Um comando da CLI que
declara uma org lê o mesmo em uma sessão limitada própria: toda tabela de estado ausente
(julgada por `to_regclass`, de modo que um papel sem um grant ainda assim recusa) é lida como `open`, e uma
leitura que falha recusa. Um comando sem org, como a revogação de um token, `loom migrate` ou
`loom doctor`, não é controlado pelo modo e executa, inclusive durante uma indisponibilidade do Postgres. Com as duas linhas
ausentes e sem linha de tenancy nem própria, como em um deployment nunca preparado, ou na primeira célula em
`recording`, o processo continua servindo como um deployment único; todo estado que a regra da linha `first`
(acima) conta como `enforced`, ou como um estado `partial`, o controla como `enforced` controla.
`enforced` (que o attach define na primeira célula quando todo processo ativo tem `LOOM_CELL` definido,
e o prepare em uma célula posterior, cujos processos executam com ele desde o primeiro boot) faz um
processo de servidor ficar não pronto, não executar nenhum worker e não responder nada além de suas sondas de saúde
e da rota de sonda (para que uma nova execução do prepare consiga alcançá-lo), e faz o comando da CLI recusar; o
motivo de prontidão e a recusa citam `LOOM_CELL`. Assim, um pod ou uma CLI de operador que perde
`LOOM_CELL` em uma célula `enforced` não escreve para nenhuma org; um que também perde `LOOM_PG_DSN` ou
`LOOM_STORAGE_AUTHORITY` é recusado pela verificação de autoridade que o deployment já executa
(o registro `loom.storage_authority` do ClickHouse). Os comandos `loom cell` que se conectam aos
stores de uma célula (prepare, attach, place, restore, update) recusam sem `LOOM_CELL` e,
quando o diretório já tem uma linha para `LOOM_CELL`, recusam stores cujo id de deployment não seja
o registrado nessa linha. Um deployment iniciado com `LOOM_CELL` não definido é um deployment único
até ser preparado, e apontá-lo para o prefixo de bucket de outro deployment é a
má configuração que é para qualquer deployment. Na primeira célula, `loom cell prepare` executa
antes de `LOOM_CELL` ser distribuído; uma célula posterior executa com `LOOM_CELL` definido desde o primeiro
boot, então seus processos ficam controlados e se mantêm à parte dos workers (inclusive da varredura de blobs)
até o prepare gravar sua linha, e o prepare recusa uma célula posterior enquanto o heartbeat de qualquer processo ativo
não tiver `LOOM_CELL` ou nomear outra célula. O prepare faz três coisas, em ordem:

1. **Ele recusa uma célula cujos stores já carregam a identidade de outra célula**: um
   id de deployment que o diretório registra, ou estado que o diretório não posicionou ali.
   Isso é verificado pelo roteador, na própria transação do registro, a partir de contagens que o
   prepare envia: uma célula para a qual o diretório não tem linha não pode ter linha de tenancy, `first`, modo
   ou própria, e uma célula posterior não pode ter nenhum objeto sob seu prefixo além da sonda do prepare e nenhum
   processo ativo sem seu `LOOM_CELL`. Uma nova execução conta as linhas de tenancy e de estado e
   os objetos do prefixo como seus; os processos de uma célula posterior executam com seu `LOOM_CELL` em
   toda execução, o que o prepare verifica por conta própria.
2. **Ele registra a célula** no diretório como `attaching`, sob o lock do diretório:
   seu host, o id de deployment lido da célula, o SHA-256 da credencial com a qual
   ela chama o roteador, e o nome e o prefixo de seu bucket, verificados contra os de todas as outras
   células, preparadas ou anexadas. A credencial é `lcr_` e 128 caracteres hexadecimais minúsculos
   (512 bits), como `loom cell credential new` a gera offline, imprimindo a
   credencial e seu hash; o boot e `loom doctor` recusam um `LOOM_CELL_ROUTER_TOKEN` de
   qualquer outra forma. O prepare recebe o hash como argumento, 64 caracteres hexadecimais minúsculos,
   de modo que nenhuma CLI precisa do segredo; qualquer outro valor, inclusive a credencial ou seu
   segredo de 128 caracteres, é recusado, citando `loom cell credential new`, e não é armazenado em lugar nenhum.
   Nenhuma outra linha de célula pode conter o hash, nem como atual nem como próximo.
3. **Ele grava as linhas da célula**: o modo (`recording` na primeira célula, `enforced` em
   uma célula posterior), a linha `first` e `current`.

O prepare é retomável. Uma nova execução para a linha do diretório de mesmo nome, id de deployment e
host grava a que estiver faltando entre a linha `first` e a linha de modo, a partir do estado
do diretório, e `loom.cell_self` como acima (antes do attach a partir dos heartbeats quando
ela falta; depois que o diretório guarda a impressão digital, `current` e `next` a partir dele,
sobrescrevendo um valor presente que difira); antes do attach, uma linha própria presente permanece, e
um processo ativo que não executa nem seu `current` nem seu `next` recusa. O estado do diretório
é o valor de `first` que o índice único nomeia e o maior modo que ele registra para
a célula: `recording` para a primeira célula ou `enforced` para uma posterior no registro,
na mesma transação da linha, e depois cada modo que o attach eleva, registrado no
diretório antes de o attach defini-lo na linha de modo da célula (uma recusa informa a linha de modo
como o attach a lê de volta após qualquer retorno a `recording`, e nunca reduz o
registro do diretório). Uma nova execução recusa uma linha de modo acima do estado do diretório e a linha
`recording` de uma célula posterior, sendo nenhuma delas desta célula, e deixa a linha de uma primeira célula abaixo
dele para o attach concluir. Uma nova execução recusa um hash de credencial que não seja nem o `current`
nem o `next` da linha (a rotação cabe a `loom cell credential`), conta as linhas de tenancy
posicionadas ou registradas desde seu próprio registro como suas, e pula as verificações do registro
que já passou (o prefixo vazio e a sobreposição de prefixos), de modo que executa em uma célula
`serving` cujo prefixo contém dados.

Um processo com `LOOM_CELL` definido e sem linha de modo não fica pronto. Com ele definido, o Postgres de cada
célula contém `loom.tenancy` (org, since) e `loom.tenancy_mode` (`recording`,
`enforced`); um processo lê o modo por um cache de no máximo
`LOOM_TENANCY_MODE_CACHE_S` (padrão 5 s), mantém o último modo que leu quando não consegue
lê-lo, e continua servindo as orgs que possui, por no máximo
`LOOM_JWT_REVOCATION_MAX_STALE_SECONDS`, depois do que fica controlado até que uma leitura se conclua.
Todo processo de servidor sob a autoridade Postgres, com `LOOM_CELL` definido ou não, lê as
linhas logo após seus stores abrirem e migrarem, antes de seu primeiro heartbeat e de sua decisão
de lease; uma primeira leitura que falha faz o boot falhar (com migrações do Postgres pendentes ele inicia
controlado e não pronto até que uma leitura se conclua). Depois disso, um processo com `LOOM_CELL` definido mantém
o limite de obsolescência acima, e um com ele não definido mantém seu veredito através de leituras que falham
sem limite de obsolescência, como um deployment único atravessa uma indisponibilidade do Postgres (a primeira
exceção em Invariantes). O processo filho de workers de uma célula lê suas próprias linhas e fica controlado até que
uma leitura se conclua. Com `LOOM_CELL` não definido, linhas de modo e `first` ausentes e sem linha de tenancy
nem própria indicam um deployment nunca preparado, e uma leitura que falha nunca é tomada por uma linha
ausente; um processo que não lê nenhum estado (o filho de workers e uma CLI com
`LOOM_CELL` não definido, e todo processo sob a autoridade ClickHouse) não é controlado por
ele. Em `enforced`, uma célula serve exatamente as orgs para as quais tem uma linha; uma org que um processo
leu como não possuída, para um chamador agindo em outra org, é lida novamente após
`LOOM_TENANCY_MODE_CACHE_S`. Uma linha `recording` é gravada por uma instrução que a insere
somente enquanto o modo é `recording`, lendo a linha de modo `FOR SHARE`; ela executa em
`READ COMMITTED` em sua própria transação e confirma antes de a requisição prosseguir.
A gravação de `enforced` pelo attach e esses inserts nunca se cruzam: um insert já em execução quando o attach
grava o modo é posicionado pelo passo 4, um que começa depois lê `enforced`, um fluxo de
inserts não consegue segurar a gravação, uma requisição de org nova espera o attach por poucos segundos
no máximo, e um attach que trava segurando o lock da gravação o libera em segundos.
O attach toma `LOCK TABLE loom.tenancy_mode IN EXCLUSIVE MODE` (que espera os inserts em execução
e enfileira os posteriores atrás dele) em tentativas curtas, em uma transação com
timeouts do lado do servidor, depois grava o modo e confirma; após qualquer erro nessa
transação ele encerra o backend dessa sessão (`pg_terminate_backend`, o backend nomeado pelo
pid e pelo `backend_start` que o attach capturou ao conectar) e espera até que tenha
saído de `pg_stat_activity`, depois lê o modo de volta antes de tentar de novo ou informar; um modo
lido de volta como `enforced` é gravado novamente em uma transação nova, cujo commit espera
o standby, antes de o attach prosseguir, já que um commit encerrado enquanto ele esperava está
apenas no primário. O comando attach fixa esses timeouts, e seus testes cobrem cada
um desses casos. O cache de modo lê o modo com um `SELECT` simples, que não toma lock de linha
e nunca espera o attach. Um insert que encontra `enforced` (um processo cujo cache
ainda lê `recording`) não grava nada: a requisição segue para a chamada de posicionamento como em
`enforced`, e um comando da CLI recusa como em `enforced`, citando a org. Um processo cujo
cache ainda lê `enforced` depois que o passo 3 ou o passo 4 devolve a célula a `recording` posiciona
pelo gate do roteador até seu cache expirar.
`loom doctor` lista qualquer linha de tenancy que o diretório não posicionou.

- **Executa após a verificação, antes de qualquer coisa ser gravada para a org.** O gate de op
  (`gates.authorize`) verifica primeiro a org que o transporte resolve (`gates.resolve_org`, com
  seus padrões do chamador e orgs de plano), antes de `check_acted_for` e do
  provisionamento do namespace pessoal, tanto para HTTP quanto para MCP (via MCP, uma chamada para a org de outra
  célula responde `isError`, de modo que um lote mantém suas outras respostas, e uma chamada na
  própria org do token, como `gates.resolve_org` a resolve, é verificada antes de os padrões do chamador
  serem lidos, já que eles vivem nessa org). As rotas fora do registro
  verificam por `check_authorized`, `admit_org` ou pela própria verificação, e um middleware verifica
  a própria org do bearer verificado nas que agem nela (as rotas que o censo marca
  `token`; para um token de org inteira ou sem org nas rotas do chamador que recorrem a
  ela, a org de cadastro, verificada sem principal, de modo que é registrada ou recusada, nunca
  posicionada). Um principal que não pode agir na org (um token de outra org) é recusado
  primeiro, com o 403 do gate, de modo que não registra, não posiciona e não descobre nada: nem se
  a célula possui a org, nem onde ela está posicionada. Uma op cujo campo de org tem outro nome
  (`from_org`) chega à mesma verificação por `check_authorized`, e um teste de censo
  lista cada um desses campos. Uma recusa gravada antes da verificação (JSON malformado, um argumento
  desconhecido, uma falha de validação) não cita, em sua telemetria nem em seu rastro de código, nenhuma org que a célula
  não possua. Um teste unitário lista cada rota do app e falha a menos que ela esteja marcada como
  verificada quanto a tenancy ou sem org.
- **A CLI declara suas orgs.** Todo comando da CLI que se conecta passa por
  `run_connected`, `connect_checked` ou `connect_declared` (`server/cli/_ch.py`), que recebem
  as orgs que o comando grava, ou o motivo de ele não gravar para nenhuma. Um comando que declara uma org
  primeiro verifica seu deployment (`check_configured`), de modo que sob a autoridade Postgres ele precisa de
  `LOOM_PG_DSN`. Em uma célula, antes de verificar uma org declarada, eles leem as linhas da célula e
  recusam o comando a menos que a linha `first` nomeie sua célula e seus stores (`deployment_id`,
  `bucket`, `bucket_prefix`) coincidam com um valor da linha própria da célula: uma CLI não verifica nenhum token
  e sua impressão digital nunca chega ao roteador, de modo que ela pode executar onde os transportes recusam
  por outro campo da impressão digital. Com `LOOM_CELL` não definido, o comando é recusado em uma célula além de
  `recording` (acima). Uma org que a célula não possui é registrada na primeira célula em
  `recording`, como a org de uma requisição é (lendo o estado da célula com a credencial de roteador
  da célula, `LOOM_CELL_ROUTER_TOKEN`, na configuração da CLI da primeira célula enquanto seu modo
  é `recording`, e somente enquanto o diretório responde `attaching`), e recusada em
  `enforced`; nunca é posicionada. Um comando do índice de código declara a org que grava: seu
  `--org`, senão `LOOM_CODE_ORG`. Uma passagem por todo repositório configurado não declara nenhuma,
  já que o attach, ou o `loom cell update` que altera um, posiciona as orgs configuradas, e um
  comando somente leitura ou uma simulação (dry run) não declara nenhuma, já que uma leitura não registra nada. Um comando
  sem org diz o que faz aos stores: ele lê, grava (`loom ch-setup`, `secrets`, uma
  alteração de configuração, prepare e os demais; uma simulação lê) ou revoga (`loom revoke-token`, a
  revogação de um usuário SQL, `loom signing-key retire`, `loom bot disable`, `loom sso unlink`;
  um que pode devolver acesso é uma gravação), por subcomando onde uma conexão atende vários
  (`loom signing-key`: `list` lê, `retire` revoga, `rotate` e `record-root` gravam).
  Com `LOOM_CELL` definido, uma gravação ou revogação verifica seu ClickHouse contra seu Postgres
  (`check_configured`), lê a linha `first` em uma sessão limitada própria e recusa
  quando ela nomeia outra célula, o que uma CLI apontada para os stores de outra célula sempre encontra, ou
  quando a leitura falha; uma linha ausente, ou tabelas de estado ainda não migradas, passa, de modo que uma célula posterior
  executa esses comandos antes de ser preparada. Uma revogação executa durante uma indisponibilidade (a quarta
  exceção em Invariantes); uma gravação recusa durante uma; uma leitura não é controlada.
  `loom ch-setup`, que configura o ClickHouse em um cliente próprio, executa o mesmo gate
  antes de gravar, passando um ClickHouse ainda sem registro; `loom migrate` e
  `loom provider-lock clear` gravam pelo cliente do próprio executor de esquema e não são controlados.
  Um teste de censo falha em uma conexão que não declara nenhuma das duas coisas, em uma sem org que não
  diz de que tipo é, em uma conexão sob `server/cli/` fora de `_ch.py`, e em um
  comando `loom code` que resolve sua org sem se conectar para isso. Quem detém a
  credencial de roteador da célula pode posicionar orgs para a célula, dentro da taxa da célula e de
  `placements_max`, como a credencial de operador que `loom cell place` usa pode para qualquer célula
  dentro de seu `placements_max` (uma chamada de operador não tem limite de taxa nem conta para a
  taxa da célula), e um posicionamento é permanente, de modo que um nome de org que ele toma é perdido para todas
  as outras células: um poder sobre o diretório compartilhado que as credenciais de store da célula não têm. Por isso
  os pods a detêm, e uma CLI apenas para a única leitura que precisa dela, enquanto o modo da primeira célula
  é `recording`: um comando da CLI que declara uma org recusa executar com ela definida, citando-a,
  a menos que a célula seja a primeira e seu modo leia `recording`, e um comando sem org nunca
  a usa; uma credencial vazada é contida por `loom cell limit --closed`, que não precisa de nenhum
  dos stores da célula, e substituída por `loom cell credential` (abaixo).
  `loom cell place --org <org>` (declarado sem org: ele grava o posicionamento da org e a linha de
  tenancy, que é como uma org passa a ser possuída), com a credencial de operador do roteador,
  posiciona uma org para um comando que grava uma nova (um bot, uma importação), pela API de
  posicionamento do roteador e em sua ordem, como o posicionamento de orgs configuradas pelo attach: ele insere
  o posicionamento e lê de volta o vencedor, grava a linha de tenancy da célula somente se o vencedor
  for esta célula (caso contrário recusa e não grava nada), e informa sucesso somente depois que a
  linha é gravada, de modo que uma nova execução é idempotente. Para uma org que o diretório ainda não posicionou ele
  passa pelo gate da API de posicionamento (abaixo): recusado enquanto o diretório está indisponível, para uma
  célula que o gate não aceita, fechada, ou quando a impressão digital da configuração da própria CLI da célula
  (com o id de deployment lido dos stores aos quais ela se conecta)
  não coincide com nenhum dos dois valores da linha da célula, ou além de `placements_max`, e contado para ele.
- **Escritores mais antigos que a verificação são recusados pelos stores em que gravam.** Uma sessão que
  executou a verificação de tenancy se marca com `loom.tenancy_checked = 'on'`: toda sessão de
  um processo com `LOOM_CELL` definido a envia como parâmetro de inicialização (de modo que o `RESET ALL` do pool a mantém),
  e a sessão de migração a define depois de tomar o lock de migração,
  já que os passos de dados de uma migração apenas copiam ou reescrevem linhas já armazenadas. Em cada
  tabela ordinária da autoridade com uma coluna `org` (não a própria `loom.tenancy`, nem
  `loom.idempotency` e `loom.idempotency_fences`, indexadas pela org do token do chamador) um
  trigger de linha `AFTER INSERT` e `AFTER UPDATE`, habilitado como triggers de origem, ignora uma sessão marcada em seu `WHEN`
  (o de update também ignora um update que deixa a org da linha intacta, de modo que um
  `ON CONFLICT DO UPDATE` que atinge uma linha existente não é recusado) e
  chama uma função (migração 011). A função segue a regra de estado (acima): ela
  passa toda linha enquanto o estado é open ou uma primeira célula está em recording, e em todo
  outro estado recusa, SQLSTATE `LT001` (503 `TENANCY_REFUSED` para um chamador), uma linha de uma
  org sem linha de tenancy (nunca `''`, `'-'` ou `'*'`); ela lê o estado depois que a linha
  é gravada, sem lock de linha, de modo que nenhuma gravação fica na fila atrás do attach, e o passo 4 (abaixo) espera
  cada transação que poderia ter lido `recording`. Um boot com migrações ligadas
  cria os triggers, em toda tabela assim que encontra no catálogo, somente em uma célula
  totalmente preparada em `enforced` e, ali, volta para origem qualquer um que encontre fora dela;
  o DDL de cada tabela espera 100 ms por tentativa pelo seu lock, três tentativas com um segundo de intervalo (cerca de 2.3
  s para uma tabela ocupada, de modo que um boot que encontra toda tabela protegida ocupada espera esse tempo
  em cada uma em sequência, segurando o lock de migração em que os outros pods em boot esperam),
  e o boot segue servindo além de uma tabela cujo lock não consegue tomar, registrado em log com os detentores de seus locks, ou
  uma que o papel não pode alterar, registrada como tal (a terceira exceção em Invariantes). Uma
  primeira célula fica em `recording` até o attach definir `enforced` enquanto executa, de modo que nenhum boot pode
  tê-los criado: o attach os cria por conta própria com a mesma passagem
  (`cell_triggers.ensure`) depois de definir `enforced`, e a célula passa a `serving` somente
  quando `cell_triggers.missing` não nomeia nenhum defeito. Uma célula posterior é `enforced` desde o prepare
  e os recebe em seu primeiro boot com migração após o prepare, o que seu attach exige
  da mesma forma. `loom doctor` nomeia um desabilitado ou alterado em qualquer estado, um ausente em
  uma célula `enforced`, e uma tabela particionada com coluna `org` como não suportada, já que
  ela não recebe trigger. Triggers de origem não disparam na aplicação da replicação lógica nem sob
  `session_replication_role = replica`, que os scripts de uma célula não usam. O marcador não
  é uma fronteira de privilégio: um escritor que o define de propósito passa; os triggers param
  escritores que não o conhecem, uma CLI mais antiga, um pod rebaixado para antes da verificação, ou um script,
  de adicionar uma linha para uma org que a célula não possui ou mover uma linha para uma; um update
  que mantém a org de uma linha, e um delete, não são recusados. O custo dos triggers de org é uma
  consulta de configuração por linha marcada, e uma leitura do estado e uma consulta a `loom.tenancy` por
  linha não marcada; o gate sob o qual eles são lançados é um aumento de p99 de `memory.set` de no máximo 10 ms e uma
  taxa de linhas por segundo de uma passagem de entrega em `LOOM_PG_DELIVERY_BATCH_ROWS` no máximo 10% menor, em
  `enforced` com as orgs possuídas. Toda gravação do Loom em uma célula é marcada, de modo que paga apenas
  a consulta de configuração, medida nas instruções com os triggers ligados e desligados (nenhum custo
  distinguível de zero: seis rodadas variam de −0.36 a 0.49 µs por linha, [resultados de
  benchmark](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md)), o que prevê que o gate se sustenta com ampla margem. O
  gate em si é uma medição de ponta a ponta de `memory.set` e de passagem de entrega em uma célula, que
  precisa do attach e é verificada com ele; até lá os triggers, que uma célula posterior recebe no
  próximo boot com migração do prepare, são lançados com a medição no nível da instrução. `loom.tenancy`
  tem sua própria proteção, já que uma linha de tenancy torna uma org possuída: `tenancy_record`, um
  trigger de origem de linha `AFTER INSERT OR UPDATE OF org` com o mesmo `WHEN` do marcador,
  chama `loom.tenancy_refuse_unmarked()` (migração 012), que recusa, `LT001`, a linha de tenancy
  de uma sessão não marcada, ou um update que a move para outra org, em todo estado
  exceto o de uma primeira célula ainda em recording (`open` também, já que a própria linha tornaria o estado
  `partial`), de modo que um script não pode tornar possuída uma org que o diretório não posiciona. Boots
  e o attach o criam e reparam como fazem com os triggers de org. Com o attach, `loom doctor`
  também lista qualquer linha de tenancy que o diretório não posiciona, o que nomeia uma gravada antes da
  proteção ou por um escritor que definiu o marcador, e qualquer org em uma tabela do Postgres indexada por org
  sem linha de tenancy, o que nomeia linhas gravadas antes dos triggers de org, por um escritor que
  definiu o marcador, ou deixadas por uma linha de tenancy excluída. Antes do primeiro attach o
  operador rotaciona as credenciais de escrita do ClickHouse da célula (cria o novo usuário, distribui-o
  para as configurações das builds que verificam, remove o antigo), e o attach recusa enquanto
  qualquer usuário do ClickHouse com `INSERT` em `loom.*` (`system.grants`, com grants de banco de dados, globais e
  de papel seguidos por `system.role_grants`, e usuários de arquivos de configuração ou
  com `ALL` implícito contados) não for um que um processo ativo de uma build que verifica ou o usuário
  configurado da CLI informe, de modo que apenas os detentores da credencial rotacionada gravem no ClickHouse; uma
  CLI mais antiga que receba essa credencial ainda grava sem verificação, e com o attach `loom doctor`
  lista o que ela gravou: qualquer org encontrada em uma tabela do ClickHouse indexada por org sem linha de tenancy,
  a partir da mesma lista de esquemas do passo 2 do attach.
- **Uma org que a célula não possui é posicionada ali ou recusada.** Para um principal verificado
  com escopo na org (um token dessa org, ou um principal de um emissor ou bot cuja org
  é ela), a célula chama a API de posicionamento do roteador (`POST /cells/v1/placements` em
  `LOOM_CELL_ROUTER_URL`, respondida com a célula vencedora e seu id de deployment) com sua
  própria credencial (`LOOM_CELL_ROUTER_TOKEN`), e a célula que a chamada nomeia deve ser aquela
  cujo hash registrado a credencial coincide (um hash nomeia uma célula): o roteador posiciona
  uma org por vez (um lock com escopo de transação na org, que todo caminho que posiciona
  toma), responde uma org já posicionada com sua célula e, caso contrário, julga a
  célula solicitante em sua linha travada, insere o posicionamento e lê o vencedor de volta, o que a
  célula compara por seu nome e id de deployment registrado. Se o vencedor é esta célula, a
  célula grava a linha de tenancy e serve a requisição; o nome desta célula sob outro id de
  deployment que o do processo verificado responde 503 e não grava nada; se for o nome de outra
  célula, a requisição responde 409 `WRONG_CELL` e nada é gravado. Este é o
  único caminho pelo qual uma requisição traz uma org a uma célula, seja o que for que a criou: uma emissão (mint), um
  token de um gateway ou de um emissor federado, uma org `prefix*` de um emissor de usuário final, um bot. Um
  principal de org `*` ou de plataforma age apenas em orgs que a célula já possui e nunca
  posiciona uma (em uma primeira célula antes de `serving`, veja [as chaves do
  heartbeat](#the-heartbeats-keys)). Para uma org que o diretório ainda não posicionou, o roteador
  aceita uma chamada de posicionamento somente de uma célula `serving` (ou da primeira célula enquanto
  `attaching` depois que o diretório registra `enforced`, o que o passo 3 do attach grava antes de
  definir o modo da célula), e somente com a impressão digital de configuração do processo chamador,
  cujos campos devem coincidir com um valor da linha do diretório da célula; a linha de uma célula preparada não contém
  emissores nem padrões de org até o attach gravá-los. Uma chamada que falha, uma sem resposta dentro de
  `LOOM_CELL_ROUTER_TIMEOUT_S`, uma célula ainda não anexada ou fechada para novas orgs, ou uma
  impressão digital que não coincide com nenhum valor responde 503 e não grava nada; uma nova tentativa é
  segura, já que a leitura de volta a torna idempotente. Para uma org que o diretório já
  posiciona, a chamada de uma célula é verificada apenas quanto à sua credencial (contra os hashes das células
  que o roteador mantém em cache por `LOOM_ROUTER_DIRECTORY_CACHE_S`, padrão 5 s) e à taxa da célula,
  e um `loom cell place` apenas quanto à credencial de operador; ambos respondem o vencedor
  qualquer que seja o estado, a cota ou a impressão digital da célula, sem adicionar posicionamento (o vencedor
  é público pela consulta de qualquer forma); uma requisição recusada além da taxa de sua célula responde 503
  com `Retry-After` (no máximo um minuto), e sua nova tentativa é admitida pela réplica que
  a responde quando as chamadas da célula ali estiverem abaixo da taxa. Uma org que a célula já
  possui não precisa de chamada, de modo que servi-la nunca depende do roteador.
- **Os posicionamentos são limitados por célula.** Cada réplica do roteador limita a taxa das
  chamadas de posicionamento de cada célula por minuto (`LOOM_ROUTER_PLACEMENTS_PER_MINUTE`), o que limita seu
  ritmo a essa taxa vezes as réplicas, e a linha de diretório de uma célula pode carregar uma cota de posicionamentos
  (`placements_max`, não definida por padrão), que, uma vez definida, limita o total deles, contado
  e inserido sob um lock na linha de diretório da célula, de modo que chamadas concorrentes entre
  réplicas do roteador nunca a ultrapassam; além da taxa, toda chamada de posicionamento da célula responde
  503 com `Retry-After`; além da cota, uma chamada para uma org que o diretório ainda não posiciona
  responde 503 como para uma célula fechada (um `loom cell place` de operador é isento da
  taxa, como acima); e os posicionamentos de cada célula são informados em
  `/v1/health?include=capacity` e por `loom doctor`, que nomeia uma célula sem cota.
  Toda org que o attach da primeira célula posiciona (o snapshot do passo 2, suas orgs configuradas incluídas,
  e o do passo 4) é contada mas nunca recusada por cota ou célula fechada, já que a célula
  já a serve e recusá-la deixaria seus dados órfãos (uma além de `ORG_MAX_BYTES` o
  diretório recusa, abaixo); todo outro posicionamento de uma org que o diretório ainda não posiciona,
  incluídas as orgs configuradas de uma célula posterior no attach e uma alterada em `loom cell update`,
  passa pelo gate, célula fechada e `placements_max` incluídos, de modo que uma célula posterior
  fechada ou cheia antes do attach recusa o attach, citando a org.
- **A primeira célula registra até `enforced`, como um deployment único.** Em
  `recording`, que dura até o passo 3 gravar `enforced` no modo da célula (e de novo
  sempre que o passo 3 ou o passo 4 a devolve a esse modo), uma requisição verificada para uma org nova
  grava uma linha de tenancy sem chamada de posicionamento, de modo que a taxa do roteador, `placements_max`
  e `loom cell limit --closed` não se aplicam: novas orgs chegam como em um deployment
  único, limitadas por suas próprias configurações de cadastro, emissor e JIT, e o passo 2 ou o passo 4
  as posiciona sem recusa. `loom doctor` lista as orgs registradas que não contêm dados, para que o
  operador veja o que o attach vai posicionar.
- **As orgs configuradas são posicionadas no attach.** O `oidc_jit_org` da célula, o
  `self_signup_org` de uma célula com cadastro ligado, e suas orgs de código (`LOOM_CODE_ORG` e a
  org de cada entrada de `LOOM_CODE_REPOS_JSON` que a configuração de código mantém) e a org do tracker são
  posicionadas como suas quando ela é anexada: as da primeira célula no snapshot do passo 2, as de uma célula
  posterior antes de ela ser marcada `serving`. JIT e cadastro próprio passam pela verificação de tenancy como
  qualquer requisição, e nada recusa no boot: na `recording` da primeira célula eles
  registram sua org enquanto o diretório responde `attaching`, e nos demais casos uma org configurada que a célula
  não possui responde 503 até ser posicionada (422 para uma além de
  `ORG_MAX_BYTES`, que o diretório recusa), o que `loom doctor` nomeia; todo attach,
  primeiro ou posterior, novo ou retomado, executa as recusas de sobreposição e posiciona as orgs
  configuradas dos dois valores de `loom.cell_self`, de modo que uma que um update colocou em `next` antes do attach é
  posicionada com as demais; depois que o attach grava a linha de diretório da célula, `loom cell update`
  posiciona uma alterada na transação do diretório que grava `next`, pelo
  gate da API de posicionamento para uma org que o diretório ainda não posiciona (células fechadas e
  `placements_max` incluídos), recusando, citando-a, quando outra célula a possui, e então
  grava sua linha de tenancy na célula; para uma célula posterior ainda `attaching` depois de sua transação
  de diretório, `loom cell update` recusa uma alteração de org configurada até a célula estar
  `serving`. Em `enforced`, um worker grava para uma org somente depois que a célula a possui (a
  verificação de tenancy sem principal), de modo que uma célula posterior não indexa, importa nem varre nada
  sob suas orgs configuradas antes de o attach posicioná-las; em `recording`, os workers executam
  como antes, e o snapshot do passo 2 e a segunda leitura do passo 4 encontram toda org sob a qual eles gravam.
- **As linhas nunca são excluídas.** Uma linha de tenancy e um posicionamento nomeiam uma org, não sua memória:
  ambos são classificados para apagamento como não contendo conteúdo de memória, como a reivindicação de um namespace,
  e permanecem após qualquer apagamento, de modo que um nome de org nunca fica livre para outra célula. Um
  processo mantém as orgs que viu como possuídas, e uma org mantida nunca é errada de servir.
- **Um limite de nome de org.** Uma célula não registra nem posiciona nenhuma org nova com mais de
  `ORG_MAX_BYTES` (2048 bytes UTF-8, `server/core/cell.py`, abaixo do limite de entrada de índice do Postgres): a
  verificação de tenancy de uma célula em recording recusa uma com 422 e não grava nada,
  e o diretório recusa uma com 400 (`ORG_TOO_LONG`) antes de gravar um posicionamento, para
  toda rota que posiciona, o que o posicionamento de uma célula transforma no mesmo 422 (via MCP, um
  erro de ferramenta contado como do chamador). Assim, toda org que uma célula registra pode ser posicionada, e uma
  org possuída ou posicionada antes do limite mantém sua célula.
- **Uma org posicionada sem linha** (uma restauração para antes de seu posicionamento) tem sua linha de volta
  por `loom cell restore`, antes de a célula restaurada iniciar ([O que cada célula
  executa](#what-each-cell-runs)).

<a id="attaching-a-cell"></a>

## Anexando uma célula

`loom cell attach`, executado contra o router, registra uma célula, um attach por vez, e recusa em qualquer falha, nomeando-a:

- a célula responde em seu próprio host com o deployment id que seu `probe_url` informa, todo processo ativo tem `LOOM_CELL` definido com o nome dela, ela executa a autoridade Postgres com um standby síncrono (`LOOM_PG_DURABILITY=synchronous`) e autenticação imposta, e registra um deployment id que nenhuma outra célula registra;
- na primeira célula, o papel Postgres do attach consegue ler o estado de transação de todos os backends (`pg_read_all_stats`) e o Postgres da célula não permite transações preparadas (`max_prepared_transactions = 0`), de modo que o passo 4 vê todas as transações abertas;
- suas chaves são só dela: nenhum emissor que ela aceita para bearer tokens é aceito por outra célula, e a impressão digital do segredo raiz (`keys_fp`) não coincide com a de nenhuma outra célula. Provedores de identidade upstream e emissores de workload podem ser compartilhados, pois a célula emite seu próprio token a partir deles; um gateway (`LOOM_IDENTITY_PROVIDER=gateway`) emite para uma célula, então um deployment executa um gateway por célula;
- nenhum processo ativo de qualquer outra célula informa uma impressão digital que não coincide com nenhum dos dois valores da linha do seu diretório, e todo processo ativo da célula que está sendo anexada coincide com o `current` ou o `next` de seu `loom.cell_self` (o attach nomeia qualquer um que não coincida);
- nenhuma outra célula registra o mesmo par (emissor OIDC, client id) (a linha do diretório os registra, e `loom cell update` os conta em suas recusas por sobreposição);
- com SCIM ativado, todo token SCIM que ela possui a nomeia (`lsc_<cell>_…`) ou, apenas na primeira célula, não nomeia nenhuma célula; uma célula posterior não possui linha de usuário SCIM nem tombstone (suas pessoas são provisionadas após o attach, na ordem `LOOM_CELL`, um token nomeado e depois a atribuição da aplicação);
- seu `LOOM_PUBLIC_BASE_URL` está definido (um processo com `LOOM_CELL` definido se recusa a iniciar sem ele, de modo que nenhuma URL que ele entrega venha do Host de uma requisição), e os hosts dele e de `LOOM_OAUTH_ISSUER` (um vazio resolve para a URL base pública) são iguais ao seu host registrado: para a primeira célula, cujo host registrado é o host compartilhado, são o host compartilhado ou um alias registrado dele; o host de nenhuma célula posterior é o host compartilhado ou um alias, o que a tabela `hosts` do diretório impõe (host como chave primária, normalizado por uma única função, `normalise_host`: minúsculas, IDNA para ASCII, sem ponto final nem porta padrão, colchetes mantidos em IPv6, usada por `hosts`, pela regra 1, pelo attach e pela verificação de prontidão; pertencente a uma célula, com um alias pertencendo à primeira célula), escrita na mesma transação de cada verificação, e a partir da qual a regra 1 roteia; `LOOM_JWT_ISSUER` é uma string opaca, que só precisa ser única; o prepare verifica o mesmo;
- suas orgs configuradas não colidem com nenhuma org colocada em outra célula, e nenhum padrão de org em que ela confia (os `orgs` de seus emissores de usuário final) se sobrepõe aos padrões ou às orgs configuradas de outra célula, nem o padrão de outra célula corresponde às suas orgs configuradas, pela regra de sobreposição que um deployment único aplica aos seus próprios emissores de usuário final;
- seus stores não são os de outra célula: uma chave de sonda escrita em um nome reservado de seu KeeperMap `loom._migration_lock` não é visível a nenhuma outra célula e a sonda de toda outra célula é invisível a ela (um ClickHouse sem Keeper recusa); seu prefixo de bucket não é prefixo de caminho do de outra célula em um bucket de mesmo nome, em qualquer endpoint, nem tem um, e objetos de sonda não são listados em nenhuma das direções;
- todo processo ativo de toda célula executa um build cujo predecessor também executa a verificação de tenancy (abaixo), e um heartbeat que informa seu modo de tenancy, a impressão digital e o `LOOM_CELL` com o qual executa;
- com SCIM ativado, nenhuma conta ativa tem um e-mail de contato ou verificado sem uma linha SCIM ativa (contas com senha, sessões de refresh ativas e contas de serviço pessoais incluídas), e nenhuma conta de serviço ativa tem uma cadeia de criadores que termina em uma conta sem e-mail (tokens de agent-signup feitos antes do SSO): o attach as lista e recusa, ou, com `--revoke-unprovisioned`, encerra seus tokens emitidos antes do attach sob o próprio prefixo de revogação do SCIM, de modo que uma pessoa que o provedor de identidade provisionar depois seja admitida, já que ele nunca desprovisionará uma pessoa que não conta como atribuída.

O primeiro attach transforma o deployment existente na primeira célula, em ordem:

1. verificar se o modo é `recording`, como o prepare definiu: toda requisição verificada escreveu a linha de tenancy de sua org, se ausente, enquanto o diretório respondia que a célula está `attaching`;
2. em uma transação do diretório, escrever `attach_started_at`, os emissores, padrões e impressão digital da célula (ambos os valores de `loom.cell_self`) e as colocações, depois as linhas de tenancy, de toda org que a célula mantém, a partir de um snapshot, direto no diretório (nenhuma outra célula existe ainda): suas orgs configuradas (de ambos os valores), o `org` distinto de toda tabela dos dois schemas (gerado a partir do schema, como a classificação de tabelas do erasure) e as orgs de objetos de bucket catalogados; nunca `''`, `'-'` ou `'*'`;
3. definir `enforced` como acima, dentro de `--wait`, que a espera abaixo então compartilha, sem iniciar nenhuma tentativa que possa confirmar o modo com menos tempo restante que o mínimo dessa espera (a vida útil do cache de modo mais o maior prazo de requisição): o attach então recusa, informando o modo que lê de volta (`recording`, e uma nova execução retoma do passo 1), ou o modo como desconhecido quando não consegue lê-lo, e uma nova execução então retoma pelo modo que ler; depois esperar, dentro do que resta de `--wait` (padrão 10 minutos, depois recusar e definir `recording` de volta), até que todo processo ativo informe `enforced` e então a vida útil do cache de modo mais o maior prazo de requisição tenham passado; depois executar a precondição SCIM novamente (`--revoke-unprovisioned` de novo, se informado), para contas feitas antes de `enforced` entrar em vigor; se ela recusar, a célula permanece `enforced` e `attaching`, e executar o attach novamente retoma a partir da espera, como faz um attach retomado (abaixo);
4. esperar, dentro do que resta de `--wait`, até que nenhuma transação que possa ter lido `recording` ainda esteja aberta, julgado a partir de um snapshot do Postgres que o attach toma depois de ter lido `enforced` de volta nesta execução (não de um relógio): conta como aberta uma transação no banco da célula que começou antes dele, de qualquer cliente, worker em segundo plano ou conexão de replicação lógica, inclusive as somente leitura, uma transação de escrita em outro banco do mesmo servidor que começou antes dele (ela segura o horizonte de todo snapshot, e é nomeada como tal), uma transação preparada (que recusa de imediato), ou uma cujo estado o attach não consegue ler; não contam os processos de manutenção do próprio Postgres, as conexões de replicação física (as de todo standby e de um base backup, que não executam SQL) e as conexões do próprio attach. Passado `--wait`, o attach recusa, nomeando o que esperou, e deixa a célula `enforced`, de modo que uma nova execução retoma da espera do passo 3. O comando attach fixa a consulta exata, e seus testes cobrem cada tipo de transação. Quando a espera termina, colocar as linhas de tenancy escritas desde o snapshot e toda org que as fontes do passo 2 fornecem quando lidas novamente (o `org` distinto de toda tabela dos dois schemas e as orgs de objetos de bucket catalogados; uma linha ou um objeto que um escritor não verificado, como uma CLI mais antiga ou um worker, escreveu em `recording` depois do snapshot também é colocado), verificar de novo que todo processo ativo informa `enforced` (recusando e voltando a `recording` como o passo 3 faz, se algum não informar) e marcar a célula como `serving`. O diretório registra o modo mais alto que o attach elevou ou tentou elevar, que `loom cell restore` só escreve de volta para cima: uma célula cujo passo 3 foi recusado no lock é restaurada como `enforced`, e seu attach retomado então executa a partir da espera do passo 3.

Uma célula posterior só é anexada quando a primeira está `serving`. Ela não deve conter dados de org (o attach recusa caso contrário) e começa em `enforced`. Seu attach executa, em ordem: as recusas; uma transação do diretório, sob o lock do diretório e com as recusas por sobreposição executadas novamente dentro dela, que escreve `attach_started_at`, as colocações de suas orgs configuradas e seus emissores, padrões e impressão digital (ambos os valores de `loom.cell_self`), recusando e não escrevendo nada quando uma leitura de volta nomeia outra célula (ela nomeia a org); as linhas de tenancy de suas orgs configuradas e de seu `loom.cell_self`, na célula; a precondição SCIM (`--revoke-unprovisioned`, se informado); e `serving`. Um attach posterior retomado executa novamente as recusas, exceto as de uma célula nova, e continua a partir do primeiro destes passos não concluído; as colocações são idempotentes por meio da leitura de volta. Suas pessoas são provisionadas pela aplicação do próprio provedor de identidade antes de entrarem. Os serviços próprios de uma org são anexados como uma célula à parte. Uma nova org de pessoas começa com `loom cell place --org`, depois a atribuição de suas pessoas às aplicações dessa célula; uma célula com SCIM ativado não admite ninguém até que seu primeiro ciclo de provisionamento tenha sido executado. Uma célula que se aproxima dos limites de seus stores é fechada para novas orgs: suas chamadas de placement respondem 503, de modo que as novas orgs de seus emissores esperam por capacidade em vez de se espalharem para outro lugar.

Um primeiro attach retomado, para uma célula cuja linha do diretório tem `attach_started_at`, executa novamente toda recusa da lista acima que se aplica à primeira célula. Em seguida continua pelo modo da célula: em `recording`, a partir do passo 1; em `enforced`, a partir da espera do passo 3 (todo processo ativo informa `enforced`, depois passam a vida útil do cache e o maior prazo de requisição) e então sua precondição SCIM, de modo que a verificação SCIM que encerra um attach interrompido após definir `enforced`, seja qual for o motivo, sempre execute depois que `enforced` tenha entrado em vigor.

<a id="what-each-cell-runs"></a>

## O que cada cell executa

Tudo o que um deployment executa, sem alterações: seus pools, migrations, workers e seus
leases, health e readiness (a readiness de um pod é a da sua cell), erasure e seu census,
audit checkpoints, backups e restores. O restore de uma cell é o de um único deployment
([backup e recuperação](https://github.com/ClickHouse/loom/blob/main/docs/operations/backup-and-recovery.md)), com os mesmos limites:
o que o backup contém (suas chaves, revogações, sessões, bots e linhas de SCIM) volta
como estava. Uma cell acrescenta um passo depois do restore do store e antes de qualquer outro passo que
essa página execute: `loom cell restore`, com a cell parada, executado onde o operador executa
`loom cell place` e attach, com a credencial de operador do router, que nenhuma configuração de cell contém (um comando de CLI declarado sem org: ele grava as linhas da própria cell),
grava de volta, a partir do directory, as linhas que um restore pode recuperar, de modo que as
erasures e redactions do `loom reconcile` passem na verificação de tenancy, e um restore de ambos os stores,
que o `loom reconcile` recusa, ainda recupera as linhas da sua cell. Ele recusa enquanto o
directory estiver indisponível, e grava:

- uma linha de tenancy para cada org que o directory coloca nela;
- a linha `first` e a linha de mode, o que estiver ausente, a partir do directory, como faz um
  prepare retomado; uma linha `first` presente que difere da do directory é recusada, e
  uma que coincide é mantida;
- depois que as linhas de tenancy voltam, o mode registrado no directory sobre a linha de mode, somente
  para cima (`recording` para `enforced`), nunca para baixo;
- depois que a linha do directory contém o fingerprint da cell (o attach o grava), o
  `loom.cell_self` dela a partir dele (`current` e `next`); antes do attach o directory não contém
  nenhum, e o `loom.cell_self` restaurado permanece como o backup o contém (um `current` ausente
  é gravado por uma nova execução de `loom cell prepare` assim que a cell inicia, um `next` perdido
  por outro `loom cell update --config`).

Uma primeira cell restaurada para antes do seu prepare, e ainda não anexada, não serve nada no
host compartilhado em seus processos com `LOOM_CELL` definido (um com ele indefinido serve como um deployment único, como antes do rollout) até essa nova execução de `loom cell prepare`, que é o
passo logo após ela iniciar.

A readiness de uma cell nunca espera pelo directory, então uma indisponibilidade dele não trava o rollout de nenhuma cell. Um processo que lê `recording` registra uma nova org somente enquanto o
directory responde que sua cell está `attaching` ([as chaves do heartbeat](#the-heartbeats-keys)); enquanto o
directory está ilegível ou marca a cell como `serving`, uma nova org responde 503 e as orgs que a
cell contém continuam sendo servidas. Um restore para antes do passo 3 do primeiro attach cujo
`loom cell restore` foi pulado, portanto, não serve nenhuma org que não contenha; o `loom doctor`
aponta tal cell, e o `loom cell restore` eleva o seu mode.

**O directory é um store de alta disponibilidade.** Seu Postgres roda com um standby síncrono
e point-in-time recovery até o seu último commit, como o de uma cell, de modo que um failover
ou uma corrupção não perde nada que tenha sido confirmado. Perdê-lo por completo é uma indisponibilidade que falha de forma fechada:
toda cell continua servindo as orgs que contém, o roteamento continua a partir do snapshot assinado de cada réplica do router
com o seu último cache (gravado em armazenamento compartilhado a cada mudança do directory), e os placements, as consultas de orgs fora do cache, os comandos `loom cell`
que leem ou gravam no directory e as leituras de estado das cells em recording respondem 503 ou
recusam até o directory voltar, enquanto o `loom doctor` informa suas verificações do directory como
não executadas e, com `LOOM_CELL` definido, sai com código diferente de zero; reconstruir um directory perdido
a partir das cells é um design à parte.

Cada cell mantém, em seu próprio Postgres, uma cópia do fingerprint de configuração da linha do seu directory
(`loom.cell_self`), com o qual seus processos comparam a sua configuração e que
o attach e o `loom cell update` gravam ([A linha própria da cell](#the-cells-own-row)).
O `loom cell restore` regrava o `loom.cell_self` a partir do directory; os stores restaurados
carregam o deployment id da cell, então um restore para um novo endpoint precisa apenas do novo
endereço na configuração da cell. Fechar uma cell ou definir seu `placements_max` é
um único comando,
`loom cell limit --cell <cell> [--closed | --open] [--placements-max (<n> | none)]`,
que grava somente no directory, com a credencial de operador do router e sem nenhum dos
stores da cell, de modo que funciona enquanto a cell está fora do ar; o router avalia um placement a partir
da linha do directory.

<a id="operations"></a>

## Operações

- `loom cell restore`, após qualquer restauração de store e antes de `loom reconcile`, grava
  de volta as linhas da célula a partir do diretório ([O que cada célula executa](#what-each-cell-runs)).
- `loom cell which --org <org>` indica a célula de uma org (apagamento, restauração e suporte começam
  por aí); para uma org sem placement, indica a primeira célula, e a consulta não
  distingue os dois casos.
- `loom cell limit --cell <cell> --closed` fecha uma célula para novas orgs, `--open` a reabre
  e `--placements-max <n>` define sua cota de placements (`--placements-max none`
  remove a cota).
- `loom cell remove --cell <cell>` remove a linha do diretório de uma célula que está `attaching`
  sem nenhum placement (`--cell` é o `LOOM_CELL` da célula, já sem valor definido nesse ponto). Para a primeira célula,
  lê apenas o Postgres da célula, em uma sessão a partir de `LOOM_PG_DSN`, sem ClickHouse,
  e recusa a operação a menos que leia `open` e registre o deployment id da linha, o que impõe a
  ordem de reversão: remover `LOOM_CELL` de todos os processos (cada um passa a servir como em `recording`), depois
  a limpeza completa do estado e, por fim, a remoção
  ([solução de problemas](https://github.com/ClickHouse/loom/blob/main/docs/operations/troubleshooting.md#a-process-with-loom_cell-unset-is-gated)).
  Remover a linha primeiro faz toda nova org receber 503; limpar o estado primeiro bloqueia todos
  os processos que têm `LOOM_CELL` definido. Uma primeira célula com placement só avança, nunca volta. Os
  stores de uma célula removida mantêm as linhas de estado que contêm, então a limpeza completa do estado roda
  antes de eles serem preparados novamente.
- A credencial de operador do router é `lco_` seguida de 128 caracteres hexadecimais minúsculos (um router sem
  `LOOM_ROUTER_OPERATOR_SHA256` responde 503 à API de operador com
  `NO_OPERATOR_CREDENTIAL`), que `loom cell credential new --operator` gera; o
  router guarda apenas o SHA-256. As recusas da API de operador não têm limite de taxa: um
  limite por endereço permitiria que um estranho atrás de um ingress compartilhado bloqueasse todos os
  operadores, e 512 bits não são adivinhados online. Um comando de operador só conhece seu resultado
  quando a conexão nunca foi aberta ou o próprio router respondeu, exceto
  `BACKEND_UNAVAILABLE` (um COMMIT pode ser concluído após seu limite); após um timeout, uma
  conexão interrompida ou a página de um gateway, ele informa que a alteração pode ter sido feita e o que uma nova tentativa
  mostra. A credencial de operador é rotacionada definindo o novo hash em
  `LOOM_ROUTER_OPERATOR_SHA256` e reiniciando o router; enquanto suas réplicas são atualizadas, algumas
  ainda recusam a nova credencial com 401, e o operador executa o comando novamente
  até que todas as réplicas a aceitem. Os comandos chamam o router em `--router`, ou em
  `LOOM_CELL_ROUTER_URL`, na raiz (um caminho ali é recusado, pois o router
  encaminha um para uma célula), e enviam a credencial de operador somente por https (http em
  loopback), de modo que um endereço de router `http` na rede interna atende a uma célula, mas não esses
  comandos.
- `loom cell credential new` gera uma credencial de router offline e a imprime com seu
  SHA-256; `loom cell credential --cell <cell> --next <sha256>` registra o SHA-256 de uma nova credencial de router
  como o hash `next` da célula; como o prepare, recebe o hash como
  argumento, na mesma forma. Usa a credencial de operador do router, grava apenas o
  diretório e mantém o lock do diretório; é recusado quando qualquer célula registra esse hash
  como current ou next, e enquanto houver um `next` pendente. O router aceita ambos nas
  chamadas de placement e nas leituras de estado, cada réplica dentro de `LOOM_ROUTER_DIRECTORY_CACHE_S`
  de `--next`, de modo que a nova credencial só vai para os pods depois disso;
  `loom router migrate` roda antes de um router que lê o hash `next`, e uma rotação
  começa quando todas as réplicas do router o fazem. Um router revertido para um build que lê apenas o
  hash current recusa um pod com o próximo: faça o rollback sem `next` pendente
  (`--revert` antes) e não entre `--finish` e a saída da credencial antiga dos
  pods. Quando a nova credencial estiver nos pods da célula e, enquanto o modo da primeira célula
  for `recording`, na configuração da CLI, `loom cell credential --cell <cell> --finish`
  torna `next` o current e descarta o antigo; ele não verifica o que não consegue ver (o router
  não sabe qual credencial um pod possui), e uma célula finalizada cedo demais falha
  de forma fechada: suas chamadas de placement e leituras de estado respondem 401, no mais tardar quando o
  cache do router com os hashes da célula expira (`LOOM_ROUTER_DIRECTORY_CACHE_S`), até que
  seus pods tenham a nova credencial, ou que o hash antigo seja registrado novamente com `--next` e
  finalizado. `loom cell credential --cell <cell> --revert` descarta um `next` pendente, de modo que um
  valor errado ou vazado nunca precise ser finalizado; `--finish` e `--revert` são recusados sem
  `next` pendente.
- As configurações de uma nova célula nomeiam `LOOM_CELL`, seu host público (`LOOM_PUBLIC_BASE_URL`,
  `LOOM_OAUTH_ISSUER`, `LOOM_JWT_ISSUER`), suas chaves, seu prefixo de bucket, suas orgs
  configuradas, seu cliente OIDC do provedor de identidade e o token SCIM, e sua credencial de router
  (`LOOM_CELL_ROUTER_URL`, `LOOM_CELL_ROUTER_TOKEN`, que seus pods possuem, e
  `LOOM_CELL_ROUTER_TIMEOUT_S`).
- Cada célula informa gravações por segundo, linhas por namespace e bytes por chave em
  `/v1/health?include=capacity`, contra cerca de 13.6 KB por chave no ClickHouse e 6.8 KB no
  Postgres ([Memória em escala](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#memory-at-scale)).
- **Uma org permanece em sua célula.** Mover uma org entre células é um projeto à parte: exige
  que as gravações da org sejam bloqueadas em todos os processos da célula, uma cópia de cada store que
  não conte nada duas vezes e uma remoção verificada.
- **Os dados de uma célula permanecem em seus stores.** Movê-los para outros stores (outro deployment
  id, nome de bucket ou prefixo), ou adicionar um bucket a uma célula preparada sem um, é um
  projeto à parte: processos antigos e novos gravariam em stores diferentes durante qualquer
  rollout, então exige que as gravações da célula sejam bloqueadas, como em uma movimentação de org.
- **Uma célula permanece anexada.** Aposentar uma célula é um projeto à parte: exige prova,
  vinda dos próprios stores, de que toda cópia dos dados de suas orgs foi apagada, antes que qualquer coisa
  possa afirmar que foi. Até lá, uma célula que está fora do ar continua sendo uma célula: suas orgs respondem
  503, e seus nomes, issuers, hosts e placements permanecem reservados.

<a id="failure-modes"></a>

## Modos de falha

| Falha                                                                    | O que acontece                                                                                                                                                                                                                                                                                                                                                                                                                       |
| ------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| O diretório está fora do ar                                              | as requisições por host, por emissor e para a primeira célula são roteadas; novas orgs e consultas sem cache respondem 503                                                                                                                                                                                                                                                                                                           |
| A chamada de posicionamento do roteador falha                            | a requisição responde 503 e nada é gravado; as orgs que a célula mantém continuam sendo atendidas                                                                                                                                                                                                                                                                                                                                    |
| O emissor de uma célula posiciona orgs em massa                          | a partir de `enforced`, suas chamadas de posicionamento respondem 503 com `Retry-After` acima da sua taxa, e suas chamadas para novas orgs respondem 503 acima da sua cota, se houver uma definida; as outras células posicionam como antes                                                                                                                                                                                          |
| Um token SCIM de outra célula, ou uma aplicação no host da célula errada | o processo não inicia, ou `/scim/v2` responde 401 e não grava nada                                                                                                                                                                                                                                                                                                                                                                   |
| O Postgres ou o ClickHouse de uma célula                                 | essa célula falha como uma implantação única ([autoridade de armazenamento](storage-authority.md#failure)); as outras células continuam atendendo                                                                                                                                                                                                                                                                                    |
| As linhas próprias de uma célula ou o modo de tenancy ficam ilegíveis    | com `LOOM_CELL` definido, um processo mantém as últimas linhas e o último modo dentro do limite de obsolescência e depois fica bloqueado; um processo de servidor cuja primeira leitura falha não inicia (bloqueado e não pronto com migrações do Postgres pendentes), e um processo filho de workers que nunca as leu fica bloqueado; com ele indefinido, um processo de servidor mantém seu veredito sem limite, e uma primeira leitura com falha faz o boot falhar (bloqueado até que uma leitura funcione, com migrações do Postgres pendentes) |
| Uma linha `first` ou de modo removida, ou ambas ao lado de uma linha de tenancy ou própria | os gatilhos de org, onde a célula os tem, agem como em `enforced` e todos os processos ficam bloqueados; `loom doctor` aponta o problema, e uma nova execução de prepare grava as linhas de volta                                                                                                                                                                                                                                    |
| Nenhuma linha de estado, de tenancy ou própria em uma célula que o diretório registra | aberto; `loom doctor` aponta o problema; `loom cell restore` o fecha após uma restauração, e uma nova execução de prepare o fecha após um prepare interrompido                                                                                                                                                                                                                                                                       |
| O valor `first` de uma célula ainda não foi lido                         | toda requisição, exceto as sondas de saúde e a rota de sonda, responde 503 (SCIM com `Retry-After`); o processo fica não pronto e não executa nenhum worker (um processo filho de workers, ou um processo de servidor com migrações do Postgres pendentes; qualquer outro processo de servidor falha no boot)                                                                                                                        |
| Uma requisição para a org de outra célula                                | 409 `WRONG_CELL` após a verificação, nada é gravado                                                                                                                                                                                                                                                                                                                                                                                  |
| Uma chamada de plataforma, de org `*` ou sem principal para uma org que não é mantida | em uma primeira célula, 503 enquanto o diretório responde `attaching` ou não pode ser lido antes de uma resposta `serving`; 409 `WRONG_CELL` depois de `serving` e em uma célula posterior                                                                                                                                                                                                                                             |
| Um pooler que remove o marcador de tenancy das sessões de uma célula     | a verificação de prontidão `cell_marker` falha em `enforced` e `partial` e emite aviso em `recording`; o heartbeat informa `marked` false e o attach recusa o processo                                                                                                                                                                                                                                                               |
| Uma célula restaurada para um momento anterior                           | `loom cell restore` grava de volta as linhas das orgs posicionadas antes de iniciar                                                                                                                                                                                                                                                                                                                                                  |
| Uma primeira célula restaurada para antes do seu prepare, ainda não anexada | em processos com `LOOM_CELL` definido, toda requisição, exceto as sondas de saúde e a rota de sonda, responde 503 até que o prepare seja executado de novo                                                                                                                                                                                                                                                                           |
| O diretório perdido                                                      | cada célula atende as orgs que mantém; posicionamentos e consultas sem cache respondem 503 e os comandos do diretório são recusados, até que ele volte                                                                                                                                                                                                                                                                               |
| Uma célula em `recording`, com o diretório ilegível                      | uma nova org responde 503; as orgs que ela mantém continuam sendo atendidas                                                                                                                                                                                                                                                                                                                                                          |
| Um processo travado durante o primeiro attach                            | o attach é recusado após `--wait` e a célula volta para `recording`                                                                                                                                                                                                                                                                                                                                                                  |
| Um insert de recording mantido aberto durante o primeiro attach          | o attach para de tentar quando resta pouco de `--wait` para a espera, informando o modo que lê de volta; uma nova execução o retoma                                                                                                                                                                                                                                                                                                  |
| O attach trava enquanto mantém o lock da tabela de modo                  | o Postgres encerra sua sessão em segundos e libera o lock; novas orgs respondem dentro do seu prazo                                                                                                                                                                                                                                                                                                                                  |
| Uma transação aberta desde antes do snapshot do passo 4                  | o attach é recusado quando `--wait` se esgota (uma transação preparada, de imediato), nomeando a transação que esperou, e deixa a célula em `enforced`                                                                                                                                                                                                                                                                               |
| Um comando da CLI com `LOOM_CELL` indefinido, iniciado antes do passo 3 do attach | ele leu o estado enquanto a primeira célula estava em `recording` e continua gravando no ClickHouse após `serving`; o runbook define `LOOM_CELL` em toda CLI antes do attach                                                                                                                                                                                                                                                          |
| Uma CLI de um build anterior à leitura do estado                         | ela não é recusada em uma célula `enforced`; toda imagem de CLI e de CI executa um build com a leitura antes do attach ([atualizações](https://github.com/ClickHouse/loom/blob/main/docs/operations/upgrades.md#upgrade-procedure))                                                                                                                                                                                                  |

<a id="rolling-upgrades"></a>

## Atualizações graduais

A verificação de tenancy, a chamada de placement e a leitura do estado da célula, as declarações de org da CLI, `LOOM_CELL`, `loom cell prepare`, `scim.refuses_subject`, o e-mail que um grant armazena, `loom.cell_self`, o gate de requisição e a verificação de prontidão da fingerprint de um processo contra a própria linha, a recusa do token SCIM de outra célula, o requisito de SCIM e o modo, a fingerprint e o `LOOM_CELL` do heartbeat são entregues em uma única release. O gate em um processo com `LOOM_CELL` não definido, o marcador de sessão e os triggers de org são entregues juntos em uma única release; o prepare de uma primeira célula admite um processo da release anterior, que atende enquanto a célula está em `recording`, e o `loom cell attach`, que é entregue em uma release posterior e lê a própria chave de heartbeat, recusa a operação até que todos os processos ativos executem essa release, de modo que o único rollback suportado a partir de uma célula anexada cai em um build que envia o marcador. A migração dos triggers de org é aditiva, e os triggers não fazem nada em um deployment que nunca foi preparado (sem linhas de estado, a regra da linha `first`). Com `LOOM_CELL` não definido, um processo lê apenas as linhas de modo e `first` e se existe uma linha de tenancy ou própria, e somente sob a autoridade Postgres, onde um deployment nunca preparado não tem nenhuma (as tabelas são criadas vazias), então os builds N-1 e N atendem lado a lado sem mudanças. O roteador roteia por host, issuer e pela primeira célula, nunca pelas ops de uma célula, então a release de uma célula não espera pela do roteador, exceto pela que o próximo parágrafo cita.

As chaves ao lado da fingerprint de um heartbeat ([as chaves do heartbeat](#the-heartbeats-keys)) são as chaves de heartbeat próprias da release do attach: um build anterior a elas lê apenas a versão e os campos de uma fingerprint, então aceita o heartbeat de um build mais novo como está, e o attach recusa um processo que não informa nenhuma. Essa release espera pelas dos roteadores: toda réplica do roteador a executa antes de qualquer processo de qualquer célula, já que uma primeira célula preparada consulta a rota de estado desde a primeira leitura de estado da célula até ler `serving`, e somente um roteador dessa release a responde a partir do seu cache ([atualizações](https://github.com/ClickHouse/loom/blob/main/docs/operations/upgrades.md#upgrade-procedure)).

<a id="invariants"></a>

## Invariantes

- Uma org é atendida por exatamente uma célula, a que o seu placement indica, e o seu placement nunca é excluído nem alterado.
- Um processo com `LOOM_CELL` definido não responde a nada além de seus health probes e da rota de probe, e não executa nenhum worker, a menos que sua configuração corresponda a um valor da linha da própria célula, que as recusas de sobreposição do diretório já verificaram; um processo com `LOOM_CELL` não definido em uma célula cujas linhas de estado contam como `enforced` ou estão em um estado `partial` (a regra da linha `first`) não responde a nada além de seus health probes e da rota de probe e não executa nenhum worker.
- Nenhum emissor de bearer token, host, cliente OIDC, padrão de org ou prefixo de bucket é aceito por duas células, e nenhum id de deployment ou credencial de roteador é mantido por duas.
- Todo caminho de segurança falha de forma fechada, com quatro exceções. Primeira: um processo de servidor com `LOOM_CELL` não definido mantém seu veredito diante de leituras com falha das linhas de estado, sem limite de obsolescência: aberto em `open` e `recording`, como é a indisponibilidade parcial de um deployment único, e bloqueado em todos os outros estados; uma primeira leitura com falha faz o boot falhar ou, com migrações do Postgres pendentes, o deixa bloqueado até que uma leitura seja concluída. Segunda: uma célula registrada sem linha de estado, de tenancy ou própria (seu Postgres restaurado para antes do seu prepare, ou um prepare interrompido antes da gravação da sua linha), que fica aberta até `loom cell restore` (após uma restauração) ou uma nova execução do prepare (após um prepare interrompido) e é apontada por `loom doctor` enquanto isso: uma linha, um modo ou um valor de `first` não lido causa recusa; com o diretório inacessível, placements, consultas fora do cache, os comandos `loom cell` que o leem ou gravam e as leituras de estado das células de registro são recusados, enquanto cada célula continua atendendo as orgs que mantém. Terceira: um boot com migração que não consegue criar ou habilitar um trigger de org ou o guard `loom.tenancy` em uma tabela, ou restaurar os triggers de `loom.cell_first` para always (seu lock não obtido em três tentativas de 100 ms, ou uma tabela que o role não pode alterar), registra o problema em log e passa a atender, deixando essa tabela sem proteção até que um boot posterior tenha êxito: os triggers são apenas uma salvaguarda contra escritores que não enviam marcador, recusar o boot derrubaria uma célula por causa de uma salvaguarda, e o attach recusa um trigger ausente. Quarta: um comando de revogação sem org que não pode devolver acesso (um `loom revoke-token` simples, não um que restringe ou expira, a revogação de um usuário SQL, `loom signing-key retire`, `loom bot disable`, `loom sso unlink`) com `LOOM_CELL` definido, cuja leitura da linha `first` da célula falha porque o Postgres não responde, é executado e informa que a célula não foi verificada, já que uma revogação precisa funcionar durante uma indisponibilidade; qualquer outra leitura com falha, incluindo a falta de uma permissão, é recusada.

<a id="source-entry-points"></a>

## Pontos de entrada do código-fonte

- `server/core/cell_probe.py`: a rota de sonda e o listener de sonda, que todo processo web
  aceita e que o processo filho dos workers nunca abre;
- `server/core/cell.py`: o estado próprio da célula (`loom.cell_first`, `loom.tenancy_mode`,
  `loom.cell_self`), a regra de estado do deployment (`state`, sobre as linhas que `refresh`
  lê, incluindo se `loom.tenancy` tem alguma linha entre elas), sua impressão digital e
  `gate_reason`, o gate nos processos com `LOOM_CELL` definido ou não definido, que o
  middleware de requisição (`server/transport/model_maintenance.py`), `verify_token`, a
  prontidão, o lease dos workers e a leitura do heartbeat usam; `start_cell_gate`, em
  `server/boot.py`, é a leitura de boot de um processo do servidor;
- `server/core/cell_triggers.py`: o marcador dos triggers de org, `ensure` (a criação e o
  reparo, no boot que executa migrações, dos triggers e da proteção de `loom.tenancy`) e
  `missing` (o que `loom doctor` aponta), com suas funções em
  `schemas/pg_migrations/011_tenancy_triggers.sql` e `012_tenancy_guard.sql`;
  `server/cli/doctor_cell.py`: as verificações de célula do `loom doctor`;
- `server/cli/cell_scan.py`: a varredura de orgs do primeiro attach, uma biblioteca que
  nenhum comando chama: as tabelas dos dois stores com uma coluna `org`, avaliadas por
  engine e de acordo com a classificação do apagamento, com todos os hosts de um cluster
  listando o mesmo, e as orgs distintas de cada tabela fora de `LOG_TABLES`;
  `server/cli/cell_writers.py`: a verificação de escritores do ClickHouse (os usuários,
  diretórios de usuários, objetos e réplicas fora das listas de permissão do operador);
  `server/cli/cell_ch.py`: o que os dois compartilham (os hosts do cluster, as leituras
  por host);
- `schemas/pg_migrations/008_cells.sql`, `schemas/pg_migrations/009_cell_probe.sql` e
  `schemas/migrations/094_cell_heartbeats.sql`: as tabelas da célula;
- `server/auth/tenancy.py` (`ensure_held`): a verificação de tenancy e a chamada de
  posicionamento, executadas pelo gate de ops (`server/transport/gates.py`), por
  `check_authorized`, por `admit_org` e pelas rotas que nomeiam uma org;
  `server/transport/tenancy_gate.py` verifica a org do próprio token nas rotas fora do
  registro que agem nela, e `tests/unit/test_cell_tenancy.py` contém o censo de todas as
  rotas;
- `server/router/app.py` (`loom router serve`): a API de posicionamento, a leitura do
  estado da primeira célula (que `server/core/cell_router.py` faz em uma primeira
  célula), o roteamento pelas regras 1 a 4 (`server/router/routing.py`, que decide, e o
  encaminhamento), a consulta de org com seu cache de orgs posicionadas (que
  `loom login --org`, `loom.credentials.org_server`, consulta), seus limites de taxa
  (`server/router/limits.py`) e as verificações por que um posicionamento passa; sua API
  de operador, com as rotas de attach da primeira célula, que `loom cell limit`,
  `loom cell credential` e `loom cell remove` (`server/cli/cell.py`, com
  `loom cell which`) chamam com a credencial de operador do router
  (`LOOM_ROUTER_OPERATOR_TOKEN`; o router guarda apenas `LOOM_ROUTER_OPERATOR_SHA256`);
  `server/router/directory.py` e `schemas/router_migrations/`: o diretório, no banco
  Postgres próprio do router (`LOOM_ROUTER_PG_DSN`, `loom router migrate`).

O restante do código de célula é adicionado pelos pull requests que implementam esta
página. Ele se baseia em:

- `server/auth/jwt.py` (`verify_token`, `AuthUnavailable`): onde o gate de impressão
  digital roda primeiro;
- `server/auth/scim.py` (`tokens`) e `server/auth/scim_routes.py`
  (`scim_settings_problem`, `_authorized`): o parser de tokens SCIM e suas verificações;
- `server/core/instances.py`: o heartbeat que informa o modo, a impressão digital,
  `LOOM_CELL` e as chaves ao lado da impressão digital;
- `server/workers/leader.py`: o lease do qual um processo com gate se afasta;
- `server/core/pg.py` (`same_deployment`): o id do deployment que os dois stores
  carregam;
- `server/transport/gates.py` (`resolve_org`, `authorize`): onde a verificação de tenancy
  roda;
- `server/cli/_ch.py` (`run_connected`, `connect_checked`, `connect_declared`): as
  declarações de org da CLI, que `tests/unit/test_cell_cli_orgs.py` exige de toda
  conexão;
- `server/auth/authz.py` (`check_acted_for`, `_check_creator`) e
  `server/auth/oauth.py` (`_refresh_session`): as verificações SCIM ao agir e ao renovar;
- `server/identity/app/scim_groups.py` (`_caller`): a segunda rota SCIM.

<a id="rejected"></a>

## Rejeitado

- **Um plano de controle compartilhado pelas células** (um único conjunto de chaves, concessões e workers, com os dados divididos por org): uma verificação de segurança, um lease de worker, uma exclusão, uma geração de política e a cadeia de auditoria passariam a abranger vários armazenamentos, e cada divisão exigiria um protocolo próprio.
- **Alocar uma org na primeira requisição, antes da verificação**: qualquer pessoa poderia escolher a célula de uma org, e um token emitido em uma célula poderia endereçar uma org alocada em outra.
- **Uma lista dos caminhos que criam uma org, cada um alocando-a**: as orgs passam a existir por mais caminhos do que uma lista consegue acompanhar (tokens de gateway e federados, bots, importações); a verificação é o único ponto por onde toda requisição passa.
- **Redirecionar um fluxo de uma célula para outra no meio do caminho**: o cliente, os códigos e os refresh tokens de um fluxo vivem na célula que o iniciou.
- **O roteador mantendo os usuários do provedor de identidade para todas as células**: o alcance de cada e-mail em cada célula, uma versão que sobreviva à perda do diretório, os envios e as buscas seriam, cada um, um protocolo próprio, enquanto o provedor de identidade já desprovisiona todos os aplicativos aos quais uma pessoa está atribuída.
- **Cada réplica acessando os armazenamentos de todas as células**: um pool por réplica por célula, o custo que [Um banco de dados por tenant](storage-authority.md#a-database-per-tenant) rejeita.
- **Rotear por namespace dentro de uma org**: uma operação que abrange a org inteira (sua exclusão, ids resolvidos entre namespaces, compartilhamento) passaria a abranger várias células; uma org grande recebe uma célula própria.

<a id="tests"></a>

## Testes

Um teste de integração com duas células e um roteador no job de Postgres do CI:

- uma requisição verificada de uma nova org em cada célula a posiciona uma única vez sob requisições concorrentes, e a outra célula responde `WRONG_CELL`; uma org emitida pelo gateway, uma org `prefix*` de um emissor de usuário final e a org de um bot são posicionadas da mesma forma após `enforced`, e a requisição de um principal da plataforma para uma org não mantida não posiciona nada;
- toda rota, incluindo as entradas em lote do MCP e todo comando da CLI, recusa a org de outra célula e não grava nada no Postgres nem no ClickHouse dela (os censos de rotas, de campos de op e da CLI cobrem o restante);
- o primeiro attach posiciona toda org que tenha uma linha em qualquer tabela; uma org vista pela primeira vez durante o attach é posicionada no passo 4, assim como uma que uma inserção direta no Postgres gravou entre o snapshot do passo 2 e `enforced`, uma linha do ClickHouse que uma CLI mais antiga gravou nesse intervalo e um objeto do bucket catalogado nesse intervalo; um processo que passou de `LOOM_JWT_REVOCATION_MAX_STALE_SECONDS` com leituras falhas das próprias linhas ou do modo fica bloqueado e volta a servir quando uma leitura é bem-sucedida; um pod com `LOOM_CELL` não definido fica não pronto, nomeando a configuração, responde 503 a uma requisição com bearer, passa na sonda de saúde, responde à rota de sonda e não executa nenhum worker em uma célula anexada e em uma célula posterior entre prepare e attach, continua servindo em uma primeira célula em `recording` e mantém o último modo que leu durante uma leitura falha, `recording` incluído após o passo 3 ou o passo 4 devolver a célula a esse modo; um comando da CLI que declara uma org com ele não definido é recusado em uma célula `enforced` ou `partial`, um comando sem org (uma revogação de token) é executado, inclusive com o Postgres fora do ar em um deployment nunca preparado, e um comando de armazenamento de `loom cell` recusa; `loom cell limit --closed` tem sucesso com o Postgres da célula fora do ar; após `loom cell credential --cell <cell> --next`, ambas as credenciais posicionam e leem o estado, `--finish` recusa a credencial antiga, para uma célula finalizada antes de seus pods terem a nova o roteador recusa as chamadas dela com 401, a requisição dela responde 503 sem gravar nada, e ela se recupera registrando o hash antigo novamente, `--revert` descarta um `next` pendente, que passa a ser recusado, e a credencial antiga continua posicionando, e um segundo `--next` enquanto há um pendente e um hash que outra célula registra como current ou next são recusados; `loom cell limit` e `loom cell credential --next`, `--finish` e `--revert` recusam enquanto o diretório está indisponível; posicionamentos concorrentes em um abaixo de `placements_max` em duas réplicas do roteador gravam um; um comando da CLI que declara uma org com `LOOM_CELL_ROUTER_TOKEN` definido recusa, nomeando-o, em uma primeira célula assim que ela está `enforced` (`attaching` incluído) e em uma célula posterior, e em uma primeira célula em `recording` executa e registra a org; o attach com `placements_max` abaixo das orgs que a primeira célula mantém as posiciona todas, e o attach de uma célula posterior além da cota ou fechada recusa, nomeando a org; uma primeira célula com `--closed` e `placements_max` definidos antes do passo 2 faz o attach, e uma org que uma requisição registra entre o passo 2 e `enforced` é posicionada pelo passo 4 sem 503; após a gravação do modo da célula no passo 3, um processo cujo cache ainda lê `recording` envia a requisição de uma nova org à chamada de posicionamento, que uma célula fechada responde com 503; um comando da CLI cuja inserção encontra `enforced` recusa, nomeando a org, e não faz chamada de posicionamento; depois que a espera recusada do passo 3 ou a nova verificação do passo 4 devolve a célula a `recording`, uma nova org é registrada sem chamada de posicionamento, e uma em um processo cujo cache ainda lê `enforced` é posicionada pela barreira até o cache expirar; cada ordenação da gravação do modo pelo attach com uma inserção em registro vale como descrito acima (em execução, iniciando durante a gravação, um fluxo de inserções sobrepostas, cada uma mais curta que uma tentativa, com o attach definindo `enforced` em uma ou duas tentativas, mantida por várias tentativas, mantida até restar pouco de `--wait`, um attach travado, um commit interrompido pelo timeout, um commit cuja conexão é perdida enquanto espera o standby, relido somente depois que esse backend se foi e gravado novamente de forma durável se ler `enforced`); uma transação `REPEATABLE READ` cuja primeira instrução rodou antes de `enforced` ser confirmado, com o papel do attach ou um segundo papel, segura o passo 4 até terminar e a gravação dela de uma org não mantida é então posicionada, uma cuja primeira instrução rodou depois é recusada pelo trigger, uma transação que começa depois que o attach toma o lock mas antes de seu commit ficar visível segura o passo 4, uma transação aberta em `recording` que sobrevive ao `--wait` de uma execução segura o passo 4 da reexecução, após uma recusa e após `loom cell restore` elevar o modo, um worker de autovacuum em uma tabela do loom não segura o passo 4 e um background worker com uma transação aberta desde `recording` segura, uma transação preparada (após uma reinicialização com `max_prepared_transactions` aumentado durante a espera) faz o passo 4 recusar de imediato, nomeando-a, com o modo ainda `enforced` e uma reexecução recusada até ela ser resolvida e a configuração voltar a 0, uma transação de escrita aberta em um segundo banco do mesmo servidor segura o passo 4 e é nomeada, uma leitura longa no standby síncrono, ou em um segundo standby, com `hot_standby_feedback` ligado não o segura, nem um backup base em execução durante ele, uma escrita mantida aberta em uma conexão `replication=database` segura o passo 4, assim como uma transação `REPEATABLE READ` em uma, aberta em `recording` e sem gravar nada antes de `enforced`, um dump lógico longo (uma transação somente leitura `REPEATABLE READ`) no banco da célula além de `--wait` faz o passo 4 recusar, nomeando-o, com a célula deixada `enforced`, e o attach da primeira célula sem `pg_read_all_stats` ou com `max_prepared_transactions` acima de 0 recusa antes do passo 1 com o modo ainda `recording`, enquanto o attach de uma célula posterior não exige nenhum dos dois; um trigger de org ausente ou desabilitado é recusado pelo attach e nomeado por `loom doctor`, uma gravação para uma org não mantida em uma célula `enforced` cuja linha de modo foi apagada é recusada e o estado é nomeado por `loom doctor`; nessa célula um pod com `LOOM_CELL` não definido fica não pronto e não executa worker, e um comando da CLI que declara uma org com ele não definido é recusado, como um pod é; atualizar, apagar ou truncar a linha `first` em uma célula `enforced` é recusado, inclusive sob `session_replication_role = replica`, e um trigger da linha `first` ausente ou desabilitado é recusado pelo attach e nomeado por `loom doctor`; um processo em execução com `LOOM_CELL` definido em uma célula cuja linha de modo foi removida responde 503 a uma requisição com bearer e não executa nenhuma passagem de worker, e volta a servir depois que uma reexecução de `loom cell prepare` grava a linha de volta enquanto ele roda; uma célula que o diretório registra sem linha de estado, de tenancy ou própria é nomeada por `loom doctor`, seus processos com `LOOM_CELL` definido permanecem não prontos, um processo com ele não definido serve como um deployment único, e `loom cell restore` grava as duas linhas de volta, uma célula anexada voltando então a servir e uma não anexada após uma reexecução de prepare; uma célula com as duas linhas ausentes ao lado de uma linha de tenancy, e uma ao lado de apenas a linha própria, faz a função recusar uma org não mantida (uma inicialização não cria trigger ali), todos os processos bloqueados e `loom doctor` a nomeando, e uma reexecução de prepare grava as linhas de volta; uma restauração somente do Postgres de uma primeira célula anexada para antes do prepare é nomeada por `loom doctor` e fechada por `loom cell restore`; uma reexecução de prepare durante um `loom cell update` pendente em uma célula anexada, com processos nos dois valores e `loom.cell_self` perdido, grava os dois valores de volta e mantém os dois conjuntos de processos servindo, e uma antes do attach recusa as duas configurações; uma reexecução de prepare em uma célula anexada cujo `current` está ausente e cuja configuração dos pods difere da linha do diretório recusa, e uma antes do attach cujos heartbeats informam outro prefixo de bucket recusa; uma reexecução de prepare que recebe um hash de credencial que não é nem o `current` nem o `next` da linha é recusada; uma reexecução de prepare em uma célula posterior `serving` cujo prefixo contém dados grava de volta uma linha de modo removida; uma célula com linha de modo e sem linha `first` faz a função recusar uma org não mantida (uma inicialização não cria trigger ali), todos os processos bloqueados e `loom doctor` a nomeando, e uma reexecução de prepare grava a linha de volta; uma célula posterior cuja linha de modo é definida como `recording` não registra nada, seu trigger recusa uma org não mantida, um processo com `LOOM_CELL` definido ali serve como em `enforced`, um pod com `LOOM_CELL` não definido ali fica não pronto e não executa worker, `loom cell prepare` nomeia a linha, um comando da CLI que declara uma org com `LOOM_CELL_ROUTER_TOKEN` definido recusa, nomeando-o, e o roteador recusa a leitura de estado dela; um `memory.set` de um processo da build N-1 e a passagem de entrega em um deployment nunca preparado após a migração de N ter sucesso não gravam linha de tenancy, e na primeira célula em `recording` uma inicialização não cria trigger de org, e um criado à mão lê o estado e não consulta nada, uma gravação sob `session_replication_role = replica` para uma org não mantida é ignorada (triggers de origem), um `INSERT ... ON CONFLICT DO UPDATE` que insere uma linha para uma org não mantida é recusado e um que atinge uma linha existente dela não é, toda gravação de uma sessão marcada passa, e os triggers em uma célula `enforced` cobrem toda tabela que a erasure classifica com uma coluna `org`, exceto as três exclusões; e uma gravação indexada por org enquanto o attach segura o lock da tabela de modo retorna de imediato, com requisições de novas orgs respondendo dentro do prazo e leituras do cache de modo retornando de imediato o tempo todo; `loom cell restore` após uma recusa no lock do passo 3 deixa a célula `enforced`, e o attach retomado roda a partir da espera do passo 3; um `LOOM_CELL_ROUTER_TOKEN` que não tem a forma `lcr_` é recusado na inicialização e nomeado por `loom doctor`; `loom cell credential new` imprime uma credencial dessa forma e seu SHA-256, com o diretório e os armazenamentos inacessíveis; prepare e `--next` recusam um argumento que não seja 64 caracteres hexadecimais minúsculos, a credencial e seu segredo puro de 128 caracteres incluídos, sem armazenar nada; orgs que uma primeira célula registra em `recording` sem dados são listadas por `loom doctor`; uma enxurrada de posicionamentos sem cota definida é contida pelo limite de taxa em cada réplica do roteador, `loom doctor` nomeia a célula como uma sem cota, e uma chamada recusada além dele em uma réplica responde 503 com um `Retry-After` de no máximo 60 s, após o qual sua nova tentativa nessa réplica, sem nenhuma outra chamada da célula admitida entre elas, é admitida; o primeiro prepare de uma célula, sem linha no diretório ainda, passa na verificação do deployment-id; um pod ou CLI que perdeu `LOOM_PG_DSN` ou `LOOM_STORAGE_AUTHORITY` em uma célula é recusado pela verificação de autoridade; os tokens, refresh tokens e clientes OAuth existentes da primeira célula continuam funcionando durante a transição, e um logout pelo host compartilhado revoga o refresh token dela;
- o attach recusa um emissor compartilhado, segredo raiz, caminho do Keeper, prefixo de bucket aninhado ou org configurada, uma segunda célula que contém dados e um segundo attach enquanto um está em execução;
- um login OIDC, a autorização de um cliente MCP e um device flow para uma org na segunda célula são concluídos no host dessa célula após uma consulta; `loom login --org` mantém o host dele em `NO_CELL` e em um deployment sem roteador, tenta de novo após o `Retry-After` de um 429 até `--wait` se esgotar e então recusa como recusa por taxa e, enquanto o diretório está fora do ar, inicia o fluxo no host da célula de uma org que o roteador tem em cache e recusa, sem iniciar nenhum fluxo, para uma que ele não tem;
- com SCIM ligado, uma pessoa que o provedor de identidade nunca atribuiu a uma célula não pode fazer login, atualizar a sessão, ser representada nem usar um token do gateway ali, uma desativação enviada ao aplicativo de uma célula encerra a pessoa ali, um bot de administração da plataforma ainda alcança uma célula cujo SCIM desativou todos, e o attach recusa uma célula com subjects ativos não provisionados ou que compartilha o cliente OIDC de outra;
- um attach interrompido no passo 3 depois de definir `enforced`, antes de a espera terminar, é retomado com a espera antes da precondição de SCIM, e um recusado pela precondição de SCIM também é retomado a partir da espera, com o modo ainda `enforced`; um attach posterior recusado antes de sua transação no diretório deixa `attach_started_at` vazio, de modo que uma reexecução verifica novamente as recusas de uma nova célula, e um primeiro attach retomado encontra suas orgs configuradas posicionadas nesta célula e prossegue;
- uma célula restaurada para antes do `enforced` do primeiro attach recebe seu modo de volta de `loom cell restore`; uma cuja execução de `loom cell restore` foi pulada responde 503 para uma org que ela não mantém, a org de outra célula incluída, não grava linha de tenancy para ela e é nomeada por `loom doctor`; uma indisponibilidade do diretório durante a implantação de `LOOM_CELL` e entre prepare e attach deixa a primeira célula servindo as orgs que mantém, uma nova org respondendo 503 em todo processo com `LOOM_CELL` definido; a leitura de estado é aceita de uma primeira célula que está `attaching` antes do passo 2 do attach, e somente com a credencial própria da célula nomeada, e uma resposta recusada ou tardia conta como ilegível;
- uma concessão renovada em N que foi gravada em N-1 é verificada contra a linha da pessoa, uma renovação enquanto o SCIM está ilegível responde 503 e mantém a sessão, e um processo com `LOOM_CELL` definido antes de `loom cell prepare` permanece não pronto;
- um token delegado durante o primeiro attach por uma pessoa nunca provisionada é recusado após `enforced`; um gateway agindo por um bot é atendido e por um handle simples é recusado;
- com o diretório indisponível, toda célula serve as orgs que mantém, os posicionamentos respondem 503, e `loom doctor` em uma célula informa suas verificações do diretório como não executadas e termina com código diferente de zero;
- `LOOM_CELL` é implantado após `loom cell prepare` com todos os pods prontos, antes do attach;
- preparar uma célula vazia antes do deployment do host compartilhado é recusado, e uma célula restaurada do snapshot de outra célula é recusada no prepare;
- um prepare interrompido entre a gravação no diretório e a gravação na célula, com `LOOM_CELL` já implantado, é reexecutado por `probe_url` contra processos bloqueados; uma reexecução de prepare antes do attach cujos heartbeats não correspondem nem ao `current` nem ao `next` atuais recusa; uma reexecução de prepare anexada em que só o `next` foi perdido o grava de volta, e uma cujo `current` atual difere do diretório o sobrescreve; uma reexecução de prepare isolada após uma restauração somente do Postgres de uma primeira célula anexada para antes do prepare deixa `loom doctor` nomeando as linhas de tenancy ausentes, e um pod com `LOOM_CELL` não definido serve toda org ali até a reexecução e fica bloqueado depois dela, enquanto com `LOOM_CELL` definido uma org recebe sua linha de tenancy de volta pela chamada de posicionamento; uma reexecução de prepare contra pods com `LOOM_CELL` não definido em estado `partial` alcança a rota de sonda deles e grava a linha ausente de volta; prepare recusa o `probe_url` de uma célula posterior que informa um id diferente do de seus armazenamentos, attach recusa uma célula cujo host informa um id diferente do de seu `probe_url`, e prepare recusa um argumento de endereço de sonda para a primeira célula, e o roteador sonda apenas `LOOM_ROUTER_UPSTREAM_PROBE_URL`; o primeiro prepare de uma célula posterior tem sucesso; `loom cell update --probe-url` recusa um endereço que informa outro deployment id ou falha no eco do nonce, e recusa a primeira célula; `loom cell update` move os endereços de sonda e de upstream da primeira célula somente quando toda réplica do roteador os executa, após a sonda deles e a verificação de id do upstream, e uma reexecução de prepare por uma réplica ainda nos valores antigos (ou nos novos, antes da mudança) é recusada, nomeando o endereço da linha; um `probe_url` com userinfo ou outro esquema é recusado; a porta principal responde apenas o id em memória, com qualquer Host, sem ler nenhum armazenamento; o roteador sonda um `probe_url` https cujo certificado é assinado por uma CA em seu `SSL_CERT_FILE`; o listener de sonda responde 429 além de sua taxa; e um prepare que recebe uma cópia dos armazenamentos da primeira célula é recusado pelo nonce;
- a primeira célula padrão (`LOOM_JWT_ISSUER=loom`, um `LOOM_OAUTH_ISSUER` vazio) faz o attach; uma célula reimplantada com o host compartilhado como URL pública fica não pronta;
- um posicionamento inserido na cota de uma célula cuja chamada expirou por timeout é repetido e responde seu vencedor, e a célula grava a linha de tenancy; o mesmo em uma célula fechada nesse meio-tempo, e para uma reexecução de `loom cell place`;
- um attach que morre entre sua gravação no diretório e sua gravação em `loom.cell_self` é retomado até `serving`, e seus emissores permanecem reservados nesse meio-tempo; um `loom cell place` para uma org que outra célula mantém não grava linha, e um para uma org que o diretório não posiciona é recusado, sem gravar linha, para uma célula fechada, além de `placements_max`, para uma célula posterior ainda `attaching`, para uma primeira célula antes de o diretório registrar `enforced`, com uma configuração cuja impressão digital não corresponde a nenhum dos valores da linha da célula, e enquanto o diretório está indisponível, e não é recusado quando a taxa da célula se esgota nem conta para ela (as chamadas de posicionamento da própria célula continuam admitidas após uma sequência de places do operador); um para uma org que o diretório posiciona nesta célula grava sua linha de tenancy em uma célula fechada ou cheia; `loom bot create` para uma nova org em uma primeira célula em `recording` registra a org enquanto o diretório responde `attaching` e é recusado enquanto ele responde `serving` ou não pode ser lido;
- um host de emissor OAuth que difere do host público da célula é recusado no prepare, e o host compartilhado roteia antes de qualquer célula ser anexada; um `loom cell place` sem outras gravações mantém a org em sua célula;
- uma primeira célula restaurada para entre os passos 1 e 3 do attach depois de estar `serving` fica pronta em `enforced` assim que `loom cell restore` é executado;
- uma célula restaurada para antes de um `loom cell update --finish` volta a servir assim que `loom cell restore` reescreve sua linha própria, uma célula cujos dois armazenamentos são restaurados recebe suas linhas de volta apenas de `loom cell restore`, e uma célula preparada e não anexada mantém sua linha própria restaurada;
- uma primeira célula restaurada para antes do prepare recebe suas linhas de volta, responde 503 no host compartilhado em processos com `LOOM_CELL` definido até `loom cell prepare` ser reexecutado, e então serve; o SCIM em um deployment com `LOOM_CELL` não definido funciona como antes em N, durante prepare, a implantação de `LOOM_CELL` e o attach;
- o SCIM da primeira célula continua respondendo seu token sem nome durante a transição, uma célula posterior recusa um token sem nome, nenhuma célula aceita o token de outra célula, `lsc_b_…` e um token `lsc_` malformado são recusados na primeira célula, e uma célula posterior com uma linha de SCIM gravada antes de `LOOM_CELL` é recusada no attach; um token da célula `a-b` é recusado na célula `a`, e um nome de célula com `_` é recusado;
- uma célula posterior com `oidc_jit_org` definido inicializa e reinicia seus pods entre prepare e attach, com o JIT para essa org respondendo 503 até o attach posicioná-la; o autocadastro da primeira célula em uma org que nenhuma requisição registrou, entre prepare e attach, a registra e conclui o cadastro; uma célula posterior com repositórios indexados não grava linha de código antes do attach e então faz o attach; uma primeira célula em `recording` mantém suas passagens de worker para uma org que nenhuma requisição tocou desde a implantação; o attach de uma célula posterior interrompido após `attach_started_at` é retomado com suas orgs configuradas posicionadas e mantidas; uma atualização entre prepare e attach que muda a org de código tem essa org posicionada pelo attach, e após `--finish` ela indexa; uma atualização após o attach recusa uma org configurada alterada que outra célula mantém, nomeando-a, e, para uma org que o diretório ainda não posiciona, é recusada para uma célula fechada e além de `placements_max`; uma atualização que muda uma org configurada de uma célula posterior ainda `attaching` após sua transação no diretório é recusada até a célula estar `serving`; `--revert` mantém uma org configurada que a atualização posicionou; as orgs de código e de tracker de uma célula posterior servem e indexam quando ela está `serving`, uma op `code.*` de um membro de outra org incluída, e o mesmo vale para uma org configurada que `loom cell update` altera; um attach posterior em corrida com a atualização de outra célula que amplia um emissor recusa um dos dois; uma org configurada que outra célula posiciona entre as recusas do attach e seu posicionamento faz o attach recusar, nomeando-a, e não grava nada; prepare recusa uma credencial de roteador que outra célula registra; uma chamada de posicionamento que nomeia outra célula com uma credencial válida desta não posiciona nada na outra;
- a chamada de posicionamento de uma célula preparada e não anexada para uma org que o diretório não posiciona responde 503, uma para uma org que ele posiciona responde o vencedor mesmo com a credencial antiga ainda em cache após `loom cell credential --finish` ou uma impressão digital que não corresponde a nenhum dos valores, um vencedor com o nome desta célula sob outro deployment id responde 503 e não grava linha de tenancy, e um processo cuja configuração difere da linha de sua célula não posiciona nada;
- uma célula anexada com o SCIM desligado e ligado por `loom cell update` recusa enquanto um token de agent-signup está ativo, inclusive um emitido enquanto a atualização é implantada, e um processo configurado com o emissor de outra célula responde 503 a toda requisição com bearer, alcançado pelo roteador ou diretamente;
- durante um `loom cell update`, os processos na configuração antiga e na nova servem, `--finish` libera um emissor reduzido somente depois que nenhum processo o aceita, uma atualização que morre após sua gravação no diretório mantém o emissor ampliado reservado, e as builds N e N+1 com uma configuração correspondem à mesma linha;
- rotacionar um token SCIM não altera nenhuma impressão digital; um processo se recusa a inicializar com o token SCIM de outra célula, um provedor de identidade apontado para o host da célula errada recebe 401 e não grava nada, e uma célula revertida para N ainda se bloqueia pela própria linha e recusa o token SCIM de outra célula; uma enxurrada de posicionamentos do emissor de uma célula é recusada além de sua cota;
- um gateway agindo pela conta de serviço pessoal de uma pessoa nunca provisionada é recusado, e uma célula que passa a aceitar o emissor de outra célula não recebe posicionamentos;
- um processo que não corresponde a nenhum dos valores de sua linha responde 503 ao login, à renovação, aos endpoints de token e device do OAuth, ao callback de login e ao cadastro, e sua rota de sonda ainda ecoa o nonce do prepare; `--revert` espera os processos que correspondem apenas a `next`; `update --config` recusa enquanto um `next` diferente está pendente, e uma atualização que morreu antes de sua gravação em `loom.cell_self` é retomada para a frente; prepare recusa enquanto um processo pré-N está ativo; uma mudança de configuração durante `attaching` passa por `loom cell update` e o attach retomado reserva os dois valores; um processo cujos armazenamentos configurados diferem da linha de sua célula responde 503 e se afasta dos workers, de modo que uma build mais nova bloqueada ao lado de uma mais antiga não bloqueada deixa os workers em execução e apagando; `loom cell update` recusa uma mudança no deployment id, no nome do bucket ou no prefixo, adicionar um bucket incluído; um processo bloqueado em modo `on` não executa nenhuma passagem de worker; preparar um clone com o nome de bucket e o prefixo da primeira célula é recusado, no endpoint dele ou em qualquer outro; uma célula posterior cujo prefixo contém objetos de outro deployment é recusada no prepare; dois prepares concorrentes de prefixos aninhados não passam ambos; duas células sem bucket fazem prepare ambas; prepare recusa uma célula posterior cujo processo carrega o `LOOM_CELL` de outra célula, e um processo cuja linha `first` nomeia outra célula responde 503 e não executa worker; uma enxurrada na rota de id da porta principal não trava o attach; e uma célula restaurada em um novo endpoint de Postgres, ClickHouse ou bucket serve assim que sua configuração o nomeia; uma requisição com bearer no host compartilhado é atendida antes do prepare, entre prepare e attach e durante o attach;
- o chart não renderiza listener de sonda por padrão, e com ele ligado renderiza o Service de sonda e não altera nenhuma NetworkPolicy, de modo que, com a política do chart ligada, nenhuma regra própria admite a porta de sonda; prepare nomeia `PROBE_UNREACHABLE` e `PROBE_BUSY` do roteador quando `--wait` se esgota; uma enxurrada na porta principal não afeta a taxa do listener de sonda;
- uma célula posterior iniciada a partir do ambiente da primeira célula, com seu bucket e prefixo, roda bloqueada desde a primeira inicialização e não apaga nenhum original; prepare recusa uma célula posterior enquanto um processo ativo não tem `LOOM_CELL`;
- `loom cell limit --closed` faz as chamadas de posicionamento da célula responderem 503 enquanto ela continua servindo as orgs que mantém, `--open` as restaura, e `--placements-max` entra em vigor na próxima chamada de posicionamento;
- uma restauração do Postgres de uma célula `enforced` para antes do posicionamento de uma org, com essa org apagada depois do ponto, reconcilia com código de saída 0 após `loom cell restore` e então serve a org;
- uma célula restaurada de antes do posicionamento de uma org a serve quando fica pronta, uma erasure deixa o posicionamento da org, e, com o diretório perdido, toda célula serve as orgs que mantém.
