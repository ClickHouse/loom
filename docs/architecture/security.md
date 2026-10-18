# Security

Loom separates tenants by columns: every row carries the identity tuple, and every read
filters on it. The transports authenticate and set the request's `AuthInfo`; the shared
gate and the handlers authorize; nothing downstream reads a raw credential. To report a
vulnerability, and for the deployment hardening checklist, see
[SECURITY.md](../../SECURITY.md).

## Threat model

| Threat                                               | Boundary                                                                      | Enforced by                                                                                                          |
| ---------------------------------------------------- | ----------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------- |
| An unauthenticated caller reads or writes data       | Bearer token on every op; 401 before the body is read                         | `server/transport/http.py` (`_extract_auth`), `server/auth/jwt.py`                                                   |
| A forged or tampered token                           | Signature, algorithm allow-list, `kid`, issuer, audience, time claims         | `server/auth/jwt.py` (`verify_token`)                                                                                |
| A revoked token keeps working                        | Per-process revocation snapshot, fail closed until loaded                     | `server/auth/revocation.py`                                                                                          |
| Tenant A reads tenant B's rows                       | Org + namespace check on every call; row-scoped ops check the row's namespace | `server/auth/authz.py` (`check_authorized`, `row_in_scope`)                                                          |
| A refusal confirms that an id or namespace exists    | `found=False` or a generic "cannot access", never a 403 naming the target     | Row-scoped handlers; the restricted gate                                                                             |
| One caller exhausts shared capacity                  | Token buckets per namespace, org and actor                                    | `server/core/ratelimit.py`                                                                                           |
| An agent is induced to echo or enumerate memory      | `read_scope` ceilings, exposure ledger, breadth budgets, prompt fencing       | `server/identity/domain/read_scope.py`, `server/identity/app/policy_gate.py`, `server/memory/domain/prompt_fence.py` |
| Stored text carries instructions into a later prompt | Recalled memory is fenced with a per-prompt nonce                             | `server/memory/domain/prompt_fence.py`                                                                               |
| Secrets written as memory are embedded and served    | Sensitivity detection; `high` rows are never embedded                         | `server/memory/domain/sensitivity.py`, `sensitivity_sweep` worker                                                    |
| Audit rows are edited or deleted                     | Per-row hashes plus a hash-chained checkpoint per window                      | `server/observability/audit.py`, `audit_verifier` worker                                                             |
| Erasure leaves residue                               | Checkpointed saga ending in a residue census                                  | `server/erasure/app/saga.py`, `server/erasure/store/census.py`                                                       |

Out of scope: an attacker who already operates the ClickHouse database, vulnerabilities
in ClickHouse or the integrated backends, and third-party MCP clients.

## Tokens

Credentials are JWTs. `loom mint-token`, the dashboard, self-signup, OIDC login, the
OAuth server and a gateway all produce the same claim shape. Minting and the claim table
are in [tokens and grants](../guides/tokens-and-grants.md).

**Verification** (`verify_token` in `server/auth/jwt.py`):

- Algorithms are an allow-list: `HS256` and `RS256`. `alg=none` and every other
  algorithm are refused.
- The key is picked by the header's `kid` and `alg` (`server/auth/keyring.py`): a key in
  `loom.signing_keys`, the root secret (kid `default`, HS256), or a gateway's RS256 public
  key in `LOOM_JWT_PUBLIC_KEYS_JSON`. A missing `kid` means `default`; an RS256 token of
  `default` verifies with a PEM RSA root secret (which signs as `default`) and with the
  gateway map's `default`. A `kid` with no key under the token's algorithm is refused,
  and so is a retired one, unless the gateway map still lists it with a key this
  deployment never retired: a retire stops Loom's keys under every kid, and a gateway
  kid verifies until it leaves the map.
- `iss` must be `LOOM_JWT_ISSUER` or in `LOOM_JWT_TRUSTED_ISSUERS`; `aud` must be
  `LOOM_JWT_AUDIENCE`. `iat`, `sub`, `iss` and `aud` are required. `exp` is enforced when
  present, with `LOOM_JWT_LEEWAY_SECONDS` (30) of skew.
- A Loom claim of the wrong shape (non-string `org`, non-list `namespaces`, unknown role)
  refuses the token.
