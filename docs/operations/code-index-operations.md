# Code index operations

The code index runs only when an operator enables its workers and configures
repositories. Every `code_*` worker except `code_coverage_notify` and
`code_backport_gaps` is off by default. What the `code.*` operations answer is in the
[code index overview](../features/code-index/overview.md); how the index is built is in
[code index architecture](../architecture/code-index-architecture.md).

One code namespace indexes one branch of one repository. There are two ways to add one:

| Way             | How                                         | Tiers                  | Page                                                   |
| --------------- | ------------------------------------------- | ---------------------- | ------------------------------------------------------ |
| Self-service    | dashboard **Repositories → Add repository** | parser (`code_ingest`) | [repositories](../features/code-index/repositories.md) |
| Operator config | `LOOM_CODE_REPOS_JSON`                      | parser + compiler      | this page                                              |

An operator entry wins over a self-service one with the same namespace.

Run `loom code` inside the container that runs the workers (`loom-workers` in
`dev/docker-compose.cloud.yml`, `loom-server` on single-container stacks), so it uses the
deployment's `.env`, git mirrors and ClickHouse:

```bash
docker exec loom-workers loom code stats --namespace code-myrepo
```

`--org` / `--namespace` default to `LOOM_CODE_ORG` / `LOOM_CODE_NAMESPACE`. Pass both
when the deployment indexes more than one repository.

## Prerequisites

- **The image.** It includes the parser grammars (the `code` extra), Go with scip-go,
  Node with scip-typescript and scip-python, and npm, pnpm, Yarn and Bun for
  automatic TypeScript dependency preparation. Docker users do not install these
  on the host. ClickHouse is the only required running service.
