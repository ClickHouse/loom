"""The judge stage of `tracker.duplicate_check`.

The fit says how much the record's evidence looks like a duplicate; it
cannot read. When a candidate lands in the related band — found, scored,
below the duplicate line — one model call reads the draft against the top
candidates' own text and labels each `duplicate` (the same defect: same
failing code path, same error under the same conditions), `related` (the
same area or pattern, another defect) or `different`. A `duplicate` label on
a related-band candidate promotes it; nothing is ever demoted, a recorded
`related_to` is never promoted (the graph's word beats the judge's), and the
call fails open: no label, nothing changes. Pure: the caller fetches the
text and passes the client.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from server.core.errors import caller_text
from server.llm import customer
from server.tracker.domain.llm_reply import clip, json_object

log = logging.getLogger(__name__)

LABELS = ("duplicate", "related", "different")
_MAX_DRAFT_CHARS = 3000
_MAX_CANDIDATE_CHARS = 1500

SYSTEM = (
    "You review a draft bug report against existing issues of the same repository and say, "
    "for each existing issue, whether it reports the SAME DEFECT as the draft.\n"
    "duplicate: the same root cause — the same failing code path or the same error under the "
    "same conditions; filing the draft would be a duplicate.\n"
    "related: the same area, component or pattern, but a different defect or a different "
    "trigger; worth a cross-reference, not a duplicate.\n"
    "different: nothing beyond a shared word or component.\n"
    "Be conservative: a shared error code, component or stack frame alone is `related`, not "
    "`duplicate`. Judge the defect, not the wording. A generic symptom — memory limit "
    "exceeded, a timeout, connection reset, an SSL error, a crash with no shared frame — is "
    "`related` at most unless the draft and the issue share a specific cause: the same "
    "function, setting, query shape, version regression or reproducer. A draft with no "
    "specifics (a bare error line, a one-word title) has no duplicate: say `related` or "
    "`different`.\n"
    'Answer JSON only: {"verdicts": [{"number": <int>, "label": "duplicate|related|different", '
    '"reason": "<one sentence naming the shared or differing cause>"}]} — one entry per '
    "candidate number given, no other keys."
)


@dataclass
class Judgement:
    labels: dict[int, str] = field(default_factory=dict)
    reasons: dict[int, str] = field(default_factory=dict)
    ran: bool = False
    model: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    error: str = ""

    def duplicates(self) -> list[int]:
        return [n for n, lab in self.labels.items() if lab == "duplicate"]


def build_user_prompt(title: str, body: str, candidates: list[dict[str, Any]]) -> str:
    """The draft, then each candidate as `[#number] title\\nbody`."""
    parts = [f"DRAFT:\n{clip(title, 300)}\n{clip(body, _MAX_DRAFT_CHARS)}", "EXISTING ISSUES:"]
    for c in candidates:
        n = int(c.get("number") or 0)
        parts.append(
            f"[#{n}] {clip(str(c.get('title') or ''), 300)}\n"
            f"{clip(str(c.get('body') or ''), _MAX_CANDIDATE_CHARS)}"
        )
    return "\n\n".join(parts)


def parse_verdicts(text: str, numbers: list[int]) -> tuple[dict[int, str], dict[int, str]]:
    """{number: label}, {number: reason} for the numbers asked; anything else
    the model wrote is ignored, an unknown label is ignored, and a `duplicate`
    with no reason is read as `related`."""
    obj = json_object(text)
    if obj is None:
        return {}, {}
    wanted = {int(n) for n in numbers}
    labels: dict[int, str] = {}
    reasons: dict[int, str] = {}
    for v in obj.get("verdicts") or [] if isinstance(obj, dict) else []:
        if not isinstance(v, dict):
            continue
        try:
            n = int(v.get("number") or 0)
        except (TypeError, ValueError):
            continue
        lab = str(v.get("label") or "").strip().lower()
        reason = clip(str(v.get("reason") or ""), 240)
        if lab == "duplicate" and not reason.strip():
            lab = "related"  # a duplicate the model cannot give a cause for is not one
        if n in wanted and lab in LABELS:
            labels[n] = lab
            reasons[n] = reason
    return labels, reasons


async def judge(
    llm: Any,
    title: str,
    body: str,
    candidates: list[dict[str, Any]],
    *,
    stage: str = "dupcheck_judge",
) -> Judgement:
    """One call over the candidates the caller chose (title + body head each).
    Fails open: `ran=False` and empty labels on any error but a call the
    model-call switch holds (`customer.HOLDS`), which the request answers."""
    numbers = [int(c.get("number") or 0) for c in candidates if int(c.get("number") or 0) > 0]
    if llm is None or not numbers or not (title or body):
        return Judgement()
    from server.observability.genai import llm_stage

    try:
        with llm_stage(stage):
            res = await llm.complete(
                SYSTEM,
                build_user_prompt(title, body, candidates),
                max_tokens=80 + 60 * len(numbers),
                response_format={"type": "json_object"},
            )
    except customer.HOLDS:
        raise
    except Exception as e:  # noqa: BLE001 — a judge never breaks the verdict
        log.warning("dupcheck judge failed (open, the fit's verdict stands): %s", e)
        return Judgement(error=caller_text(e))  # the provider's text is in the log
    labels, reasons = parse_verdicts(str(getattr(res, "text", "") or ""), numbers)
    return Judgement(
        labels=labels,
        reasons=reasons,
        ran=True,
        model=str(getattr(res, "model", "") or ""),
        tokens_in=int(getattr(res, "tokens_in", 0) or 0),
        tokens_out=int(getattr(res, "tokens_out", 0) or 0),
    )


def apply(
    out: Any,
    judgement: Judgement,
    *,
    unserved: Sequence[dict[str, Any]] = (),
    top_k: int | None = None,
) -> Any:
    """Fold the judgement into the op's answer: a related-band item the judge
    labels `duplicate` becomes `duplicate` (unless the record says
    `related_to`), the verdict follows, `undecided` clears when a duplicate is
    confirmed; every judged item carries `judge: {label, reason}`; the answer
    carries a `judge` block. A candidate under the related line (`unserved`)
    that the judge labels `duplicate` is served as one (`inserted`);
    the answer keeps `top_k` items. Nothing is demoted."""
    block: dict[str, Any] = {
        "ran": judgement.ran,
        "model": judgement.model,
        "tokens_in": judgement.tokens_in,
        "tokens_out": judgement.tokens_out,
        "labels": {str(n): lab for n, lab in sorted(judgement.labels.items())},
        "promoted": [],
        "inserted": [],
    }
    if judgement.error:
        block["error"] = judgement.error
    if not judgement.ran:
        out.judge = block
        return out
    items = list(out.items or [])
    for cand in unserved:
        n = int(cand.get("number") or 0)
        lab = judgement.labels.get(n)
        if lab is None:
            continue
        if lab == "duplicate" and str(cand.get("relation_recorded") or "none") != "related_to":
            it = dict(cand)
            it["relation"] = "duplicate"
            it["judge"] = {"label": lab, "reason": judgement.reasons.get(n, "")}
            reason = judgement.reasons.get(n) or ""
            it["evidence"] = [
                *list(cand.get("evidence") or []),
                "judge: same defect" + (f" — {reason}" if reason else ""),
            ]
            items.append(it)
            block["promoted"].append(n)
            block["inserted"].append(n)
    for it in items:
        if it.get("judge") and int(it.get("number") or 0) in block["inserted"]:
            continue
        n = int(it.get("number") or 0)
        lab = judgement.labels.get(n)
        if lab is None:
            continue
        it["judge"] = {"label": lab, "reason": judgement.reasons.get(n, "")}
        if (
            lab == "duplicate"
            and it.get("relation") == "related"
            and str(it.get("relation_recorded") or "none") != "related_to"
        ):
            it["relation"] = "duplicate"
            reason = judgement.reasons.get(n) or ""
            it.setdefault("evidence", []).append(
                "judge: same defect" + (f" — {reason}" if reason else "")
            )
            block["promoted"].append(n)
    items.sort(
        key=lambda it: (it.get("relation") != "duplicate", -float(it.get("confidence") or 0.0))
    )
    out.items = items[:top_k] if top_k else items
    if block["promoted"]:
        out.verdict = "duplicate_of"
        out.undecided = False
    out.judge = block
    return out


__all__ = ["LABELS", "SYSTEM", "Judgement", "apply", "build_user_prompt", "judge", "parse_verdicts"]
