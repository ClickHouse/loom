# Security

Loom separates tenants by columns: every row carries the identity tuple, and every read
filters on it. The transports authenticate and set the request's `AuthInfo`; the shared
gate and the handlers authorize; nothing downstream reads a raw credential. To report a
vulnerability, and for the deployment hardening checklist, see
[SECURITY.md](../../SECURITY.md).

## Threat model

| Threat                                               | Boundary                                                                                                                                                                                                                                                                            | Enforced by                                                                                                                       |
| ---------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------- |
| An unauthenticated caller reads or writes data       | Bearer token on every op; 401 before the body is read                                                                                                                                                                                                                               | `server/transport/http.py` (`_extract_auth`), `server/auth/jwt.py`                                                                |
| A forged or tampered token                           | Signature, algorithm allow-list, `kid`, issuer, audience, time claims                                                                                                                                                                                                               | `server/auth/jwt.py` (`verify_token`)                                                                                             |
| A revoked token keeps working                        | Per-process revocation snapshot, fail closed until loaded                                                                                                                                                                                                                           | `server/auth/revocation.py`                                                                                                       |
| Tenant A reads tenant B's rows                       | Org + namespace check on every call; row-scoped ops check the row's namespace                                                                                                                                                                                                       | `server/auth/authz.py` (`check_authorized`, `row_in_scope`)                                                                       |
| A refusal confirms that an id or namespace exists    | `found=False` or a generic "cannot access", never a 403 naming the target                                                                                                                                                                                                           | Row-scoped handlers; the restricted gate                                                                                          |
| One caller exhausts shared capacity                  | Token buckets per namespace, org and actor                                                                                                                                                                                                                                          | `server/core/ratelimit.py`                                                                                                        |
| An agent is induced to echo or enumerate memory      | `read_scope` ceilings, exposure ledger, breadth budgets, prompt fencing                                                                                                                                                                                                             | `server/identity/domain/read_scope.py`, `server/identity/app/policy_gate.py`, `server/memory/domain/prompt_fence.py`              |
| Stored text carries instructions into a later prompt | Recalled memory is fenced with a per-prompt nonce                                                                                                                                                                                                                                   | `server/memory/domain/prompt_fence.py`                                                                                            |
| Secrets written as memory are embedded and served    | Sensitivity detection; `high` rows, and chunks, digests and turns that read as high, are never embedded                                                                                                                                                                             | `server/memory/domain/sensitivity.py`, `sensitivity_sweep` worker                                                                 |
| Audit rows are edited or deleted                     | Per-row hashes plus a hash-chained checkpoint per window                                                                                                                                                                                                                            | `server/observability/audit.py`, `audit_verifier` worker                                                                          |
| Erasure leaves residue                               | Checkpointed saga ending in a residue census                                                                                                                                                                                                                                        | `server/erasure/app/saga.py`, `server/erasure/store/census.py`                                                                    |
| A service signs beyond its end users                 | Each issuer signs for its listed orgs alone, no two for one org; a token names one user and agent, never a group or a role                                                                                                                                                          | `server/auth/jwt.py` (`_verify_end_user`), `server/settings/catalog.py` (`EndUserIssuer`)                                         |
| A collection is read without a grant                 | Only grants of the request's principals, inherited down the tree, open one                                                                                                                                                                                                          | `server/auth/sharing.py`, `server/auth/authz.py` (`check_authorized`)                                                             |
| A hostile document takes over a parser               | Parsing runs in the extractor service, never the server: each extraction in a child with no token, no environment, no socket (seccomp) while extractions run one at a time (the default), no new process and bounded memory and CPU; the server checks what it returns as untrusted | `extractor/loom_extractor/child.py`, `server/knowledge/domain/extract/validate.py`, [document extraction](document-extraction.md) |

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
`GET /v1/.well-known/jwks.json`. With `LOOM_INFERENCE_TOKEN_AUDIENCE` the newest RS256 key
also signs the identity token Loom exchanges at an inference gateway; that gateway trusts
those keys, so Loom refuses the settings when the token's audience is `LOOM_JWT_AUDIENCE` or
its issuer one Loom's verifier accepts, and a retire may not leave no RS256 key active. The `default` row holds `HMAC(root, label)`, written by
the workers leader or a keyring command (never a replica's first boot, which would trust a
mistyped secret); a process whose root does not match it reports
`keyring_root_mismatch`, and the keyring commands and `loom mint-token` refuse there.