- `verify_bearer` (`server/auth/bearer.py`) parses the bearer for routes outside the
  operation transport: 401 `AUTH_INVALID` for a missing, foreign-scheme or failing token;
  503 `AUTH_UNAVAILABLE` with `Retry-After` while revocation or the keyring is not loaded.

**Signing keys.** `LOOM_JWT_SECRET` is the root secret: it signs as kid `default` and
seals the private half of every other key in `loom.signing_keys`, so a database reader
without it cannot mint tokens. Every process loads the keyring at boot and every
`LOOM_JWT_KEYRING_REFRESH_SECONDS` (30 s); the newest active key signs, else the root
secret (HS256, or RS256 for a PEM RSA one; any other PEM signs nothing and sign-ins answer
503 `NO_SIGNING_KEY`). A keyring last read longer ago than
`LOOM_JWT_REVOCATION_MAX_STALE_SECONDS` (at least three refresh intervals) verifies
nothing (503 `AUTH_UNAVAILABLE`) and signs nothing (503 `BACKEND_UNAVAILABLE`) until a read
lands: a retire since may be unseen. The public keys of RS256 keys are served at
`GET /v1/.well-known/jwks.json`. The `default` row holds `HMAC(root, label)`, written by
the workers leader or a keyring command (never a replica's first boot, which would trust a
mistyped secret); a process whose root does not match it reports
`keyring_root_mismatch`, and the keyring commands and `loom mint-token` refuse there.

**Rotation.** `loom signing-key rotate [--algorithm HS256|RS256] [--activate-in 300]`
writes a key whose `activate_at` lies `--activate-in` seconds ahead on the ClickHouse
server's clock; it must exceed two of the slowest process's refresh intervals (each
process records its own in `loom.keyring_readers` hourly), so every process verifies the
key before any token carries it. Until a process has read the keyring once,
it refuses tokens with a retryable 503 and signs none. Tokens signed under an older `kid` verify until
`loom signing-key retire <kid>`, which invalidates them all within a refresh interval.
`default` can be retired too, for an RS256-only deployment; a retire that would leave no
key to sign with is refused. One retire runs at a time across the deployment (a cluster
claim, `server.core.leases.holding`), and it reads the keyring with sequential
consistency, so two retires at once cannot each leave the other's key as the last signer. Changing `LOOM_JWT_SECRET`
re-keys everything: keyring keys and stored credentials sealed with the old value no
longer open.

**Expiry.** Tokens have no `exp` unless minted with a TTL (`--ttl-days`). Every token
Loom mints carries a `jti`; an outside token without one is identified by its digest.

**Revocation** (`server/auth/revocation.py`). `loom.revoked_tokens` holds revoked `jti`
or `sub` values, optionally with an issued-before cutoff. Each process loads a snapshot at
boot and refreshes it on its own loop every `LOOM_JWT_REVOCATION_CACHE_TTL_SECONDS` (30 s).

- Until the first load succeeds, every token gets 503 `AUTH_UNAVAILABLE`.
- After that, a ClickHouse outage keeps the last good snapshot for at most
  `LOOM_JWT_REVOCATION_MAX_STALE_SECONDS` (600 s, and it must be larger than the refresh
  interval); past it every token gets 503 again until a refresh lands.
- `/v1/health` reports `revocation_not_loaded` (error), `revocation_stale` (warning,
  once older than `max(300 s, 10 × the refresh interval)`) and `revocation_too_stale`
  (error, past the max-stale bound).

**Why fail closed.** Admitting tokens before the list is loaded would accept a revoked
token on every fresh process, which is when an operator restarts the fleet after
revoking a leaked token. The cost is a short 503 window at boot.

**A claim goes no further than the grant.** A token names namespaces, and a flat token's
claim on one would otherwise admit it with the token's own role. Where the subject holds a
grant row on the namespace, of any status, the claim is capped at that grant instead
(`authz.claim_cap`): a revoked or lowered grant ends every older token's claim there, a
pending or rejected request caps it at no access, and a scoped token keeps its own role
there capped at the live grant. A namespace the subject never held a grant on (a `loom
mint-token` claim) keeps the claim; grant rows are never deleted, so a revocation is never
forgotten.

