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

An operator entry for a namespace a member added from the page is skipped
([adding repositories](../features/code-index/repositories.md)).

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
- **Writable git mirrors.** The workers run as uid 999 and fetch into the bare clones
  under `var/code_repos` (bind-mounted into the containers). The host operator shares
  that directory with them through a group, gid 999 here. Prepare it once:

  ```bash
  sudo chgrp -R 999 var/code_repos && sudo chmod -R g+rwX,o-rwx var/code_repos
  sudo find var/code_repos -type d -exec chmod g+s {} +
  ```

  The setgid bit keeps new entries in the group. Then add the host operator to the
  group and set `LOOM_CODE_REPOS_SHARED_GID=999` in `.env`, which the containers and
  the host's `loom code` commands both read. A mirror or checkout directory Loom
  creates in that group is mode 2770; every other one is 0700, open to the server's
  user only, so nothing is shared until the setting names the group. With the
  setting, the server and every `loom code` command run with umask 007: the
  directories and lock files they create are writable by the group, git's files give
  the group what they give the owner (its objects stay read-only), and other users get
  nothing. Run a harness by hand on a checkout under the root through
  `loom code entry-lock`, which runs it under the same umask. Loom changes an existing
  directory's mode only for a restricted entry: run the commands again for files an
  older build or another tool created.

  The group reads every mirror, restricted ones included, so it must hold only the
  users that run Loom: the host operator and the containers' user (`getent group 999`).
  When gid 999 is already a system group with other members (`systemd-journal` on many
  distributions), run the containers with a dedicated group instead (Compose
  `user: "999:<gid>"`) and use that gid in the commands and the setting. Keep every
  path the host and the containers share (`code_repos_root`, `code_scip_checkout_dir`
  and each entry's `git_dir` and `scip_checkout_dir`) inside the prepared directory: a
  directory or lock file Loom creates elsewhere takes its parent's group under a
  setgid parent and the creating process's group otherwise, and with the umask that
  group could write it.

  The umask covers every file those processes write, outside the root too, where a
  file takes the writer's primary group: the host operator's primary group must be
  its own (a user private group, the default on most distributions), not one shared
  with others such as `users`.

  Upgrade the host checkout that runs `loom code` jobs together with the image, and
  run the commands above again once a colour deployment's previous colour has stopped.

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
| `scip_producer`, `scip_checkout_dir` | the compiler-tier indexer (`clang`, `typescript`, `python`, `go`; unset, the tree's build files pick) and its working tree    |
| `scip_extra_producers`               | further compiler-tier producers over the same tree (a Python repository with a TypeScript frontend), each in its own checkout |
| `git_extra_branches`                 | globs of other remote branches the shared mirror should carry                                                                 |
| `github_token`                       | per-entry override of `LOOM_CODE_GITHUB_TOKEN`                                                                                |

Name a `git_remote` host in ASCII, as punycode for an internationalised name: a page
add refuses any other, so the two spell a host alike.

An entry whose `git_remote` is a local path or `file://` mirror is known by its `repo`,
which a page add of the same repository must match: name a mirror of a repository on a
host other than GitHub as `host/owner/name` in `repo`.

`public` is read when `loom code init` first registers the namespace; editing it later
does not change the join policy, which a maintainer changes with `code.repo.set_access`.
A configured entry starts with no maintainer. `POST /v1/admin/namespace.adopt` refuses a
restricted namespace, and no route or command installs the first direct grant on a
configured restricted namespace, so a private repository is set up in this order:

1. Configure the entry without `classification`, with `public` false (the default) and no
   `org_readable`, and leave `LOOM_CODE_ORG_READABLE` at its default, `false`. Until step
   3 the namespace is standard: a grant there, or a token whose claim covers it (a `*`
   claim included, not only one naming it), reads it.
2. Run `loom code init` for it, which writes the namespace's directory row (adopt answers
   `NAMESPACE_NOT_FOUND` before that), and adopt it for its maintainer. Do not run
   `loom code backfill` (or any `loom code backfill-*` command) yet: a configured entry
   gets no first index, PR history, backport verdicts or extra branches
   (`code.repo.add_branch` answers `REPO_NOT_INDEXED`) until the backfill writes its
   `git_head` watermark, so nothing of the repository is indexed until you run it. The
   entry's git mirror is cloned meanwhile, and its history reads pass the same gate, so
   finish step 3 before handing out `*` tokens of the org.
3. The maintainer restricts it with `code.repo.set_access` (`classification: "restricted"`
   and `join_policy: "approval"` or `"closed"`; both are required); then set
   `classification: "restricted"` on the entry as well, so either input restricts, and run
   `loom code backfill`. Mirrors and compiler-tier checkouts are created at mode 2770
   when they belong to `code_repos_shared_gid`, open to the server's
   user and that group only, and 0700 otherwise (see "Writable git mirrors"); one an
   older build created, or whose mode was changed by hand, is set to that mode at once
   on the host that serves the call, and on every other host that holds it at that
   host's next mirror round, once the host reads the namespace's directory row as
   restricted (within half a minute). Only the directory itself changes: what an older
   build created inside it needs the commands in "Writable git mirrors".

The restriction is per branch: each branch's index is its own namespace. A person whose
own token reads the repository on its host can still add another of its branches with
`code.repo.add`, into a new standard namespace; that person can already read the
repository. Set up each configured branch of a private repository in the order above.

An entry that names no `scip_producer` keeps `scip_checkout_dir` for C/C++ (see
[Compiler tier](#compiler-tier)), so name the producer when you prepare that tree for
another language.

The repository's `.gitattributes` `linguist-generated` / `linguist-vendored` patterns
and a `.loomignore` file at the indexed commit are honoured as excludes. A dependency
vendored as a git submodule is not in the bare mirror; give it its own entry and name it
in `depends_on`. The gauge `loom.code.repos_config_errors` reports entries the server
could not use (invalid JSON, a nameless entry, a namespace with `.` or `/`, a duplicate
namespace); 0 means the value parsed clean. Validate the JSON before a restart
with `docker compose --env-file .env -f dev/docker-compose.cloud.yml config`.

Who can read an index is governed by the namespace's join policy and grants
([tokens and grants](../guides/tokens-and-grants.md)). `LOOM_CODE_ORG_READABLE=true`
makes every code namespace readable by every principal in the org without a grant; the
default is off.

## First index

```bash
docker exec loom-workers loom code init     --namespace code-myrepo --org acme   # optional: bare clone + index state
docker exec loom-workers loom code backfill --namespace code-myrepo --org acme --embed
docker exec loom-workers loom code backfill-prs --namespace code-myrepo --org acme --months 12
```

- `init` is optional: the ingest pass clones a missing mirror in the background, for an
  operator-configured entry too, and logs the path it clones into (`loom doctor`'s
  `code_mirrors` line names an operator `git_dir` that is missing, so a mistyped path
  is not a silent multi-GB clone). The mirror is a cache of the remote ClickHouse
  records: every serving host clones the mirrors of indexed entries at boot
  (`LOOM_CODE_MIRROR_WARMUP`) and, every `LOOM_CODE_MIRROR_FETCH_INTERVAL_S`, fetches
  the refs the leader's pass recorded in ClickHouse that it lacks (no `ls-remote`, and
  no network call when it is current). Until a host has the mirror, its history reads
  answer `no_git_mirror: cloning <entry>`; `/v1/health?include=mirrors` counts the host's
  mirrors under `mirrors`. Disk: every host holds a copy of every mirror (N replicas, N copies), and
  an operator `git_dir` must be writable by the `loom` user. Network: a host with an
  empty disk makes one full clone per indexed repository; keep `LOOM_CODE_REPOS_ROOT`
  on a persistent volume so a rollout does not re-clone every mirror on every pod.
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
after a merge. It runs the same ingest pass immediately for that namespace on the replica
that received it, under the namespace's lease (`code_refresh:<org>/<namespace>`), so
triggers landing on different replicas coalesce into one pass. It answers 404 for a
namespace no entry configures, so the pusher never decides what gets indexed. It works
with the poller disabled.

Open pull requests are not indexed; the working tree stays the client's own.

## Compiler tier

The parser tier answers from tree-sitter. Where a compiler-grade (SCIP) index exists, its
symbols and references win. Each language has a worker that indexes the commits between
its own watermark and the branch head in the entry's mirror (fetched by `code_ingest`)
and applies the delta in-process, on whichever process holds the workers lease. A
compiler tier does not wait for the parser tier's `git_head`; only an entry's first
index does.

| Language   | `scip_producer` | Worker flag                                                   | Host needs                                                                                                          |
| ---------- | --------------- | ------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------- |
| C / C++    | `clang`         | `LOOM_CODE_SCIP_DELTA_ENABLED`, `LOOM_CODE_SCIP_FULL_ENABLED` | clang 21, CMake and ninja (in the image) and scip-clang (in the amd64 image; elsewhere `LOOM_CODE_SCIP_CLANG_PATH`) |
| TypeScript | `typescript`    | `LOOM_CODE_SCIP_TS_DELTA_ENABLED`                             | Node and the pinned indexer/package managers (included in the image); dependencies are prepared automatically       |
| Python     | `python`        | `LOOM_CODE_SCIP_PY_DELTA_ENABLED`                             | scip-python (in the image)                                                                                          |
| Go         | `go`            | `LOOM_CODE_SCIP_GO_DELTA_ENABLED`                             | Go and scip-go (in the image)                                                                                       |

An entry that names no `scip_producer` gets every tier whose build file its tree
carries: `go.mod` (at any depth) for Go; `tsconfig.json`, `jsconfig.json` or
`package.json` at the root for TypeScript; a root `CMakeLists.txt` for C/C++;
`pyproject.toml`, `setup.py` or `requirements.txt` at the root for Python.
`code.index_status` lists them as `compiler_candidates`. Name the producer where a build
file is incidental, such as a C++ repository whose `pyproject.toml` only serves its test
tooling; `scip_extra_producers` adds tiers beside a named one. Each tier works in its own
tree: the named producer (C/C++ when the entry names none) in `scip_checkout_dir`, every
other one in `<scip_checkout_dir>.<producer>`.

All the workers are enabled by default, and the Docker image carries every indexer:
scip-python, scip-typescript, scip-go, and for C/C++ clang 21, lld, CMake, ninja and
scip-clang. Upstream builds scip-clang for x86_64 Linux only, so the arm64 image has
the rest of the C/C++ toolchain but no scip-clang: point `LOOM_CODE_SCIP_CLANG_PATH` at
your own build, or the C/C++ tier records `scip-clang missing` as a skip. Each worker
creates a missing working tree as an empty repository and fetches each commit into it
from the entry's mirror, so a new leader starts from nothing. A worker whose project
prerequisites are absent records a skip in `workers.status`; the Tree-sitter tier
remains available. TypeScript projects have their dependencies installed
automatically in their checkout.

The C/C++ harness runs the repository's CMake configure (compile database only, no
build) on the first run in a tree, or when its `CMakeCache.txt` is missing, was made in
another directory, or names a compiler the host no longer has. It runs the configure
again on the existing cache before a full index and for a delta whose window adds a
`.cpp`, `.cc` or `.cxx` file or adds, changes or deletes a `CMakeLists.txt` or `.cmake`
file, because the compile database lists only the translation units of the tree it was
configured on. A reconfigure that fails fails the run with CMake's last lines, and the
entry's `clang` record (`clang_full` for a full index) reads failing with kind `cmake`
(`disk` when the disk is full). A failed delta moves no watermark, so its window, with
the translation units it adds, is retried every pass; a full index is retried 6 hours
later, as any failed full run is.

A large C++ tree is the heavy case: a ClickHouse-sized checkout with its submodules
and build tree is about 30 GB beside the mirror, and scip-clang holds a translation
unit's AST per job (a few GB each), so `LOOM_CODE_SCIP_CLANG_JOBS` should fit the
memory the workers process has. The full index of such a tree takes about an hour.

- **C/C++ delta cap.** The worker indexes changed translation units, including those
  that include a changed header, up to `LOOM_CODE_SCIP_DELTA_MAX_TUS` (200); above the
  cap it indexes only the changed `.cpp` files. A delta that reconfigures CMake runs the
  configure inside the same `LOOM_CODE_SCIP_DELTA_TIMEOUT_SECONDS` (3600) as the index;
  a timed-out delta leaves `scip_commit` where it was, so the next pass retries a larger
  window.
- **Delta caps.** The server reads a compiler tier's delta (every tier, delta or full
  run) only when it is a regular file, not a link, and applies it a round of files at a
  time (`server/code/app/index/delta_file.py`). It first counts the delta's `{`, `[`
  and `,` bytes, then parses every value once and checks each file entry, and only
  then reopens it and reads the files again a round at a time for the apply. So a
  delta over a cap, or one that does not parse, fails the entry's pass before anything
  is applied. The caps hold as read when the delta is opened; a changed value reaches
  the next pass.
  - `LOOM_CODE_SCIP_DELTA_FILE_MAX_MB` (180 MiB) bounds the file's bytes and
    `LOOM_CODE_SCIP_DELTA_MAX_NODES` (6,000,000) its counted bytes, which bound the JSON
    values the parses build. They bound the work of a pass. Its memory is the round
    below plus a record of each file entry and deleted path: about 70 B each, as many
    as the node cap allows (a minimal entry costs 2 counted bytes, a deleted path 1:
    about 3 million entries, some 200 MiB, or 6 million deleted paths, some 400 MiB,
    at the default), and their paths' text (at most 4,096 bytes each), which the byte
    cap bounds. A large C++ tree's full run writes over 500 MiB in entries of tens of
    KB, a few MiB of bookkeeping: raise both for it.
  - `LOOM_CODE_SCIP_DELTA_ROUND_MAX_NODES` (3,000,000) bounds one round's memory: the
    counted bytes one apply round parses. A round holds at most 100 files, and is
    released before the next is parsed; one file entry alone may fill it, and a value
    over it fails the pass, naming the entry (the deleted paths are read one at a
    time, each its own value). A fixed cap of 64 million characters, not a setting,
    bounds one value's and one round's text the same way; a real entry has about 32
    characters per counted byte, so for one it binds near 2,000,000 counted bytes.
    The largest file entry of a large C++ tree's full run holds about 600,000 counted
    bytes (some 100,000 edges), about a fifth of the round budget and a third of the
    character cap. The densest round repository code can write (minimal symbols)
    peaks at about 1.8 GiB resident while it is read at the default budget (measured
    with `ru_maxrss`; about 330 B per counted byte of Python objects, the rest the
    allocator's); a value's text near the character cap (up to 4 bytes a character)
    may be held up to three times while the read buffer grows, so the worst round's
    read is about 2.5 GiB, whatever the delta's size. Its apply holds that round's
    rows on top (the live rows read for its paths and the new rows to insert), about
    as much again for a round of new symbols, so size the worker for about twice the
    read. A real file entry of a C++ delta has one counted byte per about 32 bytes: a
    round is its 100 files long before it reaches the budget. To measure the read of
    the densest default round from a source checkout:

    ```bash
    .venv/bin/python -c "import json, resource, tempfile, pathlib; from server.settings import settings; from server.code.app.index.delta_file import open_delta; p = pathlib.Path(tempfile.mkdtemp()) / 'delta.json'; n = settings.code_scip_delta_round_max_nodes // 2 - 10; p.write_text('{\"commit\":\"c\",\"files\":[{\"path\":\"a\",\"symbols\":[' + ','.join(['{\"qualified_name\":\"\"}'] * n) + ']}]}'); list(open_delta(p).rounds(100, lambda x: True)); print(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss >> 10, 'MiB peak RSS')"
    ```

  An invalid delta fails the pass with a short `delta invalid: <reason>` naming its
  first problem (for an entry, `file entry <i>: …`), not pydantic's list of every bad
  item. `--keep-delta` keeps the delta as the harness wrote it, before it is parsed
  (the byte cap still applies), as a file only the user that ran the command can read
  (mode 0600).

- **C/C++ full index.** `code_scip_full` re-indexes one entry per pass: an entry whose
  `scip_commit` is not seeded yet (the delta skips it with "watermarks not seeded"), and
  every entry once its last applied full run is `LOOM_CODE_SCIP_FULL_EVERY_HOURS` (168)
  old, which heals capped deltas. A run that fails is retried 6 hours after it failed,
  and one killed with its pod (a rollout) 6 hours after it started. A
  base-unknown delta clears `scip_commit`, so the entry gets a full index next. The pass
  runs under the entry lock with `LOOM_CODE_SCIP_FULL_TIMEOUT_SECONDS` (8 hours) for
  the harness and six more for the checkout move, the apply and the repack.
  `loom code scip-full` runs the same index by hand (`--keep-delta` saves the delta so an
  interrupted apply can be re-run with `--apply-delta`), and `loom code scip-delta` one
  delta pass, which prints a `RESULT:` line.
- **C/C++ submodules.** The tier fetches submodule pins with the entry's access
  token, so a private submodule whose `.gitmodules` URL is an absolute https URL under
  the remote's owner needs no other credential. The owner is matched as written, case
  included, so an owner spelled in another case is fetched without the token. A
  relative URL resolves to a local path in the checkout, which has no remote; the tier
  refuses the `file` transport, so that submodule update fails. Whoever can commit to
  the repository's `.gitmodules` can point a submodule at any repository of that owner
  the token reads, so give an entry a token scoped to the repositories its submodules
  need.
- **SQL ↔ C++ crossref.** `code_crossref` rebuilds `code_test_map`'s content-reference
  rows (test queries to the files that register the functions and settings they use)
  for each C/C++ entry whose `git_head` moved since its last run. It reads the parser
  tier's chunks only.
- **Checkout housekeeping.** The full C/C++ pass (under the entry lock) and the
  Python, TypeScript and Go passes repack their entry's checkout with
  `git gc --no-prune` every `LOOM_CODE_MIRROR_GC_INTERVAL_H`, under the same rules as
  the mirrors (below). Every git command a tier runs in a checkout has auto-gc and
  background maintenance off, so no fetch starts a repack.
- **Entry locks.** A full pass and a delta never work on the same entry at once: each
  takes the entry's lock file in `LOOM_CODE_SCIP_LOCK_DIR` (`var/cron/locks`),
  `scip.<org>.<namespace>.lock` with both names percent-encoded
  (`loom code scip-lock-path --org ORG --namespace NS` prints it). A full pass waits for
  a delta already on its entry; a delta skips an entry a full pass holds and names it
  under `skipped` ("entry lock held (full pass running)"). Different entries run side by
  side. Every pass also takes the entry's key in ClickHouse (`scip_entry:<org>/<namespace>`
  in `loom._migration_lock`), so
  passes on two hosts exclude each other too; a dead holder's key expires after 120 s.
  A pass that cannot take the key (ClickHouse or Keeper unreachable) fails instead of
  running on the host's file alone. A holder counts its key lost after 60 s without a
  renewal (a pause included), before another host can take it, and writes no further
  index row or watermark: every such write checks the key first, and the pass ends with
  `EntryLockLost`. A manual harness run goes under
  `loom code entry-lock --org ORG --namespace NS -- <command>`, which takes both locks,
  exits 75 when another pass holds the entry, and kills the command (exit 3) if the key
  is lost. It hands the command the key's holder (`LOOM_ENTRY_LEASE_HOLDER`), which the
  harness's uploads carry: `code.scip_ingest` refuses a part (409 `ENTRY_LEASE_NOT_HELD`)
  once that holder no longer has the key, so a pass that lost its entry completes no
  batch for `scip_apply`. With `--tier <producer>` it also records the command's exit as
  that compiler tier's health on the entry (`tier_health` in `code.repos`), and relays the
  command's output, stderr included, to stdout.
