# Loom in the MemoryData harness

The MemoryData testbed (github.com/OpenDataBox/MemoryData) ships many method
presets, four benchmark families and one `main.py`. Running loom through it
puts loom's row on the harness's own axes — EM / F1 / ROUGE-L, source-id
Recall@k, memory-construction and query time — with the same reader and the
same chunking as every other method it runs. This folder holds the adapter
and the recipe; the harness itself is not vendored.

## Files

- `loom_adapter.py` — the method adapter (`add_chunk`, `retrieve`, `ask`,
  `finalize`). Two arms via `loom_mode`: `verbatim` (one `memory.set` per
  4,096-char chunk, the harness's `[LOCOMO_META …]` header kept in the value)
  and `extract` (one `memory.set_from_messages` per chunk under
  `session_id = chunk_id`; retrieve() re-attaches the source ids from the
  hit's `session_id`). Reads use `memory.search` with `bodies=unit` so whole
  chunks reach the reader.

## Wiring (one checkout, ~15 minutes)

1. `git clone https://github.com/OpenDataBox/MemoryData && cd MemoryData`;
   copy `loom_adapter.py` to `methods/loom/loom_adapter.py` and add an empty
   `methods/loom/__init__.py`.
2. The harness dispatches on substrings of `agent_name`; add `"loom"` to
   every memory-agent name list: `utils/agent.py` (`_initialize_agent_by_type`,
   `_supports_locomo_recall`, `send_message`'s memory-agent branch,
   `_handle_memory_agent`'s elif chain, `save_agent` and `load_agent` gates),
   `utils/initialization.py` (the marker check in
   `initialize_and_memorize_agent`, `_apply_chunk_size_ablation`, the
   artifact-name components), `utils/artifact_paths.py`
   (`MEMORY_AGENT_NAMES`), `utils/conversation_creator.py`
   (`MEMORY_AGENT_NAME_HINTS`).
3. Add `_initialize_loom_agent` (build the adapter from `loom_*` config keys
   and an OpenAI client for the shared answer step) and `_handle_loom_agent`
   (a copy of the raw-text adapter's handler: `add_chunk` when memorizing,
   else `retrieve` → source-id groups → `_fit_memories_for_answer` →
   `_generate_answer_from_memories`) next to their siblings in
   `utils/agent.py`; the save/load branches write and check
   `loom_ready.txt`.
4. Presets: `config/hybrid_loom_verbatim.yaml` / `hybrid_loom_extract.yaml`
   with `agent_name: Agentic_memory_loom_<mode>`, `retrieve_num: 10`,
   `agent_chunk_size: 4096`, `loom_mode`, `loom_search_mode: semantic`,
   `loom_bodies: unit`, `loom_body_budget_tokens: 6000`,
   `loom_token_env: LOOM_API_KEY`, `loom_base_url`, `loom_namespace_prefix`,
   `provider: openai`, `model: gpt-4.1-mini`.
5. Dataset: a config over the public `locomo10.json` with
   `locomo_categories: [1, 2, 3, 4]` (1,540 questions) is the comparable run; drop the JSON at `datasets/LoCoMo/locomo10.json`.
6. Env: `LOOM_API_KEY`, `LOOM_BASE_URL` (default `http://127.0.0.1:7777`),
   `LOOM_ORG` (default `dev`), `OPENAI_API_KEY`. Venv: the harness imports
   torch, transformers and langchain-core at module level; CPU torch plus
   `pyarrow` (post-run report) is enough.

```bash
python main.py --agent_config config/hybrid_loom_verbatim.yaml \
  --dataset_config benchmark/locomo/config/Locomo_tracker_4cat_full_locomo10.yaml --force
```

Results land under `results/outputs/<model>-loom-<mode>/LoCoMo/*_results.json`
with per-question `f1`, `exact_match`, `rougeL_f1`, `rougeL_recall`,
`locomo_recall@{1,5,10}`, `memory_construction_time`, `query_time_len`.

## Gotchas

- Point reads are session-scoped: a row written with `session_id = X` is
  `found: false` for `memory.get` under session `-`. The verbatim arm
  therefore writes without a per-chunk session; the extract arm keeps it
  (the source-id map needs it).
- Loom JSON-encodes string values: excerpts and unit bodies come back
  quoted with `\\n`; decode before parsing the META header.
- The harness prefixes every query with its retrieval template ("Search
  Archival Memory and answer …") for every method; the adapter keeps that
  parity rather than stripping it.
- `memory.set` needs `org` in the identity; the adapter defaults it to
  `LOOM_ORG` or `dev`.

## Reading the two arms

The extract arm runs the same chunks through loom's fact plane. An extracted
fact is a small unit that lands first, so Recall@1 and single-hop answers
gain; temporal answers can lose even as recall rises when extraction drops or
re-anchors a date; and multi-hop source recall can fall even as its F1 rises,
because ten small facts cover fewer source turns than ten chunks. Report
construction time beside accuracy: extraction dominates the extract arm's
wall clock.
