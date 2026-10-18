# OKF bundles

A namespace can be rendered as an OKF (Open Knowledge Format) v0.2 bundle: one Markdown
file per memory with YAML frontmatter, an `index.md` per directory listing each concept
with a one-line description, and a `log.md` of changes. The CLI is client-side, over the
HTTP API. A bundle can be diffed, reviewed in a pull request and read offline; Loom stays
the store.

The bundle is a projection, not a second store: it is regenerated from the namespace,
and an edit comes back through the ordinary write path (conflict detection, blacklist,
audit).

```bash
loom export-okf --namespace <ns> --out ./knowledge        # render
loom okf-lint ./knowledge                                 # offline check: exit 2 critical, 1 warnings, 0 clean
loom import-okf ./knowledge --namespace <ns> --dry-run    # plan the write-back
```

All three take `--server-url`, `--token` (default `$LOOM_API_KEY`, then
`~/.loom/token`) and `--org` (default: the token's `org` claim); `okf-lint` needs none of
them. Every flag is in the [CLI reference](../../reference/cli.md).

## Export

`loom export-okf` is read-only. It pages every live row of the namespace (all agents)
through `memory.list`, fetches full values with `memory.bulk_get`, and writes the planned
files under `--out`. Existing concept files are overwritten; nothing else in the directory
is touched. The mapping lives in `server/memory/domain/okf_bundle.py` (pure functions, unit
tested); `server/cli/okf.py` is the transport and the file system.

| Memory                             | Concept                                                                                                                                     |
| ---------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------- |
| `memory_key`                       | file name (slug) and `loom.memory_key`                                                                                                      |
| `memory_type`                      | directory and `type`: `semantic` → Fact, `procedural` → Procedure, `preference` → Preference, `episodic` → Event, anything else title-cased |
| value                              | body: text as is, JSON as a fenced block                                                                                                    |
| `tags`                             | `tags`                                                                                                                                      |
| agent and `created_at`             | `generated: {by: "agent:<agent>", at}`                                                                                                      |
| live `verified_by` / `verified_at` | `verified: [{by, at}]` (human and process attestations only)                                                                                |
| `status`                           | `active` → `stable`, `conflicted` → `draft`, anything else → `deprecated`                                                                   |
| `expires_at`                       | `stale_after`, a review-by date                                                                                                             |
| `source_resource_id`               | `sources: [{resource}]`                                                                                                                     |
| everything Loom-specific           | the `loom:` block, including `source_class`, `sensitivity`, `superseded_by`, `valid_to` and `content_sha256`                                |

**Links.** Edges someone authored (the namespace's typed `memory.link` edges, read with
`memory.export_evidence kind=edges`, and the `supersedes` chain a superseded row implies)
are written as authoritative frontmatter `relates_to: [{path, kind}]` and mirrored in the
body's "Related Concepts" section. Shared-entity links follow, capped and symmetric, so
the bundle has a graph a validator can walk. Concepts that link to nothing remain orphans,
and the CLI reports them rather than hiding them.

**Provenance travels.** The `loom:` block carries the row's provenance tier so a round
trip cannot turn an inference into a first-hand fact.

| Flag                   | Effect                                                                                                                    |
| ---------------------- | ------------------------------------------------------------------------------------------------------------------------- |
| `--include-high`       | also export `sensitivity: high` rows. Off by default: a mirror repository is a wider audience than the namespace's grants |
| `--no-evidence`        | concepts only; skip the raw chunks (`evidence/`) and session digests (`digests/`) the bundle carries by default           |
| `--no-links`           | skip the typed edges (shared-entity links are still written)                                                              |
| `--include-tombstoned` | also export soft-deleted rows, as `status: deprecated`                                                                    |
| `--limit N`            | at most N most-recent rows (smoke runs)                                                                                   |
| `--title`, `--dry-run` | the root index title; plan without writing                                                                                |

## Lint

`loom okf-lint DIR` reads the bundle directory and nothing else: no server, no ClickHouse,
no network. Each finding carries an id, a path, a message and a severity
(`server/memory/domain/okf_lint.py`).

| id                              | Severity | Means                                                                                            |
| ------------------------------- | -------- | ------------------------------------------------------------------------------------------------ |
| `frontmatter`                   | critical | a concept has no frontmatter, unparseable YAML, or an empty `type`                               |
| `required_keys`                 | critical | `title`, `description`, `loom.memory_key` or `loom.memory_type` is missing                       |
| `relates_to`                    | critical | an edge is malformed or its `.md` target does not exist in the bundle                            |
| `index_missing` / `okf_version` | critical | a directory with concepts has no `index.md`; the root index declares no `okf_version`            |
| `staging`                       | critical | a `staging/` directory exists; staging never answers                                             |
| `type_directory_mismatch`       | warning  | `loom.memory_type` disagrees with the folder the file sits in                                    |
| `pointer_unmounted`             | warning  | a `relates_to` path is a `loom://org/namespace/key` pointer to a concept outside the bundle      |
| `unknown_relation`              | warning  | a `relates_to` kind outside Loom's canonical verbs                                               |
| `edited_since_export`           | warning  | the body no longer hashes to `loom.content_sha256` (a concept or raw chunk edited by hand)       |
| `derived_needs_confidence`      | warning  | `source_class: derived` at confidence 1.0: an inference written as a fact                        |
| `high_sensitivity_present`      | warning  | a `sensitivity: high` row (by field or tag) travelled                                            |
| `index_listing`                 | warning  | a concept its directory index or the root index does not list                                    |
| `drift`                         | warning  | the root index's `loom.corpus_digest` no longer matches the concept files                        |
| `orphans`                       | info     | concepts that link to nothing and are linked from nothing (a property of the data, not a defect) |

Exit codes follow what a compile step needs: **2** on any critical finding, **1** on
warnings only, **0** clean. `--strict` makes warnings exit 2 as well (orphans stay
informational); `--json` prints the machine-readable report.

**The corpus digest.** When the root `index.md` carries `loom.corpus_digest`, the lint
recomputes it: take the concept files only, sort them by bundle-relative path, append each
file's SHA-256 hex digest with no separator, and SHA-256 that string. A mismatch is the
`drift` warning, naming how many concept files the tree holds against what the index says.

## Import

`loom import-okf DIR --namespace NS` lints first and refuses a bundle with critical
findings unless `--force-import` (`--no-lint` skips the gate). It then writes each concept
through `memory.set`, with identity from the token or the `--agent`, `--user-id` and
`--session-id` flags, so an import is idempotent by content hash and subject to conflict
detection, the blacklist and the audit log. Afterwards it re-creates `relates_to` edges
with `memory.link` (`--no-links` skips this). The bundle's `evidence/` and `digests/` are
written back through `memory.import_evidence` (`--no-evidence` skips them) and re-embedded
by the importing server.

| Flag              | Effect                                                                                                                                                       |
| ----------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `--verify`        | re-attest concepts whose frontmatter carries a `human:` verification, through `memory.verify`, **as the importing caller**                                   |
| `--keep-sessions` | write each concept under the session in its `loom.session_id`, so a key that lived under several sessions stays several rows; `--session-id` is the fallback |
| `--dry-run`       | plan the writes without sending them                                                                                                                         |

`--verify` never forges an attestation. The server decides the actor from the token; the
bundle's original verifier is recorded in the memory's tags for provenance.

## CI gate

`integrations/okf-ci/okf-lint.yml` is a GitHub Actions workflow for the repository that
holds the bundle:

1. Copy it into that repository as `.github/workflows/okf-lint.yml`.
2. Set `LOOM_REF` to a 40-character Loom commit SHA. The job refuses any other value
   (the file ships with it empty): the rules are versioned with Loom, and a branch or tag
   can move under a green check.
3. Set `OKF_ROOT` to the bundle directory, the one holding the root `index.md` (default
   `.`): one namespace's bundle, or a matrix over the namespaces the repository holds.

The job checks out Loom at `LOOM_REF` beside the bundle and runs the lint with
`uv run --frozen`, so the packages come from that commit's `uv.lock`: one `LOOM_REF` is
one set of rules and one set of dependencies. It runs the lint with `--json`, prints the
summary and the first forty findings (sorted by severity, id and path) into the job log,
uploads `okf-lint.json` as an artifact, and fails on exit 2 or on a lint that did not run
(any other exit code). Warnings pass, with the report kept for the reviewer.

Every action is pinned to a full commit SHA, with its version in a comment. The workflow
asks for `contents: read` only, and neither checkout keeps the token in the git config
(`persist-credentials: false`). The lint itself opens no network connection and needs no
ClickHouse. `integrations/okf-ci/README.md` describes each check.

### Run the gate locally

The same command gives the same result:

```bash
git clone https://github.com/ClickHouse/loom .okf-lint-loom
git -C .okf-lint-loom checkout <LOOM_REF>
uv run --project .okf-lint-loom --frozen --python 3.12 loom okf-lint <OKF_ROOT>          # human report
uv run --project .okf-lint-loom --frozen --python 3.12 loom okf-lint <OKF_ROOT> --json   # the CI report
```

## Limits

- **A bundle is a snapshot.** Changes made in Loom after the export are not in it until
  the next export; re-export before editing a stale bundle.
- **Deletions do not travel.** Removing a concept file does not delete the memory on
  import. Use `memory.delete` for that.
- **High-sensitivity rows stay home** unless you pass `--include-high`: a row is high by
  its `sensitivity` field or its `sensitivity:high` tag. A row whose sensitivity the export
  could not read, or read as a level it does not know, stays home too. So do the raw chunks
  and session digests linked to such a row, and any whose text holds a credential shape (raw
  text has no sensitivity column, so the detector `memory.set` runs decides). The lint warns
  when a high row travelled.

## Related

- [Writing memory](../memory/writing.md) and
  [conflicts and refusal](../memory/conflicts-and-refusal.md): what an import goes through.
- [History and provenance](../memory/history-and-provenance.md).
- [Entity graph](../entity-graph.md): the typed edges `relates_to` carries.
