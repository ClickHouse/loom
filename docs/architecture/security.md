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

**One exchange per credential.** `/oauth/token` spends a code or refresh token by
compare-and-set in Keeper, so of two concurrent presenters exactly one wins; see "Each
credential is spent exactly once" below. A read that fails answers a retryable 503 and
leaves the credential usable.

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
emails and nonce-bound id_tokens, sends PKCE to the provider, and refuses a callback whose
`iss` names another issuer (RFC 9207). With `LOOM_OAUTH_LOGIN_PROVIDER=oidc` the provider
is the only way in: password sign-in and self-signup are refused, since a local password
would bypass the provider's MFA and outlive a deactivation there.

A verified email reaches a password account only once the account is linked to it
(`loom.sso_links`, `server/auth/sso_links.py`). Self-signup never verified the email it was
given, so linking on an email match alone would hand the account to whoever registered the
address first. The owner links it by proving the account's password once from the SSO
sign-in, under the password sign-in limits; an operator can vouch for a whole domain with
`loom sso adopt`, which lists each account and its owner before `--apply`, skips an email
whose accounts span orgs or owners or that SSO already reaches, and revokes the tokens the
adopted subjects held (whoever registered an address may hold some). It applies only once
SSO is on, when the password no longer signs in to mint a token past that revocation. A linked account keeps
its subject, grants and data. An email whose linked accounts disagree (two owners, two orgs)
signs in to none, and JIT adds nothing beside them. A person who says a password account
naming their address is not theirs goes on to a JIT account, unless an account naming the
address is revoked: offboarding stands, and a squatter's account is released
(`loom claim-release` clears its email and its links) rather than revoked.

Loom stays the authorization server for every client (the dashboard, `loom login`, MCP
clients); the provider only authenticates the person. The OAuth server:

- accepts only https, loopback-http or app-scheme redirect URIs;
- binds codes to client, redirect and PKCE S256;
- stores codes, refresh tokens and device codes only as hashes, user codes keyed with
  the server secret, and the provider's refresh token sealed
  (`server/core/secrets_at_rest.py`);
- binds each refresh to its client; a refresh honours revocation, re-checks that the
  account is active and re-derives its roles.