- **TypeScript dependencies are automatic.** Fresh checkouts install dependencies
  before compiler indexing, including development dependencies containing type
  definitions. Loom uses npm, pnpm, Yarn (classic or modern) or Bun according to the
  lockfile, enforces existing lockfiles and disables lifecycle scripts and executable
  package-manager hooks. Plain scripts without a package manifest need no install.
  Dependency manifest/lockfile changes trigger a refresh before the next index; a failed
  or interrupted install is retried without advancing the compiler watermark.
  Downloads need network access to the repository's package registry. A private package
  registry, incompatible lockfile or generated types requiring a build can still need
  project-specific configuration. `workers.status` names the failed installer and its
  exit code, and the entry's tier-health record (`code.index_status`) holds the end of
  the installer's output. Server credentials are not passed into the installer environment.
  The server reads the checkout's `package.json` (up to 1 MiB) and each install root's
  `yarn.lock` (up to 64 MiB) only as regular files, never through a link; a symlinked one
  fails the TypeScript pass even when the link points inside the repository.
- **Repository files are size-checked before they are read.** The ingest asks the mirror
  for a file's size first: a file over `LOOM_CODE_INGEST_MAX_FILE_BYTES` is not read, and
  its `code_files` row records the reason (`code.index_errors`); 0 lifts the cap. Every
  manifest the import resolver reads (the TypeScript, Python and Go project and workspace
  files) is read from the mirror up to 1 MiB, a `go.mod` up to 64 KiB and at most 200 of
  them; a larger one is skipped once per commit with a warning naming the org and
  namespace, and the imports it would have resolved stay unresolved. Outside a worker
  pass the resolver reads a file from its indexed chunks instead, which hold only files
  within `LOOM_CODE_INGEST_MAX_FILE_BYTES` (any size at 0). A barrel's re-exports are read
  under the same file cap. A binary or denylisted file over the cap is tombstoned as
  one under it is, by its path and its first 8 KiB; a generated one gets the skipped
  row, with no declarations.
