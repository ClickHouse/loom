# Single sign-on (Okta and other OIDC providers)

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

The verified email matches an account by its contact email. For a person
with no account:

- `LOOM_OIDC_JIT_PROVISION=true` creates one on their first sign-in: a
  personal namespace in `LOOM_OIDC_JIT_ORG`, role `writer`.
- `LOOM_OIDC_JIT_EMAIL_DOMAINS=example.com,example.org` limits that to
  those domains. Anyone else is shown "sign-in denied", and `loom login`
  stops with an error.
- With JIT off, only existing accounts can sign in.

Platform admins are the verified emails listed in `LOOM_PLATFORM_ADMINS`.
The provider can never grant admin.

## Sessions and logout

- The dashboard's SSO token lasts 12 hours. After that the user clicks the
  button again, and Okta's own session usually makes that one click.
- `loom logout` revokes the Loom session on the server and the Okta refresh
  token at Okta.
- If a spent refresh token is ever presented again, Loom ends the whole
  session, because someone else holds a copy.
- Every sign-in, approval, denial, JIT account and ended session writes an
  `auth.*` row to the audit log.

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