- **Native server toolchains.** Install Python 3.12+, Git and Node.js 22+ with npm,
  then run these commands from the Loom checkout before starting the server:

  ```bash
  pip install -e ".[code]"
  sh scripts/install-code-toolchains.sh
  export PATH="$PWD/var/code-toolchains/bin:$PATH"
  ```

  The script installs pinned TypeScript/Python indexers and package managers without
  requiring root. Keep that `PATH` in the environment of the Loom server, including
  its service manager. Native Go indexing additionally needs Go and scip-go:
  `go install github.com/scip-code/scip-go/cmd/scip-go@v0.2.7` (put Go's bin directory
  on the server's `PATH`). C/C++ needs scip-clang and the project's build setup.

- **An embedder.** Chunks are embedded with the server's `LOOM_EMBEDDING_MODEL`.
  `loom code backfill --embed` refuses the fake embedder unless you pass
  `--allow-fake-embedder` (development only).
- **Writable git mirrors.** The workers run as uid/gid 999 and fetch into the bare clones
  under `var/code_repos` (bind-mounted into the containers). Prepare the host directory
  once so both the host operator and uid 999 can write:

  ```bash
  sudo chgrp -R 999 var/code_repos && sudo chmod -R g+rwX var/code_repos
  sudo find var/code_repos -type d -exec chmod g+s {} +
  ```

  After `loom code init` creates a clone, run
  `git --git-dir var/code_repos/<name>.git config core.sharedRepository group`.

- **A GitHub token** (`LOOM_CODE_GITHUB_TOKEN`, or `github_token` per entry) for
  merged-PR history and for private remotes.

## Configure the entry

`LOOM_CODE_REPOS_JSON` is a JSON array with one object per indexed branch. Fields an
entry leaves out fall back to the scalar `LOOM_CODE_*` settings; an entry for a
repository other than the scalar `LOOM_CODE_REPO` derives its `git_remote`
(`https://github.com/<owner>/<name>.git`) and `git_dir` (`<name>.git` beside the scalar
clone, or under `var/code_repos` when there is no scalar repository) from `repo`.

```bash
LOOM_CODE_REPOS_JSON='[
  {"org": "acme", "namespace": "code-myrepo", "repo": "acme/myrepo",
   "git_branch": "main", "public": true},
  {"org": "acme", "namespace": "code-myrepo-release-2", "repo": "acme/myrepo",
   "git_branch": "release-2", "embed_reuse_from": "code-myrepo"}
]'
```

| Field                                | Meaning                                                                                                                       |
| ------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------- |
| `namespace`, `org`                   | where the index lives                                                                                                         |
| `repo`                               | `owner/name` on GitHub                                                                                                        |
| `git_dir`, `git_remote`              | the bare clone and its remote; branches of one repository share one clone                                                     |
| `git_branch`                         | the branch this namespace tracks                                                                                              |
| `path_prefixes`                      | comma-separated prefixes to index; an entry without it indexes the whole tree minus the built-in denylist                     |
| `exclude`                            | globs never indexed (list or comma-separated)                                                                                 |
| `exclude_generated`                  | refuse files whose content marks them generated (`Code generated … DO NOT EDIT.`, `@generated`)                               |
| `include_contrib_paths`              | vendored `contrib/` paths to index anyway, so their definitions bind the repository's call sites                              |
| `depends_on`                         | other code namespaces this code calls into; a definition missing here is looked up there                                      |
| `embed_reuse_from`                   | a namespace whose embeddings are reused when the chunk text is identical (a release branch of the same repository)            |
| `public`                             | the repository is public on its host: anyone in the org can join its index as a reader at once                                |
| `classification`                     | `restricted` for a private repository: a maintainer approves each reader, and it is never org-readable                        |
| `scip_producer`, `scip_checkout_dir` | the compiler-tier indexer (`clang`, `typescript`, `python`, `go`) and the working tree it runs in                             |
| `scip_extra_producers`               | further compiler-tier producers over the same tree (a Python repository with a TypeScript frontend), each in its own checkout |
| `git_extra_branches`                 | globs of other remote branches the shared mirror should carry                                                                 |
| `github_token`                       | per-entry override of `LOOM_CODE_GITHUB_TOKEN`                                                                                |

The repository's `.gitattributes` `linguist-generated` / `linguist-vendored` patterns and
a `.loomignore` file at the indexed commit are honoured as excludes. A dependency
vendored as a git submodule is not in the bare mirror; give it its own entry and name it
in `depends_on`. The gauge `loom.code.repos_config_errors` reports entries the server
could not use (invalid JSON, a nameless entry, a duplicate namespace); 0 means the value
parsed clean. Validate the JSON before a restart with
`docker compose --env-file .env -f dev/docker-compose.cloud.yml config`.

Who can read an index is governed by the namespace's join policy and grants
([tokens and grants](../guides/tokens-and-grants.md)). `LOOM_CODE_ORG_READABLE=true`
makes every code namespace readable by every principal in the org without a grant; the
default is off.

## First index

```bash
docker exec loom-workers loom code init     --namespace code-myrepo --org acme   # bare clone + index state
docker exec loom-workers loom code backfill --namespace code-myrepo --org acme --embed
docker exec loom-workers loom code backfill-prs --namespace code-myrepo --org acme --months 12
```

- `backfill` parses, chunks, extracts symbols and edges, and embeds each file. It is
  resumable: re-run it after an interruption. `--phase index` then `--phase embed` splits
  the work, since the unembedded rows are the embed phase's queue. `--commit <sha>` pins
  the tree (and implies `--no-fetch`); `--concurrency N` parallelises files; `--limit N`
  indexes a sample.
- The first full backfill of a namespace also re-derives every file's edges after the
  name table is complete, so cross-file references bind (`--no-edges-second-pass` skips
  it). `--dialect-folder-pass` lets SQL files of undecided dialect take the dialect their
  directory agrees on.
- `backfill-prs` loads merged-PR history from GitHub, resumable by cursor and bounded by
  GitHub's rate limit (`LOOM_CODE_PR_BACKFILL_MONTHS`, default 12). `backfill-reviews`
  adds inline review comments, `backfill-assignees` requested reviewers, and
  `backfill-items` issues and PRs in any state.
- **The worker does not start a first index for a configured entry.** An entry without a
  `git_head` watermark is skipped with the note "run `loom code backfill` first". (It
  does build self-service first indexes, `LOOM_CODE_BOOTSTRAP_FILES_PER_PASS` files per
  pass.)

Then:

- `POST /v1/code.calibrate` (`ADMIN`) derives the code index's abstention thresholds from
  `bench/fixtures/code_calibration_probes.jsonl` (shipped in the image).
- `loom code describe --top-pct 20` (optional, LLM cost) writes one-line descriptions for
  the most central chunks.
- `loom code stats` prints the watermarks (`git_head`, `scip_commit`, …) and live row
  counts; `POST /v1/code.index_status` answers the same from the API.

`scripts/ops/code_intel_bootstrap.sh` runs this sequence for the configured
`LOOM_CODE_ORG` / `LOOM_CODE_NAMESPACE` on the Loom host (init, both backfill phases, PR
history, calibrate, then the `bench/code_intel_qa.py` gates), refusing a fake embedder
or a server whose `/v1/health` does not answer; `--dry-run` runs only that read-only
preflight and prints the commands. It reads `LOOM_BASE_URL` and `LOOM_ADMIN_TOKEN`.

## Steady state

Enable the workers in `.env` on the workers container and recreate it. Every worker's
cadence setting and default is in the [workers reference](../reference/workers.md).

| Worker                 | Flag                                     | Does                                                                                                                                                                                                                                                                                                                                                                                                 |
| ---------------------- | ---------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `code_ingest`          | `LOOM_CODE_INGEST_ENABLED`               | every `LOOM_CODE_INGEST_INTERVAL_SECONDS`: fetch, then per first-parent commit re-chunk only changed content, refresh symbols and edges, tombstone deletions, build the merged PR's context pack, advance `git_head`. Hourly it also finds indexed files whose chunks predate their content at head and heals up to `LOOM_CODE_RECONCILE_HEAL_PER_PASS` per pass. Builds self-service first indexes. |
| `code_embed_recovery`  | `LOOM_CODE_EMBED_RECOVERY_ENABLED`       | embeds chunks left without a vector (provider outage, `--phase index`)                                                                                                                                                                                                                                                                                                                               |
| `code_pr_history`      | `LOOM_CODE_PR_HISTORY_ENABLED`           | syncs newly merged PRs from GitHub                                                                                                                                                                                                                                                                                                                                                                   |
| `code_graph_stats`     | `LOOM_CODE_GRAPH_STATS_ENABLED`          | weekly: PageRank over the include graph, per-file degrees, the test ↔ source map                                                                                                                                                                                                                                                                                                                     |
| `code_summary`         | `LOOM_CODE_SUMMARY_ENABLED`              | weekly: LLM file and module cards for `code.map` (cost-capped per namespace)                                                                                                                                                                                                                                                                                                                         |
| `code_scip_apply`      | `LOOM_CODE_SCIP_APPLY_ENABLED`           | applies compiler-tier deltas uploaded through `code.scip_ingest`                                                                                                                                                                                                                                                                                                                                     |
| `code_coverage_notify` | `LOOM_CODE_COVERAGE_NOTIFY_ENABLED` (on) | a task event when a watched commit's coverage is ready in every tier                                                                                                                                                                                                                                                                                                                                 |
| `code_backport_gaps`   | `LOOM_CODE_BACKPORT_GAPS_ENABLED` (on)   | the nightly backport report; runs only with `code_pr_history`                                                                                                                                                                                                                                                                                                                                        |

The per-language compiler-tier workers are in the next section.

**Push-triggered refresh.** A CI job can call `POST /v1/code.refresh` (`WRITER`) right
after a merge. It runs the same ingest pass immediately for that namespace, coalesces
concurrent triggers, and answers 404 for a namespace no entry configures, so the pusher
never decides what gets indexed. It works with the poller disabled.

Open pull requests are not indexed; the working tree stays the client's own.

## Compiler tier

The parser tier answers from tree-sitter. Where a compiler-grade (SCIP) index exists, its
symbols and references win. Each language has a worker that indexes the commits between
its own watermark and the branch head in the entry's mirror (fetched by `code_ingest`)
and applies the delta in-process; the C/C++ tier can instead run from the
[host cron](#host-cron). A compiler tier does not wait for the parser tier's `git_head`;
only an entry's first index does.

| Language   | `scip_producer` | Worker flag                       | Host needs                                                                                                           |
| ---------- | --------------- | --------------------------------- | -------------------------------------------------------------------------------------------------------------------- |
| C / C++    | `clang`         | `LOOM_CODE_SCIP_DELTA_ENABLED`    | a configured build tree at `LOOM_CODE_SCIP_CHECKOUT_DIR` (CMake configure done once) and `LOOM_CODE_SCIP_CLANG_PATH` |
| TypeScript | `typescript`    | `LOOM_CODE_SCIP_TS_DELTA_ENABLED` | Node and the pinned indexer/package managers (included in the image); dependencies are prepared automatically        |
| Python     | `python`        | `LOOM_CODE_SCIP_PY_DELTA_ENABLED` | scip-python (in the image)                                                                                           |
| Go         | `go`            | `LOOM_CODE_SCIP_GO_DELTA_ENABLED` | Go and scip-go (in the image)                                                                                        |

All four workers are enabled by default. The Docker image includes the Python,
TypeScript, and Go indexers and creates persistent compiler checkouts for repositories
added from the dashboard. A worker whose project prerequisites are absent records a
skip in `workers.status`; the Tree-sitter tier remains available. TypeScript projects
have their dependencies installed automatically in their checkout. C/C++ projects need a
compilation database and scip-clang (upstream publishes Linux binaries only for
x86_64).

- **C/C++ delta cap.** The worker indexes changed translation units, including those
  that include a changed header, up to `LOOM_CODE_SCIP_DELTA_MAX_TUS` (200); above the
  cap it indexes only the changed `.cpp` files. `loom code scip-full` re-indexes
  everything and applies it (`--keep-delta` saves the delta so an interrupted apply can
  be re-run with `--apply-delta`); schedule it weekly to heal capped passes and seed
  entries the delta reports as "watermarks not seeded". It runs in the CLI process, not
  the server. `loom code scip-delta` runs one delta pass for a host cron instead of the
  worker and prints a `RESULT:` line.
- **Entry locks.** A full pass and a delta never work on the same entry at once: each
  takes the entry's lock file in `LOOM_CODE_SCIP_LOCK_DIR` (`var/cron/locks`),
  `scip.<org>.<namespace>.lock` with both names percent-encoded
  (`loom code scip-lock-path --org ORG --namespace NS` prints it). A full pass waits for
  a delta already on its entry; a delta skips an entry a full pass holds and names it
  under `skipped` ("entry lock held (full pass running)"). Different entries run side by
  side. Shell callers lock the same file with `flock(1)`.
- **TypeScript dependencies are automatic.** Fresh checkouts install dependencies
  before compiler indexing, including development dependencies containing type
  definitions. Loom uses npm, pnpm, Yarn (classic or modern) or Bun according to the
  lockfile, enforces existing lockfiles and disables lifecycle scripts and executable
  package-manager hooks. Plain scripts without a package manifest need no install.
  Dependency manifest/lockfile changes trigger a refresh before the next index; a failed
  or interrupted install is retried without advancing the compiler watermark.
  Downloads need network access to the repository's package registry. A private package
  registry, incompatible lockfile or generated types requiring a build can still need
  project-specific configuration; the worker reports the exact failure in
  `workers.status`. Server credentials are not passed into the installer environment.
- **A failed apply** leaves its batch `failed` with the error;
  `loom code scip-retry <batch_id>` resets it to pending.
- **Manual and CI producers** live in `scripts/scip_harness/` (`run_delta.sh`,
  `run_typescript_delta.sh`, `run_python_delta.sh`, `run_go_monorepo_delta.sh`,
  `scip_to_delta.py`; see its README).

### Repository-job sandbox

**Build-based tiers execute repository code.** The C/C++ tier runs the repository's own
`cmake` configure (a `CMakeLists.txt` is a program), the Go tier runs `go mod download`
over the repository's module graph, and the TypeScript tier installs the packages its
lockfile names. Treat every compiler-tier run as running code the repository's authors
wrote, on the host that runs it.

Loom runs every repository job (clone, fetch and `ls-remote`, `git submodule update`,
dependency installs and the compiler-tier harnesses) inside one boundary built on
[bubblewrap](https://github.com/containers/bubblewrap)
(`server/code/app/index/sandbox.py`) when the host can run it:

- its own user, pid, network, mount, ipc and uts namespaces, no capabilities, no new user
  namespaces, and a seccomp filter that refuses `ptrace`, `mount`, `unshare`, `setns`,
  a `clone` with `CLONE_NEWUSER`, `bpf`, `perf_event_open`, kernel keyrings, module and
  kexec loading, `userfaultfd`, `open_by_handle_at` and `process_vm_readv`/`writev`
  (`clone3` answers `ENOSYS`, so the C library falls back to `clone`);
- only system directories, the toolchains and the job's own paths are mounted. A
  toolchain is mounted only where it exposes none of Loom's data: never a directory
  that is or holds `var/`, the mirrors and checkouts, the blob store, the env file, the
  install root or `HOME`, nor one inside `var/` or under `HOME` (the default
  `var/bin/scip-clang` is mounted as that one file). Where one of those paths lies
  inside any other mounted directory (the blob store inside the server's Python
  prefix, `var/` under a system path), an empty mount covers it inside the sandbox.
  The install root is the exception, since the server's Python may run from it. A host
  whose Python prefix or a system path is, or sits inside, one of Loom's data paths
  runs no sandboxed job: no empty mount could hide that data, and `/v1/health` and
  `loom doctor` report `repo_sandbox_layout_refused`. The checkout (or the
  bare mirror, for a fetch) and the output directory are writable, the checkout's
  `.git` and the harness scripts are read-only, and `/tmp` and `HOME` are a fresh
  directory removed after the job. No server environment, credentials, host `HOME`,
  Docker socket or other host paths;
- rlimits, set by `prlimit` before bwrap starts: `LOOM_CODE_SANDBOX_CPU_SECONDS` (7200),
  `LOOM_CODE_SANDBOX_MEMORY_MB` (16384) and `LOOM_CODE_SANDBOX_MAX_FILE_MB` (32768)
  bound **each process** of a job (CPU time, data memory, file size), not the job's
  total, and `LOOM_CODE_SANDBOX_MAX_PIDS` (2048) bounds the processes of the server's
  uid, so one job's fork bomb can starve concurrent jobs of processes. Loom sets no
  cgroup; give the container its own memory and pids limits for a hard total. Each job
  also has a wall-clock timeout that kills its whole tree; the job's pid namespace
  guarantees no process it started outlives it;
- bounded output in the server: a job's stderr, and the stdout of a build, install or
  clone, keep only their last 1 MiB; a command whose stdout Loom parses (`ls-remote`,
  fetch) may print up to 64 MiB, and past that the whole job is killed and fails;
- no network, except for clones, fetches, submodules and dependency downloads, and then
  only through an egress proxy outside the sandbox. It accepts `CONNECT` to ports 443,
  22 and the remote's own port, resolves the name, refuses it unless **every** address
  is public (loopback, private ranges, link-local and the cloud metadata address are
  refused, including when reached through a redirect or a `.gitmodules` URL) and
  connects to the address it vetted. Plain HTTP has no route. One job holds at most 32
  tunnels at a time, and a tunnel idle for 5 minutes is closed.

Every git a repository job runs reads no system or global git config and allows only
the `https` and `ssh` transports (`protocol.allow=never`); `ext::` and `file://` are
refused, so are URL rewrites. The entry's access token reaches only its clone and
fetches, never a build or `go mod download`. Inside the sandbox an `ssh` remote has no
host SSH key: use an HTTPS remote with an access token.

**Which stacks run the sandbox.** The blue/green colours `redeploy.sh` deploys
(`dev/docker-compose.colour.yml`) set the container options below. The other Compose
files (`dev/docker-compose.cloud.yml`, `dev/docker-compose.dev.yml`,
`dev/docker-compose.read-replica.yml`, `deploy/standalone/compose.yaml`) do not: an
AppArmor profile the host has not loaded stops a container from starting, so they keep
Docker's defaults, the probe fails and every repository job runs as a plain process (see
_Without the sandbox_). Copy the colour file's `security_opt` block into them once the
host has the profile. A source run (`make server`) has the sandbox only where the host
passes the same requirements.

**Requirements.** `bwrap` and `prlimit` (util-linux) on `PATH` (the image has both) and
unprivileged user namespaces. Docker's defaults block bwrap three ways, and the colour
compose relaxes each as narrowly as it can:

```yaml
security_opt:
  - seccomp=./seccomp/loom-repo-sandbox.json
  - apparmor=loom-repo-sandbox
  - systempaths=unconfined
```

- **seccomp.** Docker's default profile allows `clone` with namespace flags, `unshare`,
  `mount`, `umount2` and `pivot_root` only to a container with `CAP_SYS_ADMIN`.
  `dev/seccomp/loom-repo-sandbox.json` is Docker's default profile (moby/profiles
  `seccomp/v0.2.3`, the one Docker 29 ships) plus one rule allowing those five; bwrap
  needs all of them. Inside the sandbox, Loom's own seccomp filter refuses them again.
- **AppArmor.** `docker-default` has `deny mount`. `apparmor=unconfined` does not help
  on hosts that restrict unprivileged user namespaces (Ubuntu 23.10 and later,
  `kernel.apparmor_restrict_unprivileged_userns=1`): there an unconfined process that
  creates a user namespace is moved into the `unprivileged_userns` profile, which
  refuses the mounts. `deploy/apparmor/loom-repo-sandbox` is `docker-default` with
  `userns, mount, pivot_root` in place of `deny mount` (the container still has no
  `CAP_SYS_ADMIN`, so a mount works only inside the namespace bwrap creates). Load it
  on the Docker host once; `/etc/apparmor.d` is loaded again at every boot:

  ```bash
  sudo install -m644 deploy/apparmor/loom-repo-sandbox /etc/apparmor.d/loom-repo-sandbox
  sudo apparmor_parser -r -W /etc/apparmor.d/loom-repo-sandbox
  ```

  `redeploy.sh` stops before building a colour when the profile is not loaded. On a
  host without AppArmor, Docker ignores the option.

- **`/proc`.** Docker masks paths under `/proc`, and the kernel then refuses the fresh
  procfs mount the sandbox makes; `systempaths=unconfined` unmasks them.

These options still weaken the container's own confinement a little (mounts and user
namespaces become reachable to code in the container); weigh them against running
repository code without the sandbox. `seccomp=unconfined` and `apparmor=unconfined`
also work where the host does not restrict user namespaces, but they drop the whole
profile.

