# Loom — orientation for coding agents that read AGENTS.md

The project memory for this repository is `CLAUDE.md` at the repo root. Read
it first: repo layout, the wire-op registry, identity tuple, belief-revision
lens, the ReplacingMergeTree write-path rule, provider defaults, and the
`make lint` / `make test-unit` gate. Everything below is the short form.

- `server/` is the FastAPI control plane, one package per domain
  (`docs/architecture/server-layout.md` is the map); ops are declared in
  `server/<domain>/api/ops*.py` and flattened by `server/operations/registry.py`;
  HTTP and MCP transports iterate that registry. `sdk/python` and `sdk/typescript` are generated from it
  (`scripts/codegen/sdk_py_gen.py`, `scripts/codegen/sdk_py_client_gen.py`, `scripts/codegen/sdk_ts_gen.py`; CI runs `--check`).
- Migrations are numbered SQL under `schemas/migrations/`; after changing
  one, `make schema-dump` and commit `schemas/schema.sql`.
- The pull request description carries the why of a change. A design that
  outlives the PR goes into the matching doc under `docs/`, written as the
  current behaviour, not as history.
- The two coding-agent plugins share their hook scripts: edit
  `integrations/claude-code-plugin/`, then `python scripts/codegen/agent_plugin_sync.py`.
- Settings are `server/settings/_<area>.py` mixins; read them as
  `settings.<field>` (never `getattr` with a default); a new field's comment
  says what it controls and what its values mean.
- Workers are rows in `server/workers/registry.py::_WORKERS`.
- The code index's extractor modules (`extractor_stamp.EXTRACTOR_MODULES`)
  carry the index's version stamp: a change to their code re-runs that stage
  over every indexed repository. Comments and docstrings are not part of the stamp.
- `make format`, `make lint`, `make typecheck` and `make test-unit` before
  committing; each runs what CI runs. Tests skip, never hard-fail, when an
  env var is missing.
