# Single sign-on (OIDC)

Loom can hand the human sign-in to an OpenID Connect identity provider. With
it on, the dashboard shows one **Sign in with SSO** button, `loom login` and
`loom.login()` approve through the provider, MCP clients that connect with
OAuth sign in there too, and Loom refuses password sign-in and self-signup.
Loom still issues its own tokens; the provider only vouches for who the user
is. Off by default: a fresh install signs in with passwords.

## How sign-in works without SSO

With auth on (`LOOM_AUTH_REQUIRED=true`), `loom login` works out of the box:
it prints a URL and a code, the user opens the URL on any browser, signs in
with their Loom password and approves. Nothing here needs configuring. See
[tokens and grants](../guides/tokens-and-grants.md#sign-in-from-a-terminal-or-a-notebook).

## Okta setup

1. In the Okta admin console, **Applications → Create App Integration →
   OIDC → Web Application**.
2. **Grant types:** Authorization Code and Refresh Token. Under refresh
   token behaviour pick **Rotate token after every use**.
3. **Sign-in redirect URI:** `<public Loom URL>/oauth/callback`, exactly
   (for example `https://loom.example.com/oauth/callback`). No sign-out
   redirect is needed.
4. **Assignments:** the people or groups allowed into Loom.
5. Copy the client ID and secret, then set:

```bash
LOOM_OAUTH_LOGIN_PROVIDER=oidc
LOOM_OAUTH_ISSUER=https://loom.example.com      # Loom's public URL, as in step 3
LOOM_OIDC_ISSUER=https://your-org.okta.com      # or https://your-org.okta.com/oauth2/default
LOOM_OIDC_CLIENT_ID=0oa…
LOOM_OIDC_CLIENT_SECRET=…
```

Loom refuses to start when one of these is missing. The org authorization
server is enough: Loom reads only the ID token's standard claims.

### Scopes and claims

Loom asks for `openid email profile`, plus `offline_access` when the
provider's discovery document lists it (Okta's does). It needs:

- `email` with `email_verified: true` — the account key. A sign-in without a
  verified email is refused.
- An Okta refresh token (from `offline_access`). Every Loom refresh, at most an
  hour apart, spends it at Okta first. A user deactivated in Okta, or signed
  out with **End all sessions**, therefore loses Loom at their next refresh.
  A provider that issues no refresh token gets no Loom refresh token either:
  its sessions end when the hour-long access token does.

Group claims are not mapped. Namespace access comes from Loom's own grants.

Loom checks an ID token's signature with the algorithms the discovery document
lists in `id_token_signing_alg_values_supported`, among RS256, RS384, RS512,
PS256, ES256, ES384 and ES512, and with RS256 alone when it lists none. A token
signed with an algorithm the document does not list is refused, as is any token
with `none` or an HMAC algorithm (`HS256` and the like), whatever the document
lists; a provider that lists only those signs no one in.

## Accounts

The verified email matches an SSO account (one with no password) by its
contact email. A password account is matched only once it is linked to the
email: self-signup took any email without verifying it, so an email match
alone would hand the account to whoever typed the address first. A linked
account keeps its subject, grants, namespaces and data; no new one is made.

- **Right after turning SSO on**, link your people's accounts in one go:
  `loom sso adopt --domain example.com` lists the password accounts at that
  domain with their owners, and `--apply` links them (it refuses while SSO is
  off: the passwords would still sign in, past the revocation below). It reads
  the settings of where it runs, so run it with the servers' (inside their
  container, or with the same environment), or call `/v1/admin/sso.adopt`,
  which a running server answers. It vouches that the
  domain's addresses are the provider's people, so read the list: an address
  someone else registered is linked too. It skips an email whose accounts are
  in several orgs or have several owners, or that SSO already reaches. It
  revokes every token the adopted subjects hold, so people sign in again, and
  that includes the service-account and MCP tokens they made from their
  sessions: CI using one of those breaks until it gets a bot of its own (see
  below). `--keep-tokens` keeps them, and with them any tokens a squatter made.
  A bot that signed up with a password at the domain is in the list too:
  leave it out with `--skip <its email>` (repeatable), so it keeps the tokens
  it runs on until it moves to `loom bot`. A `--skip` that matches no unlinked
  account there refuses the whole run, so a typo never adopts the bot.
- **Anyone not adopted** is asked, at their first SSO sign-in, for their
  account's Loom password once; it then links. Wrong passwords count against
  the usual sign-in limits.
