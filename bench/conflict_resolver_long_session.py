"""Long-session conflict resolution: does the background conflict resolver leave
each changing attribute with one live value after a dozen sessions?

One user writes twelve sessions a week apart into a fresh `newest_wins`
namespace: six attributes (home city, employer, manager, standup time, CI
provider, diet) change once or twice, each change under a new key and in new
words, among restated, elaborated and neighbouring facts, a pinned fact that is
contradicted later, a fact deleted and brought back with Undo, and eighteen
unrelated ones (`bench/conflict_resolver_scenario.py`, the scenario and its
ground truth; `tests/integration/test_conflict_resolver_long_session_ch.py`
runs it with a scripted judge). The harness then waits for the server's
resolver to judge everything it owes, reads the end state, and asks
`memory.search` one question per attribute.

Run it against a server with the resolver on and one with it off (`--base-url
on=... --base-url off=...`), each on its own ClickHouse (`--ch on=HOST:PORT --ch
off=HOST:PORT`; the others `--ch-*`): the resolver judges every namespace of its
store, so on a shared one it would judge the control's rows too. The harness reads,
from each arm's store, the resolver's queue marks
(`memory_embeddings.conflict_checked_at`) to know when it has drained, the rows' end
state, the `memory.auto_supersede` audit records and the worker's spend in
`agent_events`. Auth off; org `bench`.

Variants (`--variant`):

- `set`: the scenario's own keys and words, odd sessions through `memory.set`,
  even ones through `memory.bulk_set`, with the delete and Undo. Scored by key.
- `messages`: each session as a short dialogue through
  `memory.set_from_messages`, so the extractor picks the keys and words (the
  pinned fact still goes through `memory.set`; no delete). Scored by content:
  a row states an attribute's value when it names it (`VALUE_PATTERNS`), and
  the latest value it names is the one it states.

Reported per server and variant:

- `current_only_live`: share of attributes whose current value is live and
  unflagged and no older value is live;
- `stale_retrieval`: share of attributes whose top search hit states an older value;
- `false_retirements`: rows superseded that state no older attribute value
  (by any path), and `resolver_retired`, the rows the resolver retired;
- `false_flags`: live `conflicted` rows that state no attribute value, except
  the pinned pair, whose flag is right;
- `resolver_calls`, `resolver_cost_usd`, `namespace_cost_usd`, `drain_s`.

The treatment check: on an `on` server the drain must complete with nothing owed or
left to re-check, and the namespace must carry judged marks and resolver spend; on an
`off` server neither. A run that fails it is marked `treatment: FAILED`, with a
`failure` that says why, and its numbers do not count.

Cost: about 60 resolver calls per variant on the `on` server, each about 1.3k
input and at most 0.8k output tokens (`gpt-6.1-sol`, $2 / $10 per M: under
$0.01 a call), and twelve extraction calls for `messages`. Both variants on
both servers come to well under $1; `--budget-usd` (default 1.0) stops before
the next variant once the measured spend passes it.

Server setup: the `on` server with LOOM_MEMORY_CONFLICT_RESOLVER_ENABLED=true and
a short LOOM_MEMORY_CONFLICT_RESOLVER_INTERVAL_SECONDS (e.g. 5) so the drain
is quick; the `off` server with LOOM_MEMORY_CONFLICT_RESOLVER_ENABLED=false;
both with real embedder and LLM keys and LOOM_AUTH_REQUIRED=false, and each on its
own migrated ClickHouse (LOOM_CH_HOST / LOOM_CH_PORT).

Run (from the repository root):

    PYTHONPATH=$PWD .venv/bin/python bench/conflict_resolver_long_session.py \\
        --base-url on=http://localhost:7781 --base-url off=http://localhost:7782 \\
        --ch on=127.0.0.1:8123 --ch off=127.0.0.1:8124 \\
        --ch-user loom --ch-password "$LOOM_CH_PASSWORD" \\
        --variant both --out /tmp/resolver_long_session.json

`--dry-run` prints the scenario and the plan without touching a server.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from bench._gate import measurement_fields
from bench._http import post_json
from bench.bench_namespaces import erase_on_exit_sync, register_body, remember_bench_namespace
from bench.conflict_resolver_scenario import (
    ATTRIBUTES,
    BY_KEY,
    CURRENT,
    DELETE_IN,
    FACTS,
    KEPT,
    OLDER,
    RESTORE_IN,
    SESSIONS,
    dialogue,
    relation_of,
)

ORG = "bench"
AGENT = "assistant"
USER = "u1"
PINNED_PAIR = {"code_freeze", "freeze_change"}

# attribute -> one pattern per value, oldest first (the messages variant's scoring).
VALUE_PATTERNS: dict[str, list[str]] = {
    "home city": [r"\bparis\b", r"\blisbon\b", r"\brome\b"],
    "employer": [r"\bacme\b", r"\bglobex\b"],
    "manager": [r"\bana\b", r"\bben\b", r"\bchen\b"],
    "standup time": [r"nine[ -]thirty|\b9[:.]30\b", r"quarter past ten|\b10[:.]15\b"],
    "ci provider": [r"\bgitlab\b", r"\bbuildkite\b"],
    "diet": [r"\bvegetarian\b", r"\bvegan\b", r"\bpescatarian\b"],
}
PINNED_PATTERN = r"code freeze"

Post = Callable[[str, dict[str, Any]], Any]


# ---------------------------------------------------------------- writes


def _identity(namespace: str, session: int) -> dict[str, Any]:
    return {
        "org": ORG,
        "namespace": namespace,
        "agent": AGENT,
        "user_id": USER,
        "session_id": f"s{session}",
    }


def _when(start: datetime, session: int, i: int) -> str:
    return (start + timedelta(weeks=session, minutes=i)).isoformat()


def _entry(start: datetime, session: int, i: int, key: str) -> dict[str, Any]:
    f = BY_KEY[key]
    return {
        "memory_key": f.key,
        "value": f.value,
        "relations": [["user", relation_of(f.key), f.obj]] if f.obj else [],
        "valid_at": _when(start, session, i),
        "pinned": f.pinned,
    }


def write_set_session(post: Post, namespace: str, session: int, start: datetime) -> None:
    """The session's facts under the scenario's keys, then its delete or Undo."""
    keys = [f.key for f in FACTS if f.session == session]
    entries = [_entry(start, session, i, k) for i, k in enumerate(keys)]
    ident = _identity(namespace, session)
    if session % 2 == 0:
        post("memory.bulk_set", {**ident, "entries": entries, "detect_updates": False})
    else:
        for e in entries:
            post("memory.set", {**ident, **e})
    if session in DELETE_IN:
        key = DELETE_IN[session]
        post("memory.delete", {**_identity(namespace, BY_KEY[key].session), "memory_key": key})
    if session in RESTORE_IN:
        key = RESTORE_IN[session]
        post("memory.restore", {**_identity(namespace, BY_KEY[key].session), "memory_key": key})


def write_messages_session(post: Post, namespace: str, session: int, start: datetime) -> None:
    """The session as a dialogue for the extractor; a pinned fact through memory.set."""
    ident = _identity(namespace, session)
    turns = dialogue(session)
    for i, t in enumerate(turns):
        t["timestamp"] = _when(start, session, i)
    if turns:
        post("memory.set_from_messages", {**ident, "messages": turns, "detect_updates": False})
    pinned = [f.key for f in FACTS if f.session == session and f.pinned]
    for i, key in enumerate(pinned):
        post("memory.set", {**ident, **_entry(start, session, len(turns) + i, key)})


# ---------------------------------------------------------------- ClickHouse reads

_QUEUE_SQL = """
SELECT countIf(conflict_checked_at < toDateTime(2, 'UTC')) AS owed,
       countIf(status = 'conflicted'
               AND (conflict_checked_at IS NULL OR conflict_checked_at > toDateTime(2, 'UTC')))
           AS recheck,
       countIf(toUnixTimestamp(conflict_checked_at) = 2) AS judged
