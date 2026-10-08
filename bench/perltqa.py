"""PerLTQA adapter: a personal long-term memory bank per character — profile, social
relationships, dated events and event-tied dialogues — ingested into one loom namespace
each, then the dataset's questions asked through `memory.ask`.

What it measures, per memory section and overall:
  anchor_map     — the dataset's metric: share of a question's memory anchors
                   (answer phrases located in the reference memory) that the
                   synthesized answer reproduces. Reads the answer, not the
                   retrieval, so a synthesis regression shows while recall holds.
  ref_recall@k   — `memory.search` top-k holds a row extracted from the
                   question's reference memory (rows carry `ref:<id>` tags at
                   ingest; `memory.list` maps keys back to references).
  cited_ref      — `memory.ask` cited a row from the reference memory.
  correctness    — optional LLM judge (OPENAI_API_KEY) over question, gold and
                   generated answer, the dataset's third metric.
  abstain rate, ask latency p50.

Ingest shape (one namespace per character; `--namespace-json` registers it):
  profile attributes     → memory.set, key `<protagonist>_<attribute>`, tag
                           `ref:profile:<Attribute>`; the narrative description
                           → set_from_messages, tag `ref:profile_description`.
  social relationships   → set_from_messages per entry ("<P>'s <relationship>
                           is <name>. <description>"), tag `ref:<id>`.
  events                 → set_from_messages per event, observation_date from
                           `Creation Time`, tags `ref:<id>`, `theme:<slug>`.
  dialogues              → set_from_messages with one message per utterance
                           (`name` = speaker, `timestamp` = the dialogue's
                           stamp), tag `ref:<id>`; with --record-conversations
                           also conversation.set_logging(on) + one
                           conversation.append per utterance (thread = id).

Dataset: https://github.com/Elvin-Yiming-Du/PerLTQA (CC BY-NC 4.0, research
use). `Dataset/en_v2/perltmem_en_v2.json` + `perltqa_en_v2.json`: 31 shared
characters, 1,905 questions (profile 357, social 294, events 628, dialogues 626).

Usage:
  LOOM_API_KEY=… python bench/perltqa.py --mem perltmem_en_v2.json --qa perltqa_en_v2.json \
      --max-characters 5 --max-questions-per-section 10 --base-url http://127.0.0.1:7777 \
      --org <org> --namespace-json '{"default_conflict_policy":"newest_wins"}' \
      --judge-model gpt-4o-mini --out perltqa_run.jsonl
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import hashlib
import json
import os
import re
import secrets
import statistics
import sys
import time
from datetime import datetime
from typing import Any

import httpx

_OPENAI_CHAT_URL = "https://api.openai.com/v1/chat/completions"
SECTIONS = ("profile", "social_relationship", "events", "dialogues")
_DATE_FORMATS = ("%B %d, %Y", "%b %d, %Y", "%Y-%m-%d", "%B %Y", "%Y/%m/%d", "%d %B %Y", "%Y")


# --- pure helpers (unit-tested) ------------------------------------------------


def slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", str(text or "").lower()).strip("_")
    return s[:48] or "x"


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", str(text or "").lower())).strip()


def anchor_phrases(record: dict[str, Any]) -> list[str]:
    """The anchor phrases of a QA record: keys of the `Memory Anchors` dicts."""
    out: list[str] = []
    for a in record.get("Memory Anchors") or []:
        if isinstance(a, dict):
            out.extend(str(k) for k in a.keys() if str(k).strip())
        elif isinstance(a, str) and a.strip():
            out.append(a)
    return out


def anchor_map(answer: str, anchors: list[str]) -> float | None:
    """Share of anchor phrases present in the answer (case, punctuation and
    whitespace folded). None when the record has no anchors."""
    if not anchors:
        return None
    norm = f" {normalize(answer)} "
    hit = sum(1 for a in anchors if normalize(a) and f" {normalize(a)} " in norm)
    return hit / len(anchors)


def reference_ids(record: dict[str, Any], section: str) -> list[str]:
    """`Reference Memory` → ref ids. Profile records name the attribute
    (`Gender`); the other sections carry a repr'd list (`"['4_0_0']"`)."""
    raw = record.get("Reference Memory")
    if raw is None:
        return []
    if section == "profile":
        # The attribute row (memory.set) and the facts extracted from the
        # narrative description both carry the answer.
        return [f"profile:{str(raw).strip()}", "profile_description"]
    if isinstance(raw, list):
        return [str(x) for x in raw]
    s = str(raw).strip()
    if s.startswith("["):
        try:
            v = ast.literal_eval(s)
            return [str(x) for x in (v if isinstance(v, (list, tuple)) else [v])]
        except (ValueError, SyntaxError):
            pass
    return [s]


