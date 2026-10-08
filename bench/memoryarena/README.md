# MemoryArena through loom

MemoryArena scores a memory system by whether it lets an agent act
correctly in LATER sessions of a task whose subtasks depend on each other.
Its gym talks to a memory system over three HTTP calls — `initialize`, `add`
(a finished subtask's trace), `wrap_user_prompt` (the next subtask's prompt,
wrapped with retrieved memory) — and to an environment server that judges
each subtask.

This directory holds loom's side of that contract:

- `loom_memory.py` — `LoomMemorySystem` for the gym's `memory_systems`
  package. Two modes. `verbatim`: each chunk is stored as-is with
  `memory.set` and retrieved with `memory.search` (hybrid, top-3) — the RAG
  shape, on loom's ranking. `extract`: chunks go through
  `memory.set_from_messages`. One fresh namespace per gym task (or one shared
  namespace with a `run_id` per task); every call's latency is appended to
  `LOOM_MA_LATENCY_LOG`.
- `memory_server.py` — a drop-in for the gym's `memory/server.py` that
  serves the `loom-*` arms plus `long_context`, `bm25` and
  `text-embedding-3-small` without importing third-party memory SDKs.
- `summarize.py` — SR, PS, SR@k and seconds per subtask from the gym's
  `result.jsonl` files, in the benchmark's shape.
- `scope_probe.py` — replays stored lemmas into one namespace per task and
  into one shared namespace tagged `run_id=<task>`, and reports how often a
  search returns rows from another task or namespace.
- `travel/` — the group-travel environment: a runner patch, an arm config,
  `score.py` and `ledger.py` (below).

Loom's `episodic` memory type is session-scoped and not searchable; the
plugin stores as `semantic` on purpose.

## Run (formal reasoning, the environment that needs no external data)

```bash
git clone https://github.com/ZexueHe/MemoryArena.git && cd MemoryArena
cp <loom>/bench/memoryarena/loom_memory.py memory/memory_systems/
cp <loom>/bench/memoryarena/memory_server.py memory/server_loom.py
uv venv .venv && uv pip install --python .venv/bin/python fastapi uvicorn tiktoken \
    rank_bm25 openai datasets requests numpy sympy scipy python-dotenv google-genai google-generativeai
export OPENAI_API_KEY=... OPENAI_BASE_URL=https://api.openai.com/v1
export LOOM_BASE_URL=http://127.0.0.1:7777 LOOM_ORG=dev \
       LOOM_API_KEY="$(loom mint-token --subject memoryarena --org dev --namespaces '*' --roles writer --ttl-days 3)"
(cd env && ../.venv/bin/python env_server.py &)       # :8001
(cd memory && ../.venv/bin/python server_loom.py &)   # :8000
# configs/formal_reasoning_configs/*.json: set agent.temperature to 1.0 (gpt-5-mini accepts only the default)
MA_LIMIT=20 .venv/bin/python run_math.py -c configs/formal_reasoning_configs/math_loom-verbatim_slice.json
python <loom>/bench/memoryarena/summarize.py --results results/json/math
```

Compare arms within one run: the absolute level depends on the task agent and
judge configuration. `summarize.py` also prints the share of common subtasks
wrong under every arm and each arm's accuracy on the rest — that remainder is
the denominator a read-side change can move.

Pin `namespace_scope="this"` on every arm. Under the server default
(`all_mine`) a token that can read other namespaces fans each search out over
all of them, and rows from unrelated benches land in the prompt.

## Arms

Arm names in `memory_server.py` encode the knobs they turn:

| knob             | what it does                                                               |
| ---------------- | -------------------------------------------------------------------------- |
| `-unit`          | one memory per subtask, whole bodies read under a token budget             |
| `-recent`        | `recent_first: 1` — the run's most recent memory is always the first hit   |
| `-ledger`        | a client-side results ledger listed before the retrieved bodies            |
| `-this` / `-run` | `namespace_scope="this"`; one namespace per task, or `run_id` per task     |
| `-shared`        | ONE namespace for every group, separated by `run_id`                       |
| `-names*`        | traveler tags on each trace; reads filter by the names the query cites     |
| `-ents*`         | the traveler written as the memory's `entities`; the server does the match |
| `extract-actor*` | extraction under the traveler's `user_id` (`actor_ids`); `-k25` = top_k 25 |

## Group travel (the value-reuse environment)

`travel/` holds the runner patch (`MA_LIMIT`, per-round reward / steps /
seconds / retrieved context under `rounds`), an arm config, and `score.py`,
which scores saved groups with the gym's own judge: PS = full-plan pass,
sPS = constraint-slot pass (the benchmark's soft progress score; PS is near
zero for every system). Run it inside the gym's venv (`--gym <checkout>`).
Loom arms that received an empty context (a 5xx from storage, failed open)
are excluded and counted.

Run the loom arms with one `run_id` per group and the `run_id` filter on
search, inside a namespace that already exists: that is the production shape
of a shared store and registers no new namespaces.

## Loss ledger — every failed slot, by mechanism

`bench/memoryarena/travel/ledger.py --gym <checkout> ARM…` prints one line per
failed constraint slot with the constraint sentence, the travelers it cites,
the gold and agent values, whether each cited traveler's trace (whole-trace
arms) or rows (extraction arms) reached the window, whether the referenced
value did, whether the cited traveler's own stored plan already deviated
from gold, and whether the long-context arm passed the slot. Classes:

| class          | meaning                                                                                                                                                                |
| -------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| agent floor    | long context fails the slot too                                                                                                                                        |
| own constraint | no traveler cited; the agent's own search failed                                                                                                                       |
| inherited      | the cited traveler's stored value differs from gold — the judge scores against gold predecessors, so a memory that faithfully returns what was decided is marked wrong |
| retrieval miss | the cited traveler's trace / rows are not in the window                                                                                                                |
| right-row miss | the actor is in the window, the referenced value is not                                                                                                                |
| reasoning      | everything needed is in the window                                                                                                                                     |

Only retrieval misses, right-row misses and reasoning failures with the
reference present are attributable to memory; the agent floor and own
constraints belong to the planner, and inherited failures to the judge.

## Two judges: gold and memory-faithful

`score.py` prints `sPS` (the gym's judge: each slot against the gold plan,
which assumes every earlier traveler chose the gold venue) and `fPS`
(memory-faithful): a join or same-place slot also passes when the agent
matched what the cited traveler _actually stored_ in that run. Numeric
relations keep the gold verdict. The gap between the two is the cascade the
memory could not have removed.
