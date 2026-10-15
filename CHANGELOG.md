# Changelog

All notable changes to Loom (the server, its web app and its coding-agent plugins). The
format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[SemVer](https://semver.org/). The SDKs keep their own changelogs in
[sdk/python](./sdk/python/CHANGELOG.md) and [sdk/typescript](./sdk/typescript/CHANGELOG.md).
A release tag `vX.Y.Z` ships only when the top entry reads `## [X.Y.Z] - YYYY-MM-DD`.

## [Unreleased]

### Added

- `loom login`, `loom logout`, `loom whoami` (and `python -m loom …` with only the SDK): sign
  in on any browser with a one-time code, with the password form or SSO, using the OAuth
  device grant. The session (an hour-long access token and a rotating refresh token) lives
  in `~/.loom/credentials`. The sign-in routes (`/oauth/device*`, `/oauth/token`,
  `/oauth/revoke`, `/oauth/callback`, `/oauth/sso`, discovery) are served whenever auth is
  on; `LOOM_OAUTH_ENABLED` still gates MCP connect.
- `loom call <op|/path> key=value …`: every operation and every route outside the registry
  from a shell, with the stored session or `LOOM_API_KEY`
  ([without the dashboard](docs/guides/without-the-dashboard.md)).
- With `LOOM_OAUTH_LOGIN_PROVIDER=oidc` the dashboard signs in through the identity
  provider ([single sign-on](docs/operations/sso.md)), and every refresh first spends the
  provider's refresh token, so a user deactivated there loses their sessions at their next refresh (machine tokens they made are revoked by `loom revoke-token --subject` when offboarding).
- A sign-in session's access tokens carry its family as their `sid` claim: logout, a replayed
  refresh token, code or device code, and a provider refusing the user each revoke the
  whole session, and a replay also the machine tokens minted from it. Every sign-in, approval, denial and ended session writes an `auth.*`
  audit row.
- Migration `028` adds the device-grant and session columns to `oauth_state`.

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

### Changed

- **Breaking, under `LOOM_OAUTH_LOGIN_PROVIDER=oidc`:**
  - Password sign-in is refused everywhere (`/v1/auth/login` and a password POST to
    `/oauth/authorize`).
  - An SSO email no longer matches an account that was created with a password, because
    self-signup never verified that address. Grant the person's SSO account on such a
    namespace instead ([single sign-on](docs/operations/sso.md#accounts)).
  - MCP connect asks the user to approve before it sends a code.
  - Sessions with no provider refresh token stop refreshing, so MCP clients connected
    before this release sign in once more.

- **Breaking:** `rerank` (`memory.search`, `memory.search_trace`, `code.search`, `code.ask`)
  and a namespace's `rerank_endpoint` take a name, not a URL: `none`, `builtin:openai[:model]`,
  `builtin:cohere[:model]`, or an endpoint the operator names in `LOOM_RERANK_ENDPOINTS_JSON`.
  A URL or an unknown name is a 422. A namespace that still stores a URL is no longer reranked.
- **Breaking:** a `LOOM_CODE_REPOS_JSON` entry (or the scalar `LOOM_CODE_REPO`) that names
  no `scip_producer` gets the compiler tiers its tree's build files call for, as a
  dashboard entry does, instead of `clang`. An entry relying on the old default can gain a
  tier (a C++ repository with a root `pyproject.toml` also gets scip-python), lose
  scip-clang (no root `CMakeLists.txt`), or lose the host cron's scip-python pass (no root
  Python build file); add `"scip_producer":"clang"` to keep it as it was.
  The Python, TypeScript and Go tiers now create a missing working tree for these entries
  too; the C/C++ tier still needs the configured build tree.
- Each compiler tier of an entry has its own working tree, so two tiers never move one tree:
  the producer the entry names (C/C++ when it names none) keeps `scip_checkout_dir`, and
  every other tier, extra producers included, uses `<scip_checkout_dir>.<producer>` (it
  was `-<producer>`, which could be another entry's `checkout-<namespace>`). A repository
  added from the dashboard therefore moves its Python, TypeScript or Go tier, and an extra
  producer moves its tier, to a new working tree, filled once from the mirror (a
  TypeScript tier also reinstalls its dependencies there). The old directories are no
  longer used and can be deleted, except a `clang` extra producer's: Loom does not create
  a C/C++ build tree, so move it from `<scip_checkout_dir>-clang` to
  `<scip_checkout_dir>.clang` and delete its `build/compile_commands.json`, which records
  the old absolute paths; the next C/C++ run configures it again with the harness's own
  CMake options. For a build with options of its own, configure it again yourself with
  `cmake --fresh` (CMake 3.24 or later), those options and
  `-DCMAKE_EXPORT_COMPILE_COMMANDS=ON`; `--fresh` drops whatever cache a C/C++ run wrote
  in between. `loom code scip-full` now runs in the C/C++ tier's own tree and moves the
  C/C++ watermark where `clang` is an extra producer. A `LOOM_CODE_REPOS_JSON` entry whose
  namespace contains `.` or `/` is skipped and counted in `loom.code.repos_config_errors`;
  rename it, which indexes it again under the new name.
- The server probes the repository-job sandbox at boot, so `/v1/health` reports
  `repo_jobs_unsandboxed` before any repository job runs. The Repositories page hides
  **Add repository** and **Add branch** when it does, where the server refuses both.
- **Operators:** a release's installer bundle runs the ClickHouse release that was latest
  when it was built (migrated and schema-checked then), so rerunning the installer keeps it
  and a newer Loom release moves it; the repository's compose files and CI follow
  `clickhouse/clickhouse-server:latest`. On 26.8 or later a fresh install creates its vector
  columns coded (`Quantized('rabitq', dim, 0)`) when the width is a multiple of 8; an install
  whose vector tables are plain stays plain until `scripts/ops/vector_codec_rebuild.py` moves
  it. `LOOM_CLICKHOUSE_IMAGE` overrides the image (a release, or
  `clickhouse/clickhouse-server:head`, unsupported).
- **Operators:** with `LOOM_WEB_WORKERS` unset, the image starts one web process per CPU the
  container may use, at most one per GiB of memory and at most 8, instead of one. Set it to
  keep the old behaviour or to leave cores for ClickHouse on a shared host. Several
  processes share files in a private runtime directory (`/run/loom`, which the compose files
  mount as a tmpfs); with none, as on a read-only root without one, the image starts one
  process and says so, and an explicit `LOOM_WEB_WORKERS` above 1 refuses to start, as does a
  pinned `LOOM_RUNTIME_DIR` the server cannot use or that others can enter (a mode looser than
  0700). With `docker run`, mount `--tmpfs /run/loom:uid=999,gid=999,mode=0700`. The
  repository's cloud and blue/green compose files keep their web tier at one process unless
  `.env` sets `LOOM_WEB_WORKERS`, and `scripts/serve.sh` takes a count that only `.env` names.
- **Operators:** the image owns `/run/loom` (uid 999) and the compose files mount it as a
  tmpfs. Runtime files under the earlier names (`loom-ratelimit-<pid>.buckets`,
  `loom-graph-marks-<pid>/`) are not removed automatically; delete them once the old server is
  gone.
- With `LOOM_EMBEDDING_DIM` unset, a `local:*` embedding model sets the width itself (384 for
  `bge-small`).
- `loom reembed` refuses a width the coded vector tables do not take; change a model of another
  width in Settings → Models & keys, which rebuilds the tables.
- An MCP session's implicit run is shared by every web process of the server, filed in the
  caller's default namespace under the user and session the call acts as. Its id binds the
  token's subject and is no longer `mcp-<Mcp-Session-Id>`, and its goal is the label
  `mcp session (<agent>)`, not the first call's query, which may reach namespaces the run's
  readers cannot; a `run.begin` naming an id of its shape (`mcp-` and 40 hex digits) is
  answered with an id of its own. Its close no longer writes `metadata.calls`: count the
  `agent_events` rows with its `run_id`. The new `mcp_session_run_sweep` worker closes an idle
  one from the ledger, so it closes after a restart too, and so do the implicit runs an earlier
  server left open.
  **Operators:** a web process no longer closes idle runs itself, so a deployment that runs no
  background workers anywhere (`LOOM_BACKGROUND_WORKERS=off` on every process) keeps them open.
  The first sweeps after the upgrade close every implicit run an earlier server left open. A
  closed implicit run keeps its session's memories whatever `LOOM_MEMORY_RUN_COMPLETE_RETAIN`
  says: the transport cannot judge the work, so a session ending no longer demotes or expires
  what it wrote.
- **Operators:** `LOOM_MCP_SESSION_RUN_IDLE_SECONDS` must be at least 60, the interval at which
  a web process re-checks a session's run; a lower value fails validation at boot.
- `DELETE /mcp` draws the rate limiter like a `run.complete` call, per caller, and past the cap
  answers `429 RATE_LIMITED` with `Retry-After`.

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
- A server with several web processes no longer exits when a boot migration holds the
  provider configuration lock for more than 5 seconds: a booting process now waits for it as
  long as for the migration lease (`LOOM_BOOT_MIGRATE_LEASE_WAIT_S`, 900 s) and logs who
  holds it, instead of failing its start (uvicorn then stopped every process).
- The blue/green proxy re-resolves the serving colour's name, so a colour container that
  restarts on a new address is reached again within about 30 s, instead of timing out until
  the next reload. `redeploy.sh` no longer reloads the proxy after a failed `nginx -t`.
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
