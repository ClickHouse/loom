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

| Memory                             | Concept                                                                                                                                                                                                                                                                                                                                                        |
| ---------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `memory_key`                       | file name and `loom.memory_key`: the key's slug, capped at 160 bytes, with a `-<hash>` of the key and row id when another key holds that name or it is a bundle file's (`index`, `log`, `README`, `AGENTS`, in any case) or a Windows device name (`nul`, `con`, `com1`)                                                                                       |
| `memory_type`                      | directory (the type's lowercased slug, capped like a file name; `type-<slug>` for `evidence`, `digests`, `staging`, `node_modules`, a bundle file's full name (`index.md`, `log.md`) or a Windows device name such as `nul`) and `type`: `semantic` → Fact, `procedural` → Procedure, `preference` → Preference, `episodic` → Event, anything else title-cased |
| value                              | body: text as is, JSON as a fenced block                                                                                                                                                                                                                                                                                                                       |
| `tags`                             | `tags`                                                                                                                                                                                                                                                                                                                                                         |
| agent and `created_at`             | `generated: {by: "agent:<agent>", at}`                                                                                                                                                                                                                                                                                                                         |
| live `verified_by` / `verified_at` | `verified: [{by, at}]` (human and process attestations only)                                                                                                                                                                                                                                                                                                   |
| `status`                           | `active` → `stable`, `conflicted` → `draft`, anything else → `deprecated`                                                                                                                                                                                                                                                                                      |
| `expires_at`                       | `stale_after`, a review-by date                                                                                                                                                                                                                                                                                                                                |
| `source_resource_id`               | `sources: [{resource}]`                                                                                                                                                                                                                                                                                                                                        |
| everything Loom-specific           | the `loom:` block, including `source_class`, `sensitivity`, `superseded_by`, `valid_to` and `content_sha256`                                                                                                                                                                                                                                                   |

**Links.** Edges someone authored (the namespace's typed `memory.link` edges, read with
`memory.export_evidence kind=edges`, and the `supersedes` chain a superseded row implies)
are written as authoritative frontmatter `relates_to: [{path, kind}]` and mirrored in the
body's "Related Concepts" section. Shared-entity links follow, capped and symmetric, so
the bundle has a graph a validator can walk. Concepts that link to nothing remain orphans,
and the CLI reports them rather than hiding them.

**Provenance travels.** The `loom:` block carries the row's provenance tier so a round
trip cannot turn an inference into a first-hand fact.

| Flag                   | Effect                                                                                                                                                                                                                                                                                                                                                                                                |
| ---------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `--include-high`       | also export `sensitivity: high` rows, the raw chunks and digests linked to them, and raw text that holds a high shape (a credential, card number, SSN or stated PIN); the server audits each released item; `memory.bulk_get` does not serve a `high` row past its retention window, so it is named and left out. Off by default: a mirror repository is a wider audience than the namespace's grants |
| `--no-evidence`        | concepts only; skip the raw chunks (`evidence/`) and session digests (`digests/`) the bundle carries by default                                                                                                                                                                                                                                                                                       |
| `--no-links`           | skip the typed edges (shared-entity links are still written)                                                                                                                                                                                                                                                                                                                                          |
| `--include-tombstoned` | also export soft-deleted rows, as `status: deprecated`; one the list does not carry in full (a value past the preview, or a `high` row under `--include-high`) is named and left out                                                                                                                                                                                                                  |
| `--limit N`            | at most N most-recent rows (smoke runs)                                                                                                                                                                                                                                                                                                                                                               |
| `--title`, `--dry-run` | the root index title; plan without writing                                                                                                                                                                                                                                                                                                                                                            |

## Lint

`loom okf-lint DIR` reads the bundle directory and nothing else: no server, no ClickHouse,
no network. Each finding carries an id, a path, a message and a severity
(`server/memory/domain/okf_lint.py`).