**Model calls behind the gateway.** While the gateway is required
(`LOOM_INFERENCE_GATEWAY_REQUIRED`, a token exchange, or the workload token sent as the
bearer), model calls and the memory
content posted to classifiers and rerankers go only to the hosts
`LOOM_INFERENCE_GATEWAY_HOSTS` lists, matched exactly (`Settings.off_gateway`); an
allow-list, so a cloud's own endpoint, an IP address or a look-alike name is refused as a
provider's API is. The settings refuse to load with no hosts, or with an API root, the
token endpoint or a rerank endpoint elsewhere, or the Gemini embedder (`gateway_refusal`),
and with a `builtin:cohere` code-search or knowledge-search default
(`_check_rerank_defaults_behind_the_gateway`).
Model calls go through their own client, `server/clients/_http.py::get_model_client`,
whose request hook (`_on_request`) refuses every other host on every hop, redirects
included; GitHub and an issuer's discovery document use the other client, which has no
such hook. The rerank and classify senders, on their own clients, check each URL. Every
check reads the host as httpx sends it (`url_host`: httpx's parser, an IDN as punycode),
so a URL with several `@` or a backslash cannot pass the check for one host and be sent to
another. `namespace.register` refuses a `classify_endpoint` that is not https at the
default port of a listed host (422), and one stored before is never called: a namespace
admin can neither send memory content off the gateway nor reach another port or service
with it. A dashboard configuration goes through the same check: refused at save (422), and
a stored one that fails is not applied, its serving vector settings included
(`provider_runtime.load_active_configuration`), and `/v1/health` reports
`model_configuration_refused`. The check reads only the embedding, so a refused one always
differs from the one in force and the vector tables may hold that other space; it fails
closed: every data-plane request on that server answers 503 `GATEWAY_REQUIRED` with no
`Retry-After` (the gate's `refusal`, which the maintenance middleware reads) and readiness
fails, until a configuration the check accepts, or a reset to the environment, replaces
it; the transition that replaces it takes the hold over. A rolling restart stops at such a
server until then. A refusal (`GATEWAY_REQUIRED`) is a setting to fix, and neither SDK
retries it.

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

**Revocation** (`server/auth/revocation.py`). `loom.revoked_tokens` holds revoked `jti` or
`sub` values, optionally with an issued-before cutoff. An end user's tokens are revoked by
subject under their org, `<org>/user:<id>`
(`server.core.principals.end_user_revocation_subject`), since `user:<id>` names one person
per org. Each process loads a snapshot at boot and refreshes it on its own loop every
`LOOM_JWT_REVOCATION_CACHE_TTL_SECONDS` (30 s).

Operator cutoffs only advance. Every cutoff writer holds the same subject lease while
reading and writing the cutoff. Revoking an account records every directory spelling
before token-list housekeeping, so a busy token name cannot leave its login subject active.
Token lists mark only permanent revocations; temporary suspensions leave their rows intact.
Historical cutoffs require every server to run this build: older cutoff writers do not
share the subject lease. During a rolling upgrade, use outright revocation or a cutoff
at the current time; wait until the rollout completes before submitting a past cutoff.

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
  accounts that tie a person's spellings drops the org's.
- Every other process, on this server or another, drops the org's entries within
  `acl_watermark_poll_seconds` (1 s). The write adds one change for its org to
  `loom.acl_versions` (a `SummingMergeTree`, counted per org and kind of change), and each
  process reads `sum(changes)` per org and kind with sequential consistency; an entry read
  before its count moved is not served. A sum only grows, so neither a merge nor a writer's
  clock can hide a change. A process whose last good poll is older than
  `acl_watermark_max_stale_seconds` (10 s) serves no cached grant and reads per request. The
  TTL bounds a change whose watermark row was never written, and a change made by a build
  that predates the table.
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
  promotes. A bot registered `--platform-admin` gets it in every machine token, as a flat
  token over every namespace (`_machine_tokens` in `server/auth/oauth.py`); such a bot
  names no namespace, tells no agents apart and, as a trust, matches every claim exactly.
  Only a person signed in as a platform admin, or an operator on the host, registers one:
  `/v1/admin/bots.create` refuses the flag to every service account's token and to a
  third-party client's session, so a token holding the role cannot mint itself a lasting
  successor.
  `loom mint-token --platform-admin` mints it only for a non-agent `sa:` subject, for at
  most 90 days, and audits each mint.
  Both token issuance paths require a fresh fleet view with every replica protecting
  human credentials, including processes outside the deployment on the same ClickHouse;
  an older replica could accept the token on an unprotected SSO route. Both checks read
  ClickHouse directly and leave the kind-filtered replica cache unchanged.
  Before a rollback starts an older replica, disable admin bots and revoke standalone
  CLI-minted admin subjects or tokens.

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

With `LOOM_SCIM_TOKEN` set, the provider deactivates people over SCIM 2.0 (`/scim/v2`,
`server/auth/scim.py`); that bearer token, compared in constant time, is the only credential
those routes take. Loom refuses to start with one shorter than 32 characters, and with
SCIM set while sign-in is not SSO or auth is off, where a deactivation would refuse nobody. A
deactivation revokes outright every subject an SSO sign-in with the email could reach, in
any org, which ends their sessions and every token delegated from them, and
`loom.scim_users` refuses SSO sign-in with the email until a reactivation, which narrows the
revocations to tokens issued before it. SCIM's revocation of a subject is stored under its
own key (`revocation.SCIM_PREFIX`), beside the subject's own row, and every reader takes it
as it takes that row: the subject is revoked while either says so. SCIM writes only its own
key and an operator's `revoke-token` only the subject's, so neither undoes the other: an
operator's time-box or lift does not let a deactivated person in, and a reactivation does not
lift an operator's revocation. One shared key would make SCIM read, merge and repair the
operator's row around each write, and an operator's write landing in between could still
replace SCIM's or be replaced by it; the operator cutoffs (`CUTOFF_PREFIX`) are kept apart
for the same reason. A SCIM user's id is a UUIDv5 of its email, so it is a pseudonym, not a
secret: anyone who knows an address can compute its id. Keying it with the root secret would
change every id when that secret rotates, and the provider would find none of its users.

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
itself at `/oauth/token` for a service-account token of an hour, or of the bot's
`max_access_token_s` (60 s to a day, set at registration) when shorter, unless its request
asks for another `expires_in` up to that maximum: with a key, by a client assertion it
signs (RFC 7523 `private_key_jwt`: issuer and subject its name, audience this token
endpoint, at most five minutes long, its `jti` spent once in Keeper, ES256, RS256 or EdDSA
only), or as a CI job, by token exchange (RFC 8693) of the OIDC token its platform issued
for the run, checked against the registered issuer's keys (fetched only for a registered
issuer), the audience and every registered claim. A CI token may be exchanged again while
it lives (every client of the job, each renewal), and the token issued never outlives it.
A CI trust must name at least one claim: any job on a platform can ask for any audience. A
token is issued as of the moment before the bot is read, so a disable landing during the
exchange still cuts it off.
An exchange whose subject token verifies is counted, before an agent's namespace is made,
against its trust (`LOOM_WORKLOAD_EXCHANGES_PER_TRUST`, 50,000) and, for a trust with an
agent claim, its agent (`LOOM_WORKLOAD_EXCHANGES_PER_AGENT`, 300), per 10 minutes, as rows
of `loom.auth_attempts`, so the limits hold across replicas. A trust without agents is one
workload (every job of a repository shares one `sub`) and counts only against the trust.
Past a limit the exchange is refused with 429 `temporarily_unavailable` and
`Retry-After: 60`; a process with no free check slot answers 503, retryable. A token that
fails verification counts nowhere, so a caller without the issuer's key cannot spend a
trust's budget, and an agent's loop cannot spend the trust's beyond its own limit: an
exchange refused at the first count is not recorded, and one recorded then refused at its
recount (renewals of one agent racing, on any replica) is released under every gate it was
counted under with an `.ok` row, as a sign-in is.
Disabling a bot refuses later proofs and revokes its tokens; no refresh token exists to
outlive it. No revocation waits for a token's expiry: a subject's revocation, a cutoff and
a parent's revocation refuse a token by its subject and issue time, and are kept for good
(or until an operator's own `--expires-at`), so a day-long token ends with them; only
`/oauth/revoke`'s row for one token lapses, at that token's own expiry. The cutoffs an
operator action writes (a bot replaced or disabled, an account adopted) sit in their own
revocation row beside the subject's, so an outright revocation written at any moment is
never replaced by one.

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

An agent's token also names every active namespace of the trust's org that its subject holds a
live grant on (`agents.with_grants`), at the grant's role but never above writer, so an agent
cannot pass on admin over a namespace it was lent. They are added by name until the next would
take all the token's namespaces, its own and the trust's included, past
`agents.TOKEN_NAMESPACE_BYTES` (4,096 bytes, each name counted as it is written in `namespaces`
and `ns_roles`), so the token stays within a proxy's 8 KB header limit with a 4096-bit signing
key, at the longest and the shortest names alike. An agent trust whose shared namespaces would
not leave room for an agent's own is refused at registration. A grant reaches an agent's next
token; a lower or revoked one caps its claim at once (`authz.claim_cap`), on other servers
within `grant_cache_ttl_seconds`. Agents lend to each other through `grants.share_agent` and
`grants.unshare_agent` (`server/identity/app/agent_shares.py`): the caller must be an agent,
the namespace its own and the grantee an agent of its trust, at viewer or writer, and a
namespace the grantee's token does not name yet within that budget (one it names already, the
trust's or lent before, only has its role raised; two shares at once can both pass it, and the
token then names those that fit, by name). A share gives the grantee only what the lender
already reads, so a compromised agent gains nothing by it; which agent may read which is the
control plane's decision, made by calling as the lending agent. A share never lowers a grant
nor writes over one someone else made, and an unshare revokes only a grant the calling agent
made, each checked on the row read under the grant's claim, so an agent never takes over or
undoes an admin's grant. A share is refused while its namespace is restricted, since an agent's
token is writer there and a restricted read needs a grant an admin chose to give; a share made
before the namespace was restricted stays until the agent or an admin revokes it. A keyed bot's
token names only its registered roles.

**An org's model calls switched off** (`server/llm/customer.py`). An org admin's
`org.set_model_calls` (`loom.org_model_calls`) stops every model request for the org's data:
each send point refuses a call for a switched-off org (403 `MODEL_CALLS_DISABLED`) before
anything is sent, and background jobs leave the org's work queued. The server that took the
change applies it on its next call, every other within
`LOOM_MODEL_CALLS_POLICY_REFRESH_SECONDS`; an older build ignores the switch. It fails
closed: a process whose copy of the switch is older than
`LOOM_MODEL_CALLS_POLICY_MAX_STALE_SECONDS` refuses every model call for an org until it
reads it again. An org admin reaches only its own org's switch. A platform admin's lock
(`"locked": true`, settable only by a platform admin) stops the org's admins from switching
it back on or unlocking it, never from switching it off. Offboarding is manual: a
platform admin switches the org off locked, and the org's cached query vectors expire after
30 days. Changes to one org's switch are serialized, so an org admin's write never carries
an old lock state over a new one.

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

## Response headers

Every response carries these except an unhandled 500, which Starlette's error middleware
answers outside every middleware the app adds. A route that sets one of them itself keeps
its value.

| Header                      | Value                                                                      |
| --------------------------- | -------------------------------------------------------------------------- |
| `X-Content-Type-Options`    | `nosniff`                                                                  |
| `X-Frame-Options`           | `DENY`                                                                     |
| `Content-Security-Policy`   | `frame-ancestors 'none'`                                                   |
| `Referrer-Policy`           | `same-origin`                                                              |
| `Strict-Transport-Security` | `max-age=<LOOM_HSTS_MAX_AGE_SECONDS>`; one year by default, `0` sends none |

- `nosniff`: no body is sniffed into a type it was not served as.
- Frame denial: no other page can frame the dashboard or an API answer. Every CSP Loom
  sends has `frame-ancestors 'none'`, the routes' own (the dashboard page, downloads, the
  sign-in pages) included; `X-Frame-Options: DENY` is the same rule for browsers without
  CSP. Nothing in Loom frames the dashboard, and the dashboard frames nothing: its own CSP
  sets `frame-src 'none'`.
