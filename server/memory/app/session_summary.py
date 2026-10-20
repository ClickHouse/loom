"""Session-summary plane (write side) for memory.set_from_messages."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from server.core.ch import get_client
from server.core.datetimes import naive_utc_now, parse_iso_dt
from server.embedding import get_embedder
from server.memory.domain.sensitivity import reads_high, value_reads_high
from server.observability.genai import staged
from server.settings.budgets import llm_max_tokens

log = logging.getLogger(__name__)

# After extraction, compress a session into one dense dated digest, embed it
# with the fact-plane embedder, and store it in loom.session_summaries. The
# read leg ANN-retrieves these as source="summary" hits — a compact,
# high-recall stand-in for a whole session.

# Bumped when the prompt text changes, so an operator can tell which prompt
# produced a given digest.
_SESSION_SUMMARY_PROMPT_VERSION = "ss-3"

# Bounds the digest call's input cost. ~3000 chars ≈ 750 tokens.
_SESSION_SUMMARY_MAX_INPUT_CHARS = 3000


# How many fact values ride along as key_facts — a dedup signal for the
# assembler (which facts a summary subsumes); never independently embedded.
_SESSION_SUMMARY_MAX_KEY_FACTS = 6

# Calibrated to emit dated, fact-only sentences.
_SESSION_SUMMARY_SYSTEM = (
    "You are compressing one conversation session into a dense, factual digest "
    "for later retrieval. Write 1-3 sentences stating only the concrete facts, "
    "decisions, events, and stated values from THIS session. Prefer dated "
    "phrasing ('on 2024-03-11, the user ...'). No greetings, no meta-commentary, "
    "no 'the user discussed'. If a date is given, attribute facts to it. Date "
    "each EVENT at the time the speaker placed it, resolved against the session "
    "date: 'dyed my hair last week' in a session dated 2022-04-15 is 'around "
    "2022-04-08', never 'on 2022-04-15'; 'yesterday' is the day before the "
    "session date. Only what happened during the session itself is dated at "
    "the session date. State every fact as the speakers gave it: the digest is "
    "their record, so never judge a statement true or false, never call one "
    "incorrect or a misstatement, and never replace a stated value with the "
    "real-world one. Output the digest only."
)


def _digest_max_input_chars() -> int:
    from server.settings import settings

    try:
        return max(
            500,
            int(settings.memory_session_digest_max_input_chars or _SESSION_SUMMARY_MAX_INPUT_CHARS),
        )
    except (TypeError, ValueError):
        return _SESSION_SUMMARY_MAX_INPUT_CHARS


def _top_key_facts(
    extracted_facts: list[dict[str, Any]],
    *,
    limit: int = _SESSION_SUMMARY_MAX_KEY_FACTS,
) -> list[str]:
    """Top-N non-empty fact values as strings, order-preserving + deduped. A fact whose text
    reads as a credential is left out: the digest is listed by memory.digests and sent to
    the outcome judge, so its key facts never carry such a value."""
    out: list[str] = []
    seen: set[str] = set()
    for fact in extracted_facts or []:
        if not isinstance(fact, dict):
            continue
        raw = fact.get("value")
        if isinstance(raw, str):
            text = raw.strip()
        elif raw is None:
            text = ""
        else:
            text = json.dumps(raw, default=str)
        text = text.strip()
        if not text or text in seen:
            continue
        if value_reads_high(raw):
            continue
        seen.add(text)
        out.append(text)
        if len(out) >= limit:
            break
    return out


def _build_session_summary_user_prompt(
    msgs: list[dict[str, str]],
    observation_date: str | None,
) -> str:
    """Role-prefixed, length-bounded transcript with a session-date header."""
    transcript = "\n".join(
        f"[{m.get('role', 'user')}] {m.get('content', '')}" for m in (msgs or [])
    )
    cap = _digest_max_input_chars()
    if len(transcript) > cap:
        transcript = transcript[:cap]
    return (
        f"Session date: {observation_date or 'unknown'}\n\nConversation:\n{transcript}\n\nDigest:"
    )


@dataclass(frozen=True)
class SessionSummaryDraft:
    """The model's digest and its vector, drafted before the row exists.

    The digest depends on the transcript only, so it is drafted while the extraction
    pipeline runs and joined to the written keys after.
    """

    summary_text: str
    embedding: list[float]
    embedding_model: str
    tokens_in: int = 0
    tokens_out: int = 0


@staged("session_summary")
async def _draft_session_summary(
    input: Any,
    msgs: list[dict[str, str]],
    observation_date: str | None,
) -> SessionSummaryDraft | None:
    """One LLM call + one embedding: everything about the digest that does not
    need the extraction result. None when the model returns nothing or the
    embedder returns an empty vector (the read leg filters embedding_dim > 0);
    a digest that reads as a credential is drafted with no vector."""
    from server.llm.client import get_llm_client

    if not msgs:
        return None
    user_prompt = _build_session_summary_user_prompt(msgs, observation_date or None)
    llm = get_llm_client()
    result = await llm.complete(
        _SESSION_SUMMARY_SYSTEM,
        user_prompt,
        max_tokens=llm_max_tokens("session_summary"),
    )
    summary_text = (result.text or "").strip()
    if not summary_text:
        log.info("session_summaries: empty digest for session=%r; skipping", input.session_id)
        return None
    embedder = get_embedder()
    # A digest that reads as a credential is written without a vector: the summary leg
    # reads only digests that have one, and the text never reaches the embedder.
    embedding: list[float] = []
    if not reads_high(summary_text):
        embedding = list(await embedder.embed(summary_text))
        if not embedding:
            log.info(
                "session_summaries: empty embedding for session=%r; skipping", input.session_id
            )
            return None
    return SessionSummaryDraft(
        summary_text=summary_text,
        embedding=embedding,
        embedding_model=embedder.model_name,
        tokens_in=int(getattr(result, "tokens_in", 0) or 0),
        tokens_out=int(getattr(result, "tokens_out", 0) or 0),
    )


async def _insert_session_summary(
    input: Any,
    state: Any,  # WorkflowState
    draft: SessionSummaryDraft,
) -> None:
    """Join a draft to the extraction result and insert the row."""
    from server.memory.store import build_session_summary_row

    extracted_facts = list(getattr(state, "extracted_facts", None) or [])
    observation_date = getattr(state, "observation_date", None) or None
    ch = get_client()
    row = build_session_summary_row(
        org=input.org,
        namespace=input.namespace,
        agent=getattr(input, "agent", "-") or "-",
        user_id=getattr(input, "user_id", "-") or "-",
        session_id=getattr(input, "session_id", "") or "",
        # The digest belongs to the run that wrote it.
        run_id=str(getattr(input, "run_id", "") or "-"),
        summary_text=draft.summary_text,
        summary_embedding=draft.embedding,
        embedding_model=draft.embedding_model,
        embedding_dim=len(draft.embedding),
        key_facts=_top_key_facts(extracted_facts),
        # Lineage: the memory keys this digest compressed.
        derived_from=sorted(
            {
                str(w.get("memory_key") or "")
                for w in (getattr(state, "written", None) or [])
                if w.get("memory_key")
            }
        ),
        # valid_at = the observation date, surfaced as a per-line LABEL (never a
        # reorder key); parse_iso_dt fail-opens to NULL, matching the column DEFAULT.
        valid_at=parse_iso_dt(observation_date),
        created_at=naive_utc_now(),
    )
    await ch.insert(
        "loom.session_summaries",
        [row],
        settings={"async_insert": 0, "wait_for_async_insert": 1},
    )
    log.info(
        "session_summaries: wrote digest for session=%r (%d chars, prompt=%s)",
        input.session_id,
        len(draft.summary_text),
        _SESSION_SUMMARY_PROMPT_VERSION,
    )
    # The digest chain: this digest is linked to the previous and next digest of the same
    # identity by event time, as `precedes` edges in loom.operator_edges. Model-free, two
    # indexed reads and one insert; fail-open, after the digest is in place.
    from server.memory.app.digest_chain import link_digest

    await link_digest(
        ch,
        org=input.org,
        namespace=input.namespace,
        agent=str(row.get("agent") or "-"),
        user_id=str(row.get("user_id") or "-"),
        session_id=str(row.get("session_id") or ""),
        at=row.get("valid_at") or row["created_at"],
    )


@staged("session_summary")
async def _write_session_summary(
    input: Any,
    msgs: list[dict[str, str]],
    state: Any,  # WorkflowState
    *,
    draft: SessionSummaryDraft | None = None,
) -> None:
    """Generate + embed + insert one session-summary row. Fail-open.

    Skips a session with no extracted facts. One LLM call produces the digest;
    the fact-plane embedder embeds it so the read leg's cosine is comparable;
    inserted synchronously so the immediate post-ingest read sees it. A
    `draft` made ahead of time skips the model and embedder calls; a session that extracted
    nothing discards it.
    """
    extracted_facts = list(getattr(state, "extracted_facts", None) or [])
    if not extracted_facts:
        return
    if not msgs:
        return
    if draft is None:
        observation_date = getattr(state, "observation_date", None) or None
        draft = await _draft_session_summary(input, msgs, observation_date)
    if draft is None:
        return
    await _insert_session_summary(input, state, draft)
