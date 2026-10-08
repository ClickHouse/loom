"""Time-Dialog (a 200-question temporal-reasoning test set) through loom.

Time-Dialog evaluates temporal reasoning over multi-session dialogues: 200
held-out questions from the TIME benchmark's dialogue portion, each with its
own 7–32-session history (16k–142k characters), gold evidence sessions, and a
gold question time window. Eleven subtasks in three levels.

The data lives in the Memory-T1 repository (MIT):
  data/time_dial_context_list_with_evidence_200_v2.json   (questions + contexts)
  data/question_time_range_annotated_0717.json             (gold windows, 4,716)

Protocol here (verbatim, LLM-free write by default):
  1. each session is split into windows of --turns-per-memory turns and written
     with memory.set, valid_at = the session's timestamp, session_id =
     "session_N" (the id the gold evidence names), one namespace per question;
     --write-mode extract uses memory.set_from_messages per session instead;
  2. memory.search(question, top_k, observation_date = last session's date,
     + --search-json) → hits; evidence-session recall@k from the hits'
     session_id against the gold evidence; the server's question_window is
     compared with the gold window (coverage);
  3. a fixed reader answers from the hits under the published evaluation prompt
     and the answer is scored with the published metrics
     (eval/calculate_scores.py, ported): option EM/F1, Localization within
     one day, Computation within one day of total duration, Timeline
     positional F1/EM.

    python bench/time_dialog.py --data <...200_v2.json> --windows <...0717.json> \
        --base-url http://localhost:7777 --token $LOOM_API_KEY --org dev \
        --reader-model gpt-4.1-mini --out td.jsonl
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

# ---------------------------------------------------------------------------
# Context parsing
# ---------------------------------------------------------------------------

_SESSION_HDR = re.compile(r"^Session (\d+) happened at (.+?)\.\s*$", re.M)
_TURN = re.compile(r"^([^:\n]{1,60}): (.*)$")


def parse_session_time(raw: str) -> datetime | None:
    """'3:18 pm on 8 February, 2020' → datetime."""
    s = raw.strip()
    for fmt in ("%I:%M %p on %d %B, %Y", "%I:%M:%S %p on %d %B, %Y", "%H:%M on %d %B, %Y"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def parse_context(context: str) -> list[dict[str, Any]]:
    """[{n, time, turns: [(speaker, text)]}] in order."""
    heads = list(_SESSION_HDR.finditer(context))
    out: list[dict[str, Any]] = []
    for i, h in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(context)
        body = context[h.end() : end]
        turns: list[tuple[str, str]] = []
        for line in body.splitlines():
            m = _TURN.match(line.strip())
            if m:
                turns.append((m.group(1).strip(), m.group(2).strip()))
            elif turns and line.strip():
                turns[-1] = (turns[-1][0], turns[-1][1] + " " + line.strip())
        out.append({"n": int(h.group(1)), "time": parse_session_time(h.group(2)), "turns": turns})
    return out


def memory_windows(turns: list[tuple[str, str]], per: int) -> list[str]:
    return [
        "\n".join(f"{sp}: {tx}" for sp, tx in turns[i : i + per]) for i in range(0, len(turns), per)
    ]


# ---------------------------------------------------------------------------
# Scoring (ported from Memory-T1 eval/calculate_scores.py, MIT)
# ---------------------------------------------------------------------------

_UNIT_DAYS = {
    "year": 365.0,
    "years": 365.0,
    "month": 30.0,
    "months": 30.0,
    "day": 1.0,
    "days": 1.0,
    "hour": 1 / 24,
    "hours": 1 / 24,
    "minute": 1 / 1440,
    "minutes": 1 / 1440,
    "second": 1 / 86400,
    "seconds": 1 / 86400,
}


def duration_days(s: str) -> float:
    total = 0.0
    for value, unit in re.findall(
        r"(\d+)\s+(years?|months?|days?|hours?|minutes?|seconds?)", s.lower()
    ):
        total += int(value) * _UNIT_DAYS[unit]
    return total


def extract_options(ans: str) -> set[str]:
    ans = ans.strip()
    if "(" in ans and ")" in ans:
        return set(ans.replace(")(", ") (").split())
    return {t for t in re.split(r"[\s,]+", ans) if t}


def set_f1(pred: set[str], gold: set[str]) -> dict[str, float]:
    tp = len(pred & gold)
    p = tp / len(pred) if pred else 0.0
    r = tp / len(gold) if gold else 0.0
    f1 = 2 * p * r / (p + r) if p + r else 0.0
    return {"f1": f1, "em": float(pred == gold)}


def split_items(s: str) -> list[str]:
    s = s.strip()
    if s.startswith("(") and s.endswith(")"):
        s = s[1:-1]
    return [x.strip() for x in re.split(r"\)\s*\(", s) if x.strip()]


def timeline_score(pred: str, gold: str) -> dict[str, float]:
    p, g = split_items(pred), split_items(gold)
    tp = sum(1 for a, b in zip(p, g, strict=False) if a == b)
    prec = tp / len(p) if p else 0.0
    rec = tp / len(g) if g else 0.0
    f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
    return {"f1": f1, "em": float(p == g)}


_DT_FORMATS = (
    "%I:%M:%S %p, %B %d, %Y",
    "%I:%M %p, %B %d, %Y",
    "%B %d, %Y",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d",
    "%b %d, %Y",
    "%d %B %Y",
    "%m/%d/%Y",
    "%I:%M %p on %d %B, %Y",
)


def parse_any_datetime(s: str) -> datetime | None:
    s = s.strip().rstrip(".")
    for fmt in _DT_FORMATS:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        try:
            return datetime(int(m[1]), int(m[2]), int(m[3]))
        except ValueError:
            return None
    return None


def localization_score(pred: str, gold: str) -> dict[str, float]:
    p, g = parse_any_datetime(pred), parse_any_datetime(gold)
    if p is None or g is None:
        return {"f1": 0.0, "em": 0.0}
    ok = abs(p - g) <= timedelta(days=1)
    return {"f1": float(ok), "em": float(ok)}


def computation_score(pred: str, gold: str) -> dict[str, float]:
    p, g = duration_days(pred), duration_days(gold)
    ok = bool(re.search(r"\d", pred)) and abs(p - g) <= 1.0
    return {"f1": float(ok), "em": float(ok)}


def score_answer(task: str, pred: str, gold: str) -> dict[str, float]:
    if task == "Localization":
        return localization_score(pred, gold)
    if task == "Computation":
        return computation_score(pred, gold)
    if task == "Timeline":
        return timeline_score(pred, gold)
    return set_f1(extract_options(pred.upper()), extract_options(gold.upper()))


def final_answer(text: str) -> str:
    """The answer contract: the last 'Answer: ...' line; else the last line."""
    lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
    for ln in reversed(lines):
        m = re.match(r"^\**answer\**\s*:\s*(.+)$", ln, re.IGNORECASE)
        if m:
            return m.group(1).strip().strip("*").strip()
    return lines[-1] if lines else ""


_OPTION_LINE = re.compile(r"\n\s*\*?[A-H][.)]\s")


def retrieval_query(question: str, task: str = "") -> str:
    """The question stem: everything before the first option line. Measured
    on the isolated server: the full multiple-choice text (350+ characters of
    options) pushes every candidate under memory.search's noise floor and
    the search abstains with no_match; the stem alone retrieves. The reader
    still sees the whole question."""
    if task == "Timeline":  # the facts to order ARE the retrieval targets
        return question.strip()[:2000]
    m = _OPTION_LINE.search("\n" + question)
    stem = question[: m.start() - 1] if m and m.start() > 1 else question
    return stem.strip()[:2000] or question[:2000]


def evidence_sessions(row: dict[str, Any]) -> set[str]:
    return {f"session_{e['session_id']}" for e in row.get("Evidence") or [] if e.get("session_id")}


_KEY_SESSION = re.compile(r"^s(\d+)_c\d+$")


def hit_session(h: dict[str, Any]) -> str:
    """The session a hit came from: the row's session_id, else
    the session encoded in this bench's own key (`s<N>_c<i>`)."""
    sid = str(h.get("session_id") or "")
    if sid:
        return sid
    m = _KEY_SESSION.match(str(h.get("memory_key") or ""))
    return f"session_{m.group(1)}" if m else ""