**Without the sandbox** Loom probes once per process, logs a warning and reports
`repo_jobs_unsandboxed` in `/v1/health` and `loom doctor`. Then:

- operator repositories (`LOOM_CODE_REPOS_JSON`) keep every tier, and their build steps
  run as plain processes on the host, as before;
- self-registered repositories whose remote is a network URL are not cloned or fetched
  and get no compiler tier: without the egress proxy nothing would vet the hosts and
  ports a job connects to. The ingest pass note and `workers.status` name the skip; the
  entry resumes once the sandbox is available;
- self-registered repositories on a local path keep the parser tier and the static
  compiler tiers but get no build-based tier (C/C++);
- adding a repository or a branch from the page is refused with `SANDBOX_UNAVAILABLE`.

Each Go entry has its own module cache under `LOOM_CODE_SCIP_GO_MODCACHE`
(`<root>/<org>/<namespace>`): `go` does not re-check the sums of modules already in its
cache, so a cache one repository's job wrote is never another's.

### Host cron

`deploy/cron/scip-delta.sh` and `deploy/cron/scip-full.sh` are the host-side schedules.
They run on the Loom host from the loom root with its `.venv`
(`.venv/bin/python -m server.cli code …`), not in a container.

| Script          | Schedule             | Runs                                                                                           | Step timeouts (env)                                                                       |
| --------------- | -------------------- | ---------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------- |
| `scip-delta.sh` | every 30 minutes     | the C++ delta for every entry, then per entry the SQL/shell → C++ crossref and the Python tier | `SCIP_DELTA_CPP_TIMEOUT` 3h, `SCIP_DELTA_CROSSREF_TIMEOUT` 1h, `SCIP_DELTA_PY_TIMEOUT` 2h |
| `scip-full.sh`  | weekly, under `nice` | `loom code scip-full` for every entry, one at a time                                           | `SCIP_FULL_ENTRY_TIMEOUT` 8h                                                              |

