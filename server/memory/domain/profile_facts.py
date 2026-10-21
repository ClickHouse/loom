"""The facts a profile builder shows its model, as text."""

from __future__ import annotations

import json
from typing import Any


def fact_text(value: Any) -> str:
    """A stored value (canonical JSON) as the text of its claim."""
    try:
        decoded = json.loads(value) if isinstance(value, str) else value
    except ValueError:
        return str(value)
    return decoded if isinstance(decoded, str) else json.dumps(decoded, ensure_ascii=False)


def render_fold_input(rows: list[dict[str, Any]]) -> str:
    """The fold's input: one dated line per fact, oldest first (by the full
    timestamp, so two facts of one day keep their order), so the newest
    statement of a changed preference is the last the model reads. Rows with an
    empty value are dropped; "" when nothing remains."""
    dated: list[tuple[str, str]] = []
    for r in rows:
        value = " ".join(str(r.get("value") or "").split())
        if value:
            dated.append((str(r.get("since") or ""), value))
    dated.sort(key=lambda t: (not t[0], t[0]))
    return "\n".join(f"- [{since[:10] or 'undated'}] {value}" for since, value in dated)
