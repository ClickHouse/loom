"""Memory integrity and false-memory rate at write time.

The QA benches score what a reader answers; this scores what the extractor
wrote. Two numbers:

* **Memory Integrity (MI)** — of the turns a benchmark marks as gold
  evidence, how many produced at least one memory that cites them. A turn
  nobody cites is a fact the store never held, whatever retrieval does.
* **False Memory Rate (FMR)** — of the memories whose evidence is one to
  three cited or lexically matched turns, how many a judge finds
  UNSUPPORTED by those turns. Window-fallback memories (the whole session
  linked) are counted as `unattributable`, not judged: there is nothing
  specific to check them against.

Both read `memory.evidence`, so the harness needs no ClickHouse access and
runs against any deployment. Dataset shape is LoCoMo (`locomo10.json`) —
`--namespaces-json` maps `sample_id -> namespace` as `locomo_qa.py` writes
it. Pure helpers are unit-tested in `test_memory_integrity.py`.

    python bench/memory_integrity.py --dataset bench/datasets/locomo10.json \\
        --namespaces-json out/locomo.namespaces.json --judge-model gpt-4o-mini
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import re
import statistics
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import httpx

_NORM_RE = re.compile(r"[^a-z0-9]+")
_EVIDENCE_ID_RE = re.compile(r"^D(\d+):(\d+)$")


def norm(text: str) -> str:
    return _NORM_RE.sub(" ", (text or "").lower()).strip()


def gold_turns_by_question(conv: dict[str, Any]) -> list[list[str]]:
    """The gold-evidence turn ids of each question that cites any, in order.

    Question-level integrity counts a question covered when every turn it
    cites has a fact citing it (`all`) or when at least one does (`any`);
    LoCoMo marks both sides of an exchange as evidence, so the reactive
    half drags the turn-level number below what the write path lost."""
    ids: set[str] = set()
    for key, turns in conv.get("conversation", {}).items():
        if (
            not key.startswith("session_")
            or key.endswith("_date_time")
            or not isinstance(turns, list)
        ):
            continue
        for t in turns:
            did = str(t.get("dia_id") or "")
            if did:
                ids.add(did)
    out: list[list[str]] = []
    for q in conv.get("qa", []):
        cited = [
            str(e)
            for e in (q.get("evidence") or [])
            if _EVIDENCE_ID_RE.match(str(e)) and str(e) in ids
        ]
        if cited:
            out.append(cited)
    return out


def gold_turns(conv: dict[str, Any]) -> dict[str, str]:
    """`D<session>:<turn>` -> turn text for every turn a question cites."""
    by_id: dict[str, str] = {}
    for key, turns in conv.get("conversation", {}).items():
        if (
            not key.startswith("session_")
            or key.endswith("_date_time")
            or not isinstance(turns, list)
        ):
            continue
        for t in turns:
            did = str(t.get("dia_id") or "")
            if did:
                by_id[did] = str(t.get("text") or "")
    cited: dict[str, str] = {}
    for q in conv.get("qa", []):
        for ev in q.get("evidence") or []:
            ev = str(ev)
            if _EVIDENCE_ID_RE.match(ev) and ev in by_id:
                cited[ev] = by_id[ev]
    return cited


def turn_is_covered(turn_text: str, evidence_contents: list[str]) -> bool:
    """A gold turn is covered when some cited evidence row carries its text.

    Evidence rows hold the message as sent (`Speaker: text`, captions
    inlined), so the turn text is matched as a normalised substring."""
    needle = norm(turn_text)
    if len(needle) < 8:
        return False
    return any(needle in norm(c) for c in evidence_contents)


_DERIVED_KEY_PREFIXES = ("period_digest_", "consolidated:")
_DERIVED_TAGS = {"period_digest", "consolidated"}


def is_derived_entry(entry: dict[str, Any]) -> bool:
    """A memory.list row that a worker derived from other rows (a period
    digest or a dense topic memory): by key prefix, tag or source_trace. The
    second summary layer, and the only rows `--derived-only` judges."""
    key = str(entry.get("memory_key") or "")
    if key.startswith(_DERIVED_KEY_PREFIXES):
        return True
    tags = entry.get("tags") or []
    if isinstance(tags, list) and _DERIVED_TAGS.intersection(str(t) for t in tags):
        return True
    trace = str(entry.get("source_trace") or "")
    return trace.startswith(("digest.period", "consolidate."))


def classify_evidence(out: dict[str, Any]) -> str:
    """cited | lexical | window | derived | none — the attribution class of one memory.
    `derived` = a consolidated row whose evidence is the memories it compressed."""
    items = out.get("items") or []
    if not items:
        return "none"
    scopes = {str(i.get("scope") or "other") for i in items}
    if scopes <= {"derived"}:
        return "derived"
    if scopes <= {"cited"}:
        return "cited"
    if scopes <= {"cited", "lexical"}:
        return "lexical"
    if "window" in scopes:
        return "window"
    return "none"


_JUDGE_LABEL_RE = re.compile(r"\b(UNSUPPORTED|PARTIAL|SUPPORTED)\b")


def parse_judge_label(reply: str) -> str:
    """The first whole-word label in the reply; UNSUPPORTED when none. Whole
    words, because "UNSUPPORTED" contains "SUPPORTED"."""
    m = _JUDGE_LABEL_RE.search((reply or "").upper())
    return m.group(1) if m else "UNSUPPORTED"


JUDGE_SYSTEM = (
    "You audit an agent's memory store. You are given the source turn(s) a "
    "memory was extracted from and the memory's stored text. Answer with one "
    "word: SUPPORTED if every claim in the memory is stated or directly "
    "entailed by the turns (dates resolved from a relative phrase count as "
    "supported when the turn carries a timestamp); PARTIAL if the memory is "
    "true to the turns but adds, drops, or blurs a detail (a wrong name, a "
    "changed number, an attribute the turn did not give); UNSUPPORTED if the "
    "memory states something the turns do not say or contradict. Do not reward "
    "plausibility; reward what the text says."
)


_TURN_CLASS_SYSTEM = (
    "You classify one conversation turn. Reply with exactly one word: FACT if the turn "
    "states a durable, later-askable fact about the speaker or someone they mention (an "
    "event, possession, plan, preference, date, relationship, activity, or something shown "
    "in a photo the turn describes); REACTION if it only reacts, praises, asks a question, "
    "or adds nothing a memory system should store."
)


_DIGEST_AUDIT_SYSTEM = (
    "You audit a session digest against the transcript it summarises. Reply with one label "
    "then one short reason. The transcript opens with its session date. Before judging, "
    "resolve every relative phrase in the transcript against that date, day by day: "
    "'yesterday' is the day before it, 'two days ago' two days before, 'last Saturday' or "
    "'last Friday' the most recent such weekday before it, 'last night' the evening before. "
    "SUPPORTED: every claim in the digest is stated or directly entailed by the transcript; "
    "a date equal to the session date or to one of those resolutions is supported. "
    "PARTIAL: the digest is true to the transcript but more precise or more intense than it: "
    "an exact day for a vague phrase ('last week', 'a few weeks ago', 'recently', 'this "
    "month'), a count, place or name the transcript never gives, 'loves' for 'likes', a "
    "connection the speaker did not make. UNSUPPORTED: the digest states something the "
    "transcript does not say or contradicts. Check your arithmetic before calling a date wrong."
)

_SESSION_KEY_RE = re.compile(r"(session_\d+)$")
_ISO_DAY_RE = re.compile(r"\b(20\d\d-\d\d-\d\d)\b")
_WEEKDAY_PHRASE_RE = re.compile(
    r"\b(?:last|this past) (?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
    re.IGNORECASE,
)


def period_date_drift(
    value: str,
    source_sessions: list[str],
    digests_by_session: dict[str, dict[str, Any]],
) -> tuple[int, list[str]]:
    """(dates in the period digest, those absent from every input digest's text
        and session date). A period digest may only carry dates its inputs carried
    ; an LLM judge cannot see the inputs' dates, this can."""
    allowed: set[str] = set()
    for sid in source_sessions:
        d = digests_by_session.get(sid) or {}
        allowed.update(_ISO_DAY_RE.findall(str(d.get("summary_text") or "")))
        at = str(d.get("valid_at") or d.get("at") or "")[:10]
        if _ISO_DAY_RE.fullmatch(at):
            allowed.add(at)
    dates = _ISO_DAY_RE.findall(value or "")
    return len(dates), [x for x in dates if x not in allowed]