- **The repository's own exclusions are bounded and fail open.** `.loomignore` and
  `.gitattributes` are read up to 1 MiB and give at most 1,000 patterns between them. A
  larger file gives no patterns, and patterns past the 1,000th are dropped, so the paths
  they would have excluded are indexed; the log names the file or the count. The
  entry's own `exclude` globs are never cut, so a path that must stay out belongs
  there.
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
  that is or holds `var/`, the mirrors and checkouts, the env file, the
  install root or `HOME`, nor one inside `var/` or under `HOME` (the default
  `var/bin/scip-clang` is mounted as that one file). Where one of those paths lies
  inside any other mounted directory (a mirror inside the server's Python
  prefix, `var/` under a system path), an empty mount covers it inside the sandbox.
  The install root is the exception, since the server's Python may run from it. A host
  whose Python prefix or a system path is, or sits inside, one of Loom's data paths
  runs no sandboxed job: no empty mount could hide that data, and `/v1/health` and
  `loom doctor` report `repo_sandbox_layout_refused`. The checkout (or the
  bare mirror, for a fetch) and the output directory are writable, the checkout's
  `.git` and the harness scripts are read-only, and `/tmp` and `HOME` are a fresh
  directory removed after the job. No server environment, credentials, host `HOME`,
  Docker socket or other host paths;
