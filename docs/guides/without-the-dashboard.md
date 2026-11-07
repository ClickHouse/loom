# Use Loom without the dashboard

Everything the `/me` dashboard does can be done from a terminal, a script or
an agent's shell. Sign in once with `loom login`, then use `loom call` or the
Python SDK. This page maps each dashboard view to its command and SDK call; every view not listed is plain operations too (`loom call --list`).

## Sign in

```console
$ loom login --server https://loom.example.com   # approve on any browser: password or SSO
$ loom whoami
$ loom logout
```

`loom login` stores a session that refreshes itself. CI and services have no
one to approve the code, so they sign in as a bot (below), or with a scoped
token in `LOOM_API_KEY`. See [tokens and grants](tokens-and-grants.md#sign-in-from-a-terminal-or-a-notebook).
With only the SDK installed, run the same commands as `python -m loom login`
and so on.

## Bots and CI jobs

A bot needs no `loom login`: register it once (`loom bot create`, or a platform admin's
`loom call /v1/admin/bots.create`), then run it with `LOOM_CLIENT_ID` and either its
private key (`LOOM_CLIENT_KEY_FILE`, `LOOM_CLIENT_KEY`, or an AWS Secrets Manager secret in
`LOOM_CLIENT_KEY_SECRET`) or, in a CI job, `LOOM_WORKLOAD_AUDIENCE`.
The SDK gets and renews its token itself: an hour, less when the bot's maximum is shorter. See
[single sign-on](../operations/sso.md#bots-and-ci).

## `loom call`: the whole API

```console
$ loom call --list                          # every operation and the role it needs
$ loom call memory.search query="deploy window" top_k:=5
$ loom call grants.approve namespace=support subject=bob@example.com decision=approve
$ loom call /v1/me/service_accounts         # a route outside the operation registry
```

Your own tokens for CI and agents have a command of their own:

```console
$ loom token create --name ci --namespace-role support=writer --namespace docs --ttl-days 0
$ loom token list
$ loom token revoke --name ci
```

- **An operation name** is POSTed to `/v1/<op>`. `org` and `namespace` come
  from your token; `--org` and `--namespace` override them.
- **A path** (it starts with `/`) is sent as given: GET without a body, POST
  with one.
- **Fields** are `key=value` for text, sent exactly as typed, and
  `key:=json` for any other value (`top_k:=5`, `pinned:=true`,
  `tags:='["a","b"]'`). `--data` takes a whole JSON object, `@file` or `-`
  for stdin.
- **Output:** the answer prints as JSON. An error from the server prints one
  line with its code on stderr and exits 1; a mistake in the command itself
  (an unknown operation, a malformed field) exits 2.

The fields of each operation are listed in the
[operations reference](../reference/operations.md).

In Python, `agent.call("<op>", {...})` reaches any operation, and every
operation is also a method on `agent.client`. The routes outside the registry
are on `agent.account` and `agent.deployment`.

## Dashboard view → command and SDK call

| Dashboard                         | `loom call`                                                                                                                    | SDK (`agent = loom.Agent(org, namespace, name)`)                                                                      |
| --------------------------------- | ------------------------------------------------------------------------------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------- |
| Memories: search, browse, edit    | `memory.search`, `memory.list`, `memory.set`, `memory.update_by_id`, `memory.delete`                                           | `agent.memory.search(...)`, `.list(...)`, `.set(...)`                                                                 |
| Knowledge: upload, read, download | `knowledge.add`, `knowledge.list`, `knowledge.search`, `/v1/knowledge.download?org=…&namespace=…&source_uri=…`                 | `agent.knowledge.add(...)`, `.list()`, `.download(source_uri)`                                                        |
| Namespaces: create, settings      | `namespace.register`, `namespace.get`, `namespace.list_mine`, `policy.set`, `blacklist.add`                                    | `agent.namespaces.*`, `agent.policy.*`, `agent.blacklist.*`                                                           |
| Join a namespace                  | `namespace.browse`, `namespace.join`, `namespace.leave`                                                                        | `agent.client.namespace.join(...)`                                                                                    |
| Approve join requests             | `grants.pending_for_me`, then `grants.approve namespace=… subject=… decision=approve` (or `reject`)                            | `agent.client.grants.pending_for_me()`, `agent.client.grants.approve(...)`                                            |
| Members and grants                | `grants.list`, `grants.create`, `grants.revoke`                                                                                | `agent.grants.*`                                                                                                      |
| Review queue (conflicts)          | `conflicts.list`, `conflicts.resolve`, `learnings.review`                                                                      | `agent.conflicts.list()`, `.resolve(...)`                                                                             |
| Activity, audit log               | `profile.activity`, `audit.list`, `agents.connected`, `mentor.brief`, `memory.exposure_summary`, `memory.owner_report`         | `agent.audit.list(...)`, `agent.profile.*`                                                                            |
| Cost and budgets                  | `cost.summary`, `cost.budget_status`                                                                                           | `agent.cost.summary(...)`                                                                                             |
| Runs                              | `run.list`, `run.get`                                                                                                          | `agent.run.*`                                                                                                         |
| Code index                        | `code.index_status`, `code.repo.add`, …                                                                                        | `agent.code.*`                                                                                                        |
| Erasure, forgetting               | `erasure.request`, `erasure.status`, `erasure.list`, `erasure.export`, `memory.clear`                                          | `agent.erasure.*`                                                                                                     |
| Tasks and inbox                   | `task.create`, `task.pull`, `task.updates`, `inbox.*`                                                                          | `agent.tasks.*`                                                                                                       |
| Tracker sources                   | `tracker.source.add`, `tracker.source.list`, `tracker.item`, …                                                                 | `agent.tracker.*`                                                                                                     |
| Graph (entities and facts)        | `facts.traverse`, `facts.*`                                                                                                    | `agent.facts.*`                                                                                                       |
| Conversations and session briefs  | `conversation.*`, `session.brief_*`                                                                                            | `agent.conversations.*`, `agent.briefs.*`                                                                             |
| Settings: tokens                  | `/v1/me/service_accounts`, `/v1/me/service_accounts.create`, `/v1/me/service_accounts.revoke`, `loom token create`             | `agent.account.tokens()`, `.create_token(...)`, `.revoke_token(name)`                                                 |
| Default namespace                 | `/v1/me/set-default-namespace namespace=…`                                                                                     | `agent.account.set_default_namespace(...)`                                                                            |
| Sharing summary                   | `/v1/me/sharing-summary`                                                                                                       | `agent.account.sharing_summary()`                                                                                     |
| Admin: health                     | `/v1/health`, `ops.health`                                                                                                     | `agent.deployment.health()`                                                                                           |
| Admin: background workers         | `/v1/admin/workers.status`                                                                                                     | `agent.deployment.workers_status()`                                                                                   |
| Admin: models and providers       | `/v1/admin/models`, `/v1/admin/models/validate`, `/v1/admin/models` (POST), `/v1/admin/models/reset`, `/v1/admin/models/retry` | `agent.deployment.models()`, `.validate_models(...)`, `.save_models(...)`, `.reset_models(...)`, `.retry_models(...)` |
| Admin: users, release a claim     | `admin.users`, `/v1/admin/claim_release`                                                                                       | `agent.client.admin.users()`, `agent.deployment.release_claim(...)`                                                   |

The same roles apply as in the dashboard. Approving a join needs admin on
that namespace. The admin views need an org or platform admin.
