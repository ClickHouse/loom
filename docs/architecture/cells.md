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

## Participant lifecycle baseline

A participant advertising the ninth `host_proof` field `cell-retirement-v1`
enrolls before its first fresh primary acceptance read and uses permanent guarded
retirement. A serving cell heartbeat includes its `participant_id` beside the
fingerprint, allowing a replacement to be joined to the participant registry.
Workers children enroll independently and do not publish serving instance rows.
Native commands hold one admission scope through their complete org or principal
work and drain: claim lookup and release share that scope; token readiness, keyring
read, signing and audit share another. Connections and helper calls do not retire
a command that still has principal work to perform. Store pairing is verified
before enrollment and fresh acceptance. Org-free diagnostics do not install an
accepting view. The pre-schema `ch-setup` ownership probe discards its diagnostic
mode result and checks only store/cell identity before raw database user/profile
setup; its optional analyst-row work opens a separate verified, enrolled scope
and obtains fresh acceptance. That bootstrap exception admits no tenant or token
work.

Local physical-death evidence requires the same host, kernel boot and PID namespace.
It accepts an absent PID, a different process birth, or the captured birth in a
single fresh kernel process record showing a zombie with exactly one thread. A
zombie leader with other threads is unproved. Known-zero tagged queries remain a
separate requirement before retirement; kernel process exit does not settle a
query still executing in a store.

Permanent completion drains application work and buffered writes before sealing
Postgres openings. Pool construction, connection initialization, acquisition and
native control tasks remain owned until they actually finish. Completion closes
its pool within the remaining shutdown deadline; a later disconnect does not
start another cleanup wait.

Under the Postgres authority, every authority session carries the participant
UUID in its startup `application_name`, including dedicated and unmarked sessions.
Retirement verifies the deployment and epoch against the exact ClickHouse and
Postgres pair, then requires no other backend with that participant tag. Idle
backends count too. The metadata verifier excludes only its own backend and must
finish closing within the same deadline. Local abort, cancellation and an
unacknowledged close leave the participant unretired; later positive backend
absence can resolve it. Manual retirement, reaped-child recovery and dead-process
sweeps use the same store checks. The proof relies on the deployment's existing
[primary endpoint and fencing requirements](../operations/postgres.md#self-hosted-postgres).

Builds without participant session tags use the generic authority application name
`loom`. Recertifying their retained identities requires all such Postgres backends
to be absent, including idle pools; stop and drain the remaining legacy authority
clients before that census. Unrelated tagged participants can remain live. A
deployment whose verified, immutable authority is ClickHouse needs only its
ClickHouse settlement checks, including during recovery of a legacy participant.
A missing authority record or a mismatched configuration never selects that path.

The capability identifies installed code; it does not prove that every older
process or unsafe retirement client has disappeared.

Before online configuration finalization is supported, stop every unsafe manual
retirement and pruning writer and prevent its binary/image from restarting.
Inventory and positively terminate every accepting server, workers child,
standalone worker and native command that overlapped those writers. Include paused
boots, already retired/pruned identities and lifecycle-capable processes started
during the overlap: old maintenance code could have hidden their rows too. Only
then start replacement accepting and maintenance processes with this lifecycle.
A shared image tag, pod deletion, readiness or heartbeat expiry is insufficient.
Keep exact PID/boot/namespace/start or trusted runtime termination evidence with
the deployment's upgrade record. No CLI flag or stored acknowledgement substitutes
for that evidence, and the runtime census does not verify the external inventory.

Census retained legacy rows while unsafe writers are stopped. Recertify a retained
identity only after exact verifiable physical death and positive settlement in
every authority store. Keep empty or unknown proofs quarantined. If the legacy
inventory is incomplete, stop all accepting/native-command and retirement/pruning processes,
positively terminate or fence every runtime that could execute an old binary, and
prevent old restarts. Only after that physical quiescence may the operator request
an exact audited cleanup of selected unknown legacy participant identities. There
is no automatic age-based cleanup or affirmation bypass. Restart only guarded
lifecycle processes afterward. Unknown or partitioned runtimes remain blockers.

This baseline is an operational prerequisite, not a release-cut requirement.
Configuration finalization also needs the directory's controller-fencing protocol;
the lifecycle certificate alone cannot authorize reservation release.

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
  resumed attach reads), its tenancy mode and when it was set (prepare writes the first,
  and attach records each it raises or tries to raise), whether it is closed to new orgs
  and its placement quota (`placements_max`), the SHA-256 of the credential it calls the
  router with (`current`, and during a rotation `next`), its root-secret fingerprint
  (`keys_fp`), its `probe_url` (the router's own for the first cell), the names and key
  thumbprints of its break-glass bots, and its configuration fingerprint (`current` and,
  during a change, `next`; the fields named under "Checked continuously"), which attach
  and `loom cell update` write together with the cell's own copy of that fingerprint
  (`loom.cell_self`);
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
   handled by rules 2 to 4 (rule 4's cell is the pass-through upstream, before the first
   cell is registered and after, since prepare registers it at that address), never
   answered 421; any other host that no `cells` row names answers 421. Every URL a cell
   hands out names its own host: its OAuth and OIDC endpoints, the device flow's
   verification URI, the sign-in callback (`LOOM_OAUTH_ISSUER`, registered at the
   identity provider per cell), its JWKS, the dashboard's pages and assets, and its MCP
   resource metadata. MCP clients of a cell other than the first use the cell's host,
   which the cell's setup lines print.
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

The router reaches a cell at the `upstream_url` its directory row records, an address
inside the deployment's network (the first cell's is `LOOM_ROUTER_UPSTREAM_URL`, written
by prepare, so registering it changes no route; a later cell names at prepare), never at
the cell's public host, which names the router itself; before the first cell is
registered, rule 4's cell is the deployment it was put in front of
(`LOOM_ROUTER_UPSTREAM_URL`). The shared host is `LOOM_ROUTER_SHARED_HOST`, the first
cell's own host and that cell's `hosts` aliases. It forwards each request as the client
sent it (its raw path and query, its own `Host`) with the router's own view of the
client's address, scheme and host in `X-Forwarded-For`, `-Proto` and `-Host`, replacing
what the client claims there (so each cell lists the router's addresses in
`LOOM_TRUSTED_PROXIES`, or it counts every client as the router for its sign-in limits
and reads the scheme as the router's), drops the headers that describe one connection
(and those `Connection` names), keeps every header line of the answer (a second
`Set-Cookie` included), streams both bodies (a server-sent event stream included),
answers 502 when the cell cannot be reached, and never forwards a path under `/cells/`
(one it does not serve answers 404). A registered first cell with no upstream address
answers 503, as any cell's does. It reads the cells, their issuers and hosts once per
`LOOM_ROUTER_DIRECTORY_CACHE_S`, answering from the last table while a stale one is read
again behind it, so a directory that hangs or cannot be read delays no request; before
its first read, it answers 503. An issuer two cells list (which attach refuses) routes
to neither.

A client finds an org's cell with `GET /cells/lookup?org=<org>`, unauthenticated and
rate-limited; an org with no placement answers the first cell with the same body as an
org placed there (only a placed org's answer is cached, so timing and an outage can
still tell them apart: an org name is not a secret), and before any cell is registered
the lookup answers 503 with the code `NO_CELL`; a directory it cannot read for an org it
has not cached answers 503 with `BACKEND_UNAVAILABLE`. A client matches the code, never
the message: on `NO_CELL`, and on a 404 from a deployment with no router,
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
treats any mode but `enforced` as `enforced` in its org triggers, its mode cache and
its CLI, so a later cell's mode row set to `recording` records nothing. A cell in
`recording` reads its own state with `GET /cells/v1/cells/<cell>`, with its router
credential, answered only for the cell the directory's unique index names first and
accepted only when the credential's SHA-256 is one of the hashes the directory records
for `<cell>`, within `LOOM_CELL_ROUTER_TIMEOUT_S` (default 5 s, the placement call's
timeout); a refused, failed or late answer counts as unreadable.

With the directory unreachable, rules 1 to 4 route from the router's cached cells; a
lookup of an org it has not cached and a placement answer 503.

The operator's routes, each with the router's operator credential, are under
`/cells/v1/operator/cells/<cell>`: `limit`, `credential` and `register` (prepare's), the row
itself (`GET`, with the name of any other row, which attach reads for its refusals, its done
check and its resume), the first cell's attach (`attach/begin`, `attach/place`,
`attach/enforced` and `attach/serving`, in that order), and `DELETE`. Each but `register`
answers a cell the directory does not hold 404 with the code `NO_SUCH_CELL`, so a 404
without it is a router replica of an earlier build. Every call that succeeds, the read
included, is written to the audit log (a registration rerun, which changes nothing, writes
none), and every call runs a fixed number of statements, each bounded by the directory's
statement timeout (enforced on the directory's server too); the read and the removal carry
the derived bounds `loom cell` waits for. Every change takes the directory lock and then the
cell's row lock (the order registration, the operator changes and the removal take). Every
router replica runs a build with these routes before a first cell's attach starts: an
earlier build's placement collides with an attach batch's as a transient 503, and its
registration can add a later cell mid-attach, which every attach call then refuses until it
is removed. Each attach call refuses unless the row is the first cell's, `attaching`, and
records the deployment id the call names, and while any other row exists (registration
refuses a later cell until the first is `serving`, and a router replica of an earlier build
could still register one):

- `attach/begin` carries the cell's fingerprints exactly as `loom.cell_self` stores them,
  its root-secret fingerprint, the allow lists attach judged the stores by, and fresh
  nonces. It refuses a fingerprint whose deployment id, bucket, prefix, public host or
  issuer host differ from the row's (an issuer host other than the cell's own is refused),
  and one whose live placement body, with an org name at its longest, would pass the
  router's body bound, naming the largest fields. Outside any directory transaction it runs
  registration's probe at the row's `probe_url` and asks the row's `upstream_url` for the
  deployment id, so the stores attach reads are the ones the live cell serves from. Then,
  under the locks, it writes `attach_started_at` once, the fingerprints only while the row
  has none (other fingerprints there answer 409: they change with `loom cell update`), and
  `keys_fp` on every call, and audits the allow lists. It is idempotent. Its body has its
  own bound, 256 KiB, past which it answers 413.
- `attach/place` places a batch of at most 500 orgs (within the 64 KiB body bound) once
  `begin` has run and the directory mode is `enforced`, past the cell's closed mark and
  quota. It runs a fixed number of statements: one insert of the batch that skips an org
  placed already, one read refusing the batch (rolled back, naming the org) when an org is
  placed in another cell, one count, and one audit row per org it placed and one for the
  call. A live placement of the same org runs beside it once the mode is `enforced`; both
  hold the cell's row lock, a live placement reads the winner again once it holds it, and
  every insert skips an org placed already and counts only what it wrote, so the org is
  placed and counted once.
- `attach/enforced` records the directory mode `enforced`, after which the first cell places
  new orgs live; it is idempotent.
- `attach/serving` runs the probe and the upstream check again, then, under the locks,
  computes the row's issuers from its fingerprints (`bearer_issuers` of `current` and
  `next`) and marks the cell `serving`; on a `serving` cell it answers at once, audited.
  `begin`, `place` and `enforced` on a `serving` cell answer 409.

`DELETE` removes the row of a cell that is `attaching` with no placement, whatever its
directory mode, with its host rows, audited: a first cell is backed out until its first
placement, and a later cell registered by an earlier build stops blocking the first attach.
A placement in flight either commits first, and the removal refuses, or finds no row. The
router cannot read the cell's stores, so `loom cell remove`, which calls it, checks a first
cell's Postgres first ([Operations](#operations)).

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

A cell in `enforced` with SCIM on admits a person only while its SCIM data
qualifies that person's complete identity graph. `scim.refuses_subject` checks the
primary subject, token creator, every parent and any acted-for subject together, in
the concrete target org before grants or placement. A wildcard token supplies no
guessed org; the concrete operation checks the complete graph again.

A person's signed email and the verified email from their OIDC sign-in are positive
ownership witnesses. In the target org, the check resolves one normalized owner from
an active account's owner or its verified contact pair (`VERIFIED_EMAIL_SQL`). Two
different owners refuse. An email-shaped canonical owner is both mandatory and a
positive witness. Every contact on every active account of that owner is mandatory,
including an unlinked password contact; that unverified contact cannot supply a
positive witness or resolve another owner. At least one positive witness is required.
Every mandatory address needs its own active, undeleted `scim.user_id(email)` row.
Only a missing own row may pass as a work address of an active primary row belonging
to this person's positive witnesses. An inactive or deleted own row always refuses.
One person's verified email never qualifies a different root or service-account
ancestor.

Exemptions are closed: an active registered bot and its agents, including machine or
workload exchanges backed by that registration, and a signed end user whose issuer
and end-user metadata were verified. A prefix, an admin role or the absence of email
creates no exemption. An actual bearer naming another `sa:` subject uses the current
personal service-account row: it must exist, remain unrevoked and unexpired, and have a creator. Its creator
and every persisted lineage subject must qualify, even when grant inheritance is
disabled. This security read bypasses the grant-inheritance metadata cache. Cycles
refuse; a shared ancestor completed on another path is checked once. A trusted caller
acts for an `sa:` subject only when it names an active registered bot or its agent;
a personal row cannot prove the token's, session's or parent credentials' liveness.

One decision allows at most 1,024 supplied roots, distinct subjects, examined lineage
edges, account rows and distinct emails in each dimension. Repeated root entries
count before deduplication, and repeated lineage edges count as examined work. Input
and serialized query parameters each fit 65,536 bytes; account and ancestry bytes
transferred across the decision share that bound. SQL returns overflow counts and
bounded projections rather than accepting a truncated prefix. Unsupported sizes
refuse before admission. The traversal costs O(S + E) for S subjects and E examined
edges, plus the bounded account data; resolving an owner still scans that org's
account rows.

