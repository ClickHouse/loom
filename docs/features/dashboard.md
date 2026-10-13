# The dashboard

The dashboard is a React single-page app the Loom server serves at `/me`. It calls the same operations agents call,
with the signed-in person's token, so it shows what that person may read and nothing more. It is where people decide
what Loom leaves to them: conflicts, proposals, triaged tasks, grant requests and policy modes.

![The Memories view of the dashboard](../images/dashboard-memories.png)

## Serving it

The server serves the built bundle from `frontend/dist/`. Without a build, every `/me`
path answers **503** with `Dashboard not built. Run npm --prefix frontend run build or
use the published Docker image.`

Opening the server origin (for example, `http://localhost:7777`) redirects to `/me`, so
the base URL and dashboard URL lead to the same interface.

| How you run Loom                | What to do                                                                                     |
| ------------------------------- | ---------------------------------------------------------------------------------------------- |
| The published Docker image      | Nothing: the image's frontend stage runs `npm ci` and `npm run build`.                         |
| From a checkout (`make server`) | `make frontend-build` (or `npm --prefix frontend run build`) once, and after frontend changes. |
| Working on the frontend         | `make frontend-dev`: a Vite dev server on `:5173` that proxies `/v1` and `/mcp` to `:7777`.    |

`LOOM_DASHBOARD_REDIRECT_URL`, when set on a deployment that requires auth, makes `GET /me`
redirect (302) to another entry point instead, for a Loom that sits behind an
aggregating front end.

## Signing in

Sign-in depends on the deployment's identity mode:

| Deployment                                                                    | What the person sees                                                                                                         |
| ----------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------- |
| Auth off (`LOOM_AUTH_REQUIRED=false` or `LOOM_IDENTITY_PROVIDER=single-user`) | No sign-in. The page serves the one namespace `LOOM_DEFAULT_NAMESPACE` names (`local` by default). A laptop mode only.       |
| Auth on, password accounts                                                    | **Sign in** with an email or namespace and a password; the server mints the token and the page stores it without showing it. |
| Auth on, `LOOM_SELF_SIGNUP=open`                                              | A **Sign up** tab creates an account on a fresh namespace.                                                                   |
| A token minted with `loom mint-token`                                         | Open `/me#token=<jwt>`. The page stores the token and scrubs it from the URL. A `?token=` query is ignored and removed.      |
| Behind a reverse proxy that adds `Authorization`                              | The page sends no token of its own; the proxy's header authenticates every call.                                             |

`loom init --agent NAME` signs an agent up on its own and prints a one-time code a person
enters at `/me` to claim the namespace. The token sits in the browser's local storage;
an expired or rejected token sends the person back to the sign-in screen.

## The pages

The left rail groups pages into Memory, Activity and Sources. Every page has a URL under `/me/` that can be shared.

| Page                         | URL                 | What it is for                                                                                                                                                                                                                                                                                                                                     |
| ---------------------------- | ------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Profile**                  | `/me/profile`       | The always-loaded context every AI client receives (`profile.*`), which clients read it and how (`profile.activity`).                                                                                                                                                                                                                              |
| **Memories**                 | `/me/memories`      | Browse and search the namespace's memories, open one to see its versions, provenance and verification, add or edit, and restore a deleted one within its recovery window. Also shows the namespace audit and the mentor's hints.                                                                                                                   |
| **Recall** (tab of Memories) | `/me/recall`        | "Why did this rank here": the rows the Memories list shows for a query with no filters set, in its order (keyword matches newest first, then `memory.search` hits with their ranking terms), and read past searches with every key and score they returned (`memory.search_trace_log`).                                                            |
| **Library**                  | `/me/library`       | Knowledge documents: add, preview, download, edit metadata, delete. See [knowledge](knowledge.md).                                                                                                                                                                                                                                                 |
| **Conversations**            | `/me/conversations` | Stored conversation threads, replayed turn by turn, and the saved session briefs. See [conversations and sessions](conversations-and-sessions.md).                                                                                                                                                                                                 |
| **Runs**                     | `/me/runs`          | The run ledger: goal, how it ended, what it relied on and wrote, and in the replay what it recalled, observed and cost. See [runs](runs.md).                                                                                                                                                                                                       |
| **Tasks**                    | `/me/tasks`         | The task inbox board. Tiles count what needs a person (triage, blocked on a person, failed, leases about to end); rows group as Triage, To do, In progress, Waiting, Closed, as a list or a board, with Accept / Won't do inline; the panel's buttons or a drag onto another group move a task. See [tasks and decisions](tasks-and-decisions.md). |
| **Repositories**             | `/me/repositories`  | Indexed code repositories, each with Health, Explore, PR packs and Access tabs, and the org's trackers. See [code index](code-index/overview.md) and [tracker](tracker/overview.md).                                                                                                                                                               |
| **Settings**                 | `/me/settings`      | Two tabs: **Tokens** (mint and list tokens and service accounts, client setup snippets) and **Memory** (`?tab=memory`: patterns never to save, and seeding memory from a document).                                                                                                                                                                |