- `same-origin`: a dashboard URL, which can name a record, is never sent to another origin,
  and a same-origin non-GET request still carries its real `Origin`, which the cross-site
  check reads. Under `no-referrer` a browser sends `Origin: null` on a form POST, which the
  check refuses. The sign-in pages under `/oauth/` run no script and send the same policy:
  their URLs can carry a user code or an authorization request, so no other site receives
  them as a referrer, and their form POSTs carry the page's own `Origin`.
- HSTS, without `includeSubDomains` or `preload`: Loom serves plain HTTP behind a TLS
  terminator, and a browser ignores the header over plain HTTP and honours it over HTTPS.

**Route policies.** The dashboard page carries a Content-Security-Policy whose
`script-src` admits the bundle's origin and the one boot snippet by a per-response nonce,
never `unsafe-inline`, and whose `frame-src 'none'` allows no frame. An uploaded document
is attacker-chosen bytes under an attacker-chosen type, so `knowledge.download` previews
inline only plain text (text, Markdown, CSV and JSON, all served as `text/plain`) and PNG,
JPEG, GIF and WebP whose bytes match their type; anything else (HTML, SVG, XML, PDF,
unknown, mismatched) is an `application/octet-stream` attachment, and every download
carries `Content-Security-Policy: sandbox; default-src 'none'; frame-ancestors 'none'`.
The dashboard applies the same allowlist to the bytes it fetches: it decodes the first 1
MB of text as UTF-8 into a text node in a `<pre>`, shows images with `<img>` from a
re-typed `blob:` URL (which ignores the server's headers), and downloads everything else,
PDF and SVG included.

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

### Extraction policy

An org admin sets rules for what extraction never stores, once for the org and
optionally added to for a namespace (`extraction_policy.set`, audited, `org_admin` of
that org only; an end-user token holds `viewer` and cannot set, clear or read one). The
rows live in `loom.extraction_policies`; every server keeps a copy, read at most every
`LOOM_MODEL_CALLS_POLICY_REFRESH_SECONDS` and trusted for no longer than
`LOOM_MODEL_CALLS_POLICY_MAX_STALE_SECONDS`. Past that it fails closed: inline
extraction answers 503, and queued requests and worker passes wait. A replica of a build
that predates the policy table extracts without it; set a policy once every replica runs
a build that reads it ([rolling upgrades](rolling-upgrades.md)).

The handler reads the policy for the request's `(org, namespace)` itself; no request
field carries it. Each model call of the memory, knowledge and observe planes that
writes stored text from raw text (fact extraction with its retries and coverage sweep,
the typed-record value stage, the session digest, observation distillation,
conversation summaries and titles, session briefs and `bulk_set` context lines) ends
its system prompt with it, after the request's `extraction_instructions` and any retry
addendum, in a delimited block that states it outranks them and the messages. The
deterministic passes that would add rows the model left out, or copy from the raw turns
what the model did not write, do not run under a policy, and the response says so
(`under_operator_policy`). The rule is given to the model, not checked against what it
returns; verbatim evidence, chunks and observations are stored as sent. Model passes
over rows already stored rather than over raw text do not add it, such as period
digests, profile consolidation, contrast reflections, `memory.consolidate` merges, the
structured profile worker (`server/memory/workers/structured_profile.py`) and the
entity extractor (`server/memory/workers/entity_extractor.py`); those rows include
`memory.set` values, which no policy filtered. A namespace policy covers that namespace
only, not its child collections. The
[extraction guide](../features/memory/extraction.md#operator-extraction-policy) lists
the paths.

## Sensitivity

`server/memory/domain/sensitivity.py` classifies a value with deterministic regexes. It is
a lower bound: it recognises the label forms and shapes listed here, not every way a person
can phrase a secret. The catch-all for extraction is an operator's extraction policy.

| Level    | Matches                                                                    |
| -------- | -------------------------------------------------------------------------- |
| `high`   | A credential, payment card number, US Social Security number or stated PIN |
| `medium` | An email address, a phone number or an IBAN                                |
| `low`    | Set only by a caller                                                       |

The `high` shapes:

- Credential: a prefixed API key, an access key id, a private-key block, a JWT, or a
  `password=` / `api_key:` assignment of an 8+ character token.
- Card number: one ungrouped group of 15 (Amex) or 16 digits, or, in a run of digit
  groups joined by single spaces or dashes, consecutive groups in a printed card grouping
  (4-4-4-4, 4-4-4-4-3, 4-6-5, 4-6-4) with at most three short groups around it, at most
  two of them after it. A card pasted with its expiry and CVV or after a date still counts;
  two phone numbers side by side, a table of eight or more numbers, a 19-digit hash and a
  13-digit phone number without its `+` do not. It has a card network's leading digits and
  length (Visa 4 at 16 or 19; Mastercard 51-55 and 2221-2720 at 16; Amex 34 and 37 at 15;
  Diners 300-305, 3095, 36, 38 and 39 at 14; JCB 3528-3589 and Discover/UnionPay 6011, 62,
  644-649 and 65 at 16 or 19) and passes the Luhn check. A 15-digit IMEI passes Luhn by
  design; the only 15-digit network is Amex (34, 37), so an IMEI with any other leading
  digits is not a card. Groups joined by two spaces or by dots
  (`4111  1111  1111  1111`, `4111.1111.1111.1111`) are not read.
- Social Security number: `ddd-dd-dddd` outside the never-issued ranges (area 000, 666,
  9xx; group 00; serial 0000).
- Stated PIN: one of the labels `pin`, `pin code`, `pin number`, `passcode` /
  `pass code`, `cvv`, `cvc`, `cvv2`, `cvc2`, `cvv code`, `cvv number`, `cvc code`,
  `cvc number` and `security code` (`_` or a space inside a two-word label, after any
  prefix that ends in something other than a letter or digit: `BADGE_PIN`, `CARD_CVV`),
  followed by its value after `is`, `was`, `:` or `=` (`PIN is 4471`, `cvv: 123`), with an
  optional `for` and up to three words before it (`PIN for the front door is 4471`). Every
  label but a bare `pin`, which is also a verb and a connector pin (`pin 115`, `pin #115`),
  also takes the value directly (`CVV 123`), unless `of` follows it
  (`passcode 100 of the batch`). `PIN number`, passcode, CVV/CVC and security code take
  3-8 digits; `PIN` and `PIN code` take 3-5, since `PIN: 560001` and `PIN code 560001` are
  Indian postal codes, so a 6-digit PIN counts when stated as a `PIN number`. A connector
  pin written as an assignment or with `number` (`GPIO pin: 115`, `GPIO pin number 115`)
  reads as a PIN, the price of reading `PIN: 4471` and `PIN number 4471` as one. The value
  must end at its number: a version, a date or a run-on word (`pin is 2024.1`,
  `pin is 2024/01/15`, `pin is 1234abc`) is not a PIN.

An IBAN counts when its country code is in the ISO 13616 registry, its length is that
country's and it passes the mod-97 check. The card check skips an IBAN's digits, whose
printed groups can read as a card number that passes Luhn (`DE24 4005 0150 0000 0005 00`),
so an IBAN is `medium`. A question naming an IBAN or a bank account (number) counts as
contact intent, so ranking does not demote the row that answers it.

A JSON value is read scalar by scalar (`detector_text`). A key whose last one or two words
name a PIN-like secret (three for `social_security_number`) states its digits under that
label, so the free-text rules above decide the length. A label's words may also be written
as one word (`pincode`, `cvv2`, `PINcode`). `pin`, `pin_code` and `pincode` read as
`pin is <value>` (3-5 digits), `pin_number` as `pin number is <value>`, and `passcode`,
`cvv`, `cvc` and `security_code` (with, for example, `pass_code`, `cvv_2`, `cvc2`,
`cvv_code` and `cvc_number`) as `<label> is <value>` (3-8 digits). An `ssn`, `ssn_number`,
`ssn_no` or `social_security_number` key states an SSN with its dashes optional. A card
number is found under any key. So `{"pin": "4471"}`, `{"pin_code": "4471"}`,
`{"badge_pin": "4471"}` and `{"ssn": "123456789"}` are `high`, and `{"pin": "560001"}`,
`{"pincode": "560001"}` (a postal code), `{"pin_count": 4}` and `{"pin": "A1"}` are not. A
connector pin in a config (`{"pin": 115}`) reads as a PIN, as `GPIO pin: 115` does. A key
whose last one or two words are an assignment word (`password`, `db_password`,
`dbPassword`, `client_secret`; a bare `pwd` key in a JSON value is a working directory and
not one, while `PWD=...` in free text still reads as an assignment) states its string value
as `password: <value>` when the value is one token of 8+ characters holding a letter and a
digit and is not a reference (`env:NAME`, `vault:PATH`, `secret:NAME`, `ref:NAME`, an
all-caps `$NAME`, `${NAME}`, a value that opens with `/`, a segment, then `/`, such as
`/etc/key1`, or one opening `~/`, `./` or `../`; a base64 secret of that shape reads as a
path and is missed). A URL is never a reference, since a webhook or a presigned URL is the
secret itself, so a URL that only names where a secret lives reads `high`, as does an ARN
that names one (its `secret:` part reads as an assignment). A relative path without one of
those prefixes (`src/app1`) still reads as a secret. A quote, comma or semicolon in the
value does not cut it short. So `{"db": {"password": "hunter2xyz"}}` and
`{"password": "admin:Passw0rd"}` are `high`, and `{"password": "required"}`,
`{"password": "********"}`, `{"api_key": "YOUR_API_KEY"}` and
`{"api_key": "env:OPENAI_API_KEY"}` are not. Any other key labels nothing. `reads_high`
reads the same pairs in raw JSON text, such as a chunk of an object value, when the value
is one quoted token or digits and dashes. The sweep's prefilter is a superset of this key
rule, not a copy: it selects a key that ends in a label or a credential word, so it may
select a few non-secret rows (`spin`, `unpin`, `ATMPIN`, `{"password": "required"}`), which
the detector, comparing whole key words, leaves unlabelled; each costs a re-check on the
sweep's passes within its window. The sweep reads the bytes its writer stored in `value`,
where a dotted key such as `{"security.code": "5512"}` keeps its words, and also the object
in `value_json`, which an import may fill with more than `value` holds.

A caller-supplied level wins for the stored label. Detection is
`LOOM_MEMORY_SENSITIVITY_AUTODETECT`.

The readers that send a value to a model still check the value itself
(`value_reads_high`): consolidation, the profile fold, the structured profile, the existing
memories an extraction prompt shows, `observe_extract` and the entity extractor leave out a
value that reads as `high` even when its writer labelled it `low` or left it unset. This
fails closed by design.

A `high` row is never embedded (`is_embedded` in `server/memory/domain/memory_types.py`), so
no search leg returns it. The other reads that could serve its value follow three rules:

- Left out: the `anchor_first` and `recent_first` tiers, `memory.related`'s excerpts,
  `memory.aggregate` (no group key or filter match on a `high` value), `profile.render` and
  the `memory.stack`, context pack and MCP primer built from it, `memory.ask`'s bridge hop
  and stale-fact masks, `conflicts.propose_revision`, `namespace.validate`'s checks that
  quote a value, a search's duplicate substitution (a `high` canonical is not seated in its
  duplicate's place), the profile fold and the structured profile, the entity graph (a row
  written as `high` projects only the entities its writer declared, and the entity
  extractor never sends it to its model, nor an unlabelled value that reads as `high`; for a fact
  `memory.set_from_messages` extracts, the declared entities are the extraction model's,
  read from the same turn), the entities of a `high` row in `memory.list`, `memory.related`
  and the graph walk's recency seeds, the candidates `procedure.match` reads, the bodies
  `procedure.list` summarises and the bodies `bodies: "unit"` reads (each read leaves `high`
  rows out before it picks a row, so another identity's `high` row of a key never hides the
  plain one), the existing memories an extraction prompt shows the LLM (labelled `high` or
  reading as `high`), the observations `observe_extract` sends its model (labelled
  `high` or reading as `high`: capture writes them unlabelled), the entities a period
  digest copies from its sources and `namespace.taxonomy` counts, and a session digest's key
  facts that read as `high` (left out when the digest is written, listed by
  `memory.digests` and sent to the outcome judge, so a digest an earlier build wrote is
  covered too). A period row an earlier build wrote keeps its `high` sources' entities until
  it is written again: `loom period-digest --org <org> --namespace <ns> --rewrite --apply`
  does that (one model call per period, at most `--max-identities` identities and
  `--max-periods` periods each per run). A row the sensitivity sweep labels `high` after the
  extractor ran (one written unlabelled, or arriving that way through `memory.copy_to` or an
  import) keeps the entities mined from its value on the row, not served by those reads. A
  row re-set as `high` through `memory.set` carries only its declared entities. Either way
  the row keeps the graph edges of the entities on it and no other: those an earlier
  version's text gave the graph end. The edges it keeps are walked by `facts.traverse` and
  `facts.path`, as by `memory.backlinks` (which names a `high` key's shared entities and
  neighbours from them), `namespace.summary`'s top entities, `memory.outline`,
  `namespace.taxonomy`'s communities, the graph export and search's graph mode and
  expansion.
- Listed without the value: `memory.list` (no value or hash, `value_truncated: true`, and
  `contains` does not search it), `conflicts.list`, `memory.proposals`, and the preview of a
  `memory.move` dry run.
- Read by key and audited: `memory.get`, `memory.get_by_id`, `memory.bulk_get`,
  `memory.history` (one row per version it serves of a row with any `high` version, since
  the version before a sweep labelled the row holds the same value unlabelled; a redacted
  version, which served no value, is not) and `memory.evidence` each write a
  `memory.sensitive_read` row. The admin dumps `namespace.snapshot` and `erasure.export`
  carry `high` rows on purpose and write one per row. A conversation row has no label, so
  `erasure.export` labels `high` a turn or thread whose text reads as high and audits it
  the same way. `memory.copy_to` and `memory.cross_promote` read a `high` row by key and
  copy it to another namespace with its label; each writes its own audit row (both are
  `audit=True` ops), which records the copy.
  `memory.get` (with or without `as_of`), `memory.get_by_id` and `memory.bulk_get` never
  serve a `high` row past its retention window; `memory.history` and `memory.evidence` still
  do until the sweep. `memory.bulk_get`, like `memory.list`, serves any other row past
  `expires_at` until the expiry sweep.

Raw evidence has no caller to label it, so the text decides (`reads_high`, whatever
`LOOM_MEMORY_SENSITIVITY_AUTODETECT` says). A passage whose text reads as high (any
high shape: a credential, card number, SSN or stated PIN) is stored as written with no
vector, and its text is never sent to the embedder. Every read that ranks passages reads
only those with a vector (`embedding_dim > 0`), so no search leg returns it.

- Chunk writers: a verbatim chunk in `loom.memory_chunks` (a conversation chunk
  `memory.set_from_messages` writes, a chunk of a long `memory.set` value, a chunk
  `memory.import_evidence` writes) is held this way. Chunks overlap and can be cut
  through a key, and a chunk that holds only a key's head or tail does not read as one
  on its own. The conversation and long-value writers check the chunked text once, plus
  one chunk past its end for a key that crosses it, in a worker thread, and also hold
  every chunk whose character range overlaps a high shape, and every chunk whose own
  text reads as high (a chunk can start inside a word, where a pattern's word boundary
  falls). A private-key block reads as high from its header to its closing line
  (without one, through the base64 characters, line breaks and `Proc-Type:` /
  `DEK-Info:` lines after the header, so prose ends it), so every chunk its body covers
  is held, though a body line does not read as a key alone. A chunk of a long
  `memory.set` value is held by the value's text, as a conversation chunk is, whatever
  its row's label; a `low` row stays findable through its own embedding.
  `memory.import_evidence` applies the same rule to each session's text rebuilt from its
  imported chunks: the chunks in `chunk_index` order, each one's overlap with the chunk
  before it dropped (a repeat of at least 50 characters; chunks with no overlap are
  joined whole), and a gap in `chunk_index` starts a new text. Imported chunks have no
  minimum length, so a key can run through a whole chunk; every chunk a key in the
  rebuilt text touches is held, and a private-key block with no closing line holds every
  chunk its body lines cover. A session written by two calls can hold two texts at one
  index, which leaves no single text to rebuild, so each chunk of such a session is held
  when its own text reads as high, or when it and a chunk at the next index that
  it overlaps (a repeat of at least 50 characters) make a text where a key touches it;
  two chunks with no overlap are not joined. The check reads one call's chunks only (at
  most 256), so a key cut at the boundary between the last chunk of one call and the
  first chunk of the next reads as high in neither, and both chunks are
  embedded. `loom import-okf` sends each session's chunks whole in one call, so only a
  session of more than 256 chunks can still hit this gap. `memory.get` on a `chunk:` key
  leaves out a neighbour with no vector, as it leaves out one that reads as high, and
  audits a key read of either.