def source_sessions_of(entry: dict[str, Any]) -> list[str]:
    """The `src:summary:<session_id>` tags a period digest carries."""
    return [
        str(t)[len("src:summary:") :]
        for t in (entry.get("tags") or [])
        if str(t).startswith("src:summary:")
    ]


def check_relative_dates(
    entries: list[dict[str, Any]],
    session_dates: dict[str, str],
) -> tuple[dict[str, int], list[dict[str, str]]]:
    """Deterministic drift gauge for dates. A stored value that
    keeps a day-precise relative phrase ("yesterday", "two days ago", "last
    Friday") and names an ISO day is checked against the server's own
    resolver run on the session date the dataset holds; a judge cannot do
    this arithmetic reliably in either direction (gpt-4o-mini flagged correct
    "yesterday" resolutions, gpt-4.1 passed dates a week off). Returns the
    counts and one row per disagreement. Weekday phrases are counted apart:
    they are where the extractor's convention ("last Friday" = the Friday of
    last week) and the resolver's (the most recent Friday) part ways."""
    from server.memory.domain.extraction_temporal import (
        relative_phrase_class,
        resolve_relative_date,
    )

    counts = {"point": 0, "point_mismatch": 0, "weekday": 0, "weekday_mismatch": 0}
    rows: list[dict[str, str]] = []
    for e in entries:
        value = str(e.get("value") or "")
        key = session_key_of(str(e.get("session_id") or ""))
        session_date = session_dates.get(key, "")
        if not session_date or relative_phrase_class(value) != "point":
            continue
        resolved = resolve_relative_date(value, session_date)
        isos = _ISO_DAY_RE.findall(value)
        if not resolved or not isos:
            continue
        weekday = bool(_WEEKDAY_PHRASE_RE.search(value))
        counts["point"] += 1
        counts["weekday"] += int(weekday)
        if resolved in isos:
            continue
        counts["point_mismatch"] += 1
        counts["weekday_mismatch"] += int(weekday)
        rows.append(
            {
                "session": key,
                "session_date": session_date,
                "resolver": resolved,
                "stored": isos[0],
                "memory_key": str(e.get("memory_key") or ""),
                "value": value,
            }
        )
    return counts, rows


