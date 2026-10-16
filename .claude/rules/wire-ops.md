---
paths:
  - "server/**/api/**"
  - "server/operations/**"
  - "server/transport/**"
  - "sdk/**"
  - "server/auth/**"
  - "server/observability/budgets.py"
---

# Wire operations, transports and auth

- `server/operations/base.py` defines `Operation`, `Role` and `grant_rank`; `server/transport/mcp_primer.py` renders the primer and answers `prompts/*` / `resources/*`; `server/observability/budgets.py::effective_budget_ms` resolves each op's latency budget (runtime and per-consumer overrides).
- Models in `server/<domain>/api/models_<group>.py` import no handler; the `Operation` and its `OPERATIONS` entry live in `ops_<group>.py`. `server/operations/registry.py` imports each list, and the handler in `server/<domain>/app/` imports its models at module top. Transports discover ops from the registry; a change that edits transport code to add an op is in the wrong place.
- After any op or field change: `python -m scripts.codegen.<gen>` for `sdk_py_gen`, `sdk_py_client_gen`, `sdk_ts_gen`, `docs_reference`, `op_reach_census`. Generated SDK files are never edited by hand.
- A field description is what a model reads when it calls the tool: one sentence on what the value does, its unit and its default.
- The default MCP `tools/list` has a token ceiling (`test_mcp_listing_budget.py`); a new tool joins the default listing only when an agent needs it without asking.
- Auth fails closed and runs first: a revocation list not yet loaded, a missing grant or an unverifiable token refuses; an admin gate reads the effective auth mode, not a default; the idempotency replay is scoped to the caller and checked after auth.
- A removed field is served and named in `ignored_args`; the PR body's changelog entry names it.
- Guide: [adding-an-operation](../../docs/development/adding-an-operation.md).
