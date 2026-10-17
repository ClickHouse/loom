---
paths:
  - "server/llm/**"
  - "server/embedding/**"
  - "server/settings/_models.py"
---

# LLM and embedder providers

- Dev and test defaults are fake providers (no network, no key). A change to a default model or embedder needs `make bench-quick` evidence first, and a run only on request (`loom-bench`).
- Every setting that names a model lives in `server/settings/_models.py`, one per job.
- Switching the embedder changes the vector space: vectors are rebuilt (Settings → Models & keys, or `loom reembed` at the same width), never mixed.
- `LOOM_LLM_PROVIDER` is resolved once when `Settings` loads, from the model name or the key present when unset; an explicit value is never overridden, and a missing key falls back to the fake client with a `loom doctor` warning ([LLM architecture](../../docs/architecture/llm-architecture.md)).