- Each script holds its own pass lock (`scip-delta.lock`, `scip-full.lock`); the entry
  locks keep the two apart, so a long full pass holds back only its current entry.
- `scip-delta.sh` reads a writer token for the Python step from
  `var/cron/loom_writer_token.json`; without it that step logs `reason=no-writer-token`.
  `scip-full.sh` needs no token.
- Logs go to `var/cron/scip-delta.log` and `var/cron/scip-full.log`. A timed-out step
  exits 124 and frees its lock. Every step ends with one line:

```text
STEP <step> ns=<namespace> exit=<code> seconds=<n> [reason=<why>]
```

`<step>` is `scip-delta`, `crossref`, `python-scip`, `entry-lock` or `scip-full`;
`grep '^STEP' var/cron/scip-delta.log` lists the outcomes. Both scripts list their
entries with `loom code entries` (org, namespace, checkout, lock file), so they name no
server module. Install from the loom root:

```bash
install -m 755 deploy/cron/scip-delta.sh deploy/cron/scip-full.sh deploy/cron/repo-gc.sh var/cron/
# crontab -e
*/30 * * * * <loom root>/var/cron/scip-delta.sh
0 3 * * 0    <loom root>/var/cron/scip-full.sh
0 4 * * 6    <loom root>/var/cron/repo-gc.sh
```