def recall_at(hit_sessions: list[str], gold: set[str], k: int) -> float | None:
    if not gold:
        return None
    return len(set(hit_sessions[:k]) & gold) / len(gold)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


async def _post(
    client: httpx.AsyncClient, url: str, body: dict[str, Any], token: str
) -> dict[str, Any]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    last: httpx.Response | None = None
    for attempt in range(8):
        try:
            r = await client.post(url, json=body, headers=headers, timeout=300.0)
        except httpx.TransportError:
            if attempt >= 2:
                raise
            await asyncio.sleep(1.0 * (2**attempt))
            continue
        if r.status_code == 429 or r.status_code >= 500:
            last = r
            wait = r.headers.get("retry-after") or r.headers.get("x-ratelimit-reset") or "1"
            try:
                delay = min(60.0, max(0.5, float(wait)))
            except ValueError:
                delay = min(60.0, 1.0 * (2**attempt))
            if r.status_code >= 500:
                delay = min(60.0, max(delay, 2.0 * (2**attempt)))  # a table restart, not a burst
            await asyncio.sleep(delay)
            continue
        r.raise_for_status()
        return r.json()
    if last is not None:
        last.raise_for_status()
    raise RuntimeError("unreachable")


ASK_FORMAT_RULE = (
    "Answer with the correct format only, on the last line as 'Answer: ...': single choice "
    "one uppercase letter; multiple choice space-separated letters; a time as "
    "HH:MM:SS am/pm, Month DD, YYYY; a sequence as (1)(3)(2); a duration in natural units."
)

