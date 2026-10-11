"""Move stored entities to the current canonical form, once after the normaliser
changes. The mapping is decided per (org, namespace) from the stored text and the
memories that name it (`migrated_entity`: a name with no evidence is kept), saved to
`state` before any table changes and reused until a run completes, so an interrupted run
resumes with the same decisions after the rows that held the evidence were rewritten."""

from __future__ import annotations

import json
import os
from collections import defaultdict
from pathlib import Path
from typing import Any

from server.memory.domain.entity_sentinels import NONE_SENTINEL, PARSE_FAILED_SENTINEL
from server.memory.domain.graph_quality import canonical_entity_text, migrated_entity
from server.memory.store import entity_migration as store

_SENTINELS = {NONE_SENTINEL, PARSE_FAILED_SENTINEL}
Mapping = dict[store.Scope, dict[str, str]]


def _may_change(value: str) -> bool:
    head = value.rsplit(" ", 1)[-1]
    return canonical_entity_text(value) != value or head.endswith(("s", "z"))


async def plan(*, ch: Any = None) -> Mapping:
    """(org, namespace) -> old text -> new text, for every entity that changes."""
    out: Mapping = {}
    for scope, values in sorted((await store.stored_values(ch=ch)).items()):
        candidates = sorted(v for v in values - _SENTINELS if _may_change(v))
        if not candidates:
            continue
        texts = await store.naming_texts(scope, candidates, ch=ch)
        changes = {
            v: new
            for v in candidates
            if (new := migrated_entity(v, texts.get(v, []))) not in ("", v)
        }
        if changes:
            out[scope] = changes
    return out


def _load(state: Path) -> Mapping | None:
    if not state.exists():
        return None
    out: Mapping = defaultdict(dict)
    for org, ns, old, new in json.loads(state.read_text())["mapping"]:
        out[(org, ns)][old] = new
    return dict(out)


def _save(state: Path, mapping: Mapping) -> None:
    rows = [
        [*scope, old, new] for scope, m in sorted(mapping.items()) for old, new in sorted(m.items())
    ]
    tmp = state.with_suffix(state.suffix + ".tmp")
    tmp.write_text(json.dumps({"mapping": rows}))
    os.replace(tmp, state)


async def migrate_entities(*, apply: bool, state: Path, ch: Any = None) -> dict[str, int]:
    """Rows per table whose entities change (and namespaces whose degrees are rebuilt);
    with `apply`, rewritten. A saved `state` is resumed rather than planned again."""
    mapping = _load(state)
    if mapping is None:
        mapping = await plan(ch=ch)
        if apply:
            _save(state, mapping)
    counts: dict[str, int] = defaultdict(int)
    for scope, changes in mapping.items():
        olds = sorted(changes)
        for table, cols, pair in store.GRAPH_TABLES:
            n = await store.count_graph(table, cols, scope, olds, ch=ch)
            counts[table] += n
            if apply and n:
                await store.rename_graph(table, cols, pair, scope, changes, ch=ch)
        for table in store.MEMORY_TABLES:
            counts[table] += await store.count_memory(table, scope, olds, ch=ch)
        if apply:

            def rename(entities: list[str], changes: dict[str, str] = changes) -> list[str]:
                return list(dict.fromkeys(changes.get(e, e) for e in entities))

            await store.reemit_memory(scope, olds, rename, ch=ch)
    unsynced = await store.unsynced_degrees(ch=ch)
    counts["loom.entity_degree namespaces"] = len(unsynced)
    if apply:
        for scope in unsynced:
            await store.rebuild_degrees(scope, ch=ch)
        _clear(state)
    return dict(counts)


def _clear(state: Path) -> None:
    state.unlink(missing_ok=True)
