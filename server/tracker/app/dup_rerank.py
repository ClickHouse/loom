"""The rerank stage of `tracker.duplicate_check`.

A filed duplicate can sit in one leg's own head while fusion places it after
every lexical row, so the fit never scores it and the nearest list never
shows it. The rerank reads each leg's head directly.

- **Pool.** Each leg's own top rows (not the fused list), plus the rows that
  share the draft's error fingerprint.
- **Rerank.** One model call reads the draft against each pool candidate's
  title and body head and returns at most five `same` / `related` picks with a
  confidence. The picks lead the `nearest` list. The verdict stays the fit's,
  and nothing is promoted.

Everything fails open: a timeout or error leaves the answer as it was. Pure
except `rerank`, which takes the client it is given.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from server.core.errors import caller_text
from server.llm import customer
from server.settings.budgets import llm_max_tokens
from server.tracker.domain.llm_reply import clip, json_object

log = logging.getLogger(__name__)

PROMPT_VERSION = "1"

RERANK_SYSTEM = (
    "You compare a draft bug or feature report against candidate existing issues of the same "
    "repository.\n"
    "For each candidate decide whether it is the SAME issue as the draft (the same defect or the "
    "same request: filing the draft would duplicate it), RELATED (the same area, a different "
    "defect or request), or DIFFERENT.\n"
    "Judge the underlying problem, not the wording: a report and its duplicate often use different "
    "words (a crash title vs the root-cause title, a symptom vs the feature request that fixes "
    "it).\n"
    "Answer JSON only, at most 5 entries, the most likely duplicate first, omitting DIFFERENT "
    'ones, each [number, "s" for same or "r" for related, confidence 0..1]: '
    '{"top": [[123, "s", 0.8]]}'
)

# picks the model may return; more is truncated
MAX_PICKS = 5
# candidates one call reads, over every namespace of a fan-out
POOL_CAP = 40
_LABELS = ("same", "related")
_SHORT = {"s": "same", "same": "same", "r": "related", "related": "related"}


@dataclass
class Rerank:
    picks: list[tuple[int, str, float]] = field(default_factory=list)  # (number, label, conf)
    ran: bool = False
    model: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    pool: int = 0
    ms: int = 0
    error: str = ""

    def block(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "ran": self.ran,
            "model": self.model,
            "prompt": PROMPT_VERSION,
            "pool": self.pool,
            "picks": [{"number": n, "label": lab, "confidence": c} for n, lab, c in self.picks],
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "ms": self.ms,
        }
        if self.error:
            out["error"] = self.error
        return out


def build_pool(
    leg_rows: dict[str, list[int]],
    *,
    per_leg: int,
    extra: list[list[int]] | None = None,
    exclude: set[int] | None = None,
    cap: int = 60,
) -> list[int]:
    """Each leg's own top `per_leg` numbers, the legs taking turns so no leg
    crowds out another, then each `extra` list (the fingerprint rows)
    in order; deduplicated, `exclude` dropped, at most `cap`."""
    skip = exclude or set()
    lists = [list(v or [])[: max(0, per_leg)] for v in leg_rows.values()]
    lists += [list(v or [])[: max(0, per_leg)] for v in (extra or [])]
    out: list[int] = []
    seen: set[int] = set()
    for i in range(max((len(v) for v in lists), default=0)):
        for v in lists:
            if i < len(v):
                n = int(v[i] or 0)
                if n > 0 and n not in seen and n not in skip:
                    seen.add(n)
                    out.append(n)
    return out[:cap]


def build_user_prompt(
    title: str, body: str, candidates: list[dict[str, Any]], *, body_chars: int
) -> str:
    parts = [f"DRAFT:\n{clip(title, 300)}\n{clip(body, 1500)}", "CANDIDATES:"]
    for c in candidates:
        parts.append(f"[#{_key(c)}] {clip(c.get('title'), 200)}\n{clip(c.get('body'), body_chars)}")
    return "\n\n".join(parts)


def _key(c: dict[str, Any]) -> int:
    """The number the model sees: `key` when the caller set one (a fan-out
    over repositories whose issue numbers collide), else the issue number."""
    return int(c.get("key") or c.get("number") or 0)


def parse_top(text: str, numbers: list[int]) -> list[tuple[int, str, float]]:
    """The model's picks among the numbers asked, `same` before `related`,
    each by confidence; anything else it wrote is ignored."""
    obj = json_object(text)
    if obj is None:
        return []
    wanted = {int(n) for n in numbers}
    picks: list[tuple[int, str, float]] = []
    seen: set[int] = set()
    for x in (obj.get("top") or []) if isinstance(obj, dict) else []:
        if isinstance(x, list | tuple) and len(x) >= 2:
            x = {"n": x[0], "l": x[1], "c": x[2] if len(x) > 2 else 0.0}
        if not isinstance(x, dict):
            continue
        try:
            n = int(x.get("n") or x.get("number") or 0)
            conf = max(0.0, min(1.0, float(x.get("c") or x.get("confidence") or 0.0)))
        except (TypeError, ValueError):
            continue
        lab = _SHORT.get(str(x.get("l") or x.get("label") or "").strip().lower(), "")
        if n in wanted and n not in seen and lab in _LABELS:
            seen.add(n)
            picks.append((n, lab, round(conf, 3)))
    picks.sort(key=lambda p: (_LABELS.index(p[1]), -p[2]))
    return picks[:MAX_PICKS]


async def rerank(
    llm: Any,
    title: str,
    body: str,
    candidates: list[dict[str, Any]],
    *,
    body_chars: int,
    stage: str = "dupcheck_rerank",
) -> Rerank:
    """One call over the pool. Fails open: `ran=False`, no picks."""
    numbers = [_key(c) for c in candidates if _key(c) > 0]
    res = Rerank(pool=len(numbers))
    if llm is None or not numbers or not (title or body):
        return res
    from server.observability.genai import llm_stage

    t0 = time.monotonic()
    try:
        with llm_stage(stage):
            out = await llm.complete(
                RERANK_SYSTEM,
                build_user_prompt(title, body, candidates, body_chars=body_chars),
                max_tokens=llm_max_tokens("tracker_dup_rerank"),
                response_format={"type": "json_object"},
            )
    except customer.HOLDS:
        raise  # the org's model calls are held: the request answers that
    except Exception as e:  # noqa: BLE001 — a rerank never breaks the answer
        log.info("dupcheck rerank failed (open, the answer stands): %s", e)
        res.error = caller_text(e)  # the provider's text is in the log
        res.ms = int((time.monotonic() - t0) * 1000)
        return res
    res.ms = int((time.monotonic() - t0) * 1000)
    res.ran = True
    res.model = str(getattr(out, "model", "") or "")
    res.tokens_in = int(getattr(out, "tokens_in", 0) or 0)
    res.tokens_out = int(getattr(out, "tokens_out", 0) or 0)
    res.picks = parse_top(str(getattr(out, "text", "") or ""), numbers)
    return res


def pick_rows(
    rerank_result: Rerank,
    text_by: dict[int, dict[str, Any]],
    *,
    exclude: set[int],
    namespace: str = "",
) -> list[dict[str, Any]]:
    """The picks as `nearest` rows: number, title, state, url, and the model's
    label and confidence. `text_by` is keyed by the key the model saw; a row
    carrying `number` / `namespace` names its own. Never a match: the
    verdict stays the fit's."""
    rows: list[dict[str, Any]] = []
    for key, lab, conf in rerank_result.picks:
        it = text_by.get(key) or {}
        n = int(it.get("number") or key)
        if n in exclude and not it.get("namespace"):
            continue
        row: dict[str, Any] = {"number": n}
        ns = str(it.get("namespace") or namespace or "")
        if ns:
            row["namespace"] = ns
        for k in ("title", "state", "url"):
            if it.get(k) not in (None, ""):
                row[k] = it[k]
        row["rerank"] = {"label": lab, "confidence": conf}
        if it.get("anchors"):
            row["anchors"] = list(it["anchors"])
        rows.append(row)
    return rows