READER_PROMPT = (
    "You are presented with a temporal question and a previous memory, please answer the "
    "question with the correct format. The last line of your response should be of the form: "
    "Answer: $Answer, where $Answer is the answer to the problem.\n"
    "Output requirements:\n"
    "1. Single choice: A || B || ... (uppercase)\n"
    "2. Multiple choice: A B C (space-separated uppercase)\n"
    "3. Time: HH:MM:SS am/pm, Month DD, YYYY\n   Example: 10:45:41 pm, January 15, 2024\n"
    "4. Sequence: (1)(3)(2)(4)(5)(6)(7)(8)\n"
    "5. Duration: natural time expressions, e.g. 2 months 4 days\n"
    "<previous_memory>\n{memory}\n</previous_memory>\n"
    "<question>\nTime: {current_time}\nQuestion: {question}\n</question>\n"
    "Remember to put your answer on its own line after 'Answer:'"
)


async def _read(
    client: httpx.AsyncClient, prompt: str, model: str, api_key: str
) -> tuple[str, int, int]:
    r = await client.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
            "max_tokens": 600,
        },
        timeout=120.0,
    )
    r.raise_for_status()
    payload = r.json()
    usage = payload.get("usage") or {}
    return (
        (payload["choices"][0]["message"]["content"] or "").strip(),
        int(usage.get("prompt_tokens") or 0),
        int(usage.get("completion_tokens") or 0),
    )


CONTEXT_PROMPT = (
    "<document>\n{session}\n</document>\n"
    "Here is the chunk we want to situate within the whole document\n"
    "<chunk>\n{chunk}\n</chunk>\n"
    "Please give a short succinct context to situate this chunk within the overall document "
    "for the purposes of improving search retrieval of the chunk. Name who is speaking, when "
    "(the session date), and what the exchange is about, in one or two sentences. Answer only "
    "with the succinct context and nothing else."
)


async def contextualize(
    client: httpx.AsyncClient,
    *,
    session_text: str,
    chunk: str,
    model: str,
    api_key: str,
    cache: dict[str, str],
    key: str,
) -> str:
    """Contextual retrieval: prepend 50–100 tokens of
    chunk-specific context, generated from the whole document, before
    embedding. Cached by key so arms and re-runs do not pay twice."""
    if key in cache:
        return cache[key]
    prompt = CONTEXT_PROMPT.format(session=session_text[:12000], chunk=chunk)
    text, _, _ = await _read(client, prompt, model, api_key)
    ctx = " ".join(text.split())[:600]
    cache[key] = ctx
    return ctx