A complete cold decision has one finite 30-second deadline, reduced by a smaller
positive `LOOM_CH_TIMEOUT_READ_S`. Setting that field to zero leaves general reads
unbounded but does not remove the SCIM deadline. Queries use the remaining decision
budget through the normal ClickHouse statement limits; a tighter scoped or query-class
limit still applies. A read failure or deadline raises `SignInUnavailable`, which
becomes retryable 503 rather than deactivation. The refresh path checks this policy
before rotating the provider's credential and puts the same token and family back
on an outage. Positive refusal and an unavailable read are different outcomes.

Only a completed whole-graph result is memoized, in authorization's existing final
memo. Its key includes deployment and cell identity, concrete org, exact primary
subject, normalized verified email and canonical additional roots. No inner subject
or ancestry cache supplies a security answer. `LOOM_GRANT_CACHE_TTL_SECONDS` is counted
from the complete read's start, not its finish or a cache hit. Identity, account,
link, machine and lineage writes invalidate through the existing forget hook before
the attempt and in `finally`, so an overtaken read cannot install its stale result.
Across processes, an already-running decision can observe a change within at most
`max(grant_cache_ttl_seconds, decision_s)`, where `decision_s` is the finite complete
decision budget. This is not a globally atomic identity snapshot; ordinary revocation
checks retain their separate rules. Standalone deployments and cells without the
required policy retain deactivation checking without installing a positive-provisioning
permit. The global cell gate still refuses unknown or unread identity.

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
secret of at least 32 lowercase hex characters. The offline command draws 32 random
bytes and prints 64 lowercase hex characters; a configured token's length alone does
not establish its randomness.

The pure parser and configuration policy in `server.core.scim_tokens` share the token
splitter exposed as `scim.tokens()`. Core admission and authentication use the same
policy without a second identity query or cache. Callers compare the token's name
with `LOOM_CELL` and the full immutable `first` tuple (below).
`scim_settings_problem` takes both, so boot and `loom doctor` refuse through one
function, and with `LOOM_CELL` set they refuse a third configured token (two allow a
rotation). One function, `scim.authorized(presented)`, holds the constant-time
comparison for both SCIM routes (`_authorized`, `scim_groups._caller`). With `LOOM_CELL`
unset it compares against the configured tokens as a single deployment does and reads
nothing. With it set it compares against the tokens the parser admits for `LOOM_CELL`
and the `first` row; while that row is unread it raises as `AuthUnavailable` does, so
the routes answer 503 with `Retry-After` and the identity provider retries.

The cell admission gate derives token policy before returning a cached readiness
verdict. A late first successful identity read cannot admit an invalid configuration.
Server and worker startup check known identity before starting writers; unknown
identity keeps the process unready while its deployment probe remains available for
prepare. Doctor grades policy from its own bounded state query, including both fields
of the first tuple. Unread identity fails the configured SCIM check.

Store generated tokens in the operator's secret store and configure the identity
provider for that cell's host. To rotate, configure current and replacement tokens,
switch the identity provider to the replacement, verify requests, then remove the old
entry. Two configured entries are allowed per cell, including duplicate entries in
that count. Standalone deployments retain their existing configured-token behavior.

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
address. That address is the router's own configuration
(`LOOM_ROUTER_UPSTREAM_PROBE_URL`) and the first cell's `probe_url`, never one of
prepare's arguments: the router reads it there at every prepare and rerun, and refuses
an answer with another deployment id or none. The router also asks its pass-through
upstream's main port (`LOOM_ROUTER_UPSTREAM_URL`, which registration copies into the
row) and refuses one that reports another deployment id, so the row never names two
deployments. Prepare refuses every other cell until the first is registered and `serving`,
under the directory lock, so no later cell appears while the first attaches.

**The stores are the live ones.** Before writing anything, prepare writes a random nonce
into the target Postgres, under a reserved key of the target ClickHouse's
`loom._migration_lock`, and as a probe object under the target bucket prefix. The cell's
probe route must echo each back as it reads it from its own connection to the primary on
that request. The nonces are then deleted; their rows hold no memory and no org and are
classified so. A copy with the same deployment id is refused. Prepare sends the router
the three nonces with the registration (`POST /cells/v1/operator/cells/<cell>/register`,
with the operator credential); the router reads the probe listener itself and registers
the cell only when it answers the cell's deployment id, those nonces, and the bucket and
prefix the registration names (so one naming no bucket cannot pass for a cell that has
one). The router answers a listener past its rate (429) as `PROBE_BUSY`, and one it
cannot reach or that cannot read a store, or a cell's upstream main port it cannot
reach yet, as `PROBE_UNREACHABLE`, both 503, and prepare waits for them within `--wait`.
A rerun whose addresses differ from the row's is refused before the probe.

**The probe route**, `GET /v1/health/deployment`, takes no credential and is outside the
fingerprint gate:

- On the main port it answers only the deployment id each process verified at boot, held
  in memory, whatever the request's Host. It reads no store and is not rate-limited, so
  no flood on the main port stalls attach's host check.
- In full, with the values under prepare's reserved nonce keys, it is served only on a
  separate probe listener port (`LOOM_CELL_PROBE_PORT`, `0` for off; it needs the
  Postgres authority and authentication on, and binds every address of the host): the
  Postgres row `loom.cell_probe` and the KeeperMap key, each named `cell-probe`, and the
  object `<prefix>/_cell_probe`; it also names the bucket and prefix it stores under.
  None of these values is secret. It answers 429 past a fixed rate per process on that
  listener.
- Only the cell's probe address (`probe_url`) targets the listener: an internal address
  that no ingress exposes and the router never routes to, which the deployment sends to
  every live process, ready or not, so prepare and attach reach a process the gate holds
  unready.
- The chart's probe listener is off by default and is turned on, in the existing
  deployment too, before the first `loom cell prepare`. It renders a Service on that
  port that publishes unready addresses and changes no NetworkPolicy. Who may reach the
  port is the operator's network policy, as it is for ClickHouse's: with the chart's own
  policy on, which admits only the `http` port, the operator adds a policy admitting the
  probe port from the router's pods (prepare's registration is probed by the router) and
  from where the CLI runs (policies add up), and without one any pod can reach it.
  Likewise the cell's egress to the router's placement address and to Postgres is the
  operator's, through `networkPolicy.extraEgress` where the chart's policy is on.
- Prepare retries the router's `PROBE_BUSY` and `PROBE_UNREACHABLE` (a listener starting
  up is unreachable a while), and attach and `loom cell update --probe-url` a 429 from
  the listener they probe, for at most `--wait` (default 10 minutes), and then name it.

A later cell requires both `--probe-url`, its separate probe listener, and
`--upstream-url`, its internal main-port HTTP(S) origin. Both use the same address
normalization: no userinfo, query, fragment or non-root path. The router verifies the
three-store nonce response at the probe listener, then checks the proposed main port
reports the same deployment id without listener-only nonce fields. It records the
verified main origin in the registration transaction before any local prepared-row
acknowledgment. The first cell accepts neither a caller-supplied upstream field nor a
probe address; its addresses remain the router's configuration.

Every router must support this registration input before preparing a later cell.
An unknown field or a response missing the exact verified origin refuses; the client
does not retry without the field. A rerun preserves the exact registered origin,
including refusing a registered later cell whose origin is blank. It never fills or
moves that address. Surviving local state is not cleared to bypass this refusal.
Registration immediately invalidates this replica's credential and routing caches,
even if the request is cancelled after commit. An in-flight older routing read cannot
mark its table fresh after invalidation; the last table remains available during a
directory outage. Other replicas retain their ordinary refresh bounds. Unknown commit or
reply outcomes stop the invocation before local writes; a fresh normal prepare rerun reads
and verifies the actual row.

This preparation contract does not implement later-cell attach or a read-only attach
preview. Those paths still require their own peer, automation and state-transition
proofs. Preparing an origin is not an attach-success or public-readiness result.

