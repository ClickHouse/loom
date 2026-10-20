# okf-ci — the compile gate for a repository of Loom OKF bundles

Loom renders a namespace as an OKF v0.2 bundle (one Markdown
file per memory, YAML frontmatter, an `index.md` per directory, a `log.md`)
with `loom export-okf`; committing that bundle into a git repository makes
memory changes arrive as diffs the team already reviews. A diff nobody can
gate is a log, not a review. `loom okf-lint` is the gate, and this directory
is how such a repository runs it in CI.

## What the gate checks

`loom okf-lint DIR` reads the bundle directory and nothing else: no server,
no ClickHouse, no network. Findings carry an id, a path, a message and a
severity.

| id                              | severity | what it means                                                                                                   |
| ------------------------------- | -------- | --------------------------------------------------------------------------------------------------------------- |
| `frontmatter`                   | critical | a concept has no frontmatter, unparseable YAML, an empty `type` or a `tags` value that is not a list            |
| `required_keys`                 | critical | `title`, `description`, `loom.memory_key` or `loom.memory_type` is missing                                      |
| `relates_to`                    | critical | an edge is malformed or its `.md` target does not exist in the bundle                                           |
| `index_missing` / `okf_version` | critical | a directory with concepts has no `index.md`; the root index declares no `okf_version`                           |
| `staging`                       | critical | a `staging/` directory exists; staging never answers                                                            |
| `type_directory_mismatch`       | warning  | `loom.memory_type` disagrees with the folder the file sits in                                                   |
| `pointer_unmounted`             | warning  | a `relates_to` path is a `loom://org/namespace/key` pointer to a concept outside this bundle                    |
| `unknown_relation`              | warning  | a `relates_to` kind outside Loom's canonical verbs                                                              |
| `edited_since_export`           | warning  | the body no longer hashes to `loom.content_sha256` (a concept or a raw chunk edited by hand)                    |
| `derived_needs_confidence`      | warning  | `source_class: derived` at confidence 1.0, an inference written as a fact                                       |
| `high_sensitivity_present`      | warning  | a high row (`loom.sensitivity: high` or the `sensitivity:high` tag) travelled; the export skips them by default |
| `index_listing`                 | warning  | a concept its directory index or the root index does not list                                                   |
| `drift`                         | warning  | the root index's `loom.corpus_digest` no longer matches the concept files                                       |
| `orphans`                       | info     | concepts that link to nothing and are linked from nothing (a property of the data, not a defect)                |

Exit codes follow the shape a compile step needs: **2** on any critical
finding, **1** on warnings only, **0** clean. `--strict` makes warnings exit
2 as well; `--json` prints the machine-readable report.

The same gate runs in `loom import-okf`, which lints before writing and
refuses on a critical finding unless `--force-import`.

## Install

1. Copy `okf-lint.yml` into the bundle repository as
   `.github/workflows/okf-lint.yml`.
2. Set `LOOM_REF` to a 40-character Loom commit SHA. The job refuses any other
   value: the rules are versioned with Loom, and a branch or tag can move
   under a green check.
3. Set `OKF_ROOT` to the bundle directory (the one holding the root
   `index.md`): one namespace's bundle, or run the job in a matrix over the
   namespaces the repository holds.

The job checks out Loom at `LOOM_REF` beside the bundle and runs the lint
with `uv run --frozen`, so the packages come from that commit's `uv.lock`;
one `LOOM_REF` is one set of rules and one set of dependencies. It prints the
summary and the first forty findings, sorted by severity, id and path, uploads
`okf-lint.json` as an artifact, and fails on exit 2 or on a lint that did not
run.

Every action is pinned to a full commit SHA, with its version in a comment.
The workflow asks for `contents: read` only, and neither checkout keeps the
token in the git config (`persist-credentials: false`). The lint itself opens
no network connection and needs no ClickHouse.

## Run it locally

The same command, the same result:

```bash
git clone https://github.com/ClickHouse/loom .okf-lint-loom
git -C .okf-lint-loom checkout <LOOM_REF>
uv run --project .okf-lint-loom --frozen --python 3.12 loom okf-lint <OKF_ROOT>          # human report
uv run --project .okf-lint-loom --frozen --python 3.12 loom okf-lint <OKF_ROOT> --json   # the CI report
```

## The generation digest

When the root `index.md` carries `loom.corpus_digest`, the
gate recomputes it: take the concept files only, sort them by
bundle-relative path, append each file's sha256 hex digest without a
separator, and sha256 that string. A mismatch is the `drift` warning and
names how many concept files the tree holds against what the index says.
