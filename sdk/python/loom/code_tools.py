"""Read-only model code lookup: fixed request shapes and whole-evidence budgeting."""

from __future__ import annotations

import copy
import json
import re
from typing import Any

from loom.errors import LoomError

CODE_TOOL_SPEC: dict[str, Any] = {
    "name": "loom_code_lookup",
    "description": (
        "Investigate indexed code first for definitions, behavior, callers and change impact, "
        "including bug reports quoting identifiers or errors. Use symbol for named definitions "
        "(prefer qualified names or path:line), search for concepts, callers for incoming calls, "
        "impact for prospective effects. Use local tools for explicit literal-search requests, "
        "uncommitted/unindexed files, or insufficient/abstaining results; reuse sufficient "
        "supplied evidence. Honor ambiguity, elision, client_truncation and tier/lower-bound "
        "warnings. "
        "Client omissions mean incomplete evidence even if server epistemic is exact. "
        "An empty graph does not prove no callers. Skip unrelated lookups."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "kind": {
                "type": "string",
                "enum": ["symbol", "search", "callers", "impact"],
                "description": (
                    "symbol: definition; search: conceptual behavior; callers: incoming calls; "
                    "impact: prospective effects"
                ),
            },
            "target": {
                "type": "string",
                "description": (
                    "Qualified symbol or path:line for symbol/callers/impact; "
                    "a natural-language question for search"
                ),
            },
            "depth": {
                "type": "integer",
                "minimum": 1,
                "maximum": 3,
                "description": (
                    "Caller hops: callers 1..3 (default 1), impact 1..2 (default 2); "
                    "omit for symbol/search"
                ),
            },
        },
        "required": ["kind", "target"],
        "additionalProperties": False,
    },
}

# Compact Python/C++ names (including templates/operators) and repo-relative path:line.
# Prose belongs to search; a server miss/ambiguity remains a server result.
_REFERENCE = re.compile(r"[\w$~][\w$~.:/<>,()\[\]*&+=!%|^-]*\Z", re.ASCII)


def code_request(args: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    unexpected = args.keys() - {"kind", "target", "depth"}
    if unexpected:
        raise LoomError(f"unexpected code tool arguments: {', '.join(sorted(unexpected))}")
    kind, target = args.get("kind"), args.get("target")
    if kind not in ("symbol", "search", "callers", "impact"):
        raise LoomError("kind must be symbol, search, callers or impact")
    if not isinstance(target, str) or not target.strip():
        raise LoomError("target must be a non-empty string")
    target = target.strip()
    depth = args.get("depth", 2 if kind == "impact" else 1)
    if "depth" in args and kind in ("symbol", "search"):
        raise LoomError("depth applies only to callers and impact")
    if isinstance(depth, bool) or not isinstance(depth, int) or not 1 <= depth <= 3:
        raise LoomError("depth must be an integer within 1..3")
    if kind == "impact" and depth > 2:
        raise LoomError("impact depth must be within 1..2")
    if kind != "search" and not _REFERENCE.fullmatch(target):
        raise LoomError("target must be a symbol reference or path:line; use search for prose")
    body: dict[str, Any] = {"include_lessons": False}
    if kind == "search":
        body.update(query=target, top_k=3, token_budget=2500)
    else:
        body["name"] = target
        if kind == "symbol":
            body.update(
                token_budget=2500,
                include_body=True,
                include_history=False,
                include_examples=False,
                uses=False,
                uses_brief=False,
            )
        elif kind == "callers":
            body.update(depth=depth, max_nodes=20)
        else:
            body.update(depth=depth, per_hop_limit=20)
    return f"code.{kind}", body


_ROW_KEYS = (
    "reverts",
    "owners",
    "co_change_files",
    "impacted_tests",
    "documented_in",
    "dispatch_seeds",
    "candidates",
    "impacted_symbols",
    "edges",
    "results",
    "definitions",
)


def fit_code_result(result: dict[str, Any], limit: int) -> str:
    """Keep metadata and whole evidence; disclose every client omission separately."""
    out = copy.deepcopy(result)

    def dump() -> str:
        return json.dumps(out, ensure_ascii=False, separators=(",", ":"), allow_nan=False)

    def fits(text: str) -> bool:
        # Match the TypeScript cap without escaping non-ASCII source into extra tokens.
        return len(text.encode("utf-16-le", errors="surrogatepass")) <= limit * 2

    text = dump()
    if fits(text):
        return text
    cut: dict[str, Any] = {
        "reason": "max_result_chars",
        "insufficient": True,
        "omitted_bodies": 0,
        "omitted_rows": {},
    }
    out["client_truncation"] = cut
    data = out["data"]
    for key in ("results", "definitions"):
        for row in reversed(data.get(key, [])):
            if row.get("body"):
                row.update(body="", elided=True)
                cut["omitted_bodies"] += 1
                text = dump()
                if fits(text):
                    return text
    for key in _ROW_KEYS:
        rows = data.get(key)
        if not isinstance(rows, list):
            continue
        while rows:
            rows.pop()
            cut["omitted_rows"][key] = cut["omitted_rows"].get(key, 0) + 1
            text = dump()
            if fits(text):
                return text
    # If uncertainty metadata itself does not fit, no partial evidence can stand in for it.
    out = {k: v for k, v in result.items() if k != "data"}
    out["client_truncation"] = {
        "reason": "max_result_chars",
        "insufficient": True,
        "metadata_omitted": True,
    }
    text = dump()
    if fits(text):
        return text
    out = {"client_truncation": out["client_truncation"]}
    return dump()