def parse_event_date(text: str) -> str:
    """'May 12, 2022' → '2022-05-12'; '' when the stamp does not parse."""
    s = str(text or "").strip()
    if not s:
        return ""
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", s)
    return m.group(0) if m else ""


def dialogue_messages(dialogue: dict[str, Any]) -> list[dict[str, str]]:
    """`contents` {stamp: ["Speaker: text", …]} → set_from_messages messages
    with the speaker as `name` and the stamp as `timestamp`."""
    out: list[dict[str, str]] = []
    for stamp, lines in (dialogue.get("contents") or {}).items():
        ts = ""
        try:
            ts = datetime.strptime(str(stamp).strip(), "%Y-%m-%d %H:%M").isoformat()
        except ValueError:
            ts = parse_event_date(str(stamp))
        for line in lines or []:
            text = str(line).strip()
            if not text:
                continue
            name, content = "", text
            if ":" in text[:60]:
                head, tail = text.split(":", 1)
                if head.strip() and len(head.split()) <= 4:
                    name, content = head.strip(), tail.strip()
            m: dict[str, str] = {"role": "user", "content": content or text}
            if name:
                m["name"] = name
            if ts:
                m["timestamp"] = ts
            out.append(m)
    return out


def qa_records(
    qa: list[dict[str, Any]] | dict[str, Any],
) -> dict[str, dict[str, list[dict[str, Any]]]]:
    """PerLT_TRACKER → {character: {section: [record]}}. Event and dialogue
    sections nest as [{ref_id: [record]}]; profile and social are flat."""
    entries = qa if isinstance(qa, list) else [qa]
    out: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for entry in entries:
        for name, sections in entry.items():
            per = out.setdefault(name, {})
            for sec, recs in (sections or {}).items():
                flat = per.setdefault(sec, [])
                for r in recs or []:
                    if isinstance(r, dict) and "Question" in r:
                        flat.append(r)
                    elif isinstance(r, dict):
                        for ref, inner in r.items():
                            for x in inner or []:
                                if isinstance(x, dict) and "Question" in x:
                                    x = dict(x)
                                    x.setdefault("Reference Memory", f"['{ref}']")
                                    flat.append(x)
    return out


def refs_of_tags(tags: list[str]) -> set[str]:
    return {t[4:] for t in tags if isinstance(t, str) and t.startswith("ref:")}


def kin_refs(mem: dict[str, Any]) -> dict[str, set[str]]:
    """Reference id → the ids that hold the same facts: a dialogue is tied to
    the event it happened in (`dialogues[id]["events"]`), and the event's
    narrative states what the dialogue says. A hit from either is the
    memory the question is about; `ref_recall_kin` credits both, `ref_recall`
    only the exact reference."""
    out: dict[str, set[str]] = {}
    for did, dl in (mem.get("dialogues") or {}).items():
        eid = str((dl or {}).get("events") or "").strip()
        if eid:
            out.setdefault(did, set()).add(eid)
            out.setdefault(eid, set()).add(did)
    return out


# --- transport -------------------------------------------------------------------

RETRIES: dict[str, int] = {}