- rlimits, set by `prlimit`: `LOOM_CODE_SANDBOX_CPU_SECONDS` (7200),
  `LOOM_CODE_SANDBOX_MEMORY_MB` (16384) and `LOOM_CODE_SANDBOX_MAX_FILE_MB` (32768),
  set before bwrap starts, bound **each process** of a job (CPU time, data memory, file
  size), not the job's total. `LOOM_CODE_SANDBOX_MAX_PIDS` (2048) bounds the processes
  and threads of **one job**: it is set inside the job's user namespace, where the
  kernel counts only that job's tasks (Linux 5.14 and later; older kernels count the
  uid's tasks host-wide). Outside it, RLIMIT_NPROC would count every task of the uid on
  the host, other containers' included. The sandbox runs the `prlimit`
  found on the server's `PATH`, so it must sit under a mounted system path such as
  `/usr`. Loom sets no cgroup; give the container its own memory and pids limits for a
  hard total. The colours set `pids_limit: 8192` on each container as that backstop.
  Each job also has a wall-clock timeout that kills its whole tree; the job's pid namespace
  guarantees no process it started outlives it;
- bounded output in the server: a job's stderr, and the stdout of a build, install or
  clone, keep only their last 1 MiB; a command whose stdout Loom parses (`ls-remote`,
  fetch) may print up to 64 MiB, and past that the whole job is killed and fails. A file
  the server reads back from a job or a checkout is a regular file, never a link: a
  compiler tier's delta up to `LOOM_CODE_SCIP_DELTA_FILE_MAX_MB` and
  `LOOM_CODE_SCIP_DELTA_MAX_NODES`, the checkout's `package.json` up to 1 MiB and each
  install root's `yarn.lock` up to 64 MiB;
