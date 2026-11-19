<!-- i18n: source=docs/guides/without-the-dashboard.md blob=d950ac1fe252e9d585139b1da802185c47ab7aa9 model=claude-sonnet-5-5 -->
> Esta página é uma tradução automática da [página em inglês](https://github.com/ClickHouse/loom/blob/main/docs/guides/without-the-dashboard.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/guides/without-the-dashboard.md), which is authoritative.

<a id="use-loom-without-the-dashboard"></a>

# Use o Loom sem o painel

Tudo o que o painel `/me` faz pode ser feito a partir de um terminal, de um script ou do shell de um agente. Faça login uma vez com `loom login` e depois use `loom call` ou o SDK Python. Esta página associa cada tela do painel ao seu comando e à sua chamada do SDK; toda tela que não está listada também corresponde a operações simples (`loom call --list`).

<a id="sign-in"></a>

## Entrar

```console
$ loom login --server https://loom.example.com   # approve on any browser: password or SSO
$ loom whoami
$ loom logout
```

`loom login` armazena uma sessão que se renova sozinha. Atrás de um roteador de
[células](../architecture/cells.md), `loom login --org <org>` faz o login na célula própria da organização:
ele consulta o `GET /cells/lookup` do roteador, mantém o servidor informado quando não há
roteador ou ainda não há célula, espera um 429 por no máximo `--wait` segundos (padrão 120) e,
caso contrário, recusa sem iniciar um login. A sessão passa então a ser do host da célula: aponte
`--server` ou `LOOM_BASE_URL` para o host que ele imprime. CI e serviços não têm
ninguém para aprovar o código, então entram como bot (abaixo) ou com um token
com escopo em `LOOM_API_KEY`. Veja [tokens e concessões](https://github.com/ClickHouse/loom/blob/main/docs/guides/tokens-and-grants.md#sign-in-from-a-terminal-or-a-notebook).
Com apenas o SDK instalado, execute os mesmos comandos como `python -m loom login`
e assim por diante.

<a id="bots-and-ci-jobs"></a>

## Bots e jobs de CI

Um bot não precisa de `loom login`: registre-o uma vez (`loom bot create`, ou, como administrador da plataforma, `loom call /v1/admin/bots.create`) e depois execute-o com `LOOM_CLIENT_ID` e com a chave privada (`LOOM_CLIENT_KEY_FILE`, `LOOM_CLIENT_KEY` ou um segredo do AWS Secrets Manager em `LOOM_CLIENT_KEY_SECRET`) ou, em um job de CI, com `LOOM_WORKLOAD_AUDIENCE`.
O SDK obtém e renova o próprio token: ele dura uma hora, ou menos quando o máximo do bot é menor. Veja [login único (single sign-on)](https://github.com/ClickHouse/loom/blob/main/docs/operations/sso.md#bots-and-ci).

<a id="loom-call-the-whole-api"></a>

## `loom call`: a API inteira

```console
$ loom call --list                          # every operation and the role it needs
$ loom call memory.search query="deploy window" top_k:=5
$ loom call grants.approve namespace=support subject=bob@example.com decision=approve
$ loom call /v1/me/service_accounts         # a route outside the operation registry
```

Os seus próprios tokens para CI e agentes têm um comando específico:

```console
$ loom token create --name ci --namespace-role support=writer --namespace docs --ttl-days 0
$ loom token list
$ loom token revoke --name ci
```

- **Um nome de operação** é enviado com POST para `/v1/<op>`. `org` e `namespace` vêm do seu token; `--org` e `--namespace` os substituem.
- **Um caminho** (começa com `/`) é enviado como foi informado: GET sem corpo, POST com corpo.
- **Os campos** são `key=value` para texto, enviado exatamente como digitado, e `key:=json` para qualquer outro valor (`top_k:=5`, `pinned:=true`, `tags:='["a","b"]'`). `--data` aceita um objeto JSON completo, `@file` ou `-` para stdin.
- **Saída:** a resposta é impressa como JSON. Um erro do servidor imprime uma linha com o seu código em stderr e encerra com o código 1; um erro no próprio comando (uma operação desconhecida, um campo malformado) encerra com o código 2.

Os campos de cada operação estão listados na [referência de operações](https://github.com/ClickHouse/loom/blob/main/docs/reference/operations.md).

Em Python, `agent.call("<op>", {...})` alcança qualquer operação, e cada operação também é um método de `agent.client`. As rotas fora do registro estão em `agent.account` e `agent.deployment`.

<a id="dashboard-view--command-and-sdk-call"></a>

## Visão do painel → comando e chamada do SDK

| Painel                                  | `loom call`                                                                                                                    | SDK (`agent = loom.Agent(org, namespace, name)`)                                                                      |
| --------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------- |
| Memórias: buscar, navegar, editar       | `memory.search`, `memory.list`, `memory.set`, `memory.update_by_id`, `memory.delete`                                           | `agent.memory.search(...)`, `.list(...)`, `.set(...)`                                                                 |
| Conhecimento: enviar, ler, baixar       | `knowledge.add`, `knowledge.list`, `knowledge.search`, `/v1/knowledge.download?org=…&namespace=…&source_uri=…`                 | `agent.knowledge.add(...)`, `.list()`, `.download(source_uri)`                                                        |
| Namespaces: criar, configurações        | `namespace.register`, `namespace.get`, `namespace.list_mine`, `policy.set`, `blacklist.add`                                    | `agent.namespaces.*`, `agent.policy.*`, `agent.blacklist.*`                                                           |
| Entrar em um namespace                  | `namespace.browse`, `namespace.join`, `namespace.leave`                                                                        | `agent.client.namespace.join(...)`                                                                                    |
| Aprovar solicitações de entrada         | `grants.pending_for_me`, depois `grants.approve namespace=… subject=… decision=approve` (ou `reject`)                          | `agent.client.grants.pending_for_me()`, `agent.client.grants.approve(...)`                                            |
| Membros e concessões                    | `grants.list`, `grants.create`, `grants.revoke`                                                                                | `agent.grants.*`                                                                                                      |
| Fila de revisão (conflitos)             | `conflicts.list`, `conflicts.resolve`, `learnings.review`                                                                      | `agent.conflicts.list()`, `.resolve(...)`                                                                             |
| Atividade, log de auditoria             | `profile.activity`, `audit.list`, `agents.connected`, `mentor.brief`, `memory.exposure_summary`, `memory.owner_report`         | `agent.audit.list(...)`, `agent.profile.*`                                                                            |
| Custo e orçamentos                      | `cost.summary`, `cost.budget_status`                                                                                           | `agent.cost.summary(...)`                                                                                             |
| Execuções                               | `run.list`, `run.get`                                                                                                          | `agent.run.*`                                                                                                         |
| Índice de código                        | `code.index_status`, `code.repo.add`, …                                                                                        | `agent.code.*`                                                                                                        |
| Apagamento, esquecimento                | `erasure.request`, `erasure.status`, `erasure.list`, `erasure.export`, `memory.clear`                                          | `agent.erasure.*`                                                                                                     |
| Tarefas e caixa de entrada              | `task.create`, `task.pull`, `task.updates`, `inbox.*`                                                                          | `agent.tasks.*`                                                                                                       |
| Fontes do tracker                       | `tracker.source.add`, `tracker.source.list`, `tracker.item`, …                                                                 | `agent.tracker.*`                                                                                                     |
| Grafo (entidades e fatos)               | `facts.traverse`, `facts.*`                                                                                                    | `agent.facts.*`                                                                                                       |
| Conversas e resumos de sessão           | `conversation.*`, `session.brief_*`                                                                                            | `agent.conversations.*`, `agent.briefs.*`                                                                             |
| Configurações: tokens                   | `/v1/me/service_accounts`, `/v1/me/service_accounts.create`, `/v1/me/service_accounts.revoke`, `loom token create`             | `agent.account.tokens()`, `.create_token(...)`, `.revoke_token(name)`                                                 |
| Namespace padrão                        | `/v1/me/set-default-namespace namespace=…`                                                                                     | `agent.account.set_default_namespace(...)`                                                                            |
| Resumo de compartilhamento              | `/v1/me/sharing-summary`                                                                                                       | `agent.account.sharing_summary()`                                                                                     |
| Admin: integridade                      | `/v1/health`, `ops.health`                                                                                                     | `agent.deployment.health()`                                                                                           |
| Admin: workers em segundo plano         | `/v1/admin/workers.status`                                                                                                     | `agent.deployment.workers_status()`                                                                                   |
| Admin: modelos e provedores             | `/v1/admin/models`, `/v1/admin/models/validate`, `/v1/admin/models` (POST), `/v1/admin/models/reset`, `/v1/admin/models/retry` | `agent.deployment.models()`, `.validate_models(...)`, `.save_models(...)`, `.reset_models(...)`, `.retry_models(...)` |
| Admin: usuários, liberar uma reivindicação | `admin.users`, `/v1/admin/claim_release`                                                                                    | `agent.client.admin.users()`, `agent.deployment.release_claim(...)`                                                   |

Valem os mesmos papéis do painel. Aprovar uma entrada exige permissão de admin nesse namespace. As visões de admin exigem um admin da organização ou da plataforma.
