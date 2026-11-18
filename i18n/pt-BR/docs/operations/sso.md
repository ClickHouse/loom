<!-- i18n: source=docs/operations/sso.md blob=43ac40cca373fa5c10d8d54eab80809979c4b157 model=claude-sonnet-5-5 -->
> Esta página é uma tradução automática da [página em inglês](https://github.com/ClickHouse/loom/blob/main/docs/operations/sso.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/operations/sso.md), which is authoritative.

<a id="single-sign-on-oidc"></a>

# Login único (OIDC)

O Loom pode delegar o login de usuários a um provedor de identidade OpenID Connect. Com
o recurso ativado, o painel exibe um único botão **Sign in with SSO**, `loom login` e
`loom.login()` aprovam o acesso por meio do provedor, os clientes MCP que se conectam com
OAuth também fazem login por ele, e o Loom recusa o login por senha e o autocadastro.
O Loom continua emitindo os próprios tokens; o provedor apenas atesta quem é o usuário.
Desativado por padrão: uma instalação nova usa login por senha.

<a id="how-sign-in-works-without-sso"></a>

## Como funciona o login sem SSO

Com a autenticação ativada (`LOOM_AUTH_REQUIRED=true`), `loom login` funciona sem nenhuma configuração adicional: ele exibe uma URL e um código, o usuário abre a URL em qualquer navegador, faz login com a senha do Loom e aprova. Nada aqui precisa ser configurado. Veja [tokens e permissões](https://github.com/ClickHouse/loom/blob/main/docs/guides/tokens-and-grants.md#sign-in-from-a-terminal-or-a-notebook).

<a id="okta-setup"></a>

## Configuração do Okta

1. No console de administração do Okta, vá em **Applications → Create App Integration →
   OIDC → Web Application**.
2. **Grant types:** Authorization Code e Refresh Token. Em comportamento do
   refresh token, escolha **Rotate token after every use**.
3. **Sign-in redirect URI:** `<public Loom URL>/oauth/callback`, exatamente
   assim (por exemplo, `https://loom.example.com/oauth/callback`). Não é necessário
   um redirect de sign-out.
4. **Assignments:** as pessoas ou os grupos que podem acessar o Loom.
5. Copie o client ID e o secret e, em seguida, defina:

```bash
LOOM_OAUTH_LOGIN_PROVIDER=oidc
LOOM_OAUTH_ISSUER=https://loom.example.com      # Loom's public URL, as in step 3
LOOM_OIDC_ISSUER=https://your-org.okta.com      # or https://your-org.okta.com/oauth2/default
LOOM_OIDC_CLIENT_ID=0oa…
LOOM_OIDC_CLIENT_SECRET=…
```

O Loom se recusa a iniciar se qualquer um destes estiver ausente. O servidor de
autorização da organização é suficiente: o Loom lê apenas as claims padrão do ID token.

<a id="scopes-and-claims"></a>

### Scopes e claims

O Loom solicita `openid email profile`, além de `offline_access` quando o
documento de discovery do provedor o lista (o do Okta lista). Ele precisa de:

- `email` com `email_verified: true` — a chave da conta. Um login sem e-mail
  verificado é recusado.
- Um refresh token do Okta (obtido com `offline_access`). Cada refresh do Loom, com no máximo
  uma hora de intervalo, o utiliza primeiro no Okta. Portanto, um usuário desativado no Okta, ou
  desconectado com **End all sessions**, perde o acesso ao Loom no próximo refresh.
  Um provedor que não emite refresh token também não gera refresh token do Loom:
  as sessões terminam quando o access token, de uma hora de duração, expira.

As claims de grupo não são mapeadas. O acesso aos namespaces vem das concessões do próprio Loom.

O Loom verifica a assinatura de um ID token com os algoritmos que o documento de discovery
lista em `id_token_signing_alg_values_supported`, entre RS256, RS384, RS512,
PS256, ES256, ES384 e ES512, e apenas com RS256 quando o documento não lista nenhum. Um token
assinado com um algoritmo que o documento não lista é recusado, assim como qualquer token
com `none` ou com um algoritmo HMAC (`HS256` e similares), independentemente do que o documento
lista; um provedor que lista somente esses algoritmos não consegue autenticar ninguém.

<a id="accounts"></a>

## Contas

O email verificado corresponde a uma conta SSO (uma sem senha) pelo seu email
de contato. Uma conta com senha só corresponde depois de vinculada ao
email: o autocadastro aceitava qualquer email sem verificá-lo, então uma
correspondência apenas por email entregaria a conta a quem digitasse o endereço
primeiro. Uma conta vinculada mantém seu subject, concessões, namespaces e dados; nenhuma nova é criada.

- **Logo depois de ativar o SSO**, vincule as contas das suas pessoas de uma só vez:
  `loom sso adopt --domain example.com` lista as contas com senha nesse
  domínio com seus donos, e `--apply` as vincula (ele se recusa a rodar enquanto o SSO
  estiver desativado: as senhas ainda permitiriam login, passando pela revogação descrita abaixo). Ele lê
  as configurações do local onde roda, então execute-o com as dos servidores (dentro do
  contêiner deles, ou com o mesmo ambiente), ou chame `/v1/admin/sso.adopt`,
  que um servidor em execução responde. Ele atesta que os
  endereços do domínio são pessoas do provedor, então leia a lista: um endereço
  que outra pessoa registrou também é vinculado, e um vínculo faz com que toda concessão escrita
  sob o endereço admita a conta dele. Ele ignora um email cujas contas estejam
  em várias orgs ou tenham vários donos, ou que o SSO já alcance. Ele
  revoga todos os tokens que os subjects adotados possuem, então as pessoas entram novamente, e
  isso inclui os tokens de conta de serviço e de MCP que elas criaram a partir de
  suas sessões: o CI que usa um deles deixa de funcionar até ganhar um bot próprio (veja
  abaixo). `--keep-tokens` os mantém, e com eles quaisquer tokens criados por um invasor.
  Um bot que se cadastrou com senha no domínio também está na lista:
  deixe-o de fora com `--skip <its email>` (repetível), para que ele mantenha os tokens
  com que roda até migrar para `loom bot`. Um `--skip` que não corresponda a nenhuma conta
  não vinculada recusa a execução inteira, então um erro de digitação nunca adota o bot.
- **Quem não foi adotado** é solicitado, no primeiro login SSO, a informar uma vez a
  senha da sua conta no Loom; depois disso ela é vinculada. Senhas erradas contam para os
  limites habituais de login.
- Uma conta registrada com outro endereço (um pessoal, um `+tag`) não está
  no domínio, ou não está sob o endereço que o provedor verifica: `loom sso
link --email <their provider address> --org <org> --namespace <ns>` mostra
  a conta, e `--apply` faz desse endereço o seu contato e a vincula, de modo que
  o próximo login SSO da pessoa caia nela, com seu subject, concessões, dados e tokens
  (nenhum é revogado). Exige o SSO ativado, como o adopt: com o autocadastro fechado,
  ninguém pode registrar o endereço liberado e herdar o que ele nomeava. Os vínculos do
  endereço antigo são removidos, e uma concessão já revogada sob ele é transferida
  para o subject da conta, revogada, de modo que continue limitando os tokens da pessoa. Ele
  se recusa a prosseguir se o endereço antigo ainda tiver concessões ativas próprias (quando o endereço antigo
  está vinculado, conceda-as ao subject da conta e revogue-as primeiro;
  quando nunca foi verificado, elas não admitem ninguém: conceda-as ao
  subject da conta se eram destinadas a ele, e revogue-as) ou uma solicitação pendente de
  entrada (rejeite-a); quando o endereço antigo é o email verificado de outra conta,
  suas concessões e solicitações pertencem a essa conta e o vínculo as deixa como estão, transferindo
  cada uma ao subject da conta revogada, como limitou os tokens da conta. Ele
  também se recusa a prosseguir se outra conta já usar o endereço. Quando for uma conta JIT
  criada por um login SSO anterior, execute `loom claim-release` nela primeiro: suas
  memórias ficam nela (transfira-as com `namespace.snapshot` e
  `namespace.restore`), e as concessões feitas a ela precisam ser feitas de novo.
- `loom sso unlink --email <email> --apply` desfaz um vínculo: as concessões armazenadas
  sob o endereço (escritas antes do vínculo) deixam de admitir a conta.
  As concessões feitas enquanto estava vinculada são armazenadas sob o handle de login e permanecem;
  revogue-as com `grants.revoke`. As sessões já abertas não são encerradas;
  revogue-as com `loom revoke-token --subject`.
- Quem afirma que uma conta com senha que cita seu endereço não é sua
  é negado quando essa conta está em `LOOM_OIDC_JIT_ORG`, e
  `auth.sso_link_declined` é auditado: ao lado dessa conta, um login com senha
  usando o endereço ainda a encontraria, e uma revogação do endereço
  atingiria ambas. Para liberar um endereço que outra pessoa registrou, libere essa conta
  (`loom claim-release`), o que limpa seu email e vínculos; com
  `LOOM_OIDC_JIT_PROVISION=true`, o próximo login da pessoa cria então a conta dela.
  Uma conta em outra org não a impede. Uma conta revogada
  que cita o endereço bloqueia novas contas, pois pode ser a mesma pessoa.

Para uma pessoa sem conta:

- `LOOM_OIDC_JIT_PROVISION=true` cria uma no primeiro login: um
  namespace pessoal em `LOOM_OIDC_JIT_ORG`, do qual a pessoa é admin;
  suas sessões têm o papel `writer`.
- `LOOM_OIDC_JIT_EMAIL_DOMAINS=example.com,example.org` limita isso a
  esses domínios. Qualquer outra pessoa vê "sign-in denied", e `loom login`
  termina com um erro.
- Com o JIT desativado, apenas contas existentes podem entrar.
- O namespace de uma conta JIT é `u-<name>-<8 hex>`, nomeado a partir do email.
  Com o JIT ativado, ninguém mais pode criar um. Um nome criado por alguém antes de o JIT
  ser ativado continua sendo dessa pessoa, e a outra pessoa recebe o próximo livre
  (`-2`, `-3`, depois um sufixo aleatório).

Os admins da plataforma são os emails verificados listados em `LOOM_PLATFORM_ADMINS`.
O provedor nunca pode conceder admin.

<a id="bots-and-ci"></a>

## Bots e CI

Um bot nunca faz login no provedor e não deve guardar nenhum segredo de longa duração.
Registre-o com os namespaces aos quais seus dados pertencem (normalmente um namespace
de equipe com mais de um administrador, registrado primeiro, em uma org); seus tokens são para
`sa:<name>`, duram uma hora, ou o máximo do bot quando for menor, a menos que a requisição
peça outra duração até esse máximo (abaixo), e suas escritas são marcadas com
`actor=process:sa:<name>`.

- **Uma chave.** O Loom guarda a metade pública; o bot assina uma declaração curta para
  cada token (credenciais de cliente OAuth com `private_key_jwt`).

  ```bash
  loom bot create --name triage-bot --org acme --namespace-role triage=writer \
    --generate-key ./triage-bot.pem
  # on the bot, with `pip install clickhouse-loom[machine]`:
  LOOM_CLIENT_ID=triage-bot LOOM_CLIENT_KEY_FILE=./triage-bot.pem python agent.py
  ```

  A chave vem de uma destas fontes: `LOOM_CLIENT_KEY_FILE` (um arquivo PEM), `LOOM_CLIENT_KEY`
  (o próprio PEM, como um gerenciador de segredos o injeta no ambiente) ou
  `LOOM_CLIENT_KEY_SECRET` (um id ou ARN de segredo do AWS Secrets Manager que contém o
  PEM; `pip install clickhouse-loom[aws]`). O SDK a lê de novo a cada
  token, então uma chave rotacionada no mesmo lugar é reconhecida. O SDK de TypeScript
  (`@clickhouse/loom`) usa as mesmas variáveis; qualquer outro cliente assina a
  declaração por conta própria (RFC 7523: `iss` e `sub` o nome do bot, `aud` o
  `token_endpoint` de `/.well-known/oauth-authorization-server`, um `exp` de dois minutos
  e um `jti` novo).

- **Um job de CI.** Nenhum segredo: o job entrega o token OIDC que sua plataforma de CI
  emite para aquela execução, e o Loom o aceita somente daquele emissor, para aquela
  audiência, com todo `--claim` correspondendo (um valor terminado em `*` corresponde a um
  prefixo). Qualquer job da plataforma pode obter um token para qualquer audiência, então indique
  o repositório e a branch nos claims.

  ```bash
  loom bot create --name deploy --org acme --namespace-role deploys=writer \
    --issuer https://token.actions.githubusercontent.com --audience loom \
    --claim repository=acme/app --claim ref=refs/heads/main
  # in the job (it needs permission to request its OIDC token):
  LOOM_CLIENT_ID=deploy LOOM_WORKLOAD_AUDIENCE=loom python deploy.py
  ```

  Uma plataforma que entrega o token ao job coloca-o em
  `LOOM_WORKLOAD_TOKEN` (ou em um arquivo indicado por `LOOM_WORKLOAD_TOKEN_FILE`). Todo
  cliente no job o troca quantas vezes precisar enquanto for válido, e
  o token do Loom nunca dura mais que ele.

- **Muitos agentes, uma confiança.** Um plano de controle que emite a cada agente seu próprio
  token OIDC registra uma única confiança para todos. `--agent-claim` indica o
  claim que diz para qual agente um token se destina (letras, dígitos e `-`). Cada agente
  recebe o subject `sa:<trust>:<agent>` e seu próprio namespace
  `<trust>-<agent>` na org da confiança, criado na primeira troca. Os
  `--namespace-role` são o que todo agente compartilha além do seu próprio.
  A confiança nunca alcança um namespace que ela não criou, e um claim nunca escolhe a org.
  Pessoas acessam a memória de um agente como a de qualquer namespace: um administrador concede acesso.

  ```bash
  loom bot create --name fleet --org acme --issuer https://issuer.example \
    --audience loom --claim tenant_id=acme-prod --agent-claim agent_id \
    --jwks-uri https://keys.issuer.example/jwks.json
  # on each agent: its token in a file, renewed by the control plane
  LOOM_CLIENT_ID=fleet LOOM_WORKLOAD_TOKEN_FILE=/run/agent/oidc-token python agent.py
  ```

  Um agente pode deixar outro da mesma confiança ler sua memória, em uma só direção. O plano
  de controle decide e chama como o agente que empresta (com o token desse agente):

  ```bash
  curl -s -X POST "$LOOM_BASE_URL/v1/grants.share_agent" -H "Authorization: Bearer $A_TOKEN" \
    -d '{"org":"acme","namespace":"fleet-a","grantee":"b","role":"viewer"}'
  ```

  O próximo token do agente `b` nomeia `fleet-a` como viewer (emitido em outro servidor,
  dentro de `LOOM_GRANT_CACHE_TTL_SECONDS`); `a` não recebe nada de `b`. `role` é
  `viewer` ou `writer`. Um agente compartilha somente seu próprio namespace, somente com
  agentes de sua confiança, e não enquanto o namespace estiver restrito (um compartilhamento feito
  antes permanece até ser revogado)
  ([`grants.share_agent`](https://github.com/ClickHouse/loom/blob/main/docs/reference/operations/grants.md#grantsshare_agent)).
  Um compartilhamento nunca reduz uma concessão que `b` já tem, nem sobrescreve uma
  feita por outra pessoa (409): para reduzir seu próprio compartilhamento, um agente o retira e compartilha de novo.
  [`grants.unshare_agent`](https://github.com/ClickHouse/loom/blob/main/docs/reference/operations/grants.md#grantsunshare_agent)
  com o mesmo corpo (sem `role`) retira o que o agente emprestou: `b` é
  recusado imediatamente nesse servidor, e dentro de `LOOM_GRANT_CACHE_TTL_SECONDS` nos
  demais. Uma concessão feita por um administrador permanece (409). Um administrador do namespace
  concede ou revoga `sa:fleet:b` com `grants.create` e `grants.revoke`; o token de um
  agente aceita qualquer concessão que seu subject tenha na org, no máximo como writer.
  Um token nomeia namespaces emprestados pelo nome enquanto todos os seus namespaces, os seus
  próprios e os da confiança incluídos, couberem em 4,096 bytes do token (quinze dos
  nomes mais longos, mais se forem mais curtos), o que o mantém abaixo do limite de 8 KB de
  cabeçalho de um proxy; um compartilhamento que ultrapassaria isso é recusado, e um que apenas
  eleva um papel que o token já nomeia não é (dois compartilhamentos simultâneos podem ultrapassá-lo,
  e o token então nomeia os que couberem). Os namespaces compartilhados de uma confiança,
  com espaço para o namespace próprio de um agente de 63 caracteres, devem caber nesses 4,096 bytes, ou
  `loom bot create` a recusa.

  Um `tenant_id` exato mantém uma confiança restrita a um tenant, de modo que os agentes de cada tenant
  caiam em sua própria org (um prefixo de `sub` pode ser adicionado como condição
  adicional, nunca como a única). `--jwks-uri`
  indica onde o emissor publica suas chaves quando seu documento de descoberta não é
  acessível a partir do Loom. Um token que dura minutos é aceitável: o do Loom nunca
  dura mais que ele, e os SDKs e os plugins de agentes de código o renovam antes que
  expire (abaixo).
  Em vez de o agente guardar seu token, um proxy à frente dele pode fazer a mesma
  troca e adicionar o cabeçalho `Authorization` por conta própria. Uma troca pode
  omitir `client_id` (a RFC 8693 não o exige): o Loom então escolhe a única confiança
  ativa do emissor do token cuja audiência e claims ele atenda, e recusa um token que não
  atenda nenhuma ou atenda várias.

  Desativar a confiança encerra o token de todos os agentes; `loom revoke-token --subject
sa:fleet:<agent>` encerra os de um agente, e `loom claim-release` libera seu
  namespace definitivamente (esse agente é recusado depois). Um agente que o plano de controle
  aposenta mantém seu namespace e seus dados até você liberá-lo ou apagá-lo.

  O namespace de um agente existe a partir de sua primeira troca. Para conceder algo ao agente
  antes disso, como um namespace que um plano de controle anexa ao
  criar o agente, um administrador da plataforma o cria antecipadamente:
  `loom call /v1/admin/bots.provision_agent --data '{"name":"fleet","agent":"a"}'`
  cria `fleet-a` como essa troca faria (a marca da confiança, o agente como seu
  único dono, contado em `--max-agents`) e responde com sua org, namespace
  e subject (`sa:fleet:a`). Registra `auth.agent_provisioned` na auditoria com o
  administrador como ator, não cria nenhum namespace que a confiança recusaria, e uma repetição
  não muda nada; a primeira troca do agente assume o namespace como seu.

  Toda confiança limita o que aceita: um token de workload com duração maior que
  `--max-token-age` segundos (exp menos iat, padrão 3600) é recusado, uma confiança de agentes
  mantém no máximo `--max-agents` agentes ativos (padrão 1000; o namespace de um agente
  liberado devolve sua vaga, embora seu nome nunca seja criado de novo), e uma confiança precisa de
  um claim com valor exato além de `iss`, `aud`, `exp`, `iat`, `nbf` e
  `jti`, que todo token que seu emissor assina para o Loom carrega. Duas confianças de um mesmo
  emissor que um único token poderia atender, uma delas distinguindo agentes, são
  recusadas no registro: uma troca sem cliente indicado não poderia escolher nenhuma.
  O algoritmo de assinatura segue o tipo de cada chave publicada (RS256 ou ES256).
  As trocas são limitadas por 10 minutos, em todas as réplicas: 50,000 para uma
  confiança (`LOOM_WORKLOAD_EXCHANGES_PER_TRUST`) e, em uma confiança com um claim de
  agente, 300 para um agente (`LOOM_WORKLOAD_EXCHANGES_PER_AGENT`). Uma confiança
  sem agentes é um único workload e tem apenas o limite da confiança: todo job de
  um repositório compartilha um mesmo `sub`. Ao passar de um limite, `/oauth/token` responde 429,
  e os SDKs e os plugins mantêm o token que ainda têm e tentam de novo
  30 s depois. Só conta um token que o emissor assinou, então ninguém sem a
  chave do emissor pode consumir o orçamento de uma confiança, e um agente recusado em seu próprio
  limite não gasta nada do da confiança. Aumente o limite da confiança para uma frota cujos
  agentes renovam com frequência. Os SDKs e os plugins renovam cinco minutos antes de o
  token expirar, ou na metade de sua vida quando isso vier antes; os
  SDKs renovam em cada processo, os plugins uma vez por home de agente e por novo
  token de workload (hooks que disparam juntos podem renovar cada um). Quando cada
  renovação apresenta um token de workload novo, um token de uma hora renova após 55
  minutos e um de cinco minutos a cada 2.5 minutos, quatro trocas a cada 10
  minutos. O token do Loom vive só o tempo que resta ao token de workload, então
  um que a plataforma substitui no meio de sua vida, ou nunca, é renovado
  com mais frequência: um token de cinco minutos substituído a cada 4 minutos se estabiliza em cerca de
  três renovações por substituição, 7.5 trocas a cada 10 minutos em média
  (uma janela de dez minutos pode conter até nove), e um token nunca substituído
  renova cada vez mais cedo em seus últimos cinco minutos.

  Uma rajada de novos agentes é sinal de uma chave de emissor vazada ou de um plano de controle
  fora de controle, e uma confiança em seu limite recusa todo novo agente: crie alertas para
  `loom.auth.agents_provisioned` e `loom.auth.trust_agents_used_ratio`
  ([o que alertar](https://github.com/ClickHouse/loom/blob/main/docs/operations/monitoring.md#what-to-alert-on)).

- **Quanto tempo dura um token.** Sem pedido, um token dura uma hora, ou o máximo do bot
  quando for menor. Um bot registrado com
  `--max-access-token-ttl SECONDS` (60 a 86,400; `"max_access_token_s"` em
  `/v1/admin/bots.create`, listado por `/v1/admin/bots.list` e, quando não é
  uma hora, por `loom bot list`) recebe um token mais longo quando sua requisição para
  `/oauth/token` pede: `expires_in=<seconds>` junto com os demais campos de formulário. Ele
  recebe o que pediu até esse máximo (mais que isso é reduzido ao máximo, não recusado),
  e o `expires_in` da resposta diz o que ele recebeu; um máximo abaixo de uma hora encurta
  também o padrão. Um token de CI ou de agente continua nunca durando mais que o token de workload
  entregue, então um plano de controle que quer tokens de um dia entrega um
  token de workload que dura um dia, em uma confiança cujo `--max-token-age` o permita.
  Um `expires_in` que não é um número inteiro de segundos, ou é menor que 60, é
  recusado com `invalid_request` antes que a prova seja gasta ou contada.

  Um máximo diferente de uma hora é recusado até que todas as réplicas executem um servidor que
  o respeite (migração 77), e enquanto nenhum servidor em execução tiver se registrado em
  `loom.instances`: um servidor mais antigo ignora `expires_in` e o máximo,
  emite uma hora e redefine o máximo para 3600 ao salvar um bot. Após um
  rollback para esse servidor, os tokens de todo bot voltam a durar uma hora, e seus
  salvamentos redefinem o máximo; aplique-o de novo quando o servidor mais novo voltar a atender, com
  `loom bot create --replace`, a chave ou confiança e os papéis do bot como registrados
  e `--max-access-token-ttl`. Substituir um bot também encerra os tokens que ele possui.

  Um token mais longo não é mais difícil de encerrar: desativar ou substituir o bot,
  `loom revoke-token --subject` sem `--expires-at`,
  `/v1/admin/revoke_subject` (com ou sem `issued_before`) e
  `loom claim-release` recusam seus tokens imediatamente, qualquer que seja a expiração, e
  `/oauth/revoke` mantém a revogação de um token até que ele expire. Um
  `--expires-at` anterior à expiração do próprio token do bot deixa esse token voltar
  quando a revogação terminar. O que um token nomeia é lido quando ele é emitido: um
  compartilhamento ou concessão feito depois chega a um agente no próximo token, até um dia
  depois com tokens de um dia, enquanto revogar um não espera por isso.
  Excluir um namespace (`namespace.delete`) não encerra os tokens de bot que
  o nomeiam, como faz `loom claim-release`: eles mantêm o acesso até
  expirar, até um dia para um bot que o permita, então substitua ou desative o bot,
  ou revogue seu subject, para encerrá-los imediatamente. Rotacionar a chave de assinatura do
  emissor também não encerra os tokens do Loom já trocados por seus tokens antigos: eles
  vivem até expirar, então substitua ou desative a confiança para encerrá-los imediatamente.

- `loom bot list` os mostra; `loom bot disable --name …` recusa toda prova
  posterior e revoga os tokens que o bot possui, e `loom bot create --replace`
  (uma nova chave, menos papéis) também os revoga. Um administrador da plataforma faz o mesmo
  de qualquer lugar com `loom call /v1/admin/bots.create --data '{…}'` (com
  `"generate_key": true` a resposta traz a chave privada, uma única vez, e um
  `warning` para enviar a requisição de novo se o salvamento puder não ter terminado),
  `/v1/admin/bots.list` e `/v1/admin/bots.disable`; `/v1/admin/sso.adopt`,
  `sso.link`, `sso.unlink` e `sso.password_link` espelham `loom sso`.
- Substituir ou desativar um bot revoga seus tokens até o próximo segundo mais
  5 s, e ambos esperam esse tempo (cerca de 6 s) antes de retornar. Os relógios das réplicas
  devem concordar em até 5 s (o NTP garante isso): uma réplica adiantada poderia emitir um token
  do bot antigo que sobrevive, e uma atrasada recusa a nova chave até que seu
  relógio passe do ponto de corte.

Um software que só consegue guardar uma chave estática recebe um token emitido pelo operador, com
expiração (`--ttl-days 90`, reemitido periodicamente) ou sem ela, que vive
até `loom revoke-token`:

```bash
loom mint-token --subject sa:legacy --org acme --service-account \
  --namespace-role legacy=writer --json
```

Um sistema que administra o próprio Loom, como um plano de controle que registra
confianças e concessões para seus tenants, detém o papel `platform_admin` como bot.
**O administrador da plataforma abrange todas as orgs**: um token que o detém passa por toda rota
de administração e atua em qualquer org, então conceda-o ao menor número possível de identidades, e prefira uma
confiança (sem segredo para vazar) a uma chave. `--platform-admin` registra esse bot; ele
não aceita `--namespace-role` (seus tokens cobrem todos os namespaces), nem
`--agent-claim` (é uma única identidade, nunca a de um agente), e aceita apenas valores exatos de `--claim`
(um prefixo entregaria o papel a todo token que ele corresponda). Dê a ele uma
audiência própria, para que nenhum token destinado a outra confiança possa atendê-lo:

```bash
loom bot create --name controlplane --org acme --platform-admin \
  --issuer https://cp.example/workload-identity --audience loom-admin \
  --claim sub=controlplane
```

Cada prova recebe um token para `sa:controlplane` com o papel, com a mesma
duração solicitada e o mesmo máximo de qualquer bot. Sem pedido, dura uma hora,
ou o máximo do bot quando for menor. `loom bot disable` o encerra como encerra o de qualquer
bot. Somente uma pessoa autenticada
no Loom como administradora da plataforma (`"platform_admin": true` em `/v1/admin/bots.create`)
ou um operador no host define a flag: a rota a recusa ao token de qualquer
conta de serviço, inclusive o de um bot platform-admin, e à sessão de um cliente MCP
de terceiros, cujos tokens terminam com ela, para que um token vazado nunca crie para si
um sucessor duradouro. A linha de auditoria `auth.machine_created` a registra,
e `loom bot list` e `/v1/admin/bots.list` a mostram. Uma confiança do mesmo emissor
que distingue agentes não pode conseguir pegar o token da confiança platform-admin:
dê a cada uma um claim que os tokens da outra não possam atender, como valores distintos de `sub`
(`sub=controlplane` para esta, `sub=tenants/<id>/agents/*` junto a um
`tenant_id` exato para a de um tenant), ou o registro recusa a segunda.
Onde uma confiança não pode ser usada, um operador emite um token platform-admin com expiração para uma conta de
serviço. `--platform-admin` recusa um token sem
`--ttl-days` ou por mais de 90 dias, recusa o subject de um agente
(`sa:<trust>:<agent>`) e registra cada emissão como uma linha de auditoria
`auth.platform_admin_minted`:

```bash
loom mint-token --platform-admin --subject sa:controlplane --org acme --ttl-days 30 --json
```

Tokens de máquina de administrador só são emitidos depois que todas as réplicas ativas protegem as alterações de login humano. Uma réplica mais antiga ou uma frota não lida recusa a troca com 503 `temporarily_unavailable`; a CLI lê uma visão atual da frota e também se recusa a emitir.
Ambas as verificações incluem processos de implantação e de não implantação que compartilham o ClickHouse; nenhuma usa o cache de réplicas, que separa esses dois tipos.
A CLI exige pelo menos uma réplica ativa e não pode emitir tokens contra uma frota vazia.
Antes de voltar para uma build mais antiga, desative os bots de administrador e revogue todo subject de administrador ou token emitido pela CLI de forma independente, antes que essa build comece a atender.

O SSO não afeta bots nem tokens emitidos. Um token que uma pessoa cria a partir de uma sessão de `loom
login` ou de SSO do painel (`/v1/me/service_accounts`) dura o tempo que ela escolheu e pertence a ela: revogar a pessoa encerra o token. Um token criado pela sessão de um cliente MCP de terceiros termina junto com essa sessão. Bots de longa duração usam uma das opções acima, portanto não deixam de funcionar quando alguém é desligado.

<a id="sessions-and-logout"></a>

## Sessões e logout

- O token de SSO do painel dura uma hora. Depois disso, o usuário clica no
  botão de novo, e a própria sessão do Okta normalmente faz disso um único clique.
- `loom logout` revoga a sessão do Loom no servidor e o refresh token do Okta
  no Okta.
- Se um refresh token já usado voltar, outra pessoa pode ter uma cópia (ou um
  cliente perdeu a resposta de um refresh): o Loom encerra essa sessão e a
  pessoa entra novamente. Contas de serviço e tokens MCP gerados a partir de uma
  sessão de `loom login` ou de SSO do painel não são afetados; após uma suspeita
  de roubo, revise-os (`loom call /v1/me/service_accounts`) e revogue o que você
  não reconhecer. Os que foram gerados pela sessão de um cliente MCP de terceiros
  terminam junto com ela.
- Se o Okta recusar o usuário em um refresh, essa sessão é encerrada.
- Contas de serviço e tokens MCP que uma pessoa cria a partir de uma sessão de
  `loom login` ou de SSO do painel sobrevivem a ela, inclusive a `loom logout`,
  pelo tempo que ela escolheu (em branco são 90 dias, 0 nunca expira). Cada um
  é salvo na lista de tokens da pessoa, então nada que ela não consiga ver
  sobrevive à sessão. A sessão de um cliente MCP de terceiros não gera nenhum
  que sobreviva a ela. Desativar alguém no provedor encerra as sessões da pessoa
  no próximo refresh (dentro de uma hora), mas não esses tokens: só um refresh
  consulta o provedor sobre uma pessoa. Com o [SCIM](#deprovisioning-with-scim)
  configurado, o provedor avisa o Loom na próxima execução de provisionamento
  (o Okta quando a pessoa é desativada, o Entra dentro do seu ciclo), e o Loom
  revoga a pessoa e todos os tokens que ela criou. Sem ele, revogue a pessoa no
  desligamento, o que também encerra todos os tokens que ela criou:
  `loom revoke-token --subject <their subject>`, ou
  [`POST /v1/admin/revoke_subject`](#deprovisioning-without-scim) de qualquer lugar.
- Cada login, aprovação e negação de dispositivo, consentimento de MCP, conta JIT
  e sessão encerrada grava uma linha `auth.*` no log de auditoria:
  `auth.sso_signin`, `auth.signin_denied`, `auth.device_approved`,
  `auth.device_denied`, `auth.connect_approved`, `auth.connect_denied`,
  `auth.jit_provisioned`, `auth.sso_linked`, `auth.sso_unlinked`, `auth.sso_link_declined`, `auth.password_set`, `auth.agent_claimed`, `auth.machine_created`, `auth.machine_token`, `auth.machine_disabled`, `auth.agent_provisioned`, `auth.platform_admin_minted`,
  `auth.session_revoked`, `auth.scim_deactivated`, `auth.scim_reactivated`,
  `auth.scim_deleted`, `auth.scim_collateral`.

<a id="deprovisioning-with-scim"></a>

## Desprovisionamento com SCIM

Os provedores de identidade enviam desativações aos aplicativos por SCIM 2.0 (RFC 7643/7644).
O Loom aceita esse envio: assim que o provedor o envia, as sessões da pessoa e
os tokens que ela criou são recusados imediatamente pelo processo do Loom que recebeu a
requisição, e por todos os outros processos, nessa réplica ou em outra, na sua próxima
atualização de revogações (`LOOM_JWT_REVOCATION_CACHE_TTL_SECONDS`, 30 s por padrão).
Nenhum processo cria um novo token para a pessoa nesse intervalo: toda emissão lê as revogações do
ClickHouse. Defina um token longo e aleatório e reinicie:

```bash
LOOM_SCIM_TOKEN=$(openssl rand -hex 32)
```

Vazio (o padrão), o SCIM fica desativado e suas rotas não são montadas. O Loom se recusa a
iniciar com um token de menos de 32 caracteres, ou com o SCIM definido enquanto o login
não é SSO (`LOOM_OAUTH_LOGIN_PROVIDER=oidc`) ou a autenticação está desativada: uma desativação
não recusaria ninguém. O `loom doctor` também avisa. O token é a única
credencial que `/scim/v2` aceita; guarde-o junto com o client secret.

Para trocá-lo sem recusar o provedor no meio do processo, liste os dois tokens,
separados por vírgula, e faça o rollout das réplicas; defina o novo no provedor; depois
remova o antigo e faça o rollout de novo:

```bash
LOOM_SCIM_TOKEN="$OLD_TOKEN,$(openssl rand -hex 32)"
```

O Loom identifica um usuário SCIM por um e-mail: entre as entradas de `emails` da requisição que têm
endereço, a marcada como primary, senão a primeira work, senão a primeira; ou
o seu `userName` quando a requisição não envia nenhuma (o Loom registra um aviso quando um usuário é criado
ou renomeado com um `userName` diferente desse endereço). Esse endereço deve ser
o mesmo que o provedor coloca na claim `email` do ID token, pois é isso que
um login carrega.

No Okta, na integração do aplicativo de [Configuração do Okta](#okta-setup):

1. **General → App Settings → Edit**, defina **Provisioning** como **SCIM** e
   salve.
2. **Provisioning → Integration → Edit:**
   - **SCIM connector base URL:** `<public Loom URL>/scim/v2`
   - **Unique identifier field for users:** `userName`
   - **Supported provisioning actions:** Push New Users e Push Profile Updates
   - **Authentication Mode:** HTTP Header, com o valor de `LOOM_SCIM_TOKEN` como
     bearer token.

   Clique em **Test Connector Configuration** e salve.

3. **Provisioning → To App → Edit:** ative **Create Users**, **Update User
   Attributes** e **Deactivate Users**, e salve. O Loom não guarda atributos
   além do `userName` (uma renomeação é mantida) e da lista `emails`, cujo
   e-mail de identificação não pode mudar.
   Com as atualizações ativadas, uma mudança de e-mail aparece como tarefa com falha no Okta (o Entra
   registra a mesma recusa); siga
   [Alteração do e-mail de uma pessoa](#changing-a-persons-email). Com as atualizações desativadas,
   o Okta nunca informa que o e-mail mudou: o próximo login da pessoa cria uma nova
   conta com o novo endereço, que uma desativação posterior do antigo
   não alcança.
4. Em **General → Sign On**, defina **Application username format** como
   **Email**: o Loom identifica as pessoas pelo e-mail.

As pessoas já atribuídas são enviadas na próxima vez que você as atribuir ou atualizar, ou
em **Assignments → Provision User**. Outros provedores (OneLogin, …) usam a
mesma URL base e o mesmo bearer token.

O Okta envia uma desativação (a pessoa desativada no Okta, ou removida
do aplicativo) como `active: false` por `PATCH` ou `PUT`, e
nunca envia `DELETE`.

No **Microsoft Entra ID**, no aplicativo corporativo com o qual você faz login:

1. **Provisioning → Get started**, defina **Provisioning Mode** como **Automatic**.
2. **Admin Credentials:** **Tenant URL** `<public Loom URL>/scim/v2`, e o
   valor de `LOOM_SCIM_TOKEN` como **Secret Token**. Clique em **Test Connection** e salve.
3. **Mappings:** mantenha o mapeamento de usuários. O Entra mapeia `userPrincipalName` para
   `userName` e `mail` para `emails[type eq "work"].value`, e a claim
   `email` do seu ID token é `mail`, então o Loom obtém o e-mail de `mail`. Quando um usuário
   não tem `mail`, mapeie `mail` a partir do endereço que o ID token dele carrega, não
   do UPN, que pode ser outro endereço. Correspondência por `userName` (o
   padrão) ou por `emails[type eq "work"].value` funcionam. Defina **Provision
   Microsoft Entra ID Groups** como **No**: o Loom não guarda grupos e recusa
   criá-los, o que o Entra contaria como falha a cada ciclo.
4. **Settings → Scope:** **Sync only assigned users and groups** e depois
   **Start provisioning**.

O Entra envia `active: false` quando um usuário é desabilitado, excluído de forma reversível ou
removido do aplicativo, e `DELETE` apenas em uma exclusão definitiva (30 dias após uma exclusão reversível).
Ele provisiona em ciclos de cerca de 40 minutos, então uma desativação chega ao Loom
no ciclo seguinte; **Provision on demand** no usuário a envia imediatamente.

O que o Loom faz com cada requisição:

- **Criar um usuário** registra o e-mail e não cria conta: o primeiro login SSO
  da pessoa a cria (JIT) ou alcança a que ela já tem, como sem SCIM. Uma segunda criação
  do mesmo e-mail, ou de um `userName` que outro usuário já tem, responde 409.
- **Atualizar** (`PUT`, ou `PATCH` de `active`, `userName` ou `emails`) mantém
  um novo `userName`, a menos que outro usuário o tenha (409), e mantém a lista `emails`
  enviada. Um `PATCH` aplica suas operações a essa lista em ordem,
  e a requisição é recusada (400 `mutability`) quando a lista resultante identifica
  outro e-mail pela regra acima: a pessoa entraria em outra
  conta, que uma desativação posterior do e-mail antigo não alcançaria. Um `add`
  ou `replace` por meio de um filtro que não seleciona nenhuma entrada adiciona uma, como as gravações do Entra
  esperam. Outros endereços podem ser adicionados, alterados ou removidos (veja
  [Alteração do e-mail de uma pessoa](#changing-a-persons-email)). Uma requisição recusada
  que também define `active: false` ainda desativa: os provedores enviam as mudanças de quem saiu
  juntas e não repetem um 400. Um `PUT` cujo corpo cita o `id` de outro
  usuário é recusado antes que qualquer coisa seja aplicada.
- **Desativar** (`active: false` por `PATCH` ou `PUT`, ou um `DELETE`)
  revoga de imediato todo subject que um login SSO com o e-mail poderia alcançar
  (cada conta SSO que o cita e cada conta de senha vinculada a ele, em qualquer
  org): todas as sessões, e toda service account e todo token MCP que a pessoa
  criou. Os tokens dela aparecem como revogados por `scim` na lista Tokens e seus
  nomes ficam livres de novo; uma reativação não os traz de volta. Uma concessão
  que um desses tokens tinha em um namespace restrito em seu próprio nome permanece
  na lista de concessões do namespace, embora não alcance nada: revogue-a ali para limpar
  a lista. Enquanto durar, o login SSO com o e-mail é recusado ("this
  account is deactivated"), mesmo que ele ainda não alcance nenhuma conta. As contas,
  suas concessões e seus dados permanecem; isso não é apagamento. Bots e confianças de CI
  que a pessoa registrou (`loom bot`) pertencem à org e continuam funcionando:
  desative-os com `loom bot disable` se devem parar. Uma conta de senha que
  cita o e-mail mas nunca foi vinculada a ele (não adotada, vinculada ou comprovada
  em um login SSO) não é alcançada por um login SSO, então é deixada em paz:
  revogue-a com `loom revoke-token --subject`, ou adote-a antes do desligamento
  ([Contas](#accounts)). Do mesmo modo, um token que um operador emitiu com o próprio e-mail
  como subject (`loom mint-token --subject <email>`) para uma conta adotada
  cujo subject é um handle: revogue o e-mail com
  `loom revoke-token --subject <email>`. Um subject é um nome, não pertence a uma org:
  quando um também possui uma conta ou tem uma concessão em uma org onde o e-mail
  não alcança esse subject (o `alice` de outra pessoa), ele ainda assim é revogado,
  e o Loom registra um aviso e audita `auth.scim_collateral` citando essas orgs.
  Encontre-o em `loom revoke-token --list` (`scim→<it>`) e, se for de
  outra pessoa, devolva-o com
  `loom revoke-token --subject <it> --issued-before <now> --scim`: seus novos
  tokens funcionam, e os tokens anteriores à desativação continuam mortos (o Loom
  recusa um `--issued-before` anterior). O provedor repetir a
  desativação, ou excluir o usuário, mantém o subject devolvido, mas, quando a
  pessoa for reativada, a próxima desativação dela o revoga de novo por completo:
  devolva-o novamente nesse caso.
- **Reativar** (`active: true`) permite que a pessoa entre de novo. Os tokens anteriores
  continuam revogados; ela cria novos. Um subject que o e-mail de outro usuário desativado ou
  excluído ainda alcança (dois endereços alcançando uma conta)
  continua revogado, e o Loom registra um aviso para cada um que deixa assim. Quando nenhum
  o alcança mais (o último é reativado, ou um operador move a conta para
  outro endereço com `loom sso link`), o próximo `PUT` ou `PATCH` de um usuário
  ativo cujo e-mail o alcança o libera. O Okta envia um em uma mudança de perfil
  somente com **Update User Attributes** ativado; o Entra envia um quando um atributo
  mapeado muda. Caso contrário, ou imediatamente,
  `loom revoke-token --subject <it> --issued-before <now> --scim` o devolve
  como operador. Uma reativação responde após cerca de 6 s (até 11 s
  quando vem logo após a desativação), quando o relógio de todas as réplicas
  passou do seu ponto de corte, de modo que um login depois dela recebe um token que vive (um login
  durante essa espera receberia um token anterior ao corte, já morto); uma nova tentativa enviada
  nesse meio-tempo espera o restante. Outro processo, nessa réplica ou em outra,
  ainda recusa a nova sessão até a sua próxima atualização de revogações, e depois a aceita.
- **Excluir** desativa e o usuário responde 404 a partir daí
  (`auth.scim_deleted` é auditado). O provedor criá-lo de novo
  o reativa, respondendo após a mesma espera; uma nova tentativa dessa criação
  espera o restante e depois responde 409.

O SCIM mantém suas revogações separadas das de um operador. Cada subject que uma
desativação revoga recebe uma revogação SCIM própria, ao lado da que
`loom revoke-token --subject` grava, e um subject fica revogado enquanto qualquer uma
disser isso. O SCIM nunca altera a revogação de um operador, e a de um
operador nunca altera a do SCIM: um `--expires-at` com prazo ou uma suspensão com
`--issued-before` enquanto a pessoa está desativada não a deixa entrar, e
uma reativação suspende apenas a do SCIM, então um subject que um operador revogou continua
revogado. Somente `--scim` altera a revogação SCIM de um subject.

O Loom guarda a linha de cada usuário SCIM em `loom.scim_users` (o userName, o e-mail,
a lista `emails` que o provedor enviou, os subjects que revogou e se está
ativo) pelo tempo em que o deployment existir, inclusive usuários excluídos: essa linha
é o que mantém uma pessoa desativada fora. Um `DELETE` limpa a lista `emails`;
o e-mail permanece. As requisições são idempotentes, então as novas tentativas do provedor são seguras,
e rodam em paralelo: só as de pessoas que alcançam uma conta compartilhada
esperam umas pelas outras (uma ocupada responde 503 e o provedor tenta de novo). Os
filtros atendidos são `userName eq "<userName>"` e, para o e-mail,
`emails[type eq "work"].value eq "<email>"` (ou `emails.value eq`), que
correspondem a todo usuário com uma entrada de `emails` contendo o endereço, em qualquer caixa (uma
entrada `work` para a primeira), não apenas ao identificado por ele. `GET /Users`,
`GET /Users/{id}` e `GET /ServiceProviderConfig` respondem como a RFC 7644 determina,
e `GET /Groups` responde com uma lista vazia.

Desativar o SCIM (`LOOM_SCIM_TOKEN` vazio) desmonta `/scim/v2`, então nada
desativa ou reativa mais ninguém, e deixa todos como estão:
o login SSO com um e-mail desativado continua recusado, e os subjects de uma pessoa
desativada permanecem revogados, inclusive os novos tokens, até que um operador suspenda
cada um com `loom revoke-token --subject <it> --issued-before <now> --scim`.
Isso faz os novos tokens do subject funcionarem (os antigos continuam revogados), não o
login dele: para permitir que uma pessoa desativada entre de novo com o SCIM desligado, defina
`LOOM_SCIM_TOKEN` novamente e envie `active: true` para ela, pelo provedor ou
manualmente. Reative primeiro no provedor quem deve manter o acesso ao Loom.

<a id="changing-a-persons-email"></a>

### Alteração do e-mail de uma pessoa

O Loom identifica um usuário SCIM pelo endereço e recusa uma mudança dele (400), então um
novo endereço deve chegar como um novo usuário SCIM. Até que chegue, um login com o
novo endereço cria uma conta que nenhum usuário SCIM alcança, e que a desativação do
endereço antigo não alcança. Altere-o antes que a pessoa entre com o novo:

1. Remova a pessoa do aplicativo no provedor. Isso desativa o endereço antigo
   e revoga as contas que ele alcança.
2. No Entra, exclua também o usuário SCIM antigo: o Entra corresponde usuários por
   `userName` (o UPN), que uma mudança de e-mail mantém, então uma nova atribuição
   encontraria o usuário antigo e enviaria o novo endereço como uma mudança que o Loom recusa. Encontre
   o id dele com `GET /scim/v2/Users?filter=userName eq "<upn>"` e depois
   `DELETE /scim/v2/Users/<id>`, ambos com o token SCIM. O Okta procura usuários
   por `userName`, que ali é o e-mail, então ele mesmo cria um novo.
3. Atribua a pessoa novamente. O provedor cria o usuário SCIM do novo endereço.

O que acontece com a conta antiga:

- Uma conta de senha: aponte-a para o novo endereço com
  `loom sso link --email <new> --org <org> --namespace <ns> --apply`. O endereço
  antigo deixa de alcançá-la, e o próximo `PUT` ou `PATCH` do novo
  usuário a libera (ou devolva-a imediatamente com
  `loom revoke-token --subject <it> --issued-before <now> --scim`).
- Uma conta criada em um login SSO não tem comando para movê-la: o primeiro login
  da pessoa com o novo endereço cria uma nova conta. Conceda a ela os namespaces da conta
  antiga; a conta antiga e seus dados permanecem, revogados.

<a id="deprovisioning-without-scim"></a>

## Desprovisionamento sem SCIM

Um sistema que sabe quando uma pessoa sai, como um plano de controle que também
emite o login dela, a revoga pela API de administração. Um administrador da plataforma
envia:

```bash
loom call /v1/admin/revoke_subject --data \
  '{"org":"acme","subject":"lee@acme.io","reason":"left","issued_before":"2026-01-31T12:00:00Z"}'
```

O Loom revoga o subject e todas as grafias que o diretório da org associa a ele,
com o e-mail verificado ou não (uma concessão só admite por pares verificados): um e-mail
alcança o identificador de login de cada conta que o cita (o subject de uma conta SSO é
o seu namespace, `u-…`), e um identificador alcança o seu e-mail. Para cada um, todo token
cujo subject seja ele, todo token que ele criou ou que descenda de um de seus tokens,
e toda renovação de suas sessões de login são recusados imediatamente pelo processo que
recebeu a requisição, e por todos os outros processos na sua próxima atualização de revogações.
Toda emissão lê as revogações do ClickHouse, de modo que um login, uma aprovação de dispositivo ou
uma conexão MCP para ele também não obtém token. As contas de serviço e os tokens MCP
que ele encerra aparecem como revogados na lista de Tokens, por `admin:<you>`, e seus nomes
ficam livres novamente, como após uma desativação via SCIM. As contas, suas concessões e
seus dados permanecem; isto não é uma exclusão.

`issued_before` (RFC 3339, com o seu offset) limita a revogação ao que foi
emitido antes desse instante, em caráter definitivo: uma plataforma que desprovisiona alguém envia o
horário atual, de modo que, se a pessoa for convidada de volta mais tarde, seu próximo login, uma
nova aprovação de dispositivo ou uma nova conexão MCP funcione. O Loom arredonda o instante para o próximo
segundo, mais os 5 s que os relógios das réplicas podem divergir, de modo que nada emitido antes dele em
qualquer réplica sobreviva; um token emitido nesses poucos segundos depois dele também é recusado.
Um instante à frente do relógio do servidor conta como agora, de modo que o relógio de um chamador adiantado
nunca defina um corte que recuse logins ainda por vir. Sem
`issued_before`, a revogação é total, como `loom revoke-token --subject`
a grava: todo token do subject, emitido antes ou depois, é recusado
em definitivo, que é a forma para um banimento permanente. Uma requisição nunca enfraquece o que o
subject já tem. Um corte é registrado ao lado da revogação do próprio subject (como
um corte do operador, `kind: cutoff` na lista abaixo) e só avança para
mais tarde, de modo que uma revogação total continua em vigor, e uma com prazo
(`loom revoke-token --expires-at`) vale até expirar, e o corte permanece
depois disso; uma requisição total deixa como está uma revogação que já é total
em definitivo.

```json
{
  "subjects": ["lee@acme.io", "u-lee-1a2b3c4d"],
  "org": "acme",
  "revoked_at": "2026-01-31T12:00:00",
  "reason": "left",
  "issued_before": "2026-01-31T12:00:06Z",
  "outright": [],
  "sessions": 2,
  "tokens": 3
}
```

`issued_before` na resposta é o corte agora em vigor para os subjects não
revogados totalmente, o mais antigo quando os deles diferem: o que a requisição pediu,
ou um mais tardio definido por uma requisição anterior; é `null` para uma requisição total, e
quando todos os subjects são revogados totalmente. `outright` nomeia os subjects recusados
totalmente agora: todos eles após uma requisição total, e, após um corte, aqueles que uma
revogação total (a própria deles, com prazo ou não, ou a do SCIM) ainda mantém. `sessions` conta as sessões de login desses subjects que a revogação
encerra (cada uma renovada antes do corte), e `tokens` os tokens que eles criaram,
diretamente ou por meio de seus tokens, que ela encerra. Um subject é um nome, não pertence a
uma org, então a revogação vale em todas as orgs; `org` escolhe o diretório de onde vêm as
grafias e o log de auditoria em que cada linha `token.revoked` é gravada, e o subject de um
usuário final, `<org>/user:<id>`, deve nomear a sua própria org. Quando o diretório
não pode ser lido, nada é revogado e a requisição falha. Uma repetição responde
o mesmo e não grava mais nada, então um chamador tenta de novo até obter um 200; uma falha após
as revogações terem sido gravadas (a marcação dos tokens não terminou) é concluída
pela nova tentativa.

`GET /v1/admin/revocations?subject=<subject>` lista as revogações de um subject:
as próprias, as de um corte do operador e as do SCIM (`kind`). Sem `subject`, lista
todas as não expiradas, como `loom revoke-token --list` faz, da mais nova para a mais antiga; `since`
(RFC 3339) mantém as gravadas desde então, e `limit` (1 a 10000, padrão 1000)
limita a resposta. O SDK Python os passa como
`agent.deployment.routes.revocations(params={"subject": ...})`. Para deixar uma pessoa
revogada totalmente voltar, restrinja a revogação em vez de removê-la: com
`loom revoke-token --subject <it> --issued-before <now>`, os tokens criados a partir
daí funcionam e tudo o que veio antes continua morto.

<a id="behind-a-proxy"></a>

## Atrás de um proxy

Os limites de login por 15 minutos (10 códigos de dispositivo incorretos, 30 novos logins de dispositivo, 300 inícios de login no provedor de identidade e 30 callbacks com falha) contam por endereço do cliente, sendo um IPv6 /64 contado como um só. Defina `LOOM_TRUSTED_PROXIES` com os endereços do seu proxy, para que o endereço considerado seja o do próprio usuário, e não o do proxy para todos.

Quando o Loom não consegue alcançar o provedor de identidade no host do próprio emissor (por exemplo, quando seu nome público resolve para um endereço que só os navegadores alcançam), defina `LOOM_OIDC_BACKCHANNEL_BASE_URL` com um endereço pelo qual ele consiga alcançar o provedor, como `http://idp.example:8080`: um esquema, um host e uma porta opcional, nada além disso. As chamadas do próprio Loom ao host do emissor passam então por esse endereço, mantendo o caminho e enviando o host do emissor no cabeçalho `Host`: o documento de descoberta (`<base>/<issuer path>/.well-known/openid-configuration`), os endpoints de token e de revogação, e as chaves com as quais os ID tokens são verificados. O navegador continua indo ao `authorization_endpoint` do emissor, um endpoint que o documento indique em outro host é chamado como está, as chaves da confiança de um bot são sempre buscadas no endereço https que ela indica, e tanto o documento quanto cada ID token ainda precisam indicar `LOOM_OIDC_ISSUER` como emissor. Nenhuma dessas chamadas segue um redirecionamento. Uma base `http` simples envia o segredo do cliente sem criptografia: use-a somente em uma rede em que você confie.

<a id="clickhouse-keeper"></a>

## Keeper do ClickHouse

O login mantém códigos, concessões de dispositivo e o refresh token atual de cada sessão no
Keeper do ClickHouse e só os altera por compare-and-set, de modo que cada um seja consumido
exatamente uma vez; o Loom exige um Keeper e se recusa a iniciar sem um.
Se `/v1/health` informar `oauth_signin_state_full`, a tabela do Keeper atingiu o limite de
chaves (uma enxurrada de logins de dispositivo): novos logins respondem 503 até que as chaves expirem,
portanto limite a taxa das rotas de login por rede no seu proxy.

<a id="upgrading-to-this-build"></a>

## Atualizando para esta build

As conexões MCP feitas antes dela precisam entrar mais uma vez: seus tokens de renovação não tinham nenhuma sessão que os encerrasse, então não são rotacionados. Enquanto a build anterior ainda atende (uma atualização gradual, um rollback), ela não conhece sessões nem cortes de operador: um token de acesso cuja sessão terminou continua funcionando lá durante a sua hora de validade, e o mesmo vale para os tokens que a desativação de um bot ou uma adoção cortaram. Conclua o rollout antes de adotar contas ou de contar com um logout ou com a desativação de um bot. A build anterior não atende SCIM e não lê as desativações nem as revogações do SCIM: uma pessoa desativada via SCIM ainda entra lá, e os tokens dela ainda funcionam lá. Defina `LOOM_SCIM_TOKEN` somente quando todas as réplicas executarem esta build, e não faça rollback para uma versão anterior a ela enquanto houver alguém desativado: a build anterior permite que essa pessoa entre. Antes de um rollback, bloqueie cada uma delas no provedor de identidade (suspenda-as ou remova-as do aplicativo) e revogue cada subject que elas alcançam com `loom revoke-token --subject`. Um e-mail desativado que não alcança nenhuma conta não tem subject a revogar: o bloqueio no provedor é a única coisa que o mantém fora da build anterior.

Um token criado por outro token é encontrado, quando uma desativação libera os nomes de token da pessoa, por meio dos subjects acima dele. Esta build os registra em cada token e os repassa aos filhos de um token antes que um novo token reutilize o nome dele. Um token criado antes desta build perde a pessoa no topo de sua cadeia quando um nome acima dele foi reutilizado antes desta build (reutilizações posteriores repassam apenas o que restou): ele ainda é recusado (a revogação o alcança pela cadeia de delegação), mas continua listado como ativo e mantém o nome. Liste os tokens cujos subjects acima são todos tokens, nenhum deles ativo, de modo que nenhum leve a uma pessoa, com

```sql
SELECT org, name, sa_subject, created_by FROM loom.service_accounts FINAL
WHERE revoked_at IS NULL AND (expires_at IS NULL OR expires_at > now())
  AND arrayAll(s -> match(s, '^sa:[^:]+$'), arrayConcat([created_by], lineage))
  AND (org, name) NOT IN (
    SELECT org, name FROM loom.service_accounts FINAL
    ARRAY JOIN arrayConcat([created_by], lineage) AS above
    WHERE above IN (
      SELECT sa_subject FROM loom.service_accounts FINAL
      WHERE revoked_at IS NULL AND (expires_at IS NULL OR expires_at > now())))
```

Os agentes de uma confiança de máquina (`sa:<trust>:<agent>`) contam aqui como pessoas. Um token criado a partir de um subject que você gerou com `loom mint-token --subject sa:…` aparece em toda execução e não é um desses: mantenha-o. Um token criado antes desta build por um token já revogado também aparece: ele já está morto, e revogá-lo libera o nome. Revogue cada um dos outros tokens listados, como administrador de um de seus namespaces, com `/v1/me/service_accounts.revoke` e o `name` dele (o criador dele é um token, não uma pessoa, então ele não está na lista de ninguém; a lista de Tokens de um administrador do namespace o mostra entre os 200 mais recentes). Depois execute a consulta de novo: um token abaixo de um que você revogou aparece assim que esse é revogado. Pare quando ela listar apenas tokens criados a partir de subjects que você gerou. Enquanto a build anterior ainda atende, um token que ela lista, revoga ou cujo nome reutiliza não registra nenhuma linhagem.

Um login pelo navegador cujas etapas caem em ambas as builds (um código trocado, o callback de um provedor de identidade) falha e é iniciado de novo. O SDK e o `loom login` contornam isso: esta build marca suas respostas de token e de login por dispositivo com um header `Loom-Sessions`, então, depois que um servidor o enviou, uma recusa sem ele é da build anterior, e eles mantêm a sessão (uma renovação diz para tentar de novo) ou continuam consultando (um login por dispositivo). Um proxy que remove o header apenas perde isso. Faça o upgrade rapidamente para que poucos logins atravessem a transição: com o Helm chart, um surge de toda a contagem de réplicas de uma vez.

<a id="turning-sso-off"></a>

## Desativando o SSO

Defina `LOOM_OAUTH_LOGIN_PROVIDER=password` (ou remova a variável) e reinicie. As contas de senha, inclusive as adotadas ou vinculadas, voltam a entrar com suas senhas; um vínculo nunca remove uma senha. Uma sessão iniciada por SSO termina na próxima renovação, e o dono dela entra novamente. Uma conta criada por JIT não tem senha: defina uma para o dono com

```bash
loom sso password-link --email lee@example.com
```

que imprime um link que a define, uma única vez, dentro de `LOOM_PASSWORD_LINK_TTL_SECONDS` (um dia por padrão), e somente enquanto o SSO estiver desativado. A conta continua vinculada, então ativar o SSO novamente a alcança. Ativar ou desativar o SSO não afeta os bots (`loom bot`) nem os tokens emitidos pelo operador.

<a id="break-glass"></a>

## Acesso de emergência

Com o SSO ativado, ninguém consegue entrar com senha, incluindo os operadores. Se o provedor estiver fora do ar, gere um token no host do servidor com
`docker exec loom-server loom mint-token --subject <you> --org <org> --namespaces <ns>`
e abra `/me#token=<token>`: a página de entrada de login, que nunca redireciona (`/`
segue `LOOM_DASHBOARD_REDIRECT_URL`, e o navegador levaria o token junto).

<a id="other-providers"></a>

## Outros provedores

As mesmas configurações funcionam com qualquer provedor OIDC que disponibilize a descoberta em
`<issuer>/.well-known/openid-configuration`:

- **Microsoft Entra ID:** `LOOM_OIDC_ISSUER=https://login.microsoftonline.com/<tenant>/v2.0`.
  Os ID tokens dele muitas vezes não trazem `email_verified`, e o Loom os recusa.
  Use uma configuração de provedor que afirme esse campo.
- **Google:** `LOOM_OIDC_ISSUER=https://accounts.google.com`. O Google não emite
  refresh token para o Loom, então as sessões duram uma hora.
- **Keycloak:** `LOOM_OIDC_ISSUER=https://<host>/realms/<realm>`.
