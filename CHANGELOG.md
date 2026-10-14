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
  namespaces a `namespace_scope=all_mine` search reads besides the addressed one.
  For anyone but a scoped token the cut drops public namespaces before private ones.
  `memory.search` names the rest in a new output field, `namespaces_skipped`, and the
  `loom_search` text lists them. `knowledge.search` and `conversation.search` carry the
  same field, and every page of a paged `memory.search` repeats the first call's lists.

### Changed

- **Breaking:** `rerank` (`memory.search`, `memory.search_trace`, `code.search`, `code.ask`)
  and a namespace's `rerank_endpoint` take a name, not a URL: `none`, `builtin:openai[:model]`,
  `builtin:cohere[:model]`, or an endpoint the operator names in `LOOM_RERANK_ENDPOINTS_JSON`.
  A URL or an unknown name is a 422. A namespace that still stores a URL is no longer reranked.
- **Security:** revoking or lowering a grant now ends what the subject's older tokens claim
  on that namespace. A flat or scoped token minted before it reaches the namespace only
  through the subject's current grant, and a grant made later does not restore the claim.
  Signing in again, an OAuth code exchange and an OAuth refresh leave the namespace out of
  the token they mint, until the subject is given an admin grant there after it (only
  another admin gives one, and the claim confers no more). The cutoff is a
  `loom.revoked_tokens` row (`grant:<org>/<namespace>/<subject>`);
  `loom revoke-token --list --grants` shows them. A namespace never granted to the subject
  (named by an operator at mint) keeps its claim.
- **Security:** `/oauth/token` runs one exchange per code or refresh token: a KeeperMap
  claim (`loom.oauth_claims`, migration `027_oauth_claims.sql`) tells a concurrent
  presenter to retry, where two could both mint before. When a read the exchange needs fails it
  answers a retryable 503 `temporarily_unavailable` and leaves the credential usable,
  where a failed account read on refresh used to answer 500 and burn the token. The
  exchange is bounded well inside the age its claim is taken over at. Without a
  Keeper, `/v1/health` reports `oauth_claims_not_atomic`.
- **Security:** `namespace.restore` into an existing namespace needs an admin of it. The
  owner a first write auto-registered, who holds no grant there, could restore a payload
  whose grants made them, or anyone, admin.
- **Security:** `context.fetch` no longer serves, and `context.offload` refuses to write,
  `page/` blobs: a parked `memory.search` page holds hits from every namespace its search
  fanned out over, and only `memory.search(page=...)` serves it, re-checking each of them.
- **Security:** a second `namespace.join` request on an approval namespace no longer turns
  the first, still pending, request into an active grant; it replaces the request.
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

- Choosing a new embedding model in **Settings → Models & keys** failed with
  `source_text_missing` on a freshly indexed repository: the rebuild took the zero vector
  that pads a text-less row (a file's structural summary card before its summary is
  written) for a real one, and its verification did too. Such rows now rebuild as
  placeholders.
- The quickstart told released-stack users to add repositories from the dashboard, which
  that stack refuses with `SANDBOX_UNAVAILABLE`, since it runs without the repository-job
  sandbox. It now shows how to index one through `LOOM_CODE_REPOS_JSON`, `loom code init`
  and `loom code backfill`, with no `scip_producer` or working tree to set up. The refusal
  names that route where it works, the `repo_jobs_unsandboxed` fix in `loom doctor` and
  `/v1/health` matches it, and the sandbox docs say which hosts can load the AppArmor
  profile: AppArmor 4.0 or later, tested on Ubuntu 24.04.
- On a host whose time zone is not UTC, a timestamp written through the driver's insert path
  was shifted by the host's offset (seven or eight hours on the US west coast): the driver
  reads a naive datetime as local time. Those writes now carry the UTC instant. Conversation
  threads, which wrote local time to make up for it, now write UTC, so `conversation.create`
  and `conversation.update` answer UTC times, as `conversation.get` does. Other rows written
  before the upgrade keep their shift. The images default to UTC, so a container that sets no
  `TZ` was not affected; a server run on a laptop was. A spend-ledger turn time with an offset
  is now converted to UTC, not truncated.
- A request whose body holds a number JSON cannot carry back (`1e400` parses to infinity) gets
  a 422 that echoes it as the string `"Infinity"`, instead of a 500.
- `admin/budget.set` refuses an integer too large for a float with a 422; it was stored, and
  the server's other web processes then ignored every budget override.
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