- The subject's rows, under every spelling the directory ties to it, are read in one
  statement per (org, subject) into a per-process cache (`server/core/grant_cache.py`) kept
  for `grant_cache_ttl_seconds` (10 s). A grant write in this process drops at once the
  entries whose read covered the spelling it wrote, and any read in flight; a write to the
  accounts that tie a person's spellings drops the org's. The server's other web processes
  (`LOOM_WEB_WORKERS`) read again at once: the write marks a file in their runtime directory,
  and an entry read before the mark is not served. Another server sees either within the TTL.
- A failed re-read serves the cached entry while it is younger than
  `jwt_revocation_max_stale_seconds`, the bound a token check already lives with, and
  answers a retryable 503 past it.

**One exchange per credential** (`server/auth/oauth.py::_exchange`). The OAuth store marks
a code or refresh token used by writing a new ReplacingMergeTree version, so reading and
then marking cannot keep two concurrent exchanges apart. `/oauth/token` first takes the
credential's deployment-wide claim (`server.core.leases`: a Keeper key in strict mode),
so exactly one presenter runs on any replica.

- **The other presenter** gets a retryable 503, not `invalid_grant`: it may be the owner
  retrying after a lost reply. Its retry, after the release, finds the credential used.
- **Time limits.** Each phase has a wall-clock limit that counts retries and pool waits:
  10 s for a first read that finds the credential (a made-up one is refused there, before
  any claim), 10 s to take the claim, 60 s to read the credential again and decide, 10 s
  to use it up. A live claim is at most 80 s old; it expires after 120 s, and the next
  presenter takes it over.
- **A consume that did not finish** (failed or cancelled) may still land, so the claim is
  kept until it expires. A refresh answers 503 with `Retry-After: 120`; a code, which
  expires sooner, answers with its tokens. A retry inside those 120 s finds the claim held
  and is told to retry in 5 s, as any concurrent presenter is.

Every token an OAuth exchange mints takes its issue time before it reads the subject's
revocations from ClickHouse, so a `revoke-token --subject` that landed while it ran still
postdates the token.

## Authorization

### Roles

`Role` (`server/operations/base.py`) is ascending:

```text
VIEWER < WRITER < ADMIN < ORG_ADMIN < PLATFORM_ADMIN
```

Each op declares `required_role`.

- Grants, a scoped token's `ns_roles` and `loom mint-token --roles` hold only `viewer`,
  `writer` or `admin`. An `ns_roles` entry naming anything else confers nothing.
- A flat `roles` claim may name any role, so a gateway can sign `org_admin`. Loom itself
  never assigns it.
- `platform_admin` is written into the token at login for entries of
  `LOOM_PLATFORM_ADMINS` (`platform_roles` in `server/auth/login.py`). A handle matches a
  password account; an email matches only an OIDC-verified sign-in. Self-signup emails are
  unverified and never confer a role. An OAuth refresh re-derives the role and never
  promotes.

### The gate

`check_authorized(auth, role, org, namespace)` (`server/auth/authz.py`) runs on every
authenticated call, from `gates.authorize` in both transports:

1. A scoped token (with `ns_roles`) on a request missing its org or namespace gets 400
   `IDENTITY_INCOMPLETE`, unless the op is `deployment_scoped` or `row_scoped`; those pass
   on the token's best namespace role in its own org.
2. A delegated token (a `creator` claim, minted by `server/auth/delegation.py`) is refused
   when its creator's account is disabled, on an op with no namespace that writes unless the
   creator holds that role somewhere in the org, and on a namespace needs its own claim to reach
   the role and the creator's live grant to reach it too (`_check_delegated`; restricted
   namespaces also take the restricted gate). Nothing below applies to it.
