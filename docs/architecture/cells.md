# Cells

A deployment's memory can outgrow one Postgres primary and one ClickHouse service, and
an org may want its memory in services of its own. A **cell** is one complete Loom
deployment as this documentation describes it: its replicas and workers, one Postgres
unit (a primary with its standbys), one ClickHouse service, one document bucket prefix,
its own public host, and its own signing keys, grants, workers and erasure. A **router**
on the deployment's shared host sends a request to the cell that holds what it
addresses. An org lives in exactly one cell, the one that placed it, and never leaves
it. An org is placed at attach (the orgs the cell held, and its configured orgs), by
`loom cell update` (a changed configured org) or `loom cell place`, or at the first
verified request of a principal scoped to it. A deployment with one cell and no router
works as a single deployment does.

Cells need the Postgres authority (`LOOM_STORAGE_AUTHORITY=postgres`) and authentication
that every transport enforces (`auth_actually_required()`, and `LOOM_MCP_AUTH_MODE` not
`open`): `loom cell attach` refuses without them, and a process with `LOOM_CELL` set
refuses to boot without them. [Storage authority](storage-authority.md) describes what a
cell holds and how its two stores agree.

## Why whole deployments

A cell shares no store, key, lease or worker with another. Every guarantee a single
deployment makes (tenant isolation, a revocation that holds at once, erasure coverage, a
worker lease, an audit chain, a restore) holds per cell unchanged, and an org's data,
grants and erasure never span cells. The router adds the directory and the routing rules
below; each cell adds a check that it holds the org a request addresses.

What spans orgs becomes per cell:

- a token is valid in the cell that minted it and no other;
- deployment-wide rows are per cell: the global blacklist (`org='*'`), runtime settings
  and overrides, policy rules, rate-limit and budget overrides, signing-key rotation, a
  subject's revocation. `loom cell each -- <command>` runs an org-free admin command in
  every cell with a short-lived credential per cell, records each cell's outcome,
  resumes the cells that failed, and fails until all succeed; `loom doctor` compares the
  cells' settings and configuration fingerprints and names what differs;
- the code and tracker orgs (`LOOM_CODE_ORG`, `LOOM_TRACKER_ORG`) are each cell's own:
  each cell indexes its own repositories, at the ingest cost of each;
- an operator's platform view and the dashboard are per cell, at the cell's host.

## The router

The router is a stateless HTTP proxy, run as several replicas behind the deployment's
shared host, released on its own and supporting cells of build N-1 and N. It holds the
directory in its own Postgres database:

- `cells`: cell, public host, the bearer-token issuers it alone accepts (its own
  `LOOM_JWT_ISSUER`, its `LOOM_JWT_TRUSTED_ISSUERS`, its end-user issuers), its stores
  (its deployment id and the bucket name and prefix), its state (`attaching`,
  `serving`), when attach started (`attach_started_at`, which attach writes under the
  directory's lock only after all its refusals have passed, before it writes any row in
  the cell other than its probes and `--revoke-unprovisioned`'s revocations, and which a
  resumed attach reads), the highest tenancy mode attach has raised or tried to raise in
  it and when, whether it is closed to new orgs and its placement quota
  (`placements_max`), the SHA-256 of the credential it calls the router with (`current`,
  and during a rotation `next`), its root-secret fingerprint (`keys_fp`), a later cell's
  `probe_url`, the names and key thumbprints of its break-glass bots, and its
  configuration fingerprint (`current` and, during a change, `next`; the fields named
  under "Checked continuously"), which attach and `loom cell update` write together with
  the cell's own copy of that fingerprint (`loom.cell_self`);
- `placements`: org, cell. One row per org, written once and never deleted;
- `hosts`: every cell's host and the shared host's aliases (below).

The router has its own operator credential, set in its configuration and never in the
directory; every operator change of the directory is made with it, and every change,
placements included, is written to the router's audit log. Attaching a cell also takes a
platform-admin credential of the cell being attached. The router reports its own
liveness and readiness, apart from any cell's.

The router is put in front of the existing deployment first, passing every request to
it; that deployment becomes the first cell when it is attached, and keeps the shared
host as its own host, so its clients, tokens, refresh tokens, OAuth clients and
registered callbacks keep working. The router answers its own paths (`/cells/`,
`/livez`, `/readyz`) itself, and routes everything else, a cell's `/v1/health` paths
included, to `attaching` and `serving` cells alike, in order:

1. **By host.** A request to another cell's own host goes to that cell. The shared host,
   and any alias of it recorded in `hosts`, are the router's own configuration and are
   handled by rules 2 to 4 (rule 4's cell is the pass-through upstream before any cell
   is attached), never answered 421; any other host that no `cells` row names
   answers 421. Every URL a cell hands out names its own host: its OAuth and OIDC
   endpoints, the device flow's verification URI, the sign-in callback
   (`LOOM_OAUTH_ISSUER`, registered at the identity provider per cell), its JWKS, the
   dashboard's pages and assets, and its MCP resource metadata. MCP clients of a cell
   other than the first use the cell's host, which the cell's setup lines print.
2. **SCIM** goes by host, as rule 1 does: each cell's identity provider application
   provisions it at its own host (below); SCIM on the shared host is the first cell's,
   except a `/scim/v2/Groups` request carrying a JWT, which goes by its issuer as rule 3
   does. The Groups routes are tenancy-checked (they write the org's
   `loom.principal_groups`).
3. **By issuer.** A token-bearing request to the shared host goes to the one cell that
   accepts its `iss` claim, read unverified; the cell verifies. An issuer no cell
   accepts answers 401. Until the first cell is `serving`, rules 2 to 4 send every
   request on the shared host to rule 4's cell, which verifies it, so the deployment
   behind the router serves the orgs it holds as before through prepare, the `LOOM_CELL`
   rollout and attach; from the rollout until `serving`, a new org also needs the
   directory at every process with `LOOM_CELL` set (it answers 503 there while the
   directory is unreadable). This covers MCP and every op, whatever org a body names,
   and the federated and end-user issuers' tokens of integrations.
4. **Everything else on the shared host goes to the first cell**: its OAuth and device
   flows, refreshes, callbacks, password and self-signup requests, health probes, and
   the sign-in page; a token revocation (`/oauth/revoke`) of a JWT goes by its issuer,
   as rule 3 does, and of an opaque refresh token to the first cell, since only the
   first cell's clients use the shared host (`loom logout` revokes at the host it signed
   in at). The shared host's discovery documents are the first cell's.

A client finds an org's cell with `GET /cells/lookup?org=<org>`, unauthenticated and
rate-limited; an org with no placement answers the first cell, and before any cell is
registered the lookup answers 503 with the code `NO_CELL`. A client matches the code,
never the message: on `NO_CELL`, and on a 404 from a deployment with no router,
`loom login --org` keeps the host it was given (the shared host, served as before by
rule 4, or the single deployment); on a 429 it waits the `Retry-After` and asks again,
for at most `--wait` (default 2 minutes), then refuses as a rate refusal; on any other
answer it refuses, naming the router, so no flow starts at a cell that may not hold the
org. An org's cell is public, as a workspace's URL is: the lookup tells that an org
outside the first cell exists. `loom login --org`, the first cell's sign-in page (which
asks for the org first) and the setup lines start every OAuth, device and sign-in flow
at the org's cell host, so no flow crosses cells; a browser is redirected only to the
start of a fresh flow.

Only the first cell is ever in `recording`: a cell whose `first` row is `(false, …)`
treats any mode but `enforced` as `enforced` in its triggers, its mode cache and its
CLI, so a later cell's mode row set to `recording` records nothing. A cell in
`recording` reads its own state with `GET /cells/v1/cells/<cell>`, with its router
credential, answered only for the cell the directory's unique index names first and
accepted only when the credential's SHA-256 is one of the hashes the directory records
for `<cell>`, within `LOOM_CELL_ROUTER_TIMEOUT_S` (default 5 s, the placement call's
timeout); a refused, failed or late answer counts as unreadable.

With the directory unreachable, rules 1 to 4 route from the router's cached cells; a
lookup of an org it has not cached and a placement answer 503.

## SCIM and sign-in across cells

Each cell is its own application at the identity provider: an OIDC client for sign-in at
its host and a SCIM application provisioning its `/scim/v2`, both assigned to the same
group of people, the people of that cell's orgs. A person the identity provider
deactivates, deletes or unassigns is deprovisioned in every cell whose application they
were assigned to; Loom cannot see an application set to admit unassigned people or to
skip deactivation on unassignment, so the operator configures each application to
require assignment and to deactivate on unassignment. Each cell keeps its SCIM users,
deactivations and tombstones exactly as a single deployment does ([SSO and
SCIM](../operations/sso.md)); the router holds no identity.

