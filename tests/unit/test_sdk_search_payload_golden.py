"""`memory.search` request bodies, byte for byte, over a kwarg matrix: the
defaults, each optional kwarg set on its own, and the paired knobs. A change to
how `search()` builds its body must leave every one of them identical.
Regenerate deliberately with LOOM_UPDATE_SNAPSHOT=1."""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from loom import Agent

GOLDEN = Path(__file__).parent / "snapshots" / "sdk_search_payloads.json"

# One non-default value per search() kwarg (0 / False where that is a value
# the server must see, next to the value meaning "unset").
_ONE_AT_A_TIME: list[tuple[str, Any]] = [
    ("memory_type", "semantic"),
    ("memory_type", ""),
    ("tags", ["a", "b"]),
    ("tags", []),
    ("scope", "shared"),
    ("user_id", "u1"),
    ("user_id", ""),
    ("session_id", "s1"),
    ("run_id", "r1"),
    ("run_id", ""),
    ("agent_id", "bot"),
    ("entity_filter", ["E1"]),
    ("top_k", 20),
    ("search_mode", "semantic"),
    ("alpha", 0.0),
    ("auto_touch", False),
    ("namespace_scope", "all_mine"),
    ("namespace_scope", ""),
    ("bodies", "unit"),
    ("bodies", ""),
    ("body_budget_tokens", 400.0),
    ("body_budget_tokens", 0),
    ("recipe", "quality"),
    ("hyde", False),
    ("hyde", True),
    ("rerank", "builtin:cohere"),
    ("retrieval_budget", "deep"),
    ("retrieval_budget", ""),
    ("category", "ops"),
    ("observation_date", "2026-09-01"),
    ("include_top_n_unmatched", 3),
    ("tags_soft", True),
    ("tag_boost", 0.9),
    ("category_soft", True),
    ("category_boost", 0.9),
    ("confidence_weight", 0.0),
    ("confidence_weight", 0.4),
    ("demote_tags", ["noise"]),
    ("reinforcement_weight", 0.0),
    ("reinforcement_half_life_days", 7.0),
    ("valid_as_of", datetime(2026, 9, 15, 12, 30)),
    ("known_as_of", datetime(2026, 9, 20, 8, 0)),
    ("include_tags", True),
    ("include_usage", True),
    ("include_duplicates", True),
    ("exclude_keys", ["k1"]),
    ("exclude_keys", []),
    ("explain", False),
    ("explain", True),
    ("validity_policy", "strict"),  # a server field through **extra
]
_COMBOS: list[dict[str, Any]] = [
    {"tags": ["a"], "tags_soft": True, "tag_boost": 0.7},
    {"category": "ops", "category_soft": True, "category_boost": 0.2},
    {"agent_id": "bot", "user_id": "u", "session_id": "s", "run_id": "r", "memory_type": "x"},
    {"bodies": "source", "body_budget_tokens": 1000, "explain": True, "top_k": 25},
    {"extra_field": 1, "top_k": 9, "exclude_keys": ["a", "b"]},
]


class _Capture:
    def __init__(self) -> None:
        self.bodies: list[str] = []

    async def post(self, op_path: str, payload: dict[str, Any], **_: Any) -> dict[str, Any]:
        self.bodies.append(json.dumps(payload))
        if op_path == "/v1/run.begin":
            return {"run_id": "run-1", "status": "active"}
        return {"results": []}

    async def close(self) -> None:
        pass


async def _bodies() -> dict[str, str]:
    agent = Agent(org="o", namespace="n", agent="ag", api_key="x", auto_run=False)
    cap = _Capture()
    agent.transport = cap  # type: ignore[assignment]
    cases: list[tuple[str, dict[str, Any]]] = [("defaults", {})]
    cases += [(f"{k}={v!r}", {k: v}) for k, v in _ONE_AT_A_TIME]
    cases += [(" ".join(f"{k}={v!r}" for k, v in c.items()), c) for c in _COMBOS]
    out: dict[str, str] = {}
    for name, kw in cases:
        await agent.memory.search("what broke ci?", **kw)
        out[name] = cap.bodies[-1]
    async with agent.run_scope(goal="g"):  # the builder stamps the scope's run
        await agent.memory.search("q")
        out["inside run_scope"] = cap.bodies[-1]
    return out


async def test_search_bodies_match_the_golden() -> None:
    current = await _bodies()
    if os.environ.get("LOOM_UPDATE_SNAPSHOT") == "1":
        GOLDEN.write_text(json.dumps(current, indent=2) + "\n")
    golden = json.loads(GOLDEN.read_text())
    changed = sorted(k for k in set(current) | set(golden) if current.get(k) != golden.get(k))
    assert not changed, {k: (golden.get(k), current.get(k)) for k in changed[:5]}
