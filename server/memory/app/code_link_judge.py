"""The `revise` policy's judgement: whether a memory's claim still holds for its code's
new text, and the corrected claim when it does not. Loom never rewrites the memory: a
correction is served as the link's `suggestion`."""

from __future__ import annotations

import json

from server.code.domain.anchor import Definition
from server.memory.store.code_links import LinkRow

# A claim or code longer than the judge reads is never judged.
_JUDGE_CLAIM_CHARS = 2000
_JUDGE_CODE_CHARS = 6000
# room for a revised claim as long as the longest judged, inside the verdict's JSON
_JUDGE_ANSWER_TOKENS = _JUDGE_CLAIM_CHARS // 2

_JUDGE = (
    "You check a stored claim about code against a change to that code. Reply with JSON "
    '{"verdict": "holds" | "contradicted" | "unsure", "revised": "<the claim, corrected '
    'for the new code, in the same voice; only when contradicted>"}. Say contradicted only '
    "when the new code clearly makes the claim false; when in doubt, unsure."
)


class Malformed(ValueError):
    """The model's reply is not one of the verdicts."""


async def judge_claim(claim: str, before: str, after: str) -> tuple[str, str]:
    """(verdict, revised claim) for `claim` once its code went from `before` to `after`;
    ("malformed", "") when the model's reply is not one of the verdicts."""
    from server.llm.client import get_llm_client_for_model
    from server.llm.json_text import strip_code_fence
    from server.observability.genai import llm_stage
    from server.settings import settings

    with llm_stage("code_link_revise"):
        res = await get_llm_client_for_model(settings.code_link_revise_model).complete(
            _JUDGE,
            f"CLAIM:\n{claim}\n\nCODE BEFORE:\n{before}\n\nCODE NOW:\n{after}",
            max_tokens=_JUDGE_ANSWER_TOKENS,
            response_format={"type": "json_object"},
        )
    try:
        data = json.loads(strip_code_fence(str(getattr(res, "text", "") or "")))
    except ValueError:
        return "malformed", ""
    verdict = str(data.get("verdict") or "") if isinstance(data, dict) else ""
    if verdict not in ("holds", "contradicted", "unsure"):
        return "malformed", ""
    return verdict, str(data.get("revised") or "").strip()


def readable(claim: str, old: str, new: str) -> bool:
    """The judge can read the claim and both texts: none empty, none past its reach."""
    too_long = len(claim) > _JUDGE_CLAIM_CHARS or max(len(old), len(new)) > _JUDGE_CODE_CHARS
    return bool(old and new) and not too_long


async def judge(
    claim: str, link: LinkRow, old: str, new: str, d: Definition, head: str
) -> LinkRow | None:
    """`link` once its claim is judged against the code it was written for (`old`) and
    the code now (`new`, `d` at `head`): a claim that holds re-anchors it; a
    contradicted one keeps it `changed` with the corrected claim as `suggestion`. None
    on an unsure verdict; `Malformed` when the reply is no verdict."""
    verdict, revised = await judge_claim(claim, old, new)
    if verdict == "malformed":
        raise Malformed("the judge's reply is no verdict")
    if verdict == "contradicted" and revised:
        return {**link, "suggestion": revised, "judged_hash": d.body_hash}
    if verdict != "holds":
        return None
    return {
        **link,
        "body_hash": d.body_hash,
        "checked_hash": d.body_hash,
        "state": "fresh",
        "changed_since": "",
        "suggestion": "",
        "anchored_commit": head,
        "anchored_path": d.path,
        "anchored_hash": d.body_hash,
        "anchored_name": d.qualified_name,
        "baseline_commit": "",
        "judged_hash": "",
        "start_line": d.start_line,
        "end_line": d.end_line,
        "carried": 0,
    }