**Sessions.** Each sign-in starts a session family. Its access tokens (one hour) carry the
family as their `sid` claim, and its refresh tokens rotate within it. Revoking the family in
`loom.revoked_tokens` ends every token of the session: on logout (`/oauth/revoke`), when the
provider refuses the user at a refresh, and when a spent refresh token, code or device code
comes back (RFC 9700 reuse detection). A spent credential coming back means a copy is in
other hands, or a client lost the answer that spent it; rotation cannot tell the two apart,
so it does what is safe for both: the session ends, and its owner signs in again. That
verdict reaches no further: tokens minted from a `loom login` or dashboard SSO session, the
person's other sessions and the subject are never touched by it (a third-party MCP client's
minted tokens end with its session, below), and nothing automatic writes a subject-wide
revocation (which could replace an operator's outright ban). A subject revoked outright is
issued nothing.

Tokens delegated from a session of Loom's own sign-in (`loom login` and the dashboard's SSO
sign-in; the access token's `fps` claim) are not clipped to the session's hour, and they
outlive logout. Every one is saved to the person's token list, where they see and revoke it,
and lasts what they asked for (blank 90 days, 0 never). A session a third-party MCP client
holds is not one: what it mints ends with its access token at the latest, and at once when
the session is revoked (the session is one of its `parents`), so disconnecting the client
ends its reach. Any other parent bounds its children by its own expiry. Under OIDC each
refresh first spends the provider's refresh token, so a user deactivated there loses their
sessions within the hour. Their machine tokens are not re-checked at the provider:
offboarding, or a session found stolen, revokes them (by subject, or each from the owner's
service-account list). A revocation of a token that never expires lasts as long as the table
can hold a row (2105). A provider that issues no refresh token gets no Loom refresh token. A
session's refresh tokens roll: each refresh lasts 30 days from itself, so a session used at
least monthly does not end on its own, and a password change does not end it; under OIDC the
provider's own session limits apply at every refresh. A stolen access token can mint machine
tokens, listed ones that never expire included, without touching the refresh token, so
replay detection only covers a stolen refresh token; the owner sees and revokes them in
their list.

**Each credential is spent exactly once.** What a sign-in spends or moves lives in
`loom.oauth_signin_state`, a KeeperMap table (`server/auth/claims.py`), one key each: a
code, a device grant, the id of each session's current refresh token, an MCP approval
awaiting its click, and a single-use mark for each finished IdP round trip. Every change
is a compare-and-set, a strict-mode Keeper write applied only while the key still holds
the value the request read: spending a refresh
token swaps its session's key from that token's generation and id. Of two requests
presenting it one wins; the other is told to retry, and its retry finds a later generation,
which proves the token spent: a Keeper read may lag behind but never runs ahead, so nothing
short of a later generation is taken for a replay. A spend left unfinished for two minutes
(its process died before saving the next token, so none was handed out) is taken over by
the next presenter of that token, one of them by compare-and-set. The guarantee is Keeper's,
not a lease's: no timing or replica lag can let two requests through. Codes and device
grants are the Keeper value itself; a refresh token's grant is a `loom.oauth_state` row
written before the token is handed out and read with sequential consistency. Each key
carries its credential's expiry and is swept once it passes. Starting a sign-in writes
nothing on the server, so only an authenticated callback, a device sign-in start or a code
adds a key. Sign-in starts are limited per address (an IPv6 /64 counts as one), and the
table's key limit is the backstop: past it new sign-ins answer 503 and `/v1/health` reports
`oauth_signin_state_full`. When Keeper fails mid-change, the change is undone before the
client is told to retry, so its retry is not taken for a replay. A refresh token without a
family is refused, so its owner signs in again.

**The IdP round trip is bound to its browser.** What the callback needs (the nonce, the
PKCE verifier, where to send the result) travels sealed in an HttpOnly cookie of the
browser that started the sign-in, never stored on the server. The callback finishes a
sign-in only with that cookie, once: its state is marked used in Keeper. The callback URL
alone (from a log, browser history or a screenshot) finishes nothing. The cookie is scoped
to `/oauth/callback`, not `__Host-`, so it is not sent on every request; a site on a
sibling subdomain could therefore plant its own sign-in for the victim to finish (login
CSRF), which serving Loom on a host of its own rules out.

**A revocation that cannot be written is owed.** When ending a session cannot write
`loom.revoked_tokens`, a `revocation_owed` row in `loom.oauth_state` stands in for it for
as long as a session revocation lasts (30 days): the process that wrote it refuses the
session at once, and the revocation snapshot every process loads (at boot and on its
refresh cadence) counts the family as revoked, after any restart. A refresh asks storage
itself whether its session ended, so it never waits for a snapshot. When neither write
lands, the request is answered 503 `temporarily_unavailable`, never a final
`invalid_grant`.

OAuth answers carry `error` and `error_description` at the top of the JSON body (RFC 6749
§5.2, RFC 8628 §3.5).

**Device grant** (RFC 8628, `server/auth/device.py`). `loom login` gets a device code and
an eight-letter user code (20 letters, about 2^34.6) that both live ten minutes. Approval
happens on a page shown only after sign-in. The page names the code, the requesting
address and the time, and it needs a click. It carries a one-time ticket bound to that
signed-in user, so a forged cross-site POST approves nothing, and it cannot be framed.
Wrong codes (10 per address per 15 minutes) and new device grants (30) are counted across
replicas (`loom.auth_attempts`); polls (one per interval, else `slow_down`) are paced per
process.

Whoever approves a user code first decides the grant, as RFC 8628 §5.4 warns. Two people
signing in with one code end it for both, but someone who saw the code (a screen share)
and approves before its owner signs the terminal in as themselves. The owner, entering the
code or coming back from the provider, finds it already used and is told to run
`loom logout`, and `loom login` prints the account it signed in as.

**Bots and CI** (`server/auth/machines.py`, `loom.machine_identities`). A bot is
registered by an operator or a platform admin with the namespaces it may use, and proves
itself at `/oauth/token` for an hour-long service-account token: with a key, by a client
assertion it signs (RFC 7523 `private_key_jwt`: issuer and subject its name, audience
this token endpoint, at most five minutes long, its `jti` spent once in Keeper, ES256,
RS256 or EdDSA only), or as a CI job, by token exchange (RFC 8693) of the OIDC token its
platform issued for the run, checked against the registered issuer's keys (fetched only
for a registered issuer), the audience and every registered claim. A CI token may be
exchanged again while it lives (every client of the job, each renewal), and the token
issued never outlives it. A CI trust must name at least one claim: any job on a platform
can ask for any audience. A token is issued as of the moment before the bot is read, so a
disable landing during the exchange still cuts it off.
Disabling a bot refuses later proofs and revokes its tokens; no refresh token exists to
outlive it. The cutoffs an operator action writes (a bot replaced or disabled, an account
adopted) sit in their own revocation row beside the subject's, so an outright revocation
written at any moment is never replaced by one.

