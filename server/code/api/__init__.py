"""Code intelligence plane — retrieval over a shared, per-merge-fresh index
of a source repository.

Surface: the `code.*` operations `ops.py` and `ops_coverage_watch.py` declare —
retrieval (search, ask, grep), the structural graph (symbol, callers, impact, …),
history, review and index status, the ingest surface and admin ops. Handlers live in
server/code/app/ (server/code/app/symbol/ for the structural ops); retrieval
composition in server/code/app/search/; ALL SQL in server/code/store/index/.

Identity: `namespace=""` resolves to the deployment's code namespace
(LOOM_CODE_NAMESPACE) so agents on personal namespaces land on the indexed
repo without knowing its name. Reads are VIEWER; the namespace is expected
to be org-public-read with the ingest service account as sole writer.

Layout: `models_common` is the leaf every family's wire models build on; each
`models_<family>` module (search, symbol, testing, history, conventions, grep,
navigation, verify, context, review, outcome, status, repos, ingest,
coverage_watch) holds its ops' input and output models and imports only
`models_common` and its siblings; `ops` (and `ops_coverage_watch`) bind the
models to their handlers in `OPERATIONS`.
"""