async def _post(
    client: httpx.AsyncClient, url: str, body: dict[str, Any], token: str
) -> dict[str, Any]:
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {token}"}
    last: httpx.Response | None = None
    for attempt in range(6):
        try:
            r = await client.post(url, json=body, headers=headers, timeout=300.0)
        except httpx.TransportError:
            if attempt >= 2:
                raise
            await asyncio.sleep(2.0 * (attempt + 1))
            continue
        if r.status_code < 500 and r.status_code != 429:
            r.raise_for_status()
            return r.json()
        last = r
        RETRIES[url.rsplit("/", 1)[-1]] = RETRIES.get(url.rsplit("/", 1)[-1], 0) + 1
        reset = r.headers.get("retry-after") or r.headers.get("x-ratelimit-reset")
        try:
            delay = float(reset) if reset else 2.0 * (attempt + 1)
        except ValueError:
            delay = 2.0 * (attempt + 1)
        await asyncio.sleep(min(max(delay, 1.0), 60.0))
    if last is not None:
        last.raise_for_status()
    raise RuntimeError("unreachable")


async def _judge(
    client: httpx.AsyncClient, *, model: str, api_key: str, question: str, gold: str, answer: str
) -> str | None:
    prompt = (
        "You grade an answer from a personal memory assistant.\n"
        f"Question: {question}\nReference answer: {gold}\nGenerated answer: {answer}\n\n"
        "Does the generated answer state the same fact(s) as the reference answer? "
        "Extra correct detail is fine; a missing or contradicting fact is not. "
        "Reply with exactly one word: yes or no."
    )
    try:
        r = await client.post(
            _OPENAI_CHAT_URL,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": model,
                "temperature": 0,
                "max_tokens": 3,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=60.0,
        )
        r.raise_for_status()
        reply = (r.json()["choices"][0]["message"]["content"] or "").strip().lower()
        return "yes" if reply.startswith("yes") else "no"
    except Exception:  # noqa: BLE001 — a judge failure is a missing label, not a wrong one
        return None


# --- ingest -------------------------------------------------------------------------