- no network, except for clones, fetches, submodules and dependency downloads, and then
  only through an egress proxy outside the sandbox. It accepts `CONNECT` to ports 443,
  22 and the remote's own port, resolves the name (from `/etc/resolv.conf` alone: not
  `/etc/hosts` or nsswitch, and no search domains, so a remote needs its full DNS name),
  refuses it unless **every** address
  is public (loopback, private ranges, link-local and the cloud metadata address are
  refused, including when reached through a redirect or a `.gitmodules` URL) and
  connects to the address it vetted. Plain HTTP has no route. One job holds at most 32
  tunnels at a time, and a tunnel idle for 5 minutes is closed.

Every git a repository job runs reads no system or global git config and allows only
the `https` and `ssh` transports (`protocol.allow=never`); `ext::` and `file://` are
refused, so are URL rewrites. The entry's access token reaches only its clone, its
fetches and the C/C++ tier's `git submodule update`, never a build or `go mod
download`, and git sends it only to URLs under the remote's owner
(`https://host/owner/`; the whole host for a remote with no owner path). Inside the
sandbox an `ssh` remote has no host SSH key: use an HTTPS remote with an access token.

**Which stacks run the sandbox.** The blue/green colours `redeploy.sh` deploys
(`dev/docker-compose.colour.yml`) set the container options below. The other Compose
files (`compose.yaml`, `dev/docker-compose.cloud.yml`) do not: an
AppArmor profile the host has not loaded stops a container from starting, so they keep
Docker's defaults, the probe fails and every repository job runs as a plain process (see
_Without the sandbox_). To run it there, once the host has the profile:

