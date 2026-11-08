"""A defect-shaped loom_search answers the defect.

For a defect-shaped query under a code scope, the tracker verdict is ~45
tokens of a ~2,000-token answer. The rest is noise for the question asked:

- knowledge: three or four other-team SRE documents at similarity 0.32-0.57,
  ~600-1,400 tokens, whole chunks;
- conversations: five old chat turns, the same generic ones for unrelated
  defects;
- memory: transcript chunks of earlier sessions quoting Loom's own tool
  output (the shunt's deny text, a verdict line), and earlier
  "already reported?" questions.

The question text also defeats the check itself: a draft title that keeps
its "— already reported?" tail never passes the near-identical-title gate
against a record filed under the very same title.

Three pure transforms, no I/O:

- `strip_intent` takes the asking clauses out of a defect draft before the
  duplicate check reads it;
- `drop_loom_output` removes memory and conversation hits whose text is Loom's
  own tool output, for every query (such a row is Loom quoting itself, never
  evidence);
- `verdict_leads` narrows the other sections when the tracker leg answered.
"""

from __future__ import annotations

import re
from typing import Any

from server.memory.domain.loom_output import is_loom_output

# The asking clauses of a defect question — what the duplicate check must
# not read as part of the report.
_ASK_RE = re.compile(
    r"(?i)^(?:is (?:this|it) (?:a )?(?:already (?:reported|filed|known)|known(?: issue| bug)?)"
    r"|(?:has|have) (?:this|it) been (?:reported|filed|seen)(?: before)?"
    r"|(?:was|is) (?:this|it) (?:reported|filed|seen) before"
    r"|already (?:reported|filed|known)|reported before|seen before|known (?:issue|bug)"
    r"|(?:a |is there an? )?(?:open issue|issue) for (?:this|it)|dup(?:licate)?(?: check)?"
    r"|check the (?:qa|tracker) record(?: before i file(?: it)?)?|before i file(?: it)?)"
    r"[\s?.!:]*$"
)
# A dash split starts at the first blank of a run, so a long run is one pass.
_SEGMENT_SPLIT_RE = re.compile(r"((?<!\s)\s+[—–]\s+|(?<!\s)\s+-\s+(?=\S)|(?<=[?!])\s+|:\s+(?=\S))")

# Under a tracker verdict: the knowledge row that may stay, and how much of
# it (knowledge hits carry whole chunks).
VERDICT_KNOWLEDGE_MIN_SIMILARITY = 0.5
VERDICT_KNOWLEDGE_CHARS = 400
VERDICT_MEMORY_MAX = 3


def strip_intent(text: str) -> str:
    """`text` without the clauses that only ask whether it is known: a
    trailing "— already reported?", a leading "Is this already reported?",
    "Check the QA record before I file it:". A clause that carries anything
    else stays whole."""
    out_lines: list[str] = []
    for line in str(text or "").split("\n"):
        # [segment, separator, segment, …]: an asking segment goes with the
        # separator that joined it, so the report keeps its own punctuation.
        parts = _SEGMENT_SPLIT_RE.split(line)
        segs, seps = parts[0::2], parts[1::2] + [""]
        kept: list[str] = []
        for seg, sep in zip(segs, seps, strict=True):
            if seg.strip() and _ASK_RE.match(seg.strip()):
                if kept and not sep:
                    kept.pop()  # the separator before a trailing ask
                continue
            kept.extend([seg, sep])
        out_lines.append("".join(kept).strip())
    return "\n".join(out_lines).strip().rstrip("?").strip()


def _text_of(h: dict[str, Any]) -> str:
    return str(h.get("content_excerpt") or h.get("content") or "")


def drop_loom_output(payload: dict[str, Any]) -> int:
    """Remove memory and conversation hits that quote Loom's own output;
    the number removed. The rows kept are the same dict objects."""
    removed = 0
    for name in ("memory", "conversations"):
        sec = payload.get(name)
        if not isinstance(sec, dict) or not isinstance(sec.get("results"), list):
            continue
        rows = sec["results"]
        kept = [h for h in rows if not (isinstance(h, dict) and is_loom_output(_text_of(h)))]
        if len(kept) != len(rows):
            removed += len(rows) - len(kept)
            sec["results"] = kept
    return removed


def verdict_leads(payload: dict[str, Any]) -> dict[str, int]:
    """When the tracker leg answered, the other sections shrink to what can
    still inform a filing decision: at most VERDICT_MEMORY_MAX memory rows, a
    knowledge row only at VERDICT_KNOWLEDGE_MIN_SIMILARITY (one, clipped), no
    conversation turns. The rows kept are the same dict objects. Returns the
    counts withheld; {} when no verdict."""
    tracker = payload.get("tracker")
    if not isinstance(tracker, dict) or not tracker.get("verdict"):
        return {}
    withheld: dict[str, int] = {}
    mem = payload.get("memory")
    if isinstance(mem, dict) and isinstance(mem.get("results"), list):
        rows = mem["results"]
        if len(rows) > VERDICT_MEMORY_MAX:
            withheld["memory"] = len(rows) - VERDICT_MEMORY_MAX
            mem["results"] = rows[:VERDICT_MEMORY_MAX]
    know = payload.get("knowledge")
    if isinstance(know, dict) and isinstance(know.get("results"), list):
        rows = [k for k in know["results"] if isinstance(k, dict)]
        keep = [
            k for k in rows if float(k.get("similarity") or 0.0) >= VERDICT_KNOWLEDGE_MIN_SIMILARITY
        ][:1]
        for k in keep:
            for field in ("content", "content_excerpt"):
                if isinstance(k.get(field), str) and len(k[field]) > VERDICT_KNOWLEDGE_CHARS:
                    k[field] = k[field][:VERDICT_KNOWLEDGE_CHARS] + "…"
        if len(keep) != len(rows):
            withheld["knowledge"] = len(rows) - len(keep)
        know["results"] = keep
    conv = payload.get("conversations")
    if isinstance(conv, dict):
        crows = conv.get("results") or conv.get("hits") or []
        if crows:
            withheld["conversations"] = len(crows)
            conv["results"] = []
            conv.pop("hits", None)
    return withheld