`repo-gc.sh` owns git housekeeping for `var/code_repos`: it turns git's auto-gc off in every
repository (a repack of a large checkout writes a temporary pack as big as the repository,
which can fill the host disk), then repacks each one at idle priority when the disk has room
for it (its git directory's size plus a 50 GB margin), under the entry lock for a scip
checkout. `grep '^STEP' var/cron/repo-gc.log` lists what it did; `reason=low-disk` means a repository was left unpacked this week. The
`loom.host.disk.free_bytes` / `loom.host.disk.total_bytes` gauges are the disk alarm's input.

## Measured coverage

A repository whose CI runs an instrumented build with per-test line coverage can feed the
test map with what each test executed, not only what it names. The import reads the
newest complete run from the ClickHouse server that holds the CI's `checks_coverage_lines`
and `checks` tables, in `LOOM_CODE_COVERAGE_SHARDS` slices by file hash. One
slice must fit that server's per-query memory. A server with an hourly quota per statement
shape must also take a whole import (two statements per slice) plus a retry. A public
playground allows 100 statements per shape per hour; the default 16 slices use 16 of each,
so a dry run and a real run fit in the same hour.

It writes:

- one `code_test_map` row per (source file, test), `method = coverage`, when the test
  executed a line at most `LOOM_CODE_COVERAGE_SPECIFIC_MAX_TESTS` tests reach (a line every
  query runs names no test); `confidence` falls with how many tests share the pair's most
  specific line, and `evidence` holds the run's commit and the specific line ranges;
- one `code_coverage_files` row per source file with every range any test executed, so
  `code.coverage_claim` can tell "executed by many tests" from "executed by none".

Rows the newest run no longer has are tombstoned, and only after every slice was read.
`code.coverage_claim` ranks a measured row as `executed`, above every other tier, when its
ranges meet the subject's span; a subject whose file the run covered but whose lines no
test executed is `uncovered`, with the measurement in `measured`.

```bash
LOOM_CODE_COVERAGE_SOURCE_URL=https://<coverage server>
LOOM_CODE_COVERAGE_SOURCE_USER=<reader>
LOOM_CODE_COVERAGE_NAMESPACES=<namespace>[,<namespace>]
loom code coverage-import --namespace <namespace> --dry-run   # counts only, writes nothing
loom code coverage-import --namespace <namespace>
LOOM_CODE_COVERAGE_IMPORT_ENABLED=true                         # then daily, in the workers
```

## Another branch

A namespace follows one branch, and a branch is indexed only when an entry names it. Add
an entry with the same `repo` and a different `namespace` and `git_branch`; it shares the
bare clone. Set `embed_reuse_from` to the main branch's namespace so identical chunks
reuse embeddings. Then run `loom code init` and `loom code backfill` for the new
namespace, as for the first index.

## Repairs

`loom code reconcile --namespace NS` lists indexed paths whose chunks predate their last
change at the branch head; `--heal` re-ingests them without moving the watermark.

**After an extractor change.** The code of the modules in `EXTRACTOR_MODULES`
(`server/code/domain/extract/extractor_stamp.py`) and the grammar versions form the
index's version stamp. The stamp hashes code tokens only; comment and docstring edits do
not move it. When a deploy moves the stamp, `code_ingest` re-derives the live files of
every namespace in the language families the change touches,
`LOOM_CODE_EXTRACTOR_RECHUNK_PER_PASS` files per pass, and runs only the stages (chunk,
symbol, edge, guard) whose stamp moved. `code.index_status` shows progress in its
`extractor_stamp*` state keys. On a large index this takes hours.

**After a change outside the stamp** (a language lane, a facet, a new file type),
existing rows are not healed automatically. Re-ingest the affected paths at the
namespace's own watermark:

```bash
HEAD=$(docker exec loom-workers loom code stats --namespace "$NS" | awk '$1 == "git_head" {print $2}')
git --git-dir var/code_repos/<name>.git ls-tree -r --name-only "$HEAD" | grep -E '\.(sql|sh)$' > /tmp/$NS-paths.txt
docker cp /tmp/$NS-paths.txt loom-workers:/tmp/
docker exec loom-workers loom code backfill --namespace "$NS" --commit "$HEAD" --no-fetch --embed \
  --paths-file /tmp/$NS-paths.txt
```

`--paths-file` is repair mode: it re-ingests only the listed paths and leaves the
watermark alone. Files whose content is unchanged keep their chunks and embeddings;
symbols, facets and edges are re-derived. Add `--edges-second-pass` when many files gained
symbols at once.

The optional lanes are switched by settings; after turning one on, run the repair over
the matching paths:

| Setting                           | Default | Lane                                                                                |
| --------------------------------- | ------- | ----------------------------------------------------------------------------------- |
| `LOOM_CODE_SQL_STATEMENT_RECORDS` | off     | `.sql` files as one `statement` symbol per statement, with dialect and table facets |
| `LOOM_CODE_SHELL_ISLANDS`         | off     | SQL issued from shell scripts as statement records                                  |
| `LOOM_CODE_PY_SQL_ISLANDS`        | off     | SQL strings in Python calls as statement records                                    |
| `LOOM_CODE_HOST_ISLANDS`          | off     | CI workflows, Dockerfiles and Makefiles: jobs, stages and targets as symbols        |

`scripts/ops/code_edge_audit.py` is a read-only census of the parser-tier call graph's
two silent holes (declined call sites whose callee now has a definition, and low-grade
edges whose target is gone); run it with `LOOM_BACKGROUND_WORKERS=off` against a staging
copy.

## Maintenance commands

| Command                                      | Does                                                                                                                                                                                                                                                                                                                    |
| -------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `loom code compact [--table T] [--apply]`    | measures, and with `--apply` runs, `OPTIMIZE … FINAL` on a code vector table once its physical copies per key reach `LOOM_CODE_COMPACT_MIN_RATIO` (1.5), and waits for the merge; bulk passes do it themselves after `LOOM_CODE_COMPACT_AFTER_BULK_ROWS` rows ([maintenance](./maintenance.md#compaction-and-optimize)) |
| `loom code warm <questions.txt>`             | asks each question once so `code.ask` answers are cached for everyone (text lines or JSONL with `question`); hits are free, so re-running is safe                                                                                                                                                                       |
| `loom code answers-purge`                    | retires cached `code.ask` answers after a synthesis-model change (the cache otherwise invalidates only when a cited file changes); dry run until `--apply`                                                                                                                                                              |
| `loom code repack`                           | rebuilds context packs for recently merged PRs in the current shape                                                                                                                                                                                                                                                     |
| `loom code crossref`                         | the SQL/shell test → implementing C++ reference pass (idempotent)                                                                                                                                                                                                                                                       |
| `loom code coverage-import [--dry-run]`      | imports the newest per-test line coverage run of the configured source into the test map ([measured coverage](#measured-coverage))                                                                                                                                                                                      |
| `loom code reembed`, `loom code vector-twin` | write a namespace's chunks under a second embedding model's label, or fill a Matryoshka-prefix twin of the vectors, inside ClickHouse; resumable                                                                                                                                                                        |
| `loom code lint-instructions`                | lists paths and `Qualified::symbol` references in `CLAUDE.md`, `AGENTS.md`, `.claude/**` and skills that no longer resolve at head; `--fail-on-stale` for CI                                                                                                                                                            |
| `loom code export-matrix`                    | writes a "when this file changes, these usually change too" table from merged-PR co-change, for an instructions file                                                                                                                                                                                                    |
| `loom code export-pairs`                     | exports (query → acted-on unit, hard negatives) retrieval pairs from real traffic, for embedder or reranker tuning                                                                                                                                                                                                      |

`make bench-code-intel LOOM_BASE_URL=…` runs the code-index quality gate
(`bench/code_intel_qa.py`) against a server with real providers, for a repository that
has a gold question set; it is never run in CI, whose embedder is fake
([benchmarks](../benchmarks/running.md)).

## Rollout and shutdown

- `LOOM_MCP_CODE_TOOLS` (default off) lists the `code.*` tools on the default MCP
  toolset. The `coding` toolset lists them regardless, and HTTP and the SDKs can always
  call them, so you can evaluate an index before exposing it to chat clients.
- `LOOM_CODE_INTEL_ENABLED=false` makes every `code.*` operation return a precondition
  error. The workers are switched separately.
- **Retire a repository.** A maintainer uses **Retire…** on the repository's Access tab
  (`code.repo.remove` with `confirm` set to the namespace). The entry goes inactive, an
  erasure saga purges every `loom.code_*` table for the namespace, and the bare mirror is
  deleted when no other branch uses it. Search telemetry is kept. An operator-configured
  entry is refused with `REPO_FROM_CONFIG`: remove it from `LOOM_CODE_REPOS_JSON`
  instead.
- **Erase an index.** `erasure.request` with `scope_type = namespace` on a code namespace
  also purges the `loom.code_*` tables. An entry that is still configured is re-indexed
  from scratch on the next pass, so retire it first.

## Failure model

| Failure                                                              | Behaviour                                                                                                                          | Recovery                                                                                                                                         |
| -------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------ |
| `git fetch` fails                                                    | watermark unmoved                                                                                                                  | the next pass (supervisor backoff)                                                                                                               |
| crash mid-commit                                                     | the commit is re-run                                                                                                               | idempotent upserts                                                                                                                               |
| a file fails to parse                                                | file skipped, `code_files.index_error` set                                                                                         | `code.index_errors` lists them; fixed by the next change or a repair                                                                             |
| embedding provider down                                              | chunks stored without vectors; lexical search still serves                                                                         | `code_embed_recovery`                                                                                                                            |
| compiler-tier apply fails                                            | batch `failed` with the error                                                                                                      | `loom code scip-retry <batch_id>`                                                                                                                |
| a compiler-tier step hangs on the host                               | the step's timeout ends it (exit 124) and frees its lock                                                                           | the next cron pass; read the `STEP` lines                                                                                                        |
| compiler-tier window base unknown (history rewritten, pruned mirror) | every `scripts/scip_harness/` tier exits 3, never an empty delta; the Python, TypeScript and Go workers clear the tier's watermark | the next run is a full index for that tier; the C/C++ tier fails the window loudly and its full index is the host cron's (`loom code scip-full`) |
| GitHub rate limit                                                    | the pass ends, cursor unmoved                                                                                                      | the next pass                                                                                                                                    |
| branch force-pushed (watermark no longer an ancestor)                | watermark reset to the new head, logged                                                                                            | `loom code backfill` to re-derive                                                                                                                |
| weekly graph stats fail                                              | last week's ranks keep serving                                                                                                     | the next weekly pass                                                                                                                             |

The code index's gauges (`loom.code.index_lag_commits`, `loom.code.stale_files`,
`loom.code.tier_lag_seconds`, `loom.code.embed_backlog`, `loom.code.files_errored`) are
catalogued in [telemetry](../features/observability/telemetry.md); what to alert on is in
[monitoring](./monitoring.md#what-to-alert-on), and the "Loom — Code Plane" dashboard is
defined in `deploy/dashboards/code_plane.json`.

## Related

- [Repositories](../features/code-index/repositories.md): self-service repositories,
  maintainers and readers
- [Freshness](../features/code-index/freshness.md): how a client reads index lag
- [Code index architecture](../architecture/code-index-architecture.md)