Two views are reached from elsewhere rather than the rail: **Spaces** (`/me/spaces`,
public namespaces to browse and join, opened from the namespace picker) and **Graph**
(`/me/graph`, the entity graph explorer, opened from an entity; see
[entity graph](entity-graph.md)).

### The admin drawer

Admins of the current namespace get a drawer with three groups. A viewer never sees an
entry that would answer 403.

| Group   | Page             | URL                      | What it is for                                                                                                                                                                                                                                                                  |
| ------- | ---------------- | ------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Review  | **Review queue** | `/me/admin/review_queue` | Conflicts (pairs of memories that disagree), learnings proposed for promotion, and exceptions (the allowlist of pairs that may coexist). Shown in the drawer only while something waits; the URL always resolves. See [conflicts and refusal](memory/conflicts-and-refusal.md). |
| Review  | **Guardrails**   | `/me/admin/guardrails`   | Blacklist patterns: content Loom should never store.                                                                                                                                                                                                                            |
| Review  | **Policies**     | `/me/admin/policies`     | History policy rules, a dry run of a hypothetical call, and the decision ledger.                                                                                                                                                                                                |
| Observe | **Health**       | `/me/admin/health`       | Worker status and how agents in the namespace spend their calls.                                                                                                                                                                                                                |
| Observe | **Activity**     | `/me/admin/activity`     | The audit feed, one readable line per event.                                                                                                                                                                                                                                    |
| Observe | **Flow**         | `/me/flow`               | Read-only: where memories flow — writes and recalls, cross-namespace reach, producer and consumer agents, memberships.                                                                                                                                                          |
| Observe | **Usage**        | `/me/admin/usage`        | LLM and embedding cost by day, operation, agent or user, and the namespace's daily cap.                                                                                                                                                                                         |
| Observe | **Agents**       | `/me/admin/agents`       | Which clients connected in a window (`agents.connected`); confirms a new MCP connection works.                                                                                                                                                                                  |
| Access  | **Users**        | `/me/admin/users`        | Org and platform admins only: every subject, their namespaces and roles, last activity, and releasing a namespace whose owner lost their password.                                                                                                                              |

Badges draw attention without a rail entry: the Review queue appears with its conflict
count, Usage warns when the namespace is over its 24-hour cap, Agents counts clients new
in the last 24 hours. The header carries a task bell (triage and blocked-on-person items)
and a pending-approvals bell (grant requests the person can decide).

## Operations the dashboard uses

The dashboard calls public operations (`memory.*`, `conflicts.*`, `task.*`, `namespace.*`, …) with the person's own
role; an integration with the same grants can do everything it does. `ui.vitals` and the `/v1/me/*`, `/v1/admin/*`
routes serve the dashboard; they are not an integration surface.

- `ui.vitals` (hidden from MCP): one row per page view with
  the route, largest contentful paint, long tasks, DOM size, `/v1` call count and build,
  posted when the person leaves the view. The operation ledger row is the record;
  `loom deploy-delta` reads it per route before and after a deploy.
- `/v1/me/*` routes (the namespace default, sharing summary, service accounts) and
  `/v1/admin/*` routes (worker status) are HTTP endpoints outside the operation registry.

## Limits

- `/me` answers 503 until `frontend/dist/index.html` exists.
- In auth-off mode there is no namespace switcher: the page serves
  `LOOM_DEFAULT_NAMESPACE` only, and never persists a different pick.
- Choosing a namespace in the picker also saves it server-side as the person's default,
  so MCP clients pick up the same namespace on their next call.
- Admin badges poll only for a confirmed admin of a memory namespace; a code or tracker
  namespace has no conflicts or budget to count.

## Related

- [Quickstart](../getting-started/quickstart.md) — boot the stack and open `/me`.
- [Tokens and grants](../guides/tokens-and-grants.md) — minting the token a sign-in uses.
- [Namespaces and sharing](namespaces-and-sharing.md) — roles, joining, the mentor.
- [Security architecture](../architecture/security.md) — identity modes and the proxy
  deployment.