**Agents of one trust** (`server/auth/agents.py`). A workload trust with an agent claim
issues each agent its own subject, `sa:<trust>:<agent>`, and namespace, `<trust>-<agent>`:
the claim's value is used as given (lower-case letters, digits, `-`) or refused, never
rewritten into another agent's name. The namespace is made on the agent's first exchange
under the account's claim, tagged `machine:<trust>`, and only a namespace carrying that tag
is ever reached: an agent claim cannot name a person's namespace, another trust's or a
released one, and the org is the trust's. A trust takes no workload token living longer than
its `max_token_s` (default an hour), makes at most `max_agents` agents, and must name one
claim with an exact value beyond `iss`, `aud`, `exp`, `iat`, `nbf` and `jti` (alone, or a
prefix, they take every token the issuer signs for Loom). Two trusts of one issuer that one
token could meet, either telling agents apart, are refused at registration. An exchange that
names no client takes the one trust the token meets; while a trust that could also meet it
cannot fetch its keys, the answer is to retry, never the other trust. An
agent's namespace carries no email, so no sign-in by email ever resolves to an agent. An
agent's token names the trust as its parent subject, so the trust's disable cutoff ends
every agent's. The trust's issuer is trusted for every agent name it signs. An agent's
namespace is provisioned as an account is: its row names its one owner, the agent, and that
owner's grant follows; a later exchange writes the grant only if it was never written, under
the grant's claim, so one revoked since stays revoked. A person reaches an agent's memory
only as any namespace's: granted by its admin, or by joining.

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
row is never embedded (`is_embedded` in `server/memory/domain/memory_types.py`), so no search
leg returns it. The other reads that could serve its value follow three rules:

- Left out: the `anchor_first` and `recent_first` tiers, `bodies: "unit"`,
  `memory.related`'s excerpts, `memory.aggregate` (no group key or filter match on a `high`
  value), `profile.render` and the `memory.stack`, context pack and MCP primer built from
  it, `memory.ask`'s bridge hop and stale-fact masks, `conflicts.propose_revision`,
  `namespace.validate`'s checks that quote a value, a search's duplicate substitution (a
  `high` canonical is not seated in its duplicate's place), the profile fold, the entity
  graph (a row written as `high` projects only the entities its writer declared, and the
  entity extractor never reads it), the entities of a `high` row in `memory.list`,
  `memory.related` and the graph walk's recency seeds, the candidates `procedure.match`
  reads and the bodies `procedure.list` summarises, and the existing memories an extraction
  prompt shows the LLM. A row the sensitivity sweep labels `high` after the extractor ran
  (one written unlabelled, or arriving that way through `memory.copy_to` or an import) keeps
  the entities mined from its value on the row, not served by those reads. A row re-set as
  `high` through `memory.set` carries only its declared entities. Either way the graph edges
  an earlier version projected remain, and `facts.traverse` and `facts.path` still walk
  them.
- Listed without the value: `memory.list` (no value or hash, `value_truncated: true`, and
  `contains` does not search it), `conflicts.list`, `memory.proposals`, and the preview of a
  `memory.move` dry run.
- Read by key and audited: `memory.get`, `memory.get_by_id`, `memory.bulk_get`,
  `memory.history` (one row per version it serves of a row with any `high` version, since
  the version before a sweep labelled the row holds the same value unlabelled) and
  `memory.evidence` each write a `memory.sensitive_read` row. The admin dumps
  `namespace.snapshot` and `erasure.export` carry `high` rows on purpose and write one per
  row.

Raw evidence (conversation chunks, session digests, the passages `bodies: "source"` serves)
has no sensitivity column. `memory.export_evidence` leaves out the chunks and digests linked
to a key with any `high` version, and those whose text the detector reads as a credential,
unless asked with `include_high` (each one it then releases is audited). `bodies: "source"`
leaves out a passage whose text reads as a credential. Other raw text is served to readers of
the namespace as it is: a caller-labelled `high` value that shares a passage with a plain
row, `memory.search`'s `chunk` and `summary` hits (which `memory.ask` also hands its model),
`memory.get` on a `chunk:` key, the session digests `memory.digests` lists, and
`memory.recall`'s raw turns.

Search demotes labelled rows on questions that did not ask for a contact detail
(`LOOM_MEMORY_SEARCH_SENSITIVITY_DEMOTE`).

The detector runs on `memory.set` and extraction when `LOOM_MEMORY_SENSITIVITY_AUTODETECT` is
on. `memory.export_evidence` and `bodies: "source"` also run it on raw text, whatever the
setting: raw text has no level to fall back on. The `sensitivity_sweep` worker (on,
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
- A spent refresh token, code or device code that comes back ends its whole session.
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