def session_key_of(session_id: str) -> str:
    """`<namespace>:session_7` -> `session_7` (how locomo_qa.py names sessions)."""
    m = _SESSION_KEY_RE.search(session_id or "")
    return m.group(1) if m else ""


def parse_digest_verdict(reply: str) -> tuple[str, str]:
    """(label, drift_class): label in SUPPORTED/PARTIAL/UNSUPPORTED; drift_class is
    `date` when the reason names a date/time the transcript lacks, else `other`
    (empty for SUPPORTED)."""
    text = (reply or "").strip()
    up = text.upper()
    label = "UNSUPPORTED"
    for cand in ("SUPPORTED", "PARTIAL", "UNSUPPORTED"):
        if up.startswith(cand):
            label = cand
            break
    else:
        for cand in ("UNSUPPORTED", "PARTIAL", "SUPPORTED"):
            if cand in up:
                label = cand
                break
    if label == "SUPPORTED":
        return label, ""
    low = text.lower()
    cls = (
        "date"
        if any(w in low for w in ("date", "time", "day", "month", "year", "when"))
        else "other"
    )
    return label, cls


def parse_turn_class(reply: str) -> str:
    return "FACT" if (reply or "").strip().upper().startswith("FACT") else "REACTION"


@dataclass
class MemoryAudit:
    namespace: str
    memory_key: str
    value: str
    attribution: str  # cited | lexical | window | none
    n_evidence: int
    judge_label: str = ""  # SUPPORTED | PARTIAL | UNSUPPORTED | "" (not judged)
    judge_reply: str = ""
    evidence_excerpt: str = ""


@dataclass
class NamespaceIntegrity:
    sample_id: str
    namespace: str
    memories: int = 0
    by_attribution: dict[str, int] = field(default_factory=dict)
    gold_turns: int = 0
    gold_turns_covered: int = 0
    # question-level coverage (see gold_turns_by_question)
    questions: int = 0
    questions_all_cited: int = 0
    questions_any_cited: int = 0
    # --classify-uncited: uncited gold turns labelled by a judge
    uncited_fact_bearing: int = 0
    uncited_reaction: int = 0
    # --digests: session digests judged against their transcript
    # derived (consolidated) rows judged against the memories they compressed
    derived_judged: int = 0
    derived_unsupported: int = 0
    derived_partial: int = 0
    digests: int = 0
    digest_supported: int = 0
    digest_partial: int = 0
    digest_unsupported: int = 0
    digest_date_precision: int = 0
    # deterministic date check: stored values that keep a
    # day-precise relative phrase and name an ISO day, and how many of those
    # days disagree with the resolver run on the dataset's session date
    dated_point_facts: int = 0
    dated_point_mismatch: int = 0
    dated_weekday_facts: int = 0
    dated_weekday_mismatch: int = 0
    date_mismatches: list[dict[str, str]] = field(default_factory=list)
    # derived rows: dates in a period digest that none of its input digests
    # (text or session date) carried — the "copied, never resolved" rule for
    # the second summary layer, checked by arithmetic
    derived_dates: int = 0
    derived_dates_invented: int = 0
    derived_date_examples: list[dict[str, str]] = field(default_factory=list)
    # one row per judged digest (session, label, class, judge reason, digest text)
    # so a drift count can be read back to the sentences that produced it
    digest_verdicts: list[dict[str, str]] = field(default_factory=list)
    judged: int = 0
    supported: int = 0
    partial: int = 0
    unsupported: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def memory_integrity(self) -> float | None:
        return round(self.gold_turns_covered / self.gold_turns, 4) if self.gold_turns else None

    @property
    def false_memory_rate(self) -> float | None:
        return round(self.unsupported / self.judged, 4) if self.judged else None