async def ingest_character(
    client: httpx.AsyncClient,
    *,
    base: str,
    token: str,
    org: str,
    ns: str,
    name: str,
    mem: dict[str, Any],
    namespace_json: dict[str, Any],
    record_conversations: bool,
    sem: asyncio.Semaphore,
    defer: bool,
) -> dict[str, int]:
    agent = "perltqa-bench"
    ident = {"org": org, "namespace": ns, "agent": agent, "user_id": "-"}
    await _post(
        client,
        f"{base}/v1/namespace.register",
        {
            "org": org,
            "namespace": ns,
            "owner_email": "bench@loom.local",
            "description": f"PerLTQA memory bank: {name}",
            **namespace_json,
        },
        token,
    )
    counts = {
        "profile": 0,
        "social_relationship": 0,
        "events": 0,
        "dialogues": 0,
        "conversation_turns": 0,
    }
    jobs: list[Any] = []

    async def _sfm(
        messages: list[dict[str, Any]],
        tags: list[str],
        observation_date: str = "",
        session_id: str = "-",
    ) -> None:
        body: dict[str, Any] = {
            **ident,
            "session_id": session_id,
            "messages": messages,
            "extra_tags": tags,
            "defer": defer,
        }
        if observation_date:
            body["observation_date"] = observation_date
        async with sem:
            await _post(client, f"{base}/v1/memory.set_from_messages", body, token)

    prof = mem.get("profile") or {}
    protagonist = str(prof.get("Protagonist") or name)
    for attr, val in prof.items():
        if attr == "Protagonist" or not str(val).strip():
            continue
        counts["profile"] += 1

        async def _set(attr: str = attr, val: Any = val) -> None:
            async with sem:
                await _post(
                    client,
                    f"{base}/v1/memory.set",
                    {
                        **ident,
                        "session_id": "profile",
                        "memory_key": f"{slug(protagonist)}_{slug(attr)}",
                        "memory_type": "semantic",
                        "value": f"{protagonist}'s {attr.lower()}: {val}.",
                        "tags": [f"ref:profile:{attr}", "perltqa:profile"],
                        "entities": [protagonist.lower()],
                    },
                    token,
                )

        jobs.append(_set())
    desc = str(mem.get("profile_description") or "").strip()
    if desc:
        jobs.append(
            _sfm(
                [{"role": "user", "content": desc[:6000]}],
                ["ref:profile_description", "perltqa:profile"],
            )
        )
    for rid, rel in (mem.get("social_relationship") or {}).items():
        who = str(rel.get("Supporting Characters") or "").strip()
        kind = str(rel.get("Relationship") or "").strip()
        text = f"{protagonist}'s {kind} is {who}. {rel.get('Description') or ''}".strip()
        counts["social_relationship"] += 1
        jobs.append(
            _sfm(
                [{"role": "user", "content": text[:4000]}],
                [f"ref:{rid}", "perltqa:social_relationship"],
            )
        )
    for eid, ev in (mem.get("events") or {}).items():
        content = str(ev.get("content") or "").strip()
        if not content:
            continue
        counts["events"] += 1
        tags = [f"ref:{eid}", "perltqa:event"]
        theme = str(ev.get("Theme") or "").strip()
        if theme:
            tags.append(f"theme:{slug(theme)[:40]}")
        jobs.append(
            _sfm(
                [{"role": "user", "content": content[:8000]}],
                tags,
                observation_date=parse_event_date(str(ev.get("Creation Time") or "")),
                session_id=f"event-{eid}",
            )
        )
    if record_conversations:
        await _post(
            client,
            f"{base}/v1/conversation.set_logging",
            {"org": org, "namespace": ns, "enabled": True},
            token,
        )
    for did, dl in (mem.get("dialogues") or {}).items():
        msgs = dialogue_messages(dl)
        if not msgs:
            continue
        counts["dialogues"] += 1
        obs = ""
        for m in msgs:
            if m.get("timestamp"):
                obs = m["timestamp"][:10]
                break
        jobs.append(
            _sfm(
                msgs[:400],
                [f"ref:{did}", "perltqa:dialogue"],
                observation_date=obs,
                session_id=f"dialogue-{slug(did)}",
            )
        )
        if record_conversations:
            for i, m in enumerate(msgs):
                counts["conversation_turns"] += 1

                async def _append(i: int = i, m: dict[str, str] = m, did: str = did) -> None:
                    async with sem:
                        await _post(
                            client,
                            f"{base}/v1/conversation.append",
                            {
                                "org": org,
                                "namespace": ns,
                                "agent": agent,
                                "thread_id": did,
                                "msg_id": f"{slug(did)}-{i}",
                                "role": "user",
                                "content": (f"{m['name']}: " if m.get("name") else "")
                                + m["content"],
                                "metadata": {
                                    "timestamp": m.get("timestamp", ""),
                                    "speaker": m.get("name", ""),
                                },
                            },
                            token,
                        )

                jobs.append(_append())
    await asyncio.gather(*jobs)
    return counts


def ingest_sessions(mem: dict[str, Any]) -> list[str]:
    """The session ids the ingest writes under: "-" for profile and social
    rows, one per event and one per dialogue. `memory.list` is identity-scoped
    (agent, user, session), so the reference map reads each in turn."""
    out = ["-", "profile"]
    out.extend(f"event-{eid}" for eid in (mem.get("events") or {}))
    out.extend(f"dialogue-{slug(did)}" for did in (mem.get("dialogues") or {}))
    return out


async def key_refs(
    client: httpx.AsyncClient,
    *,
    base: str,
    token: str,
    org: str,
    ns: str,
    mem: dict[str, Any],
    sem: asyncio.Semaphore,
) -> dict[str, set[str]]:
    """memory_key → ref ids from the `ref:` tags the ingest wrote, read
    session by session (memory.list scopes by identity)."""
    out: dict[str, set[str]] = {}

    async def _one(session_id: str) -> None:
        body = {
            "org": org,
            "namespace": ns,
            "agent": "perltqa-bench",
            "user_id": "-",
            "session_id": session_id,
            "limit": 1000,
        }
        async with sem:
            res = await _post(client, f"{base}/v1/memory.list", body, token)
        for e in res.get("entries") or []:
            refs = refs_of_tags(list(e.get("tags") or []))
            if refs:
                out.setdefault(str(e.get("memory_key") or ""), set()).update(refs)

    await asyncio.gather(*[_one(sid) for sid in ingest_sessions(mem)])
    return out


