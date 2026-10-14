# Changelog

All notable changes to Loom (the server, its web app and its coding-agent plugins). The
format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[SemVer](https://semver.org/). The SDKs keep their own changelogs in
[sdk/python](./sdk/python/CHANGELOG.md) and [sdk/typescript](./sdk/typescript/CHANGELOG.md).
A release tag `vX.Y.Z` ships only when the top entry reads `## [X.Y.Z] - YYYY-MM-DD`.

## [Unreleased]

### Added

- `CODED_WIDTH_MISMATCH` (503): a write whose vector is not the width its coded column was
  created at; `memory.set`, `bulk_set`, `update` and the `knowledge.*` writes are refused before
  any of their rows lands, and `/v1/health` reports `coded_width_mismatch` with the fix. A table
  rebuilt at another width outside Loom is noticed within a few seconds; in that window a
  write's version row or document can land while its vectors are refused.
- `OVERRIDE_NOT_SHARED` (503): a budget or rate-limit override the server's web processes could
  not share is refused and changes nothing; the server log says what failed.
- `LOOM_RUNTIME_DIR`: the directory one server's web processes share files in.
  `scripts/serve.sh` chooses it once and exports it; set it for another launcher that starts
  several workers.
- `LOOM_LISTEN_HOST`: the address `scripts/serve.sh` binds; loopback by default, every
  interface in the image.
- Migration `027` adds `run_id` skip indexes to `agent_events` and `agent_runs`.
- The blue/green colours (`dev/docker-compose.colour.yml`) run the bubblewrap
  repository-job sandbox, so `/v1/health` no longer reports `repo_jobs_unsandboxed` there.
  They use narrow profiles instead of `unconfined`: `dev/seccomp/loom-repo-sandbox.json`
  (Docker's default plus one rule allowing `clone`, `unshare`, `mount`, `umount2` and
  `pivot_root` to every process; the default allows the first four only with
  `CAP_SYS_ADMIN`, and `pivot_root` never), the AppArmor profile
  `deploy/apparmor/loom-repo-sandbox` (`docker-default` for every process, and a child
  profile adding `userns, mount, pivot_root` for whatever execs `bwrap` and what it runs)
  and `systempaths=unconfined`, plus `pids_limit: 8192` per container as a backstop.
  Load the AppArmor profile on the host before the next redeploy; `redeploy.sh` (through
  `dev/apparmor_check.sh`) stops when it is missing or the host has no AppArmor, and
  (through `dev/sandbox_probe.sh`) removes the new colour instead of swapping to it when
  the sandbox probe fails, or cannot run, in either of its containers. A CI job
  (`sandbox-profile` in `docker-smoke.yml`) runs the probe and the hostile suite under the
  shipped profile. The other Compose stacks are unchanged. See "Repository-job sandbox" in
  docs/operations/code-index-operations.md.

- `LOOM_CODE_SANDBOX_REQUIRED` (default off; the colours set it): where the sandbox
  cannot run, no repository job runs as a plain process. The ingest pass and the
  compiler tiers skip every entry with a note, `loom code scip-full` exits 1, and
  `/v1/health` reports `repo_jobs_unsandboxed` as an error. `loom code init`'s first bare
  clone stays a plain `git clone`.

- `LOOM_MEMORY_SEARCH_FANOUT_MAX_NAMESPACES` (default 32) caps how many of the caller's
  namespaces a `namespace_scope=all_mine` search reads besides the addressed one. For
  anyone but a scoped token the cut drops public namespaces before private ones.
  `memory.search` names the rest in a new output field, `namespaces_skipped`, and the
  `loom_search` output and text list them. `knowledge.search` and `conversation.search`
  carry the same field, and every page of a paged `memory.search` repeats the first call's
  lists, less any namespace the caller can no longer read.

### Changed

- **Breaking:** `rerank` (`memory.search`, `memory.search_trace`, `code.search`, `code.ask`)
  and a namespace's `rerank_endpoint` take a name, not a URL: `none`, `builtin:openai[:model]`,
  `builtin:cohere[:model]`, or an endpoint the operator names in `LOOM_RERANK_ENDPOINTS_JSON`.
  A URL or an unknown name is a 422. A namespace that still stores a URL is no longer reranked.
- **Security:** revoking or lowering a grant ends what the subject's older tokens claim on
  that namespace: a flat one reaches it only through the subject's current grant, and a
  scoped one keeps its own role there capped at that grant. Tokens minted later (sign-in,
  an OAuth code exchange or refresh) leave it out for good; a later grant gives access
  back through itself, not through the old claim. A restricted namespace, read only by a
  token naming it, is named again while a live grant covers the person. The cutoff is a
  `loom.revoked_tokens` row keyed by org, namespace and a hash of the lower-cased
  subject, and `loom revoke-token --list --grants` shows them. A namespace never
  granted to the subject (named by an operator at mint) keeps its claim. A refresh token
  saved before this release can carry a cutoff that landed while it was minted until it
  expires.
- **Security:** with a Keeper, `/oauth/token` runs one exchange per code or refresh token
  (a KeeperMap claim, `loom.oauth_claims`, migration `027_oauth_claims.sql`); a concurrent
  presenter is told to retry, and a failed read answers a retryable 503
  `temporarily_unavailable` that leaves the credential usable, where a failed account read
  on refresh used to answer 500 and burn the token. Without a Keeper the credential is used
  up before the decision, as before; a failure after that answers `invalid_grant`, and
  `/v1/health` reports `oauth_claims_not_atomic`.
- **Security:** `context.fetch` no longer serves, and `context.offload` refuses to write,
  `page/` blobs: a parked `memory.search` page holds hits from every namespace its search
  fanned out over, and only `memory.search(page=...)` serves it, re-checking each of them.
- Sign-in answers a retryable 503 when it cannot read the grant cutoffs, as an OAuth
  exchange does.
- Sign-in refuses with 409 `TOO_MANY_SPELLINGS` when more than 1000 accounts name the
  person's email, and an OAuth exchange answers `invalid_grant`; taking a grant away on
  that person refuses the same way. An operator releases the extra accounts.
- `grants.approve` refuses (409 `GRANT_WOULD_DOWNGRADE`) to approve a request below the
  subject's current grant; lower a grant with `grants.create replace=true`.
- With auth off, an `all_mine` search also reads every namespace the single user registered,
  not only the addressed one.
- The addressed namespace is always searched first. A scoped token's other namespaces follow
  in its order, so a confident hit in a namespace the token lists first no longer ends the
  search before the addressed one is read. Without a scoped token, an addressed public
  namespace is read in the private pass. Knowledge, conversation and profile search read it
  even when the fan-out does not list it.

### Fixed

- **Security:** OIDC just-in-time provisioning no longer rebuilds an existing account. A
  person's next sign-in recreated a released or deleted account, or one another account
  registered under the derived name, with default settings and an admin grant, its data
  included. Now a person with an active account in the org is signed in to it, one with an
  account revoked outright (`loom revoke-token --subject`) is refused, and anyone else
  gets the next free name (`<name>-2`, ..., then a random suffix). A first sign-in whose
  owner grant failed to land is finished at the next sign-in, from a new `oidc-jit` tag on
  the account; a grant revoked or lowered since is never given back.
- **Security:** self-signup refuses an email a deleted account still holds, as it refuses
  a taken name. Signing up and deleting again and again tied any number of accounts to
  someone else's address. `loom claim-release` frees the email; the name stays reserved.
- **Security:** `namespace.restore` into an existing namespace needs an admin of it. The
  owner a first write auto-registered, who holds no grant there, could restore a payload
  whose grants made them, or anyone, admin.
- **Security:** a second `namespace.join` request on an approval namespace no longer turns
  the first, still pending, request into an active grant; it replaces the request.
- `loom code scip-full` moves its checkout and runs its harness (the repository's CMake)
  as repository jobs, in the sandbox like the delta worker, instead of as plain processes.
  The checkout fetch now names the bare clone and the submodule update gets the network,
  without which neither worked under the sandbox. The last MiB of the harness output is
  printed when it ends, and a harness past its 6 h bound (inside the cron's 8 h) is
  reported as a timeout, exit 124.
- `LOOM_CODE_SANDBOX_MAX_PIDS` bounds each repository job: it is set inside the job's
  user namespace, so on Linux 5.14 and later it no longer counts every task of the
  server's uid on the host.
- Where an AppArmor profile refuses the sysctl write of bwrap's `--disable-userns`, the
  sandbox probe retries without the flag instead of reporting the sandbox unavailable;
  Loom's seccomp filter still refuses jobs new user namespaces.

## [1.0.0] - 2026-10-16

First public release.