async def _post(
    client: httpx.AsyncClient, url: str, body: dict[str, Any], token: str
) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {token}", "content-type": "application/json"}
    for attempt in range(8):
        try:
            resp = await client.post(url, json=body, headers=headers, timeout=60.0)
        except httpx.TransportError:
            if attempt == 7:
                raise
            await asyncio.sleep(1.5 * (attempt + 1))
            continue
        if resp.status_code == 429 and attempt < 7:
            # The server's per-token rate limit: honour Retry-After, else back off.
            try:
                wait = float(resp.headers.get("Retry-After") or 0) or 5.0 * (attempt + 1)
            except ValueError:
                wait = 5.0 * (attempt + 1)
            await asyncio.sleep(min(wait, 60.0))
            continue
        if resp.status_code >= 500 and attempt < 4:
            await asyncio.sleep(1.5 * (attempt + 1))
            continue
        resp.raise_for_status()
        return resp.json()
    raise RuntimeError("unreachable")


async def _judge_text(
    client: httpx.AsyncClient,
    *,
    api_key: str,
    model: str,
    system: str,
    user: str,
    usage: dict[str, int],
    max_tokens: int = 5,
) -> str:
    body = {
        "model": model,
        "temperature": 0,
        "max_tokens": max_tokens,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
    }
    url = os.environ.get("BENCH_OPENAI_CHAT_URL", "https://api.openai.com/v1/chat/completions")
    resp = await client.post(
        url, json=body, headers={"Authorization": f"Bearer {api_key}"}, timeout=60.0
    )
    resp.raise_for_status()
    data = resp.json()
    u = data.get("usage") or {}
    usage["tin"] += int(u.get("prompt_tokens") or 0)
    usage["tout"] += int(u.get("completion_tokens") or 0)
    usage["calls"] += 1
    return str(data["choices"][0]["message"]["content"] or "")


async def _judge(
    client: httpx.AsyncClient,
    *,
    api_key: str,
    model: str,
    evidence: str,
    value: str,
    usage: dict[str, int],
) -> str:
    """The False Memory Rate judge: one memory against its source turns."""
    return await _judge_text(
        client,
        api_key=api_key,
        model=model,
        system=JUDGE_SYSTEM,
        user=f"Source turn(s):\n{evidence}\n\nMemory:\n{value}\n\nLabel:",
        usage=usage,
    )


