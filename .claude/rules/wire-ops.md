---
paths:
  - "server/**/api/**"
  - "server/operations/**"
  - "server/transport/**"
  - "sdk/**"
---

# Wire operations and transports

- Models in `server/<domain>/api/models_<group>.py` import no handler; the `Operation` and its `OPERATIONS` entry live in `ops_<group>.py`. Transports discover ops from the registry; a change that edits transport code to add an op is in the wrong place.
- After any op or field change: `python scripts/codegen/sdk_py_gen.py`, `sdk_py_client_gen.py`, `sdk_ts_gen.py`, `docs_reference.py`, `op_reach_census.py`. Generated SDK files are never edited by hand.
- A field description is what a model reads when it calls the tool: one sentence on what the value does, its unit and its default.
- The default MCP `tools/list` has a token ceiling (`test_mcp_listing_budget.py`); a new tool joins the default listing only when an agent needs it without asking.
- Auth runs before anything that reads or replays state: the idempotency replay is scoped to the caller and checked after auth. Unknown auth state refuses.
- A removed field is served and named in `ignored_args`; the PR's changelog entry names it.
- Guide: [adding-an-operation](../../docs/development/adding-an-operation.md).