`loom cell update --probe-url`
replaces a later cell's under the directory lock after the same check and a fresh nonce
echo. The first cell's probe and upstream addresses are copied from the router's
configuration at registration, and `loom cell update` replaces them from it, once every
router replica runs the new values, after the same probe and a check that the new
upstream reports the cell's deployment id. The generation/address CAS does not prove
all-router agreement: an old router can read fresh expected values and reverse the
addresses. Keep old endpoints reachable until every router refreshes its routing table
or restarts after the directory write; config rollout alone does not flush the cache,
which can persist indefinitely during directory failure. A prepare rerun never moves
an address: it refuses while the router replica it reaches is configured with others.

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
of it (which reads the directory, and ships with the router's read-only scope) is the only
signal. A
`(true, …)` row with `recording` and the own row: the first cell recording. Both present
with the own row otherwise: `enforced`, a later cell's mode reading as `enforced`
whatever its row says (its triggers, mode cache and CLI treat it so, and
`loom cell prepare` names a row that is not). Either row alone, both without the own
row, or both absent beside a tenancy or own row (the `partial` states: some state row
present but not all of the `first`, mode and own rows), which prepare never leaves: the
org triggers, where a cell has them, act as in `enforced` (a boot creates none in it),
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
whether SCIM is on, the bearer issuers it accepts (`trusted_issuers()` and its end-user
issuers), its configured orgs, the `orgs` of its `jwt_end_user_issuers_json`, the
deployment id it verified at boot, and the bucket name and prefix). Its Postgres and
ClickHouse both carry the deployment id (the pairing check a single deployment already
runs). The bucket, when one is set, is identified by its name and prefix (a cell without
a bucket takes part in no bucket check), which no two cells share on any endpoint: no
cell's prefix in a bucket of the same name nests inside another's or holds it, so an
endpoint taken from the environment cannot land in another cell's prefix. No store is
identified by its address, so a standby replaced, a failover, an address taken from the
environment or a restore to a new endpoint changes nothing the cell checks. The
deployment id, the bucket name and the prefix never change after prepare:
`loom cell update` refuses a change to any of them, adding a bucket to a cell prepared
without one included, and moving a cell's data to other stores, or adding one, is a
design of its own (Operations). Bots are not in it: `loom bot create` adds them at run
time, each with one org, and placement already bounds them. The cell's own row in
`loom.cell_self` holds an accepted fingerprint, `current`, and during a change a second,
`next`. Its rows, and the cell heartbeat (`loom.cell_heartbeats`, keyed by the process's
`loom.instances` row and written with it, since a KeeperMap table takes no new column:
the `LOOM_CELL` it runs with, the state its last read makes, its fingerprint and
[the keys beside it](#the-heartbeats-keys)), hold no memory
content and are classified so for erasure. A process matches a value when every field
both know is equal, a field the value lacks counting as equal only at its default, so
builds N and N+1 with the same configuration match one row. With `LOOM_CELL` set, while
its cell's own row, `first` row or mode row is unread, the state rows are in a `partial`
state (the `first` row's rule, above), the cell's own row is absent or matched by
neither value, or the `first` row names another cell, every request it receives but its
health probes and the probe route answers 503 `AuthUnavailable`: bearer requests (the
first check in `verify_token`, before the end-user issuer branch), SCIM, and every route
that signs in, mints, refreshes or exchanges a token, signup, the OAuth and device
endpoints and the sign-in callback included. It also reports itself unready, and it
stands aside from the workers as a drifted process does: it stops its passes, gives up
the lease and names no lease in its instance row, in every `LOOM_BACKGROUND_WORKERS`
mode (`on` included: with no lease to keep, a cell's `on` process runs the lease loop,
which starts and stops the supervisor on the gate within `LOOM_TENANCY_MODE_CACHE_S` and
one loop period), and its instance row does not count it as serving, so an older build
beside it keeps the workers running. A failed read keeps the last rows for at most
`LOOM_JWT_REVOCATION_MAX_STALE_SECONDS`, as the signing keyring and the revocation
snapshot do; past it the process is gated until a read lands. The mode (below) has the
same bound. A cell configured to accept another cell's issuer, a host it was not
attached with or an org pattern it was not granted therefore serves nothing with it,
wherever the request reaches it. Its heartbeat reports the fingerprint, the router
refuses a placement call whose fields match neither value of the directory's row, and
`loom doctor` names a mismatch, a cell whose OIDC client another cell shares, and a cell
with SCIM off beside cells with it on.

`loom cell prepare` validates every live process's deployment, bucket and prefix
against its native configuration. Missing heartbeats or differing configurations
refuse a new own row; a present own row requires every process to match its current
or next value. The local first and mode rows retain their normal preparation rules.

For the first cell, the pair stays local until first attach reserves it in the
directory. For a later cell, prepare selects that same process/local pair before
probing and publishes it in the normal registration transaction. Each non-null value
must be readable, name the registered stores and public host, fit the placement envelope,
and avoid every other cell's current and next issuer, OIDC, pattern and configured-org
reservations. The bounded read admits at most 4096 registered cells including the
new cell, 4 MiB of selected reservation JSON and 64 KiB per fingerprint. A bound or
unreadable stored reservation refuses registration rather than omitting a cell.

Later prepare requires the exact published pair in the registration response and
the full row returned by its fresh control claim before local acknowledgment. Its
second live-process census and local acceptance CAS remain required. A rerun cannot
fill a blank directory pair or change a reservation; it refuses and retains state.
Before first attach publishes its pair, first-cell updates remain local. A later
cell's configuration changes use the existing directory-fenced `loom cell update` rules; a configured-org
reservation is not a placement, and prepare creates neither placements nor tenancy.
A prepared later cell remains attaching/enforced: its reserved issuers are not
routed and its reservations do not admit fleet business actions.

A change to what a cell accepts goes through `loom cell update --config <file>`
([operator procedure](../operations/cell-update.md)). The nonsecret settings file
overlays the CLI's effective settings without installing them. It computes `next`
with the same function the processes use and refuses while a different `next` is
pending. Under the directory lock, overlap checks count both values of every other
cell. Configured placements, their counts, issuer routing and the directory pair
commit together before local tenancy repair and acceptance of the pair.

`--finish` selects next; `--revert` selects current. Each repairs local tenancy from
actual same-cell immutable placement winners for both reserved values, using an
operator read fenced by the generation, exact registration and pair, locked
root-secret fingerprint, audit and synchronous WAL acknowledgement. First-cell
lookup fallback is not evidence of a placement. The attaching first cell may still
lack placements for its preexisting configured orgs; other missing placements
require directory metadata recovery.

Finalization durably narrows local acceptance while the directory keeps both
reservations. It then takes a complete post-cutoff participant census and requires
permanent guarded retirement or verified exact death for every captured identity,
including target processes, and a serving target replacement outside that set.
Only then may the directory release its old reservation. Heartbeat expiry,
request deadlines, missing participant rows and workers-only replacements do not
prove this. Online finalization needs the
[lifecycle baseline](#participant-lifecycle-baseline) and controller fencing.
For a SCIM-on target, the CLI runs the shared fresh provisioning census before
claiming the generation, after durable acceptance, and after participant turnover
before reservation release. It uses the target cell's configured SCIM credentials,
OIDC login and required authentication. `--revoke-unprovisioned` permits the same
reported, irreversible SCIM cutoffs as attach; without it, an unqualified live
subject refuses. Revert to SCIM-off current requires no provisioning census.

A timeout after cutoff keeps local acceptance single and the directory dual; the
command reports incomplete turnover. Repeating the selected action takes a fresh
census. An identical `--config` repairs directory-first interruption and can widen
local acceptance again; subsequent finalization takes another cutoff and census.
A singleton no-op still proves local synchronous durability. Every configured org
placed by an update stays placed and retains its tenancy row after revert.

A new SCIM token changes no fingerprint and needs no `loom cell update`. A cell changes
its identity provider application as a single deployment does ([SSO and
SCIM](../operations/sso.md)), with the same limits; nothing in that is particular to
cells.

### The heartbeat's keys

A process's cell heartbeat reports, beside the `LOOM_CELL` it runs with, its tenancy mode:
the state its last read of the state rows makes (`open`, `recording`, `enforced` or
`partial`, a later cell's `recording` read as `enforced`), and `''` only before a read lands
or in a process that never reads them. Its `fingerprint` JSON holds the fingerprint's
version and fields, and beside them keys that no fingerprint compares: they are never in
`loom.cell_self`, the directory's fingerprints or a placement body, and a KeeperMap row
needs no column for them (`server.core.instances.cell_row`). Their presence proves the build
that reports them; there is no generation key.

- `participant_id`: this server's provider participant UUID, for joining its actual
  process lifecycle to the serving instance row. A workers child publishes no instance row.
- `serves`: `LOOM_SERVES_TRAFFIC`.
- `marked`: whether this process's pooled Postgres sessions keep the tenancy marker
  (`loom.tenancy_checked`). The cell refresh reads it with the state rows, after `RESET ALL`
  in the same transaction: a startup value survives the reset, a `SET` does not, so a
  pooler, which a cell must not have ([Postgres](../operations/postgres.md)), reads unmarked
  whether it strips startup parameters or sends them again with `SET`, and the read runs on
  the server connection the reset ran on. It costs three round trips more per refresh
  (`BEGIN`, `RESET ALL` and `COMMIT`), and every process reports a fresh value with no
  prober. A session the pooler stripped, or a process with `LOOM_CELL` unset under the
  Postgres authority, reads `false`; `null` means only that no read has landed (a boot with
  skipped Postgres migrations whose first read failed, or the ClickHouse authority, where
  nothing is read). The `cell_marker` readiness check of a process with `LOOM_CELL` set
  reads the same value: it fails in `enforced` and `partial`, where the triggers refuse an
  unmarked session's writes for an org the cell does not hold and the tenancy row of each
  newly placed org, and passes with a warning in `recording`, so a first cell behind such a
  pooler does not stall its rollout before attach, which refuses the process
  ([troubleshooting](../operations/troubleshooting.md#the-cell_marker-readiness-check-names-the-tenancy-marker)).
- `router` and `router_why`: with `LOOM_CELL` set, `router` is `null` and `router_why` names
  why until the `first` row is read, and then both are present on a first cell only (a later
  cell's state route answers 404, so it neither asks nor reports them). `router` is whether
  this process's last read of `GET /cells/v1/cells/<cell>` with its own credential answered
  200 naming the cell: `null` until the first read answers (still booting), `true` on such
  an answer, and `false` for a configuration that never works: a router setting unset (the
  process never asks), a URL that cannot be requested, a 404, an answer for another cell, or
  a 401 that persists past one `LOOM_ROUTER_DIRECTORY_CACHE_S`. That is the router's
  setting, which the cell's processes carry too: a router replica's cached credential hashes
  answer 401 for a token rotated a moment ago, so until then the last value stands and
  `router_why` reads "401, possibly a rotation" (a router run with a longer cache can make
  attach refuse during a rotation; a rerun clears it). A transient failure (a timeout, a
  connect error, a 503, a 200 that is not a JSON object naming the cell) keeps the last
  value, and while `router` is `null` `router_why` names the last such cause.

One background reader per server process asks the route every
`min(LOOM_INSTANCE_HEARTBEAT_SECONDS, LOOM_TENANCY_MODE_CACHE_S)` on a first cell until it
has read `serving` (`server/core/cell_router.py`); nothing awaits it, so a slow router
delays no beat and no request, and the beat reports what it last read, at most one period
plus `LOOM_CELL_ROUTER_TIMEOUT_S` old when a beat writes it. Every uvicorn process runs one,
so a thousand pods of eight workers ask up to 1,600 times a second. Each router replica
answers the route from its own read of the cell's row, made at most once per
`LOOM_ROUTER_DIRECTORY_CACHE_S`, so the directory sees one read per replica per window; a
failed read answers 503 for its window, so a down directory is asked no more often and still
answers 503. A process with no heartbeat (a CLI, the workers child) asks on demand instead,
at most once every `LOOM_TENANCY_MODE_CACHE_S`.

The reader keeps the directory's last answer for the tenancy check too, as one of four:
`attaching`, `serving`, another state, or unknown while no read has landed, the last one
failed, answered for no state of this cell (a 401, a 404, or an answer for another cell), or
is late (older than the period this process reads at, the background reader's or the
on-demand window, plus `LOOM_CELL_ROUTER_TIMEOUT_S`). Once a process has read `serving` it
keeps that for its life and asks no more: only `loom cell restore` lowers it, and that
restarts the processes. A first cell in `recording` records a new org only on `attaching`. A
platform or `*`-org principal, or a call with no principal, places no org: on a first cell
whose directory answers `attaching`, or cannot be read before it has read `serving`, such a
call for an org the cell does not hold answers 503 ("attach in progress" or the directory
unreadable), since the org's data may be in this cell until attach places it; once
`serving`, and on a later cell, it answers 409 `WRONG_CELL`. A process booting after
`serving` while the router is down answers those calls 503 until its first read lands.

With `LOOM_CELL` set, a non-empty `LOOM_CELL_ROUTER_URL` that is not an http(s) URL with a
host, a port at most 65535 and no `?` or `#` refuses the boot (`cell.settings_problem`); an
empty one is allowed, for workers-only pods (`LOOM_SERVES_TRAFFIC=false`), whose heartbeat
then reports `router` as `false` with the unset settings in `router_why`, logging nothing.

## The tenancy check and placement

A process with `LOOM_CELL` unset runs no tenancy check. Under the Postgres authority
(`LOOM_STORAGE_AUTHORITY=postgres`, the only one a cell runs) a server process still reads,
in one statement, the mode row, the `first` row and whether any `loom.tenancy` or
`loom.cell_self` row exists, with the mode cache's rules (below). A CLI command that
declares an org reads the same on a bounded session of its own: every state table absent
(judged by `to_regclass`, so a role missing a grant still refuses) reads as `open`, and a
failed read refuses. An org-free command, such as a token revocation, `loom migrate` or
`loom doctor`, is not gated by the mode and runs, a Postgres outage included. Both rows
absent with no tenancy or own row, as on a deployment never prepared, or the first cell in
`recording`, leave the process serving as a single deployment; every state the `first` row's
rule (above) counts as `enforced`, or as a `partial` state, gates it as `enforced` does.
`enforced` (which attach sets on the first cell once every live process has `LOOM_CELL` set,
and prepare on a later cell, whose processes run with it from their first boot) makes a
server process unready, run no worker and answer nothing but its health probes and the probe
route (so a prepare rerun can reach it), and makes the CLI command refuse; the readiness
reason and the refusal name `LOOM_CELL`. So a pod or an operator's CLI that loses
`LOOM_CELL` on an `enforced` cell writes for no org; one that also loses `LOOM_PG_DSN` or
`LOOM_STORAGE_AUTHORITY` is refused by the authority check the deployment already runs
(ClickHouse's `loom.storage_authority` record). The `loom cell` commands that connect to a
cell's stores (prepare, attach, place, restore, update) refuse without `LOOM_CELL`, and,
once the directory has a row for `LOOM_CELL`, refuse stores whose deployment id is not the
one that row records. A deployment started with `LOOM_CELL` unset is a single deployment
until prepared, and pointing it at another deployment's bucket prefix is the
misconfiguration it is for any deployment. On the first cell, `loom cell prepare` runs
before `LOOM_CELL` is rolled out; a later cell runs with `LOOM_CELL` set from its first
boot, so its processes are gated and stand aside from the workers (its blob sweep included)
until prepare writes its row, and prepare refuses a later cell while any live process's
heartbeat lacks `LOOM_CELL` or names another cell. Prepare does three things, in order:

1. **It refuses a cell whose stores already carry another cell's identity**: a
   deployment id the directory records, or state the directory has not placed there.
   These are checked by the router, in the registration's own transaction, from counts
   prepare sends: a cell the directory has no row for may hold no tenancy, `first`, mode
   or own row, and a later one no object under its prefix but prepare's probe and no
   live process without its `LOOM_CELL`. A rerun counts the tenancy and state rows and
   the prefix's objects as its own; a later cell's processes run with its `LOOM_CELL` on
   every run, which prepare checks itself.
2. **It registers the cell** in the directory as `attaching`, under the directory lock:
   its host, the deployment id read from the cell, the SHA-256 of the credential it
   calls the router with, and its bucket name and prefix, checked against every other
   cell's, prepared or attached. The credential is `lcr_` and 128 lowercase hex
   characters (512 bits), as `loom cell credential new` makes it offline, printing the
   credential and its hash; boot and `loom doctor` refuse a `LOOM_CELL_ROUTER_TOKEN` of
   any other form. Prepare takes the hash as an argument, 64 lowercase hex characters,
   so no CLI needs the secret; any other value, the credential or its 128-character
   secret included, is refused, naming `loom cell credential new`, and stored nowhere.
   No other cell's row may hold the hash, as its current or next one. A later
   registration also stores the verified current/next fingerprint pair in that same
   transaction, after comparing both values against all other reservations. First
   registration leaves the pair NULL. The later request names both fields explicitly;
   the first request names neither. All routers must support the later fields before
   prepare runs: an older strict parser refuses them, with no reduced-request retry.
3. **It writes the cell's rows**: the mode (`recording` on the first cell, `enforced` on
   a later one), the `first` row and `current`.

Prepare is resumable. A rerun for the directory row of the same name, deployment id and
host writes whichever of the `first` row and the mode row is missing, from the
directory's state, and `loom.cell_self` as above. First-cell preparation may derive
an absent own row from the heartbeats before attach. Later preparation requires its
submitted process/local pair to equal the already published directory pair; blank or
changed pairs refuse without repair. Every process must match the acknowledged current
or next value before local acceptance is written. The directory's
state is the `first` value the unique index names and the highest mode it records for
the cell: `recording` for the first cell or `enforced` for a later one at registration,
in the same transaction as the row, then each mode attach raises, recorded in the
directory before attach sets it in the cell's mode row (a refusal reports the mode row
as attach reads it back after any return to `recording`, and never lowers the
directory's record). A rerun refuses a mode row above the directory's state and a later
cell's `recording` row, neither being this cell's, and leaves a first cell's row below
it for attach to finish. A rerun refuses a credential hash that is neither the row's
`current` nor its `next` (rotation is `loom cell credential`'s), counts the tenancy rows
placed in or recorded since its own registration as its own, and skips registration's
checks it has already passed (the empty prefix and the prefix overlap), so it runs on a
`serving` cell whose prefix holds data.

A process with `LOOM_CELL` set and no mode row does not become ready. With it set, each
cell's Postgres holds `loom.tenancy` (org, since) and `loom.tenancy_mode` (`recording`,
`enforced`); a process reads the mode through a cache of at most
`LOOM_TENANCY_MODE_CACHE_S` (default 5 s), keeps the last mode it read when it cannot
read it, and keeps serving the orgs it holds, for at most
`LOOM_JWT_REVOCATION_MAX_STALE_SECONDS`, past which it is gated until a read lands.
Every server process under the Postgres authority, `LOOM_CELL` set or not, reads the
rows right after its stores open and migrate, before its first heartbeat and its lease
decision; a failed first read fails its boot (with Postgres migrations pending it boots
gated and unready until a read lands). After that a process with `LOOM_CELL` set keeps
the stale bound above, and one with it unset keeps its verdict through failed reads
with no stale bound, as a single deployment rides out a Postgres outage (the first
exception under Invariants). A cell's workers child reads its own rows and is gated
until a read lands. With `LOOM_CELL` unset absent mode and `first` rows with no tenancy
or own row are a deployment never prepared, and a failed read is never taken for an
absent row; a process that reads no state (the workers child and a CLI with
`LOOM_CELL` unset, and every process under the ClickHouse authority) is not gated by
it. In `enforced`, a cell serves exactly the orgs it holds a row for; an org a process
read as not held, for a caller acting in another org, is read again after
`LOOM_TENANCY_MODE_CACHE_S`. A `recording` row is written by one statement that inserts
it only while the mode is `recording`, reading the mode row `FOR SHARE`; it runs at
`READ COMMITTED` in its own transaction and commits before the request goes on. Attach's
write of `enforced` and these inserts never cross: an insert already running when attach
writes the mode is placed by step 4, one that starts after reads `enforced`, a stream of
inserts cannot hold the write off. If attach stalls while holding the mode lock, it
attempts bounded cleanup. A client abort does not prove that the lock was released;
stop and verify the exact holder before retrying.
Attach takes `LOCK TABLE loom.tenancy_mode IN EXCLUSIVE MODE` (which waits for running
inserts and queues later ones behind it) in short tries, in one transaction with
server-side timeouts, then writes the mode and commits on its exact ATTACH backend.
Only recognized pre-COMMIT contention can retry, after acknowledged rollback and a check
that this backend still holds ATTACH. Unexpected failure or an uncertain COMMIT closes
the owned session and ends the run. Attach neither finds nor ends another backend. A
fresh run takes the lock and claims again; whether an earlier commit landed is decided
by a durable mode read whose COMMIT waits for the standby
([attaching a cell](#attaching-a-cell): step 3 and the durable read).
The mode cache reads the mode with a plain `SELECT`, which takes no row lock and never
waits on attach. An insert that finds `enforced` (a process whose cache
still reads `recording`) writes nothing: the request goes on to the placement call as in
`enforced`, and a CLI command refuses as in `enforced`, naming the org. A first attach
never lowers the mode once it commits `enforced`. `loom doctor`, with the router's
read-only scope, lists any tenancy row the directory does not place.

- **It runs after verification, before anything is written for the org.** The op gate
  (`gates.authorize`) checks the org the transport resolves (`gates.resolve_org`, with
  its caller defaults and plane orgs) first, before `check_acted_for` and
  personal-namespace provisioning, for HTTP and MCP alike (over MCP a call for another
  cell's org answers `isError`, so a batch keeps its other answers, and a call in the
  token's own org, as `gates.resolve_org` resolves it, is checked before the caller's
  defaults are read, since they live in that org). The routes outside the registry check
  through `check_authorized`, `admit_org` or the check itself, and a middleware checks
  the verified bearer's own org on those that act in it (the routes the census marks
  `token`; for an org-wide or org-less token on the caller's routes that fall back to
  it, the sign-up org, checked with no principal, so it is recorded or refused, never
  placed). A principal that may not act in the org (a token of another org) is refused
  first, with the gate's 403, so it records, places and learns nothing: not whether the
  cell holds the org, nor where it is placed. An op whose org field is named otherwise
  (`from_org`) reaches the same check through `check_authorized`, and a census test
  lists every such field. A refusal written before the check (malformed JSON, an unknown
  argument, a validation failure) names no org the cell does not hold in its telemetry
  or code trace. A unit test lists every route of the app and fails unless it is marked
  tenancy-checked or org-free.
- **The CLI declares its orgs.** Every CLI command that connects goes through
  `run_connected`, `connect_checked` or `connect_declared` (`server/cli/_ch.py`), which take
  the orgs the command writes, or why it writes for none. A command that declares an org
  first checks its deployment (`check_configured`), so under the Postgres authority it needs
  `LOOM_PG_DSN`. On a cell, before checking a declared org, they read the cell's rows and
  refuse the command unless the `first` row names its cell and its stores (`deployment_id`,
  `bucket`, `bucket_prefix`) match a value of the cell's own row: a CLI verifies no token
  and its fingerprint never reaches the router, so it may run where the transports refuse on
  another fingerprint field. With `LOOM_CELL` unset the command is refused on a cell past
  `recording` (above). An org the cell does not hold is recorded on the first cell in
  `recording`, as a request's org is (reading the cell's state with the cell's router
  credential, `LOOM_CELL_ROUTER_TOKEN`, in the first cell's CLI configuration while its mode
  is `recording`, and only while the directory answers `attaching`), and refused in
  `enforced`; it is never placed. A code-index command declares the org it writes: its
  `--org`, else `LOOM_CODE_ORG`. A pass over every configured repository declares none,
  since attach, or the `loom cell update` that changes one, places configured orgs, and a
  read-only command or a dry run declares none, since a read records nothing. An org-free
  command says what it does to the stores: it reads, writes (`loom ch-setup`, `secrets`, a
  settings change, prepare and the rest; a dry run reads) or revokes (`loom revoke-token`, a
  SQL user's revocation, `loom signing-key retire`, `loom bot disable`, `loom sso unlink`;
  one that can give access back is a write), per subcommand where one connect serves several
  (`loom signing-key`: `list` reads, `retire` revokes, `rotate` and `record-root` write).
  With `LOOM_CELL` set, a write or a revocation checks its ClickHouse against its Postgres
  (`check_configured`), reads the `first` row on a bounded session of its own and refuses
  when it names another cell, which a CLI pointed at another cell's stores always finds, or
  when the read fails; an absent row, or state tables not migrated yet, passes, so a later
  cell runs these before it is prepared. A revocation runs through an outage (the fourth
  exception under Invariants); a write refuses during one; a read is not gated.
  `loom ch-setup`, which sets ClickHouse up on a client of its own, runs the same gate
  before it writes, a ClickHouse with no record yet passing; `loom migrate` and
  `loom provider-lock clear` write through the schema runner's own client and are not gated.
  A census test fails on a connect that declares neither, on an org-free one that does not
  say which kind it is, on a connect under `server/cli/` outside `_ch.py`, and on a
  `loom code` command that resolves its org without connecting for it. A holder of the
  cell's router credential can place orgs for the cell, within the cell's rate and
  `placements_max`, as the operator credential `loom cell place` uses can for any cell
  within its `placements_max` (an operator's call is not rate-limited, nor counted toward
  the cell's rate), and a placement is permanent, so an org name it takes is lost to every
  other cell: a power over the shared directory that the cell's store credentials lack. So
  the pods hold it, and a CLI only for the one read that needs it, while the first cell's
  mode is `recording`: a CLI command that declares an org refuses to run with it set, naming
  it, unless the cell is the first and its mode reads `recording`, and an org-free command
  never uses it; a leaked one is stopped by `loom cell limit --closed`, which needs none of
  the cell's stores, and replaced by `loom cell credential` (below).
  `loom cell place --cell <cell> --org <org>` (declared org-free: it writes the org's
  placement and tenancy row, which is how an org comes to be held), with the router's operator credential,
  places an org for a command that writes a new one (a bot, an import), through the router's
  placement API and in its order, as attach's placement of configured orgs does: it inserts
  the placement and reads back the winner, writes the cell's tenancy row only if the winner
  is this cell (otherwise it refuses and writes nothing), and reports success only once the
  row commits synchronously. The CLI holds the attach lock on a direct marked Postgres
  session, requires a full enforced local cell state and the full accepted configuration
  fingerprint, and checks both again before inserting. A transactional durability message
  makes even a no-op insert flush an earlier primary-only tenancy row to the standby.
  Opening the session, initial checks and the local transaction through COMMIT each have
  a separate client deadline derived from `LOOM_PG_STATEMENT_TIMEOUT_MS`; the directory
  request keeps its own transport timeout. Each local phase reserves cleanup inside its
  original deadline. On failure the shared control owner closes the exact backend before
  cancelling or reaping its work, deferring repeated signals within that same deadline.
  Successful exit and failures outside a local phase have a separate cleanup allowance
  derived from the same setting. An unknown outcome names rerunning the same command;
  the permanent directory winner and idempotent local insert let that rerun finish. The
  route checks directory state; a direct operator API caller is trusted to check local
  Postgres mode as the CLI does. For an org the directory does not place yet it
  passes the placement API's gate (below): refused while the directory is unavailable, for a
  cell the gate does not accept, closed, or when the fingerprint of the CLI's own
  configuration of the cell (with the deployment id read from the stores it connects to)
  matches neither value of the cell's row, or past `placements_max`, and counted toward it.
- **Writers older than the check are refused by the stores they write.** A session that
  ran the tenancy check marks itself with `loom.tenancy_checked = 'on'`: every session of
  a process with `LOOM_CELL` set sends it as a startup parameter (so the pool's
  `RESET ALL` keeps it), and the migration session sets it after taking the migration
  lock, since a migration's data steps only copy or rewrite rows already stored. On each
  ordinary table of the authority with an `org` column (not `loom.tenancy` itself, nor
  `loom.idempotency` and `loom.idempotency_fences`, keyed by the caller's token org) a
  row-level `AFTER INSERT` and `AFTER UPDATE` trigger, enabled as origin triggers, skips a
  marked session in its `WHEN` (the update one also skips an update that leaves the row's
  org alone, so an `ON CONFLICT DO UPDATE` that hits an existing row is not refused) and
  calls one function (migration 011). The function follows the state rule (above): it
  passes every row while the state is open or a first cell is recording, and in every
  other state refuses, SQLSTATE `LT001` (503 `TENANCY_REFUSED` to a caller), a row for an
  org with no tenancy row (never `''`, `'-'` or `'*'`); it reads the state once the row is
  written, with no row lock, so no write queues behind attach, and step 4 (below) waits
  out every transaction that could have read `recording`. A boot with migrations on
  creates the triggers, on every such table it finds in the catalog, only on a fully
  prepared cell in `enforced`, and, there, sets any it finds not at origin back to origin;
  each table's DDL waits 100 ms a try for its lock, three tries a second apart (about 2.3
  s for a busy table, so a boot that finds every guarded table busy waits that long for
  each in turn, holding the migration lock other booting pods wait on), and the boot
  serves past a table whose lock it cannot take, logged with the holders of its locks, or
  one the role cannot alter, logged as such (the third exception under Invariants). A
  first cell is `recording` until attach sets `enforced` while it runs, so no boot can
  have created them: attach creates them with `cell_triggers.ensure_table`, passing
  `recording_first=True`, before step 2 and again before setting `enforced`.
  The cell goes `serving` only once `cell_triggers.missing` names no defect. A later cell is `enforced` from prepare
  and gets them at its first migrating boot after prepare, which its attach requires the
  same way. `loom doctor` names a disabled or altered one in any state, a missing one on
  an `enforced` cell, and a partitioned table with an `org` column as unsupported, since
  it gets no trigger. Origin triggers do not fire for logical replication's apply or under
  `session_replication_role = replica`, which a cell's scripts do not use. The marker is
  no privilege boundary: a writer that sets it on purpose passes; the triggers stop
  writers that do not know it, an older CLI, a pod downgraded past the check, or a script,
  from adding a row for an org the cell does not hold or moving a row to one; an update
  that keeps a row's org, and a delete, are not refused. The org triggers' cost is one
  setting lookup per marked row, and one read of the state and a `loom.tenancy` lookup per
  unmarked row; the gate they ship under is a `memory.set` p99 rise of at most 10 ms and a
  delivery pass's rows per second at `LOOM_PG_DELIVERY_BATCH_ROWS` at most 10% lower, in
  `enforced` with the orgs held. Every Loom write on a cell is marked, so it pays the
  setting lookup alone, measured on the statements with the triggers on and off (no cost
  distinguishable from zero: six rounds range from −0.36 to 0.49 µs a row, [benchmark
  results](../benchmarks/results.md)), which predicts the gate holds by a wide margin. The
  gate itself is an end-to-end `memory.set` and delivery-pass measurement on a cell, which
  needs attach and is checked with it; until then the triggers, which a later cell gets from
  prepare's next migrating boot, ship on the statement-level measurement. `loom.tenancy`
  itself has its own guard, since a tenancy row makes an org held: `tenancy_record`, a
  row-level `AFTER INSERT OR UPDATE OF org` origin trigger with the same marker `WHEN`,
  calls `loom.tenancy_refuse_unmarked()` (migration 012), which refuses, `LT001`, an
  unmarked session's tenancy row, or an update that moves one to another org, in every state
  but a first cell still recording (`open` too, since the row itself would make the state
  `partial`), so a script cannot make an org held that the directory does not place. Boots
  and attach create and repair it as they do the org triggers. With attach, `loom doctor`
  also lists any tenancy row the directory does not place, which names one written before
  the guard or by a writer that set the marker, and any org in an org-keyed Postgres table
  with no tenancy row, which names rows written before the org triggers, by a writer that
  set the marker, or left behind by a deleted tenancy row. Before the first attach the
  operator rotates the cell's ClickHouse writer credentials (create the new user, roll it
  out to configurations of builds that check, drop the old one), and attach refuses while
  any ClickHouse user with `INSERT` on `loom.*` (`system.grants`, with database, global and
  role grants followed through `system.role_grants`, and users from configuration files or
  with implicit `ALL` counted) is not one a live process of a checking build or the CLI's
  configured user reports, so only holders of the rotated credential write ClickHouse; an
  older CLI given that credential still writes unchecked, and with attach `loom doctor`
  lists what it wrote: any org found in an org-keyed ClickHouse table with no tenancy row,
  from the same schema list as attach step 2.
- **An org the cell does not hold is placed there or refused.** For a verified principal
  scoped to the org (a token of that org, or an issuer's or bot's principal whose org it
  is), the cell calls the router's placement API (`POST /cells/v1/placements` at
  `LOOM_CELL_ROUTER_URL`, answered with the winning cell and its deployment id) with its
  own credential (`LOOM_CELL_ROUTER_TOKEN`), and the cell the call names must be the one
  whose recorded hash the credential matches (a hash names one cell): the router places
  one org at a time (a transaction-scoped lock on the org, which every path that places
  takes), answers an org placed already with its cell, and otherwise judges the asking
  cell on its locked row, inserts the placement and reads the winner back, which the
  cell compares by its name and recorded deployment id. If the winner is this cell, the
  cell writes the tenancy row and serves the request; this cell's name under another
  deployment id than the process verified answers 503 and writes nothing; if another
  cell's name, the request answers 409 `WRONG_CELL` and nothing is written. This is the
  one path by which a request brings an org to a cell, whatever created it: a mint, a
  gateway's or a federated issuer's token, an end-user issuer's `prefix*` org, a bot. A
  `*`-org or platform principal acts only in orgs the cell already holds and never
  places one (on a first cell before `serving`, see [the heartbeat's
  keys](#the-heartbeats-keys)). For an org the directory does not place yet, the router
  accepts a placement call only from a `serving` cell (or from the first cell while
  `attaching` once the directory records `enforced`, which attach step 3 writes before it
  sets the cell's mode), and only with the calling process's configuration fingerprint,
  whose fields must match a value of the cell's directory row; a prepared cell's row holds
  no issuers or org patterns until attach writes them. A failed call, one unanswered within
  `LOOM_CELL_ROUTER_TIMEOUT_S`, a cell not yet attached or closed to new orgs, or a
  fingerprint that matches neither value answers 503 and writes nothing; a retry is
  safe, since the read-back makes it idempotent. For an org the directory already
  places, a cell's call is checked only for its credential (against the cells' hashes
  the router caches for `LOOM_ROUTER_DIRECTORY_CACHE_S`, default 5 s) and the cell's
  rate. An operator's `loom cell place` checks its deployment id and fingerprint against
  the requested cell's locked row even for an org already placed there; an org placed
  elsewhere answers that winner and the CLI refuses without inserting local tenancy.
  Existing placements bypass closed and quota checks. Every successful call writes an
  audit row before its synchronous commit, including a retry of an existing winner, so
  the returned winner survives failover. A request refused past its cell's rate answers 503
  with `Retry-After` (at most a minute), and its retry is admitted by the replica that
  answers it once the cell's calls there are under the rate. An org the cell already
  holds needs no call, so serving it never depends on the router.
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
  Every org the first cell's attach places (step 2's snapshot, its configured orgs included,
  and step 4's) is counted but never refused by quota or a closed cell, since the cell
  already serves it and refusing it would strand its data (one past `ORG_MAX_BYTES` the
  directory refuses, below); every other placement of an org the directory does not place
  yet, a later cell's configured orgs at attach and a changed one at `loom cell update`
  included, passes the gate, a closed cell and `placements_max` included, so a later cell
  closed or full before attach refuses the attach, naming the org.
- **The first cell records until `enforced` as a single deployment does.** In `recording`,
  which lasts until step 3 writes `enforced` to the cell's mode (the mode only rises after
  it), a verified request for a new org writes a tenancy row with no placement call, so the
  router's rate, `placements_max` and `loom cell limit --closed` do not apply: new orgs
  arrive as they do on a single deployment, bounded by its own signup, issuer and JIT
  settings, and step 2 or step 4 places them unrefused. `loom doctor` lists the recorded
  orgs that hold no data, so the operator sees what attach will place.
- **Configured orgs are placed at attach.** The cell's `oidc_jit_org`, the
  `self_signup_org` of a cell with signup on, and its code orgs (`LOOM_CODE_ORG` and the
  org of each `LOOM_CODE_REPOS_JSON` entry the code config keeps) and tracker org are
  placed as its own when it is attached: the first cell's in step 2's snapshot, a later
  cell's before it is marked `serving`. JIT and self-signup pass the tenancy check as
  any request does, and nothing refuses at boot: in the first cell's `recording` they
  record their org while the directory answers `attaching`, and elsewhere a configured
  org the cell does not hold answers 503 until it is placed (422 for one past
  `ORG_MAX_BYTES`, which the directory refuses), which `loom doctor` names; every attach,
  first or later, new or resumed, runs the overlap refusals on and places the configured
  orgs of both values of `loom.cell_self`, so one an update put in `next` before attach is
  placed with the rest; once attach has written the cell's directory row, `loom cell update`
  places a changed one in the directory transaction that writes `next`, through the
  placement API's gate for an org the directory does not place yet (closed cells and
  `placements_max` included), refusing, naming it, when another cell holds it, and then
  writes its tenancy row in the cell; for a later cell still `attaching` after its directory
  transaction, `loom cell update` refuses a configured-org change until the cell is
  `serving`. In `enforced`, a worker writes for an org only once the cell holds it (the
  tenancy check with no principal), so a later cell indexes, imports and sweeps nothing
  under its configured orgs before attach places them; in `recording`, workers run as
  before, and step 2's snapshot and step 4's second read find every org they write under.
- **Rows are never deleted.** A tenancy row and a placement name an org, not its memory:
  both are classified for erasure as holding no memory content, as a namespace's claim
  is, and stay after any erasure, so an org name never comes free for another cell. A
  process keeps the orgs it has seen held, and a kept org is never wrong to serve.
- **One org name limit.** A cell records or places no new org longer than
  `ORG_MAX_BYTES` (2048 UTF-8 bytes, `server/core/cell.py`, below Postgres's index entry
  limit): a recording cell's tenancy check refuses one with 422 and writes nothing, and
  the directory refuses one with 400 (`ORG_TOO_LONG`) before it writes a placement, for
  every route that places, which a cell's placement turns into the same 422 (over MCP, a
  tool error counted as the caller's). So every org a cell records can be placed, and an
  org held or placed before the limit keeps its cell.
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

The provisioning census reads active accounts, current unexpired refresh families and
unrevoked, unexpired personal service accounts in bounded pages of 128 rows and 65,536
bytes of projected string metadata. It uses each table's actual row key to continue, refuses an oversized
page, and shares one finite scan budget. The same bounded person and ancestry reader
checks every root freshly, including while the cell is recording; an authentication
memo or a deactivation-only check cannot establish this precondition. A current refresh
family supplies only its own stored verified email. Registered machine identities keep
their existing closed exemption; a subject prefix or an absent creator grants nothing.

With `--revoke-unprovisioned`, each accepted cutoff uses SCIM's own subject lease and
revocation prefix. An outright revocation or a newer cutoff is retained. The command
never creates a provisioned user, changes the operator's revocation row, or revokes a
registered machine identity. An accepted cutoff is reported before confirmation and
audit; a later failure leaves it in force and the census incomplete. Rerun with the same
flag to finish. A dry run and a serving rerun never revoke.

The first attach (`loom cell attach`, its procedure on
[attaching the first cell](../operations/cell-attach.md)) turns the existing deployment
into the first cell. It aliases nothing (an `oauth_issuer_host` on another host refuses at
`attach/begin`). With SCIM on, it freshly checks the complete provisioning precondition
while still recording, before each mode-commit try, after enforcement and before serving.
A serving rerun repeats that check without changing revocations. A run holds an
advisory lock, `cell.ATTACH_LOCK_KEY`, on a session of its own for its life (prepare
takes the same lock), after checking that the session is one direct
backend that keeps the tenancy marker: a lock left on a pooler's server connection would
refuse every later run, and attach's tenancy rows would be refused as an unchecked
writer's. A lost or unreadable lock session ends the run: it never reconnects or
reacquires the lock. Rerun attach to obtain a fresh directory claim and recompute the
remaining work. Every local acceptance write uses this exact backend, so losing the
lock also ends its authority to change acceptance. The lock serializes prepare, attach
and update, and keeps concurrent scans from holding the snapshot horizon that step 4
waits on.

It refuses, before any write, naming each:

- stores other than the registered ones: the Postgres deployment id, ClickHouse's
  `loom.storage_authority` and the shared host's `/v1/health/deployment` each name the
  directory row's deployment id, `loom.cell_first` reads `(true, <cell>)`, the row is the
  first cell's, `attaching`, with no other directory row;
- a database encoding other than UTF8 (prepare refuses the same), so a tenancy row
  within the length limit is always an org a request can name;
- a synchronous standby not attached now, a durability profile that would not refuse
  without one, or a role that may not write the durable read's logical message; a role
  of attach's own lacking its grants (`pg_read_all_stats` among them) or a server with
  `max_prepared_transactions` above 0;
- **the tenancy-privilege check**: a login role, other than attach's own (the role attach
  runs as, which the operator runs it with; superusers; and a role that itself owns
  `loom.tenancy`, `loom.tenancy_mode` or a guarded table) and those named with
  `--pg-allow-role`, that holds or reaches through any membership a right to make an org
  held or lower the mode: `INSERT` on any column of `loom.tenancy` or
  `UPDATE` of its `org`, `INSERT` on `loom.tenancy_mode` or `UPDATE` of its `mode`,
  `TRIGGER` on a `loom` table, `CREATE` on the database or a schema that is not a system
  one (a function there can shadow one the servers call unqualified on their marked
  session), `SET` or `ALTER SYSTEM` on `session_replication_role`, `ALTER SYSTEM` on
  `loom.tenancy_checked`, membership in `pg_write_server_files` or
  `pg_execute_server_program`, or ownership of a `loom` table, of the trigger functions or
  of `loom.cell_first_immutable()`, the database or a schema;
- **the unmarked-session check**: a session that sends no marker reads one, or runs as
  `replica`; a `pg_db_role_setting` entry of `loom.tenancy_checked` or
  `session_replication_role` for this database or every one, matched case-insensitively;
  any logical-replication subscription into this database, enabled or not (its apply
  worker fires no origin trigger);
- the heartbeats (both tables, stopped rows included for the widened window, the live
  window plus a stopping pod's drain and retirement): a live process with `LOOM_CELL`
  unset or another cell, one without the heartbeat's keys, `marked` false, `router` false
  where `serves` is true, a fingerprint on neither `loom.cell_self` value, several
  `keys_fp` values, or none live; a booting process (no cell row yet, no mode, `marked`
  or `router` null) is waited on within `--wait`; in `recording` a stopped process with
  `LOOM_CELL` unset refuses too;
- the ClickHouse writer check and the scan's table list (`server/cli/cell_writers.py`,
  `server/cli/cell_scan.py`), and a settings profile or quota that caps what attach's user
  reads.

Then, in order (the step numbers are the ones the rest of this page uses):

- **`attach/begin`**, on every run, before any long step: the router probes the stores
  attach wrote its nonces to, checks the upstream, and writes the fingerprints and
  `keys_fp`; a directory restored to before it is written again.
- **Step 1, the mode**: `recording`, or `enforced` on a resume, which goes from the
  triggers, through `attach/enforced` again, to the durable read.
- **The triggers**: created by attach itself, on every run, before step 2 and again just
  before step 3 (so a table a migration added during the scan is guarded from the commit
  on): one table at a time under the migration lock, each DDL waiting 100 ms for its lock
  and retried a second apart within `--wait`; an autovacuum holding it gets one try just
  past `deadlock_timeout`, so it yields, and an anti-wraparound vacuum is named. Then
  `cell_triggers.missing`; any defect refuses. A refused attach in `recording` leaves the
  triggers, which pass every row there.
- **Step 2, the scan**, in `recording`: one statement per table, no long transaction (step 4
  reads every source again): the Postgres tables (`loom.tenancy` among them) by skip scans
  on an `org`-led btree index, then the ClickHouse tables, each flushed and synced first,
  with the configured orgs of both fingerprints. Log tables (`cell_scan.LOG_TABLES`) are not
  read: a log row keeps the org a caller typed before any check, so placing a log-only org
  would let anyone pin an org name to the cell; `loom doctor --stray-orgs` names such orgs.
  An org longer than `cell.ORG_MAX_BYTES` refuses, naming it, before any tenancy row is
  written; a name no request can carry is skipped and named. Then the tenancy rows of the
  orgs found, on a marked session, and no placement: unscoped callers are answered from the
  tenancy rows, and placing now would move the point of no return earlier.
- **The pre-commit checks**: the heartbeats, the tenancy-privilege and unmarked-session
  checks and the standbys again, and the scan's elapsed time within half of `--scan-budget`,
  since step 4 reads the same tables again. Each refuses in `recording`, which can still be
  backed out.
- **Step 3, the mode commit**: `attach/enforced`, then on the same ATTACH session an EXCLUSIVE
  lock on `loom.tenancy_mode`, 2 s a try within `--wait`, the attach lock confirmed and the
  cheap heartbeat refusals run before each try (a booting process skips the try). Each try,
  including COMMIT, has a client deadline: the lock wait plus a 3 s statement margin and
  a 5 s standby allowance. Each mode or trigger try reserves the smaller of one second
  and one quarter of its original client bound for cleanup. Its independent owner
  closes the session through the public driver API while query/transaction work is
  still alive, then cancels and reaps that work within the same absolute deadline.
  Borrowed checks and trigger DDL leave failed-session cleanup to that owner. Close
  failure or exhausted cleanup time aborts the transport and preserves the original
  failure; abort alone does not prove that server locks disappeared. A commit past
  its deadline has an unknown outcome until the durable read.
  Only acknowledged pre-COMMIT rollback with ATTACH still held permits another try on
  that backend. Any other failure ends the run without terminating another backend;
  rerun to take the lock and claim afresh. From an acknowledged commit on, the mode
  only rises: every later refusal leaves the cell `enforced` and `attaching`, and a
  rerun resumes from the durable read.
- **The durable read**, in every run that reads `enforced`: the mode and the `first` row
  read in a transaction that emits a transactional logical message, whose commit waits for
  the synchronous standbys, with the standbys checked attached just before and after. Then
  `X`, the snapshot's xmax, which must exceed that message's xid. A READ COMMITTED writer
  that read `recording` holds an xid below `X`, and a REPEATABLE READ one a snapshot older
  than `X`: the trigger functions are `VOLATILE`, and the message's xid is assigned after
  the mode commit.
- **The heartbeats** again: a process with `LOOM_CELL` unset or another cell, without the
  keys, unmarked, or with no router where it serves, refuses at once. No process depends on
  its cached mode (a request records a new org through `tenancy._RECORD`, which reads the
  mode `FOR SHARE`), so neither a cache lifetime nor a request deadline is waited out.
- **Step 4**: wait, within `--wait`, while any backend of the cell's database but the
  polling one has an xid older than `X`, or any but an autovacuum a snapshot older than `X`
  (compared through `age()`, exactly `< X` and wraparound-safe); past `--wait` attach
  refuses, naming each, and each backend of another database holding the horizon. A prepared
  transaction, or a row of the cell's database whose state is hidden, refuses at once. Then
  the cheap checks (the writer check, `missing`, the Postgres checks, the heartbeats with
  every stopped process with `LOOM_CELL` unset aged out, the standbys); the rescan of every
  source in full, Postgres before ClickHouse (the outbox and delivery tables move rows to
  ClickHouse), and the queue and drain tables and the tables a view reads before the others
  (`cell_scan.MOVE_SOURCES`); the tenancy rows; the placements, in `attach/place` batches;
  the writer, trigger, Postgres and standby checks again; and the final checks, under a
  floor of their own (the larger of
  what `--wait` left and `instances.boot_allowance_s()`): the heartbeats, read through the
  rescan every half of the time the leader keeps a stopped row (`instances.PRUNE_AFTER_S`),
  refusing any process that ran with `LOOM_CELL` unset during it, `loom.cell_self` equal to
  the directory's fingerprints, one `keys_fp` equal to the one `begin` wrote, the mode
  `enforced`; then `attach/serving`.

`--wait` (default 10 minutes) is the wall time the bounded waits share, each lock try
included and never longer than what is left, but for a floor of 100 ms: every try runs once
before the wait is judged spent, so `--wait 0` is one try of each wait with no retry;
`--scan-budget` (default 2 hours) bounds each full scan, which `--wait` does not count.
`--dry-run` runs the refusals and the scan, prints what would be placed and skipped and the
scan's time. Cell state, tenancy triggers and placements stay unchanged; normal participant
enrollment and retirement are still recorded. The router's checks at `attach/begin` do not
run. On a `serving` row attach first verifies that the connected authority, configured cell
and bucket match the directory registration. It then reads the cell's mode: `enforced` is
done (exit 0), and any other mode refuses: the directory and the cell's Postgres disagree,
as after a restore of one of them, and attach lowers and repairs neither.

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

A resumed first attach reruns every refusal and continues by the directory row and the
cell's mode: in `recording` from the scan, in `enforced` from the durable read; whenever
the cell reads `enforced` it posts `attach/enforced` again, so a directory restored below
the cell's mode is raised.

The checks of the stores' writers are point-in-time: a grant, a role setting, a
subscription, a ClickHouse writer or object added after `serving` passes them, as does a
configuration file attach cannot read (`postgresql.conf`, `postgresql.auto.conf`, a
standby's own) or a role that runs code as the server's OS user. `loom doctor` runs the
tenancy-privilege and unmarked-session reads again on every run and warns on what it finds
(`cell_tenancy_privilege`, `cell_unchecked_writers`); the runbook has the operator confirm
the rest.

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
erasures and redactions pass the tenancy check, and a restore of both stores,
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
rollout. A process that reads `recording` records a new org only while the directory answers
that its cell is `attaching` ([the heartbeat's keys](#the-heartbeats-keys)); while the
directory is unreadable or marks the cell `serving`, a new org answers 503 and the orgs the
cell holds keep serving. A restore to before the first attach's step 3 whose
`loom cell restore` was skipped therefore serves no org it does not hold; `loom doctor`
names such a cell, and `loom cell restore` raises its mode.

**The directory is a highly available store.** Its Postgres runs with a synchronous
standby and point-in-time recovery to its last commit, as a cell's does, so a failover
or a corruption loses nothing committed. Losing it entirely is an outage that fails
closed: every cell keeps serving the orgs it holds, routing continues from each router
replica's signed snapshot of its last cache (written to shared storage on every
directory change), and the placements, lookups of uncached orgs, `loom cell` commands
that read or write the directory and the recording cells' state reads answer 503 or
refuse until the directory is back, while `loom doctor`, with the router's read-only
scope, reports its directory checks as not run and, with `LOOM_CELL` set, exits
non-zero; rebuilding a lost directory from the cells is a design of its own.

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
the directory row. An explicit `--open` refuses while the cell is `attaching`, including
an already-open row and a combined quota edit. This behavior break preserves the closed,
zero-placement boundary used by state cleanup. Close and quota-only edits still work;
an attaching cell can complete attach while closed, then reopen once `serving`.

## Control mutation fencing

A cell controller holds the session-level attach lock on one direct, marked, UTF8
Postgres connection for its whole run. Every write to `loom.cell_self`, `loom.cell_first`
or `loom.tenancy_mode` checks that lock and its observed local state, then writes and
commits on that exact connection. Prepare passes its lock connection to its row writer;
attach commits mode on its lock connection. A lost connection ends the run. It cannot
reconnect and continue an earlier claim. Terminating the client does not prove its
server backend ended: another controller must actually acquire the attach lock before
it starts, and an unresponsive backend can keep that lock until it is positively ended.

The directory stores one opaque UUID, `cells.mutation_generation`, for each cell.
`POST /cells/v1/operator/cells/<cell>/claim` takes the directory advisory lock and cell
row lock, compares the full observed registration and exact current/next JSON pair,
generates a fresh UUID on the server, audits it and commits synchronously before
answering. Every execution generates another UUID, including a repeated HTTP request.
The caller never proposes a replacement UUID. Registration assigns a fresh default;
a registration rerun reads the existing value without resetting it. Removing and
registering the same name therefore cannot restore an earlier generation.

Before its first local acceptance write, prepare, attach or a recovery controller
claims the generation while holding its local lock. Every subsequent directory
mutation carries `expected`: cell, first-cell flag, host, deployment id, bucket, prefix,
probe/upstream addresses, exact current/next pair and the claimed generation. The claim
carries the same fields without a generation. The directory compares them under its
locks before any mutation or no-op success. Canonical JSON comparison distinguishes
`false`, `0` and `0.0`. A stale request cannot release a reservation after its local
backend dies and another controller claims and widens acceptance, even if the directory
pair later returns to its earlier values. A delayed claim can invalidate another
controller's work; it cannot let a dead local session write acceptance state.

A lost acknowledgement is an unknown outcome. Stop the attempt and rerun: obtain the
local lock, read current state and claim a fresh UUID before recomputing the action.
Do not copy a latest UUID onto an old action. A successful claim or control transaction
waits through synchronous COMMIT. A no-op transaction emits transactional WAL so it
also proves an observed earlier write durable. Acquisition, statements, COMMIT and
cleanup have explicit bounds; an interrupted or uncertain local transaction never
continues on another connection. No operation record, persistent census, phase or lease
is added. The UUID is coordination metadata and holds no memory content.

One ephemeral owner supervises the command's raw session and child work. Opening keeps
its 75-second bound and each local control transaction keeps its 60-second bound,
reserving cleanup time inside that bound. On failure it sends public connection close
before cancelling child work, so active statements and rollback cannot prevent bounded
cleanup. An unexpected transaction error propagates immediately; it never waits for
rollback on a session the owner will discard. Uncapped command work retains the existing
one-second session-exit allowance, which cannot extend an expired bounded phase. A
transport abort remains an unknown outcome, not proof that the server backend ended.
Mode-lock contention retries only after an acknowledged pre-COMMIT rollback and a check
that the same backend still holds ATTACH. A COMMIT, rollback or ownership-check failure
never becomes a busy retry. After attempting the mode update, the diagnostic stays
unknown until rollback plus ownership or COMMIT is acknowledged.

Directory control mutations supervise the complete callback transaction independently,
including acquisition and COMMIT. One 131-second bound includes public close, child
settlement and pool-context exit, with cleanup time reserved inside it. Both a failed
attempt and an acknowledged successful transaction close only their own acquired
connection before releasing the pool context. Unexpected errors never wait for rollback
on that discarded session. A late acquisition cannot start mutation work or obtain a
fresh cleanup allowance; acquisition without a known proxy remains an unknown outcome.
Ordinary live placement retains its separate transaction and connection handling.
Repeated interruption keeps the same close task and original end; a completed child
cancellation remains a terminal failure. Fallback abort errors cannot replace the first
operation or cleanup failure.

The shared directory pool opens physical sessions only for foreground acquisitions;
pool object creation does not establish connectivity. Healthy released sessions are
cached under the existing size cap, recycling and idle expiry. A later cold acquisition
opens a new session. Fixed JSON codec initialization quarantines its never-borrowed raw
session on failure; native interruptions return to that foreground consumer after driver
cleanup, with no autonomous opening or replenishment task. If quarantine fails, the
original failure records that session closure is unknown.

The source census for acceptance and operator directory writes is checked by
`tests/unit/test_cell_control_census.py`, including dynamic cleanup tables, guarded
entry points and the named exceptions:

| Writer                                                                                  | Fence                                                                                                                                                                                                          |
| --------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `cell_prepare._write_rows`                                                              | Fresh claim, observed local rows, exact lock backend                                                                                                                                                           |
| `cell_attach_pg._mode_transaction`                                                      | Attach's claim, supervised mode try, recording/first/fingerprint comparison, exact lock backend                                                                                                                |
| `cell_control.backout`                                                                  | Fresh claim, fenced close and zero-placement read, observed local rows, exact lock backend                                                                                                                     |
| `cell_update_pg.write.change`                                                           | Fresh directory claim; owner's bounded transaction on the exact ATTACH backend, paired registration and exact observed local identity/pair before tenancy repair and acceptance write                          |
| `directory.attach_begin`, `attach_place`, `attach_enforced`, `attach_serving`, `remove` | Claimed generation, full registration and exact pair, including retries/no-ops                                                                                                                                 |
| `directory.update_addresses`                                                            | Claimed generation, full registration and exact pair under directory/row locks; commit-time address check, audit and synchronous WAL acknowledgement in one bounded transaction                                |
| `directory.update_configuration`, `_configuration_commit`                               | Claimed generation, full registration and exact pair under ordered org/directory/row locks; root-secret and reservation checks, placements, audit and acceptance commit in one bounded synchronous transaction |
| `directory.configuration_placements`                                                    | Claimed generation, full registration and exact pair under directory/row locks; root-secret and actual winner checks with audit and synchronous WAL acknowledgement in one bounded transaction                 |
| `directory.set_limit`, `rotate_credential`                                              | Read and compare the current generation/registration/pair; do not claim or rotate                                                                                                                              |
| `directory.register`                                                                    | Bootstrap default or immutable rerun lookup; never replaces existing acceptance or generation                                                                                                                  |
| `directory.place`                                                                       | Immutable placement only; keeps its org/cell locks and quota checks; never writes local acceptance                                                                                                             |

Ordinary read-only routes are exempt; `configuration_placements` is an audited,
WAL-fenced proof before local tenancy repair. Router migrations and an offline
directory restore are maintenance operations. Update acceptance writers use the
same claim and lock-session helpers; any additional acceptance writer must do so
as well. A directory mutation uses the common comparison before its no-op branch.
Direct privileged SQL is not an online control writer.
The [control upgrade and recovery procedure](../operations/cell-control.md) defines the
required fleet turnover and offline restore of generations. N-1 data-plane builds can
read and insert on the additive schema; old control clients and router handlers cannot
participate in this protocol.

## Operations

- `loom cell restore`, after any store restore and before `loom reconcile`, writes the
  cell's rows back from the directory ([What each cell runs](#what-each-cell-runs)).
- `loom cell which --org <org>` names an org's cell (erasure, restore and support start
  there); for an org with no placement it names the first cell, which the lookup does
  not tell apart.
- `loom cell limit --cell <cell> --closed` closes a cell to new orgs, `--open` reopens
  a serving cell, and `--placements-max <n>` sets its placement quota (`--placements-max none`
  clears it).
- `loom cell remove --cell <cell>` removes the directory row of an `attaching` cell
  with no placement. For both first and later cells, keep `LOOM_CELL=<cell>` and the
  registered store settings on the control client. Removal takes the direct marked
  ATTACH backend, verifies the paired deployment and registration, and claims a fresh
  directory generation before deleting the row. The first cell must also read `open`,
  enforcing the back-out order: stop or unset `LOOM_CELL` on the accepting processes,
  complete full-state cleanup, then remove the directory row
  ([troubleshooting](../operations/troubleshooting.md#a-process-with-loom_cell-unset-is-gated)).
  Removing the row first answers every new org 503; clearing the state first gates every
  accepting process with `LOOM_CELL` set. A first cell with a placement goes forward,
  never back. Removal leaves the stores' state rows intact; full-state cleanup must
  precede preparing those stores again.
- The router's operator credential is `lco_` and 128 lowercase hex (a router with no
  `LOOM_ROUTER_OPERATOR_SHA256` answers the operator API 503 with
  `NO_OPERATOR_CREDENTIAL`), which `loom cell credential new --operator` makes; the
  router keeps only its SHA-256. The operator API's refusals are not rate-limited: a
  per-address limit would let one stranger behind a shared ingress lock out every
  operator, and 512 bits are not guessed online. An operator command knows its outcome
  only when the connection never opened or the router itself answered, other than
  `BACKEND_UNAVAILABLE` (a COMMIT can land after its bound); after a timeout, a dropped
  connection or a gateway's page it says the change may have been made and what a retry
  shows. The operator credential is rotated by setting the new hash in
  `LOOM_ROUTER_OPERATOR_SHA256` and restarting the router; while its replicas roll, some
  still refuse the new credential with 401, and the operator runs the command again
  until every replica accepts it. The commands call the router at `--router`, else at
  `LOOM_CELL_ROUTER_URL`, at its root (a path there is refused, since the router
  forwards one to a cell), and send the operator credential only over https (http on
  loopback), so an in-network `http` router address serves a cell but not these
  commands.
- `loom cell credential new` makes a router credential offline and prints it with its
  SHA-256; `loom cell credential --cell <cell> --next <sha256>` records a new router
  credential's SHA-256 as the cell's `next` hash; like prepare, it takes the hash as an
  argument, in the same form. It uses the router's operator credential, writes only the
  directory and holds the directory lock; it is refused when any cell records that hash
  as current or next, and while a `next` is pending. The router accepts both on
  placement calls and state reads, every replica within `LOOM_ROUTER_DIRECTORY_CACHE_S`
  of `--next`, so the new credential goes into the pods only after that;
  `loom router migrate` runs before a router that reads the `next` hash, and a rotation
  starts once every router replica does. A router rolled back to a build that reads the
  current hash alone refuses a pod on the next one: roll back with no `next` pending
  (`--revert` first), and not between `--finish` and the old credential leaving the
  pods. Once the new credential is in the cell's pods and, while the first cell's mode
  is `recording`, its CLI configuration, `loom cell credential --cell <cell> --finish`
  makes `next` current and drops the old; it checks nothing it cannot see (the router
  does not know which credential a pod holds), and a cell finished too early fails
  closed: its placement calls and state reads answer 401, at the latest once the
  router's cache of the cell's hashes expires (`LOOM_ROUTER_DIRECTORY_CACHE_S`), until
  its pods hold the new credential, or the old hash is recorded again with `--next` and
  finished. `loom cell credential --cell <cell> --revert` drops a pending `next`, so a
  wrong or leaked one never has to be finished; `--finish` and `--revert` refuse with no
  `next` pending.
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

| Failure                                                                                                                                                                      | What happens                                                                                                                                                                                                                                                                                                                                                                                                                         |
| ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| The directory is down                                                                                                                                                        | requests by host, by issuer and to the first cell are routed; new orgs and uncached lookups answer 503                                                                                                                                                                                                                                                                                                                               |
| The router's placement call fails                                                                                                                                            | the request answers 503 and nothing is written; orgs the cell holds are served                                                                                                                                                                                                                                                                                                                                                       |
| A cell's issuer places orgs in a flood                                                                                                                                       | from `enforced` on, its placement calls answer 503 with `Retry-After` past its rate, and its calls for new orgs answer 503 past its quota if one is set; other cells place as before                                                                                                                                                                                                                                                 |
| A SCIM token of another cell, or an application at the wrong cell's host                                                                                                     | the process does not boot, or `/scim/v2` answers 401 and writes nothing                                                                                                                                                                                                                                                                                                                                                              |
| A cell's Postgres or ClickHouse                                                                                                                                              | that cell fails as a single deployment does ([storage authority](storage-authority.md#failure)); other cells serve                                                                                                                                                                                                                                                                                                                   |
| A cell's own rows or tenancy mode are unreadable                                                                                                                             | with `LOOM_CELL` set a process keeps its last rows and mode within the stale bound, then is gated, a server process whose first read fails does not boot (gated and unready with Postgres migrations pending), and a workers child that never read them is gated; with it unset a server process keeps its verdict with no bound, and a failed first read fails its boot (gated until a read lands with Postgres migrations pending) |
| A `first` or mode row removed, or both beside a tenancy or own row                                                                                                           | the org triggers, where a cell has them, act as in `enforced` and every process is gated; `loom doctor` names it, a prepare rerun writes them back                                                                                                                                                                                                                                                                                   |
| No state, tenancy or own row on a cell the directory registers                                                                                                               | open; `loom doctor` names it; `loom cell restore` closes it after a restore, a prepare rerun after a stopped prepare                                                                                                                                                                                                                                                                                                                 |
| A cell's `first` value is not yet read                                                                                                                                       | every request but health probes and the probe route answers 503 (SCIM with `Retry-After`); the process is unready and runs no worker (a workers child, or a server process with Postgres migrations pending; any other server process fails its boot)                                                                                                                                                                                |
| A request for another cell's org                                                                                                                                             | 409 `WRONG_CELL` after verification, nothing written                                                                                                                                                                                                                                                                                                                                                                                 |
| A platform, `*`-org or principal-less call for an unheld org                                                                                                                 | on a first cell, 503 while the directory answers `attaching` or cannot be read before a `serving` answer; 409 `WRONG_CELL` once `serving` and on a later cell                                                                                                                                                                                                                                                                        |
| A pooler that strips the tenancy marker from a cell's sessions                                                                                                               | the `cell_marker` readiness check fails in `enforced` and `partial` and warns in `recording`; the heartbeat reports `marked` false and attach refuses the process                                                                                                                                                                                                                                                                    |
| A cell restored to an earlier time                                                                                                                                           | `loom cell restore` writes placed orgs' rows back before it starts                                                                                                                                                                                                                                                                                                                                                                   |
| A first cell restored to before its prepare, not yet attached                                                                                                                | at processes with `LOOM_CELL` set, every request but health probes and the probe route answers 503 until prepare reruns                                                                                                                                                                                                                                                                                                              |
| The directory lost                                                                                                                                                           | every cell serves the orgs it holds; placements and uncached lookups answer 503, directory commands refuse, until it is back                                                                                                                                                                                                                                                                                                         |
| A cell in `recording`, the directory unreadable                                                                                                                              | a new org answers 503; the orgs it holds keep serving                                                                                                                                                                                                                                                                                                                                                                                |
| A process stuck during first attach                                                                                                                                          | before step 3, attach refuses after `--wait` and the cell stays `recording`; after it, attach refuses, leaving the cell `enforced` and `attaching`, and a rerun resumes                                                                                                                                                                                                                                                              |
| A recording insert held open during first attach                                                                                                                             | the mode commit's tries wait behind its `FOR SHARE` lock within `--wait`; past it attach refuses and the cell stays `recording`; a rerun resumes                                                                                                                                                                                                                                                                                     |
| Attach stalls while it holds the mode table's lock                                                                                                                           | the client attempts bounded cleanup; if backend or lock release is unproved, stop and verify the exact holder before retrying                                                                                                                                                                                                                                                                                                        |
| A transaction open since before step 4's snapshot                                                                                                                            | attach refuses once `--wait` runs out (a prepared one at once), naming what it waited on, and leaves `enforced`                                                                                                                                                                                                                                                                                                                      |
| A CLI command with `LOOM_CELL` unset, started before attach's step 3                                                                                                         | it read the state while the first cell was `recording` and keeps writing ClickHouse after `serving`; the runbook sets `LOOM_CELL` on every CLI before attach, and `loom doctor --stray-orgs` names the orgs it left without a tenancy row                                                                                                                                                                                            |
| A CLI of a build before the state read                                                                                                                                       | it is not refused on an `enforced` cell; every CLI and CI image runs a build with the read before attach ([upgrades](../operations/upgrades.md#upgrade-procedure))                                                                                                                                                                                                                                                                   |
| An org found only in a log table of the first cell                                                                                                                           | never placed; once it is placed in another cell, its log rows here (`agent_events` identities among them) are outside its erasure there until they are removed by hand (the runbook's procedure); `loom doctor --stray-orgs` names it                                                                                                                                                                                                |
| An org longer than `cell.ORG_MAX_BYTES` written before the limit                                                                                                             | the first attach refuses in its scan, naming it, until the runbook's manual procedure removes it; one first found in step 4 is skipped and named                                                                                                                                                                                                                                                                                     |
| A writer, grant, role setting or subscription added after `serving`                                                                                                          | attach's checks ran once and pass it; `loom doctor` warns on the Postgres ones on every run                                                                                                                                                                                                                                                                                                                                          |
| `LOOM_SERVES_TRAFFIC=false` on a pod a load balancer routes to                                                                                                               | attach does not require its router answer; its new orgs answer 503                                                                                                                                                                                                                                                                                                                                                                   |
| A process that changes between attach's last check and the mode commit, or an HA manager that empties `synchronous_standby_names` between the checks around the durable read | not seen by attach: a window of seconds                                                                                                                                                                                                                                                                                                                                                                                              |
| A cell process with a longer `LOOM_INSTANCE_HEARTBEAT_SECONDS` than attach's                                                                                                 | it can fall outside attach's live window while live; the runbook requires one value                                                                                                                                                                                                                                                                                                                                                  |

## Rolling upgrades

The tenancy check, the placement call and the cell's state read, the CLI's org
declarations, `LOOM_CELL`, `loom cell prepare`, `scim.refuses_subject`, the email a
grant stores, `loom.cell_self`, the request gate and readiness check of a process's
fingerprint against its own row, the refusal of another cell's SCIM token, the SCIM
requirement, and the heartbeat's mode, fingerprint and `LOOM_CELL` ship in one
release. The gate on a process with `LOOM_CELL` unset, the session marker and the org
triggers ship together in one release; a first cell's prepare admits a process of the
release before it, which serves while the cell is `recording`, and `loom cell attach`,
which ships in a release after it and reads its own heartbeat key, refuses until every
live process runs that release, so the one supported rollback from an attached cell
lands on a build that sends the marker. The org triggers' migration is additive, and
the triggers do nothing on a deployment never prepared (no state rows, the `first`
row's rule). With `LOOM_CELL` unset a process reads only the mode and `first` rows and
whether a tenancy or own row exists, and only under the Postgres authority, where a
deployment never prepared has none (the tables are created empty), so builds N-1 and N
serve side by side unchanged. The router routes by host, issuer and the first cell,
never by a cell's ops, so a cell's release does not wait on the router's, but for the one
the next paragraph names.

The keys beside a heartbeat's fingerprint ([the heartbeat's keys](#the-heartbeats-keys)) are
the attach release's own heartbeat keys: a build before them reads only a fingerprint's
version and fields, so it takes a newer build's heartbeat as it is, and attach refuses a
process that reports none. That release waits on the routers': every router replica runs it
before any cell's processes do, since a prepared first cell asks the state route from its
first cell-state read until it reads `serving`, and only a router of that release answers it
from its cache ([upgrades](../operations/upgrades.md#upgrade-procedure)).

## Invariants

- An org is served by exactly one cell, the one its placement names, and its placement
  is never deleted or changed.
- A process with `LOOM_CELL` set answers nothing but its health probes and the probe
  route, and runs no worker, unless its configuration matches a value of its cell's own
  row, which the directory's overlap refusals have checked; a process with `LOOM_CELL`
  unset on a cell whose state rows count as `enforced` or are in a `partial` state (the
  `first` row's rule) answers nothing but its health probes and the probe route and runs
  no worker.
- No bearer-token issuer, host, OIDC client, org pattern or bucket prefix is accepted by
  two cells, and no deployment id or router credential is held by two.
- Every security path fails closed, with four exceptions. First, a server process with
  `LOOM_CELL` unset keeps its verdict through failed reads of the state rows with no
  stale bound: open in `open` and `recording`, as a single deployment's partial outage
  is, and gated in every other state; a failed first read fails its boot, or with
  Postgres migrations pending leaves it gated until a read lands. Second, a registered
  cell with no state, tenancy or own row (its Postgres restored to before its prepare,
  or a prepare stopped before its row write), which is open until `loom cell restore`
  (after a restore) or a prepare rerun (after a stopped prepare) and named by
  `loom doctor` meanwhile: an unread row, mode or `first` value refuses; with the
  directory unreachable, placements, uncached lookups, the `loom cell` commands that
  read or write it and the recording cells' state reads refuse while each cell keeps
  serving the orgs it holds. Third, a migrating boot that cannot create or enable an org
  trigger or the `loom.tenancy` guard on a table, or set `loom.cell_first`'s triggers back
  to always (its lock not taken in three tries of 100 ms, or a table the role cannot alter),
  logs it and serves, leaving that table unguarded until a later boot succeeds: the triggers
  are a backstop only against writers that send no marker, refusing the boot would take a
  cell down for a backstop, and attach refuses a missing trigger. Fourth, an org-free
  revocation command that cannot give access back (a plain `loom revoke-token`, not one that
  narrows or lapses, a SQL user's revocation, `loom signing-key retire`, `loom bot disable`,
  `loom sso unlink`) with `LOOM_CELL` set whose read of the cell's `first` row fails for a
  Postgres that does not answer runs and prints that the cell was not checked, since a
  revocation must work through an outage; any other failed read, a missing grant among them,
  refuses.

## Source entry points

- `server/core/cell_probe.py`: the probe route and the probe listener, which every web
  process accepts on and the workers child never opens;
- `server/core/cell.py`: the cell's own state (`loom.cell_first`, `loom.tenancy_mode`,
  `loom.cell_self`), the deployment's state rule (`state`, over the rows `refresh`
  reads, whether `loom.tenancy` holds a row among them), its fingerprint and
  `gate_reason`, the gate on processes with `LOOM_CELL` set or unset, which the request
  middleware (`server/transport/model_maintenance.py`), `verify_token`, readiness, the
  workers' lease and the heartbeat read; `server/boot.py`'s `start_cell_gate` is a
  server process's boot read;
- `server/core/cell_triggers.py`: the org triggers' marker, `ensure` (a migrating boot's
  creation and repair of the triggers and the `loom.tenancy` guard) and `missing` (what
  `loom doctor` names), with their functions in
  `schemas/pg_migrations/011_tenancy_triggers.sql` and `012_tenancy_guard.sql`;
  `server/cli/doctor_cell.py`: `loom doctor`'s cell checks;
- `server/cli/cell_attach.py`: `loom cell attach`; `server/cli/cell_attach_pg.py`: its
  Postgres reads and writes (the session checks, the attach lock, the tenancy-privilege
  and unmarked-session checks doctor shares, the trigger pass, the mode commit, the
  durable read, step 4's wait); `server/cli/cell_attach_beats.py`: its judgment of the
  heartbeats and the rescan's watch;
- `server/cli/cell_scan.py`: the first attach's org scan: the tables of both stores with
  an `org` column, judged by engine and against erasure's classification, every host of a
  cluster listing the same, and the distinct orgs of each table outside `LOG_TABLES`;
  `server/cli/cell_writers.py`: the ClickHouse writer check (the users, user directories,
  objects and replicas outside the operator's allow lists);
  `server/cli/cell_ch.py`: what both share (the cluster's hosts, the per-host reads);
- `schemas/pg_migrations/008_cells.sql`, `schemas/pg_migrations/009_cell_probe.sql` and
  `schemas/migrations/094_cell_heartbeats.sql`: the cell's tables;
- `server/auth/tenancy.py` (`ensure_held`): the tenancy check and the placement call,
  run by the op gate (`server/transport/gates.py`), `check_authorized`, `admit_org` and
  the routes that name an org; `server/transport/tenancy_gate.py` checks the token's own
  org on the routes outside the registry that act in it, and
  `tests/unit/test_cell_tenancy.py` holds the census of every route;
- `server/router/app.py` (`loom router serve`): the placement API, the first cell's state
  read (which `server/core/cell_router.py` makes on a first cell), routing by rules 1 to 4
  (`server/router/routing.py`, which decides, and the forward), the org lookup with its
  cache of placed orgs (which `loom login --org`, `loom.credentials.org_server`, asks),
  their rate limits (`server/router/limits.py`) and the checks a placement passes; its
  operator API, with the first cell's attach routes, which `loom cell limit`,
  `loom cell credential` and `loom cell remove` (`server/cli/cell.py`, with
  `loom cell which`) call with the router's operator credential
  (`LOOM_ROUTER_OPERATOR_TOKEN`; the router keeps only `LOOM_ROUTER_OPERATOR_SHA256`);
  `server/router/directory.py` and `schemas/router_migrations/`: the directory, in the
  router's own Postgres database (`LOOM_ROUTER_PG_DSN`, `loom router migrate`).

The rest of the cell code is added by the pull requests that implement this page. It
builds on:

- `server/auth/jwt.py` (`verify_token`, `AuthUnavailable`): where the fingerprint gate
  runs first;
- `server/auth/scim.py` (`tokens`) and `server/auth/scim_routes.py`
  (`scim_settings_problem`, `_authorized`): the SCIM token parser and checks;
- `server/core/instances.py`: the heartbeat that reports mode, fingerprint, `LOOM_CELL` and
  the keys beside the fingerprint;
- `server/workers/leader.py`: the lease a gated process stands aside from;
- `server/core/pg.py` (`same_deployment`): the deployment id both stores carry;
- `server/transport/gates.py` (`resolve_org`, `authorize`): where the tenancy check
  runs;
- `server/cli/_ch.py` (`run_connected`, `connect_checked`, `connect_declared`): the
  CLI's org declarations, which `tests/unit/test_cell_cli_orgs.py` requires of every
  connect;
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
- the first attach places every org with a row in any table, an org first seen during the
  attach is placed in step 4, and so is one a direct Postgres insert wrote between step 2's
  scan and `enforced`, a ClickHouse row an older CLI wrote then and a bucket object
  catalogued then; a process past `LOOM_JWT_REVOCATION_MAX_STALE_SECONDS` of failed reads of
  its own rows or mode is gated and serves again once a read lands; a pod with `LOOM_CELL`
  unset is unready, naming the setting, answers 503 to a bearer request, passes its health
  probe, answers the probe route and runs no worker on an attached cell and on a later cell
  between prepare and attach, keeps serving on a first cell in `recording`, and keeps the
  last mode it read through a failed read, `recording` included; a CLI command declaring an
  org with it unset is refused on an `enforced` or `partial` cell, an org-free one (a token
  revocation) runs, also with Postgres down on a deployment never prepared, and a
  `loom cell` store command refuses; `loom cell limit --closed` succeeds with the cell's
  Postgres down; after `loom cell credential --cell <cell> --next` both credentials place
  and read state, `--finish` refuses the old credential, for a cell finished before its pods
  hold the new one the router refuses its calls with 401, its request answers 503 writing
  nothing, and it recovers by recording the old hash again, `--revert` drops a pending
  `next`, which is then refused, and the old credential keeps placing, and a second `--next`
  while one is pending and a hash another cell records as current or next are refused;
  `loom cell limit` and `loom cell credential --next`, `--finish` and `--revert` refuse
  while the directory is unavailable; concurrent placements at one under `placements_max`
  across two router replicas write one; a CLI command declaring an org with
  `LOOM_CELL_ROUTER_TOKEN` set refuses, naming it, on a first cell once it is `enforced`
  (`attaching` included) and on a later cell, and on a first cell in `recording` runs and
  records the org; attach with `placements_max` below the orgs the first cell holds places
  them all, and a later cell's attach past its quota or closed refuses, naming the org; a
  first cell with `--closed` and `placements_max` set before step 2 attaches, and an org a
  request records between step 2 and `enforced` is placed by step 4 with no 503; after step
  3's write of the cell's mode, a process whose cache still reads `recording` sends a new
  org's request to the placement call, which a closed cell answers 503; a CLI command whose
  insert finds `enforced` refuses, naming the org, and makes no placement call; a refusal
  after step 3's write leaves the cell `enforced` and `attaching`, where a new org is placed
  through the gate; each ordering of attach's write of the mode with a recording insert
  holds as stated above (running, starting during the write, a stream of overlapping inserts
  each shorter than a try, with attach setting `enforced` within a try or two, held across
  several tries, held past `--wait` (attach refuses, the cell still `recording`), a stalled
  attach, a commit cut off by its timeout, and a commit whose connection is lost while it
  waits for the standby, which the durable read decides); a `REPEATABLE READ` transaction
  whose first statement ran before `enforced` committed, on attach's role or a second role,
  holds step 4 until it ends and its write of an unheld org is then placed, one whose first
  statement ran after is refused by the trigger, a transaction that starts after attach
  takes the lock but before its commit is visible holds step 4, a transaction opened in
  `recording` that outlives one run's `--wait` holds the rerun's step 4 after a refusal, an
  autovacuum worker on a loom table does not hold step 4 and a background worker with a
  transaction open since `recording` does, a prepared transaction (after a restart with
  `max_prepared_transactions` raised during the wait) makes step 4 refuse at once, naming
  it, with the mode still `enforced` and a rerun refused until it is resolved and the
  setting is back to 0, a write transaction open in a second database on the same server
  holds step 4 and is named, a long read on the synchronous standby, or on a second standby,
  with `hot_standby_feedback` on does not hold it, nor does a base backup running across it,
  a write held open on a `replication=database` connection holds step 4, and so does a
  `REPEATABLE READ` transaction on one, opened in `recording` and writing nothing before
  `enforced`, a long logical dump (a `REPEATABLE READ` read-only transaction) on the cell's
  database past `--wait` makes step 4 refuse, naming it, with the cell left `enforced`, and
  the first cell's attach without `pg_read_all_stats` or with `max_prepared_transactions`
  above 0 refuses before step 1 with the mode still `recording`, while a later cell's attach
  does not require either; a missing or disabled org trigger is refused by attach and named
  by `loom doctor`, a write for an unheld org on an `enforced` cell whose mode row was
  deleted is refused and the state named by `loom doctor`; on that cell a pod with
  `LOOM_CELL` unset is unready and runs no worker, and a CLI command declaring an org with
  it unset is refused, as a pod is; updating, deleting or truncating the `first` row on an
  `enforced` cell is refused, also under `session_replication_role = replica`, and a missing
  or disabled `first`-row trigger is refused by attach and named by `loom doctor`; a running
  process with `LOOM_CELL` set on a cell whose mode row is removed answers 503 to a bearer
  request and runs no worker pass, and serves again after a rerun of `loom cell prepare`
  writes the row back while it runs; a cell the directory registers with no state, tenancy
  or own row is named by `loom doctor`, its processes with `LOOM_CELL` set stay unready, a
  process with it unset serves as a single deployment, and `loom cell restore` writes both
  rows back, an attached cell then serving again and an unattached one after a prepare
  rerun; a cell with both rows absent beside a tenancy row, and one beside only its own row,
  has the function refuse an unheld org (a boot creates no trigger there), every process
  gated and `loom doctor` naming it, and a prepare rerun writes the rows back; a
  Postgres-only restore of an attached first cell to before its prepare is named by
  `loom doctor` and closed by `loom cell restore`; a prepare rerun during a pending
  `loom cell update` on an attached cell, with processes on both values and `loom.cell_self`
  lost, writes both values back and keeps both sets of processes serving, and one before
  attach refuses the two configurations; a prepare rerun on an attached cell whose `current`
  is missing and whose pods' configuration differs from the directory row refuses, and one
  before attach whose heartbeats report another bucket prefix refuses; a prepare rerun given
  a credential hash that is neither the row's `current` nor its `next` is refused; a prepare
  rerun on a `serving` later cell whose prefix holds data writes a removed mode row back; a
  cell with a mode row and no `first` row has the function refuse an unheld org (a boot
  creates no trigger there), every process gated and `loom doctor` naming it, and a prepare
  rerun writes the row back; a later cell whose mode row is set to `recording` records
  nothing, its trigger refuses an unheld org, a process with `LOOM_CELL` set there serves as
  in `enforced`, a pod with `LOOM_CELL` unset there is unready and runs no worker,
  `loom cell prepare` names the row, a CLI command declaring an org with
  `LOOM_CELL_ROUTER_TOKEN` set refuses, naming it, and the router refuses its state read; a
  build N-1 process's `memory.set` and delivery pass on a never-prepared deployment after
  N's migration succeed and write no tenancy row, and on the first cell in `recording` a
  boot creates no org trigger, and one created by hand reads the state and looks nothing up,
  a write under `session_replication_role = replica` for an unheld org is skipped (origin
  triggers), an `INSERT ... ON CONFLICT DO UPDATE` that inserts a row for an unheld org is
  refused and one that hits an existing row of it is not, a marked session's every write
  passes, and the triggers on an `enforced` cell cover every table erasure classifies with
  an `org` column but the three exclusions; and an org-keyed write while attach holds the
  mode table's lock returns at once, with new-org requests answering within their deadline
  and mode-cache reads returning at once throughout; a refusal at step 3's lock leaves the
  cell `recording` with its directory row `enforced`, and the rerun runs step 2 again; a
  `LOOM_CELL_ROUTER_TOKEN` not of the `lcr_` form is refused at boot and named by
  `loom doctor`; `loom cell credential new` prints a credential of that form and its
  SHA-256, with the directory and the stores unreachable; prepare and `--next` refuse an
  argument that is not 64 lowercase hex characters, the credential and its bare
  128-character secret included, storing nothing; orgs a first cell records in `recording`
  with no data are listed by `loom doctor`; a placement flood with no quota set is held to
  the rate limit at each router replica, `loom doctor` names the cell as one with no quota,
  and a call refused past it at one replica answers 503 with a `Retry-After` of at most 60
  s, after which its retry at that replica, with no other call of the cell admitted between,
  is admitted; a cell's first prepare, with no directory row yet, passes the deployment-id
  check; a pod or CLI that lost `LOOM_PG_DSN` or `LOOM_STORAGE_AUTHORITY` on a cell is
  refused by the authority check; the first cell's existing tokens, refresh tokens and OAuth
  clients keep working through the cutover, a logout through the shared host revokes its
  refresh token;
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
- an attach stopped after step 3's commit landed, before the durable read, says the commit's
  outcome is unknown, and its rerun resumes by the mode it reads; one refused after the
  durable read resumes from it, the mode still `enforced`; a later attach refused before its
  directory transaction leaves `attach_started_at` empty, so a rerun checks a new cell's
  refusals again, and a resumed first attach finds its configured orgs placed in this cell
  and carries on;
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
  before attach whose heartbeats match neither the present `current` nor `next` refuses;
  an attached prepare rerun with only `next` lost writes it back, and one whose present
  `current` differs from the directory overwrites it; a prepare rerun alone after a
  Postgres-only restore of an attached first cell to before its prepare leaves
  `loom doctor` naming the missing tenancy rows, and a pod with `LOOM_CELL` unset serves
  every org there until the rerun and is gated after it, while with `LOOM_CELL` set an
  org gets its tenancy row back through the placement call; a prepare rerun against
  `LOOM_CELL`-unset pods in a `partial` state reaches their probe route and writes the
  missing row back; prepare refuses a later cell's `probe_url` that reports an id other
  than its stores', attach refuses a cell whose host reports an id other than its
  `probe_url`'s, and prepare refuses a probe address argument for the first cell, and
  the router probes only `LOOM_ROUTER_UPSTREAM_PROBE_URL`; a later cell's first prepare
  succeeds; `loom cell update --probe-url` refuses an address that reports another
  deployment id or fails the nonce echo, and refuses the first cell; `loom cell update`
  moves the first cell's probe and upstream addresses only once every router replica
  runs them, after their probe and the upstream's id check, and a prepare rerun through
  a replica still on the old values (or the new, before the move) is refused, naming the
  row's address; `probe_url` with userinfo or another scheme is refused; the main port
  answers only the in-memory id, with any Host, reading no store; the router probes an
  https `probe_url` whose certificate a CA in its `SSL_CERT_FILE` signs; the probe
  listener answers 429 past its rate; and a prepare given a copy of the first cell's
  stores is refused by the nonce;
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
- `loom cell update --config` and `--finish` freshly qualify a SCIM-on target before
  any generation claim or acceptance write and recheck after local durability and
  participant turnover; without explicit `--revoke-unprovisioned`, an unqualified
  live subject refuses with no cutoff write; reverting a SCIM-on pending
  value to SCIM-off current remains supported; a process configured with another
  cell's issuer answers 503 to every bearer request, through the router or directly;
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
  echoes prepare's nonce; `--revert` requires permanent retirement of every captured
  participant, including ones already on current;
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
  admits the probe port; prepare names the router's `PROBE_UNREACHABLE` and `PROBE_BUSY`
  once `--wait` runs out; a flood on the main port leaves the probe listener's rate
  untouched;
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