FROM loom.memory_embeddings FINAL
WHERE org = {org:String} AND namespace = {ns:String}
  AND tombstoned_at IS NULL AND scope = 'private'
  AND status IN ('active', 'conflicted') AND embedding_dim > 0
"""

_ROWS_SQL = """
SELECT memory_key, toString(memory_id) AS id, toString(value) AS value, status,
       toString(superseded_by) AS superseded_by, length(conflict_with) AS flags, pinned
FROM loom.memory FINAL
WHERE org = {org:String} AND namespace = {ns:String} AND tombstoned_at IS NULL
SETTINGS select_sequential_consistency = 1
"""

_RETIRED_SQL = """
SELECT subject FROM loom.audit_log
WHERE org = {org:String} AND namespace = {ns:String} AND event_type = 'memory.auto_supersede'
"""

_COST_SQL = """
SELECT sumIf(llm_calls, operation = 'worker.conflict_resolver') AS resolver_calls,
       toFloat64(sumIf(upstream_cost_usd, operation = 'worker.conflict_resolver'))
           AS resolver_cost_usd,
       toFloat64(sum(upstream_cost_usd)) AS namespace_cost_usd
FROM loom.agent_events
WHERE org = {org:String} AND namespace = {ns:String}
"""


# The resolver's last pass on the arm's store, for a drain that did not complete.
_LAST_PASS_SQL = """
SELECT status, detail FROM loom.worker_status
WHERE worker = 'conflict_resolver' ORDER BY pass_started DESC LIMIT 1
"""


def _arm_clickhouses(
    args: argparse.Namespace, servers: list[tuple[str, str]]
) -> dict[str, tuple[str, int]]:
    """Each arm's ClickHouse (host, port): its `--ch label=HOST:PORT`, else `--ch-host`
    and `--ch-port`. Two arms on one store are refused: the resolver judges every
    namespace of its store, the control's too."""
    named = {}
    for item in args.ch:
        label, _, target = item.partition("=")
        host, _, port = target.rpartition(":")
        named[label] = (host, int(port))
    out = {label: named.get(label, (args.ch_host, args.ch_port)) for label, _ in servers}
    if len(out) > 1 and len(set(out.values())) < len(out):
        raise SystemExit("each arm needs its own ClickHouse: --ch on=HOST:PORT --ch off=HOST:PORT")
    return out