- Digests and turns: a session digest whose text reads as high is written without
  a vector. A `conversation.append` turn that reads as high is stored as written in
  `loom.conversations` and gets no row in `loom.conversation_embeddings`, which keeps it
  out of vector search. `conversation.save_message` answers
  `reason: "high_sensitivity"` for such a turn, so the saved search never indexes it.
- Compaction: `conversation.compact` leaves such a turn out of its window: it stays live
  and is never sent to the summarizer. A summary that reads as high anyway is
  refused (`reason: "summary-high-sensitivity"`) and tombstones nothing; the window
  stays the same while its turns are live, so a retry most likely gets the same refusal.
- Provider rebuild: a chunk, digest or turn embedding whose text reads as high is
  staged without a vector, which covers one an earlier build embedded. A memory chunk stored
  without a vector whose own text is plain is read with the run of its session's live chunks
  around it: outward while a chunk has no vector, plus the first chunk with one on each
  side, within the most chunks one write call stores. It stays without a vector when a key
  in that joined text touches it. A writer holds every chunk a key covers, so the held
  chunks are contiguous and the key's header is inside the run: a chunk its writer held as
  part of a cut key stays held whichever page its neighbours land on, however many chunks
  the key spans. Any other row stored without a vector (a failed embed, or a row an earlier
  rebuild staged while its org's model calls were off) is embedded. Verification reads the
  text of every row left empty whose source held a vector and accepts those that read as
  high.
- `loom reembed` re-embeds only rows that have a vector, and keeps a conversation turn
  that reads as high on its old model, which search does not read, until
  `--delete-old` removes it (a memory row's vector follows its sensitivity label).
  `loom reembed --in-database` runs the embedding inside ClickHouse and cannot check the
  text, so it re-embeds such a turn too.

Chunks, digests and turn embeddings that an earlier build embedded keep their vector, so
the reads that serve passages check their text too. That check reads each passage's own
text, so a chunk an earlier build embedded that holds only the head or tail of a key cut
at a chunk boundary does not read as high and stays searchable until it is
rewritten:

- Left out: `memory.search`'s `chunk` and `summary` hits (and so what `memory.ask` hands
  its model and the context pack built from search), the chunks `memory.ask` injects for
  an identifier or a session date, the raw turns `memory.ask`'s literal completion,
  deep-evidence pass and when-evidence pass add to its prompt (literal completion still
  counts such a turn as a statement), the neighbours `memory.get` stitches around a `chunk:`
  key, `conversation.search`'s hits (the vector ranking, checked on the text the vector
  was built from, and the substring fallback the search takes when the embedder is down),
  and `conversation.search_saved_messages`'s hits (its vector leg and its BM25 keyword leg
  over the whole text). The `memory.ask` lookups, `conversation.search` and the saved
  keyword leg ask for two spare rows, so up to two held-back passages do not leave a page
  short; the chunk leg already over-fetches. The summary leg asks for none, since a spare
  row costs a granule of vectors on every search: a held-back digest leaves it one hit
  short.
