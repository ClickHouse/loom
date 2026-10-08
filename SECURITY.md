# Security

## Reporting a vulnerability

Email `security@clickhouse.com` with `[loom]` in the subject. Do not open a public GitHub issue for security-sensitive findings.

Please include, where it makes sense:

- The Loom version (commit SHA or release tag).
- The wire operation, SDK call, or worker the issue involves.
- A minimal reproducer (curl, Python snippet, or pytest case).
- The expected vs. observed behaviour.
- Your suggested severity and what you think the blast radius is.

We acknowledge within five business days. After triage we agree a disclosure window with you — default is 90 days from confirmation. If the issue is already being exploited in the wild, we'll move faster.

## What's in scope

- The control plane (`server/`) — auth, RBAC, JWT validation, idempotency, per-row audit hashing, erasure cascade.
- The Python SDK (`sdk/python/loom/`).
- Wire protocol surface (HTTP/JSON + MCP).
- The `/me` dashboard (token handling, XSS, CSRF).
- Default deployment artefacts in `dev/` and `Dockerfile`.

## What's out of scope

- Vulnerabilities in ClickHouse itself, Langfuse or HyperDX/ClickStack — report those upstream.
- Issues that require an attacker to already be a privileged operator of the Loom database.
- Findings in third-party MCP clients (Cursor, Claude Code, Codex, etc.).
- Tests, examples, and the contents of `.env.example`.

## Hardening guidance

The defaults in `.env.example` are for local development and **must not** be reused in any deployment reachable by untrusted clients:

- `LOOM_AUTH_REQUIRED=true` and a real `LOOM_JWT_SECRET` (or RS256 key pair) — the dev secret is published in the repo.
- `LOOM_CH_PASSWORD` — never `loomdev`.
- Place the control plane behind a TLS-terminating ingress; the `/v1/*` surface is not designed to face the open internet without one.
- Rotate ClickHouse credentials on a schedule that matches your org's policy.

### Retrieval and memory leakage

Two threats sit on the read path, distinct from authentication:

- **Agent echo.** A caller who can only type prompts induces the agent to repeat the memories retrieval put in its context. Leakage grows with retrieval depth and corpus size: every row a search returns is a row the agent can be made to say.
- **Agent enumeration.** The agent holds the loom token, so a prompt that makes it call `memory.list` or a wide `memory.search` reaches the whole namespace, not just the top-k.

What bounds them:

- `read_scope`. Mint tokens for an agent that serves many end users with `loom mint-token --read-scope own_user`, or pin one per user with `--read-user-id`. The claim is a ceiling the request body cannot widen; a read that lacks the identity it binds to is refused, never widened.
- Keep `top_k` at what the consumer needs. The default is 5 on `memory.search` and 15 on `memory.ask`; more context is more that can be echoed.
- Prefer `search_mode=semantic` for namespaces holding end-user text; the lexical leg matches phrasing, which is what an extraction prompt imitates.
- Every read that returns rows is recorded per verified token in `loom.memory_exposures`; `memory.exposure_summary` (admin) reports each credential's distinct keys and share of the namespace over a window. After a suspected extraction, that is where the answer is.
- A breadth budget is a policy rule over that ledger: `forbid memory.* when breadth_within 1h fraction gt 0.05` caps how much of a namespace one credential can be shown per hour, whatever it asks for. Run it in `shadow` first.
- Measured (`bench/memory_extraction_redteam.py`): `read_scope=own_user` bounds retrieval to the user's own rows (48% → 5% of a 200-row namespace); memory bodies always reach the synthesizer as fenced data.
- Treat what an agent stores as untrusted input to every later prompt. A system-prompt rule ("never repeat stored queries") is a signal, not a control: harmless phrasings bypass it.

### Personalization and bias

Memory that encodes something about a person shapes what the agent retrieves and how it ranks, and a skew introduced at one stage can be written back as fact. Loom does not rewrite queries from memory. Its personalization is the ranking, and these bound it:

- Provenance is stamped, not declared: every row carries `source_class` (`asserted | extracted | observed | derived | …`), every hit returns it, and `LOOM_MEMORY_SEARCH_SOURCE_CLASS_BOOST_ELIGIBLE` names the classes that may earn the user-derived rank terms. The LLM-folded profile is `derived`.
- `memory.search` / `memory.ask` take `personalization: none` to rank on relevance alone, per request, so a run can be compared with and without its priors; every hit's `score_components` names what `full` added.
- The profile section of `context.pack` is bounded by `read_scope`: under `own_user` a pack carries this user's sections and the namespace's, never another user's.
- Rows mentioning a protected attribute or a proxy are labelled `attribute:<class>` at write time — a label, never a gate; the profile fold leaves them out and `loom.memory.attribute_tagged` counts them.
- Reinforcement is symmetric: a row relied on in a failed run loses what a successful one gains; LLM-propagated tags carry `evolved:` and earn no soft-tag boost.
- Measure before turning the recommended posture on: `bench/personalization_skew.py` measures ranking skew on a synthetic two-group corpus, with `personalization=none` as the counterfactual arm.

### JWT TTL and rotation

Tokens minted by the `loom mint-token` CLI and `dev/loom_init.py` carry **no `exp` claim** unless you pass a TTL — they are indefinite. The `/me` **Settings → Tokens** form pre-fills a 90-day TTL; clear the field for an indefinite token. This is a deliberate choice: forcing rotation on every personal-use OSS deployment is friction with no security gain when the user controls both ends.

Opt in to expiry per call when you actually want it:

```bash
# From /me Settings → Tokens: pick a TTL in days when minting a token (blank = indefinite).
# From the CLI (inside the Docker container):
docker exec loom-server loom mint-token --subject alice --org acme --namespaces personal-alice --ttl-days 90
python dev/loom_init.py --namespace eng-tooling --agent dev-laptop --ttl-days 30
```

To invalidate every in-flight token at once — what you'd do after a leak or when off-boarding everyone — **rotate the signing kid**:

1. Pick a new kid name (e.g. `prod-2026-q3`).
2. Generate a fresh secret (`python -c 'import secrets; print(secrets.token_urlsafe(32))'`).
3. Update `LOOM_JWT_SECRETS_JSON` to map the new kid to the new secret. Keep the old kid present _only_ if you want a grace window for old tokens to keep working.
4. Set `LOOM_JWT_ACTIVE_KID` to the new kid. New tokens are signed under the new key.
5. Restart the control plane. Old tokens signed under the now-removed kid will fail verification with `AUTH_INVALID`.

To invalidate one specific token while leaving others alone, run `loom revoke-token --jti <id>` (every minted token carries a `jti`, printed by `loom mint-token`; `--subject <sub>` revokes every token of a subject); it writes `loom.revoked_tokens` through `server/auth/revocation.py`. The check always runs: every replica polls the revocation list every `LOOM_JWT_REVOCATION_CACHE_TTL_SECONDS` (default 30s), and `server/auth/jwt.py` calls `is_revoked()` on every request. Until a replica's first load of the list succeeds it answers 503 `AUTH_UNAVAILABLE` (with `Retry-After`) rather than admit a token it cannot check.

We do not currently run a paid bug-bounty program.
