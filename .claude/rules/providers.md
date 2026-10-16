---
paths:
  - "server/llm/**"
  - "server/embedding/**"
  - "server/settings/_models.py"
---

# LLM and embedder providers

- Dev defaults are fake providers (no network/key) at Loom's standard 3072-vector width. Production sets `LOOM_EMBEDDING_MODEL=text-embedding-3-large` + `LOOM_LLM_MODEL=gpt-6-luna` via env/compose. **Run `make bench-quick` before changing either default.**
- An Anthropic LLM provider exists (`server/llm/anthropic.py`, `LOOM_LLM_PROVIDER=anthropic` + `ANTHROPIC_API_KEY`) — opt-in; clear `make bench-quick` first. Embedder providers: OpenAI, Google Gemini (`gemini-embedding-001`, 3072-dim, `GEMINI_API_KEY`/`GOOGLE_API_KEY`), a local CPU embedder (`local`/`local:*`, quality-for-latency trade), and fakes. Switching the embedder changes the vector space — a full re-embed is required: Settings → Models & keys rebuilds the vector tables at the new width; `loom reembed` regenerates vectors from stored content and preserves metadata, but only at the width coded vector tables were created at.
- **`LOOM_LLM_PROVIDER` is inferred when unset** (like the *embedder*, whose provider is inferred from `LOOM_EMBEDDING_MODEL`): from `LOOM_LLM_MODEL`'s name (`gpt*`/`o1`/`o3`/`o4`/`chatgpt*` → openai, `claude*` → anthropic, `fake*` → fake), else from the key present (`OPENAI_API_KEY` → openai + `gpt-6-luna`, `ANTHROPIC_API_KEY` → anthropic + `claude-haiku-4-5`), else fake. `Settings` resolves it once at load (`settings.llm_provider` is always concrete; `settings.llm_provider_inferred()` / `llm_provider_source()` say how), the boot log prints one INFO line, and `loom doctor`'s `llm_provider` line WARNs when the resolved provider's key is missing (the runtime then falls back to the fake client). An explicit value is never overridden — the stock `.env` pins `LOOM_LLM_PROVIDER=fake`, so a checked-in dev stack stays fake until you swap the provider block; `make bench-quick` defaults are unchanged.