- Listed without the text: `conversation.list_saved_messages` shows an empty
  `content_excerpt` for a saved message that reads as high (one an earlier build
  saved), and `conversation.threads` shows an empty `last_content_excerpt` for a thread
  whose last turn reads as high, and an empty `title` for a title that reads as
  high. Both listings check the excerpt they show and 1,024 characters past it, so a key
  that crosses the cut is caught. `memory.digests` shows an empty `summary_text`, and
  `memory.outline` an empty `headline`, for a session digest whose text reads as high
  (one an earlier build wrote), and `memory.digests` leaves out key facts that read as
  high. The conversation titler never sends a turn that reads as high to its model and
  never stores a title that reads as high: it marks the thread `title_refused` in its
  metadata instead, as it does a thread whose first eight user and assistant turns all
  read as high, and later passes skip it. A thread with fewer such turns stays unmarked,
  so a plain turn that arrives later gets it titled. New `metadata` from
  `conversation.update` or `conversation.create` replaces the mark, and the titler then
  asks its model once more and marks the thread again.
- Read by key and audited: `memory.get` on the `chunk:` key of a chunk that reads as high
  serves it and writes a `memory.sensitive_read` row.
- `memory.export_evidence` leaves out the chunks and digests linked to a key with any `high`
  version, those whose text reads as high and chunks stored without a vector (which
  includes a chunk holding only part of a key), unless asked with `include_high` (each one
  it then releases is audited). `bodies: "source"` leaves out a passage whose text
  reads as high.