# --- ask + score ---------------------------------------------------------------------


async def ask_records(
    client: httpx.AsyncClient,
    *,
    base: str,
    token: str,
    org: str,
    ns: str,
    name: str,
    records: list[tuple[str, dict[str, Any]]],
    krefs: dict[str, set[str]],
    kin: dict[str, set[str]],
    top_k: int,
    ask_json: dict[str, Any],
    search_json: dict[str, Any],
    judge_model: str,
    api_key: str,
    sem: asyncio.Semaphore,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []

    async def _one(section: str, rec: dict[str, Any]) -> None:
        q = str(rec.get("Question") or "").strip()
        gold = str(rec.get("Answer") or "").strip()
        refs = set(reference_ids(rec, section))
        refs_kin = set(refs)
        for r in list(refs):
            refs_kin |= kin.get(r, set())
        anchors = anchor_phrases(rec)
        row: dict[str, Any] = {
            "character": name,
            "namespace": ns,
            "section": section,
            "question": q,
            "gold": gold,
            "refs": sorted(refs),
            "anchors": anchors,
        }
        try:
            async with sem:
                t0 = time.perf_counter()
                srch = await _post(
                    client,
                    f"{base}/v1/memory.search",
                    {
                        "org": org,
                        "namespace": ns,
                        "agent": "*",
                        "query": q,
                        "top_k": top_k,
                        "namespace_scope": "this",
                        **search_json,
                    },
                    token,
                )
                t1 = time.perf_counter()
                ask = await _post(
                    client,
                    f"{base}/v1/memory.ask",
                    {
                        "org": org,
                        "namespace": ns,
                        "agent": "*",
                        "query": q,
                        "top_k": top_k,
                        "namespace_scope": "this",
                        **ask_json,
                    },
                    token,
                )
                t2 = time.perf_counter()
        except Exception as e:  # noqa: BLE001 — one question must not sink the run
            row["error"] = f"{type(e).__name__}: {e}"[:300]
            rows.append(row)
            return
        hit_keys = [h.get("memory_key", "") for h in srch.get("results") or []]
        cited = list(ask.get("memory_keys") or [])
        answer = str(ask.get("answer") or "")
        row.update(
            {
                "answer": answer,
                "mode": ask.get("mode"),
                "abstain": bool(ask.get("abstain")),
                "search_ms": round((t1 - t0) * 1000),
                "ask_ms": round((t2 - t1) * 1000),
                "anchor_map": anchor_map(answer, anchors),
                "ref_recall": float(any(krefs.get(k, set()) & refs for k in hit_keys[:top_k]))
                if refs
                else None,
                "ref_recall_kin": float(
                    any(krefs.get(k, set()) & refs_kin for k in hit_keys[:top_k])
                )
                if refs_kin
                else None,
                "cited_ref": float(any(krefs.get(k, set()) & refs for k in cited))
                if refs
                else None,
                "hits": len(hit_keys),
                "cited": len(cited),
                # which reference memories the top hits and the citations came
                # from, for reading a miss (a dialogue question answered from
                # its event, a profile question from the narrative)
                "hit_refs": sorted({r for k in hit_keys[:top_k] for r in krefs.get(k, set())}),
                "cited_refs": sorted({r for k in cited for r in krefs.get(k, set())}),
            }
        )
        if judge_model and api_key:
            # An empty answer is wrong, not unjudged: leaving it out would raise
            # correctness exactly when the reader fails.
            row["correct"] = (
                await _judge(
                    client, model=judge_model, api_key=api_key, question=q, gold=gold, answer=answer
                )
                if answer
                else "no"
            )
        rows.append(row)

    await asyncio.gather(*[_one(sec, rec) for sec, rec in records])
    return rows


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    def _mean(xs: list[float]) -> float | None:
        return round(statistics.mean(xs), 4) if xs else None

    def _block(rs: list[dict[str, Any]]) -> dict[str, Any]:
        ok = [r for r in rs if "error" not in r]
        judged = [r for r in ok if r.get("correct") in ("yes", "no")]
        return {
            "n": len(rs),
            "errors": len(rs) - len(ok),
            "anchor_map": _mean([r["anchor_map"] for r in ok if r.get("anchor_map") is not None]),
            "ref_recall_at_k": _mean(
                [r["ref_recall"] for r in ok if r.get("ref_recall") is not None]
            ),
            "ref_recall_kin_at_k": _mean(
                [r["ref_recall_kin"] for r in ok if r.get("ref_recall_kin") is not None]
            ),
            "cited_ref": _mean([r["cited_ref"] for r in ok if r.get("cited_ref") is not None]),
            "correctness": _mean([1.0 if r["correct"] == "yes" else 0.0 for r in judged])
            if judged
            else None,
            "judged": len(judged),
            "abstain_rate": _mean([1.0 if r.get("abstain") else 0.0 for r in ok]),
            "ask_ms_p50": statistics.median([r["ask_ms"] for r in ok]) if ok else None,
            "search_ms_p50": statistics.median([r["search_ms"] for r in ok]) if ok else None,
        }

    out: dict[str, Any] = {"overall": _block(rows)}
    for sec in SECTIONS:
        sub = [r for r in rows if r.get("section") == sec]
        if sub:
            out[sec] = _block(sub)
    out["retries"] = dict(RETRIES)
    return out


# --- main ------------------------------------------------------------------------------


async def main_async(args: argparse.Namespace) -> int:
    mem = json.load(open(args.mem, encoding="utf-8"))
    qa = qa_records(json.load(open(args.qa, encoding="utf-8")))
    names = [n for n in qa if n in mem]
    if args.characters:
        wanted = [c.strip() for c in args.characters.split(",") if c.strip()]
        names = [n for n in names if n in wanted]
    names = names[: args.max_characters] if args.max_characters else names
    sections = [s for s in args.sections.split(",") if s] if args.sections else list(SECTIONS)
    namespace_json = json.loads(args.namespace_json) if args.namespace_json else {}
    ask_json = json.loads(args.ask_json) if args.ask_json else {}
    search_json = json.loads(args.search_json) if args.search_json else {}
    token = args.token or os.environ.get("LOOM_API_KEY", "")
    if not token:
        print("set --token or LOOM_API_KEY", file=sys.stderr)
        return 2
    api_key = os.environ.get("OPENAI_API_KEY", "") if args.judge_model else ""
    if args.skip_ingest and not args.namespace_salt:
        print("--skip-ingest needs the --namespace-salt its ingest run printed", file=sys.stderr)
        return 2
    # A fresh salt per ingest run: re-ingesting into the previous run's namespaces
    # would extract every character's history a second time on top of the first.
    run_salt = args.namespace_salt or secrets.token_hex(4)
    if not args.namespace_salt:
        print(f"namespace salt {run_salt} (pass --namespace-salt to reuse)", file=sys.stderr)
    base = args.base_url.rstrip("/")
    sem = asyncio.Semaphore(max(1, args.concurrency))
    all_rows: list[dict[str, Any]] = []
    ingest_counts: dict[str, dict[str, int]] = {}
    build_ms: dict[str, int] = {}
    async with httpx.AsyncClient() as client:
        for name in names:
            salt = hashlib.sha1(f"{name}|{run_salt}".encode()).hexdigest()[:8]
            ns = f"bench-perltqa-{slug(name)[:24]}-{salt}"
            if not args.skip_ingest:
                t0 = time.perf_counter()
                ingest_counts[name] = await ingest_character(
                    client,
                    base=base,
                    token=token,
                    org=args.org,
                    ns=ns,
                    name=name,
                    mem=mem[name],
                    namespace_json=namespace_json,
                    record_conversations=args.record_conversations,
                    sem=sem,
                    defer=args.defer,
                )
                build_ms[name] = round((time.perf_counter() - t0) * 1000)
                print(
                    f"[{name}] ingested {ingest_counts[name]} into {ns} in {build_ms[name]} ms",
                    file=sys.stderr,
                )
                if args.settle_seconds:
                    await asyncio.sleep(args.settle_seconds)
            krefs = await key_refs(
                client, base=base, token=token, org=args.org, ns=ns, mem=mem[name], sem=sem
            )
            records: list[tuple[str, dict[str, Any]]] = []
            for sec in sections:
                recs = qa[name].get(sec) or []
                if args.max_questions_per_section:
                    recs = recs[: args.max_questions_per_section]
                records.extend((sec, r) for r in recs)
            rows = await ask_records(
                client,
                base=base,
                token=token,
                org=args.org,
                ns=ns,
                name=name,
                records=records,
                krefs=krefs,
                kin=kin_refs(mem[name]),
                top_k=args.top_k,
                ask_json=ask_json,
                search_json=search_json,
                judge_model=args.judge_model,
                api_key=api_key,
                sem=sem,
            )
            all_rows.extend(rows)
            blk = summarize(rows)["overall"]
            print(
                f"[{name}] {len(rows)} q · anchor_map={blk['anchor_map']} ref_recall@{args.top_k}="
                f"{blk['ref_recall_at_k']} cited_ref={blk['cited_ref']} "
                f"correct={blk['correctness']} "
                f"keys_with_refs={len(krefs)}",
                file=sys.stderr,
            )
    summary = {
        "dataset": "PerLTQA en_v2",
        "characters": names,
        "sections": sections,
        "top_k": args.top_k,
        "namespace_json": namespace_json,
        "ask_json": ask_json,
        "search_json": search_json,
        "judge_model": args.judge_model or None,
        "record_conversations": args.record_conversations,
        "defer": args.defer,
        "ingest_counts": ingest_counts,
        "build_ms": build_ms,
        **summarize(all_rows),
    }
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            for r in all_rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
            f.write(json.dumps({"summary": summary}, ensure_ascii=False) + "\n")
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--mem", required=True, help="perltmem_en_v2.json")
    ap.add_argument("--qa", required=True, help="perltqa_en_v2.json")
    ap.add_argument(
        "--characters", default="", help="comma list of protagonist names; default all shared"
    )
    ap.add_argument("--max-characters", type=int, default=0)
    ap.add_argument("--max-questions-per-section", type=int, default=0)
    ap.add_argument("--sections", default="", help=f"comma list of {','.join(SECTIONS)}")
    ap.add_argument("--base-url", default=os.environ.get("LOOM_BASE_URL", "http://localhost:7777"))
    ap.add_argument("--token", default="")
    ap.add_argument("--org", default="dev")
    ap.add_argument(
        "--namespace-json",
        default="",
        help="namespace.register fields for each character namespace",
    )
    ap.add_argument(
        "--search-json",
        default="",
        help="extra memory.search fields for the reference-recall read, "
        "e.g. '{\"digest_descent\": true}'",
    )
    ap.add_argument(
        "--ask-json",
        default="",
        help='extra memory.ask fields, e.g. \'{"world_knowledge":"forbid"}\'',
    )
    ap.add_argument(
        "--record-conversations",
        action="store_true",
        help="turn recording on and append every dialogue utterance to the conversations plane",
    )
    ap.add_argument(
        "--defer",
        action="store_true",
        help="set_from_messages(defer=true); pair with --settle-seconds",
    )
    ap.add_argument("--settle-seconds", type=float, default=0.0)
    ap.add_argument(
        "--skip-ingest",
        action="store_true",
        help="reuse namespaces from a previous run (pass its --namespace-salt)",
    )
    ap.add_argument(
        "--namespace-salt", default="", help="default: a fresh one, printed at the start"
    )
    ap.add_argument("--top-k", type=int, default=5)
    ap.add_argument(
        "--judge-model", default="", help="OpenAI model for correctness; needs OPENAI_API_KEY"
    )
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