def _ch(args: argparse.Namespace, target: tuple[str, int]) -> Any:
    import clickhouse_connect

    return clickhouse_connect.get_client(
        host=target[0],
        port=target[1],
        username=args.ch_user,
        password=args.ch_password,
        database=args.ch_database,
        secure=args.ch_secure,
    )


def _query(ch: Any, sql: str, namespace: str) -> list[dict[str, Any]]:
    res = ch.query(sql, parameters={"org": ORG, "ns": namespace})
    return [dict(zip(res.column_names, r, strict=True)) for r in res.result_rows]


def wait_for_drain(ch: Any, namespace: str, *, timeout_s: float, poll_s: float) -> float | None:
    """Seconds until nothing in the namespace is owed or left to re-check (two polls
    in a row), or None at the timeout."""
    started, quiet = time.monotonic(), 0
    while time.monotonic() - started < timeout_s:
        q = _query(ch, _QUEUE_SQL, namespace)[0]
        quiet = quiet + 1 if int(q["owed"]) + int(q["recheck"]) == 0 else 0
        if quiet >= 2:
            return round(time.monotonic() - started, 1)
        time.sleep(poll_s)
    return None


# ---------------------------------------------------------------- scoring


def stated(attr: str, text: str) -> int | None:
    """The index of the latest value of `attr` that `text` names, or None."""
    hits = [i for i, p in enumerate(VALUE_PATTERNS[attr]) if re.search(p, text, re.I)]
    return max(hits) if hits else None


def _states_older(text: str) -> bool:
    return any(
        (i := stated(a, text)) is not None and i < len(VALUE_PATTERNS[a]) - 1 for a in ATTRIBUTES
    )


def _states_any(text: str) -> bool:
    return any(stated(a, text) is not None for a in ATTRIBUTES)


def _live(r: dict[str, Any]) -> bool:
    return str(r["status"]) in ("active", "conflicted")


def score_by_key(rows: dict[str, dict[str, Any]], top: dict[str, str]) -> dict[str, Any]:
    clean = stale = 0
    for name, a in ATTRIBUTES.items():
        head = rows.get(a.keys[-1])
        older_live = any(k in rows and _live(rows[k]) for k in a.keys[:-1])
        clean += bool(head and head["status"] == "active" and not older_live)
        stale += top.get(name, "") in a.keys[:-1]
    return {
        "current_only_live": clean / len(ATTRIBUTES),
        "stale_retrieval": stale / len(ATTRIBUTES),
        "false_retirements": sum(
            1 for k in KEPT | CURRENT if k in rows and rows[k]["status"] == "superseded"
        ),
        "false_flags": sum(
            1 for k in KEPT - PINNED_PAIR if k in rows and rows[k]["status"] == "conflicted"
        ),
        "flagged_current": sum(
            1 for k in CURRENT if k in rows and rows[k]["status"] == "conflicted"
        ),
        "missing_rows": sorted({f.key for f in FACTS} - set(rows)),
        "older_live": sorted(k for k in OLDER if k in rows and _live(rows[k])),
    }