A cell in `enforced` with SCIM on requires SCIM: a person acts in the cell only while
its SCIM holds an active row for them. The check is on the person's account, not on how
a subject is spelled: `scim.refuses_subject(subject)` takes the subject's own email
spellings and the contact and verified emails of the active accounts it owns, password
accounts included, and refuses if any of them has no active row (the row whose id is
`scim.user_id(email)`; an inactive or deleted row with that id always refuses, and an
email with no row of its own passes only as a work address of an active row whose
primary email is one of the subject's own spellings). A principal outside SCIM is one of
a closed list: a bot (a subject whose `sa:<name>` before any further `:` names a
`loom.machine_identities` row: the bot and its agents, as `check_acted_for` parses it),
a machine or workload subject, an end user (`user:`). Any other `sa:` subject, a
personal service account, is checked through its creator (`service_accounts.sa_creator`)
and every subject its row's `lineage` lists, so acting for it is acting for the person
who made it; every other subject on these paths is a person, and is refused unless it
has an email spelling or owns an account with one. A SCIM read that fails raises
`SignInUnavailable`, as `scim.deactivated` does, never an `OAuthError`, so a refresh
puts its token back and no session ends on an outage. Every path that admits a person
uses it:

- an SSO sign-in, before the account is found or made (JIT), with the email the identity
  provider verified;
- every refresh of a session (`_refresh_session`), with the verified email the grant
  stores, or, for a grant without one, the subject's accounts;
- an act on a person's behalf (`authz.check_acted_for`), whatever the subject's
  spelling;
- a gateway token's subject, checked when verified and remembered as verification's
  other answers are;
- the creator of a delegated token or personal service account (`authz._check_creator`),
  cached as the acted-for check is, so a token made by a person never provisioned is
  refused once the cell is `enforced`.

SCIM on already closes password sign-in and self-signup (`oauth_login_provider=oidc`),
and the device flow signs in through SSO, so through the same check. Principals without
an email are outside SCIM and deprovisioned by whoever made them: bots and minted tokens
by an operator, end-user issuers' users by the service that signs for them, workload
trust by its rule.

**Break-glass.** Every cell holds a key-proof platform-admin bot
(`loom bot create --platform-admin`), which has no email and which SCIM never revokes,
and a second, separate one for attach and `loom cell each`. Their keys stay in the
operator's secret store, read at each use; the directory records only each bot's name
and key thumbprint, so the router holds no cell's credential, and every use of the
break-glass bot is in the cell's audit log and named by `loom doctor`. Operators are
assigned to every cell's applications for the dashboard, and reach a cell whose identity
provider application is broken through its bot.

**A cell's SCIM tokens are its own by construction.** A cell's name matches
`[a-z0-9-]{1,32}` (no `_`, compared exactly; enforced at prepare, attach and boot, and
unique in the directory). A token that starts with `lsc_` names a cell and must match
`lsc_[a-z0-9-]{1,32}_[0-9a-f]{32,}` in full, or every cell refuses it; only a token
without that prefix names no cell. With `LOOM_CELL` set, a SCIM token has the form
`lsc_<cell>_<secret>`, as `loom cell scim-token new --cell <name>` makes it (it runs
offline): exactly three fields split on `_`, the middle one equal to `LOOM_CELL`, and a
secret of at least 32 lowercase hex characters (128 bits).

One parser beside `scim.tokens()` returns a token's cell name or nothing, and its
callers compare that with `LOOM_CELL` and the cell's `first` row (below).
`scim_settings_problem` takes both, so boot and `loom doctor` refuse through one
function, and with `LOOM_CELL` set they refuse a third configured token (two allow a
rotation). One function, `scim.authorized(presented)`, holds the constant-time
comparison for both SCIM routes (`_authorized`, `scim_groups._caller`). With `LOOM_CELL`
unset it compares against the configured tokens as a single deployment does and reads
nothing. With it set it compares against the tokens the parser admits for `LOOM_CELL`
and the `first` row; while that row is unread it raises as `AuthUnavailable` does, so
the routes answer 503 with `Retry-After` and the identity provider retries.

- A process refuses to boot with a SCIM token that names another cell, and `/scim/v2`
  answers 401 to one.
- A later cell refuses a token that names no cell. The first cell, which keeps the
  shared host, also accepts its existing unnamed tokens, only while its `first` row is
  `(true, LOOM_CELL)`, so its SCIM keeps working through the cutover with no rotation.
- A token copied into another cell's settings, or an identity provider application
  pointed at the wrong cell's host, therefore fails closed, and no cell ever compares
  its tokens with another's.

## Preparing a cell

`loom cell prepare` registers a cell in the directory and writes the rows a cell's
processes read. The tenancy check (below) says what it writes and how it resumes; this
section says how it proves it was given the right cell.

**Which cell is first** is never a setting and never the order of preparation. Prepare
grants it only to the cell whose host is the router's shared host and whose deployment
id is the one the router's pass-through upstream reports at the upstream's probe
address. That address is the router's own configuration (`upstream_probe_url`) and the
first cell's `probe_url`, never one of prepare's arguments; prepare, and every rerun of
it, reads it there and refuses an upstream that reports none. Prepare refuses every
other cell until the first is registered.

**The stores are the live ones.** Before writing anything, prepare writes a random nonce
into the target Postgres, under a reserved key of the target ClickHouse's
`loom._migration_lock`, and as a probe object under the target bucket prefix. The cell's
probe route must echo each back as it reads it from its own connection to the primary on
that request. The nonces are then deleted; their rows hold no memory and no org and are
classified so. A copy with the same deployment id is refused.

**The probe route** takes no credential and is outside the fingerprint gate:

- On the main port it answers only the deployment id each process verified at boot, held
  in memory, whatever the request's Host. It reads no store and is not rate-limited, so
  no flood on the main port stalls attach's host check.
- In full, with the values under prepare's reserved nonce keys, it is served only on a
  separate probe listener port. Neither value is secret. It answers 429 past a fixed
  rate per process on that listener.
- Only the cell's probe address (`probe_url`) targets the listener: an internal address
  that no ingress exposes and the router never routes to, which the deployment sends to
  every live process, ready or not, so prepare and attach reach a process the gate holds
  unready.
- The chart's probe listener is off by default and is turned on, in the existing
  deployment too, before the first `loom cell prepare`. It renders a Service on that
  port that publishes unready addresses and changes no NetworkPolicy. Who may reach the
  port is the operator's network policy, as it is for ClickHouse's: with the chart's own
  policy on, which admits only the `http` port, the operator adds a policy admitting the
  probe port from where the CLI runs (policies add up), and without one any pod can
  reach it. Likewise the cell's egress to the router's placement address and to Postgres
  is the operator's, through `networkPolicy.extraEgress` where the chart's policy is on.
- Prepare, attach and `loom cell update --probe-url` retry a 429 for at most `--wait`
  (default 10 minutes) and then name it, and name an unreachable `probe_url` as a
  connection failure.

A later cell's `probe_url` is given to prepare, normalised (`http` or `https`, no
userinfo), and accepted only when it reports the deployment id prepare reads from the
target stores; attach then checks that the cell's own host reports the same id. Prepare
refuses a probe address argument for the first cell. `loom cell update --probe-url`
replaces a later cell's under the directory lock after the same check and a fresh nonce
echo, and refuses the first cell, whose address changes only in the router's
configuration.

**The `first` row.** Prepare writes an explicit, immutable `first` row into every cell's
Postgres (`loom.cell_first`: a row trigger refuses its update and delete and a statement
trigger its truncate, both created `ENABLE ALWAYS` and checked by attach and
`loom doctor` as the org triggers are), `(true, <cell>)` for the first cell and
`(false, <cell>)` for any other, in the same transaction as the tenancy mode, so neither
exists without the other. Read together, the two rows give one of four states. Both
absent with no tenancy or own row either: open, a single deployment's, with no check; a
process with `LOOM_CELL` set stays unready there (no mode row). On a cell the directory
registers this happens only when its Postgres is restored to before its prepare (closed
by `loom cell restore`) or a prepare stops between its registration and its row write
(closed by a prepare rerun), and the triggers and processes with `LOOM_CELL` unset stay
open meanwhile, as on a deployment never prepared, since the cell's Postgres, which
holds its state, cannot tell the two apart (ClickHouse carries no cell state: adding it
would make the write path read a second store for tenancy) and such a process never
reads the directory; where every process has `LOOM_CELL` unset, `loom doctor`'s naming
of it is the only signal. A `(true, …)` row with `recording`: the first cell recording.
Both present otherwise: `enforced`, a later cell's mode reading as `enforced` whatever
its row says (its triggers, mode cache and CLI treat it so, and `loom doctor` names a
row that is not). Either row alone, or both absent beside a tenancy or own row (the
one-row states), which prepare never leaves: the org triggers act as in `enforced`,
every process is gated as by an unread row, `loom doctor` names it, and a rerun of
`loom cell prepare` writes the missing rows back from the directory while the cell runs.
A unique index in the directory allows one first cell, and the value holds no memory
content and is classified so. Attach rechecks the host and deployment id. A process
reads the value with the mode, and "not yet read", "absent" and "false" are distinct:
only a row that has been read makes a process ready, and a row whose cell is not
`LOOM_CELL` gates it (below) and is reported by `scim_settings_problem`. Boot refuses on
settings alone (`scim_settings_problem` with the row unread checks only the token's form
against `LOOM_CELL`); the comparison with the `first` row runs per request once the row
is read, and `loom doctor` reads the value from the cell's Postgres or reports it
unknown.

## The cell's own row

**Checked continuously, not only at attach.** Each process computes its configuration
fingerprint: named fields under a schema version (the hosts of its
`LOOM_PUBLIC_BASE_URL` and resolved `LOOM_OAUTH_ISSUER`, its OIDC issuer and client id,
whether SCIM is on, the bearer issuers it accepts (`trusted_issuers()` and its end-user issuers), its configured orgs,
the `orgs` of its `jwt_end_user_issuers_json`, the deployment id it verified at boot,
and the bucket name and prefix). Its Postgres and ClickHouse both carry the deployment
id (the pairing check a single deployment already runs). The bucket, when one is set, is
identified by its name and prefix (a cell without a bucket takes part in no bucket
check), which no two cells share on any endpoint: no cell's prefix in a bucket of the
same name nests inside another's or holds it, so an endpoint taken from the environment
cannot land in another cell's prefix. No store is identified by its address, so a
standby replaced, a failover, an address taken from the environment or a restore to a
new endpoint changes nothing the cell checks. The deployment id, the bucket name and the
prefix never change after prepare: `loom cell update` refuses a change to any of them,
adding a bucket to a cell prepared without one included, and moving a cell's data to
other stores, or adding one, is a design of its own (Operations). Bots are not in it:
`loom bot create` adds them at run time, each with one org, and placement already bounds
them. The cell's own row in `loom.cell_self` holds an accepted fingerprint, `current`,
and during a change a second, `next`. Its rows, and the cell heartbeat
(`loom.cell_heartbeats`, keyed by the process's `loom.instances` row and written with
it, since a KeeperMap table takes no new column: the `LOOM_CELL` it runs with, the mode
it last read and its fingerprint), hold no memory content and are classified so for
erasure. A process matches a value when every field both know is equal, a field the
value lacks counting as equal only at its default, so builds N and N+1 with the same
configuration match one row. With `LOOM_CELL` set, while its cell's own row, `first` row
or mode row is unread, the state rows are in a one-row state (the `first` row's rule,
above), the cell's own row is absent or matched by neither value, or the `first` row
names another cell, every request it receives but its health probes and the probe route
answers 503 `AuthUnavailable`: bearer requests (the first check in `verify_token`,
before the end-user issuer branch), SCIM, and every route that signs in, mints,
refreshes or exchanges a token, signup, the OAuth and device endpoints and the sign-in
callback included. It also reports itself unready, and it stands aside from the workers
as a drifted process does: it stops its passes, gives up the lease and names no lease in
its instance row, in every `LOOM_BACKGROUND_WORKERS` mode (`on` included: with no lease
to keep, a cell's `on` process runs the lease loop, which starts and stops the
supervisor on the gate within `LOOM_TENANCY_MODE_CACHE_S` and one loop period), and its
instance row does not count it as serving, so an older build beside it keeps the workers
running. A failed read keeps the last rows for at most
`LOOM_JWT_REVOCATION_MAX_STALE_SECONDS`, as the signing keyring and the revocation
snapshot do; past it the process is gated until a read lands. The mode (below) has the
same bound. A cell configured to accept another cell's issuer, a host it was not
attached with or an org pattern it was not granted therefore serves nothing with it,
wherever the request reaches it. Its heartbeat reports the fingerprint, the router
refuses a placement call whose fields match neither value of the directory's row, and
`loom doctor` names a mismatch, a cell whose OIDC client another cell shares, and a cell
with SCIM off beside cells with it on.