| id                              | Severity | Means                                                                                                |
| ------------------------------- | -------- | ---------------------------------------------------------------------------------------------------- |
| `frontmatter`                   | critical | a concept has no frontmatter, unparseable YAML, an empty `type` or a `tags` value that is not a list |
| `required_keys`                 | critical | `title`, `description`, `loom.memory_key` or `loom.memory_type` is missing                           |
| `relates_to`                    | critical | an edge is malformed or its `.md` target does not exist in the bundle                                |
| `index_missing` / `okf_version` | critical | a directory with concepts has no `index.md`; the root index declares no `okf_version`                |
| `staging`                       | critical | a `staging/` directory exists; staging never answers                                                 |
| `type_directory_mismatch`       | warning  | `loom.memory_type` disagrees with the folder the file sits in                                        |
| `pointer_unmounted`             | warning  | a `relates_to` path is a `loom://org/namespace/key` pointer to a concept outside the bundle          |
| `unknown_relation`              | warning  | a `relates_to` kind outside Loom's canonical verbs                                                   |
| `edited_since_export`           | warning  | the body no longer hashes to `loom.content_sha256` (a concept or raw chunk edited by hand)           |
| `derived_needs_confidence`      | warning  | `source_class: derived` at confidence 1.0: an inference written as a fact                            |
| `high_sensitivity_present`      | warning  | a high row (`loom.sensitivity: high` or the `sensitivity:high` tag) travelled                        |
| `index_listing`                 | warning  | a concept its directory index or the root index does not list                                        |
| `drift`                         | warning  | the root index's `loom.corpus_digest` no longer matches the concept files                            |
| `orphans`                       | info     | concepts that link to nothing and are linked from nothing (a property of the data, not a defect)     |

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
`--session-id` flags, subject to conflict detection, the blacklist and the audit log.
Afterwards it re-creates `relates_to` edges with `memory.link` (`--no-links` skips this).

Before writing, the import reads the live rows under its identity and skips every concept
whose row already holds what the write would send: the value (`content_sha256`), type,
tags, confidence, `valid_at`, `valid_to`, `source_trace` and `source_class` (the declared
class, else `imported`), and the entities when the bundle carries them. It skips edges
that are already live. Concepts that land on the same row (one key exported for several
writers) are written once: the most recently updated wins. Re-sending an identical row
would count as a reinforcement, so importing the same bundle twice writes no concept or
edge the second time, except a concept with `sources:`, which is written on every run
because the reads the import makes do not return its source, and a concept the target
merges into another key, which is sent again on every run and reinforces that row (counted
as merged). The last line counts the target rows written, unchanged, merged and failed.

`source_trace` is `okf:<path inside the bundle>`, so where the bundle sits on disk does
not change it: each run can export to a fresh directory.

The bundle's `evidence/` and `digests/` are written back through `memory.import_evidence`
(`--no-evidence` skips them) and re-embedded by the importing server on every import,
changed or not: a scheduled import passes `--no-evidence`.

| Flag              | Effect                                                                                                                                                                                  |
| ----------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `--verify`        | re-attest concepts whose frontmatter carries a `human:` verification, through `memory.verify`, **as the importing caller**                                                              |
| `--keep-sessions` | write each concept under the session in its `loom.session_id`, so a key that lived under several sessions stays several rows; `--session-id` is the fallback                            |
| `--dry-run`       | plan the writes against the target's live rows (it reads them) without sending any write                                                                                                |
| `--prune`         | delete the rows an earlier import with this `--agent` and token wrote (an `okf:` `source_trace`) whose key is no longer in the bundle, through `memory.delete_version`; needs `--agent` |

Each bundle imported into a namespace with `--prune` needs an `--agent` of its own: prune
deletes the rows of that agent with an `okf:` trace, written by its token, that the bundle
lacks. `--prune` refuses a bundle with no concepts and one whose concept files do not hash
to the root index's `loom.corpus_digest`, so a hand-edited bundle never drives deletes. A
bundle whose root index does not carry `loom.complete: true` is written but not pruned.
Export writes the marker only when it read every row: every row `memory.keys` reads in
one statement after the listing was listed, and every listed row came back from
`memory.bulk_get`. Never when it stopped at `--limit`, with `--include-tombstoned` (which
keeps deleted rows in the bundle), or from an older release. Each delete goes through
`memory.delete_version` with the version the import's listing saw with an `okf:` trace,
under that exact identity: a row rewritten since, or locked with `memory.lock`, is kept,
and a target too old to know the op deletes nothing. Export into an empty directory for a
prune: export overwrites concept files but never removes the file of a key that left the
namespace, so a reused `--out` directory no longer matches its digest. [Copying a
namespace to another Loom](../../operations/okf-sync.md) runs export and import on a
schedule.

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
- **Deletions travel only with `--prune`,** and only for rows the import wrote. Edges
  removed from the bundle are not deleted.
- **High-sensitivity rows stay home** unless you pass `--include-high`: a row is high by
  its `sensitivity` field or its `sensitivity:high` tag. A row whose sensitivity the export
  could not read, or read as a level it does not know, stays home too. So do the raw chunks
  and session digests linked to such a row, and any whose text holds a high shape (a
  credential, card number, SSN or stated PIN; raw text has no sensitivity column, so the
  detector `memory.set` runs decides, whatever `LOOM_MEMORY_SENSITIVITY_AUTODETECT`
  says). The lint warns when a high row travelled.

## Related

- [Writing memory](../memory/writing.md) and
  [conflicts and refusal](../memory/conflicts-and-refusal.md): what an import goes through.
- [History and provenance](../memory/history-and-provenance.md).
- [Entity graph](../entity-graph.md): the typed edges `relates_to` carries.
