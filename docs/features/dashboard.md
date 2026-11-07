# The dashboard

The dashboard is a React single-page app the Loom server serves at its root URL. It calls the same operations agents call,
with the signed-in person's token, so it shows what that person may read and nothing more. It is where people decide
what Loom leaves to them: conflicts, proposals, triaged tasks, grant requests and policy modes.

![The Memories view of the dashboard](../images/dashboard-memories.png)

## Serving it

The server serves the built bundle from `frontend/dist/`. Without a build, every dashboard
page answers **503** with `Dashboard not built. Run npm --prefix frontend run build or
use the published Docker image.`

The server origin is the dashboard: open `http://localhost:7777/`, and a page's URL is
the origin plus its path (`<server>/settings`). The API (`/v1/*`), MCP (`/mcp`), OAuth
(`/oauth/*`, `/.well-known/*`) and the bundle's own files (`/assets/*`) keep their paths;
no page uses them.

| How you run Loom                | What to do                                                                                     |
| ------------------------------- | ---------------------------------------------------------------------------------------------- |
| The published Docker image      | Nothing: the image's frontend stage runs `npm ci` and `npm run build`.                         |
| From a checkout (`make server`) | `make frontend-build` (or `npm --prefix frontend run build`) once, and after frontend changes. |
| Working on the frontend         | `make frontend-dev`: a Vite dev server on `:5173` that proxies `/v1` and `/mcp` to `:7777`.    |

`LOOM_DASHBOARD_REDIRECT_URL`, when set on a deployment that requires auth, makes a dashboard page load
redirect (302) to another entry point instead, for a Loom that sits behind an
aggregating front end. `/me`, where a sign-in lands with its token in the URL
fragment, is the exception: it always serves the page, so the token never
follows the redirect to another origin.

## Signing in

Sign-in depends on the deployment's identity mode:

| Deployment                                                                    | What the person sees                                                                                                                                                                                                                                       |
| ----------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Auth off (`LOOM_AUTH_REQUIRED=false` or `LOOM_IDENTITY_PROVIDER=single-user`) | No sign-in. The page serves the one namespace `LOOM_DEFAULT_NAMESPACE` names (`local` by default). A laptop mode only.                                                                                                                                     |
| Auth on, password accounts                                                    | **Sign in** with an email or namespace and a password; the server mints the token and the page stores it without showing it.                                                                                                                               |
| Auth on, `LOOM_SELF_SIGNUP=open`                                              | A **Sign up** tab creates an account on a fresh namespace.                                                                                                                                                                                                 |
| Auth on, `LOOM_OAUTH_LOGIN_PROVIDER=oidc`                                     | One **Sign in with SSO** button to the identity provider ([single sign-on](../operations/sso.md)); no password or sign-up form.                                                                                                                            |
| A token minted with `loom mint-token`                                         | Open `<server>/me#token=<jwt>`, the sign-in landing page, which never redirects. The page scrubs the token from the URL and asks to confirm the account it names, unless it names the account already signed in. A `?token=` query is ignored and removed. |
| Behind a reverse proxy that adds `Authorization`                              | The page sends no token of its own; the proxy's header authenticates every call.                                                                                                                                                                           |

A `#token=` link signs this browser in as whoever the token names, so the page stores one
without asking only when it answers the SSO sign-in this tab started from the sign-in screen
within the last ten minutes, or when it names the same subject and org as the token already
held. The sign-in screen sends a nonce it keeps in the tab to `/oauth/sso?n=<nonce>`, and
the server hands it back as `/me?n=<nonce>#token=<jwt>`; a token without that nonce, such as
one from an SSO sign-in started by typing `/oauth/sso`, is not the tab's own. Any other
link's token is first asked about with whoami: one the server refuses is dropped without
asking, and one it accepts waits on a screen that names the account the server says it signs
in as, and the account the browser is signed in as: **Sign in as …** stores it, and
**Cancel** drops it and keeps the session the browser had. Until then the token is held only
in the page's memory. A same-account token is stored only once the server accepts it, so a
refused one leaves the held token as it was. `/me?sign_in=sso` opens a sign-in screen that
starts SSO by itself with a nonce of that tab's own when a Loom page sent the browser there
(the server reads the load's `Sec-Fetch-Site`, or its `Referer` from a browser that sends
none); from another site, or typed, it shows the **Sign in with SSO** button. **Sign in in a
new tab** on the session-ended card opens it, and so does **Start again** when the server
could not finish an SSO sign-in. A `#token=` link behind a proxy that adds `Authorization`,
or with auth off, is dropped without asking.

