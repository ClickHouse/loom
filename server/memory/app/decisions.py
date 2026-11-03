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
from server.memory.domain.decision_statement import alternatives, collapse_ws
from server.memory.domain.files import detect_value_files

CHAIN_RELATIONS: tuple[str, ...] = ("caused", "influenced", "derived_from")
MAX_ALTERNATIVES = 10
ALTERNATIVE_CHARS = 300
# What a cut alternative ends in, so a reader and a re-extraction know it was cut.
CUT_MARK = "…"
MAX_CHAIN_NODES = 200


def decision_key(category: str, scenario: str) -> str:
    """The logical key: the same derivation the record plane uses for identity
    fields, so record and memory row share one key."""
    from server.memory.domain.record_identity import canonical_record_key

    return canonical_record_key(SCHEMA_NAME, {"category": category, "scenario": scenario})


def cut_alternative(text: str) -> str:
    """`text` within ALTERNATIVE_CHARS: cut at a space and ending in CUT_MARK when too long,
    so the record, the statement and file detection only see whole tokens (a cut path
    would name another file). A first word longer than the limit leaves CUT_MARK alone."""
    if len(text) <= ALTERNATIVE_CHARS:
        return text
    space = text.rfind(" ", 0, ALTERNATIVE_CHARS)
    kept = text[:space] if space > 0 else ""
    return kept + CUT_MARK


def is_cut(alternative: str) -> bool:
    """Whether the record cut `alternative`: it ends in CUT_MARK, or it is ALTERNATIVE_CHARS
    long, as a stored one cut there with no mark is."""
    return alternative.endswith(CUT_MARK) or len(alternative) >= ALTERNATIVE_CHARS


def clean_alternatives(items: list[str], *, cut: bool = True) -> list[str]:
    """The alternatives a record keeps: whitespace collapsed, deduplicated in any case, at
    most MAX_ALTERNATIVES, each cut by `cut_alternative` unless `cut` is False."""
    out: list[str] = []
    seen: set[str] = set()
    for a in items or []:
        t = collapse_ws(a)
        t = cut_alternative(t) if cut and t else t
        # Two alternatives that read alike once cut are one in the record.
        if t and t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
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


_TEXT_FIELDS = ("category", "scenario", "outcome", "reasoning")


def label_fields(payload: dict[str, Any]) -> list[str]:
    """The stored fields the row's sensitivity and attribute labels read."""
    names = (*_TEXT_FIELDS, "realized_outcome")
    texts = [str(payload.get(f) or "") for f in names] + alternatives(payload)
    return [t for t in texts if t.strip()]


def blacklist_fields(payload: dict[str, Any]) -> list[str]:
    """Every caller-written field of the decision as the record stores it: what the
    blacklist reads, the label fields plus `decision_maker` and `run_id`."""
    extra = [str(payload.get(f) or "") for f in ("decision_maker", "run_id")]
    return label_fields(payload) + [t for t in extra if t.strip()]


def decision_files(payload: dict[str, Any]) -> list[str] | None:
    """The files the decision's text names, as the record stores it (never the realized
    outcome's); None when it names none, so a re-record keeps the row's files."""
    texts = [str(payload.get(f) or "") for f in _TEXT_FIELDS] + alternatives(payload)
    return detect_value_files("\n".join(texts)) or None


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