def score_by_content(rows: list[dict[str, Any]], top: dict[str, str]) -> dict[str, Any]:
    clean = stale = 0
    for name in ATTRIBUTES:
        last = len(VALUE_PATTERNS[name]) - 1
        live = [stated(name, str(r["value"])) for r in rows if _live(r)]
        live = [i for i in live if i is not None]
        flagged = any(
            r["status"] == "conflicted" and stated(name, str(r["value"])) is not None for r in rows
        )
        clean += bool(live) and all(i == last for i in live) and not flagged
        hit = stated(name, top.get(name, ""))
        stale += hit is not None and hit < last
    return {
        "current_only_live": clean / len(ATTRIBUTES),
        "stale_retrieval": stale / len(ATTRIBUTES),
        "false_retirements": sum(
            1 for r in rows if r["status"] == "superseded" and not _states_older(str(r["value"]))
        ),
        "false_flags": sum(
            1
            for r in rows
            if r["status"] == "conflicted"
            and not _states_any(str(r["value"]))
            and not re.search(PINNED_PATTERN, str(r["value"]), re.I)
        ),
        "rows": len(rows),
    }


# ---------------------------------------------------------------- one arm


def _top_hits(post: Post, namespace: str, *, by_key: bool) -> dict[str, str]:
    """attribute -> the top search hit's key (`set`) or text (`messages`)."""
    top: dict[str, str] = {}
    for name, a in ATTRIBUTES.items():
        body = {
            "org": ORG,
            "namespace": namespace,
            "agent": "*",
            "query": a.query,
            "top_k": 5,
            **measurement_fields("memory.search"),
        }
        hits = list(post("memory.search", body).get("results") or [])
        if hits:
            top[name] = str(hits[0]["memory_key"] if by_key else hits[0]["content_excerpt"])
    return top


def run_arm(
    post: Post, ch: Any, label: str, variant: str, args: argparse.Namespace
) -> dict[str, Any]:
    namespace = f"resolver-long-{variant}-{uuid.uuid4().hex[:8]}"
    remember_bench_namespace(ORG, namespace)
    post(
        "namespace.register",
        register_body(
            ORG,
            namespace,
            description="conflict resolver long-session scenario",
            keep=args.keep_namespaces,
            extra={"default_conflict_policy": "newest_wins"},
        ),
    )
    start = datetime.now(UTC).replace(microsecond=0) - timedelta(weeks=SESSIONS + 1)
    write = write_set_session if variant == "set" else write_messages_session
    t0 = time.monotonic()
    for session in range(1, SESSIONS + 1):
        write(post, namespace, session, start)
    write_s = round(time.monotonic() - t0, 1)
    drain_s = (
        wait_for_drain(ch, namespace, timeout_s=args.drain_timeout, poll_s=args.poll)
        if label == "on"
        else 0.0
    )
    time.sleep(args.settle)  # event and audit rows are buffered inserts
    rows = _query(ch, _ROWS_SQL, namespace)
    top = _top_hits(post, namespace, by_key=variant == "set")
    if variant == "set":
        scores = score_by_key({str(r["memory_key"]): r for r in rows}, top)
    else:
        scores = score_by_content(rows, top)
    queue = _query(ch, _QUEUE_SQL, namespace)[0]
    cost = _query(ch, _COST_SQL, namespace)[0]
    outstanding = int(queue["owed"]) + int(queue["recheck"])
    failure = ""
    if label == "on" and (drain_s is None or outstanding):
        last = (_query(ch, _LAST_PASS_SQL, namespace) or [{}])[0]
        failure = (
            f"drain {'timed out' if drain_s is None else 'left rows'}: {queue['owed']} owed,"
            f" {queue['recheck']} to re-check; last pass {last.get('status')}"
            f" {last.get('detail')}"
        )
    retired = [
        e
        for r in _query(ch, _RETIRED_SQL, namespace)
        if (s := json.loads(r["subject"])).get("reason") == "conflict_resolver"
        for e in s.get("retired", [])
    ]
    judged_marks, calls = int(queue["judged"]), int(cost["resolver_calls"] or 0)
    ran = judged_marks > 0 and calls > 0 and not failure
    treatment = "ok" if (ran if label == "on" else judged_marks == 0 and calls == 0) else "FAILED"
    return {
        "server": label,
        "variant": variant,
        "namespace": namespace,
        "treatment": treatment,
        "failure": failure,
        **scores,
        "resolver_retired": len(retired),
        "resolver_calls": calls,
        "judged_marks": judged_marks,
        "resolver_cost_usd": round(float(cost["resolver_cost_usd"] or 0.0), 4),
        "namespace_cost_usd": round(float(cost["namespace_cost_usd"] or 0.0), 4),
        "write_s": write_s,
        "drain_s": drain_s,
        "top_hits": top,
    }


