"""The home namespace's fact answers first.

`loom_search` fans four sections over the addressed namespace and the
caller's own (the all_mine fan-out). A short question such as "what i test?"
can return the caller's own one-line fact at similarity 0.40 followed by
transcript chunks, knowledge rows and conversation snippets from other
namespaces at 0.27–0.31 — about 2,000 tokens for a two-word answer. The
calibration flags none of them: every row is above the similarity floor,
just far below the answer.

When the top memory hit is a `fact` from the caller's home namespace the
question is answered by a row the caller wrote; the other namespaces' rows
and the conversation section add cost, not information. `answer_first`
withholds: memory hits from other namespaces whose similarity is not within
`margin` of the lead (and home rows not within `HOME_MARGIN`), knowledge
hits from other namespaces, and the whole conversation section. Counts ride
in `payload["withheld"]` and the text summary, and `explain=true` keeps
everything.
Pure; no I/O.
"""

from __future__ import annotations

from typing import Any


def _is_home_fact(h: Any, home_ns: str) -> bool:
    return (
        isinstance(h, dict)
        and str(h.get("source") or "fact") == "fact"
        and str(h.get("namespace") or "") == home_ns
    )


def leading_home_fact(
    mem_hits: list[dict[str, Any]],
    home_ns: str,
    *,
    lead_margin: float = 0.03,
    moved: frozenset[int] = frozenset(),
) -> dict[str, Any] | None:
    """The memory hit that leads the pool when it is a fact from `home_ns`.

    When the first row is not a home fact but a home fact sits within
    `lead_margin` of its similarity, the fact is the lead: rank carries the
    boosts a transcript chunk earns (recency, reinforcement), similarity is
    what the question asked for. For example, a lookup chunk at sim 0.452 /
    rank 0.532 must not outrank `where_i_live` at sim 0.470 / rank 0.486.
    None when no home fact leads or is within the margin."""
    if not home_ns or not mem_hits:
        return None
    first = mem_hits[0]
    if _is_home_fact(first, home_ns):
        return first
    if not isinstance(first, dict):
        return None
    floor = float(first.get("similarity") or 0.0) - lead_margin
    for h in mem_hits[1:]:
        # a fact the search moved down for its stale code (`moved`: the ids of those
        # dicts) is served where the search put it, never lifted
        if id(h) in moved:
            continue
        if _is_home_fact(h, home_ns) and float(h.get("similarity") or 0.0) >= floor:
            return h
    return None


def recall_echo_hit(h: Any) -> bool:
    """A transcript chunk or digest that is itself a Loom recall (the
    `recall_echo` signature, read from the hit dict)."""
    from types import SimpleNamespace

    from server.memory.domain.fact_citations import recall_echo

    if not isinstance(h, dict) or str(h.get("source") or "fact") == "fact":
        return False
    row = SimpleNamespace(source=h.get("source"), content_excerpt=h.get("content_excerpt") or "")
    return recall_echo(row)


# A row from the home namespace itself that sits this far below the leading
# fact's similarity is withheld as well: a fact at 0.566 followed by
# extraction echoes and unrelated chunks at 0.34–0.38 is noise the
# calibration does not flag.
HOME_MARGIN = 0.15


def _defect_answer(payload: dict[str, Any]) -> bool:
    """Loom's own output quoted back is never a hit; when the
    tracker leg answered, its verdict leads and the other sections narrow.
    True when the verdict took the lead."""
    from server.tracker.domain.defect_answer import drop_loom_output, verdict_leads

    echoes = drop_loom_output(payload)
    withheld = verdict_leads(payload)
    if echoes:
        withheld["loom_output"] = echoes
    if withheld:
        reason = "tracker_verdict_leads" if payload.get("tracker") else "loom_output"
        payload["withheld"] = {**withheld, "reason": reason}
    return bool(payload.get("tracker"))


def precision_floor() -> float:
    """The similarity a loom_search memory hit must reach:
    the setting when it is >= 0, else the midpoint of the calibrated noise
    floor and question→fact p25 (scalar fallbacks when no calibration ran)."""
    from server.memory.app.embedder_calibration import (
        effective_hyde_fallback_threshold,
        effective_noise_floor,
    )
    from server.settings import settings

    configured = float(settings.mcp_loom_search_precision_floor)
    if configured >= 0:
        return configured
    noise = effective_noise_floor(float(settings.memory_search_min_similarity))
    p25 = effective_hyde_fallback_threshold(float(settings.memory_search_hyde_fallback_threshold))
    return round((noise + p25) / 2.0, 4)


def apply_precision_floor(
    payload: dict[str, Any], home_ns: str = "", floor: float | None = None
) -> int:
    """Withhold memory hits whose similarity is below the precision floor —
    a hit from another namespace below the floor plus
    `mcp_loom_search_foreign_margin` — and return the number withheld. A hit
    without a similarity (a pinned or structural row) is kept. Pure apart from
    reading the settings."""
    from server.settings import settings

    mem = payload.get("memory")
    if not isinstance(mem, dict) or not isinstance(mem.get("results"), list):
        return 0
    f = precision_floor() if floor is None else float(floor)
    if f <= 0:
        return 0
    foreign = f + float(settings.mcp_loom_search_foreign_margin)
    rows = mem["results"]

    def keep(h: Any) -> bool:
        if not isinstance(h, dict) or h.get("similarity") is None:
            return True
        ns = str(h.get("namespace") or home_ns)
        return float(h.get("similarity") or 0.0) >= (f if not home_ns or ns == home_ns else foreign)

    kept = [h for h in rows if keep(h)]
    if len(kept) != len(rows):
        mem["results"] = kept
    return len(rows) - len(kept)