`loom cell prepare` writes `current` into `loom.cell_self` with the `first` and mode
rows. Until the directory row holds a fingerprint (before attach), it writes the
fingerprint every live process reports in its heartbeat, refusing while any live process
runs a build before N, which reports none, while the fingerprints they report differ,
and, on a rerun, while they report a bucket name or prefix other than the one the row
registered. Once the directory row holds a fingerprint, a rerun writes `current` and
`next` from it, as `loom cell restore` does, refusing only while a live process's
fingerprint matches neither. Either way the cell stays ready through the `LOOM_CELL`
rollout. A later configuration change, while the cell is `attaching` or `serving`, goes
through `loom cell update`; before attach has written the cell's fingerprint to the
directory row it writes `next` only to `loom.cell_self`, reserving nothing (and
`--finish` and `--revert` then touch only `loom.cell_self`), and a resumed attach runs
its overlap refusals on, and writes to the directory, both values of `loom.cell_self`. A
prepared cell's issuers and org patterns are reserved nowhere: the router routes no
issuer to it and takes no placement from it until attach has run its overlap refusals
and written its directory row.

A change to what a cell accepts goes through `loom cell update --config <file>`, under
the same directory lock as attach; it computes `next` from that configuration with the
same function the processes use, and refuses while a different `next` is pending; rerun
with the same configuration, or `--finish`, first writes `next` wherever it is missing.
It reruns attach's overlap refusals against every other cell's directory row, counting
both values of each, writes the new fingerprint as `next` to the directory and then to
`loom.cell_self`, so old and new processes both serve while the new configuration rolls
out. `loom cell update --finish` waits, at most `--wait` (default 10 minutes), until
every live process reports `next` and the longest request deadline has passed; when the
change turns SCIM on or changes the cell's OIDC client it then runs attach's SCIM
precondition (refusing, or `--revoke-unprovisioned`); only then does it make `next` the
only value, in `loom.cell_self` first and the directory last. A widened issuer or
pattern is reserved in the directory before any process accepts it, and a narrowed one
is released only after none does. A refusal or a wait that runs out keeps both values
and prints the processes not yet reporting `next`; rerunning resumes, and
`loom cell update --revert` waits the same way until no live process matches only `next`
(refusing when `--wait` runs out), then drops `next`, in `loom.cell_self` first, under
the same lock; a configured org the update placed stays placed in the cell (a placement
is never deleted).

A new SCIM token changes no fingerprint and needs no `loom cell update`. A cell changes
its identity provider application as a single deployment does ([SSO and
SCIM](../operations/sso.md)), with the same limits; nothing in that is particular to
cells.

## The tenancy check and placement

A process with `LOOM_CELL` unset runs no tenancy check. Under the Postgres authority
(`LOOM_STORAGE_AUTHORITY=postgres`, the only one a cell runs) a server process still
reads, in one statement, the mode row, the `first` row and whether any `loom.tenancy` or
`loom.cell_self` row exists, with the mode cache's rules (below), and so does a CLI
command that declares an org; an org-free command, such as a token revocation,
`loom migrate` or `loom doctor`, is not gated by the mode and runs as it does today,
Postgres outage included. Both rows absent with no tenancy or own row, as on a
deployment never prepared, or the first cell in `recording`, leave the process serving
as a single deployment; every state the `first` row's rule (above) counts as `enforced`,
or as a one-row state, gates it as `enforced` does. `enforced` (which attach sets on the
first cell once every live process has `LOOM_CELL` set, and prepare on a later cell,
whose processes run with it from their first boot) makes a server process unready, run
no worker and answer nothing but its health probes and the probe route (so a prepare
rerun can reach it), and makes the CLI command refuse; the readiness reason and the
refusal name `LOOM_CELL`. So a pod or an operator's CLI that loses `LOOM_CELL` on an
`enforced` cell writes for no org; one that also loses `LOOM_PG_DSN` or
`LOOM_STORAGE_AUTHORITY` is refused by the authority check the deployment already runs
(ClickHouse's `loom.storage_authority` record). The `loom cell` commands that connect to
a cell's stores (prepare, attach, place, restore, update) refuse without `LOOM_CELL`,
and, once the directory has a row for `LOOM_CELL`, refuse stores whose deployment id is
not the one that row records. A deployment started with `LOOM_CELL` unset is a single
deployment until prepared, and pointing it at another deployment's bucket prefix is the
misconfiguration it is for any deployment. On the first cell, `loom cell prepare` runs
before `LOOM_CELL` is rolled out; a later cell runs with `LOOM_CELL` set from its first
boot, so its processes are gated and stand aside from the workers (its blob sweep
included) until prepare writes its row, and prepare refuses a later cell while any live
process's heartbeat lacks `LOOM_CELL` or names another cell. Prepare does three things,
in order:

1. **It refuses a cell whose stores already carry another cell's identity**: a
   deployment id the directory records, a `first` or mode row that differs from what it
   would write, or tenancy rows the directory does not place there.
2. **It registers the cell** in the directory as `attaching`, under the directory lock:
   its host, the deployment id read from the cell, the SHA-256 of the credential it
   calls the router with, and its bucket name and prefix, checked against every other
   cell's, prepared or attached. A later cell's prefix must hold no object but prepare's
   own probe. The credential is `lcr_` and 128 lowercase hex characters (512 bits), as
   `loom cell credential new` makes it offline, printing the credential and its hash;
   boot and `loom doctor` refuse a `LOOM_CELL_ROUTER_TOKEN` of any other form. Prepare
   takes the hash as an argument, 64 lowercase hex characters, so no CLI needs the
   secret; any other value, the credential or its 128-character secret included, is
   refused, naming `loom cell credential new`, and stored nowhere. No other cell's row
   may hold the hash, as its current or next one.
3. **It writes the cell's rows**: the mode (`recording` on the first cell, `enforced` on
   a later one), the `first` row and `current`, and prints, read-only, what attach would
   refuse.

Prepare is resumable. A rerun for the directory row of the same name, deployment id and
host writes whichever of the `first` row and the mode row is missing, from the
directory's state, and `loom.cell_self` as above (before attach from the heartbeats when
it is missing; once the directory holds the fingerprint, `current` and `next` from it,
overwriting a present value that differs); before attach, a present `current` that
differs from what the heartbeats report refuses, naming `loom cell update`. The
directory's state is the `first` value the unique index names and the highest mode it
records for the cell: `recording` for the first cell or `enforced` for a later one at
registration, in the same transaction as the row, then each mode attach raises, recorded
in the directory before attach sets it in the cell's mode row (a refusal reports the
mode row as attach reads it back after any return to `recording`, and never lowers the
directory's record). A rerun refuses a credential hash that is neither the row's
`current` nor its `next` (rotation is `loom cell credential`'s), counts the tenancy rows
placed in or recorded since its own registration as its own, and skips registration's
checks it has already passed (the empty prefix and the prefix overlap), so it runs on a
`serving` cell whose prefix holds data.

A process with `LOOM_CELL` set and no mode row does not become ready. With it set, each
cell's Postgres holds `loom.tenancy` (org, since) and `loom.tenancy_mode` (`recording`,
`enforced`); a process reads the mode through a cache of at most
`LOOM_TENANCY_MODE_CACHE_S` (default 5 s), keeps the last mode it read when it cannot
read it, and keeps serving the orgs it holds, for at most
`LOOM_JWT_REVOCATION_MAX_STALE_SECONDS`, past which it is gated until a read lands; a
process that has never read the mode does not become ready. These rules hold with
`LOOM_CELL` unset too: there absent mode and `first` rows with no tenancy or own row are
a deployment never prepared, and a failed read is never taken for an absent row. In
`enforced`, a cell serves exactly the orgs it holds a row for. A `recording` row is
written by one statement that inserts it only while the mode is `recording`, reading the
mode row `FOR SHARE`; it runs at `READ COMMITTED` in its own transaction and commits
before the request goes on. Attach's write of `enforced` and these inserts never cross:
an insert already running when attach writes the mode is placed by step 4, one that
starts after reads `enforced`, a stream of inserts cannot hold the write off, a new-org
request waits on attach for a few seconds at most, and an attach that stalls holding the
write's lock releases it within seconds. Attach takes
`LOCK TABLE loom.tenancy_mode IN EXCLUSIVE MODE` (which waits for running inserts and
queues later ones behind it) in short tries, in one transaction with server-side
timeouts, then writes the mode and commits; after any error in that transaction it ends
that session's backend (`pg_terminate_backend`, the backend named by the pid and
`backend_start` attach captured when it connected) and waits until it has left
`pg_stat_activity`, then reads the mode back before it retries or reports; a mode read
back as `enforced` is written again in a fresh transaction, whose commit waits for the
standby, before attach goes on, since a commit ended while it waited is on the primary
alone. The attach command fixes those timeouts, and its tests cover each of these cases.
The mode cache reads the mode with a plain `SELECT`, which takes no row lock and never
waits on attach. An insert that finds `enforced` (a process whose cache still reads
`recording`) writes nothing: the request goes on to the placement call as in `enforced`,
and a CLI command refuses as in `enforced`, naming `loom cell place`. A process whose
cache still reads `enforced` after step 3 or step 4 returns the cell to `recording`
places through the router's gate until its cache expires. `loom doctor` lists any
tenancy row the directory does not place.