- copy the colour file's `security_opt` block and its `pids_limit` (the container's
  backstop). The `seccomp=` path is relative to the project directory (the compose
  file's folder unless `--project-directory` says otherwise): `./seccomp/…` works
  for the files in `dev/`, and `compose.yaml` needs
  `./dev/seccomp/loom-repo-sandbox.json`;
- the release's standalone bundle (`loom-stack.tar.gz`) ships neither the seccomp file
  nor the AppArmor profile: take both from the same release's source tree and point
  `seccomp=` at where you put the JSON;
- `redeploy.sh` checks only the colour compose. Run `dev/apparmor_check.sh <compose file>`
  by hand before starting another stack, and the throwaway-container check below.

A source run (`make server`) has the sandbox only where the host passes the same
requirements.

**Which hosts can load the profile.** The shipped profile targets AppArmor 4.0 or later
(`abi <abi/4.0>`). Only Ubuntu 24.04 is tested, by the CI `sandbox-profile` job with
`kernel.apparmor_restrict_unprivileged_userns=1`; Debian 13's 4.x parser accepts the
ABI, but the profile is untested there. Hosts without AppArmor, such as Docker Desktop
(on any OS) or SELinux distributions like Fedora and RHEL, cannot use the colours'
options safely: Docker ignores `apparmor=` there, so nothing replaces the `/proc` masks
that `systempaths=unconfined` drops (see _`/proc`_ below). Older parsers (Ubuntu 22.04,
Debian 12) reject the profile; those hosts can run the sandbox only with
`seccomp=unconfined` and `apparmor=unconfined` (the paragraph before _Without the
sandbox_), which lose the same `/proc` protection. The released stack runs on all of
these hosts without the sandbox.

Unless `LOOM_CODE_SANDBOX_REQUIRED` is set, every host still indexes the repositories
listed in `LOOM_CODE_REPOS_JSON`; only repositories added from the page need the
sandbox. _Without the sandbox_ says what that runs, and the
[quickstart](../getting-started/quickstart.md#optional-index-a-repository-on-the-released-stack)
has the released stack's commands.

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
  `mount` and `umount2` only to a container with `CAP_SYS_ADMIN`, and `pivot_root` to no
  container. `dev/seccomp/loom-repo-sandbox.json` is Docker's default profile
  (moby/profiles `seccomp/v0.2.3`, the one Docker 29 ships) plus one rule allowing those
  five to every process in the container; bwrap needs all of them. Inside the sandbox,
  Loom's own filter refuses the job `unshare`, `mount`, `umount2`, `pivot_root` and a
  `clone` with `CLONE_NEWUSER`. A `clone` asking for any other new namespace passes the
  filter and fails for want of a capability, since bwrap runs the job with
  `--cap-drop ALL`.
- **AppArmor.** `docker-default` has `deny mount`. `apparmor=unconfined` does not help
  on hosts that restrict unprivileged user namespaces (Ubuntu 23.10 and later,
  `kernel.apparmor_restrict_unprivileged_userns=1`): there an unconfined process that
  creates a user namespace is moved into the `unprivileged_userns` profile, which
  refuses the mounts. In `deploy/apparmor/loom-repo-sandbox` every process in the
  container runs under `docker-default`, renamed, with `deny mount` kept and, on ABI
  4.0, no `userns` rule, so user namespaces are refused. Only an exec of
  `/usr/bin/bwrap` moves into the child profile `loom-repo-sandbox//bwrap`, which is the
  same plus `userns, mount, pivot_root`. bwrap and everything it starts stay in the
  child profile, so a repository job is allowed those by AppArmor and refused them by
  Loom's seccomp filter above. The boundary is the exec of bwrap: the server and the
  tools it runs outside the sandbox cannot unshare or mount, but any process in the
  container that can exec `/usr/bin/bwrap` can create user namespaces and mount inside
  them, without Loom's filter.
- **`/proc`.** Docker masks paths under `/proc` and `/sys`, and the kernel then refuses
  the fresh procfs mount the sandbox makes; `systempaths=unconfined` unmasks them. That
  also drops the read-only mounts over `/proc/sys`, `/proc/sysrq-trigger`, `/proc/irq`,
  `/proc/bus` and `/proc/fs`, and the masks over `/proc/kcore`, `/proc/keys`,
  `/sys/firmware` and the rest. In both profiles the `docker-default` rules still refuse
  writes to `/proc/sys`, `sysrq-trigger` and the other system files under `/proc` and
  `/sys`, and any access to `kcore` and `/sys/firmware`. On a host without AppArmor,
  Docker ignores the `apparmor=` option and nothing takes the mounts' place, so
  `redeploy.sh` refuses such a host.

**`--disable-userns`.** The child profile keeps `docker-default`'s `/proc/sys` write
denials, which refuse the sysctl write bwrap's `--disable-userns` makes. An allow rule
cannot override a deny, and narrowing the deny globs for both forms the path can take
would make them hard to review. So under this profile, production included, the probe is
expected to see bwrap name `max_user_namespaces`, log it at INFO and run bwrap without
the flag; Loom's filter refuses new user namespaces on its own. Any other probe failure
is logged at WARNING and leaves the sandbox off.

**Loading the profile.** Load it on the Docker host once; `/etc/apparmor.d` is loaded
again at every boot:

```bash
sudo install -m644 deploy/apparmor/loom-repo-sandbox /etc/apparmor.d/loom-repo-sandbox
sudo apparmor_parser -r -W /etc/apparmor.d/loom-repo-sandbox
```

**Checking it.** `apparmor_parser -Q -K` accepts the profile, but whether bwrap runs
under it is known only once a host has loaded it. CI is where that first happens: the
`sandbox-profile` job in `.github/workflows/docker-smoke.yml` loads the profile on
Ubuntu 24.04 with `kernel.apparmor_restrict_unprivileged_userns=1` left on and runs the
probe and the hostile suite in the production image with the colours' options. On a
deploy host, check it in a throwaway container from the build about to be deployed, not
the serving one (`True` means the sandbox runs):

```bash
docker build -q -t loom:sandbox-check .
docker run --rm --network none --user loom -w /app --entrypoint python \
  --security-opt seccomp=dev/seccomp/loom-repo-sandbox.json \
  --security-opt apparmor=loom-repo-sandbox --security-opt systempaths=unconfined \
  loom:sandbox-check -c 'from server.code.app.index import sandbox as s; print(s.sandbox_available())'
docker image rm loom:sandbox-check
```

**What `redeploy.sh` checks.** Before building a colour it stops when the profile is
not loaded, and on a host without AppArmor. After the new colour starts, and before the
proxy is swapped to it, `dev/sandbox_probe.sh` runs the same probe in each of the
colour's containers that runs under the profile (`loom-<colour>` and
`loom-<colour>-workers`). When one fails, or cannot be inspected, it prints why, removes
the new colour's containers and exits with the old colour still serving. Removed, not
stopped: the build already replaced the previous build in that colour, so it is no
rollback target, and its workers cannot take the lease and run jobs unsandboxed. That is
the first check of the real profile on a deploy that skipped the one above.

**Requiring the sandbox.** The probe gates the swap, but the new workers start before
it and may take the supervisor lease (on a first deploy, or when the old lease is free)
and start repository jobs. `LOOM_CODE_SANDBOX_REQUIRED=true`, which the colours set,
closes that window: where the sandbox cannot run, no repository job (clone, fetch,
dependency install, compiler tier, `loom code scip-full`'s harness) runs as a plain
process. The ingest pass and the compiler tiers skip every entry, operator ones
included, and name the skip on the pass note; `loom code scip-full` exits 1; any job
that still reaches the runner fails with `SandboxRequiredError`. `/v1/health` reports
`repo_jobs_unsandboxed` as an error. `loom code init`'s first bare clone is the
worker's clone, a repository job like the rest: it exits 1 with the refusal too.

**Turning it off.** To run the colours without the sandbox again, remove both the
`security_opt` block and `LOOM_CODE_SANDBOX_REQUIRED` from both services in
`dev/docker-compose.colour.yml`, then redeploy. Removing only `security_opt` leaves
every repository job refused and `/v1/health` degraded.

No `no-new-privileges`: under it the kernel refuses the exec transition into the child
profile, and bwrap would not start. The image's setuid binaries (`su`, `mount`,
`passwd` and the like) therefore still work in the container, as under Docker's
defaults; the profile's rules, not the read-only mounts, keep them off `/proc/sys`.

These options still weaken the container's own confinement: seccomp lets every process
in the container make the five calls (outside bwrap, AppArmor and the missing
`CAP_SYS_ADMIN` still refuse them); AppArmor lets bwrap and what it runs create user
namespaces and mount inside them, and any process that can exec bwrap gets that, a
repository job excepted (Loom's filter); and `/proc` and `/sys` rely on the profile's
rules instead of Docker's masks. Weigh that against running repository code without the
sandbox. `seccomp=unconfined` and `apparmor=unconfined` also work where the host does not
restrict user namespaces, but they drop the whole profile.

**Without the sandbox** Loom probes once per process, logs a warning and reports
`repo_jobs_unsandboxed` in `/v1/health` and `loom doctor`. With
`LOOM_CODE_SANDBOX_REQUIRED=true` every repository job is refused (above). Otherwise:

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

### Mirror housekeeping

Each serving process runs `git gc --no-prune` on its host's mirrors every
`LOOM_CODE_MIRROR_GC_INTERVAL_H` (low CPU and IO priority, only with room for a second
copy plus `LOOM_CODE_MIRROR_GC_MARGIN_GB`, 5 GB by default; without it the gc waits
another interval and logs a warning), and sweeps `LOOM_CODE_REPOS_ROOT` of the mirrors,
`checkout-*` trees and mirror lock files no active entry names, so a removed repository leaves
every host, not only the one `code.repo.remove` reached. The
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
- `LOOM_CODE_INTEL_ENABLED=false` makes every `code.*` operation but `code.repo.remove`
  return a precondition error. It also stops the coverage import and notify workers and
  the mirror host loop; every other code worker keeps its own switch
  ([workers](../reference/workers.md)).
- **Retire a repository.** A maintainer uses **Retire…** on the repository's Access tab
  (`code.repo.remove` with `confirm` set to the namespace). The entry goes inactive, an
  erasure saga purges every `loom.code_*` table for the namespace, and the bare mirror is
  deleted when no other branch uses it. Search telemetry is kept. An operator-configured
  entry is refused with `REPO_FROM_CONFIG`: remove it from `LOOM_CODE_REPOS_JSON`
  instead. With `LOOM_CODE_INTEL_ENABLED=false` the page cannot list repositories, but
  `code.repo.remove` over HTTP, MCP or the SDK is still served; the other replicas then
  sweep their copy of the mirror only once code intelligence is on again.
- **Erase an index.** `erasure.request` with `scope_type = namespace` on a code namespace
  also purges the `loom.code_*` tables. An entry that is still configured is re-indexed
  from scratch on the next pass, so retire it first.

## When a tier fails

Each tier that keeps an entry current (the mirror fetch, the parser ingest and every
compiler tier) records its last outcome on the entry: when it fails, the error with
credentials redacted, an error kind, the one fix for that kind, `failing_since` and the
failed passes in a row. The next pass that succeeds clears it. A tier run from cron
records the same way when it runs under `loom code entry-lock --tier <tier>`;
`loom code scip-delta` and `scip-full` record by themselves.

A Go module whose dependencies this host cannot fetch (Go reports a private repository,
often the repository's own modules through a vanity import path) is not a failure: the
Go tier indexes the rest and records the tier `unavailable`, kind
`dependency_unavailable`, with the fix (set `LOOM_CODE_SCIP_GO_PRIVATE` and give the
tier's user a git credential for that host; Go says the same of a path or version that
is wrong or gone). The note is written once, counts no passes, is never a
`code_tier_failing` problem and is not in `as_of.failing_tiers`, so an agent's code
answer does not flag those modules. It reflects the last run that built modules: a pass
that builds nothing leaves it, and the next run that builds without such a failure
clears it, so it comes back when a module that still cannot fetch its dependency next
changes. Under `entry-lock --tier go`, which sees only its command's exit and output, a
clean exit with no such failure clears it. A module that fails for any other reason
still fails the tier, and its record names the unreachable ones too.

The record is read where each reader already looks:

- an agent's code answers carry it in `as_of.failing_tiers`
  ([freshness](../features/code-index/freshness.md#the-as_of-block));
- `code.index_status` and `code.repos` return it in `tier_health`, and the dashboard's
  repository card shows the failing tier, since when, how far behind and the fix, and
  any `unavailable` tier with its fix;
- once a tier has failed `LOOM_CODE_TIER_FAILING_WARN_PASSES` passes in a row or for
  `LOOM_CODE_TIER_FAILING_WARN_HOURS`, it is the `code_tier_failing` problem in
  `/v1/health`, the dashboard banner and `loom doctor`
  ([monitoring](./monitoring.md#v1health)): a line per failing entry and tier, up to
  five, with its reason, and each distinct fix once. `loom doctor`, which reads with the
  deployment's own credentials, also names each entry it lists, up to five, as
  repository@branch (org/namespace) with the error line that says why; `code.repos`
  names the rest. A platform admin's token reads the same for every entry that is not
  [restricted](../architecture/security.md#restricted-namespaces); a restricted one reads
  as "a restricted entry", as does one whose classification cannot be read. Any other
  caller reads no entry, since `/v1/health` answers without a token;
- for push alerting, the `loom.code.tier_failing` and `loom.code.tier_lag_commits`
  gauges carry each tier the `code_tier_failing` problem reports
  ([telemetry](../features/observability/telemetry.md)), with an alert in
  `deploy/dashboards/code_plane.json` and, for Prometheus, the chart's rule
  ([alert rules](./kubernetes.md#alert-rules)).

The kinds (`_KINDS`) and their fixes (`fix_for`) are in
[`server/code/domain/index/tier_health.py`](../../server/code/domain/index/tier_health.py).
A failure that matches no kind reads "unclassified error" and carries the end of
the output to read.

## Failure model

| Failure                                                                                                             | Behaviour                                                                                                                                                                                                                                                                                | Recovery                                                                                                                                                                                                                                                                                                                                                                                                                       |
| ------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `git fetch` fails                                                                                                   | watermark unmoved; the entry's `fetch` tier records the error, its kind and fix (`tier_health`)                                                                                                                                                                                          | the next pass (supervisor backoff)                                                                                                                                                                                                                                                                                                                                                                                             |
| crash mid-commit                                                                                                    | the commit is re-run                                                                                                                                                                                                                                                                     | idempotent upserts                                                                                                                                                                                                                                                                                                                                                                                                             |
| a file fails to parse                                                                                               | chunks without symbols, `code_files.index_error` set (a parse past `code_parse_timeout_s` or `code_parse_memory_mb` included, and one that could not run in its process)                                                                                                                 | `code.index_errors` lists them; fixed by the next change or a repair (raising either limit re-reads none)                                                                                                                                                                                                                                                                                                                      |
| every file fails to parse with `LookupError` "... is loaded from outside the interpreter's site-packages"           | chunks without symbols for every file: tree-sitter or a grammar is installed where the parse process does not see it                                                                                                                                                                     | install the tree-sitter packages into the server interpreter's own environment, not through `PYTHONPATH` or the user site                                                                                                                                                                                                                                                                                                      |
| embedding provider down                                                                                             | chunks stored without vectors; lexical search still serves                                                                                                                                                                                                                               | `code_embed_recovery`                                                                                                                                                                                                                                                                                                                                                                                                          |
| compiler-tier apply fails                                                                                           | batch `failed` with the error                                                                                                                                                                                                                                                            | `loom code scip-retry <batch_id>`                                                                                                                                                                                                                                                                                                                                                                                              |
| a compiler-tier pass fails or hangs                                                                                 | the harness's timeout ends a hang, and the pass deadline (that timeout plus headroom for the git steps and the apply) cancels anything else; the entry lock is freed; a failure the pass reports (the harness's own timeout included) is recorded with its kind and fix in `tier_health` | the next pass; `workers.status` names the timeout                                                                                                                                                                                                                                                                                                                                                                              |
| compiler-tier window base unknown (history rewritten, pruned mirror)                                                | every `scripts/scip_harness/` tier exits 3, never an empty delta; the Python, TypeScript and Go workers clear the tier's watermark                                                                                                                                                       | the next run is a full index for that tier (`code_scip_full` for C/C++)                                                                                                                                                                                                                                                                                                                                                        |
| a compiler-tier delta is a link, not a regular file, or oversize, or the Go harness writes more deltas than modules | the delta is refused (`FileRefused`, or `Go harness wrote more deltas than its N module(s)`) and the entry's pass fails; the watermark stays                                                                                                                                             | oversize: raise `LOOM_CODE_SCIP_DELTA_FILE_MAX_MB` and `LOOM_CODE_SCIP_DELTA_MAX_NODES` (they bound work and the path text kept), for one value over the round budget raise `LOOM_CODE_SCIP_DELTA_ROUND_MAX_NODES`, within the worker's memory; a value over the fixed character cap names it (sizing above); a link, a non-regular file or extra deltas recur every pass until the repository changes or the entry is removed |
| GitHub rate limit                                                                                                   | the pass ends, cursor unmoved                                                                                                                                                                                                                                                            | the next pass                                                                                                                                                                                                                                                                                                                                                                                                                  |
| branch force-pushed (watermark no longer an ancestor)                                                               | watermark reset to the new head, logged                                                                                                                                                                                                                                                  | `loom code backfill` to re-derive                                                                                                                                                                                                                                                                                                                                                                                              |
| weekly graph stats fail                                                                                             | last week's ranks keep serving                                                                                                                                                                                                                                                           | the next weekly pass                                                                                                                                                                                                                                                                                                                                                                                                           |

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