MAX_CHUNKS_BEHIND_FACT = 1


def cap_chunks_behind_fact(payload: dict[str, Any], home_ns: str) -> int:
    """When a fact from the home namespace is in the memory section, keep at
    most MAX_CHUNKS_BEHIND_FACT transcript chunks after it (the best ranked):
    the fact is the answer, one chunk is its evidence, the rest repeat the
    same session. Returns the number withheld."""
    mem = payload.get("memory")
    if not isinstance(mem, dict) or not isinstance(mem.get("results"), list):
        return 0
    rows = mem["results"]
    has_fact = any(
        isinstance(h, dict)
        and str(h.get("source") or "fact") == "fact"
        and str(h.get("namespace") or home_ns) == home_ns
        for h in rows
    )
    if not has_fact:
        return 0
    kept: list[Any] = []
    chunks = 0
    for h in rows:
        if isinstance(h, dict) and str(h.get("source") or "") == "chunk":
            chunks += 1
            if chunks > MAX_CHUNKS_BEHIND_FACT:
                continue
        kept.append(h)
    if len(kept) != len(rows):
        mem["results"] = kept
    return len(rows) - len(kept)


def answer_first(
    payload: dict[str, Any],
    home_ns: str,
    *,
    explain: bool = False,
    margin: float = 0.03,
    home_margin: float = HOME_MARGIN,
    moved: frozenset[int] = frozenset(),
) -> dict[str, Any]:
    """`payload` with the sections a leading home fact makes redundant
    withheld (see the module docstring). Foreign rows stay within `margin`
    of the lead's similarity, home rows within `home_margin`.
    Returns the same object, and the rows kept are the same dict objects
    (`search_sections` maps each back to its search model by id); adds a
    `withheld` dict only when something was withheld."""

    if explain:
        return payload
    _below = apply_precision_floor(payload, home_ns)
    if _defect_answer(payload):  # a tracker verdict leads instead
        if _below:
            payload.setdefault("withheld", {})["below_precision_floor"] = _below
        return payload
    mem = payload.get("memory")
    if not isinstance(mem, dict):
        return payload
    _below += cap_chunks_behind_fact(payload, home_ns)
    hits = [h for h in (mem.get("results") or []) if isinstance(h, dict)]
    lead = leading_home_fact(hits, home_ns, moved=moved)
    if lead is None:
        if _below:
            payload["withheld"] = {"memory": _below, "reason": "below_precision_floor"}
        return payload
    lead_sim = float(lead.get("similarity") or 0.0)
    # The lead comes first whatever rank said, and a row that is
    # itself a Loom recall is withheld outright — every lookup writes one,
    # and each competes with the fact it recalled.
    kept = [lead] + [
        h
        for h in hits
        if h is not lead
        and not recall_echo_hit(h)
        and float(h.get("similarity") or 0.0)
        >= lead_sim - (home_margin if str(h.get("namespace") or "") == home_ns else margin)
    ]
    withheld: dict[str, int] = {}
    if len(kept) != len(hits):
        withheld["memory"] = len(hits) - len(kept)
        mem["results"] = kept
    know = payload.get("knowledge")
    if isinstance(know, dict) and isinstance(know.get("results"), list):
        krows = [k for k in know["results"] if isinstance(k, dict)]
        kkept = [k for k in krows if str(k.get("namespace") or home_ns) == home_ns]
        if len(kkept) != len(krows):
            withheld["knowledge"] = len(krows) - len(kkept)
            know["results"] = kkept
    conv = payload.get("conversations")
    if isinstance(conv, dict):
        crows = conv.get("results") or conv.get("hits") or []
        if crows:
            withheld["conversations"] = len(crows)
            conv["results"] = []
            conv.pop("hits", None)
    if _below:
        withheld["below_precision_floor"] = _below
    if withheld:
        payload["withheld"] = {
            **withheld,
            "reason": "home_fact_leads",
            "lead": str(lead.get("memory_key") or ""),
        }
    return payload


def withheld_note(payload: dict[str, Any]) -> str:
    """One clause for the text summary, "" when nothing was withheld."""
    w = payload.get("withheld")
    if not isinstance(w, dict):
        return ""
    sections = ("memory", "knowledge", "conversations")
    parts = [f"{int(n)} {name}" for name, n in w.items() if name in sections and n]
    if w.get("loom_output"):
        parts.append(f"{int(w['loom_output'])} quoting Loom's own output")
    if w.get("below_precision_floor"):
        parts.append(f"{int(w['below_precision_floor'])} below the precision floor")
    if not parts:
        return ""
    if w.get("reason") == "below_precision_floor":
        return (
            f" Withheld {', '.join(parts)} memory hit(s): too weak to be an answer "
            "(explain=true shows them)."
        )
    if w.get("reason") == "tracker_verdict_leads":
        return (
            f" Withheld {', '.join(parts)} hit(s): the tracker verdict answers "
            "(explain=true shows them)."
        )
    if w.get("reason") == "loom_output":
        return f" Withheld {', '.join(parts)} hit(s)."
    return (
        f" Withheld {', '.join(parts)} hit(s) from other namespaces/conversations: "
        f"the memory fact [{w.get('lead', '')}] answers (explain=true shows them)."
    )