- **It runs in the transports, after verification, before anything is written.** The
  check uses the org the transport already resolves (`gates.resolve_org`, with its
  caller defaults and plane orgs; an op whose org field is named otherwise, such as
  `from_org`, declares it, and a census test lists every op's org-like fields): in the
  HTTP op endpoint right after the body is read, in the MCP dispatch per call (a batch
  entry for another cell's org answers `isError`), and in each route outside the op
  registry, before `gates.authorize`, personal-namespace provisioning or any handler. A
  refusal's ledger row written before the check (malformed JSON, a named-argument
  refusal) is not written for an org the cell does not hold. A unit test lists every
  route of the app and fails unless it is marked tenancy-checked or org-free.
- **The CLI declares its orgs.** `run_connected` and `connect_checked`
  (`server/cli/_ch.py`) take the orgs a command writes, required unless the command is
  declared org-free, and refuse an org the cell does not hold, except that a first cell
  in `recording` records it as a request does, reading the cell's state with the cell's
  router credential (`LOOM_CELL_ROUTER_TOKEN`, in the first cell's CLI configuration
  while its mode is `recording`), and only while the directory answers `attaching`;
  every CLI command that connects goes through them (the code-index commands' own
  connects included), and a census test fails on any command not declared and on any
  connect under `server/cli/` outside `_ch.py`. A holder of the cell's router credential
  can place orgs for the cell, within the cell's rate and `placements_max`, as the
  operator credential `loom cell place` uses can for any cell within its
  `placements_max` (an operator's call is not rate-limited, nor counted toward the
  cell's rate), and a placement is permanent, so an org name it takes is lost to every
  other cell: a power over the shared directory that the cell's store credentials lack.
  So the pods hold it, and a CLI only for the one read that needs it, while the first
  cell's mode is `recording`: a CLI command that declares an org refuses to run with it
  set, naming it, unless the cell is the first and its mode reads `recording`, and an
  org-free command never uses it; a leaked one is stopped by `loom cell limit --closed`,
  which needs none of the cell's stores, and replaced by `loom cell credential` (below).
  `loom cell place --org <org>` (declared org-free: it writes the org's placement and
  tenancy row, which is how an org comes to be held), with the router's operator
  credential, places an org for a command that writes a new one (a bot, an import),
  through the router's placement API and in its order, as attach's placement of
  configured orgs does: it inserts the placement and reads back the winner, writes the
  cell's tenancy row only if the winner is this cell (otherwise it refuses and writes
  nothing), and reports success only once the row is written, so a rerun is idempotent.
  For an org the directory does not place yet it passes the placement API's gate
  (below): refused while the directory is unavailable, for a cell the gate does not
  accept, closed, or when the fingerprint of the CLI's own configuration of the cell
  (with the deployment id read from the stores it connects to) matches neither value of
  the cell's row, or past `placements_max`, and counted toward it.
- **Writers older than the check are refused by the stores they write.** In `enforced`,
  statement-level triggers on the authority's org-keyed tables (not `loom.tenancy`
  itself, which the cell's own rows are written to) refuse a row for an org with no
  tenancy row (never for `''`, `'-'` or `'*'`): an `AFTER INSERT` and an `AFTER UPDATE`
  trigger on each table, each `REFERENCING NEW TABLE` and sharing one function (Postgres
  allows a transition table on one event only; an `ON CONFLICT DO UPDATE` row reaches
  the update trigger). The function does nothing while the cell's state is open or
  recording and acts in every other state (the `first` row's rule, above); it reads the
  mode after the statement's rows are written, so after its transaction has an id, with
  no row lock, as the mode cache reads it, so no write queues behind attach, and step 4
  (below) waits out every transaction that could have read `recording` before its second
  read. The triggers are created `ENABLE ALWAYS` on every org-keyed table, a list
  generated from the schema as step 2's is, and attach and `loom doctor` refuse or name
  a missing or disabled one. So an older CLI, a pod downgraded past the check, or a
  script that leaves the triggers in place cannot write live memory for an org the cell
  does not hold; their cost, one read of the mode and `first` rows and of whether a
  tenancy or own row exists per statement and a `loom.tenancy` lookup of the statement's
  orgs in `enforced`, is measured with them on and off on `memory.set` and a delivery
  pass before they ship, both in `enforced` with the statement's orgs held and on a
  deployment never prepared, and they ship only if `memory.set`'s p99 rises by at most
  10 ms and a delivery pass's rows per second at `LOOM_PG_DELIVERY_BATCH_ROWS` falls by
  at most 10%, the result and its command recorded in [benchmark
  results](../benchmarks/results.md); a script that writes `loom.tenancy` itself is
  named by `loom doctor`, which lists any tenancy row the directory does not place and
  any org in an org-keyed Postgres table with no tenancy row. Before the first attach
  the operator rotates the cell's ClickHouse writer credentials (create the new user,
  roll it out to configurations of builds that check, drop the old one), and attach
  refuses while any ClickHouse user with `INSERT` on `loom.*` (`system.grants`, with
  database, global and role grants followed through `system.role_grants`, and users from
  configuration files or with implicit `ALL` counted) is not one a live process of a
  checking build or the CLI's configured user reports, so only holders of the rotated
  credential write ClickHouse; an older CLI given that credential still writes
  unchecked, and `loom doctor` lists what it wrote: any org found in an org-keyed
  ClickHouse table with no tenancy row, from the same schema list as attach step 2.
- **An org the cell does not hold is placed there or refused.** For a verified principal
  scoped to the org (a token of that org, or an issuer's or bot's principal whose org it
  is), the cell calls the router's placement API with its own credential, and the cell
  the call names must be the one whose recorded hash the credential matches (a hash
  names one cell): the router places one org at a time (a transaction-scoped lock on the
  org, which every path that places takes), answers an org placed already with its cell,
  and otherwise judges the asking cell on its locked row, inserts the placement and
  reads the winner back, which the cell compares by its name and recorded deployment id.
  If the winner is this cell, the cell writes the tenancy row and serves the request;
  this cell's name under another deployment id than the process verified answers 503 and
  writes nothing; if another cell's name, the request answers 409 `WRONG_CELL` and
  nothing is written. This is the one path by which a request brings an org to a cell,
  whatever created it: a mint, a gateway's or a federated issuer's token, an end-user
  issuer's `prefix*` org, a bot. A `*`-org or platform principal acts only in orgs the
  cell already holds and never places one. For an org the directory does not place yet,
  the router accepts a placement call only from a `serving` cell (or from the first cell
  while `attaching` once the directory records `enforced`, which attach step 3 writes
  before it sets the cell's mode), and only with the calling process's configuration
  fingerprint, whose fields must match a value of the cell's directory row; a prepared
  cell's row holds no issuers or org patterns until attach writes them. A failed call,
  one unanswered within `LOOM_CELL_ROUTER_TIMEOUT_S`, a cell not yet attached or closed
  to new orgs, or a fingerprint that matches neither value answers 503 and writes
  nothing; a retry is safe, since the read-back makes it idempotent. For an org the
  directory already places, a cell's call is checked only for its credential (against
  the cells' hashes the router caches for `LOOM_ROUTER_DIRECTORY_CACHE_S`, default 5 s)
  and the cell's rate, and a `loom cell place` only for the operator credential; both
  answer the winner whatever the cell's state, quota or fingerprint, adding no placement
  (the winner is public through the lookup anyway); a request refused past its cell's
  rate answers 503 with `Retry-After` (at most a minute), and its retry is admitted by
  the replica that answers it once the cell's calls there are under the rate. An org the
  cell already holds needs no call, so serving it never depends on the router.
- **Placements are bounded per cell.** Each router replica rate-limits each cell's
  placement calls per minute (`LOOM_ROUTER_PLACEMENTS_PER_MINUTE`), which bounds their
  pace to that rate times the replicas, and a cell's directory row can carry a placement
  quota (`placements_max`, unset by default), which once set bounds their total, counted
  and inserted under a lock on the cell's directory row, so concurrent calls across
  router replicas never pass it; past the rate, every placement call of the cell answers
  503 with `Retry-After`; past the quota, a call for an org the directory does not place
  yet answers 503 as for a closed cell (an operator's `loom cell place` is exempt from
  the rate, as above); and each cell's placements are reported in
  `/v1/health?include=capacity` and by `loom doctor`, which names a cell with no quota.
  Every org the first cell's attach places (step 2's snapshot, its configured orgs
  included, and step 4's) is counted but never refused, since the cell already serves it
  and refusing it would strand its data; every other placement of an org the directory
  does not place yet, a later cell's configured orgs at attach and a changed one at
  `loom cell update` included, passes the gate, a closed cell and `placements_max`
  included, so a later cell closed or full before attach refuses the attach, naming the
  org.
- **The first cell records until `enforced` as a single deployment does.** In
  `recording`, which lasts until step 3 writes `enforced` to the cell's mode (and again
  whenever step 3 or step 4 returns the cell to it), a verified request for a new org
  writes a tenancy row with no placement call, so the router's rate, `placements_max`
  and `loom cell limit --closed` do not apply: new orgs arrive as they do on a single
  deployment, bounded by its own signup, issuer and JIT settings, and step 2 or step 4
  places them unrefused. `loom doctor` lists the recorded orgs that hold no data, so the
  operator sees what attach will place.
- **Configured orgs are placed at attach.** The cell's `oidc_jit_org`, the
  `self_signup_org` of a cell with signup on, and its code orgs (`LOOM_CODE_ORG` and the
  org of each `LOOM_CODE_REPOS_JSON` entry the code config keeps) and tracker org are
  placed as its own when it is attached: the first cell's in step 2's snapshot, a later
  cell's before it is marked `serving`. JIT and self-signup pass the tenancy check as
  any request does, and nothing refuses at boot: in the first cell's `recording` they
  record their org while the directory answers `attaching`, and elsewhere a configured
  org the cell does not hold answers 503 until it is placed, which `loom doctor` names;
  every attach, first or later, new or resumed, runs the overlap refusals on and places
  the configured orgs of both values of `loom.cell_self`, so one an update put in `next`
  before attach is placed with the rest; once attach has written the cell's directory
  row, `loom cell update` places a changed one in the directory transaction that writes
  `next`, through the placement API's gate for an org the directory does not place yet
  (closed cells and `placements_max` included), refusing, naming it, when another cell
  holds it, and then writes its tenancy row in the cell; for a later cell still
  `attaching` after its directory transaction, `loom cell update` refuses a
  configured-org change until the cell is `serving`. In `enforced`, a worker writes for
  an org only once the cell holds it (the tenancy check with no principal), so a later
  cell indexes, imports and sweeps nothing under its configured orgs before attach
  places them; in `recording`, workers run as before, and step 2's snapshot and step 4's
  second read find every org they write under.
- **Rows are never deleted.** A tenancy row and a placement name an org, not its memory:
  both are classified for erasure as holding no memory content, as a namespace's claim
  is, and stay after any erasure, so an org name never comes free for another cell. A
  process keeps the orgs it has seen held, and a kept org is never wrong to serve.
- **A placed org with no row** (a restore to before its placement) gets its row back
  from `loom cell restore`, before the restored cell starts ([What each cell
  runs](#what-each-cell-runs)).

## Attaching a cell

`loom cell attach`, run against the router, registers a cell, one attach at a time, and
refuses on any failure, naming it:

- the cell answers on its own host with the deployment id its `probe_url` reports, every
  live process has `LOOM_CELL` set to its name, it runs the Postgres authority with a
  synchronous standby (`LOOM_PG_DURABILITY=synchronous`) and enforced authentication,
  and records a deployment id no other cell records;
- on the first cell, attach's Postgres role can read every backend's transaction state
  (`pg_read_all_stats`) and the cell's Postgres allows no prepared transactions
  (`max_prepared_transactions = 0`), so step 4 sees every open transaction;
- its keys are its own: no issuer it accepts for bearer tokens is accepted by another
  cell, and its root-secret fingerprint (`keys_fp`) matches no other cell's. Upstream
  identity providers and workload issuers may be shared, since a cell mints its own
  token from them; a gateway (`LOOM_IDENTITY_PROVIDER=gateway`) mints for one cell, so a
  deployment runs one gateway per cell;
- no live process of any other cell reports a fingerprint matching neither value of its
  directory row, and every live process of the cell being attached matches its
  `loom.cell_self` `current` or `next` (attach names any that does not);
- no other cell records the same (OIDC issuer, client id) pair (the directory row
  records them, and `loom cell update` counts them in its overlap refusals);
- with SCIM on, every SCIM token it holds names it (`lsc_<cell>_…`), or, on the first
  cell only, names no cell; a later cell holds no SCIM user row or tombstone (its people
  are provisioned after attach, in the order `LOOM_CELL`, a named token, then the
  application's assignment);
- its `LOOM_PUBLIC_BASE_URL` is set (a process with `LOOM_CELL` set refuses to boot
  without it, so no URL it hands out comes from a request's Host), and the hosts of it
  and of `LOOM_OAUTH_ISSUER` (an empty one resolves to the public base URL) equal its
  registered host: for the first cell, whose registered host is the shared host, they
  are the shared host or a recorded alias of it; no later cell's host is the shared host
  or an alias, which the directory's `hosts` table (host as primary key, normalised by
  one function, `normalise_host`: lowercase, IDNA to ASCII, no trailing dot or default
  port, brackets kept on IPv6, used by `hosts`, rule 1, attach and the readiness check;
  owned by a cell, an alias belonging to the first cell) enforces, written in the same
  transaction as each check, and from which rule 1 routes; `LOOM_JWT_ISSUER` is an
  opaque string, needing only to be unique; prepare checks the same;
- its configured orgs collide with no org placed in another cell, and no org pattern it
  trusts (its end-user issuers' `orgs`) overlaps another cell's patterns or configured
  orgs, nor does another cell's pattern match its configured orgs, by the overlap rule a
  single deployment applies to its own end-user issuers;
- its stores are not another cell's: a probe key written to a reserved name of its
  `loom._migration_lock` KeeperMap is visible to no other cell and every other cell's
  probe is invisible to it (a ClickHouse without a Keeper refuses); its bucket prefix is
  neither a path prefix of another cell's in a bucket of the same name, on any endpoint,
  nor has one, and probe objects are listed in neither direction;
- every live process of every cell runs a build whose predecessor also runs the tenancy
  check (below), and a heartbeat that reports its tenancy mode, fingerprint and the
  `LOOM_CELL` it runs with;
- with SCIM on, no active account has a contact or verified email without an active SCIM
  row (password accounts, live refresh sessions and personal service accounts included),
  and no live service account's creator chain ends in an account with no email
  (agent-signup tokens made before SSO): attach lists them and refuses, or with
  `--revoke-unprovisioned` ends their tokens issued before the attach under SCIM's own
  revocation prefix, so a person the identity provider provisions later is admitted,
  since it will never deprovision a person it does not count as assigned.

The first attach turns the existing deployment into the first cell, in order:

1. check the mode is `recording`, as prepare set it: every verified request has written
   its org's tenancy row if absent while the directory answered that the cell is
   `attaching`;
2. in one directory transaction, write `attach_started_at`, the cell's issuers, patterns
   and fingerprint (both values of `loom.cell_self`), and the placements, then the
   tenancy rows, of every org the cell holds, from one snapshot, straight into the
   directory (no other cell exists yet): its configured orgs (of both values), the
   distinct `org` of every table of both schemas (generated from the schema, as
   erasure's table classification is), and the orgs of catalogued bucket objects; never
   `''`, `'-'` or `'*'`;
3. set `enforced` as above, within `--wait`, which the wait below then shares, starting
   no try that could commit the mode with less left than that wait's minimum (the mode
   cache's lifetime plus the longest request deadline): attach then refuses, reporting
   the mode it reads back (`recording`, and a rerun resumes from step 1), or the mode as
   unknown when it cannot read it, and a rerun then resumes by the mode it reads; then
   wait, within what remains of `--wait` (default 10 minutes, then refuse and set
   `recording` back), until every live process reports `enforced` and then the mode
   cache's lifetime plus the longest request deadline have passed; then run the SCIM
   precondition again (`--revoke-unprovisioned` again if given), for accounts made
   before `enforced` took hold; if it refuses, the cell stays `enforced` and
   `attaching`, and running attach again resumes from the wait, as a resumed attach does
   (below);
4. wait, within what remains of `--wait`, until no transaction that could have read
   `recording` is still open, judged from a Postgres snapshot attach takes once it has
   read `enforced` back in this run (not from a clock): a transaction on the cell's
   database that started before it, from any client, background worker or logical
   replication connection, read-only ones included, a writing transaction on another
   database of the same server that did (it holds back every snapshot's horizon, and is
   named as such), a prepared transaction (which refuses at once), or one whose state
   attach cannot read counts as open, and Postgres's own maintenance processes, physical
   replication connections (every standby's and a base backup's, which run no SQL) and
   attach's own connections do not. Past `--wait` attach refuses, naming what it waited
   on, and leaves the cell `enforced`, so a rerun resumes from step 3's wait. The attach
   command fixes the exact query, and its tests cover each kind of transaction. Once the
   wait passes, place the tenancy rows written since the snapshot and every org step 2's
   sources yield when read again (the distinct `org` of every table of both schemas and
   the orgs of catalogued bucket objects; a row or an object an unchecked writer, such
   as an older CLI or a worker, wrote in `recording` after the snapshot is placed too),
   check again that every live process reports `enforced` (refusing and returning to
   `recording` as step 3 does if one does not), and mark the cell `serving`. The
   directory records the highest mode attach has raised or tried to raise, which
   `loom cell restore` writes back only upward: a cell whose step 3 was refused at the
   lock is restored `enforced`, and its resumed attach then runs from step 3's wait.

A later cell is attached only once the first is `serving`. It must hold no org data
(attach refuses otherwise) and starts in `enforced`. Its attach runs, in order: the
refusals; one directory transaction, under the directory lock and with the overlap
refusals run again inside it, that writes `attach_started_at`, the placements of its
configured orgs and its issuers, patterns and fingerprint (both values of
`loom.cell_self`), refusing and writing nothing when a read-back names another cell (it
names the org); the tenancy rows of its configured orgs and its `loom.cell_self`, in the
cell; the SCIM precondition (`--revoke-unprovisioned` if given); and `serving`. A
resumed later attach reruns the refusals other than a new cell's and continues from the
first of these steps not done; the placements are idempotent through the read-back. Its
people are provisioned by its own identity provider application before they sign in. An
org's own services are attached as a cell of their own. A new org of people starts with
`loom cell place --org`, then its people's assignment to that cell's applications; a
cell with SCIM on admits no one until its first provisioning cycle has run. A cell
nearing its stores' limits is closed to new orgs: its placement calls answer 503, so its
issuers' new orgs wait for capacity rather than spill elsewhere.

A resumed first attach, for a cell whose directory row has `attach_started_at`, reruns
every refusal of the list above that applies to the first cell. It then continues by the
cell's mode: in `recording` from step 1; in `enforced`, from step 3's wait (every live
process reports `enforced`, then the cache's lifetime and the longest request deadline
pass) and then its SCIM precondition, so the SCIM check that ends an attach stopped
after setting `enforced`, whatever stopped it, always runs after `enforced` has taken
hold.

## What each cell runs

Everything a deployment runs, unchanged: its pools, migrations, workers and their
leases, health and readiness (a pod's readiness is its cell's), erasure and its census,
audit checkpoints, backups and restores. A cell's restore is a single deployment's
([backup and recovery](../operations/backup-and-recovery.md)), with the same limits:
what the backup holds (its keys, revocations, sessions, bots and SCIM rows) comes back
as it was. A cell adds one step after the store restore and before anything else that
page runs: `loom cell restore`, with the cell stopped, run where the operator runs
`loom cell place` and attach, with the router's operator credential, which no cell's
configuration holds (a CLI command declared org-free: it writes the cell's own rows),
writes back from the directory the rows a restore can take back, so `loom reconcile`'s
erasures and redactions are not refused by the trigger, and a restore of both stores,
which `loom reconcile` refuses, still gets its cell's rows. It refuses while the
directory is unavailable, and writes:

- a tenancy row for every org the directory places in it;
- whichever of the `first` and mode rows is absent, from the directory, as a resumed
  prepare does; a present `first` row that differs from the directory's is refused, and
  one that matches is kept;
- once the tenancy rows are back, the directory's recorded mode over the mode row, only
  upward (`recording` to `enforced`), never down;
- once the directory row holds the cell's fingerprint (attach writes it), its
  `loom.cell_self` from it (`current` and `next`); before attach the directory holds
  none, and the restored `loom.cell_self` stays as the backup holds it (a missing
  `current` is written by a rerun of `loom cell prepare` once the cell starts, a lost
  `next` by `loom cell update --config` again).

A first cell restored to before its prepare, and not yet attached, serves nothing on the
shared host at its processes with `LOOM_CELL` set (one with it unset serves as a single
deployment, as before the rollout) until that rerun of `loom cell prepare`, which is the
step right after it starts.

A cell's readiness never waits for the directory, so an outage of it holds no cell's
rollout. A process that reads `recording` records a new org only while the directory
answers that its cell is `attaching` (read at most every `LOOM_TENANCY_MODE_CACHE_S`);
while the directory is unreadable or marks the cell `serving`, a new org answers 503 and
the orgs the cell holds keep serving. A restore to before the first attach's step 3
whose `loom cell restore` was skipped therefore serves no org it does not hold;
`loom doctor` names such a cell, and `loom cell restore` raises its mode.

**The directory is a highly available store.** Its Postgres runs with a synchronous
standby and point-in-time recovery to its last commit, as a cell's does, so a failover
or a corruption loses nothing committed. Losing it entirely is an outage that fails
closed: every cell keeps serving the orgs it holds, routing continues from each router
replica's signed snapshot of its last cache (written to shared storage on every
directory change), and the placements, lookups of uncached orgs, `loom cell` commands
that read or write the directory and the recording cells' state reads answer 503 or
refuse until the directory is back, while `loom doctor` reports its directory checks as
not run and, with `LOOM_CELL` set, exits non-zero; rebuilding a lost directory from the
cells is a design of its own.

Each cell keeps, in its own Postgres, a copy of its directory row's configuration
fingerprint (`loom.cell_self`), which its processes compare their configuration with and
attach and `loom cell update` write ([The cell's own row](#the-cells-own-row)).
`loom cell restore` rewrites its `loom.cell_self` from the directory; restored stores
carry the cell's deployment id, so a restore to a new endpoint needs only the new
address in the cell's configuration. Closing a cell or setting its `placements_max` is
one command,
`loom cell limit --cell <cell> [--closed | --open] [--placements-max (<n> | none)]`,
which writes the directory alone, with the router's operator credential and none of the
cell's stores, so it works while the cell is down; the router judges a placement from
the directory row.

## Operations

- `loom cell restore`, after any store restore and before `loom reconcile`, writes the
  cell's rows back from the directory ([What each cell runs](#what-each-cell-runs)).
- `loom cell which --org <org>` names an org's cell (erasure, restore and support start
  there).
- `loom cell limit --cell <cell> --closed` closes a cell to new orgs, `--open` reopens
  it, and `--placements-max <n>` sets its placement quota (`--placements-max none`
  clears it).
- `loom cell credential new` makes a router credential offline and prints it with its
  SHA-256; `loom cell credential --cell <cell> --next <sha256>` records a new router
  credential's SHA-256 as the cell's `next` hash; like prepare, it takes the hash as an
  argument, in the same form. It uses the router's operator credential, writes only the
  directory and holds the directory lock; it is refused when any cell records that hash
  as current or next, and while a `next` is pending. The router accepts both on
  placement calls and state reads. Once the new credential is in the cell's pods and,
  while the first cell's mode is `recording`, its CLI configuration,
  `loom cell credential --cell <cell> --finish` makes `next` current and drops the old;
  it checks nothing it cannot see (the router does not know which credential a pod
  holds), and a cell finished too early fails closed: its placement calls and state
  reads answer 401, at the latest once the router's cache of the cell's hashes expires
  (`LOOM_ROUTER_DIRECTORY_CACHE_S`), until its pods hold the new credential, or the old
  hash is recorded again with `--next` and finished.
  `loom cell credential --cell <cell> --revert` drops a pending `next`, so a wrong or
  leaked one never has to be finished; `--finish` and `--revert` refuse with no `next`
  pending.
- A new cell's settings name `LOOM_CELL`, its public host (`LOOM_PUBLIC_BASE_URL`,
  `LOOM_OAUTH_ISSUER`, `LOOM_JWT_ISSUER`), its keys, its bucket prefix, its configured
  orgs, its identity provider OIDC client and SCIM token, and its router credential
  (`LOOM_CELL_ROUTER_URL`, `LOOM_CELL_ROUTER_TOKEN`, which its pods hold, and
  `LOOM_CELL_ROUTER_TIMEOUT_S`).
- Each cell reports writes per second, rows per namespace and bytes per key in
  `/v1/health?include=capacity`, against about 13.6 KB a key in ClickHouse and 6.8 KB in
  Postgres ([Memory at scale](../benchmarks/results.md#memory-at-scale)).
- **An org stays in its cell.** Moving an org between cells is a design of its own: it
  needs the org's writes fenced in every process of the cell, a copy of every store that
  counts nothing twice, and a verified removal.
- **A cell's data stays in its stores.** Moving it to other stores (another deployment
  id, bucket name or prefix), or adding a bucket to a cell prepared without one, is a
  design of its own: old and new processes would write to different stores during any
  rollout, so it needs the cell's writes fenced as an org move does.
- **A cell stays attached.** Retiring a cell is a design of its own: it needs proof,
  from the stores themselves, that every copy of its orgs' data is gone, before anything
  may answer that it is. Until then a cell that is down stays a cell: its orgs answer
  503, and its names, issuers, hosts and placements stay reserved.

## Failure modes

| Failure                                                                  | What happens                                                                                                                                                                         |
| ------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| The directory is down                                                    | requests by host, by issuer and to the first cell are routed; new orgs and uncached lookups answer 503                                                                               |
| The router's placement call fails                                        | the request answers 503 and nothing is written; orgs the cell holds are served                                                                                                       |
| A cell's issuer places orgs in a flood                                   | from `enforced` on, its placement calls answer 503 with `Retry-After` past its rate, and its calls for new orgs answer 503 past its quota if one is set; other cells place as before |
| A SCIM token of another cell, or an application at the wrong cell's host | the process does not boot, or `/scim/v2` answers 401 and writes nothing                                                                                                              |
| A cell's Postgres or ClickHouse                                          | that cell fails as a single deployment does ([storage authority](storage-authority.md#failure)); other cells serve                                                                   |
| A cell's own rows or tenancy mode are unreadable                         | a process keeps its last rows and mode within the stale bound, then is gated; one that never read them is unready                                                                    |
| A `first` or mode row removed, or both beside a tenancy or own row       | triggers act as in `enforced` and every process is gated; `loom doctor` names it, a prepare rerun writes them back                                                                   |
| No state, tenancy or own row on a cell the directory registers           | open; `loom doctor` names it; `loom cell restore` closes it after a restore, a prepare rerun after a stopped prepare                                                                 |
| A cell's `first` value is not yet read                                   | every request but health probes and the probe route answers 503 (SCIM with `Retry-After`); the process is unready and runs no worker                                                 |
| A request for another cell's org                                         | 409 `WRONG_CELL` after verification, nothing written                                                                                                                                 |
| A cell restored to an earlier time                                       | `loom cell restore` writes placed orgs' rows back before it starts                                                                                                                   |
| A first cell restored to before its prepare, not yet attached            | at processes with `LOOM_CELL` set, every request but health probes and the probe route answers 503 until prepare reruns                                                              |
| The directory lost                                                       | every cell serves the orgs it holds; placements and uncached lookups answer 503, directory commands refuse, until it is back                                                         |
| A cell in `recording`, the directory unreadable                          | a new org answers 503; the orgs it holds keep serving                                                                                                                                |
| A process stuck during first attach                                      | the attach refuses after `--wait` and the cell returns to `recording`                                                                                                                |
| A recording insert held open during first attach                         | attach stops trying once too little of `--wait` is left for the wait, reporting the mode it reads back; a rerun resumes                                                              |
| Attach stalls while it holds the mode table's lock                       | Postgres ends its session within seconds and releases the lock; new orgs answer within their deadline                                                                                |
| A transaction open since before step 4's snapshot                        | attach refuses once `--wait` runs out (a prepared one at once), naming what it waited on, and leaves `enforced`                                                                      |

## Rolling upgrades

The tenancy check, the placement call and the cell's state read, the CLI's org
declarations, `LOOM_CELL`, `loom cell prepare`, `scim.refuses_subject`, the email a
grant stores, `loom.cell_self`, the request gate and readiness check of a process's
fingerprint against its own row, the refusal of another cell's SCIM token, the SCIM
requirement, the heartbeat's mode, fingerprint and `LOOM_CELL`, and the Postgres
migration that adds the org triggers ship in one release, N; the migration is additive,
and the triggers do nothing on a deployment never prepared (no state rows, the `first`
row's rule), and prepare refuses while a process older than N is live; with `LOOM_CELL`
unset a process reads only the mode and `first` rows and whether a tenancy or own row
exists, and only under the Postgres authority, where a deployment never prepared has
none (N's migration adds the tables, empty), so builds N-1 and N serve side by side
unchanged. `loom cell attach` refuses until every live process runs N+1 or later, so the
one supported rollback from there (to N) still checks tenancy. The router routes by
host, issuer and the first cell, never by a cell's ops, so a cell's release does not
wait on the router's.

## Invariants

- An org is served by exactly one cell, the one its placement names, and its placement
  is never deleted or changed.
- A process with `LOOM_CELL` set answers nothing but its health probes and the probe
  route, and runs no worker, unless its configuration matches a value of its cell's own
  row, which the directory's overlap refusals have checked; a process with `LOOM_CELL`
  unset on a cell whose state rows count as `enforced` or are in a one-row state (the
  `first` row's rule) answers nothing but its health probes and the probe route and runs
  no worker.
- No bearer-token issuer, host, OIDC client, org pattern or bucket prefix is accepted by
  two cells, and no deployment id or router credential is held by two.
- Every security path fails closed, but for a registered cell with no state, tenancy or
  own row (its Postgres restored to before its prepare, or a prepare stopped before its
  row write), which is open until `loom cell restore` (after a restore) or a prepare
  rerun (after a stopped prepare) and named by `loom doctor` meanwhile: an unread row,
  mode or `first` value refuses; with the directory unreachable, placements, uncached
  lookups, the `loom cell` commands that read or write it and the recording cells' state
  reads refuse while each cell keeps serving the orgs it holds.

## Source entry points

- `server/core/cell.py`: the cell's own state (`loom.cell_first`, `loom.tenancy_mode`,
  `loom.cell_self`), its fingerprint and `gate_reason`, which the request middleware
  (`server/transport/model_maintenance.py`), `verify_token`, readiness, the workers'
  lease and the heartbeat read;
- `schemas/pg_migrations/008_cells.sql` and
  `schemas/migrations/094_cell_heartbeats.sql`: the cell's tables.

The rest of the cell code is added by the pull requests that implement this page. It
builds on:

- `server/auth/jwt.py` (`verify_token`, `AuthUnavailable`): where the fingerprint gate
  runs first;
- `server/auth/scim.py` (`tokens`) and `server/auth/scim_routes.py`
  (`scim_settings_problem`, `_authorized`): the SCIM token parser and checks;
- `server/core/instances.py`: the heartbeat that reports mode, fingerprint and
  `LOOM_CELL`;
- `server/workers/leader.py`: the lease a gated process stands aside from;
- `server/core/pg.py` (`same_deployment`): the deployment id both stores carry;
- `server/transport/gates.py` (`resolve_org`, `authorize`): where the tenancy check
  runs;
- `server/cli/_ch.py` (`run_connected`, `connect_checked`): the CLI's org declarations;
- `server/auth/authz.py` (`check_acted_for`, `_check_creator`) and
  `server/auth/oauth.py` (`_refresh_session`): the SCIM checks on acting and refreshing;
- `server/identity/app/scim_groups.py` (`_caller`): the second SCIM route.

## Rejected

- **A control plane shared by the cells** (one set of keys, grants and workers, the data
  split by org): a security check, a worker lease, an erasure, a policy generation and
  the audit chain would each span stores, and each split needs a protocol of its own.
- **Placing an org at its first request, before verification**: anyone could choose an
  org's cell, and a token minted in one cell could address an org placed in another.
- **A list of the paths that create an org, each placing it**: orgs come into being by
  more paths than a list keeps up with (gateway and federated tokens, bots, imports);
  the verified check is the one place every request passes.
- **Redirecting a flow from one cell to another midway**: a flow's client, codes and
  refresh tokens live in the cell that started it.
- **The router holding the identity provider's users for every cell**: each email's
  reach into each cell, a version that survives the directory's loss, pushes and pulls
  would each be a protocol of its own, where the identity provider already deprovisions
  every application a person is assigned to.
- **Every replica reaching every cell's stores**: a pool per replica per cell, the cost
  [A database per tenant](storage-authority.md#a-database-per-tenant) rejects.
- **Routing by namespace within an org**: an org-wide operation (its erasure, ids
  resolved across namespaces, sharing) would span cells; a large org gets a cell of its
  own.

## Tests

An integration test with two cells and a router in CI's Postgres job:

- a verified request for a new org at each cell places it once under concurrent
  requests, and the other cell answers `WRONG_CELL`; a gateway-minted org, an end-user
  issuer's `prefix*` org and a bot's org are placed the same way after `enforced`, and a
  platform principal's request for an unheld org places nothing;
- every route, the batch entries of MCP and every CLI command included, refuses another
  cell's org and writes nothing in its Postgres or ClickHouse (the route, op-field and
  CLI censuses cover the rest);
- the first attach places every org with a row in any table, an org first seen during
  the attach is placed in step 4, and so is one a direct Postgres insert wrote between
  step 2's snapshot and `enforced`, a ClickHouse row an older CLI wrote then and a
  bucket object catalogued then; a process past `LOOM_JWT_REVOCATION_MAX_STALE_SECONDS`
  of failed reads of its own rows or mode is gated and serves again once a read lands; a
  pod with `LOOM_CELL` unset is unready, naming the setting, answers 503 to a bearer
  request, passes its health probe, answers the probe route and runs no worker on an
  attached cell and on a later cell between prepare and attach, keeps serving on a first
  cell in `recording`, and keeps the last mode it read through a failed read,
  `recording` included after step 3 or step 4 returned the cell to it; a CLI command
  declaring an org with it unset is refused on an `enforced` cell, an org-free one (a
  token revocation) runs, also with Postgres down on a deployment never prepared, and a
  `loom cell` store command refuses; `loom cell limit --closed` succeeds with the cell's
  Postgres down; after `loom cell credential --cell <cell> --next` both credentials
  place and read state, `--finish` refuses the old credential, for a cell finished
  before its pods hold the new one the router refuses its calls with 401, its request
  answers 503 writing nothing, and it recovers by recording the old hash again,
  `--revert` drops a pending `next`, which is then refused, and the old credential keeps
  placing, and a second `--next` while one is pending and a hash another cell records as
  current or next are refused; `loom cell limit` and `loom cell credential --next`,
  `--finish` and `--revert` refuse while the directory is unavailable; concurrent
  placements at one under `placements_max` across two router replicas write one; a CLI
  command declaring an org with `LOOM_CELL_ROUTER_TOKEN` set refuses, naming it, on a
  first cell once it is `enforced` (`attaching` included) and on a later cell, and on a
  first cell in `recording` runs and records the org; attach with `placements_max` below
  the orgs the first cell holds places them all, and a later cell's attach past its
  quota or closed refuses, naming the org; a first cell with `--closed` and
  `placements_max` set before step 2 attaches, and an org a request records between step
  2 and `enforced` is placed by step 4 with no 503; after step 3's write of the cell's
  mode, a process whose cache still reads `recording` sends a new org's request to the
  placement call, which a closed cell answers 503; a CLI command whose insert finds
  `enforced` refuses, naming `loom cell place`, and makes no placement call; after step
  3's refused wait or step 4's recheck returns the cell to `recording`, a new org is
  recorded with no placement call, and one at a process whose cache still reads
  `enforced` places through the gate until the cache expires; each ordering of attach's
  write of the mode with a recording insert holds as stated above (running, starting
  during the write, a stream of overlapping inserts each shorter than a try, with attach
  setting `enforced` within a try or two, held across several tries, held until too
  little of `--wait` is left, a stalled attach, a commit cut off by its timeout, a
  commit whose connection is lost while it waits for the standby, read back only once
  that backend has gone and written again durably if it reads `enforced`); a
  `REPEATABLE READ` transaction whose first statement ran before `enforced` committed,
  on attach's role or a second role, holds step 4 until it ends and its write of an
  unheld org is then placed, one whose first statement ran after is refused by the
  trigger, a transaction that starts after attach takes the lock but before its commit
  is visible holds step 4, a transaction opened in `recording` that outlives one run's
  `--wait` holds the rerun's step 4, after a refusal and after `loom cell restore`
  raised the mode, an autovacuum worker on a loom table does not hold step 4 and a
  background worker with a transaction open since `recording` does, a prepared
  transaction (after a restart with `max_prepared_transactions` raised during the wait)
  makes step 4 refuse at once, naming it, with the mode still `enforced` and a rerun
  refused until it is resolved and the setting is back to 0, a write transaction open in
  a second database on the same server holds step 4 and is named, a long read on the
  synchronous standby, or on a second standby, with `hot_standby_feedback` on does not
  hold it, nor does a base backup running across it, a write held open on a
  `replication=database` connection holds step 4, and so does a `REPEATABLE READ`
  transaction on one, opened in `recording` and writing nothing before `enforced`, a
  long logical dump (a `REPEATABLE READ` read-only transaction) on the cell's database
  past `--wait` makes step 4 refuse, naming it, with the cell left `enforced`, and the
  first cell's attach without `pg_read_all_stats` or with `max_prepared_transactions`
  above 0 refuses before step 1 with the mode still `recording`, while a later cell's
  attach does not require either; a missing or disabled org trigger is refused by attach
  and named by `loom doctor`, a write for an unheld org on an `enforced` cell whose mode
  row was deleted is refused and the state named by `loom doctor`; on that cell a pod
  with `LOOM_CELL` unset is unready and runs no worker, and a CLI command declaring an
  org with it unset is refused; updating, deleting or truncating the `first` row on an
  `enforced` cell is refused, also under `session_replication_role = replica`, and a
  missing or disabled `first`-row trigger is refused by attach and named by
  `loom doctor`; a running process with `LOOM_CELL` set on a cell whose mode row is
  removed answers 503 to a bearer request and runs no worker pass, and serves again
  after a rerun of `loom cell prepare` writes the row back while it runs; a cell the
  directory registers with no state, tenancy or own row is named by `loom doctor`, its
  processes with `LOOM_CELL` set stay unready, a process with it unset serves as a
  single deployment, and `loom cell restore` writes both rows back, an attached cell
  then serving again and an unattached one after a prepare rerun; a cell with both rows
  absent beside a tenancy row, and one beside only its own row, has its triggers refuse
  an unheld org, every process gated and `loom doctor` naming it, and a prepare rerun
  writes the rows back; a Postgres-only restore of an attached first cell to before its
  prepare is named by `loom doctor` and closed by `loom cell restore`; a prepare rerun
  during a pending `loom cell update`, with processes on both values and
  `loom.cell_self` lost, writes both values back and keeps both sets of processes
  serving; a prepare rerun on an attached cell whose `current` is missing and whose
  pods' configuration differs from the directory row refuses, and one before attach
  whose heartbeats report another bucket prefix refuses; a prepare rerun given a
  credential hash that is neither the row's `current` nor its `next` is refused; a
  prepare rerun on a `serving` later cell whose prefix holds data writes a removed mode
  row back; a cell with a mode row and no `first` row has its triggers refuse an unheld
  org, every process gated and `loom doctor` naming it, and a prepare rerun writes the
  row back; a later cell whose mode row is set to `recording` records nothing, its
  trigger refuses an unheld org, a process with `LOOM_CELL` set there serves as in
  `enforced`, a pod with `LOOM_CELL` unset there is unready and runs no worker,
  `loom doctor` names the row, a CLI command declaring an org with
  `LOOM_CELL_ROUTER_TOKEN` set refuses, naming it, and the router refuses its state
  read; a build N-1 process's `memory.set` and delivery pass on a never-prepared
  deployment after N's migration succeed and write no tenancy row, and on the first cell
  in `recording` the trigger reads the mode and looks nothing up, a write under
  `session_replication_role = replica` for an unheld org is still refused, an
  `INSERT ... ON CONFLICT DO UPDATE` row for an unheld org is refused through the update
  trigger, and a census test fails on an org-keyed table without both triggers; and an
  org-keyed write while attach holds the mode table's lock returns at once, with new-org
  requests answering within their deadline and mode-cache reads returning at once
  throughout; `loom cell restore` after a refusal at step 3's lock leaves the cell
  `enforced`, and the resumed attach runs from step 3's wait; a `LOOM_CELL_ROUTER_TOKEN`
  not of the `lcr_` form is refused at boot and named by `loom doctor`;
  `loom cell credential new` prints a credential of that form and its SHA-256, with the
  directory and the stores unreachable; prepare and `--next` refuse an argument that is
  not 64 lowercase hex characters, the credential and its bare 128-character secret
  included, storing nothing; orgs a first cell records in `recording` with no data are
  listed by `loom doctor`; a placement flood with no quota set is held to the rate limit
  at each router replica, `loom doctor` names the cell as one with no quota, and a call
  refused past it at one replica answers 503 with a `Retry-After` of at most 60 s, after
  which its retry at that replica, with no other call of the cell admitted between, is
  admitted; a cell's first prepare, with no directory row yet, passes the deployment-id
  check; a pod or CLI that lost `LOOM_PG_DSN` or `LOOM_STORAGE_AUTHORITY` on a cell is
  refused by the authority check; the first cell's existing tokens, refresh tokens and
  OAuth clients keep working through the cutover, a logout through the shared host
  revokes its refresh token;
- attach refuses a shared issuer, root secret, Keeper path, nested bucket prefix or
  configured org, a second cell that holds data, and a second attach while one runs;
- an OIDC sign-in, an MCP client's authorization and a device flow for an org in the
  second cell complete at that cell's host after a lookup; `loom login --org` keeps its
  host on `NO_CELL` and on a deployment with no router, retries after a 429's
  `Retry-After` until `--wait` is spent and then refuses as a rate refusal, and, while
  the directory is down, starts its flow at the cell host of an org the router has
  cached and refuses, starting no flow, for one it has not;
- with SCIM on, a person the identity provider never assigned to a cell cannot sign in,
  refresh, be acted for or use a gateway token there, a deactivation pushed to a cell's
  application ends the person there, a platform-admin bot still reaches a cell whose
  SCIM deactivated everyone, and attach refuses a cell with unprovisioned live subjects
  or sharing another's OIDC client;
- an attach stopped in step 3 after it set `enforced`, before the wait passed, resumes
  with the wait before the SCIM precondition, and one refused by the SCIM precondition
  resumes from the wait too, the mode still `enforced`; a later attach refused before
  its directory transaction leaves `attach_started_at` empty, so a rerun checks a new
  cell's refusals again, and a resumed first attach finds its configured orgs placed in
  this cell and carries on;
- a cell restored to before the first attach's `enforced` gets its mode back from
  `loom cell restore`; one whose `loom cell restore` was skipped answers 503 for an org
  it does not hold, another cell's org included, writes no tenancy row for it, and is
  named by `loom doctor`; a directory outage during the `LOOM_CELL` rollout and between
  prepare and attach leaves the first cell serving the orgs it holds, a new org
  answering 503 at every process with `LOOM_CELL` set; the state read is accepted from a
  first cell that is `attaching` before attach's step 2, and only with the named cell's
  own credential, and a refused or late answer counts as unreadable;
- a grant refreshed at N that was written at N-1 is checked against the person's row, a
  refresh while SCIM is unreadable answers 503 and keeps the session, and a process with
  `LOOM_CELL` set before `loom cell prepare` stays unready;
- a token delegated during the first attach by a person never provisioned is refused
  after `enforced`; a gateway acting for a bot is served and for a bare handle refused;
- with the directory unavailable, every cell serves the orgs it holds, placements answer
  503, and `loom doctor` on a cell reports its directory checks as not run and exits
  non-zero;
- `LOOM_CELL` rolls out after `loom cell prepare` with every pod ready, before attach;
- preparing an empty cell before the shared host's deployment is refused, and a cell
  restored from another cell's snapshot is refused at prepare;
- a prepare killed between the directory write and the cell write, with `LOOM_CELL`
  rolled out, is rerun through `probe_url` against gated processes; a prepare rerun
  before attach whose heartbeats differ from a present `current` refuses; an attached
  prepare rerun with only `next` lost writes it back, and one whose present `current`
  differs from the directory overwrites it; a prepare rerun alone after a Postgres-only
  restore of an attached first cell to before its prepare leaves `loom doctor` naming
  the missing tenancy rows, and a pod with `LOOM_CELL` unset serves every org there
  until the rerun and is gated after it, while with `LOOM_CELL` set an org gets its
  tenancy row back through the placement call; a prepare rerun against `LOOM_CELL`-unset
  pods in a one-row state reaches their probe route and writes the missing row back;
  prepare refuses a later cell's `probe_url` that reports an id other than its stores',
  attach refuses a cell whose host reports an id other than its `probe_url`'s, and
  prepare refuses a probe address argument for the first cell and reads only
  `upstream_probe_url`; a later cell's first prepare succeeds;
  `loom cell update --probe-url` refuses an address that reports another deployment id
  or fails the nonce echo, and refuses the first cell; `probe_url` with userinfo or
  another scheme is refused; the main port answers only the in-memory id, with any Host,
  reading no store; the probe listener answers 429 past its rate; and a prepare given a
  copy of the first cell's stores is refused by the nonce;
- the default first cell (`LOOM_JWT_ISSUER=loom`, an empty `LOOM_OAUTH_ISSUER`)
  attaches; a cell redeployed with the shared host as its public URL becomes unready;
- a placement inserted at a cell's quota whose call timed out is retried and answers its
  winner, and the cell writes the tenancy row; the same on a cell closed meanwhile, and
  for a `loom cell place` rerun;
- an attach that dies between its directory write and its `loom.cell_self` write resumes
  to `serving`, and its issuers stay reserved meanwhile; a `loom cell place` for an org
  another cell holds writes no row, and one for an org the directory does not place is
  refused, writing no row, for a closed cell, past `placements_max`, for a later cell
  still `attaching`, for a first cell before the directory records `enforced`, with a
  configuration whose fingerprint matches neither value of the cell's row, and while the
  directory is unavailable, and is neither refused once the cell's rate is spent nor
  counted toward it (the cell's own placement calls are still admitted after a run of
  operator places); one for an org the directory places in this cell writes its tenancy
  row on a closed or full cell; `loom bot create` for a new org on a first cell in
  `recording` records the org while the directory answers `attaching` and is refused
  while it answers `serving` or cannot be read;
- an OAuth issuer host that differs from the cell's public host is refused at prepare,
  and the shared host routes before any cell is attached; a `loom cell place` with no
  other writes keeps the org in its cell;
- a first cell restored to between attach steps 1 and 3 after it is `serving` becomes
  ready in `enforced` once `loom cell restore` has run;
- a cell restored to before a `loom cell update --finish` serves again once
  `loom cell restore` has rewritten its own row, a cell whose two stores are both
  restored gets its rows back from `loom cell restore` alone, and a prepared, unattached
  cell keeps its restored own row;
- a first cell restored to before its prepare gets its rows back, answers 503 on the
  shared host at processes with `LOOM_CELL` set until `loom cell prepare` is rerun, and
  then serves; SCIM on a deployment with `LOOM_CELL` unset works as before at N, through
  prepare, the `LOOM_CELL` rollout and attach;
- the first cell's SCIM keeps answering its unnamed token through the cutover, a later
  cell refuses an unnamed token, no cell accepts another cell's token, `lsc_b_…` and a
  malformed `lsc_` token are refused in the first cell, and a later cell with a SCIM row
  written before `LOOM_CELL` is refused at attach; a token of cell `a-b` is refused in
  cell `a`, and a cell name with `_` is refused;
- a later cell with `oidc_jit_org` set boots and restarts its pods between prepare and
  attach, JIT for that org answering 503 until attach places it; a first cell's
  self-signup into an org no request has recorded, between prepare and attach, records
  it and signs up; a later cell with indexed repositories writes no code row before
  attach and then attaches; a first cell in `recording` keeps its worker passes for an
  org no request has touched since the rollout; a later cell's attach stopped after
  `attach_started_at` resumes with its configured orgs placed and held; an update
  between prepare and attach that changes the code org has that org placed by attach,
  and after `--finish` it indexes; an update after attach refuses a changed configured
  org another cell holds, naming it, and, for an org the directory does not place yet,
  is refused for a closed cell and past `placements_max`; an update changing a
  configured org of a later cell still `attaching` after its directory transaction is
  refused until the cell is `serving`; `--revert` keeps a configured org the update
  placed; a later cell's code and tracker orgs serve and index once it is `serving`, a
  `code.*` op from another org's member included, and so does a configured org
  `loom cell update` changes; a later attach racing another cell's update that widens an
  issuer refuses one of them; a configured org another cell places between attach's
  refusals and its placement refuses the attach, naming it, and writes nothing; prepare
  refuses a router credential another cell records; a placement call that names another
  cell with a valid credential of this one places nothing in the other;
- a prepared, unattached cell's placement call for an org the directory does not place
  answers 503, one for an org it places answers the winner even with the old credential
  still cached after `loom cell credential --finish` or a fingerprint matching neither
  value, a winner of this cell's name under another deployment id answers 503 and writes
  no tenancy row, and a process whose configuration differs from its cell's row places
  nothing;
- a cell attached with SCIM off and switched on by `loom cell update` refuses while an
  agent-signup token is live, one minted while the update rolls out included, and a
  process configured with another cell's issuer answers 503 to every bearer request,
  reached through the router or directly;
- during a `loom cell update`, processes on the old and the new configuration both
  serve, `--finish` releases a narrowed issuer only after no process accepts it, an
  update that dies after its directory write keeps the widened issuer reserved, and
  builds N and N+1 with one configuration match the same row;
- rotating a SCIM token changes no fingerprint; a process refuses to boot with another
  cell's SCIM token, an identity provider pointed at the wrong cell's host gets 401 and
  writes nothing, and a cell rolled back to N still gates on its own row and refuses
  another cell's SCIM token; a placement flood from one cell's issuer is refused past
  its quota;
- a gateway acting for the personal service account of a person never provisioned is
  refused, and a cell that starts accepting another cell's issuer takes no placements;
- a process matching neither value of its row answers 503 to sign-in, refresh, the OAuth
  token and device endpoints, the sign-in callback and signup, and its probe route still
  echoes prepare's nonce; `--revert` waits for processes matching only `next`;
  `update --config` refuses while a different `next` is pending, and an update that died
  before its `loom.cell_self` write resumes forward; prepare refuses while a pre-N
  process is live; a configuration change while `attaching` goes through
  `loom cell update` and the resumed attach reserves both values; a process whose
  configured stores differ from its cell's row answers 503 and stands aside from the
  workers, so a gated newer build beside an ungated older one leaves the workers running
  and erasing; `loom cell update` refuses a change to the deployment id, the bucket name
  or the prefix, adding a bucket included; a gated process in `on` mode runs no worker
  pass; preparing a clone with the first cell's bucket name and prefix is refused, on
  its endpoint or any other; a later cell whose prefix holds another deployment's
  objects is refused at prepare; two concurrent prepares of nested prefixes do not both
  pass; two cells without a bucket both prepare; prepare refuses a later cell whose
  process carries another cell's `LOOM_CELL`, and a process whose `first` row names
  another cell answers 503 and runs no worker; a flood on the main port's id route does
  not stall attach; and a cell restored onto a new Postgres, ClickHouse or bucket
  endpoint serves once its configuration names it; a bearer request on the shared host
  is served before prepare, between prepare and attach, and during attach;
- the chart renders no probe listener by default, and with it on renders the probe
  Service and changes no NetworkPolicy, so with the chart's policy on no rule of its own
  admits the probe port; prepare names an unreachable `probe_url` as a connection
  failure and a 429 that outlasts `--wait` as a rate refusal; a flood on the main port
  leaves the probe listener's rate untouched;
- a later cell started from the first cell's environment, with its bucket and prefix,
  runs gated from its first boot and deletes no original; prepare refuses a later cell
  while a live process lacks `LOOM_CELL`;
- `loom cell limit --closed` makes the cell's placement calls answer 503 while it keeps
  serving the orgs it holds, `--open` restores them, and `--placements-max` takes effect
  at the next placement call;
- a Postgres restore of an `enforced` cell to before an org's placement, with that org
  erased after the point, reconciles to exit 0 after `loom cell restore` and then serves
  the org;
- a cell restored from before an org's placement serves it once ready, an erasure leaves
  the org's placement, and with the directory lost every cell serves the orgs it holds.
