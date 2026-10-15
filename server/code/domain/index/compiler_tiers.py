"""The compiler tiers a repository's status reports.

Each compiler-tier producer keeps its own watermark in `code_index_state`
(`watermark_key_for` in code_scip_apply: scip-clang advances `scip_commit`,
scip-typescript `scip_commit_ts`, scip-go `scip_commit_go`, scip-python
`scip_commit_py`). Reading the C++ and Python keys by name would make a TypeScript or Go
repository report a tier it does not run and hide the one it does. This table is the one
place that says, per producer, which key to read, what to call it and which workers move
it; the status handler and the UI's Health tab read it.
"""

from __future__ import annotations

from typing import Any, NamedTuple

from server.code.domain.index.state import IndexState


class TierSpec(NamedTuple):
    producer: str
    tool: str
    label: str
    watermark_key: str
    workers: tuple[str, ...]
    note: str


# Order is the order the cards are shown in when several tiers are live.
TIERS: tuple[TierSpec, ...] = (
    TierSpec(
        "clang",
        "scip-clang",
        "C++",
        "scip_commit",
        ("code_scip_full", "code_scip_delta", "code_scip_apply"),
        "Precise cross-TU call graph. A full index seeds it and runs weekly; deltas per merge.",
    ),
    TierSpec(
        "typescript",
        "scip-typescript",
        "TypeScript / JavaScript",
        "scip_commit_ts",
        ("code_scip_ts_delta",),
        "Per-merge delta in the tier's working tree; JavaScript is read via the tsconfig.",
    ),
    TierSpec(
        "go",
        "scip-go",
        "Go",
        "scip_commit_go",
        ("code_scip_go_delta",),
        "Per-merge delta in the tier's working tree, one build per module.",
    ),
    TierSpec(
        "python",
        "scip-python",
        "Python",
        "scip_commit_py",
        ("code_scip_py_delta", "code_scip_apply"),
        "Per-merge delta in the tier's working tree; a first run indexes the tree.",
    ),
)
WATERMARK_KEYS: tuple[str, ...] = tuple(t.watermark_key for t in TIERS)

# The source files each producer emits documents for. An entry can run more than one
# tier (Python and TypeScript in one repository); compiler rows carry no producer
# column, so a full run retires only the live paths its own producer could have emitted
# — never another tier's documents.
SUFFIXES: dict[str, tuple[str, ...]] = {
    "clang": (
        ".c",
        ".cc",
        ".cpp",
        ".cxx",
        ".c++",
        ".h",
        ".hh",
        ".hpp",
        ".hxx",
        ".h++",
        ".ipp",
        ".inl",
    ),
    "typescript": (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs"),
    "go": (".go",),
    "python": (".py", ".pyi"),
}


def owns_path(producer: str, path: str) -> bool:
    """Whether `producer` could have emitted a compiler document for `path`."""
    return path.lower().endswith(SUFFIXES.get(normalize_producer(producer), ()))


def normalize_producer(producer: str) -> str:
    """`scip-typescript`, `typescript` and `TypeScript` name the same tier; '' stays ''
    (an entry that defers to its tree)."""
    low = (producer or "").strip().lower()
    if low.startswith("scip-"):
        low = low[len("scip-") :]
    if low in ("cpp", "c++", "c"):
        return "clang"
    return low


def primary_producer(entry_producer: str, state: IndexState) -> str:
    """The tier the entry is configured for; an entry without one (a registry
    row) is the tier whose watermark is set, in TIERS order; '' when none."""
    p = normalize_producer(entry_producer)
    if p:
        return p
    for t in TIERS:
        if _value(state, t.watermark_key):
            return t.producer
    return ""


def compiler_tiers(
    entry_producer: str,
    state: IndexState,
    lags: dict[str, int],
) -> list[dict[str, Any]]:
    """The cards: the primary tier always (its watermark may be unset — a
    repository whose first compiler run has not happened yet), then every
    other tier whose watermark is set. `lags` is keyed by watermark key."""
    primary = primary_producer(entry_producer, state)
    out: list[dict[str, Any]] = []
    for t in TIERS:
        commit = _value(state, t.watermark_key)
        if t.producer != primary and not commit:
            continue
        row = state.get(t.watermark_key) or {}
        out.append(
            {
                "producer": t.producer,
                "tool": t.tool,
                "label": t.label,
                "watermark_key": t.watermark_key,
                "commit": commit,
                "lag": int(lags.get(t.watermark_key, -1)),
                "updated_at": str(row.get("updated_at") or "") if commit else "",
                "primary": t.producer == primary,
                "workers": list(t.workers),
                "note": t.note,
            }
        )
    return out


def state_value(state: IndexState, key: str) -> str:
    """One `code_index_state` row's value as text; "" when unset."""
    return _value(state, key)


def _value(state: IndexState, key: str) -> str:
    v = (state.get(key) or {}).get("value")
    if isinstance(v, bytes):
        v = v.decode("utf-8", "replace").rstrip("\x00")
    return str(v or "")


__all__ = [
    "SUFFIXES",
    "TIERS",
    "WATERMARK_KEYS",
    "TierSpec",
    "compiler_tiers",
    "state_value",
    "normalize_producer",
    "owns_path",
    "primary_producer",
]