def render_hits(hits: list[dict[str, Any]]) -> str:
    lines = []
    for h in hits:
        sess = hit_session(h)
        date = str(h.get("temporal_anchor") or h.get("valid_at") or "")[:16]
        lines.append(f"[{sess} at {date}] {h.get('content_excerpt') or h.get('value') or ''}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# One question
# ---------------------------------------------------------------------------


async def run_one(
    client: httpx.AsyncClient,
    row: dict[str, Any],
    *,
    base_url: str,
    token: str,
    org: str,
    write_mode: str,
    skip_write: bool,
    namespace_salt: str,
    turns_per_memory: int,
    top_k: int,
    search_json: dict[str, Any],
    reader_model: str,
    api_key: str,
    gold_windows: dict[str, Any],
    write_concurrency: int,
    memory_type: str = "semantic",
    contextualize_model: str = "",
    context_cache: dict[str, str] | None = None,
    answer: str = "reader",
    ask_json: dict[str, Any] | None = None,
) -> dict[str, Any]:
    qid = row["Question ID"]
    ns = f"td-{hashlib.sha1(f'{namespace_salt}|{qid}'.encode()).hexdigest()[:10]}"
    identity = {"org": org, "namespace": ns, "agent": "time-dialog-bench", "user_id": "-"}
    sessions = parse_context(row["Context"])
    last_time = max((s["time"] for s in sessions if s["time"]), default=None)
    obs_iso = last_time.date().isoformat() if last_time else ""

    t0 = time.perf_counter()
    writes = 0
    if not skip_write:
        sem = asyncio.Semaphore(write_concurrency)

        async def _set(s: dict[str, Any], i: int, text: str) -> None:
            nonlocal writes
            async with sem:
                await _post(
                    client,
                    base_url + "/v1/memory.set",
                    {
                        **identity,
                        "session_id": f"session_{s['n']}",
                        "memory_key": f"s{s['n']}_c{i}",
                        # `episodic` is NON-SEARCHABLE by design
                        # (server/memory/domain/memory_types.py): raw turns stay out of
                        # unfiltered recall. A verbatim store that must be found by
                        # memory.search is written as `semantic`; episodic rows would measure
                        # only the anchor-pool fallback.
                        "memory_type": memory_type,
                        "value": (
                            f"[Session {s['n']}, "
                            f"{s['time'].isoformat() if s['time'] else 'undated'}]\n{text}"
                        ),
                        "valid_at": s["time"].isoformat() if s["time"] else None,
                        "tags": ["time-dialog"],
                    },
                    token,
                )
                writes += 1

        async def _extract(s: dict[str, Any]) -> None:
            nonlocal writes
            async with sem:
                await _post(
                    client,
                    base_url + "/v1/memory.set_from_messages",
                    {
                        **identity,
                        "session_id": f"session_{s['n']}",
                        "observation_date": s["time"].date().isoformat() if s["time"] else "",
                        "messages": [
                            {
                                "role": "user",
                                "content": f"{sp}: {tx}",
                                "name": sp[:80],
                                "timestamp": s["time"].isoformat() if s["time"] else None,
                            }
                            for sp, tx in s["turns"]
                        ][:400],
                    },
                    token,
                )
                writes += 1

        async def _bulk(s: dict[str, Any]) -> None:
            # One memory.bulk_set per session: the same rows memory.set would write,
            # in one request, so the per-namespace write limiter sees 19 calls per
            # question instead of 100 (per-row memory.set calls hit 429s even on an
            # isolated server). temporal_anchor is the session time,
            # which the window boost and the timeline read first.
            nonlocal writes
            iso = s["time"].isoformat() if s["time"] else None
            windows = memory_windows(s["turns"], turns_per_memory)
            prefixes = [""] * len(windows)
            if contextualize_model and api_key:
                session_text = f"Session {s['n']} at {iso}\n" + "\n".join(
                    f"{sp}: {tx}" for sp, tx in s["turns"]
                )
                prefixes = list(
                    await asyncio.gather(
                        *[
                            contextualize(
                                client,
                                session_text=session_text,
                                chunk=w,
                                model=contextualize_model,
                                api_key=api_key,
                                cache=context_cache if context_cache is not None else {},
                                key=f"{qid}|s{s['n']}_c{i}",
                            )
                            for i, w in enumerate(windows)
                        ]
                    )
                )
            entries = [
                {
                    "memory_key": f"s{s['n']}_c{i}",
                    "memory_type": memory_type,
                    "value": (
                        f"[Session {s['n']}, {iso or 'undated'}]\n"
                        + (f"{prefixes[i]}\n" if prefixes[i] else "")
                        + text
                    ),
                    "temporal_anchor": iso,
                    "tags": ["time-dialog"] + (["contextualized"] if prefixes[i] else []),
                }
                for i, text in enumerate(windows)
            ]
            if not entries:
                return
            async with sem:
                await _post(
                    client,
                    base_url + "/v1/memory.bulk_set",
                    {
                        **identity,
                        "session_id": f"session_{s['n']}",
                        "entries": entries,
                    },
                    token,
                )
                writes += len(entries)

        if write_mode == "extract":
            await asyncio.gather(*[_extract(s) for s in sessions if s["turns"]])
        elif write_mode == "bulk":
            await asyncio.gather(*[_bulk(s) for s in sessions])
        else:
            await asyncio.gather(
                *[
                    _set(s, i, text)
                    for s in sessions
                    for i, text in enumerate(memory_windows(s["turns"], turns_per_memory))
                ]
            )
    write_ms = int((time.perf_counter() - t0) * 1000)

    t1 = time.perf_counter()
    body = {
        **identity,
        "query": retrieval_query(row["Question"], row["Task"]),
        "top_k": top_k,
        **search_json,
    }
    if obs_iso:
        body["observation_date"] = obs_iso
    ask_answer = ""
    if answer == "ask":
        # memory.ask: the whole contract (question + options + format rule)
        # goes in the query; the ask path splits the answer scaffold off for
        # retrieval and hands the model the full text.
        ask_body = {
            **identity,
            "query": (row["Question"] + "\n" + ASK_FORMAT_RULE)[:2000],
            "top_k": top_k,
            **(ask_json or {}),
        }
        if obs_iso:
            ask_body["observation_date"] = obs_iso
        resp = await _post(client, base_url + "/v1/memory.ask", ask_body, token)
        ask_answer = str(resp.get("answer") or "")
    else:
        resp = await _post(client, base_url + "/v1/memory.search", body, token)
    search_ms = int((time.perf_counter() - t1) * 1000)
    hits = resp.get("results") or []
    if not hits and not skip_write and answer != "ask":
        # Fresh writes: the embedding row lands a moment after memory.set
        # returns. Poll briefly rather than score an empty pool.
        for _ in range(10):
            await asyncio.sleep(2.0)
            resp = await _post(client, base_url + "/v1/memory.search", body, token)
            hits = resp.get("results") or []
            if hits:
                break
    hit_sessions = [hit_session(h) for h in hits]
    gold = evidence_sessions(row)
    qwin = str((resp.get("retrieval_work") or {}).get("question_window") or "")

    # Gold window coverage: does the server's window overlap / contain the gold one?
    gw = (gold_windows.get(qid) or {}).get("question_time_range") or {}
    gold_bounded = all(
        gw.get(k) and not str(gw.get(k)).lower().startswith("unknown") for k in ("start", "end")
    )
    window_rel = ""
    if qwin and ":" in qwin:
        kind, span = qwin.split(":", 1)
        s_, e_ = span.split("..", 1)
        if s_ != "*" and e_ != "*" and gold_bounded:
            gs, ge = str(gw["start"])[:10], str(gw["end"])[:10]
            window_rel = (
                "exact"
                if (s_, e_) == (gs, ge)
                else ("overlap" if not (e_ < gs or s_ > ge) else "disjoint")
            )
        elif s_ != "*" and e_ != "*":
            window_rel = "ours_only"
        else:
            window_rel = "open" if gold_bounded else "both_open"
    elif gold_bounded:
        window_rel = "gold_only"

    pred, in_tok, out_tok = "", 0, 0
    if answer == "ask":
        pred = final_answer(ask_answer) if ask_answer else ""
    elif reader_model and api_key:
        prompt = READER_PROMPT.format(
            memory=render_hits(hits),
            current_time=last_time.isoformat() if last_time else "unknown",
            question=row["Question"],
        )
        raw, in_tok, out_tok = await _read(client, prompt, reader_model, api_key)
        pred = final_answer(raw)
    sc = score_answer(row["Task"], pred, row["Gold Answer"]) if pred else {"f1": 0.0, "em": 0.0}
    return {
        "qid": qid,
        "task": row["Task"],
        "level": row["Level"],
        "namespace": ns,
        "sessions": len(sessions),
        "writes": writes,
        "write_ms": write_ms,
        "search_ms": search_ms,
        "hits": len(hits),
        "gold_sessions": sorted(gold),
        "recall@5": recall_at(hit_sessions, gold, 5),
        "recall@10": recall_at(hit_sessions, gold, 10),
        "question_window": qwin,
        "window_vs_gold": window_rel,
        "prediction": pred[:300],
        "gold": row["Gold Answer"][:300],
        **sc,
        "answer_path": answer,
        "reader_in_tokens": in_tok,
        "reader_out_tokens": out_tok,
    }


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    ok = [r for r in rows if "error" not in r]
    out: dict[str, Any] = {"n": len(ok), "errors": len(rows) - len(ok)}
    if not ok:
        return out

    def mean(xs: list[float]) -> float:
        return round(sum(xs) / len(xs), 4) if xs else 0.0

    out["f1"] = mean([r["f1"] for r in ok])
    out["em"] = mean([r["em"] for r in ok])
    rec5 = [r["recall@5"] for r in ok if r["recall@5"] is not None]
    rec10 = [r["recall@10"] for r in ok if r["recall@10"] is not None]
    out["evidence_recall@5"] = mean(rec5)
    out["evidence_recall@10"] = mean(rec10)
    out["by_task"] = {
        t: {
            "n": len(g),
            "f1": mean([r["f1"] for r in g]),
            "recall@10": mean([r["recall@10"] for r in g if r["recall@10"] is not None]),
        }
        for t in sorted({r["task"] for r in ok})
        for g in [[r for r in ok if r["task"] == t]]
    }
    out["by_level"] = {
        lv: {"n": len(g), "f1": mean([r["f1"] for r in g])}
        for lv in sorted({r["level"] for r in ok})
        for g in [[r for r in ok if r["level"] == lv]]
    }
    wv: dict[str, int] = {}
    for r in ok:
        wv[r["window_vs_gold"] or "none"] = wv.get(r["window_vs_gold"] or "none", 0) + 1
    out["window_vs_gold"] = wv
    out["search_ms_p50"] = sorted(r["search_ms"] for r in ok)[len(ok) // 2]
    out["write_ms_p50"] = sorted(r["write_ms"] for r in ok)[len(ok) // 2]
    out["reader_in_tokens_mean"] = mean([r["reader_in_tokens"] for r in ok])
    return out


async def main_async(args: argparse.Namespace) -> int:
    rows_in = json.loads(Path(args.data).read_text())
    if args.tasks:
        keep = {t.strip() for t in args.tasks.split(",") if t.strip()}
        rows_in = [r for r in rows_in if r["Task"] in keep]
    if args.qids:
        keep_ids = {q.strip() for q in Path(args.qids).read_text().split() if q.strip()}
        rows_in = [r for r in rows_in if r["Question ID"] in keep_ids]
    if args.limit:
        rows_in = rows_in[: args.limit]
    gold_windows = json.loads(Path(args.windows).read_text()) if args.windows else {}
    search_json = json.loads(args.search_json) if args.search_json else {}
    ask_json = json.loads(args.ask_json) if args.ask_json else {}
    api_key = os.environ.get("BENCH_OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY", "")
    context_cache: dict[str, str] = {}
    cache_path = Path(args.context_cache) if args.context_cache else None
    if cache_path and cache_path.exists():
        context_cache = json.loads(cache_path.read_text())
    if args.reader_model and not api_key:
        print(
            "OPENAI_API_KEY is required for the reader (or pass --reader-model '')", file=sys.stderr
        )
        return 2
    results: list[dict[str, Any]] = []
    sem = asyncio.Semaphore(args.concurrency)
    async with httpx.AsyncClient(timeout=300.0) as client:

        async def _one(row: dict[str, Any]) -> None:
            async with sem:
                try:
                    results.append(
                        await run_one(
                            client,
                            row,
                            base_url=args.base_url.rstrip("/"),
                            token=args.token,
                            org=args.org,
                            write_mode=args.write_mode,
                            skip_write=args.skip_write,
                            namespace_salt=args.namespace_salt,
                            turns_per_memory=args.turns_per_memory,
                            top_k=args.top_k,
                            search_json=search_json,
                            reader_model=args.reader_model,
                            api_key=api_key,
                            gold_windows=gold_windows,
                            write_concurrency=args.write_concurrency,
                            answer=args.answer,
                            ask_json=ask_json,
                            memory_type=args.memory_type,
                            contextualize_model=args.contextualize,
                            context_cache=context_cache,
                        )
                    )
                except Exception as e:  # noqa: BLE001 — one question must not sink the run
                    results.append(
                        {
                            "qid": row.get("Question ID"),
                            "task": row.get("Task"),
                            "error": f"{type(e).__name__}: {e}"[:200],
                        }
                    )

        await asyncio.gather(*[_one(r) for r in rows_in])
    summary = {
        "dataset": "Time-Dialog (Memory-T1 test, 200)",
        "write_mode": args.write_mode,
        "skip_write": args.skip_write,
        "top_k": args.top_k,
        "search_json": search_json,
        "reader": args.reader_model or None,
        "turns_per_memory": args.turns_per_memory,
        "answer": args.answer,
        "ask_json": ask_json,
        "memory_type": args.memory_type,
        "contextualize": args.contextualize or None,
        **summarize(results),
    }
    print(json.dumps(summary, indent=2))
    if args.out:
        Path(args.out).write_text("\n".join(json.dumps(r) for r in results))
    if cache_path and context_cache:
        cache_path.write_text(json.dumps(context_cache))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--data", required=True, help="time_dial_context_list_with_evidence_200_v2.json"
    )
    ap.add_argument(
        "--windows", default="", help="question_time_range_annotated_0717.json (gold windows)"
    )
    ap.add_argument("--base-url", default=os.environ.get("LOOM_BASE_URL", "http://localhost:7777"))
    ap.add_argument("--token", default=os.environ.get("LOOM_API_KEY", ""))
    ap.add_argument("--org", default="dev")
    ap.add_argument("--write-mode", choices=["bulk", "set", "extract"], default="bulk")
    ap.add_argument("--turns-per-memory", type=int, default=4)
    ap.add_argument(
        "--memory-type",
        default="semantic",
        help="memory_type for verbatim writes; episodic rows are not searched by default",
    )
    ap.add_argument(
        "--namespace-salt", default="", help="deterministic namespaces so arms share a store"
    )
    ap.add_argument(
        "--skip-write", action="store_true", help="reuse namespaces written with the same salt"
    )
    ap.add_argument("--top-k", type=int, default=10)
    ap.add_argument(
        "--search-json",
        default="",
        help='extra memory.search fields, e.g. {"search_mode":"semantic"}',
    )
    ap.add_argument("--reader-model", default=os.environ.get("BENCH_READER_MODEL", "gpt-4.1-mini"))
    ap.add_argument("--tasks", default="", help="comma list of subtasks to keep")
    ap.add_argument(
        "--answer",
        choices=["reader", "ask"],
        default="reader",
        help="reader = memory.search + fixed reader; ask = memory.ask answers (its hits scored)",
    )
    ap.add_argument("--ask-json", default="", help="extra memory.ask fields for --answer ask")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument(
        "--contextualize",
        default="",
        help="model that writes a 50-100 token context per chunk before embedding (Anthropic's "
        "contextual retrieval); empty = plain chunks",
    )
    ap.add_argument("--context-cache", default="", help="json file caching generated contexts")
    ap.add_argument("--qids", default="", help="file of Question IDs to run (whitespace-separated)")
    ap.add_argument("--concurrency", type=int, default=4, help="questions in flight")
    ap.add_argument(
        "--write-concurrency", type=int, default=4, help="writes in flight per question"
    )
    ap.add_argument("--out", default="", help="per-question rows as jsonl")
    args = ap.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