3. A restricted namespace goes to the restricted gate only.
4. A viewer read of an org-readable code namespace (`LOOM_CODE_ORG_READABLE` or the
   entry's `org_readable`) passes for any principal of that org.
5. A scoped token's `ns_roles` decide for the namespaces they name, in the token's org
   only. Grants are not consulted for them, except viewer reads of a code namespace.
6. A token acts in its own org only (or `*`, or as platform admin).
7. A flat token's roles apply to every namespace its claims cover. Otherwise a live grant
   in `loom.namespace_grants` decides: unrevoked and `active` (`grant_sql.LIVE_GRANT`); a
   `pending` join request grants nothing. A service account with no grant reads its
   creator's. A grant raises a flat token's role, never lowers it. A failed grant read
   counts as no grant.

The `all_mine` fan-out and the org-wide rollups use the same predicate, so they never
exceed the gate.

### Row-scoped operations

Ops addressed by an org-wide id (`memory.get_by_id`, `memory.update_by_id`,
`memory.redact`) declare `row_scoped=True`. The transport only checks that the token
holds the role somewhere in the org. The handler fetches the row, calls
`authz.row_in_scope(org, row.namespace, role)`, and answers `found=False` on refusal.

**Why not 403.** A 403 confirms the id exists in another tenant. "Not found" makes a
foreign row and a missing row look the same.

**CI gates.**

| Test                                                     | Checks                                                                                                                                                                                                                                                                                                                                                                            |
| -------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `tests/integration/test_cross_principal_leakage.py`      | Seeds canary nonces into one tenant's memories, facts, knowledge, blacklist reasons, grants, erasure requests and session briefs, then calls every read op of the covered groups as another namespace's writer and viewer and as another org. Any nonce in a response, or a status outside `{200, 403, 404}`, fails. New read ops in a covered group are picked up automatically. |
| `tests/integration/test_restricted_namespace_leakage.py` | The same for restricted code namespaces.                                                                                                                                                                                                                                                                                                                                          |
| `tests/unit/test_op_inputs_after_gate.py`                | No input model answers 422 before the gate runs.                                                                                                                                                                                                                                                                                                                                  |

### Restricted namespaces

A namespace classified `restricted` (in `loom.namespaces` or its repository entry; either
restricts) turns off every widening rule (`_check_restricted` in `server/auth/authz.py`).
A call passes only when all of these hold:

- the token's org is the namespace's (`*` does not count);
- the token names the namespace literally;
- the token's own role reaches the op's;
- a live direct grant for the token's own subject reaches it (a service account does not
  inherit its creator's grant).

No org-readable rule, `visibility=public`, `org_admin` or `platform_admin` bypasses it. A
refusal is the generic "cannot access". If the classification cannot be read, access is
denied.

Every admitted request writes a `namespace.restricted_access` audit row, and code search
traces keep only the query digest; neither can be switched off.

| Setting                        | Default | Effect                                                            |
| ------------------------------ | ------- | ----------------------------------------------------------------- |
| `LOOM_RESTRICTED_SERVE`        | `true`  | `false` refuses every restricted read while ingest continues.     |
| `LOOM_RESTRICTED_AUDIT_INLINE` | `true`  | `false` writes the audit row beside the answer (a kill loses it). |

### History policy

`LOOM_POLICY_MODE` (`off` by default, `shadow`, `enforce`) turns on rules written with
`policy.set` over the event ledger (`server/identity/app/history_policy.py`, `policy_gate.py`). A
rule can `forbid` or `require` an op given what the same agent, session or user did
before. The gate runs after authorization and before the handler. In `enforce`:

- a denial is 403 `POLICY_FORBIDDEN`;
- an evaluation error denies a write-shaped op (`writer` or above) and allows a read.

A `breadth_within` rule, over `loom.memory_exposures`, caps how much of a namespace one
credential is shown per window. See [tokens and grants](../guides/tokens-and-grants.md).

## Identity modes

The modes (`single-user`, `trust-network`, `gateway`) are described in
[identities and namespaces](../concepts/identities-and-namespaces.md) and their settings
in [deployment](../operations/deployment.md). Never expose `single-user` beyond the
local machine: auth is off. In `trust-network`, passwords are argon2id and there is no
email verification or password reset. In `gateway`, Loom never sees a password.

**Impersonation.** A subject in `LOOM_TRUSTED_CALLERS` may send `X-Loom-On-Behalf-Of`
over HTTP to act as another principal. The header's subject becomes the effective
subject, and `impersonated_by` records the caller on its `agent_events` rows.

**OIDC and OAuth.** OIDC login trusts only the configured issuer's discovery, verified
emails and nonce-bound id_tokens. The OAuth server (`LOOM_OAUTH_ENABLED`, off by
default):

- accepts only https, loopback-http or app-scheme redirect URIs;
- binds codes to client, redirect and PKCE S256;
- stores codes and refresh tokens only as hashes;
- binds each refresh to its client; a refresh honours revocation, re-checks that the
  account is active and re-derives its roles.

**Unauthenticated routes.** Sign-up and password sign-in are rate-limited
(`server/auth/attempts.py`) per client address (the connection's peer, which uvicorn
replaces with the `X-Forwarded-For` client only when the peer is in
`LOOM_TRUSTED_PROXIES`), and sign-in also per identifier. An unknown account costs one full argon2id
hash, like a wrong password. Attempts are rows of `loom.auth_attempts` (the kind, an HMAC
of the key keyed by the root secret, the time; a 1-day TTL), so the limits hold across
every process of the deployment and a database reader cannot recover an address by
hashing them all. Both counts read with sequential consistency, so a process sees the
attempts another replica recorded a moment ago. A sign-up is counted before it is admitted. A sign-in is recorded and
recounted before its password is checked, under its identifier and its address, and
released once it succeeds or its check fails to run, so a concurrent burst across
processes reaches the check at most the limit's number of times (10 per identifier, 50 per
address, per 15 minutes). A release carries its attempt's time, so the two leave the window
together. What a refused attempt costs is bounded per process: a key seen over its limit
is refused for 10 s without a query, at most `LOOM_AUTH_ATTEMPTS_CONCURRENCY` (8) checks
run at once (past it a caller waits a second, then gets 429 with `Retry-After: 1`), and
attempts are waiting async inserts, so a burst shares parts and cannot take the
ClickHouse pool every route uses.

