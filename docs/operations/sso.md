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

- **Before turning SSO on**, link your people's accounts in one go:
  `loom sso adopt --domain example.com` lists the password accounts at that
  domain, and `--apply` links them. It vouches that the domain's addresses are
  the provider's people, so read the list: an address someone else registered
  is linked too. An email with accounts in several orgs signs in to none.
- **Anyone not adopted** is asked, at their first SSO sign-in, for their
  account's Loom password once; it then links. Wrong passwords count against
  the usual sign-in limits.
- `loom sso unlink --email <email> --apply` takes a link back. Sessions already
  open are not ended; revoke them with `loom revoke-token --subject`.

For a person with no account:

- `LOOM_OIDC_JIT_PROVISION=true` creates one on their first sign-in: a
  personal namespace in `LOOM_OIDC_JIT_ORG`, role `writer`.
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

A bot never signs in at the provider. Give it a service-account token scoped
to the namespace its data belongs in, usually a team namespace with more than
one admin, and set it as `LOOM_API_KEY`:

```bash
loom mint-token --subject sa:triage-bot --org <org> --service-account \
  --namespace-role triage=writer --ttl-days 90
```

Its writes land in that namespace, stamped `actor=process:sa:triage-bot`. SSO
does not affect these tokens; revoke one with `loom revoke-token --subject`.
A person can also create one from their session (`/v1/me/service_accounts`),
but it lasts at most 30 days, so long-running bots use an operator's token.

## Sessions and logout

- The dashboard's SSO token lasts an hour. After that the user clicks the
  button again, and Okta's own session usually makes that one click.
- `loom logout` revokes the Loom session on the server and the Okta refresh
  token at Okta.
- If a spent refresh token comes back, someone else may hold a copy (or a
  client lost the answer to a refresh): Loom ends that session and the
  person signs in again. Service accounts and MCP tokens they minted are not
  touched; after a suspected theft, review them (`loom call
/v1/me/service_accounts`) and revoke what you don't recognise.
- Okta refusing the user at a refresh ends that session.
- Service accounts and MCP tokens a person creates from a sign-in session
  outlive it, including `loom logout`, for at most 30 days. Only a refresh
  asks Okta about the user, so for someone with no live session nothing
  checks before then. When offboarding, revoke the person:
  `loom revoke-token --subject <their subject>`.
- Every sign-in, device approval and denial, MCP consent, JIT account and
  ended session writes an `auth.*` row to the audit log:
  `auth.sso_signin`, `auth.signin_denied`, `auth.device_approved`,
  `auth.device_denied`, `auth.connect_approved`, `auth.connect_denied`,
  `auth.jit_provisioned`, `auth.sso_linked`, `auth.sso_unlinked`,
  `auth.session_revoked`.

## Behind a proxy

The sign-in limits per 15 minutes (10 wrong device codes, 30 new device
sign-ins, 300 identity-provider sign-in starts and 30 failed callbacks) count
per client address, an IPv6 /64 as one. Set `LOOM_TRUSTED_PROXIES` to your proxy's
addresses so that address is the user's own, not the proxy's for everyone.

## ClickHouse Keeper

Sign-in keeps codes, device grants and each session's current refresh token in
ClickHouse Keeper and changes them only by compare-and-set, so each is spent
exactly once. The Loom stacks and ClickHouse Cloud have a Keeper. On a
self-managed ClickHouse without one, sign-in answers 503 and `/v1/health`
reports `oauth_signin_needs_keeper`; tokens from `loom mint-token` still work.
If `/v1/health` reports `oauth_signin_state_full`, the Keeper table hit its key
limit (a flood of device sign-ins): new sign-ins answer 503 until keys expire,
so rate-limit the sign-in routes by network at your proxy.

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