async def list_memories(
    client: httpx.AsyncClient, base_url: str, token: str, identity: dict[str, str]
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    offset = 0
    while True:
        page = await _post(
            client,
            base_url + "/v1/memory.list",
            {**identity, "limit": 1000, "offset": offset},
            token,
        )
        entries = page.get("entries") or []
        out.extend(entries)
        if not page.get("truncated") or not entries:
            return out
        offset += len(entries)


async def audit_namespace(
    client: httpx.AsyncClient,
    *,
    base_url: str,
    token: str,
    identity: dict[str, str],
    conv: dict[str, Any] | None,
    api_key: str,
    judge_model: str,
    concurrency: int,
    usage: dict[str, int],
    limit: int,
    classify_uncited: bool = False,
    audit_digests: bool = False,
    derived_only: bool = False,
    derived_source_cap: int = 8,
) -> tuple[NamespaceIntegrity, list[MemoryAudit]]:
    ns = NamespaceIntegrity(
        sample_id=str((conv or {}).get("sample_id") or identity["namespace"]),
        namespace=identity["namespace"],
    )
    entries = await list_memories(client, base_url, token, identity)
    if derived_only:
        entries = [e for e in entries if is_derived_entry(e)]
    if limit:
        entries = entries[:limit]
    ns.memories = len(entries)
    if conv:
        from bench.locomo_qa import iter_sessions

        session_dates = {k: d for k, d, _turns in iter_sessions(conv.get("conversation", {}))}
        counts, mismatches = check_relative_dates(entries, session_dates)
        ns.dated_point_facts = counts["point"]
        ns.dated_point_mismatch = counts["point_mismatch"]
        ns.dated_weekday_facts = counts["weekday"]
        ns.dated_weekday_mismatch = counts["weekday_mismatch"]
        ns.date_mismatches = mismatches
    sem = asyncio.Semaphore(concurrency)
    cited_contents: list[str] = []
    audits: list[MemoryAudit] = []

    async def one(entry: dict[str, Any]) -> None:
        key = str(entry.get("memory_key") or "")
        async with sem:
            try:
                ev = await _post(
                    client,
                    base_url + "/v1/memory.evidence",
                    {**identity, "memory_key": key, "limit": 50},
                    token,
                )
            except Exception as e:  # noqa: BLE001
                ns.errors.append(f"{key}: {type(e).__name__}: {e}")
                return
            attribution = classify_evidence(ev)
            items = ev.get("items") or []
            value = str(ev.get("value") or entry.get("value") or "")
            audit = MemoryAudit(
                namespace=identity["namespace"],
                memory_key=key,
                value=value[:400],
                attribution=attribution,
                n_evidence=int(ev.get("cited") or 0)
                + int(ev.get("lexical") or 0)
                + int(ev.get("window") or 0)
                + int(ev.get("derived") or 0),
            )
            for it in items:
                if it.get("scope") in ("cited", "lexical"):
                    cited_contents.append(str(it.get("content") or ""))
            judge_this = (attribution in ("cited", "lexical") and len(items) <= 3) or (
                attribution == "derived" and len(items) <= derived_source_cap
            )
            if judge_this and api_key:
                evidence_text = "\n".join(
                    f"[{it.get('role') or 'turn'}] {it.get('content') or ''}" for it in items
                )
                audit.evidence_excerpt = evidence_text[:400]
                try:
                    reply = await _judge(
                        client,
                        api_key=api_key,
                        model=judge_model,
                        evidence=evidence_text,
                        value=value,
                        usage=usage,
                    )
                    audit.judge_reply = reply.strip()[:40]
                    audit.judge_label = parse_judge_label(reply)
                except Exception as e:  # noqa: BLE001
                    ns.errors.append(f"{key}: judge {type(e).__name__}: {e}")
            audits.append(audit)

    await asyncio.gather(*(one(e) for e in entries))
    period_rows = [e for e in entries if source_sessions_of(e)]
    if period_rows:
        try:
            dg = await _post(
                client,
                base_url + "/v1/memory.digests",
                {**identity, "session_id": "*", "limit": 2000},
                token,
            )
            by_session = {str(r.get("session_id") or ""): r for r in (dg.get("rows") or [])}
        except Exception as e:  # noqa: BLE001
            ns.errors.append(f"digests for period check: {type(e).__name__}: {e}")
            by_session = {}
        for e in period_rows:
            raw = e.get("value")
            text = str(raw)
            if text.startswith('"'):
                try:
                    text = str(json.loads(text))
                except Exception:  # noqa: BLE001
                    pass
            n, invented = period_date_drift(text, source_sessions_of(e), by_session)
            ns.derived_dates += n
            ns.derived_dates_invented += len(invented)
            if invented:
                ns.derived_date_examples.append(
                    {"memory_key": str(e.get("memory_key") or ""), "invented": ",".join(invented)}
                )
    for a in audits:
        ns.by_attribution[a.attribution] = ns.by_attribution.get(a.attribution, 0) + 1
        if a.judge_label and a.attribution == "derived":
            ns.derived_judged += 1
            ns.derived_partial += a.judge_label == "PARTIAL"
            ns.derived_unsupported += a.judge_label == "UNSUPPORTED"
        elif a.judge_label:
            ns.judged += 1
            ns.supported += a.judge_label == "SUPPORTED"
            ns.partial += a.judge_label == "PARTIAL"
            ns.unsupported += a.judge_label == "UNSUPPORTED"
    if conv is not None:
        turns = gold_turns(conv)
        covered = {did: turn_is_covered(t, cited_contents) for did, t in turns.items()}
        ns.gold_turns = len(turns)
        ns.gold_turns_covered = sum(covered.values())
        for cited in gold_turns_by_question(conv):
            flags = [covered.get(d, False) for d in cited]
            ns.questions += 1
            ns.questions_all_cited += all(flags)
            ns.questions_any_cited += any(flags)
        if classify_uncited and api_key:
            uncited = [(did, turns[did]) for did, ok in covered.items() if not ok]

            async def classify(did: str, text: str) -> None:
                async with sem:
                    try:
                        reply = await _judge_text(
                            client,
                            api_key=api_key,
                            model=judge_model,
                            system=_TURN_CLASS_SYSTEM,
                            user=text,
                            usage=usage,
                        )
                    except Exception as e:  # noqa: BLE001
                        ns.errors.append(f"{did}: classify {type(e).__name__}: {e}")
                        return
                    if parse_turn_class(reply) == "FACT":
                        ns.uncited_fact_bearing += 1
                    else:
                        ns.uncited_reaction += 1

            await asyncio.gather(*(classify(d, t) for d, t in uncited))
        if audit_digests and api_key:
            await _audit_digests(
                client,
                ns,
                base_url=base_url,
                token=token,
                identity=identity,
                conv=conv,
                api_key=api_key,
                judge_model=judge_model,
                sem=sem,
                usage=usage,
            )
    return ns, audits


async def _audit_digests(
    client: httpx.AsyncClient,
    ns: NamespaceIntegrity,
    *,
    base_url: str,
    token: str,
    identity: dict[str, str],
    conv: dict[str, Any],
    api_key: str,
    judge_model: str,
    sem: asyncio.Semaphore,
    usage: dict[str, int],
) -> None:
    """Judge every session digest (`memory.digests`) against the transcript
    the dataset holds for that session: the digest is the one
    derived row written on every ingest, and the drift the agent-memory
    governance literature warns about starts there."""
    from bench.locomo_qa import iter_sessions, turn_to_message

    # The session date heads the transcript: the digest is written against it
    # (observation_date), so "On <session date>" and a relative phrase resolved
    # against it are supported, not invented precision.
    transcripts = {
        key: f"Session date: {date or 'unknown'}\n"
        + "\n".join(f"[{m['role']}] {m['content']}" for m in map(turn_to_message, turns))
        for key, date, turns in iter_sessions(conv.get("conversation", {}))
    }
    try:
        out = await _post(
            client,
            base_url + "/v1/memory.digests",
            {**identity, "session_id": "*", "limit": 2000},
            token,
        )
    except Exception as e:  # noqa: BLE001
        ns.errors.append(f"digests: {type(e).__name__}: {e}")
        return

    async def one(row: dict[str, Any]) -> None:
        key = session_key_of(str(row.get("session_id") or ""))
        transcript = transcripts.get(key)
        if not transcript:
            return
        async with sem:
            try:
                reply = await _judge_text(
                    client,
                    api_key=api_key,
                    model=judge_model,
                    system=_DIGEST_AUDIT_SYSTEM,
                    user=(
                        f"Transcript:\n{transcript[:12000]}\n\nDigest:\n"
                        f"{row.get('summary_text', '')}\n\nLabel, then one short reason:"
                    ),
                    usage=usage,
                    max_tokens=40,
                )
            except Exception as e:  # noqa: BLE001
                ns.errors.append(f"digest {key}: {type(e).__name__}: {e}")
                return
        label, cls = parse_digest_verdict(reply)
        ns.digests += 1
        if label == "SUPPORTED":
            ns.digest_supported += 1
        elif label == "PARTIAL":
            ns.digest_partial += 1
        else:
            ns.digest_unsupported += 1
        if cls == "date":
            ns.digest_date_precision += 1
        ns.digest_verdicts.append(
            {
                "session": key,
                "label": label,
                "drift_class": cls,
                "reason": reply.strip(),
                "digest": str(row.get("summary_text", "")),
            }
        )

    await asyncio.gather(*(one(r) for r in (out.get("rows") or [])))


def summarize(results: list[NamespaceIntegrity]) -> dict[str, Any]:
    mem = sum(r.memories for r in results)
    attr: dict[str, int] = {}
    for r in results:
        for k, v in r.by_attribution.items():
            attr[k] = attr.get(k, 0) + v
    gold = sum(r.gold_turns for r in results)
    covered = sum(r.gold_turns_covered for r in results)
    qn = sum(r.questions for r in results)
    q_all = sum(r.questions_all_cited for r in results)
    q_any = sum(r.questions_any_cited for r in results)
    fact_b = sum(r.uncited_fact_bearing for r in results)
    react = sum(r.uncited_reaction for r in results)
    dj = sum(r.derived_judged for r in results)
    du = sum(r.derived_unsupported for r in results)
    dp = sum(r.derived_partial for r in results)
    dg = sum(r.digests for r in results)
    dg_ok = sum(r.digest_supported for r in results)
    dated = sum(r.dated_point_facts for r in results)
    dated_miss = sum(r.dated_point_mismatch for r in results)
    weekday = sum(r.dated_weekday_facts for r in results)
    weekday_miss = sum(r.dated_weekday_mismatch for r in results)
    ddates = sum(r.derived_dates for r in results)
    dinv = sum(r.derived_dates_invented for r in results)
    dg_part = sum(r.digest_partial for r in results)
    dg_bad = sum(r.digest_unsupported for r in results)
    dg_date = sum(r.digest_date_precision for r in results)
    judged = sum(r.judged for r in results)
    unsupported = sum(r.unsupported for r in results)
    partial = sum(r.partial for r in results)
    return {
        "namespaces": len(results),
        "memories": mem,
        "attribution": attr,
        "attributable_rate": round((attr.get("cited", 0) + attr.get("lexical", 0)) / mem, 4)
        if mem
        else None,
        "gold_turns": gold,
        "gold_turns_covered": covered,
        "memory_integrity": round(covered / gold, 4) if gold else None,
        "questions": qn,
        "mi_questions_all": round(q_all / qn, 4) if qn else None,
        "mi_questions_any": round(q_any / qn, 4) if qn else None,
        "uncited_fact_bearing": fact_b,
        "uncited_reaction": react,
        # integrity over the turns that carried a fact: covered / (covered + missed facts)
        "derived_judged": dj,
        "derived_fmr": round(du / dj, 4) if dj else None,
        "derived_partial_rate": round(dp / dj, 4) if dj else None,
        "digests": dg,
        "digest_supported": dg_ok,
        "digest_partial": dg_part,
        "digest_unsupported": dg_bad,
        "digest_date_precision": dg_date,
        # fidelity = fully supported share; drift = partial + unsupported
        "digest_fidelity": round(dg_ok / dg, 4) if dg else None,
        "dated_point_facts": dated,
        "dated_point_mismatch": dated_miss,
        "relative_date_mismatch_rate": round(dated_miss / dated, 4) if dated else None,
        "dated_weekday_facts": weekday,
        "dated_weekday_mismatch": weekday_miss,
        "weekday_mismatch_rate": round(weekday_miss / weekday, 4) if weekday else None,
        "derived_dates": ddates,
        "derived_dates_invented": dinv,
        "derived_date_drift_rate": round(dinv / ddates, 4) if ddates else None,
        "memory_integrity_fact_bearing": (
            round(covered / (covered + fact_b), 4)
            if (fact_b or react) and (covered + fact_b)
            else None
        ),
        "judged": judged,
        "false_memory_rate": round(unsupported / judged, 4) if judged else None,
        "partial_rate": round(partial / judged, 4) if judged else None,
        "errors": sum(len(r.errors) for r in results),
    }


async def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--base-url", default=os.environ.get("LOOM_BASE_URL", "http://localhost:7777"))
    ap.add_argument("--token", default=os.environ.get("LOOM_API_KEY", ""))
    ap.add_argument("--org", default="dev")
    ap.add_argument("--agent", default="locomo-bench")
    ap.add_argument("--dataset", default="", help="locomo10.json; omit to skip Memory Integrity")
    ap.add_argument(
        "--namespaces-json", required=True, help="{sample_id: namespace} as locomo_qa.py writes"
    )
    ap.add_argument("--judge-model", default="gpt-4o-mini")
    ap.add_argument(
        "--no-judge", action="store_true", help="attribution + integrity only, no LLM calls"
    )
    ap.add_argument(
        "--digests",
        action="store_true",
        help="also judge every session digest (memory.digests) against the dataset transcript "
        "of its session: digest_fidelity, partial / unsupported counts, date-precision drift",
    )
    ap.add_argument(
        "--derived-only",
        action="store_true",
        help="judge only worker-derived rows (period digests, dense topic memories) against "
        "the memories they compressed — the second summary layer; the "
        "integrity number then covers those rows alone and is not the namespace's",
    )
    ap.add_argument(
        "--derived-sources",
        type=int,
        default=8,
        help="judge a derived row only when it names at most this many sources (default 8; "
        "a period digest over five sessions names about thirty)",
    )
    ap.add_argument(
        "--classify-uncited",
        action="store_true",
        help="label each uncited gold turn FACT or REACTION with the judge model and report "
        "memory_integrity_fact_bearing (integrity over the turns that carried a fact)",
    )
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--limit", type=int, default=0, help="memories per namespace (0 = all)")
    ap.add_argument("--out", default="bench/memory_integrity.csv")
    args = ap.parse_args()

    namespaces: dict[str, str] = json.loads(Path(args.namespaces_json).read_text())
    convs: dict[str, dict[str, Any]] = {}
    if args.dataset:
        for c in json.loads(Path(args.dataset).read_text()):
            convs[str(c.get("sample_id"))] = c
    api_key = "" if args.no_judge else os.environ.get("OPENAI_API_KEY", "")
    if not args.no_judge and not api_key:
        print("OPENAI_API_KEY unset: attribution + integrity only (pass --no-judge to silence)")
    usage = {"tin": 0, "tout": 0, "calls": 0}
    results: list[NamespaceIntegrity] = []
    audits: list[MemoryAudit] = []
    async with httpx.AsyncClient() as client:
        for sample_id, namespace in namespaces.items():
            # Extracted rows carry the extraction's session_id; "*" lists and
            # resolves across sessions (memory.list and memory.evidence).
            identity = {
                "org": args.org,
                "namespace": namespace,
                "agent": args.agent,
                "session_id": "*",
            }
            ns, rows = await audit_namespace(
                client,
                base_url=args.base_url,
                token=args.token,
                identity=identity,
                conv=convs.get(sample_id),
                api_key=api_key,
                judge_model=args.judge_model,
                concurrency=args.concurrency,
                usage=usage,
                limit=args.limit,
                classify_uncited=bool(args.classify_uncited),
                audit_digests=bool(args.digests),
                derived_only=bool(args.derived_only),
                derived_source_cap=int(args.derived_sources),
            )
            results.append(ns)
            audits.extend(rows)
            print(
                f"[{sample_id}] memories={ns.memories} attribution={ns.by_attribution} "
                f"MI={ns.memory_integrity} FMR={ns.false_memory_rate} "
                f"judged={ns.judged} errors={len(ns.errors)}",
                flush=True,
            )
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(asdict(audits[0]).keys()) if audits else ["empty"])
        w.writeheader()
        for a in audits:
            w.writerow(asdict(a))
    summary = {
        **summarize(results),
        "judge_model": args.judge_model if api_key else "",
        "judge_tokens_in": usage["tin"],
        "judge_tokens_out": usage["tout"],
        "judge_calls": usage["calls"],
        "median_evidence_per_memory": statistics.median([a.n_evidence for a in audits])
        if audits
        else None,
        "per_namespace": [
            {
                "sample_id": r.sample_id,
                "namespace": r.namespace,
                "memories": r.memories,
                "attribution": r.by_attribution,
                "memory_integrity": r.memory_integrity,
                "false_memory_rate": r.false_memory_rate,
                "judged": r.judged,
                "errors": r.errors[:5],
            }
            for r in results
        ],
    }
    out.with_suffix(".summary.json").write_text(json.dumps(summary, indent=1))
    verdicts = [{"namespace": a.namespace, **v} for a in results for v in a.digest_verdicts]
    if verdicts:
        out.with_suffix(".digests.jsonl").write_text(
            "".join(json.dumps(v) + "\n" for v in verdicts)
        )
    mismatches = [{"namespace": a.namespace, **m} for a in results for m in a.date_mismatches]
    if mismatches:
        out.with_suffix(".dates.jsonl").write_text(
            "".join(json.dumps(m) + "\n" for m in mismatches)
        )
    s = summary
    print(
        f"\nmemories={s['memories']} attributable={s['attributable_rate']} "
        f"(cited/lexical/window/none = {s['attribution']})\n"
        f"memory_integrity={s['memory_integrity']} "
        f"({s['gold_turns_covered']}/{s['gold_turns']} gold turns cited); "
        f"questions all/any cited {s['mi_questions_all']}/{s['mi_questions_any']} "
        f"over {s['questions']}"
    )
    if s["derived_judged"]:
        print(
            f"derived rows: {s['derived_judged']} judged against their sources, "
            f"fmr={s['derived_fmr']} partial={s['derived_partial_rate']}"
        )
    if s["digests"]:
        print(
            f"digests: {s['digests']} judged, fidelity={s['digest_fidelity']} "
            f"(supported {s['digest_supported']}, partial {s['digest_partial']}, "
            f"unsupported {s['digest_unsupported']}; {s['digest_date_precision']} add a date "
            f"or time the transcript lacks)"
        )
    if s["derived_dates"]:
        print(
            f"period digests: {s['derived_dates']} dates, {s['derived_dates_invented']} absent "
            f"from every input digest (drift {s['derived_date_drift_rate']})"
        )
    if s["dated_point_facts"]:
        print(
            f"relative dates: {s['dated_point_facts']} day-precise facts name a date, "
            f"{s['dated_point_mismatch']} disagree with the resolver "
            f"(weekday phrases {s['dated_weekday_mismatch']}/{s['dated_weekday_facts']})"
        )
    if s["uncited_fact_bearing"] or s["uncited_reaction"]:
        print(
            f"uncited gold turns: {s['uncited_fact_bearing']} fact-bearing, "
            f"{s['uncited_reaction']} reactions → "
            f"memory_integrity_fact_bearing={s['memory_integrity_fact_bearing']}"
        )
    print(
        f"false_memory_rate={s['false_memory_rate']} partial={s['partial_rate']} "
        f"over {s['judged']} judged\n"
        f"errors={s['errors']} → {out}, {out.with_suffix('.summary.json')}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