`loom init --agent NAME` signs an agent up on its own and prints a one-time code a person
enters in the dashboard to claim the namespace. The token sits in the browser's local storage;
a stored token the server refuses (expired or revoked) ends the session on the next call, from
any page, and so does another tab dropping the token; a sign-out in another tab reloads this
one to the sign-in screen instead. Mid-session a card among the toasts says so, over any open
dialog (a pointer reaches it at once; a keyboard user closes the dialog to reach it), and the
page stays as it is. **Sign in in a new tab** signs in there, and this tab uses the new token:
a token another tab stores for someone else reloads the page as soon as the tab hears of it,
and no call of this page goes out under it; for the same person, the card goes away once the
server accepts the token. **Reload** signs in in this tab instead. While the page loads, a
refused token shows the sign-in screen saying the session ended. When that token was a sign-in
session's, with SSO on, the screen starts the sign-in again by itself (once in five minutes per
tab), so a person still signed in at the identity provider returns to the page they were on. A
minted token, and a refused `#token=` link, wait for a click instead, since either may be the
way in while the identity provider is down.
**Sign out** forgets the token here, and ends the server-side session of a sign-in session's token (the
dashboard's SSO token, or a `loom login` access token opened with `#token=`). A minted
token opened with `#token=` is only forgotten here: it may be the one a CLI or CI job uses.

## The pages

The left rail groups pages into Memory, Activity and Sources. Every page has its own URL that can be shared.

