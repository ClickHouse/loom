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
  `auth.sso_link_declined` is audited: a second account there naming the same
  address would receive every grant made to it. To free an address someone
  else registered, release that account (`loom claim-release`), which clears
  its email and links; with `LOOM_OIDC_JIT_PROVISION=true` the person's next
  sign-in then makes their own account. An account in another org does not
  stop them. A revoked account naming the address blocks new accounts, since
  it may be the same person.

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
`sa:<name>`, last an hour, and its writes are stamped `actor=process:sa:<name>`.

- **A key.** Loom keeps the public half; the bot signs a short assertion for
  each hour-long token (OAuth client credentials with `private_key_jwt`).

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

  An exact `tenant_id` keeps one trust to one tenant, so each tenant's agents
  land in their own org (a prefix of `sub` may be added as a further
  condition, never as the only one). `--jwks-uri`
  names where the issuer publishes its keys when its discovery document is not
  reachable from Loom. A token that lives minutes is fine: Loom's never
  outlives it, and the SDK and the coding-agent plugins renew halfway through.
  Instead of the agent holding its token, a proxy in front of it can make the
  same exchange and add the `Authorization` header itself. An exchange may
  leave out `client_id` (RFC 8693 does not require it): Loom then takes the one
  active trust of the token's issuer whose audience and claims it meets, and
  refuses a token that meets none or several.

  Disabling the trust ends every agent's token; `loom revoke-token --subject
sa:fleet:<agent>` ends one agent's, and `loom claim-release` frees its
  namespace for good (that agent is refused after). An agent the control
  plane retires keeps its namespace and data until you release or erase it.

  Every trust bounds what it takes: a workload token living longer than
  `--max-token-age` seconds (exp less iat, default 3600) is refused, an agent
  trust makes at most `--max-agents` agents (default 1000), and a trust needs
  one claim with an exact value besides `iss`, `aud`, `exp`, `iat`, `nbf` and
  `jti`, which every token its issuer signs for Loom carries. Two trusts of one
  issuer that a single token could meet, one of them telling agents apart, are
  refused at registration: an exchange naming no client could take neither.
  The signing algorithm follows each published key's type (RS256 or ES256).

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
  `loom revoke-token --subject <their subject>`.
- Every sign-in, device approval and denial, MCP consent, JIT account and
  ended session writes an `auth.*` row to the audit log:
  `auth.sso_signin`, `auth.signin_denied`, `auth.device_approved`,
  `auth.device_denied`, `auth.connect_approved`, `auth.connect_denied`,
  `auth.jit_provisioned`, `auth.sso_linked`, `auth.sso_unlinked`, `auth.sso_link_declined`, `auth.password_set`, `auth.machine_created`, `auth.machine_token`, `auth.machine_disabled`, `auth.agent_provisioned`,
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
or renamed with a `userName` other than that address).
That address must be the one the provider puts in the ID token's `email`
claim, since that is what a sign-in carries.

In Okta, on the app integration from [Okta setup](#okta-setup):

1. **General → App Settings → Edit**, set **Provisioning** to **SCIM**, and save.
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
  ([Accounts](#accounts)). Likewise a token an operator minted
  with the email itself as its subject (`loom mint-token --subject <email>`)
  for an adopted account whose subject is a handle: revoke the email with
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
the email stays. Requests are idempotent, so the provider's retries are safe, and
they run side by side: only those for people who reach a shared account wait
on each other (a busy one answers 503 and the provider retries it). The
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
sign-in: to let a deactivated person
sign in again once SCIM is off, set `LOOM_SCIM_TOKEN` again and send
`active: true` for them, from the provider or by hand. Reactivate at the
provider first anyone who should keep Loom.

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

## Behind a proxy

The sign-in limits per 15 minutes (10 wrong device codes, 30 new device
sign-ins, 300 identity-provider sign-in starts and 30 failed callbacks) count
per client address, an IPv6 /64 as one. Set `LOOM_TRUSTED_PROXIES` to your proxy's
addresses so that address is the user's own, not the proxy's for everyone.

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
reuses its name. A token made before this build whose parent's name was
reused before it lost that link: it is still refused (the revocation reaches
it through its delegation chain), but it stays listed live and keeps its
name. List those with

```sql
SELECT org, name, sa_subject, created_by FROM loom.service_accounts FINAL
WHERE revoked_at IS NULL AND empty(lineage) AND startsWith(created_by, 'sa:')
  AND NOT match(created_by, '^sa:[^:]+:')
  AND created_by NOT IN (SELECT sa_subject FROM loom.service_accounts FINAL)
```

It leaves out the agents of a machine trust (`sa:<trust>:<agent>`), which have
no row there; a token made from an operator-minted `sa:` subject (`loom
mint-token --subject sa:…`) shows up too, and is not one of these. Revoke each
token it lists from its owner's Tokens list, or as a namespace admin
(`/v1/me/service_accounts.revoke`), and then the tokens each one made (those
whose `created_by` is its `sa_subject`), down the chain. While the previous
build still serves, a token it lists, revokes or reuses the name of records no
lineage.

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

which prints a link that sets it, once, within a day, and only while SSO is
off. The account stays linked, so turning SSO on again reaches it. Turning
SSO on or off leaves bots (`loom bot`) and operator-minted tokens alone.

## Break glass

With SSO on, nobody can sign in with a password, including operators. If the
provider is down, mint a token on the server host with
`docker exec loom-server loom mint-token --subject <you> --org <org> --namespaces <ns>`
and open `/me#token=<token>`.

## Other providers

The same settings work with any OIDC provider that serves discovery at
`<issuer>/.well-known/openid-configuration`:

- **Microsoft Entra ID:** `LOOM_OIDC_ISSUER=https://login.microsoftonline.com/<tenant>/v2.0`.
  Its ID tokens often carry no `email_verified`, and Loom refuses those.
  Use a provider configuration that asserts it.
- **Google:** `LOOM_OIDC_ISSUER=https://accounts.google.com`. Google issues
  no refresh token to Loom, so sessions last an hour.
- **Keycloak:** `LOOM_OIDC_ISSUER=https://<host>/realms/<realm>`.
