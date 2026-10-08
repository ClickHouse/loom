"""Reading a model's reply: the one JSON object it was asked for, and the
clipped text a prompt carries. Pure."""

from __future__ import annotations

import json
import re
from typing import Any

_JSON_RE = re.compile(r"\{.*\}", re.S)


def clip(text: Any, limit: int) -> str:
    """`text` stripped, cut to `limit` characters with an ellipsis when longer."""
    t = str(text or "").strip()
    return t if len(t) <= limit else t[: limit - 1].rstrip() + "…"


def json_object(text: Any) -> Any:
    """The outermost `{…}` of the reply, parsed; None when there is none or it
    does not parse. The caller checks the shape."""
    m = _JSON_RE.search(str(text or ""))
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


__all__ = ["clip", "json_object"]