Other raw text is served to readers of the namespace, or sent to a model, as it is: a
caller-labelled `high` value that shares a chunk with a plain row (a chunk is held back by
its own text, not by the rows it holds), the digest lines of the runs manifest, what the
session-summary draft (the conversation turns), the period-digest draft and the outcome
judge (session digest text) send their model,
`memory.recall`'s raw turns, a title a caller set with `conversation.create` or
`conversation.update` as `conversation.get` serves it, and a thread's own turns as
`conversation.get` and `conversation.list` serve them. Those two read one thread by its id
under the caller's read scope, as the thread's participants read their transcript, so they
serve each turn as written. Raw text is stored as written, and erasure removes the chunk,
digest, message and embedding rows whole.

Search demotes labelled rows on questions that did not ask for a contact detail or a bank
account (`LOOM_MEMORY_SEARCH_SENSITIVITY_DEMOTE`).

The detector runs on `memory.set`, `memory.bulk_set`, `memory.update` and extraction when
`LOOM_MEMORY_SENSITIVITY_AUTODETECT` is on, and reads the decoded value (`detector_text`): each
key and scalar of the stored JSON on its own line, so a token after a newline, or a quoted
password, keeps the word boundary it matches on, and a secret held in a key is seen. Lines
are joined by a separator no pattern crosses, so two scalars never read as one: a config
object (`{"password": "required"}`, a form label `Password:` before the next key, a list of
numbers) reads as the stored JSON always did. Text that has no level of its own, or carries
one a later sweep may not have set yet, is checked whatever the setting (`reads_high`, which
also reads text holding JSON, such as a chunk of an object value, with its string escapes
undone): `memory.export_evidence` (chunks, and digests with their key facts decoded),
`bodies: "source"`, the observations `observe_extract` sends its model, the entity
extractor's input, the existing memories an extraction prompt shows, the rows
`memory.consolidate`, the profile fold and the structured profile send their models, and a
session digest's key facts when written, listed by `memory.digests` or read by the outcome judge, and
the chunks, digests and turns above when written and when served. The
`sensitivity_sweep` worker (on, daily) labels the rows from paths that skip the detector:
a new version per row and, for `high`, tombstoned embedding rows and entity vectors. Until
the sweep labels it, an unlabelled row that holds a high shape (a credential, card
number, SSN or stated PIN) is embedded like any other, so search serves it, and
`memory.ask` can send it to its model; the conflict resolver reads every row and peer for a high shape before its prompt and sends none that holds one.
The worker reads only the trailing window and the shapes in
`LOOM_SENSITIVITY_SWEEP_SHAPES` (`credential` and `email` by default); the historical
backlog and the shapes the worker leaves out (by default IBAN-only rows, shape `other`,
and phone-shaped rows) are cleared by the operator's `loom sensitivity-sweep --apply`,
which pages through its whole scope until `--limit` candidates are found, in pages of at
least `LOOM_SENSITIVITY_SWEEP_MAX_ROWS_PER_PASS` keys. The sweep reads unlabelled rows
only: a row already labelled `medium` that also holds a `high` shape stays `medium` until
it is set again with `sensitivity: "high"`.