**Client-facing URLs** (OAuth metadata, setup config, claim links) come from
`LOOM_OAUTH_ISSUER` / `LOOM_PUBLIC_BASE_URL`, else (only on a loopback bind) the
request's own scheme and `Host`, validated as `http(s)://hostname[:port]`.
`X-Forwarded-Host` is never read. The OIDC callback comes from configuration only.

**Exposure.** A non-loopback `LOOM_BIND_HOST` refuses to start without
`LOOM_PUBLIC_BASE_URL`, and with auth off or the dev JWT secret unless
`LOOM_ALLOW_INSECURE=1`. `LOOM_CORS_ORIGINS=*` refuses to start (CORS allows
credentials). With auth off, a request whose `Host` is not a loopback host or the
public URL's host gets `421`, so a DNS-rebinding page cannot drive a local server. A
request whose `Origin` is not the server's own (and is not in `LOOM_CORS_ORIGINS`), or
whose `Sec-Fetch-Site` is `cross-site` or `same-site`, gets `403`, so a page on another
site cannot post to it either: every request with auth off except a top-level page load
(`Sec-Fetch-Mode: navigate`, `Sec-Fetch-Dest: document`, GET or HEAD), and every
non-GET request with auth on, because in proxy mode the front proxy adds the bearer to
whatever the browser sends. The server's own origin is `LOOM_PUBLIC_BASE_URL` (scheme,
host and port) when it is set; otherwise an `Origin` whose host and port match the
request's `Host` counts, whatever its scheme, so a TLS proxy whose `X-Forwarded-Proto`
is not trusted still works. `loom doctor` and `/v1/health` report each of these.

**Browser.** Every response is `X-Content-Type-Options: nosniff`. The dashboard page carries a
Content-Security-Policy whose `script-src` admits the bundle's origin and the one boot
snippet by a per-response nonce, never `unsafe-inline`. An uploaded document is
attacker-chosen bytes under an attacker-chosen type, so `knowledge.download` previews
inline only plain text (text, Markdown, CSV and JSON, all served as `text/plain`) and
PNG, JPEG, GIF and WebP whose bytes match their type; anything else (HTML, SVG, XML, PDF,
unknown, mismatched) is an `application/octet-stream` attachment, and every download
carries `Content-Security-Policy: sandbox; default-src 'none'`. The dashboard applies the
same allowlist itself, because a `blob:` URL ignores the server's headers: it re-types
text as `text/plain` in an `<iframe sandbox="">`, shows images with `<img>`, and
downloads everything else, PDF and SVG included.

## Rate limiting

`server/core/ratelimit.py` keeps token buckets per (namespace, op class) and per (org, op
class), plus an optional per-actor tier (`LOOM_RATELIMIT_ACTOR_FRACTION`). If any bucket
refuses, none is decremented.

