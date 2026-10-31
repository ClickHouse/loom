"""Decisions as first-class records.

A decision is a typed record (schema `decision`, built in and registered on
first use per namespace) PLUS a searchable memory row under the same key
(memory_type="decision") PLUS authored edges to what caused or influenced it.
Nothing here is a new store: precedents are memory.search over the decision
type, the chain is a walk over loom.operator_edges, the outcome is a record
revision that also credits the memories the deciding run relied on.

Pure helpers (key, field shaping, chain BFS over an edge fetcher) are kept
free of ClickHouse so they are unit-testable; the handlers own the I/O.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from server.memory.domain.decision_schema import SCHEMA_FIELDS, SCHEMA_NAME

CHAIN_RELATIONS: tuple[str, ...] = ("caused", "influenced", "derived_from")
MAX_ALTERNATIVES = 10
MAX_CHAIN_NODES = 200


def decision_key(category: str, scenario: str) -> str:
    """The logical key: the same derivation the record plane uses for identity
    fields, so record and memory row share one key."""
    from server.memory.domain.record_identity import canonical_record_key

    return canonical_record_key(SCHEMA_NAME, {"category": category, "scenario": scenario})


def clean_alternatives(items: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for a in items or []:
        t = " ".join(str(a).split()).strip()
        if t and t.lower() not in seen:
            seen.add(t.lower())
            out.append(t[:300])
        if len(out) >= MAX_ALTERNATIVES:
            break
    return out


def record_fields(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Shape a decision payload into memory.record.write field inputs.
    Absent optional fields are written as state='absent' so a later revision
    can distinguish "not recorded" from an empty string."""
    fields: dict[str, dict[str, Any]] = {}
    for spec in SCHEMA_FIELDS:
        name = spec["name"]
        val = payload.get(name)
        if name == "alternatives" and isinstance(val, list):
            val = "\n".join(val)
        if val is None or (isinstance(val, str) and not val.strip()):
            fields[name] = {"state": "absent"}
        else:
            fields[name] = {"value": val, "state": "value"}
    return fields


def memory_value(payload: dict[str, Any]) -> dict[str, Any]:
    """The searchable memory row's value: structured, so a precedent hit
    carries its fields, and the embed text reads as prose."""
    alts = payload.get("alternatives") or []
    if isinstance(alts, str):
        alts = [a for a in alts.split("\n") if a.strip()]
    return {
        "kind": "decision",
        "category": payload.get("category", ""),
        "scenario": payload.get("scenario", ""),
        "reasoning": payload.get("reasoning", ""),
        "outcome": payload.get("outcome", ""),
        "alternatives": list(alts),
        "confidence": payload.get("confidence"),
        "decision_maker": payload.get("decision_maker", ""),
        "run_id": payload.get("run_id", ""),
        "realized_outcome": payload.get("realized_outcome", ""),
        "success": payload.get("success"),
        "made_at": payload.get("made_at", ""),
    }


def parse_record_json(raw: Any) -> dict[str, Any]:
    try:
        v = json.loads(raw) if isinstance(raw, str) and raw else (raw or {})
    except (TypeError, ValueError):
        return {}
    return v if isinstance(v, dict) else {}


@dataclass
class ChainNode:
    memory_key: str
    hop: int
    relation: str
    direction: str  # "upstream" (this caused the seed) | "downstream" (the seed caused this)
    via: str  # the node this one was reached from


EdgeFetcher = Callable[[list[str], str], Awaitable[list[tuple[str, str, str]]]]
"""(frontier keys, direction) -> [(from_key, to_key, relation)] for edges
touching the frontier on the side the direction asks for."""


async def walk_chain(
    seed: str,
    *,
    fetch: EdgeFetcher,
    direction: str = "both",
    max_hops: int = 3,
    max_nodes: int = MAX_CHAIN_NODES,
) -> list[ChainNode]:
    """BFS over causal edges. upstream follows edges INTO the frontier
    (what caused it); downstream follows edges OUT of it (what it caused).
    Each key appears once, at its shortest hop; bounded by max_nodes."""
    dirs = ["upstream", "downstream"] if direction == "both" else [direction]
    out: list[ChainNode] = []
    for d in dirs:
        seen: set[str] = {seed}
        frontier = [seed]
        for hop in range(1, max_hops + 1):
            if not frontier or len(out) >= max_nodes:
                break
            edges = await fetch(frontier, d)
            nxt: list[str] = []
            for from_key, to_key, rel in edges:
                via, node = (to_key, from_key) if d == "upstream" else (from_key, to_key)
                if node in seen or via not in seen:
                    continue
                seen.add(node)
                out.append(ChainNode(memory_key=node, hop=hop, relation=rel, direction=d, via=via))
                nxt.append(node)
                if len(out) >= max_nodes:
                    break
            frontier = nxt
    return out