A worker pass is bounded:

- It reads its window in the table's sort key order, in pages of
  `LOOM_SENSITIVITY_SWEEP_MAX_ROWS_PER_PASS` keys, until that many candidates are found,
  `LOOM_SENSITIVITY_SWEEP_PAGES_PER_PASS` pages are read, or half the pass deadline (450 s
  at the default daily interval) has passed. A pass cut by that time applies what it found,
  stores its cursor and reports `truncated`, so the next pass starts where it stopped even
  when the pages cost more than the deadline allows.
- It starts at the agent the pass before stopped in. When it reaches the window's last row
  with pages left, it goes on from the first key up to where it started, so the rows that
  sort before its cursor are read in the same pass rather than left to age out of the
  window. A full page of current unlabelled rows inside one agent that gives nothing to
  label moves the pass past that agent, so one pass crosses up to its page cap of agents
  whose selected rows the detector leaves unlabelled. A page with a key whose only
  matching version is an older one (a row the sweep labelled, or deleted, before the parts
  merge) gets no current row for that key and does not count. Once the pass has read
  every other key with pages left, it reads the rest of each agent it moved past. The
  cursor names no key inside an agent, so each pass reads the same pages of that agent:
  a row behind more than `LOOM_SENSITIVITY_SWEEP_PAGES_PER_PASS` x
  `LOOM_SENSITIVITY_SWEEP_MAX_ROWS_PER_PASS` selected-but-unlabelled keys of its own agent
  is not reached by the worker until those keys leave the window. `loom sensitivity-sweep
--apply` reaches it.
- A pass that labelled rows leaves its cursor at the agent it stopped in, so the next pass
  reads that agent again. A pass that labelled nothing leaves its cursor past that agent
  when its candidates, all in that agent, failed to apply, or when it found none and its
  last page had a key with no current row in that agent, the case of older versions
  waiting for a merge. So a row that fails every time, or a run of such versions, is read
  again when the sweep comes back round to it. Such a key in an earlier agent on that page
  sorts before the cursor, so it does not move the cursor past the agent it stopped in. A
  pass whose last page was an ordinary one, even one that crossed into the next agent,
  leaves its cursor at the agent it stopped in, and failures in an earlier agent leave the
  cursor where the pass stopped. A cursor that names an agent too long for its 512
  characters, at or past it, is past its namespace (or org) instead, so that agent's
  remaining rows wait until the sweep comes back round. The cursor, in
  `loom.worker_status`, holds the org, namespace and agent only, so it names no user or
  memory key that would outlive an erasure.
- Its ClickHouse filter selects only the shapes in `LOOM_SENSITIVITY_SWEEP_SHAPES`
  (`credential` and `email` by default; every high shape, a card number, SSN or stated PIN
  included, counts as `credential`), and within them only what the detector can label: a
  card is a lone 15- or 16-digit group or a printed grouping with a network's leading
  digit, a credential word is an assignment (`password=` and eight characters of a value),
  and `PIN` or `PIN code` takes a 3-5 digit value. So epoch timestamps, 19-digit ids,
  prose that names a credential word (`I forgot my password`) and postal codes
  (`PIN: 560001`) are not selected.

A redaction (`memory.redact`) scrubs the version it names, that key's derived rows, and
every version of the user's profiles whose trace names the memory: the structured profile
and the free-text fold's `consolidated_profile` name the memories they read in their trace
(`profile.fold:<label>:<memory ids>`), and each version whose trace names the redacted
memory is scrubbed with its key's derived rows, the live one included. The trace names
the memory and not its version, so a profile built from another version of it is scrubbed
too. The named version and every profile version reached are scrubbed in one statement
per version table, so the redaction issues two mutations however many profile versions
name the memory. The cached
answers and ask precedents whose window held a scrubbed profile key are deleted with those
of the memory's own key. A window records bare keys, and every user's profile has the same
key, so this reach is org-wide: one redaction clears every cached answer and precedent in
the org that rendered any user's profile, in every namespace, and each profile key reached
adds one deletion per table. A profile version whose trace names no memory ids
(`profile.fold:<label>` alone, as a build that does not record its sources writes it,
including an older build during a rolling upgrade) is not reached and keeps the text until
the user is erased. A lock on a profile key reached refuses the redaction
before anything is scrubbed, as a lock on the memory's own key does. A scrubbed live
profile is left out of `profile.render`, `profile.structured` and profile search, and so
of every pack and primer, until the worker rebuilds it from the user's current facts; the
fold rebuilds `consolidated_profile` only on its own schedule, and never for a user below
its minimum memory count or in a namespace that has turned the fold off.