| Op class (`op_class_for`) | Covers                                           |
| ------------------------- | ------------------------------------------------ |
| `memory`                  | Memory and conversation ops, writes and searches |
| `memory.read`             | Exact-key reads such as `memory.get`             |
| `knowledge.add`           | Every other `knowledge.*` op                     |
| `knowledge.search`        | `knowledge.search`                               |
| `code`                    | `code.*` (`code.outcome` rides `memory`)         |
| `default`                 | Everything else                                  |

The org bucket comes from the verified token, never the body. A refusal is 429
`RATE_LIMITED` with `Retry-After` and `X-RateLimit-*` headers. The limiter fails open: a
limiter error admits the call and is counted. Sizing and the shared backend are in
[failure and recovery](failure-and-recovery.md).

## Refuse: the blacklist

`blacklist.add` registers a `substring` or `regex` pattern at `global`, `org` or
`namespace` scope (`server/memory/app/blacklist_guard.py`). A regex is length-capped, rejected if it nests
quantifiers, and probed against a time budget, so it cannot stall writes. Matching writes
get 422 `BLACKLISTED` on `memory.set` and the other write paths before anything is
stored; `sweep=true` tombstones existing matches. Replicas invalidate their pattern
caches from a one-row version watermark in `loom.blacklist_version`, checked every
`LOOM_BLACKLIST_VERSION_CHECK_INTERVAL_SECONDS` (2 s).

## Prompt safety

Everything an agent stores is untrusted input to later prompts. Two mechanisms bound it:

- **Fencing.** Recalled memory in a synthesis or extraction prompt is always wrapped in `<<memory LABEL NONCE>>` and
  `<</memory NONCE>>` (`fence` in `server/memory/domain/prompt_fence.py`). The nonce is an
  HMAC over `LOOM_PROMPT_FENCE_SECRET` (`LOOM_JWT_SECRET` when empty) and the prompt
  parts. An identical prompt renders identically, so the response cache still works, but
  a memory author cannot predict the closing marker; a forged one is neutralised.
- **Capped priors.** `LOOM_MEMORY_PRIOR_CAP` (0.08) caps `memory.search`'s metadata
  priors (confidence, soft tags, categories), so a writer's self-declared confidence only
  breaks ties. A request's `prior_cap` overrides it.

