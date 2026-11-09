# Add language support to the code index

The code index reads a repository in two tiers:

- The **fast tier** (tree-sitter grammars and hand-written scanners) runs on every file at every commit. It produces chunks, symbols and call edges.
- A **compiler tier** (a SCIP indexer over changed files) adds compiler-exact symbols and edges where an indexer exists.

A language is supported when the fast tier has a lane for it. [Code index](../features/code-index/overview.md) explains the tiers and lists what each language gets.

## The one rule: stay outside the extractor stamp

Fast-tier rows carry a version stamp of the code that produced them (`server/code/domain/extract/extractor_stamp.py`):

- The stamp is a sha256 over the **code tokens** (`logic_tokens`: no comments, blank lines or docstrings; an import counts as the names it binds, not the package path) of every module in `STAGE_MODULES`, plus the installed versions of the grammars in `STAMPED_GRAMMARS` (`tree-sitter`, `tree-sitter-cpp`, `tree-sitter-python`, `tree-sitter-bash`).
- When the stamp moves, the ingest worker re-derives the moved stages (`chunk`, `symbol`, `edge`, `guard`) in paced batches for every file of every indexed namespace in the affected language families (`_maybe_rechunk` in `server/code/app/index/ingest_pass.py`).
- A module mapped to one family in `MODULE_LANGUAGES` (`lang_python.py`, the `ts_*` modules) moves only that family's files. A shared module (`treesitter.py`, `chunker.py`) or the core `tree-sitter` grammar moves every file.
- On a large deployment that is hours of ingest.

Add a new language where it moves no stamp:

| File                                                           | In the stamp? | What goes there                                                                                             |
| -------------------------------------------------------------- | ------------- | ----------------------------------------------------------------------------------------------------------- |
| `server/code/domain/extract/lang_<lang>.py`                    | no            | The lane: `parse_<lang>(path, source) -> ParsedFile` with symbols, includes and call sites.                 |
| `server/code/domain/extract/lang_detect.py`                    | no            | `LANG_BY_EXTENSION` (and the shell rules): which files are this language.                                   |
| `server/code/app/index/ingest_parse.py`                        | no            | A `_parse_<lang>` wrapper in `_PARSERS`; include resolution in `ingest_resolve.py` if the lane has imports. |
| `server/code/domain/extract/lane_units.py`                     | no            | Shared helpers for a lane **without** a grammar: `span_chunks` and the `bind_sites` reference binder.       |
| `server/code/domain/extract/treesitter.py`                     | **yes**       | `lang_for_path` for the stamped core languages only. Adding an extension here re-chunks every namespace.    |
| `chunker.py`, `edges_treesitter.py`, the other `STAGE_MODULES` | **yes**       | Shared chunking and edge extraction. Change them only when the extraction itself changes for everyone.      |

Languages `lang_for_path` does not name are detected in `lang_detect.py`, so adding one moves no stamp. Comments and docstrings in stamped modules are free to edit. Those modules are excluded from formatting, because a formatter could rewrite string quotes, which are code tokens.

Every module under `server/code/domain/extract/` is either in a stage of `STAGE_MODULES` or named, with a reason, in `NON_EXTRACTOR_MODULES` in `tests/unit/test_code_extractor_stamp.py`. Put a new lane module in `NON_EXTRACTOR_MODULES`, or that test fails.

## 1. Choose the lane's shape

| Shape             | Examples                                                                                                   | You provide                                                                                                                                                                 |
| ----------------- | ---------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Grammar lane      | `lang_typescript.py`, `lang_rust.py`, `lang_go.py`, `lang_java.py`                                         | A tree-sitter grammar dependency in `pyproject.toml`; symbols and call sites from the tree; chunks and edges come from the shared chunker and edge walker.                  |
| Tree-less lane    | `lang_proto.py`, `lang_gotemplate.py`, `lang_hcl.py`, `lang_k8s.py`, `lang_jsonnet.py` (`LANE_UNIT_LANGS`) | A hand-written scanner; `ParsedFile.tree` stays `None`; chunks from `lane_units.span_chunks`, edges from `reference_sites` + `SITE_RULES` bound by `lane_units.bind_sites`. |
| Include-only lane | `tla`, `flatbuffers`, `cmake`, `doc` (`_LANE_INCLUDE_LANGS`)                                               | Symbols and includes resolved by the simple lane resolver.                                                                                                                  |
| Embedded language | SQL inside Go and Java (`go_sql_units`, `java_sql_units`), shell islands in Dockerfiles and Makefiles      | Units joined into the host file's parse (`_join_island_symbols`), usually behind a kill switch setting.                                                                     |
| Chunk-only        | `.css`, text files (`lang_text.py`)                                                                        | Nothing but detection: the file is chunked and searchable with no symbols.                                                                                                  |

A grammar lane's grammar is **not** in `STAMPED_GRAMMARS`, so upgrading it moves no stamp. The lane owns re-deriving its rows.

## 2. Write the parser

`parse_<lang>(path: str, source: bytes) -> ParsedFile` returns the language tag, the symbols (`treesitter.Symbol`: qualified name, kind, span, signature), the includes and what the edge stage needs. Follow the nearest existing lane:

