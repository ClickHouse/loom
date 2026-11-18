<!-- i18n: source=docs/guides/without-the-dashboard.md blob=d950ac1fe252e9d585139b1da802185c47ab7aa9 model=claude-sonnet-5-5 -->
> Esta página es una traducción automática de la [página en inglés](https://github.com/ClickHouse/loom/blob/main/docs/guides/without-the-dashboard.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/guides/without-the-dashboard.md), which is authoritative.

<a id="use-loom-without-the-dashboard"></a>

# Usar Loom sin el dashboard

Todo lo que hace el dashboard `/me` se puede hacer desde una terminal, un script o
el shell de un agente. Inicia sesión una vez con `loom login` y luego usa `loom call` o el
SDK de Python. Esta página asocia cada vista del dashboard con su comando y su llamada del SDK; cada vista que no aparece aquí también se reduce a operaciones simples (`loom call --list`).

<a id="sign-in"></a>

## Iniciar sesión

```console
$ loom login --server https://loom.example.com   # approve on any browser: password or SSO
$ loom whoami
$ loom logout
```

`loom login` guarda una sesión que se renueva sola. Detrás de un router de
[celdas](../architecture/cells.md), `loom login --org <org>` inicia sesión en la celda propia de la organización:
consulta `GET /cells/lookup` del router, conserva el servidor que se le indicó cuando no hay
router o todavía no hay celda, espera un 429 durante un máximo de `--wait` segundos (120 por defecto) y,
en otro caso, se niega sin iniciar un inicio de sesión. La sesión pasa entonces a ser la del host de la celda: apunta
`--server` o `LOOM_BASE_URL` al host que imprime. CI y los servicios no tienen
a nadie que apruebe el código, así que inician sesión como un bot (más abajo) o con un
token con alcance limitado en `LOOM_API_KEY`. Consulta [tokens y permisos](https://github.com/ClickHouse/loom/blob/main/docs/guides/tokens-and-grants.md#sign-in-from-a-terminal-or-a-notebook).
Si solo tienes instalado el SDK, ejecuta los mismos comandos como `python -m loom login`
y así sucesivamente.

<a id="bots-and-ci-jobs"></a>

## Bots y trabajos de CI

Un bot no necesita `loom login`: se registra una sola vez (`loom bot create`, o `loom call /v1/admin/bots.create` por parte de un administrador de la plataforma) y luego se ejecuta con `LOOM_CLIENT_ID` y con su clave privada (`LOOM_CLIENT_KEY_FILE`, `LOOM_CLIENT_KEY` o un secreto de AWS Secrets Manager en `LOOM_CLIENT_KEY_SECRET`) o, en un trabajo de CI, con `LOOM_WORKLOAD_AUDIENCE`.
El SDK obtiene y renueva su propio token: dura una hora, o menos cuando el máximo del bot es más corto. Consulta el [inicio de sesión único](https://github.com/ClickHouse/loom/blob/main/docs/operations/sso.md#bots-and-ci).

<a id="loom-call-the-whole-api"></a>

## `loom call`: toda la API

```console
$ loom call --list                          # every operation and the role it needs
$ loom call memory.search query="deploy window" top_k:=5
$ loom call grants.approve namespace=support subject=bob@example.com decision=approve
$ loom call /v1/me/service_accounts         # a route outside the operation registry
```

Tus propios tokens para CI y agentes tienen un comando específico:

```console
$ loom token create --name ci --namespace-role support=writer --namespace docs --ttl-days 0
$ loom token list
$ loom token revoke --name ci
```

- **Un nombre de operación** se envía con POST a `/v1/<op>`. `org` y `namespace` provienen de tu token; `--org` y `--namespace` los reemplazan (sobrescritura).
- **Una ruta** (empieza con `/`) se envía tal como se indica: GET sin cuerpo, POST con cuerpo.
- **Los campos** son `key=value` para texto, enviado exactamente como se escribe, y `key:=json` para cualquier otro valor (`top_k:=5`, `pinned:=true`, `tags:='["a","b"]'`). `--data` acepta un objeto JSON completo, `@file` o `-` para stdin.
- **Salida:** la respuesta se imprime como JSON. Un error del servidor imprime una línea con su código en stderr y termina con código 1; un error en el propio comando (una operación desconocida, un campo mal formado) termina con código 2.

Los campos de cada operación están listados en la
[referencia de operaciones](https://github.com/ClickHouse/loom/blob/main/docs/reference/operations.md).

En Python, `agent.call("<op>", {...})` llega a cualquier operación, y cada operación es también un método de `agent.client`. Las rutas fuera del registro están en `agent.account` y `agent.deployment`.

<a id="dashboard-view--command-and-sdk-call"></a>

## Vista del dashboard → comando y llamada del SDK

| Dashboard                                      | `loom call`                                                                                                                    | SDK (`agent = loom.Agent(org, namespace, name)`)                                                                      |
| ---------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------- |
| Memorias: buscar, explorar, editar             | `memory.search`, `memory.list`, `memory.set`, `memory.update_by_id`, `memory.delete`                                           | `agent.memory.search(...)`, `.list(...)`, `.set(...)`                                                                 |
| Conocimiento: subir, leer, descargar           | `knowledge.add`, `knowledge.list`, `knowledge.search`, `/v1/knowledge.download?org=…&namespace=…&source_uri=…`                 | `agent.knowledge.add(...)`, `.list()`, `.download(source_uri)`                                                        |
| Espacios de nombres: crear, configuración      | `namespace.register`, `namespace.get`, `namespace.list_mine`, `policy.set`, `blacklist.add`                                    | `agent.namespaces.*`, `agent.policy.*`, `agent.blacklist.*`                                                           |
| Unirse a un espacio de nombres                 | `namespace.browse`, `namespace.join`, `namespace.leave`                                                                        | `agent.client.namespace.join(...)`                                                                                    |
| Aprobar solicitudes de ingreso                 | `grants.pending_for_me`, luego `grants.approve namespace=… subject=… decision=approve` (o `reject`)                            | `agent.client.grants.pending_for_me()`, `agent.client.grants.approve(...)`                                            |
| Miembros y permisos                            | `grants.list`, `grants.create`, `grants.revoke`                                                                                | `agent.grants.*`                                                                                                      |
| Cola de revisión (conflictos)                  | `conflicts.list`, `conflicts.resolve`, `learnings.review`                                                                      | `agent.conflicts.list()`, `.resolve(...)`                                                                             |
| Actividad, registro de auditoría               | `profile.activity`, `audit.list`, `agents.connected`, `mentor.brief`, `memory.exposure_summary`, `memory.owner_report`         | `agent.audit.list(...)`, `agent.profile.*`                                                                            |
| Costos y presupuestos                          | `cost.summary`, `cost.budget_status`                                                                                           | `agent.cost.summary(...)`                                                                                             |
| Ejecuciones                                    | `run.list`, `run.get`                                                                                                          | `agent.run.*`                                                                                                         |
| Índice de código                               | `code.index_status`, `code.repo.add`, …                                                                                        | `agent.code.*`                                                                                                        |
| Borrado, olvido                                | `erasure.request`, `erasure.status`, `erasure.list`, `erasure.export`, `memory.clear`                                          | `agent.erasure.*`                                                                                                     |
| Tareas y bandeja de entrada                    | `task.create`, `task.pull`, `task.updates`, `inbox.*`                                                                          | `agent.tasks.*`                                                                                                       |
| Fuentes del tracker                            | `tracker.source.add`, `tracker.source.list`, `tracker.item`, …                                                                 | `agent.tracker.*`                                                                                                     |
| Grafo (entidades y hechos)                     | `facts.traverse`, `facts.*`                                                                                                    | `agent.facts.*`                                                                                                       |
| Conversaciones y resúmenes de sesión           | `conversation.*`, `session.brief_*`                                                                                            | `agent.conversations.*`, `agent.briefs.*`                                                                             |
| Configuración: tokens                          | `/v1/me/service_accounts`, `/v1/me/service_accounts.create`, `/v1/me/service_accounts.revoke`, `loom token create`             | `agent.account.tokens()`, `.create_token(...)`, `.revoke_token(name)`                                                 |
| Espacio de nombres predeterminado              | `/v1/me/set-default-namespace namespace=…`                                                                                     | `agent.account.set_default_namespace(...)`                                                                            |
| Resumen de uso compartido                      | `/v1/me/sharing-summary`                                                                                                       | `agent.account.sharing_summary()`                                                                                     |
| Administración: estado de salud                | `/v1/health`, `ops.health`                                                                                                     | `agent.deployment.health()`                                                                                           |
| Administración: workers en segundo plano       | `/v1/admin/workers.status`                                                                                                     | `agent.deployment.workers_status()`                                                                                   |
| Administración: modelos y proveedores          | `/v1/admin/models`, `/v1/admin/models/validate`, `/v1/admin/models` (POST), `/v1/admin/models/reset`, `/v1/admin/models/retry` | `agent.deployment.models()`, `.validate_models(...)`, `.save_models(...)`, `.reset_models(...)`, `.retry_models(...)` |
| Administración: usuarios, liberar un claim     | `admin.users`, `/v1/admin/claim_release`                                                                                       | `agent.client.admin.users()`, `agent.deployment.release_claim(...)`                                                   |

Se aplican los mismos roles que en el dashboard. Aprobar un ingreso requiere ser
administrador de ese espacio de nombres. Las vistas de administración requieren
ser administrador de la organización o de la plataforma.