# ---------------------------------------------------------------- CLI

_COLUMNS = (
    "server",
    "variant",
    "treatment",
    "current_only_live",
    "stale_retrieval",
    "false_retirements",
    "false_flags",
    "resolver_retired",
    "resolver_calls",
    "resolver_cost_usd",
    "namespace_cost_usd",
    "drain_s",
)


def print_table(results: list[dict[str, Any]]) -> None:
    print(" | ".join(_COLUMNS))
    print(" | ".join("---" for _ in _COLUMNS))
    for r in results:
        cells = []
        for c in _COLUMNS:
            v = r.get(c)
            cells.append(f"{v:.2f}" if isinstance(v, float) and c != "drain_s" else str(v))
        print(" | ".join(cells))


def print_plan(servers: list[tuple[str, str]], variants: list[str]) -> None:
    print(f"{len(FACTS)} facts over {SESSIONS} sessions; attributes:")
    for name, a in ATTRIBUTES.items():
        values = " -> ".join(f"{BY_KEY[k].value} (s{BY_KEY[k].session})" for k in a.keys)
        print(f"  {name}: {values}")
    print(f"kept live: {len(KEPT)} facts; deleted+restored: {sorted(set(DELETE_IN.values()))}")
    for label, url in servers or [("on", "<url>"), ("off", "<url>")]:
        for v in variants:
            print(f"plan: {label} {url} variant={v}")


def _servers(raw: list[str]) -> list[tuple[str, str]]:
    out = []
    for item in raw:
        label, _, url = item.partition("=")
        if label not in ("on", "off") or not url:
            raise SystemExit(f"--base-url takes on=URL or off=URL, got {item!r}")
        out.append((label, url.rstrip("/")))
    return out


def _parse(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument(
        "--base-url",
        action="append",
        default=[],
        help="on=URL or off=URL: a server with the resolver on, or off (repeatable)",
    )
    p.add_argument("--token", default=os.environ.get("LOOM_API_KEY", ""))
    p.add_argument("--variant", choices=("set", "messages", "both"), default="set")
    p.add_argument(
        "--ch",
        action="append",
        default=[],
        help="on=HOST:PORT or off=HOST:PORT: the arm's own ClickHouse (repeatable)",
    )
    p.add_argument("--ch-host", default=os.environ.get("LOOM_CH_HOST", "127.0.0.1"))
    p.add_argument("--ch-port", type=int, default=int(os.environ.get("LOOM_CH_PORT", "8123")))
    p.add_argument("--ch-user", default=os.environ.get("LOOM_CH_USER", "default"))
    p.add_argument("--ch-password", default=os.environ.get("LOOM_CH_PASSWORD", ""))
    p.add_argument("--ch-database", default=os.environ.get("LOOM_CH_DATABASE", "loom"))
    p.add_argument("--ch-secure", action="store_true")
    p.add_argument("--drain-timeout", type=float, default=900.0, help="seconds")
    p.add_argument("--poll", type=float, default=5.0, help="seconds between queue reads")
    p.add_argument("--settle", type=float, default=5.0, help="seconds before the end reads")
    p.add_argument("--budget-usd", type=float, default=1.0)
    p.add_argument("--keep-namespaces", action="store_true")
    p.add_argument("--out", default="", help="write the results as JSON here")
    p.add_argument("--dry-run", action="store_true", help="print the scenario and plan")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse(argv)
    servers = _servers(args.base_url)
    variants = ["set", "messages"] if args.variant == "both" else [args.variant]
    if args.dry_run:
        print_plan(servers, variants)
        return 0
    if not servers:
        raise SystemExit("name at least one server: --base-url on=URL and/or off=URL")
    stores = _arm_clickhouses(args, servers)
    results: list[dict[str, Any]] = []
    spent = 0.0
    with httpx.Client() as client:
        for label, url in servers:

            def post(op: str, body: dict[str, Any], url: str = url) -> Any:
                return post_json(
                    client, f"{url}/v1/{op.removeprefix('/v1/')}", body, token=args.token
                )

            with erase_on_exit_sync(post, org=ORG, keep=args.keep_namespaces):
                for v in variants:
                    if spent > args.budget_usd:
                        print(f"budget ${args.budget_usd} spent; skipping {label}/{v}")
                        continue
                    r = run_arm(post, _ch(args, stores[label]), label, v, args)
                    spent += float(r["namespace_cost_usd"])
                    results.append(r)
                    print(json.dumps(r), file=sys.stderr)
    print_table(results)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(results, fh, indent=2)
    return 0 if all(r["treatment"] == "ok" for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
