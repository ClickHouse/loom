"""Op-planning bench: did the agent pick the right loom operation, and did
the store end up in the right state?

Two layers, scored separately — the shape Text2Mem Bench uses for
memory-operation languages, and the one BFCL uses for tool calls:

  planning   — natural-language instruction → the model, given loom's MCP
               tool surface, emits tool calls. Scored per task, all steps
               must match (BFCL's all-or-nothing):
                 tool      exact tool name, in order
                 required  every gold-required argument present
                 literal   every gold literal argument equal (strings
                           case-insensitive and whitespace-normalised;
                           bools / enums exact; lists order-insensitive)
                 no_extra  no argument the tool's schema does not declare
               SMA = fraction of tasks with every step matching on all four;
               tool accuracy / args ok / hallucination are reported too.
  execution  — the GOLD plan (not the model's) is run against a live loom
               after each task's `setup`, and the task's HTTP `assertions`
               are checked. EMR = satisfied assertions / total assertions.
               This is a semantics check on loom itself: the plan the bench
               calls correct has to leave the store in the state the
               instruction meant.

Tasks live in bench/fixtures/op_planning_tasks.jsonl (loom-native, hand
written) or come from bench/text2mem_convert.py (the public Text2Mem
English set, planning-only). A task whose gold names a tool the surface
does not expose is a GAP, not a failure. A task whose gold is empty
(`structure: noop`) passes strictly when the model calls nothing, and
leniently when it only reads.

bench/fixtures/op_planning_tasks_retired.jsonl holds seven memory.apply
tasks outside the main set; with `--tasks` it re-scores them, and with the
main file's `legacy` cohort they make up the 37-task set earlier scores used,
so a new score can be read against those. Both files
are hand written; their unit test keeps the ids disjoint.

Usage:
  # planning (needs ANTHROPIC_API_KEY); toolset = all | core | atomic,
  # profile = compact | standard | full (standard and full send the model the
  # same thing: the bench never forwards outputSchema)
  python bench/op_planning.py plan --model claude-sonnet-5 --toolset all --runs 1
  # execution against a running loom (LOOM_BASE_URL, LOOM_API_KEY or LOOM_JWT_SECRET)
  python bench/op_planning.py execute --base-url http://localhost:7789
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
import uuid
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bench.bench_namespaces import (
    erase_on_exit_sync,
    register_body,
    remember_bench_namespace,
)

ENDPOINT = "https://api.anthropic.com/v1/messages"
FIXTURE = Path(__file__).parent / "fixtures" / "op_planning_tasks.jsonl"

SYSTEM = (
    "You are an assistant with access to the Loom memory tools. The user's org, "
    "namespace and agent are already configured; never ask for them. Carry out the "
    "instruction by calling the tool or tools it needs — when it takes several steps, "
    "emit every call now, in order. Do not answer in prose. If the instruction needs no "
    "memory operation at all (nothing to save, change, look up or forget), reply with one "
    "short sentence instead of calling a tool."
)
# The first model turn: `auto` lets the model answer a no-op task in prose
# (four tasks whose gold is no write); `any` forces a tool call
# on turn one, the older protocol.
FIRST_TURN_DEFAULT = "auto"


# ---------------------------------------------------------------- tool surface


def load_tools(
    toolset: str = "all", profile: str = "compact", code_tools: str = "env"
) -> list[dict[str, Any]]:
    """The MCP tools/list exactly as the server would emit it, offline.

    ``code_tools`` pins LOOM_MCP_CODE_TOOLS for the listing: ``on`` / ``off``,
    or ``env`` to take the loaded settings (the deployment's .env). The
    ``coding`` toolset lists the code tools regardless."""
    from server.settings import settings
    from server.transport.mcp_tools import _tools_descriptor

    settings.mcp_toolset = toolset
    if code_tools in ("on", "off"):
        settings.mcp_code_tools = code_tools == "on"
    descriptors = asyncio.run(_tools_descriptor(schema_profile=profile))
    out = []
    for d in descriptors:
        schema = dict(d.get("inputSchema") or {})
        schema.setdefault("type", "object")
        out.append(
            {
                "name": d["name"],
                "description": d.get("description") or d["name"],
                "input_schema": schema,
            }
        )
    return out


# ---------------------------------------------------------------- scoring


def _norm(v: Any) -> Any:
    if isinstance(v, str):
        return re.sub(r"\s+", " ", v.strip().lower())
    if isinstance(v, list):
        return sorted(json.dumps(_norm(x), sort_keys=True) for x in v)
    if isinstance(v, dict):
        return {k: _norm(x) for k, x in sorted(v.items())}
    return v


def _literal_ok(gold: Any, got: Any) -> bool:
    if isinstance(gold, dict) and isinstance(got, dict):
        # a gold dict constrains only the keys it names (target: {"filter": {...}})
        return all(k in got and _literal_ok(v, got[k]) for k, v in gold.items())
    if isinstance(gold, bool) or isinstance(got, bool):
        return gold is got or gold == got
    return _norm(gold) == _norm(got)


def score_step(
    gold: dict[str, Any], call: dict[str, Any] | None, schema: dict[str, Any] | None
) -> dict[str, Any]:
    """One gold step against one emitted call. `schema` is the EMITTED tool's
    input schema (for the hallucination check); a wrong tool is scored on its
    own schema, not the gold's."""
    if call is None:
        # a missing step is a miss, not a hallucination
        return {
            "tool": False,
            "required": False,
            "literal": False,
            "no_extra": True,
            "missing": True,
            "got": None,
            "ok": False,
        }
    accepted = {gold["tool"], *gold.get("alt", [])}
    tool_ok = call.get("name") in accepted
    args = call.get("input") or {}
    required_ok = all(a in args for a in gold.get("required", []))
    literal_ok = all(
        k in args and _literal_ok(v, args[k]) for k, v in (gold.get("args") or {}).items()
    )
    declared = set(((schema or {}).get("properties") or {}).keys())
    no_extra = True if not declared else all(k in declared for k in args)
    ok = tool_ok and required_ok and literal_ok and no_extra
    return {
        "tool": tool_ok,
        "required": required_ok,
        "literal": literal_ok,
        "no_extra": no_extra,
        "missing": False,
        "got": call.get("name"),
        "ok": ok,
    }


def score_task(
    task: dict[str, Any], calls: list[dict[str, Any]], schemas: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    gold = task["gold"]
    steps = []
    for i, g in enumerate(gold):
        call = calls[i] if i < len(calls) else None
        steps.append(score_step(g, call, schemas.get((call or {}).get("name", g["tool"]))))
    extra_calls = max(0, len(calls) - len(gold))
    return {
        "id": task["id"],
        "class": task.get("class", {}),
        "steps": steps,
        "extra_calls": extra_calls,
        "sma": all(s["ok"] for s in steps) and extra_calls == 0,
        # a no-op gold is right when nothing was called
        "tool_acc": (sum(s["tool"] for s in steps) / len(steps))
        if steps
        else (1.0 if not calls else 0.0),
        "args_ok": all(s["required"] and s["literal"] for s in steps),
        "hallucinated": any(not s["no_extra"] for s in steps),
        "missing_steps": sum(1 for s in steps if s.get("missing")),
        "got": [c.get("name") for c in calls],
    }


READ_TOOLS = frozenset(
    {
        "loom_memory_get",
        "loom_memory_get_by_id",
        "loom_memory_list",
        "loom_memory_history",
        "loom_memory_related",
        "loom_memory_provenance",
        "loom_memory_lineage",
        "loom_facts_related",
        "loom_knowledge_list",
        "loom_knowledge_get",
        "loom_knowledge_search",
        "loom_search",
        "loom_memory_ask",
    }
)


def _same_scope(a: dict[str, Any], b: dict[str, Any]) -> bool:
    return _norm(a.get("target")) == _norm(b.get("target")) and a.get("action") == b.get("action")


def lenient_calls(gold: list[dict[str, Any]], calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The protocol-lenient view of a plan (reported beside the strict one):

    * a `memory.apply` preview immediately followed by the same-scope
      enforcing call collapses into the enforcing call — that two-step is the
      protocol the tool description asks for, not a wrong plan;
    * read-only calls are dropped when the gold names no read tool — a read
      before a write (get_by_id → update_by_id) is caution, not a wrong op.
    """
    gold_applies = sum(1 for g in gold if g["tool"] == "loom_memory_apply")
    got_applies = sum(1 for c in calls if c.get("name") == "loom_memory_apply")
    collapse = got_applies > gold_applies  # the gold itself may ask for preview + enforce
    out: list[dict[str, Any]] = []
    i = 0
    while i < len(calls):
        c = calls[i]
        nxt = calls[i + 1] if i + 1 < len(calls) else None
        if (
            collapse
            and c.get("name") == "loom_memory_apply"
            and (c.get("input") or {}).get("dry_run", True) is not False
            and nxt is not None
            and nxt.get("name") == "loom_memory_apply"
            and (nxt.get("input") or {}).get("dry_run") is False
            and _same_scope(c.get("input") or {}, nxt.get("input") or {})
        ):
            out.append(nxt)
            i += 2
            continue
        out.append(c)
        i += 1
    gold_reads = any(g["tool"] in READ_TOOLS or set(g.get("alt", [])) & READ_TOOLS for g in gold)
    if not gold_reads:
        out = [c for c in out if c.get("name") not in READ_TOOLS]
    return out


def archetype_cohort(tasks: list[dict[str, Any]], tool_names: set[str]) -> list[dict[str, Any]]:
    """The tasks a toolset can answer: every gold tool is in ``tool_names``
    (a no-op gold always qualifies). measures the coding
    archetype as the tasks the ``coding`` listing carries every gold tool of,
    run on each arm so both arms score the same set."""
    return [t for t in tasks if all(g["tool"] in tool_names for g in t["gold"])]


def gaps(tasks: list[dict[str, Any]], tool_names: set[str]) -> list[str]:
    return [t["id"] for t in tasks if any(g["tool"] not in tool_names for g in t["gold"])]


# ---------------------------------------------------------------- planning


class ModelUnavailable(RuntimeError):
    """The planning model did not answer after the retries."""


def _post_model(client: httpx.Client, key: str, payload: dict[str, Any]) -> dict[str, Any]:
    """One Messages call, retried on 429/5xx. Exhausted retries raise: an empty
    reply would read as "no tool call", which scores a no-op task as a pass."""
    for attempt in range(4):
        r = client.post(
            ENDPOINT,
            headers={
                "x-api-key": key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json=payload,
            timeout=120,
        )
        if r.status_code in (429, 500, 502, 503, 529):
            time.sleep(2 * (attempt + 1))
            continue
        r.raise_for_status()
        return r.json()
    raise ModelUnavailable(f"{ENDPOINT} kept answering {r.status_code}")


def _stub_result(call: dict[str, Any]) -> str:
    """A plausible tool result so a multi-step plan can continue: the model
    never touches a real store during planning. A dry-run apply gets a
    preview, a set gets an id, everything else a bare ok."""
    name = call.get("name", "")
    args = call.get("input") or {}
    if name == "loom_memory_apply" and args.get("dry_run", True):
        rows = [{"memory_key": f"k{i}", "status": "would_apply"} for i in range(3)]
        return json.dumps(
            {"dry_run": True, "matched": 3, "would_apply": 3, "refused": 0, "rows": rows}
        )
    if name in ("loom_memory_set", "loom_memory_pin"):
        dry = bool(args.get("dry_run"))
        return json.dumps(
            {
                "memory_id": "" if dry else "00000000-0000-4000-8000-000000000001",
                "version": 0 if dry else 1,
                "content_sha256": "0" * 64,
                "conflict_verdict": "clear",
                "peers": [],
                "next": "",
                "noop": dry,
                "dry_run": dry,
            }
        )
    if name == "loom_memory_update":
        return json.dumps(
            {
                "memory_id": "00000000-0000-4000-8000-000000000001",
                "version": 2,
                "conflict_verdict": "clear",
                "peers": [],
                "next": "",
            }
        )
    if name == "loom_memory_get":
        return json.dumps(
            {"found": True, "memory_key": args.get("memory_key", "k0"), "value": "…", "version": 1}
        )
    if name in ("loom_search", "loom_memory_ask"):
        return json.dumps(
            {"results": [{"memory_key": "k0", "value": "…", "score": 0.8}], "answer": "…"}
        )
    if name == "loom_memory_history":
        return json.dumps(
            {"versions": [{"version": 2, "tombstoned_at": "2026-09-12T00:00:00"}, {"version": 1}]}
        )
    return json.dumps({"ok": True})


def _call_model(
    client: httpx.Client,
    key: str,
    model: str,
    tools: list[dict[str, Any]],
    prompt: str,
    *,
    max_steps: int = 4,
    first_turn: str = FIRST_TURN_DEFAULT,
) -> tuple[list[dict[str, Any]], int, int]:
    """Multi-turn: the model plans step by step, each tool call answered with a
    stub result, until it stops calling tools or `max_steps` is reached. The
    first turn's tool_choice is `first_turn` (`auto` by default, `any` forces
    a call); later turns leave the model free to stop. Text2Mem
    workflows are 2–6 steps executed in order; BFCL's multi-turn category has
    the same shape."""
    messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
    calls: list[dict[str, Any]] = []
    tin = tout = 0
    for step in range(max_steps):
        payload = {
            "model": model,
            "max_tokens": 800,
            "system": SYSTEM,
            "tools": tools,
            "tool_choice": {"type": first_turn if step == 0 else "auto"},
            "messages": messages,
        }
        data = _post_model(client, key, payload)
        usage = data.get("usage") or {}
        tin += int(usage.get("input_tokens", 0))
        tout += int(usage.get("output_tokens", 0))
        uses = [b for b in data.get("content", []) if b.get("type") == "tool_use"]
        if not uses:
            break
        calls.extend(uses)
        messages.append({"role": "assistant", "content": data.get("content", [])})
        messages.append(
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": u["id"], "content": _stub_result(u)}
                    for u in uses
                ],
            }
        )
    return calls, tin, tout


def run_plan(
    tasks: list[dict[str, Any]],
    *,
    model: str,
    toolset: str,
    profile: str,
    runs: int,
    key: str,
    max_steps: int = 4,
    first_turn: str = FIRST_TURN_DEFAULT,
    code_tools: str = "env",
    cohort_toolset: str = "",
) -> dict[str, Any]:
    tools = load_tools(toolset, profile, code_tools)
    if cohort_toolset:
        # the archetype cohort: tasks the named toolset lists every gold tool of
        cohort_names = {t["name"] for t in load_tools(cohort_toolset, profile, code_tools)}
        tasks = archetype_cohort(tasks, cohort_names)
        tools = load_tools(toolset, profile, code_tools)  # restore the arm's listing
    schemas = {t["name"]: t["input_schema"] for t in tools}
    names = set(schemas)
    gap_ids = set(gaps(tasks, names))
    scored: list[dict[str, Any]] = []
    errors: list[str] = []
    tokens_in: list[int] = []
    with httpx.Client() as client:
        for run in range(runs):
            for t in tasks:
                if t["id"] in gap_ids:
                    continue
                try:
                    calls, tin, _ = _call_model(
                        client,
                        key,
                        model,
                        tools,
                        t["prompt"],
                        max_steps=max_steps,
                        first_turn=first_turn,
                    )
                except (ModelUnavailable, httpx.HTTPError) as e:
                    errors.append(f"{t['id']} (run {run}): {e}"[:200])
                    continue  # excluded from the scores, never scored as "no call"
                tokens_in.append(tin)
                s = score_task(t, calls, schemas)
                s["lenient_sma"] = score_task(t, lenient_calls(t["gold"], calls), schemas)["sma"]
                s["calls"] = [{"name": c.get("name"), "input": c.get("input")} for c in calls]
                s["run"] = run
                s["cohort"] = t.get("cohort", "legacy")
                scored.append(s)
            print(f"run {run + 1}/{runs} done", file=sys.stderr)
    return {
        "model": model,
        "toolset": toolset,
        "profile": profile,
        "tools": len(tools),
        "listing_bytes": len(json.dumps(tools, separators=(",", ":"))),
        "gaps": sorted(gap_ids),
        "scored": scored,
        "errors": errors,
        "mean_input_tokens": (sum(tokens_in) / len(tokens_in)) if tokens_in else 0,
        "max_steps": max_steps,
        "first_turn": first_turn,
        "code_tools": code_tools,
        "cohort_toolset": cohort_toolset,
        "task_ids": [t["id"] for t in tasks],
    }


def summarise_plan(res: dict[str, Any]) -> str:
    rows = res["scored"]
    errors = res.get("errors") or []
    if not rows:
        return f"no tasks scored ({len(errors)} errored)"

    def agg(sub: list[dict[str, Any]]) -> str:
        n = len(sub)
        if not n:
            return "n=0"
        sma = sum(r["sma"] for r in sub) / n
        lenient = sum(r.get("lenient_sma", r["sma"]) for r in sub) / n
        tool = sum(r["tool_acc"] for r in sub) / n
        args = sum(r["args_ok"] for r in sub) / n
        hall = sum(r["hallucinated"] for r in sub) / n
        miss = sum(r["missing_steps"] for r in sub) / n
        return (
            f"n={n:<4} SMA={sma:.3f} SMA(lenient)={lenient:.3f} tool={tool:.3f} "
            f"args={args:.3f} halluc={hall:.3f} missing/task={miss:.2f}"
        )

    out = [
        f"model={res['model']} toolset={res['toolset']} ({res['tools']} tools, "
        f"{res['listing_bytes']} bytes) profile={res['profile']} "
        f"mean_in_tok={res['mean_input_tokens']:.0f} gaps={len(res['gaps'])} "
        f"first_turn={res.get('first_turn', 'any')} "
        f"code_tools={res.get('code_tools', 'env')} "
        f"cohort_toolset={res.get('cohort_toolset') or '-'} tasks={len(res.get('task_ids', []))}",
        f"  all           {agg(rows)}",
    ]
    if errors:
        out.append(f"  errored (not scored): {len(errors)}")
    cohorts = sorted({r.get("cohort", "legacy") for r in rows})
    if len(cohorts) > 1:
        for val in cohorts:
            sub = [r for r in rows if r.get("cohort", "legacy") == val]
            out.append(f"  cohort={val:<12} {agg(sub)}")
    for key in ("instruction_type", "structure"):
        for val in sorted({r["class"].get(key, "?") for r in rows}):
            sub = [r for r in rows if r["class"].get(key) == val]
            out.append(f"  {key}={val:<10} {agg(sub)}")
    misses = Counter()
    for r in rows:
        if not r.get("lenient_sma", r["sma"]):
            misses[(r["id"], tuple(r["got"]))] += 1
    if misses:
        out.append("  misses under the lenient view (id, tools emitted) × runs:")
        for (tid, got), n in misses.most_common(25):
            out.append(f"    {tid:<28} {list(got)} ×{n}")
    return "\n".join(out)


# ---------------------------------------------------------------- execution


def _token() -> str:
    tok = os.environ.get("LOOM_API_KEY", "")
    if tok:
        return tok
    if os.environ.get("LOOM_JWT_SECRET"):
        from server.auth import keyring
        from server.auth.jwt import mint_token

        keyring.load_for_offline_mint()
        return mint_token(
            subject="op-planning-bench",
            org=ORG,
            namespaces=["*"],
            roles=["admin"],
            ttl_seconds=3600,
            is_service_account=True,
        )
    return ""


ORG = "op-planning-bench"


def _post(
    client: httpx.Client, base_url: str, token: str, op: str, body: dict[str, Any]
) -> httpx.Response:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return client.post(f"{base_url}/v1/{op}", json=body, headers=headers, timeout=60)


def _fill(obj: Any, ns: str, ids: dict[str, str]) -> Any:
    """Substitute {ns} and {id:<key>} placeholders in bodies / expectations."""
    if isinstance(obj, str):
        obj = obj.replace("{ns}", ns)
        for k, v in ids.items():
            obj = obj.replace(f"{{id:{k}}}", v)
        return obj
    if isinstance(obj, list):
        return [_fill(x, ns, ids) for x in obj]
    if isinstance(obj, dict):
        return {k: _fill(v, ns, ids) for k, v in obj.items()}
    return obj


def _tool_to_op(tool: str) -> str:
    # loom_memory_apply → memory.apply ; loom_search is synthetic → memory.search
    if tool == "loom_search":
        return "memory.search"
    name = tool[len("loom_") :] if tool.startswith("loom_") else tool
    # ops are `<group>.<verb>` where group may itself contain '_' (working_memory)
    for group in ("working_memory", "memory_schema", "memory"):
        if name.startswith(group + "_"):
            return f"{group}.{name[len(group) + 1 :]}"
    return name.replace("_", ".", 1)


def _check(expect: dict[str, Any], body: dict[str, Any]) -> list[tuple[str, bool, str]]:
    """expect: {"path.to.field": value | {"op": "==|>=|<=|in|contains|exists", "value": v}}"""
    out = []
    for path, want in expect.items():
        cur: Any = body
        found = True
        for part in path.split("."):
            if isinstance(cur, list) and part.isdigit():
                idx = int(part)
                cur = cur[idx] if idx < len(cur) else None
            elif isinstance(cur, dict):
                cur = cur.get(part)
            else:
                cur = None
            if cur is None:
                found = False
                break
        if isinstance(want, dict) and "op" in want:
            op, val = want["op"], want.get("value")
            if op == "exists":
                ok = found
            elif op == ">=":
                ok = found and cur >= val
            elif op == "<=":
                ok = found and cur <= val
            elif op == "in":
                ok = found and cur in val
            elif op == "contains":
                ok = found and (val in cur if cur is not None else False)
            elif op == "approx":
                ok = found and abs(float(cur) - float(val)) < 1e-3
            else:
                ok = found and cur == val
        else:
            ok = (cur == want) if (found or want is None) else False
        out.append((path, ok, f"got={cur!r} want={want!r}"))
    return out


def run_execute(tasks: list[dict[str, Any]], *, base_url: str) -> dict[str, Any]:
    """Each task runs in its own throwaway namespace, registered as bench and
    erased when the run ends (also when it fails or is interrupted)."""
    token = _token()
    results = []
    with httpx.Client() as client:

        def post(path: str, body: dict[str, Any]) -> Any:
            r = _post(client, base_url, token, path.removeprefix("/v1/"), body)
            r.raise_for_status()
            return r.json()

        with erase_on_exit_sync(post, org=ORG):
            results = _execute_tasks(client, post, tasks, base_url=base_url, token=token)
    return {"base_url": base_url, "results": results}


def _execute_tasks(
    client: httpx.Client,
    post: Callable[[str, dict[str, Any]], Any],
    tasks: list[dict[str, Any]],
    *,
    base_url: str,
    token: str,
) -> list[dict[str, Any]]:
    results = []
    for t in tasks:
        if not t.get("assertions"):
            continue
        ns = f"opb-{uuid.uuid4().hex[:6]}"
        try:
            post("/v1/namespace.register", register_body(ORG, ns, description="op_planning"))
        except httpx.HTTPError as e:  # bookkeeping, never the measurement
            print(f"namespace.register {ns} failed (continuing): {e}", file=sys.stderr)
        remember_bench_namespace(ORG, ns)
        ids: dict[str, str] = {}
        ident = {"org": ORG, "namespace": ns, "agent": "bench"}
        failures: list[str] = []
        for step in t.get("setup", []):
            r = _post(
                client, base_url, token, step["op"], {**ident, **_fill(step["body"], ns, ids)}
            )
            if r.status_code >= 300:
                failures.append(f"setup {step['op']}: {r.status_code} {r.text[:160]}")
            elif step.get("save_id"):
                ids[step["save_id"]] = str(r.json().get("memory_id", ""))
        for g in t["gold"]:
            op = _tool_to_op(g["tool"])
            body = _fill(dict(g.get("args") or {}), ns, ids)
            body.update(_fill(g.get("exec_args") or {}, ns, ids))
            r = _post(client, base_url, token, op, {**ident, **body})
            if r.status_code >= 300:
                failures.append(f"gold {op}: {r.status_code} {r.text[:200]}")
            elif g.get("save_id"):
                ids[g["save_id"]] = str(r.json().get("memory_id", ""))
        checks: list[tuple[str, bool, str]] = []
        for a in t["assertions"]:
            r = _post(
                client,
                base_url,
                token,
                a["op"],
                {**ident, **_fill(a.get("body") or {}, ns, ids)},
            )
            body_json = (
                r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            )
            if a.get("expect_status") is not None:
                checks.append(
                    (
                        f"{a['op']}.status",
                        r.status_code == a["expect_status"],
                        f"got={r.status_code} want={a['expect_status']}",
                    )
                )
            for path, ok, detail in _check(a.get("expect") or {}, body_json):
                checks.append((f"{a['op']}.{path}", ok, detail))
        results.append(
            {
                "id": t["id"],
                "checks": checks,
                "failures": failures,
                "passed": not failures and all(ok for _, ok, _ in checks),
            }
        )
    return results


def summarise_execute(res: dict[str, Any]) -> str:
    rows = res["results"]
    total = sum(len(r["checks"]) for r in rows)
    sat = sum(sum(ok for _, ok, _ in r["checks"]) for r in rows)
    passed = sum(r["passed"] for r in rows)
    out = [
        f"execution against {res['base_url']}: tasks={len(rows)} passed={passed} "
        f"EMR={sat}/{total}={sat / total if total else 0:.3f}"
    ]
    for r in rows:
        if not r["passed"]:
            out.append(f"  FAIL {r['id']}")
            for f in r["failures"]:
                out.append(f"      {f}")
            for path, ok, detail in r["checks"]:
                if not ok:
                    out.append(f"      {path}: {detail}")
    return "\n".join(out)


# ---------------------------------------------------------------- main


def load_tasks(path: Path, limit: int = 0) -> list[dict[str, Any]]:
    tasks = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return tasks[:limit] if limit else tasks


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = ap.add_subparsers(dest="mode", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--tasks", default=str(FIXTURE))
    p.add_argument("--model", default="claude-sonnet-5")
    p.add_argument("--toolset", default="all", choices=["all", "core", "coding", "atomic"])
    p.add_argument("--profile", default="compact", choices=["compact", "standard", "full"])
    p.add_argument(
        "--code-tools",
        default="env",
        choices=["env", "on", "off"],
        help="pin LOOM_MCP_CODE_TOOLS for the listing (env = the loaded settings)",
    )
    p.add_argument(
        "--cohort-toolset",
        default="",
        choices=["", "core", "coding", "atomic"],
        help="score only the tasks whose every gold tool this toolset lists (the "
        "archetype cohort) — the same set on every arm",
    )
    p.add_argument("--runs", type=int, default=1)
    p.add_argument("--max-steps", type=int, default=4)
    p.add_argument(
        "--first-turn",
        default=FIRST_TURN_DEFAULT,
        choices=["auto", "any"],
        help="tool_choice on the first model turn: auto (default; a no-op task can be "
        "answered in prose) or any (forces a tool call — the older protocol)",
    )
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--out", default="")
    e = sub.add_parser("execute")
    e.add_argument("--tasks", default=str(FIXTURE))
    e.add_argument(
        "--base-url",
        default=os.environ.get("LOOM_BASE_URL", ""),
        help="the Loom execute writes its opb-* namespaces to (required)",
    )
    e.add_argument("--limit", type=int, default=0)
    e.add_argument("--out", default="")
    args = ap.parse_args()
    tasks = load_tasks(Path(args.tasks), args.limit)
    if args.mode == "plan":
        key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not key:
            print("ANTHROPIC_API_KEY required", file=sys.stderr)
            return 2
        res = run_plan(
            tasks,
            model=args.model,
            toolset=args.toolset,
            profile=args.profile,
            runs=args.runs,
            key=key,
            max_steps=args.max_steps,
            first_turn=args.first_turn,
            code_tools=args.code_tools,
            cohort_toolset=args.cohort_toolset,
        )
        print(summarise_plan(res))
        failed = len(res["errors"])
    else:
        if not args.base_url:
            print("execute needs --base-url (or LOOM_BASE_URL)", file=sys.stderr)
            return 2
        res = run_execute(tasks, base_url=args.base_url)
        print(summarise_execute(res))
        # a failed setup or gold op is a broken run; a failed assertion is a result
        failed = sum(1 for r in res["results"] if r["failures"])
    if args.out:
        Path(args.out).write_text(json.dumps(res, indent=1, default=str))
    if failed:
        print(f"{failed} task(s) errored and were not scored", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