Loom does not classify stored text as instruction-like and refuses nothing on that
basis. The read-side bounds (`read_scope`, `top_k`, the exposure ledger, breadth budgets)
are in [SECURITY.md](../../SECURITY.md#retrieval-and-memory-leakage).

## Sensitivity

`server/memory/domain/sensitivity.py` classifies a value with deterministic regexes:

| Level    | Matches                                                                     |
| -------- | --------------------------------------------------------------------------- |
| `high`   | A credential shape: prefixed API key, access key id, private-key block, JWT |
| `medium` | An email or phone number                                                    |
| `low`    | Set only by a caller                                                        |

A caller-supplied level wins. Detection is `LOOM_MEMORY_SENSITIVITY_AUTODETECT`. A `high`
row is never embedded (`is_embedded` in `server/memory/domain/memory_types.py`), so no search leg
returns it; a key-only read of one writes a `memory.sensitive_read` audit row. Search
demotes labelled rows on questions that did not ask for a contact detail
(`LOOM_MEMORY_SEARCH_SENSITIVITY_DEMOTE`).

The detector runs on `memory.set` and extraction. The `sensitivity_sweep` worker (on,
daily) labels rows from paths that skip it: a new version per row and, for `high`,
tombstoned embedding rows and entity vectors.

## Audit log

`emit` in `server/observability/audit.py` writes `loom.audit_log` rows. `row_hash` is
SHA-256 over the canonical JSON of timestamp, event type, org, namespace, actor, subject
and request id. Every memory write leaves a hash-only row; ops that declare `audit=True`
leave one per successful call.

The `audit_verifier` worker recomputes row hashes and seals closed windows into
`loom.audit_checkpoints`, chained by `prev_checkpoint_hash`, so a deleted row shows as a
short recount. The chain is over windows because per-row chains fork under concurrent
writers. An audit insert that fails is logged and never fails the write it records.
Settings, the verifier's reports and offline verification are in
[audit](../features/observability/audit.md).

## Erasure

`erasure.request` runs a checkpointed saga over every table that can hold a user's or a
namespace's content, including federated backends (Langfuse and ClickStack;
`server/erasure/app/backends.py`) that fail closed. It ends in a residue census; any live row
in scope fails the saga. The saga, the census registry and the rule for new tables are in
[memory architecture](memory-architecture.md#erasure-saga); the caller's view is in
[retention and erasure](../features/memory/retention-and-erasure.md). Erasure and audit
hashing are engineering primitives, not a compliance guarantee.

## Secrets

- **Dev signing secret.** A deployment (`LOOM_DEPLOYMENT=1`) that requires auth refuses
  to start with the checked-in dev secret; only `LOOM_INSTANCE_LABEL=deploy:dev` is exempt
  (`require_real_signing_secret`). `loom doctor` and `/v1/health` flag it elsewhere
  (`dev_jwt_secret`).
- **Stored tokens.** Private-repository tokens, tracker-source passwords and provider
  keys are encrypted at rest with a key derived from `LOOM_JWT_SECRET` and a per-purpose
  salt (`seal` and `unseal` in `server/core/secrets_at_rest.py`), as are the signing
  keys in `loom.signing_keys`. Changing the secret makes them undecryptable; re-enter
  the credentials and re-mint tokens.
- **Ledger arguments.** `loom.agent_events` stores free-text arguments as
  `sha256:<digest>/<length>`, not text (`server/observability/event_args.py`).
- **Errors.** A 500 carries only the request id, never exception text.
- **Repository.** `make lint` runs `scripts/gates/check_no_secrets.py` over tracked files.

## Invariants

- Nothing below the transports reads a raw credential; handlers read `AuthInfo` from the
  request context.
- Only `HS256` and `RS256` verify; a token without `iat`, `sub`, `iss` or `aud` is
  refused.
- A process refuses every token until its revocation snapshot has loaded once.
- A namespace is never authorized without an org.
- A row-scoped op answers `found=False` outside the caller's scope, never 403.
- A restricted namespace admits only a literal, same-org, directly granted caller, and
  denies when its classification cannot be read.
- A `high`-sensitivity value has no embedding row.
- Every memory write leaves a hash-only audit row; no audit row carries a value.
- An erasure saga succeeds only when its residue census finds nothing.

## Source entry points

| What                        | Where                                                                                                                       |
| --------------------------- | --------------------------------------------------------------------------------------------------------------------------- |
| Token verification and mint | `server/auth/jwt.py` (`verify_token`, `mint_token`, `require_real_signing_secret`)                                          |
| Bearer parsing              | `server/auth/bearer.py` (`verify_bearer`)                                                                                   |
| Revocation                  | `server/auth/revocation.py` (`load_at_boot`, `is_revoked`)                                                                  |
| Authorization               | `server/auth/authz.py` (`check_authorized`, `row_in_scope`); `server/core/grant_sql.py` (`LIVE_GRANT`)                      |
| Shared gate                 | `server/transport/gates.py` (`authorize`)                                                                                   |
| Rate limiter                | `server/core/ratelimit.py` (`Limiter.check`, `op_class_for`), `server/core/ratelimit_shared.py`                             |
| Blacklist                   | `server/memory/app/blacklist_guard.py` (`enforce_on_insert`), `server/memory/app/blacklist_sweep.py`                        |
| Prompt fence                | `server/memory/domain/prompt_fence.py`                                                                                      |
| Sensitivity                 | `server/memory/domain/sensitivity.py`, `server/memory/app/sensitivity_sweep.py`                                             |
| Audit                       | `server/observability/audit.py`, `server/insights/domain/audit_checkpoints.py`, `server/insights/workers/audit_verifier.py` |
| Erasure                     | `server/erasure/app/saga.py`, `server/erasure/store/`, `server/erasure/workers/recovery.py`                                 |
| Leakage gate                | `tests/integration/test_cross_principal_leakage.py`                                                                         |

## Related

- [Tokens and grants](../guides/tokens-and-grants.md): minting, claims, grants, joining.
- [Identities and namespaces](../concepts/identities-and-namespaces.md): the model.
- [Audit](../features/observability/audit.md): reading the audit log.
- [System overview](system-overview.md): where each gate runs.