- **Qualified names** follow the language's module path (Rust: the crate path from the file path; `mod.rs` / `lib.rs` name their directory). A symbol id hashes the qualified name and normalized signature, so an unstable name churns rows on every re-index.
- **Includes** resolve against the paths the namespace holds (`wanted_paths`, then `resolve_imports` / `resolve_uses` in the lane, called from `ingest_resolve.py`). External packages resolve to nothing, not to a guess.
- **Call edges** bind same-file-unique names first, then repository-unique ones at lower confidence, and nothing when a name is ambiguous (`lane_units.bind_sites` does this for tree-less lanes).
- **Container kinds** (a class, an impl block) belong in the lane. Registering one in the chunker's per-language table changes `chunker.py` and moves the stamp.

Register the lane:

```python
# server/code/app/index/ingest_parse.py
def _parse_rust(path: str, source: bytes) -> ParsedFile:
    from server.code.domain.extract.lang_rust import parse_rust

    return parse_rust(path, source)


_PARSERS = {
    ...
    "rust": _parse_rust,
    ...
}
```

and add the extension to `LANG_BY_EXTENSION` (`".rs": "rust"`). The wrapper imports the lane lazily, so an optional grammar stays optional. A new tree-sitter grammar also goes in `bounded_parse._GRAMMARS`, which every parse runs through first.

## 3. Test the lane

- **A lane test**, `tests/unit/test_code_lang_<lang>.py`, parses small inline sources and asserts names, kinds, spans, includes and edges. Skip when the grammar is absent: `pytest.importorskip("tree_sitter_rust")`. `test_code_lang_rust.py` and `test_code_lang_typescript.py` are the models.
- **The lanes corpus.** Add fixtures under `tests/fixtures/code_lanes/<lang>/`: synthetic, or open-source excerpts under their own licence, listed in `tests/fixtures/code_lanes/NOTICE.md`. `tests/unit/test_code_lanes_corpus.py` derives languages, symbols, facets, includes and edges from every fixture and compares them with `tests/fixtures/code_lanes/expected.json`. Regenerate and review the diff:

  ```bash
  python -m tests.code_lanes.corpus --write
  git diff tests/fixtures/code_lanes/expected.json
  ```

  A number that moves there changes what every namespace holds. Review it.

- **The stamp test.** `tests/unit/test_code_extractor_stamp.py` with the new module in
  `NON_EXTRACTOR_MODULES`.
- **Retrieval.** For a lane that should answer `code.search` or `code.symbol` well, run the code-intelligence gate before and after. `make bench-code-intel-dry` checks the question set offline; `make bench-code-intel` runs it against a server with a real embedder, never the fake one ([running benchmarks](../benchmarks/running.md)).

## 4. Bring existing namespaces up to date

A lane outside the stamp changes nothing already indexed. New commits use the lane; old files keep their previous rows (often chunk-only). Re-derive them with a targeted backfill:

```bash
git -C <checkout of the repository> ls-files '*.rs' > rust-paths.txt
loom code backfill --org <org> --namespace <code-namespace> --paths-file rust-paths.txt
```

`--paths-file` re-ingests only those repo-relative paths at the branch tip and leaves the watermark alone. Run it where the server's `.env` and the repository mirrors are, such as the workers container ([code index operations](../operations/code-index-operations.md)).

> **Warning:** if a change must touch a stamped module (a `chunker.py` fix, a `treesitter.py` extension), say so in the PR. Every deployment that takes it re-derives the moved stages for every file of the affected families in every namespace. A `chunker.py` change moves only `chunk`; an `edges_treesitter.py` change moves `edge`, `chunk` and `guard`; a parser change moves every stage.

## 5. Add a compiler tier (optional)

A compiler tier pays off when fast-tier edges are ambiguous (overloads, dynamic dispatch, re-exports) and a SCIP indexer exists. `TIERS` in `server/code/domain/index/compiler_tiers.py` lists the tiers. Each `TierSpec` names the indexer (`scip-clang`, `scip-typescript`, `scip-go`, `scip-python`), its `code_index_state` watermark key (`scip_commit`, `scip_commit_ts`, `scip_commit_go`, `scip_commit_py`) and the workers that move it. A new tier needs:

1. A harness that runs the indexer over the changed files of a commit range and converts
   its output to Loom's JSON delta: `scripts/scip_harness/` (`run_delta.sh`,
   `run_typescript_delta.sh`, `run_python_delta.sh`, `run_go_monorepo_delta.sh`,
   `scip_to_delta.py`).
2. A delta worker (`server/code/workers/scip_ts_delta.py`, `scip_py_delta.py` and
   `scip_go_delta.py` are the models): its `run_pass` hands its `_run_repo` to
   `run_tier` in `server/code/app/index/compiler_tier.py`, which owns the entry loop, failure
   isolation and the full-run reset. It comes with its `LOOM_CODE_SCIP_<LANG>_DELTA_ENABLED` /
   `_INTERVAL_SECONDS` / `_TIMEOUT_SECONDS` settings
   ([adding a worker](adding-a-worker.md)). An external producer uploads through
   `code.scip_ingest` instead, and the `code_scip_apply` worker applies complete batches.
3. A watermark key in `code_scip_apply`'s `watermark_key_for` and a `TierSpec` row, so
   `code.index_status` and the dashboard report the tier.

Compiler-tier rows carry `tier='scip'`. Reads merge them with the fast tier by qualified name, preferring the compiler tier. Files the indexer skips, and commits before their delta lands, still read from the fast tier.

## Related

- [Code index overview](../features/code-index/overview.md)
- [Code index architecture](../architecture/code-index-architecture.md)
- [Code index operations](../operations/code-index-operations.md)
- [Extending Loom](extending-loom.md)