A structured profile is served only from a row written as `worker:structured-profile`, an
actor kind no token maps to, so a caller cannot forge one
([memory invariants](memory-architecture.md#invariants)).

A document a registered document source's sync wrote carries the actor
`worker:knowledge-source-sync` and the class `imported`. No token maps to that actor
kind, so no request can write a document that reads as synced from a source: a
source's documents are those tagged `source:<name>` with that actor, and only the
sync worker writes, rewrites or retires them. The worker opens a source's credential
only for its own turn; a credential that does not open stops the turn with
`credential_invalid` and nothing is fetched without it. It retires a document only when
a complete full listing no longer has the file and a lookup of that file reports it
gone, trashed or out of every folder of the source, when the document sits at an
address that is no longer its file's, or when a namespace admin asked
(`knowledge.source.remove` with `delete_documents`); never on a fetch error, and never
touches a live document another writer or source holds
([document sources](../features/knowledge.md#syncing-a-source)). Any holder of a writer
grant on the namespace may still `knowledge.delete` a synced document, as any other;
while its file is in the folder, the next full listing writes it again.

## Audit log

`emit` in `server/observability/audit.py` writes `loom.audit_log` rows. `row_hash` is
SHA-256 over the canonical JSON of timestamp, event type, org, namespace, actor, subject
and request id. Every memory write leaves a hash-only row; ops that declare `audit=True`
leave one per successful call. An op whose input carries a credential (a tracker source's
password, a repository's access token) declares an `audit_subject` that leaves it out, since
the audit log outlives the source, the repository and every erasure.

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

The [analytics export](../operations/analytics-export.md) copies rows to a store erasure
does not reach (an S3 bucket a warehouse loads), as `loom export-okf --out` does with a
namespace's memory ([copying a namespace](../operations/okf-sync.md)). It is therefore an
allowlist of aggregate and registry columns with no memory content and no stored person
identifier: users and sessions leave as distinct counts only, and
`tests/integration/test_analytics_export_live_ch.py` pins each entity's exact column list.
Org, namespace and agent names always leave as a keyed reference SipHash-2-4-128, because a
personal namespace carries its owner's email and self-signup a username. The hash is a
pseudonym the key's holder can recompute, one string has one hash in every org and column,
and erasure in Loom does not reach rows already exported. The key (32 characters or more) is
in a Secret only the export's pod reads; ClickHouse's query and debug logs record it as a
bound parameter, on Loom's server and on any warehouse server that joins with it. The
bucket's write role is bound to the export's own Kubernetes ServiceAccount, not to the web
pods, and the upload never replaces an object.

## Outbound webhooks

Task inbox webhooks and memory webhooks share one sender (`server/core/webhooks.py`).

- **No value leaves.** A memory webhook event carries the event type, ids, the memory key,
  the version, the identity tuple, the actor, the request id and a timestamp, never a
  value, tags or evidence (`server/memory/domain/webhook_events.py`); a task notice
  carries ids and kinds, never task text. The receiver reads the value with its own
  token, so authorization of the content stays at read time. The key is sent to every
  registered receiver, including one whose token may not read the row, so writers keep
  secrets and facts out of keys.
- **Signed.** HMAC-SHA256 over `"{id}.{timestamp}.{body}"` in the Standard Webhooks
  headers; the timestamp lets a receiver refuse a replay. The secret is an environment
  variable a registration names, never stored; only a name under the registering org's
  own prefix (`webhook_secret_ref_ok`) is read, so no tenant can have Loom sign with
  another's secret. The secret is the org's by design, not one webhook's: two webhooks
  of the org that name it sign alike, so a receiver checks `org` and `namespace` in the
  signed body.
- **No request forgery inward.** Each delivery resolves the host, refuses loopback,
  private, link-local and multicast addresses (`server/core/hosts.py`), connects to the
  address it vetted and follows no redirect. Memory webhooks also refuse plain `http`
  and refuse an inward URL at registration; `LOOM_MEMORY_WEBHOOK_ALLOW_HTTP` and
  `LOOM_MEMORY_WEBHOOK_PRIVATE_HOSTS` are the operator's exceptions. A refusal does not
  name the address, a URL with a user name or password is refused, and logs name the
  host only.
- **No slow receiver holds a worker.** One deadline per POST covers the name lookup, the
  connection and the status line (`LOOM_MEMORY_WEBHOOK_ACK_TIMEOUT_SECONDS` for memory
  webhooks, a fixed 10 s for task inbox notices), and the answer's body is never read.
- **No network on the request path.** A memory write leaves outbox rows; a worker sends
  them. The outbox holds keys and user ids, so erasure clears it.

## Secrets

- **Dev signing secret.** A deployment (`LOOM_DEPLOYMENT=1`) that requires auth refuses
  to start with the checked-in dev secret; only `LOOM_INSTANCE_LABEL=deploy:dev` is exempt
  (`require_real_signing_secret`). `loom doctor` and `/v1/health` flag it elsewhere
  (`dev_jwt_secret`).
- **Stored tokens.** Private-repository tokens, tracker-source passwords, document-source
  credentials (`loom.knowledge_sources.credential_enc`) and provider keys are encrypted
  at rest with a key derived from `LOOM_JWT_SECRET` and a per-purpose salt (`seal` and
  `unseal` in `server/core/secrets_at_rest.py`), as are the signing keys in
  `loom.signing_keys`. Changing the secret makes them undecryptable; re-enter
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
- A `high`-sensitivity value has no embedding row; a chunk, session digest or
  conversation turn whose text reads as high is written without a vector
  (`loom reembed --in-database` and a chunk restored with the vector an earlier build
  gave it excepted), and the ranked reads (`memory.search`, `memory.ask`,
  `conversation.search` and the saved search) hold back one an earlier build embedded,
  except a chunk that holds only part of a key; see Sensitivity for the reads that serve
  it as written.
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
| Extraction policy           | `server/memory/app/extraction_policy.py` (`effective`), `server/memory/domain/extraction_policy.py`                         |
| Sensitivity                 | `server/memory/domain/sensitivity.py`, `server/memory/app/sensitivity_sweep.py`                                             |
| Audit                       | `server/observability/audit.py`, `server/insights/domain/audit_checkpoints.py`, `server/insights/workers/audit_verifier.py` |
| Erasure                     | `server/erasure/app/saga.py`, `server/erasure/store/`, `server/erasure/workers/recovery.py`                                 |
| Outbound webhooks           | `server/core/webhooks.py`, `server/memory/app/webhooks.py`, `server/memory/workers/webhook_deliver.py`                      |
| Leakage gate                | `tests/integration/test_cross_principal_leakage.py`                                                                         |

## Related

- [Tokens and grants](../guides/tokens-and-grants.md): minting, claims, grants, joining.
- [Identities and namespaces](../concepts/identities-and-namespaces.md): the model.
- [Audit](../features/observability/audit.md): reading the audit log.
- [System overview](system-overview.md): where each gate runs.