| Page                         | URL              | What it is for                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| ---------------------------- | ---------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Profile**                  | `/profile`       | The always-loaded context every AI client receives (`profile.*`), which clients read it and how (`profile.activity`).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     |
| **Memories**                 | `/memories`      | Browse and search the namespace's memories, open its [guide](namespaces-and-sharing.md#a-namespaces-guide) from the header, open one to see its versions, provenance and verification, add or edit, and restore a deleted one within its recovery window. Also shows the namespace audit and the mentor's hints.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| **Recall** (tab of Memories) | `/recall`        | "Why did this rank here": the rows the Memories list shows for a query with no filters set, in its order (keyword matches newest first, then `memory.search` hits with their ranking terms), and read past searches with every key and score they returned (`memory.search_trace_log`).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   |
| **Library**                  | `/library`       | Knowledge documents: add (a file is uploaded for the server's extractor, badged beforehand with the format `knowledge.formats` says it is read as, and its ingest followed to the revision it publishes; or pasted text), preview, download, edit metadata, delete, and find a passage inside the documents (**Find a passage**, `knowledge.search`; each hit is cited by its revision and place, in lines, a key, pages or slides, or rows, and opens its document). A select above the list shows the namespace picked at the top or one of the [collections](../architecture/sharing.md) the person can read (`collection.list`), as a tree with each one's description; `?collection=` names it. A header namespace that is a collection shows as that collection; on a code or tracker namespace, or none, the select lists only collections. Its menu manages the members (`grants.*`) and, for its admins, deletes it, which erases what it holds (`collection.delete`); **New collection** creates one, inside the one shown when the person writes there. The header's **New** adds to the header's namespace, so while the Library shows a collection, or a namespace that holds no documents, it offers no **Document**: the Library's own **Add document** adds where the Library shows. The select finds a collection by its name or description as you type. See [knowledge](knowledge.md). |
| **Conversations**            | `/conversations` | Stored conversation threads, replayed turn by turn, and the saved session briefs. See [conversations and sessions](conversations-and-sessions.md).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                        |
| **Assistant**                | `/assistant`     | Opens on what the namespace holds and who appears in it (`namespace.inventory`, read on load); each user and agent there is a button that inspects what it remembers ([`memory.inspect`](namespaces-and-sharing.md#inspecting-one-user-agent-or-session)), and the id field suggests them while taking any other id or a session id. Inspect documents reads what the namespace's documents are ([`knowledge.inspect`](knowledge.md#inspecting-a-namespaces-documents)); Open the graph walks from an entity, or opens on the graph's own starting points; Draft improvements lists the drafts [`namespace.tune`](namespaces-and-sharing.md#drafting-changes-to-how-memories-are-made) makes, a chunking or extractor-route draft with its document sample and the deployment write that would apply it. Every read but the overview runs on a click; nothing is applied from the page.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   |
| **Runs**                     | `/runs`          | The run ledger: goal, how it ended, what it relied on and wrote, and in the replay what it recalled, observed and cost. See [runs](runs.md).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              |
| **Tasks**                    | `/tasks`         | The task inbox board. Tiles count what needs a person (triage, blocked on a person, failed, leases about to end); rows group as Triage, To do, In progress, Waiting, Closed, as a list or a board, with Accept / Won't do inline; the panel's buttons or a drag onto another group move a task. See [tasks and decisions](tasks-and-decisions.md).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                        |
| **Repositories**             | `/repositories`  | Indexed code repositories, each with Health, Explore, PR packs and Access tabs, and the org's trackers. An opened repository lists its documents and their sources, each with its last sync; a caller with the role the [sources table](knowledge.md#document-sources) names adds, checks, syncs, rotates and retires a source there. See [code index](code-index/overview.md) and [tracker](tracker/overview.md).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                        |
| **Settings**                 | `/settings`      | Two tabs: **Tokens** (one form for every token: what it reads, where it saves, its expiry; the setup for each MCP client or a script; the list, with what each token reads) and **Memory** (`?tab=memory`: patterns never to save, and seeding memory from a document).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   |

Two views are reached from elsewhere rather than the rail: **Spaces** (`/spaces`,
public namespaces to browse and join, with each one's
[guide](namespaces-and-sharing.md#a-namespaces-guide) summary, and the guide itself for
a namespace you belong to, opened from the namespace picker) and **Graph**
(`/graph`, the entity graph explorer, opened from an entity; see
[entity graph](entity-graph.md)).

### A page's address and its namespace

A page's URL names the namespace it shows, first in its query
(`/memories?namespace=team-notes&memory=api%2Frate-limit`), so a reload or a shared link
shows the same namespace.

- **A link's namespace** opens in that tab when the account can read it: a namespace its
  token claims, or one it holds an active grant on. It is never saved as the picked
  namespace.
- **A namespace the account cannot read**, a pending request included, opens a page that
  says so, with the namespace picker in the header and a way to continue in the default
  namespace. Nothing of the link's namespace is read.
- **A URL without `?namespace=`** opens the default namespace: the saved pick while the
  account can still reach it, else a namespace it administers (its personal namespace
  first, then its oldest admin grant), else its first claimed memory namespace. A token
  that reaches only code indexes and tracker records opens the first of those on the
  pages that need no memory namespace; a memory page then opens on none and says so.
- **When the membership read fails**, a link to the saved pick still opens, since the server
  answers its reads, and any other link opens a page that says its access could not be
  checked, with Retry and a way to continue in the default namespace. A URL without
  `?namespace=` then opens the saved pick only when the token claims it, else its first
  claimed namespace.
- **Back and Forward** never switch namespace: an entry made in another namespace opens
  nothing it names, and the address names the page's namespace. The one exception is Back
  from a tracker's Patterns board, which returns to the namespace its Open left.
- **On the bare root**, `?view=` names the page by view id or path (`/?view=memories`,
  `/?view=admin/health`) and `?memory=<key>` opens that memory on Memories.
- **A single sign-on** started on a page returns to it, within ten minutes.
- **The namespace picker** shows each namespace's description, and folds code indexes,
  tracker records and every three or more memory namespaces whose names share a prefix
  into groups of their own. A uuid in a name counts as one part, so names built as
  `<kind>-<owner uuid>-<id>` fold per owner.

### The admin drawer

Admins of the current namespace get a drawer with the Review and Observe groups; org and
platform admins also get Access, and platform admins Deployment. A viewer never sees an
entry that would answer 403.

| Group   | Page             | URL                   | What it is for                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| ------- | ---------------- | --------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Review  | **Review queue** | `/admin/review_queue` | Conflicts (pairs of memories that disagree, each with a suggested decision on request, except a sensitive one), learnings proposed for promotion, memories Loom retired on its own (Auto-resolved, each with an Undo), exceptions (the allowlist of pairs that may coexist), proposals (facts on trial, accepted or rejected), and, for admins, duplicate memories and documents with the namespace-wide repairs. Shown in the drawer only while a conflict waits or Loom retired a memory in the last 30 days; the URL always resolves. See [conflicts and refusal](memory/conflicts-and-refusal.md). |
| Review  | **Guardrails**   | `/admin/guardrails`   | Blacklist patterns: content Loom should never store.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   |
| Review  | **Policies**     | `/admin/policies`     | History policy rules, a dry run of a hypothetical call, and the decision ledger.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| Observe | **Health**       | `/admin/health`       | Worker status and how agents in the namespace spend their calls.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| Observe | **Activity**     | `/admin/activity`     | The audit feed, one readable line per event; the Erasures tab lists erasures with each one's details and, by role, erases or exports one namespace or one user; the Exposure tab shows which readers read the namespace's memories and one user's owner report.                                                                                                                                                                                                                                                                                                                                        |
| Observe | **Flow**         | `/flow`               | Read-only: where memories flow — writes and recalls, cross-namespace reach, producer and consumer agents, memberships.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 |
| Observe | **Usage**        | `/admin/usage`        | LLM and embedding cost by day, operation, agent or user, and the namespace's daily cap.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                |
| Observe | **Agents**       | `/admin/agents`       | Which clients connected in a window (`agents.connected`); confirms a new MCP connection works. A namespace admin can forget every memory one agent holds there.                                                                                                                                                                                                                                                                                                                                                                                                                                        |
| Access  | **Users**        | `/admin/users`        | Org and platform admins only: every subject, their namespaces and roles, last activity, and releasing a namespace whose owner lost their password.                                                                                                                                                                                                                                                                                                                                                                                                                                                     |

Badges draw attention without a rail entry: the Review queue appears with its conflict
count, or with no count when there is no open conflict but Loom retired a memory on its
own in the last 30 days (the Auto-resolved tab), Usage warns when the namespace is over
its 24-hour cap, Agents counts clients new in the last 24 hours. The header carries a task
bell (triage and blocked-on-person items) and a pending-approvals bell (grant requests the
person can decide).

## Operations the dashboard uses

The dashboard calls public operations (`memory.*`, `conflicts.*`, `task.*`, `namespace.*`, …) with the person's own
role; an integration with the same grants can do everything it does. `ui.vitals`, the caller API (`/v1/me/*`) and the `/v1/admin/*`
routes serve the dashboard; they are not an integration surface.

- `ui.vitals` (hidden from MCP): one row per page view with
  the route, largest contentful paint, long tasks, DOM size, `/v1` call count and build,
  posted when the person leaves the view. The operation ledger row is the record;
  `loom deploy-delta` reads it per route before and after a deploy.
- The caller API's `/v1/me/*` routes (the namespace default, sharing summary, service accounts) and
  `/v1/admin/*` routes (worker status) are HTTP endpoints outside the operation registry.

## Limits

- Dashboard pages answer 503 until `frontend/dist/index.html` exists.
- In auth-off mode there is no namespace switcher: the page serves
  `LOOM_DEFAULT_NAMESPACE` only, never persists a different pick, and ignores a URL's
  `?namespace=`.
- Choosing a namespace in the picker also saves it server-side as the person's default,
  so MCP clients pick up the same namespace on their next call. A link's namespace is
  never saved.
- A token whose namespaces claim is `*` reads namespaces the picker does not list; a link
  to one of those opens the page that says the account has no access.
- The rail's links carry no namespace: one opened in a new tab shows the saved pick.
- Admin badges poll only for a confirmed admin of a memory namespace; a code or tracker
  namespace has no conflicts or budget to count.

## Related

- [Quickstart](../getting-started/quickstart.md) — boot the stack and open the dashboard.
- [Tokens and grants](../guides/tokens-and-grants.md) — minting the token a sign-in uses.
- [Namespaces and sharing](namespaces-and-sharing.md) — roles, joining, the mentor.
- [Security architecture](../architecture/security.md) — identity modes and the proxy
  deployment.
