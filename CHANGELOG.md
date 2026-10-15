# Changelog

All notable changes to Loom (the server, its web app and its coding-agent plugins). The
format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[SemVer](https://semver.org/). The SDKs keep their own changelogs in
[sdk/python](./sdk/python/CHANGELOG.md) and [sdk/typescript](./sdk/typescript/CHANGELOG.md).
A release tag `vX.Y.Z` ships only when the top entry reads `## [X.Y.Z] - YYYY-MM-DD`.

## [Unreleased]

### Added

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

### Fixed

- Choosing a new embedding model in **Settings → Models & keys** failed with
  `source_text_missing` on a freshly indexed repository: the rebuild took the zero vector
  that pads a text-less row (a file's structural summary card before its summary is
  written) for a real one. Such rows now rebuild as placeholders.

- The quickstart told released-stack users to add repositories from the dashboard, which
  that stack refuses with `SANDBOX_UNAVAILABLE`, since it runs without the repository-job
  sandbox. It now shows how to index one through `LOOM_CODE_REPOS_JSON`, `loom code init`
  and `loom code backfill`, with no `scip_producer` or working tree to set up. The refusal
  names that route where it works, the `repo_jobs_unsandboxed` fix in `loom doctor` and
  `/v1/health` matches it, and the sandbox docs say which hosts can load the AppArmor
  profile: AppArmor 4.0 or later, tested on Ubuntu 24.04.
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