- An account registered under another address (a personal one, a `+tag`) is
  not at the domain, or not under the address the provider verifies: `loom sso
link --email <their provider address> --org <org> --namespace <ns>` shows
  the account, and `--apply` makes that address its contact and links it, so
  their next SSO sign-in lands on it with its subject, grants, data and tokens
  (none are revoked). It needs SSO on, as adopt does: with self-signup closed,
  nobody can register the freed address and inherit what it named. The old
  address's links go, and a grant once revoked under it is carried to the
  account's subject, revoked, so it goes on capping the person's tokens. It
  refuses an old address that still holds live grants of its own (grant them
  to the account's subject and revoke them first) or a pending request to join
  (reject it), and an address another account already names. When that is a
  JIT account an earlier SSO sign-in made, `loom claim-release` it first: its
  memories stay behind in it (carry them over with `namespace.snapshot` and
  `namespace.restore`), and grants made to it must be made again.
- `loom sso unlink --email <email> --apply` takes a link back. Sessions already
  open are not ended; revoke them with `loom revoke-token --subject`.
- Someone who says a password account naming their address is not theirs is
  denied when that account is in `LOOM_OIDC_JIT_ORG`, and
  `auth.sso_link_declined` is audited: beside that account, a password sign-in
  with the address would still find it, and a revocation of the address would
  reach both. To free an address someone else registered, release that account
  (`loom claim-release`), which clears its email and links; with
  `LOOM_OIDC_JIT_PROVISION=true` the person's next sign-in then makes their own
  account. An account in another org does not stop them. A revoked account
  naming the address blocks new accounts, since it may be the same person.

For a person with no account:

- `LOOM_OIDC_JIT_PROVISION=true` creates one on their first sign-in: a
  personal namespace in `LOOM_OIDC_JIT_ORG`, which the person is admin of;
  their sessions carry role `writer`.
- `LOOM_OIDC_JIT_EMAIL_DOMAINS=example.com,example.org` limits that to
  those domains. Anyone else is shown "sign-in denied", and `loom login`
  stops with an error.
- With JIT off, only existing accounts can sign in.
- A JIT account's namespace is `u-<name>-<8 hex>`, named from the email.
  With JIT on, nobody else can create one. A name someone made before JIT
  was turned on stays theirs, and the person gets the next free one
  (`-2`, `-3`, then a random suffix).

Platform admins are the verified emails listed in `LOOM_PLATFORM_ADMINS`.
The provider can never grant admin.

## Bots and CI

A bot never signs in at the provider, and should hold no long-lived secret.
Register it with the namespaces its data belongs in (usually a team namespace
with more than one admin, registered first, in one org); its tokens are for
`sa:<name>`, last an hour, or the bot's maximum when shorter, unless its request
asks for another lifetime up to that maximum (below), and its writes are stamped
`actor=process:sa:<name>`.

- **A key.** Loom keeps the public half; the bot signs a short assertion for
  each token (OAuth client credentials with `private_key_jwt`).

  ```bash
  loom bot create --name triage-bot --org acme --namespace-role triage=writer \
    --generate-key ./triage-bot.pem
  # on the bot, with `pip install clickhouse-loom[machine]`:
  LOOM_CLIENT_ID=triage-bot LOOM_CLIENT_KEY_FILE=./triage-bot.pem python agent.py
  ```

  The key comes from one of: `LOOM_CLIENT_KEY_FILE` (a PEM file), `LOOM_CLIENT_KEY`
  (the PEM itself, as a secret manager injects it into the environment), or
  `LOOM_CLIENT_KEY_SECRET` (an AWS Secrets Manager secret id or ARN holding the
  PEM; `pip install clickhouse-loom[aws]`). The SDK reads it again for each
  token, so a key rotated in place is picked up. The TypeScript SDK
  (`@clickhouse/loom`) takes the same variables; any other client signs the
  assertion itself (RFC 7523: `iss` and `sub` the bot's name, `aud` the
  `token_endpoint` from `/.well-known/oauth-authorization-server`, a two-minute
  `exp` and a fresh `jti`).

- **A CI job.** No secret at all: the job hands over the OIDC token its CI
  platform issues for that run, and Loom accepts it only from that issuer, for
  that audience, with every `--claim` matching (a value ending in `*` matches a
  prefix). Any job on the platform can get a token for any audience, so name
  the repository and branch in claims.

  ```bash
  loom bot create --name deploy --org acme --namespace-role deploys=writer \
    --issuer https://token.actions.githubusercontent.com --audience loom \
    --claim repository=acme/app --claim ref=refs/heads/main
  # in the job (it needs permission to request its OIDC token):
  LOOM_CLIENT_ID=deploy LOOM_WORKLOAD_AUDIENCE=loom python deploy.py
  ```

  A platform that hands the job its token instead puts it in
  `LOOM_WORKLOAD_TOKEN` (or a file named by `LOOM_WORKLOAD_TOKEN_FILE`). Every
  client in the job exchanges it as often as it needs while it is valid, and
  Loom's token never outlives it.

- **Many agents, one trust.** A control plane that issues each agent its own
  OIDC token registers one trust for all of them. `--agent-claim` names the
  claim that says which agent a token is for (letters, digits and `-`). Each
  agent gets the subject `sa:<trust>:<agent>` and its own namespace
  `<trust>-<agent>` in the trust's org, made on its first exchange. The
  `--namespace-role`s are what every agent shares besides its own. The trust
  never reaches a namespace it did not make, and a claim never picks the org.
  People reach an agent's memory as any namespace's: an admin grants it.

  ```bash
  loom bot create --name fleet --org acme --issuer https://issuer.example \
    --audience loom --claim tenant_id=acme-prod --agent-claim agent_id \
    --jwks-uri https://keys.issuer.example/jwks.json
  # on each agent: its token in a file, renewed by the control plane
  LOOM_CLIENT_ID=fleet LOOM_WORKLOAD_TOKEN_FILE=/run/agent/oidc-token python agent.py
  ```

  One agent can let another of the same trust read its memory, one way. The
  control plane decides, and calls as the lending agent (with that agent's
  token):

  ```bash
  curl -s -X POST "$LOOM_BASE_URL/v1/grants.share_agent" -H "Authorization: Bearer $A_TOKEN" \
    -d '{"org":"acme","namespace":"fleet-a","grantee":"b","role":"viewer"}'
  ```

  Agent `b`'s next token names `fleet-a` as viewer (minted on another server,
  within `LOOM_GRANT_CACHE_TTL_SECONDS`); `a` gets nothing of `b`'s. `role` is
  `viewer` or `writer`. An agent shares only its own namespace, only with
  agents of its trust, and not while the namespace is restricted (a share made
  before stays until it is revoked)
  ([`grants.share_agent`](../reference/operations/grants.md#grantsshare_agent)).
  A share never lowers a grant `b` holds, nor writes over one someone else
  made (409): to lower its own share, an agent takes it back and shares again.
  [`grants.unshare_agent`](../reference/operations/grants.md#grantsunshare_agent)
  with the same body (no `role`) takes back what the agent lent: `b` is
  refused at once on that server, and within `LOOM_GRANT_CACHE_TTL_SECONDS` on
  the others. A grant an admin made stays (409). An admin of the namespace
  grants or revokes `sa:fleet:b` with `grants.create` and `grants.revoke`; an
  agent's token takes any grant its subject holds in the org, as writer at
  most. A token names lent namespaces by name while all its namespaces, its
  own and the trust's included, fit 4,096 bytes of the token (fifteen of the
  longest names, more of shorter ones), which keeps it under a proxy's 8 KB
  header limit; a share that would pass that is refused, and one that only
  raises a role the token already names is not (two shares at once can pass
  it, and the token then names those that fit). A trust's shared namespaces,
  with room for a 63-character agent's own, must fit those 4,096 bytes, or
  `loom bot create` refuses it.

  An exact `tenant_id` keeps one trust to one tenant, so each tenant's agents
  land in their own org (a prefix of `sub` may be added as a further
  condition, never as the only one). `--jwks-uri`
  names where the issuer publishes its keys when its discovery document is not
  reachable from Loom. A token that lives minutes is fine: Loom's never
  outlives it, and the SDKs and the coding-agent plugins renew it before it
  runs out (below).
  Instead of the agent holding its token, a proxy in front of it can make the
  same exchange and add the `Authorization` header itself. An exchange may
  leave out `client_id` (RFC 8693 does not require it): Loom then takes the one
  active trust of the token's issuer whose audience and claims it meets, and
  refuses a token that meets none or several.

  Disabling the trust ends every agent's token; `loom revoke-token --subject
sa:fleet:<agent>` ends one agent's, and `loom claim-release` frees its
  namespace for good (that agent is refused after). An agent the control
  plane retires keeps its namespace and data until you release or erase it.

  An agent's namespace exists from its first exchange. To grant the agent
  something before then, such as a namespace a control plane attaches as it
  creates the agent, a platform admin makes it ahead:
  `loom call /v1/admin/bots.provision_agent --data '{"name":"fleet","agent":"a"}'`
  makes `fleet-a` as that exchange would (the trust's mark, the agent as its
  one owner, counted against `--max-agents`) and answers its org, namespace
  and subject (`sa:fleet:a`). It audits `auth.agent_provisioned` with the
  admin as actor, makes no namespace the trust would refuse, and a repeat
  changes nothing; the agent's first exchange takes the namespace as its own.

  Every trust bounds what it takes: a workload token living longer than
  `--max-token-age` seconds (exp less iat, default 3600) is refused, an agent
  trust holds at most `--max-agents` live agents (default 1000; a released
  agent's namespace gives its place back, though its name is never made again),
  and a trust needs
  one claim with an exact value besides `iss`, `aud`, `exp`, `iat`, `nbf` and
  `jti`, which every token its issuer signs for Loom carries. Two trusts of one
  issuer that a single token could meet, one of them telling agents apart, are
  refused at registration: an exchange naming no client could take neither.
  The signing algorithm follows each published key's type (RS256 or ES256).
  Exchanges are limited per 10 minutes, across every replica: 50,000 for one
  trust (`LOOM_WORKLOAD_EXCHANGES_PER_TRUST`) and, in a trust with an agent
  claim, 300 for one agent (`LOOM_WORKLOAD_EXCHANGES_PER_AGENT`). A trust
  without agents is one workload and has only the trust's limit: every job of
  a repository shares one `sub`. Past a limit, `/oauth/token` answers 429,
  and the SDKs and the plugins keep a token they still hold and try again
  30 s later. Only a token the issuer signed counts, so nobody without the
  issuer's key can use up a trust's budget, and an agent refused at its own
  limit spends none of the trust's. Raise the trust's limit for a fleet whose
  agents renew often. The SDKs and the plugins renew five minutes before the
  token runs out, or halfway through its life when that comes sooner; the
  SDKs renew in each process, the plugins once per agent home and per new
  workload token (hooks that fire together may each renew). When each
  renewal presents a fresh workload token, a one-hour token renews after 55
  minutes and a five-minute one every 2.5 minutes, four exchanges per 10
  minutes. Loom's token lives only as long as the workload token has left, so
  one the platform replaces partway through its life, or never, is renewed
  more often: a five-minute token replaced every 4 minutes settles at about
  three renewals per replacement, 7.5 exchanges per 10 minutes on average
  (a ten-minute window can hold up to nine), and a token never replaced
  renews ever sooner in its last five minutes.

  A burst of new agents is the sign of a leaked issuer key or a runaway control
  plane, and a trust at its cap refuses every new agent: alert on
  `loom.auth.agents_provisioned` and `loom.auth.trust_agents_used_ratio`
  ([what to alert on](./monitoring.md#what-to-alert-on)).

- **How long a token lives.** Unasked, a token lives an hour, or the bot's
  maximum when that is shorter. A bot registered with
  `--max-access-token-ttl SECONDS` (60 to 86,400; `"max_access_token_s"` on
  `/v1/admin/bots.create`, listed by `/v1/admin/bots.list` and, when it is not
  an hour, by `loom bot list`) gets a longer one when its request to
  `/oauth/token` asks: `expires_in=<seconds>` beside the other form fields. It
  gets what it asked up to that maximum (more is cut to it, not refused), and
  the answer's `expires_in` says what it got; a maximum under an hour shortens
  the default too. A CI or agent token still never outlives the workload token
  handed over, so a control plane that wants day-long tokens hands over a
  workload token that lives a day, on a trust whose `--max-token-age` takes one.
  An `expires_in` that is not a whole number of seconds, or is under 60, is
  refused with `invalid_request` before the proof is spent or counted.

  A maximum other than an hour is refused until every replica runs a server that
  honours it (migration 77), and while no running server has registered in
  `loom.instances` yet: an older server ignores `expires_in` and the maximum,
  issues an hour, and resets the maximum to 3600 when it saves a bot. After a
  rollback to such a server, every bot's tokens last an hour again, and its
  saves reset the maximum; reapply it once the newer server serves again, with
  `loom bot create --replace`, the bot's key or trust and roles as registered
  and `--max-access-token-ttl`. Replacing a bot also ends the tokens it holds.

  A longer token is no harder to end: disabling or replacing the bot,
  `loom revoke-token --subject` without `--expires-at`,
  `/v1/admin/revoke_subject` (with or without `issued_before`) and
  `loom claim-release` refuse its tokens at once, whatever their expiry, and
  `/oauth/revoke` keeps one token's revocation until that token expires. An
  `--expires-at` earlier than a bot token's own expiry lets that token back in
  once the revocation lapses. What a token names is read when it is issued: a
  share or grant made later reaches an agent at its next token, up to a day
  later with day-long tokens, while revoking one does not wait for that.
  Deleting a namespace (`namespace.delete`) does not end the bot tokens that
  name it, as `loom claim-release` does: they keep their access until they
  expire, up to a day for a bot that allows it, so replace or disable the bot,
  or revoke its subject, to end them at once. Nor does rotating the issuer's
  signing key end the Loom tokens already exchanged for its old tokens: they
  live until they expire, so replace or disable the trust to end them at once.

- `loom bot list` shows them; `loom bot disable --name …` refuses every later
  proof and revokes the tokens the bot holds, and `loom bot create --replace`
  (a new key, fewer roles) revokes them too. A platform admin does the same
  from anywhere with `loom call /v1/admin/bots.create --data '{…}'` (with
  `"generate_key": true` the answer carries the private key, once, and a
  `warning` to send the request again if saving it may not have finished),
  `/v1/admin/bots.list` and `/v1/admin/bots.disable`; `/v1/admin/sso.adopt`,
  `sso.link`, `sso.unlink` and `sso.password_link` mirror `loom sso`.
- Replacing or disabling a bot revokes its tokens up to the next second plus
  5 s, and both wait that out (about 6 s) before they return. Replica clocks
  must agree within 5 s (NTP does): a replica ahead could issue a token from
  the old bot that survives, and one behind refuses the new key until its
  clock passes the cutoff.

Software that can only hold a static key gets an operator-minted token, with an
expiry (`--ttl-days 90`, re-minted on a schedule) or without one, which lives
until `loom revoke-token`:

```bash
loom mint-token --subject sa:legacy --org acme --service-account \
  --namespace-role legacy=writer --json
```

A system that administers Loom itself, such as a control plane that registers
trusts and grants for its tenants, holds the `platform_admin` role as a bot.
**Platform admin spans every org**: a token holding it passes every admin route
and acts in any org, so give it to as few identities as you can, and prefer a
trust (no secret to leak) to a key. `--platform-admin` registers such a bot; it
takes no `--namespace-role` (its tokens cover every namespace), no
`--agent-claim` (it is one identity, never an agent's), and only exact `--claim`
values (a prefix would hand the role to every token it matches). Give it an
audience of its own, so no token meant for another trust can meet it:

```bash
loom bot create --name controlplane --org acme --platform-admin \
  --issuer https://cp.example/workload-identity --audience loom-admin \
  --claim sub=controlplane
```

Each proof gets a token for `sa:controlplane` carrying the role, with the same
requested lifetime and maximum as any bot. Without a request it lasts an hour,
or the bot's maximum when shorter. `loom bot disable` ends it as it ends any
bot's. Only a person signed in
to Loom as a platform admin (`"platform_admin": true` on `/v1/admin/bots.create`)
or an operator on the host sets the flag: the route refuses it to every service
account's token, a platform-admin bot's included, and to a third-party MCP
client's session, whose tokens end with it, so a leaked token never mints itself
a lasting successor. The `auth.machine_created` audit row records it, and
`loom bot list` and `/v1/admin/bots.list` show it. A trust of the same issuer
that tells agents apart must not be able to take the platform-admin trust's
token: give each a claim the other's tokens cannot meet, such as distinct `sub`
values (`sub=controlplane` for this one, `sub=tenants/<id>/agents/*` beside an
exact `tenant_id` for a tenant's), or registration refuses the second one.
Where a trust cannot be used, an operator mints an expiring platform-admin token
for a service account instead. `--platform-admin` refuses a token without
`--ttl-days` or for longer than 90 days, refuses an agent's subject
(`sa:<trust>:<agent>`), and records each mint as an `auth.platform_admin_minted`
audit row:

```bash
loom mint-token --platform-admin --subject sa:controlplane --org acme --ttl-days 30 --json
```

Admin machine tokens are issued only after every live replica protects human sign-in
changes. An older replica or an unread fleet refuses the exchange with 503
`temporarily_unavailable`; the CLI reads a fresh fleet view and refuses to mint as well.
Both checks include deployment and non-deployment processes sharing ClickHouse;
neither uses the replica cache, which separates those kinds.
The CLI requires at least one live replica and cannot mint against an empty fleet.
Before rolling back to an older build, disable the admin bots and revoke every
standalone CLI-minted admin subject or token before that build starts serving.

SSO does not affect bots or minted tokens. A token a person creates from a `loom
login` or dashboard SSO session (`/v1/me/service_accounts`) lasts as long as
they chose and belongs to them: revoking the person ends it. One a third-party
MCP client's session creates ends with that session. Long-running bots use one
of the above, so they do not stop with someone's offboarding.

## Sessions and logout

- The dashboard's SSO token lasts an hour. After that the user clicks the
  button again, and Okta's own session usually makes that one click.
- `loom logout` revokes the Loom session on the server and the Okta refresh
  token at Okta.
- If a spent refresh token comes back, someone else may hold a copy (or a
  client lost the answer to a refresh): Loom ends that session and the
  person signs in again. Service accounts and MCP tokens minted from a
  `loom login` or dashboard SSO session are not touched; after a suspected
  theft, review them (`loom call /v1/me/service_accounts`) and revoke what you
  don't recognise. Those a third-party MCP client's session minted end with it.
- Okta refusing the user at a refresh ends that session.
- Service accounts and MCP tokens a person creates from a `loom login` or
  dashboard SSO session outlive it, including `loom logout`, for as long as
  they chose (blank is 90 days, 0 never expires). Every one is saved to their
  token list, so nothing a person cannot see outlives the session. A
  third-party MCP client's session mints none that outlive it. Disabling
  someone at the provider ends their sessions at the next refresh (within the
  hour) but not these tokens: only a refresh asks the provider about a person.
  With [SCIM](#deprovisioning-with-scim) set up, the provider tells Loom on
  its next provisioning run (Okta when the person is deactivated, Entra within
  its cycle), and Loom revokes the person and every token they made. Without it,
  revoke the person when offboarding, which ends every token they made too:
  `loom revoke-token --subject <their subject>`, or
  [`POST /v1/admin/revoke_subject`](#deprovisioning-without-scim) from anywhere.
- Every sign-in, device approval and denial, MCP consent, JIT account and
  ended session writes an `auth.*` row to the audit log:
  `auth.sso_signin`, `auth.signin_denied`, `auth.device_approved`,
  `auth.device_denied`, `auth.connect_approved`, `auth.connect_denied`,
  `auth.jit_provisioned`, `auth.sso_linked`, `auth.sso_unlinked`, `auth.sso_link_declined`, `auth.password_set`, `auth.machine_created`, `auth.machine_token`, `auth.machine_disabled`, `auth.agent_provisioned`, `auth.platform_admin_minted`,
  `auth.session_revoked`, `auth.scim_deactivated`, `auth.scim_reactivated`,
  `auth.scim_deleted`, `auth.scim_collateral`.

## Deprovisioning with SCIM

Identity providers push deactivations to apps over SCIM 2.0 (RFC 7643/7644).
Loom accepts that push: once the provider sends it, the person's sessions and
the tokens they made are refused at once by the Loom process that took the
request, and by every other process, on that replica or another, at its next
revocation refresh (`LOOM_JWT_REVOCATION_CACHE_TTL_SECONDS`, 30 s by default).
No process makes them a new token meanwhile: every mint reads revocations from
ClickHouse. Set a long random token and restart:

```bash
LOOM_SCIM_TOKEN=$(openssl rand -hex 32)
```

Empty (the default) leaves SCIM off and its routes unmounted. Loom refuses to
start with a token shorter than 32 characters, or with SCIM set while sign-in
is not SSO (`LOOM_OAUTH_LOGIN_PROVIDER=oidc`) or auth is off: a deactivation
would then refuse nobody. `loom doctor` says so too. The token is the only
credential `/scim/v2` takes; keep it with the client secret.

To rotate it without refusing the provider in between, list both tokens,
comma-separated, and roll the replicas; set the new one at the provider; then
remove the old one and roll again:

```bash
LOOM_SCIM_TOKEN="$OLD_TOKEN,$(openssl rand -hex 32)"
```

Loom keys a SCIM user on an email: of the request's `emails` entries with an
address, the one marked primary, else the first work one, else the first; or
its `userName` when it sends none (Loom logs a warning when a user is created
or renamed with a `userName` other than that address). That address must be
the one the provider puts in the ID token's `email` claim, since that is what
a sign-in carries.

In Okta, on the app integration from [Okta setup](#okta-setup):

1. **General → App Settings → Edit**, set **Provisioning** to **SCIM**, and
   save.
2. **Provisioning → Integration → Edit:**
   - **SCIM connector base URL:** `<public Loom URL>/scim/v2`
   - **Unique identifier field for users:** `userName`
   - **Supported provisioning actions:** Push New Users and Push Profile Updates
   - **Authentication Mode:** HTTP Header, with the `LOOM_SCIM_TOKEN` value as
     the bearer token.

   **Test Connector Configuration**, then save.

3. **Provisioning → To App → Edit:** enable **Create Users**, **Update User
   Attributes** and **Deactivate Users**, and save. Loom keeps no attributes
   but the `userName` (a rename is kept) and the `emails` list, whose keyed
   email cannot change.
   With updates on, a changed email shows as a failed task in Okta (Entra
   logs the same refusal); follow
   [Changing a person's email](#changing-a-persons-email). With updates off,
   Okta never says the email changed: the person's next sign-in makes a new
   account under the new address, which a later deactivation of the old one
   misses.
4. Under **General → Sign On**, set **Application username format** to
   **Email**: Loom knows people by their email.

People already assigned are pushed when you next assign or update them, or
from **Assignments → Provision User**. Other providers (OneLogin, …) take the
same base URL and bearer token.

Okta sends a deactivation (the person deactivated in Okta, or unassigned
from the app) as `active: false` by `PATCH` or `PUT`, and
never sends `DELETE`.

In **Microsoft Entra ID**, on the enterprise application you sign in with:

1. **Provisioning → Get started**, set **Provisioning Mode** to **Automatic**.
2. **Admin Credentials:** **Tenant URL** `<public Loom URL>/scim/v2`, and the
   `LOOM_SCIM_TOKEN` value as **Secret Token**. **Test Connection**, then save.
3. **Mappings:** keep the user mapping. Entra maps `userPrincipalName` to
   `userName` and `mail` to `emails[type eq "work"].value`, and its ID token's
   `email` claim is `mail`, so Loom takes the email from `mail`. Where a user
   has no `mail`, map `mail` from the address their ID token carries, not
   from the UPN, which can be another address. Matching on `userName` (the
   default) or on `emails[type eq "work"].value` both work. Set **Provision
   Microsoft Entra ID Groups** to **No**: Loom keeps no groups, and refuses
   creating one, which Entra would count as a failure each cycle.
4. **Settings → Scope:** **Sync only assigned users and groups**, then
   **Start provisioning**.

Entra sends `active: false` when a user is disabled, soft-deleted or
unassigned, and `DELETE` only on a hard delete (30 days after a soft delete).
It provisions in cycles of about 40 minutes, so a deactivation reaches Loom
in the next cycle; **Provision on demand** on the user sends it at once.

What Loom does with each request:

- **Creating a user** records the email and makes no account: the person's
  first SSO sign-in makes it (JIT) or reaches the one they have, as without
  SCIM. A second create of the same email, or of a `userName` another user
  has, answers 409.
- **Updating** (`PUT`, or `PATCH` of `active`, `userName` or `emails`) keeps
  a new `userName`, unless another user has it (409), and keeps the `emails`
  list it is sent. A `PATCH` applies its operations to that list in order,
  and the request is refused (400 `mutability`) when the list it leaves keys
  another email by the rule above: the person would sign in to another
  account, which a later deactivation of the old email would miss. An `add`
  or `replace` through a filter that selects no entry adds one, as Entra's
  writes expect. Other addresses may be added, changed or removed (see
  [Changing a person's email](#changing-a-persons-email)). A refused request
  that also sets `active: false` still deactivates: providers send a leaver's
  changes together and do not retry a 400. A `PUT` whose body names another
  user's `id` is refused before anything is applied.
- **Deactivating** (`active: false` by `PATCH` or `PUT`, or a `DELETE`)
  revokes outright every subject an SSO sign-in with the email could reach
  (each SSO account naming it and each password account linked to it, in any
  org): every session, and every service account and MCP token the person
  made. Their tokens show as revoked by `scim` in the Tokens list and their
  names are free again; a reactivation does not bring them back. A grant such
  a token held on a restricted namespace in its own name stays in the
  namespace's grant list, though it reaches nothing: revoke it there to tidy
  the list. While it lasts, SSO sign-in with the email is refused ("this
  account is deactivated"), even if it reaches no account yet. The accounts,
  their grants and their data stay; this is not erasure. Bots and CI trusts
  the person registered (`loom bot`) are the org's and keep working: disable
  them with `loom bot disable` if they should stop. A password account that
  names the email but was never linked to it (not adopted, linked or proven at
  an SSO sign-in) is not one an SSO sign-in reaches, so it is left alone:
  revoke it with `loom revoke-token --subject`, or adopt it before offboarding
  ([Accounts](#accounts)). Likewise a token an operator minted with the email
  itself as its subject (`loom mint-token --subject <email>`) for an adopted
  account whose subject is a handle: revoke the email with
  `loom revoke-token --subject <email>`. A subject is a name, not an org's:
  when one also owns an account or holds a grant in an org where the email
  does not reach that subject (another person's `alice`), it is still revoked,
  and Loom logs a warning and audits `auth.scim_collateral` naming those orgs.
  Find it in `loom revoke-token --list` (`scim→<it>`) and, if it is someone
  else's, give it back with
  `loom revoke-token --subject <it> --issued-before <now> --scim`: its new
  tokens work, and its tokens from before the deactivation stay dead (Loom
  refuses an earlier `--issued-before`). The provider repeating the
  deactivation, or deleting the user, leaves it given back, but once the
  person is reactivated, their next deactivation revokes it outright again:
  give it back again then.
- **Reactivating** (`active: true`) lets the person sign in again. Tokens from
  before stay revoked; they make new ones. A subject another deactivated or
  deleted user's email still reaches (two addresses reaching one account)
  stays revoked, and Loom logs a warning for each one it leaves. Once none
  reaches it (the last is reactivated, or an operator moves the account to
  another address with `loom sso link`), the next `PUT` or `PATCH` for an
  active user whose email reaches it lets it go. Okta sends one on a profile
  change only with **Update User Attributes** on; Entra sends one when a
  mapped attribute changes. Otherwise, or at once,
  `loom revoke-token --subject <it> --issued-before <now> --scim` gives it
  back as an operator. A reactivation answers after about 6 s (up to 11 s
  when it comes right after the deactivation), once every replica's clock is
  past its cutoff, so a sign-in after it gets a token that lives (one in that
  wait would get a token from before the cutoff, already dead); a retry sent
  meanwhile waits out the rest. Another process, on that replica or another,
  still refuses the new session until its next revocation refresh, then takes
  it.
- **Deleting** deactivates and the user answers 404 from then on
  (`auth.scim_deleted` is audited). The provider creating them again
  reactivates them, answering after the same wait; a retry of that create
  waits out the rest, then answers 409.

SCIM keeps its revocations apart from an operator's. Each subject a
deactivation revokes gets a SCIM revocation of its own, beside the one
`loom revoke-token --subject` writes, and a subject is revoked while either
says so. SCIM never changes an operator's revocation, and an
operator's never changes SCIM's: a time-boxed `--expires-at` or a lift with
`--issued-before` while the person is deactivated does not let them in, and
a reactivation lifts only SCIM's, so a subject an operator revoked stays
revoked. Only `--scim` changes SCIM's revocation of a subject.

Loom keeps each SCIM user's row in `loom.scim_users` (the userName, the email,
the `emails` list the provider sent, the subjects it revoked and whether it is
active) for as long as the deployment does, deleted users included: that row
is what keeps a deactivated person out. A `DELETE` clears the `emails` list;
the email stays. Requests are idempotent, so the provider's retries are safe,
and they run side by side: only those for people who reach a shared account
wait on each other (a busy one answers 503 and the provider retries it). The
filters served are `userName eq "<userName>"` and, on the email,
`emails[type eq "work"].value eq "<email>"` (or `emails.value eq`), which
match every user with an `emails` entry holding the address, in any case (a
`work` entry for the first), not only the one keyed on it. `GET /Users`,
`GET /Users/{id}` and `GET /ServiceProviderConfig` answer as RFC 7644 says,
and `GET /Groups` answers an empty list.

Turning SCIM off (`LOOM_SCIM_TOKEN` empty) unmounts `/scim/v2`, so nothing
deactivates or reactivates anyone any more, and leaves everyone as they are:
SSO sign-in with a deactivated email is still refused, and a deactivated
person's subjects stay revoked, new tokens included, until an operator lifts
each with `loom revoke-token --subject <it> --issued-before <now> --scim`.
That lets the subject's new tokens work (its old ones stay revoked), not its
sign-in: to let a deactivated person sign in again once SCIM is off, set
`LOOM_SCIM_TOKEN` again and send `active: true` for them, from the provider or
by hand. Reactivate at the provider first anyone who should keep Loom.

### Changing a person's email

Loom keys a SCIM user on the address and refuses a change of it (400), so a
new address must arrive as a new SCIM user. Until it does, a sign-in with the
new address makes an account no SCIM user reaches, which the old address's
deactivation misses. Change it before the person signs in with the new one:

1. Remove the person from the app at the provider. That deactivates the old
   address and revokes the accounts it reaches.
2. In Entra, also delete the old SCIM user: Entra matches users on
   `userName` (the UPN), which an email change keeps, so a reassignment would
   find the old user and send the new address as a change Loom refuses. Find
   its id with `GET /scim/v2/Users?filter=userName eq "<upn>"`, then
   `DELETE /scim/v2/Users/<id>`, both with the SCIM token. Okta looks users
   up by `userName`, which is the email there, so it creates a new one
   itself.
3. Assign the person again. The provider creates the new address's SCIM user.

What happens to the old account:

- A password account: point it at the new address with
  `loom sso link --email <new> --org <org> --namespace <ns> --apply`. The old
  address no longer reaches it, and the next `PUT` or `PATCH` for the new
  user lets it go (or give it back at once with
  `loom revoke-token --subject <it> --issued-before <now> --scim`).
- An account made at an SSO sign-in has no command to move it: the person's
  first sign-in with the new address makes a new account. Grant it the old
  account's namespaces; the old account and its data stay, revoked.

## Deprovisioning without SCIM

A system that knows when a person leaves, such as a control plane that also
issues their sign-in, revokes them over the admin API instead. A platform admin
sends:

```bash
loom call /v1/admin/revoke_subject --data \
  '{"org":"acme","subject":"lee@acme.io","reason":"left","issued_before":"2026-01-31T12:00:00Z"}'
```

Loom revokes the subject and every spelling the org's directory pairs with it,
its email verified or not (a grant admits under verified pairs only): an email
reaches the login handle of each account naming it (an SSO account's subject is
its namespace, `u-…`), and a handle reaches its email. For each, every token
whose subject it is, every token it made or that descends from one of its tokens,
and every refresh of its sign-in sessions are refused at once by the process that
took the request, and by every other process at its next revocation refresh.
Every mint reads revocations from ClickHouse, so a sign-in, a device approval or
an MCP connect for it gets no token either. The service accounts and MCP tokens
it ends show as revoked in the Tokens list, by `admin:<you>`, and their names are
free again, as after a SCIM deactivation. The accounts, their grants and their
data stay; this is not erasure.

`issued_before` (RFC 3339, with its offset) limits the revocation to what was
issued before that instant, for good: a platform deprovisioning someone sends the
current time, so that if the person is invited back later, their next sign-in, a
new device approval or a new MCP connect works. Loom takes the instant to its next
second, plus the 5 s replica clocks may differ by, so nothing issued before it on
any replica survives; a token issued in those few seconds after it is refused too.
An instant ahead of the server's clock counts as now, so a caller's clock running
ahead never sets a cutoff that refuses sign-ins still to come. Without
`issued_before` the revocation is outright, as `loom revoke-token --subject`
writes it: every token of the subject, issued before or after, is refused for
good, which is the form for a permanent ban. A request never weakens what the
subject already has. A cutoff is recorded beside the subject's own revocation (as
an operator cutoff, `kind: cutoff` in the list below) and only ever moves later,
so an outright revocation stays in force, a time-boxed one
(`loom revoke-token --expires-at`) until it lapses, and the cutoff holds after
that; an outright request leaves a revocation that is already outright for good
as it is.

```json
{
  "subjects": ["lee@acme.io", "u-lee-1a2b3c4d"],
  "org": "acme",
  "revoked_at": "2026-01-31T12:00:00",
  "reason": "left",
  "issued_before": "2026-01-31T12:00:06Z",
  "outright": [],
  "sessions": 2,
  "tokens": 3
}
```

`issued_before` in the answer is the cutoff now in force for the subjects not
revoked outright, the earliest when theirs differ: the one the request asked for,
or a later one an earlier request set; it is `null` for an outright request, and
when every subject is revoked outright. `outright` names the subjects refused
outright now: all of them after an outright request, and after a cutoff those an
outright revocation (their own, time-boxed or not, or SCIM's) still holds. `sessions` counts the sign-in sessions of those subjects the revocation
ends (each refreshed before the cutoff), and `tokens` the tokens they made,
directly or through their tokens, that it ends. A subject is a name, not an
org's, so the revocation holds in every org; `org` picks the directory the
spellings come from and the audit log each `token.revoked` row lands in, and an
end user's subject, `<org>/user:<id>`, must name its own org. When the directory
cannot be read, nothing is revoked and the request fails. A repeat answers the
same and writes nothing more, so a caller retries until it gets a 200; a failure
after the revocations landed (marking the tokens did not finish) is finished by
the retry.

`GET /v1/admin/revocations?subject=<subject>` lists a subject's revocations:
its own, an operator cutoff's and SCIM's (`kind`). Without `subject` it lists
every unexpired one, as `loom revoke-token --list` does, newest first; `since`
(RFC 3339) keeps those written since, and `limit` (1 to 10000, default 1000)
bounds the answer. The Python SDK passes them as
`agent.deployment.routes.revocations(params={"subject": ...})`. To let a person
revoked outright back in, narrow the revocation rather than remove it: with
`loom revoke-token --subject <it> --issued-before <now>`, tokens made from then
on work and everything from before stays dead.

## Behind a proxy

The sign-in limits per 15 minutes (10 wrong device codes, 30 new device
sign-ins, 300 identity-provider sign-in starts and 30 failed callbacks) count
per client address, an IPv6 /64 as one. Set `LOOM_TRUSTED_PROXIES` to your proxy's
addresses so that address is the user's own, not the proxy's for everyone.

When Loom cannot reach the identity provider at the issuer's own host (its
public name resolves to an address only browsers reach, for example), set
`LOOM_OIDC_BACKCHANNEL_BASE_URL` to an address it can reach the provider at, such
as `http://idp.example:8080`: a scheme, a host and an optional port, nothing
else. Loom's own calls to the issuer's host then go there, with the path kept and
the issuer's host in the `Host` header: the discovery document
(`<base>/<issuer path>/.well-known/openid-configuration`), the token and
revocation endpoints, and the keys ID tokens are checked with. The browser still
goes to the issuer's `authorization_endpoint`, an endpoint the document names on
another host is called as it is, a bot trust's keys are always fetched from the
https address it names, and the document and every ID token must still name
`LOOM_OIDC_ISSUER` as their issuer. None of these calls follows a redirect. A
plain `http` base sends the client secret unencrypted: use one only on a network
you trust.

## ClickHouse Keeper

Sign-in keeps codes, device grants and each session's current refresh token in
ClickHouse Keeper and changes them only by compare-and-set, so each is spent
exactly once; Loom requires a Keeper and refuses to start without one.
If `/v1/health` reports `oauth_signin_state_full`, the Keeper table hit its key
limit (a flood of device sign-ins): new sign-ins answer 503 until keys expire,
so rate-limit the sign-in routes by network at your proxy.

## Upgrading to this build

MCP connections made before it sign in once more: their refresh tokens had no
session to end them, so they are not rotated. While the previous build still
serves (a rolling upgrade, a rollback), it does not know sessions or operator
cutoffs: an access token whose session ended still works there for its hour,
and so do the tokens a bot disable or an adopt cut off. Finish the rollout
before adopting accounts or relying on a logout or a bot disable. The
previous build serves no SCIM and reads neither deactivations nor SCIM's
revocations: a person deactivated over SCIM still signs in there, and their
tokens still work there. Set `LOOM_SCIM_TOKEN` only once every replica runs
this build, and do not roll back past it while anyone is deactivated: the
previous build lets them sign in. Before a rollback, block each of them at the
identity provider (suspend them, or remove them from the application), and
revoke each subject they reach with `loom revoke-token --subject`. A
deactivated email that reaches no account has no subject to revoke: the
provider's block is all that keeps it out of the previous build.

A token made by another token is found, when a deactivation frees the
person's token names, through the subjects above it. This build records them
on each token, and hands them to a token's children before a new token
reuses its name. A token made before this build loses the person at the top
of its chain when a name above it was reused before this build (later names
reused hand down only what was left): it is still refused (the revocation
reaches it through its delegation chain), but it stays listed live and keeps
its name. List the tokens whose every subject above is a token, none of them
live, so that none leads to a person, with

```sql
SELECT org, name, sa_subject, created_by FROM loom.service_accounts FINAL
WHERE revoked_at IS NULL AND (expires_at IS NULL OR expires_at > now())
  AND arrayAll(s -> match(s, '^sa:[^:]+$'), arrayConcat([created_by], lineage))
  AND (org, name) NOT IN (
    SELECT org, name FROM loom.service_accounts FINAL
    ARRAY JOIN arrayConcat([created_by], lineage) AS above
    WHERE above IN (
      SELECT sa_subject FROM loom.service_accounts FINAL
      WHERE revoked_at IS NULL AND (expires_at IS NULL OR expires_at > now())))
```

A machine trust's agents (`sa:<trust>:<agent>`) count as people here. A token
made from a subject you minted with `loom mint-token --subject sa:…` is listed
on every run and is not one of these: keep it. A token made before this build
by a token since revoked shows up too: it is dead already, and revoking it
frees its name. Revoke each other token it lists as an admin of one of its
namespaces, with `/v1/me/service_accounts.revoke` and its `name` (its creator
is a token, not a person, so it is in no one's own list; a namespace admin's
Tokens list shows it among the newest 200). Then run it again: a token below
one you revoked shows up once that one is revoked. Stop when it lists only
tokens made from subjects you minted. While the previous build still serves, a
token it lists, revokes or reuses the name of records no lineage.

A browser sign-in whose steps land on both builds (a code exchanged, an
identity provider's callback) fails and is started again. The SDK and `loom
login` ride it out: this build marks its token and device sign-in answers with
a `Loom-Sessions` header, so once a server has sent it, a refusal without it is
the previous build's, and they keep the session (a refresh says retry) or keep
polling (a device sign-in). A proxy that strips the header only loses this. Roll
the upgrade quickly so few sign-ins span it: with the Helm chart, a surge of
the whole replica count at once.

## Turning SSO off

Set `LOOM_OAUTH_LOGIN_PROVIDER=password` (or remove it) and restart. Password
accounts, adopted or linked ones included, sign in with their passwords again;
a link never removes a password. A session signed in through SSO ends at its
next refresh, and its owner signs in again. An account JIT made has no
password: give its owner one with

```bash
loom sso password-link --email lee@example.com
```

which prints a link that sets it, once, within `LOOM_PASSWORD_LINK_TTL_SECONDS` (a day
by default), and only while SSO is off. The account stays linked, so turning SSO on
again reaches it. Turning SSO on or off leaves bots (`loom bot`) and operator-minted
tokens alone.

## Break glass

With SSO on, nobody can sign in with a password, including operators. If the
provider is down, mint a token on the server host with
`docker exec loom-server loom mint-token --subject <you> --org <org> --namespaces <ns>`
and open `/me#token=<token>`: the sign-in landing page, which never redirects (`/`
follows `LOOM_DASHBOARD_REDIRECT_URL`, and the browser would carry the token along).

## Other providers

The same settings work with any OIDC provider that serves discovery at
`<issuer>/.well-known/openid-configuration`:

- **Microsoft Entra ID:** `LOOM_OIDC_ISSUER=https://login.microsoftonline.com/<tenant>/v2.0`.
  Its ID tokens often carry no `email_verified`, and Loom refuses those.
  Use a provider configuration that asserts it.
- **Google:** `LOOM_OIDC_ISSUER=https://accounts.google.com`. Google issues
  no refresh token to Loom, so sessions last an hour.
- **Keycloak:** `LOOM_OIDC_ISSUER=https://<host>/realms/<realm>`.
