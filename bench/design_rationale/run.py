"""The `run` command: set up the arms in the harness's Loom, read the surfaces, answer
each task K times per arm, and judge each answer. Every stage writes its JSON file in the
output directory and skips what is already there, so a stopped run resumes.
"""

from __future__ import annotations

import json
import math
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bench.design_rationale import detectors, prompts
from bench.design_rationale.llm import CapReached, Ledger, Model, cost_usd, parse_json, price
from bench.design_rationale.loom import (
    Loom,
    knowledge_section,
    memory_section,
    section_keys,
    tokens,
)
from bench.design_rationale.taskset import TaskSet

ARMS = (0, 1, 2, 3, 4)
ARM_NAMES = {
    0: "no design knowledge",
    1: "whole document through knowledge.get",
    2: "document as knowledge, through loom_search",
    3: "gold rows through the pre-edit block and loom_search",
    4: "extracted rows through the pre-edit block and loom_search",
}
# Planning estimates for a dry run, before any surface is read.
SEARCH_SECTION_TOKENS = 800  # a loom_search section with the reading rule
PRE_EDIT_TOKENS = 300  # the pre-edit block's budget per file
CITED_TEXT_TOKENS = 1200  # the cited source text a judge reads
TYPICAL_OUT = {"A": 400, "B": 900, "C": 400, "judge": 500, "extract": 800}
JUDGE_OUT_CAP = 4000
EXTRACT_PROMPT_TOKENS = 1200  # the extractor's instructions around one window


@dataclass
class Config:
    out: Path
    arms: list[int]
    org: str = "bench"
    k: int = 4
    reader: str = ""
    judges: list[str] = field(default_factory=list)
    judge_orders: int = 1
    judge_repeats: int = 1
    max_usd: float = 0.0
    only: set[str] = field(default_factory=set)
    keep_namespaces: bool = False
    arm4_namespace: str = ""
    extract_repo: str = ""
    extract_model: str = ""
    extract_window_chars: int = 6000
    extract_out_cap: int = 4000


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text()) if path.exists() else {}


def save_json(path: Path, obj: Any) -> None:
    path.write_text(json.dumps(obj, indent=1, ensure_ascii=False))


def answer_key(tid: str, arm: int, sample: int) -> str:
    return f"{tid}/arm{arm}/s{sample}"


# ---------------------------------------------------------------- plan (dry run)


def extraction_calls(ts: TaskSet, window_chars: int) -> list[tuple[str, int]]:
    """(document id, most model calls) of each extraction run: one per window, two when
    the first reply is unusable; a revised document is extracted again."""
    return [
        (d.id, 2 * max(1, math.ceil(len(d.content) / window_chars))) for d in ts.documents.values()
    ]


def plan(ts: TaskSet, cfg: Config) -> dict[str, Any]:
    """The calls and tokens a run would spend, worst case (every output at its cap, the
    figure the spend cap holds against) and typical (outputs at `TYPICAL_OUT`)."""
    tasks = ts.all_tasks(cfg.only)
    passes = max(1, len(cfg.judges)) * cfg.judge_orders * cfg.judge_repeats
    rows: list[dict[str, Any]] = []
    for arm in cfg.arms:
        for kind, t in tasks:
            if arm == 1:
                ctx = tokens(ts.document_of(t).content)
            elif arm == 2:
                ctx = SEARCH_SECTION_TOKENS
            elif arm in (3, 4):
                ctx = SEARCH_SECTION_TOKENS + PRE_EDIT_TOKENS * len(ts.task_paths(t))
            else:
                ctx = 0
            system, user, cap = prompts.answer_prompt(kind, t, "", ts.system)
            a_in = tokens(system + user) + ctx
            j_in = 900 + ctx + cap + CITED_TEXT_TOKENS
            rows.append(
                {"arm": arm, "kind": kind, "answer_in": a_in, "answer_cap": cap, "judge_in": j_in}
            )
    k = cfg.k
    answer_calls = len(rows) * k
    judge_calls = answer_calls * passes
    usage = {
        "answer": {
            "calls": answer_calls,
            "in": sum(r["answer_in"] for r in rows) * k,
            "out_worst": sum(r["answer_cap"] for r in rows) * k,
            "out_typical": sum(TYPICAL_OUT[r["kind"]] for r in rows) * k,
        },
        "judge": {
            "calls": judge_calls,
            "in": sum(r["judge_in"] for r in rows) * k * passes,
            "out_worst": JUDGE_OUT_CAP * judge_calls,
            "out_typical": TYPICAL_OUT["judge"] * judge_calls,
        },
    }
    extract = (
        extraction_calls(ts, cfg.extract_window_chars)
        if (4 in cfg.arms and not cfg.arm4_namespace)
        else []
    )
    n_extract = sum(n for _, n in extract)
    usage["extract"] = {
        "calls": n_extract,
        "in": n_extract * (cfg.extract_window_chars // 4 + EXTRACT_PROMPT_TOKENS),
        "out_worst": n_extract * cfg.extract_out_cap,
        "out_typical": n_extract * TYPICAL_OUT["extract"],
    }
    models = {
        "answer": cfg.reader,
        "judge": cfg.judges[0] if cfg.judges else "",
        "extract": cfg.extract_model,
    }
    for stage, u in usage.items():
        model = models[stage]
        if stage == "judge" and len(cfg.judges) > 1:
            # Judge calls split evenly over the judge models.
            share = [
                cost_usd(m, u["in"], u["out_worst"]) / len(cfg.judges)
                for m in cfg.judges
                if price(m)
            ]
            typ = [
                cost_usd(m, u["in"], u["out_typical"]) / len(cfg.judges)
                for m in cfg.judges
                if price(m)
            ]
            priced = len(share) == len(cfg.judges)
            u["usd_worst"] = round(sum(share), 4) if priced else None
            u["usd_typical"] = round(sum(typ), 4) if priced else None
        elif price(model) or not u["calls"]:
            u["usd_worst"] = (
                round(cost_usd(model, u["in"], u["out_worst"]), 4) if u["calls"] else 0.0
            )
            u["usd_typical"] = (
                round(cost_usd(model, u["in"], u["out_typical"]), 4) if u["calls"] else 0.0
            )
        else:
            u["usd_worst"] = u["usd_typical"] = None
        u["model"] = model if stage != "judge" else ",".join(cfg.judges)
    from bench.design_rationale.score import planning_mde

    counts = {kind: sum(1 for kk, _ in tasks if kk == kind) for kind in "ABC"}
    return {
        "arms": {a: ARM_NAMES[a] for a in cfg.arms},
        "tasks": counts,
        "documents": len(ts.documents),
        "k": k,
        "judge_passes_per_answer": passes,
        "usage": usage,
        "planning_mde": {kind: planning_mde(n) for kind, n in counts.items() if n},
    }


def print_plan(p: dict[str, Any]) -> None:
    print("arms: " + "; ".join(f"{a} = {n}" for a, n in p["arms"].items()))
    print(
        f"tasks: A {p['tasks']['A']}, B {p['tasks']['B']}, C {p['tasks']['C']} over "
        f"{p['documents']} document(s); K = {p['k']}; {p['judge_passes_per_answer']} judge "
        "pass(es) per answer"
    )
    total_w = total_t = 0.0
    for stage, u in p["usage"].items():
        usd = (
            f"{u['usd_typical']:.2f} typical, {u['usd_worst']:.2f} worst case USD"
            if u["usd_worst"] is not None
            else "unpriced model"
        )
        print(
            f"{stage}: {u['calls']} call(s) on {u['model'] or '-'}, {u['in']:,} tokens in, "
            f"{u['out_typical']:,} out typical ({u['out_worst']:,} at the caps); {usd}"
        )
        total_w += u["usd_worst"] or 0.0
        total_t += u["usd_typical"] or 0.0
    print(f"total: {total_t:.2f} USD typical, {total_w:.2f} USD worst case")
    print(
        "planning MDE (paired, 80% power, two-sided 5%): "
        + ", ".join(f"{k} {v * 100:.0f} points" for k, v in p["planning_mde"].items())
    )


# ---------------------------------------------------------------- setup


def namespaces(state: dict[str, Any], cfg: Config) -> dict[str, str]:
    prefix = state.setdefault("prefix", "dr-" + secrets.token_hex(3))
    return {
        "kb": f"{prefix}-kb",
        "gold": f"{prefix}-gold",
        "extract": cfg.arm4_namespace or f"{prefix}-extract",
    }


def orgs(cfg: Config) -> dict[str, str]:
    """The org of each role's namespace; arm 4 served from a sync's rows keeps the org
    they were written in."""
    out = {role: f"{cfg.org}-dr-{role}" for role in ("kb", "gold", "extract")}
    if cfg.arm4_namespace:
        out["extract"] = cfg.org
    return out


def bind(loom: Loom, state: dict[str, Any], cfg: Config) -> dict[str, str]:
    ns = namespaces(state, cfg)
    for role, org in orgs(cfg).items():
        loom.bind(ns[role], org)
    return ns


def gold_pointer(ts: TaskSet, row: Any, kb: str) -> str:
    uri = ts.documents[row.document].source_uri
    return f"knowledge:{kb}/{uri}" + (f"#{row.section}" if row.section else "")


def setup(ts: TaskSet, cfg: Config, loom: Loom, ledger: Ledger, state: dict[str, Any]) -> None:
    """Fill the arms' namespaces: documents into kb (and extract), gold rows into gold
    (never into extract), the extractor over extract; then each revision, the gold rows
    written after it, and the extractor again over each revised document."""
    if state.get("setup_done"):
        return
    ns = namespaces(state, cfg)
    own_extract = 4 in cfg.arms and not cfg.arm4_namespace
    targets = ["kb"] + (["gold"] if 3 in cfg.arms else []) + (["extract"] if own_extract else [])
    for name in targets:
        loom.register(ns[name], f"design-rationale bench: {name}", keep=cfg.keep_namespaces)
    doc_targets = ["kb"] + (["extract"] if own_extract else [])
    reports: dict[str, Any] = state.setdefault("extraction", {})
    for revised in (False, True):
        docs = [d for d in ts.documents.values() if bool(d.revision_of) == revised]
        for d in docs:
            for name in doc_targets:
                loom.upsert_document(ns[name], d.source_uri, d.title, d.content)
        if 3 in cfg.arms:
            for row in ts.gold_writes:
                if row.after_revision == revised:
                    loom.write_gold(ns["gold"], row, gold_pointer(ts, row, ns["kb"]))
        if own_extract:
            for d in docs:
                reports[d.id] = run_extraction(ts, cfg, loom, ledger, ns["extract"], d)
    if own_extract:
        write_label_sheet(cfg, loom, ns["extract"], reports)
    state["setup_done"] = True


def write_label_sheet(cfg: Config, loom: Loom, namespace: str, reports: dict[str, Any]) -> None:
    """`extraction_labels.todo.json`: each extracted row with an empty verdict, for the
    human who judges extraction precision (`score --extraction-labels`)."""
    sheet = []
    for doc_id, r in reports.items():
        for key in r["keys"]:
            got = loom.memory_get(namespace, key)
            if not got.get("found"):
                continue  # retired by a later run over the revised document
            sheet.append(
                {
                    "document": doc_id,
                    "memory_key": key,
                    "value": got.get("value"),
                    "source_resource_id": got.get("source_resource_id"),
                    "verdict": "",
                }
            )
    save_json(cfg.out / "extraction_labels.todo.json", sheet)


def run_extraction(
    ts: TaskSet, cfg: Config, loom: Loom, ledger: Ledger, namespace: str, doc: Any
) -> dict[str, Any]:
    """`loom decisions extract --apply` on one document, its worst-case spend charged to
    the ledger first: the calls run in the server's code, out of the ledger's sight."""
    calls = 2 * max(1, math.ceil(len(doc.content) / cfg.extract_window_chars))
    tin = calls * (cfg.extract_window_chars // 4 + EXTRACT_PROMPT_TOKENS)
    tout = calls * cfg.extract_out_cap
    ledger.check(cfg.extract_model, tin, tout)
    ledger.record("extract", cfg.extract_model, tin, tout, estimated=True)
    rc, report = loom.extract(namespace, doc.source_uri, cfg.extract_repo)
    (cfg.out / f"extract_{doc.id}.log").write_text(report)
    recorded = [
        line.split()[2].rstrip(":")
        for line in report.splitlines()
        if line.startswith(("- recorded ", "- revised ", "- unchanged "))
    ]
    image_only = sum(
        1
        for line in report.splitlines()
        if line.startswith("- image-only, rationale not extracted")
    )
    return {"exit": rc, "keys": recorded, "image_only_sections": image_only}


# ---------------------------------------------------------------- surfaces


def surfaces(ts: TaskSet, cfg: Config, loom: Loom, state: dict[str, Any]) -> dict[str, Any]:
    """What each arm injects for each task, and its size in tokens per surface. K samples
    of a task share its surfaces: they are read once."""
    path = cfg.out / "surfaces.json"
    out = load_json(path)
    ns = namespaces(state, cfg)
    tasks = ts.all_tasks(cfg.only)
    for arm in cfg.arms:
        session = f"{state['prefix']}-arm{arm}"
        home = {2: ns["kb"], 3: ns["gold"], 4: ns["extract"]}.get(arm, "")
        if arm in (3, 4):
            # The reading rule rides on a session's first search with memory hits: warm
            # the session so every task sees what a mid-session search shows.
            for _, t in tasks:
                if memory_section(
                    loom.search(home, t.get("question") or t["task"], session)["text"]
                ):
                    break
        for _kind, t in tasks:
            key = f"{t['id']}/arm{arm}"
            if key in out:
                continue
            query = t.get("question") or t["task"]
            entry: dict[str, Any] = {"context": "(none)", "tokens": {}, "keys": []}
            if arm == 1:
                doc = ts.document_of(t)
                got = loom.knowledge_get(ns["kb"], doc.source_uri)
                text = got.get("content") or ""
                entry["context"] = (
                    f"Document knowledge:{ns['kb']}/{doc.source_uri} ({doc.title}):\n{text}"
                )
                entry["tokens"] = {"document": tokens(text)}
            elif arm == 2:
                found = loom.search(home, query, session)
                section = knowledge_section(found["text"], found["knowledge"])
                entry["context"] = section or "(none)"
                entry["tokens"] = {"search_knowledge": tokens(section)}
            elif arm in (3, 4):
                blocks = [loom.for_file(home, p) for p in ts.task_paths(t)]
                section = memory_section(loom.search(home, query, session)["text"])
                parts = [b["rendered"] for b in blocks if b["rendered"]]
                if section:
                    parts.append("loom_search memory section:\n" + section)
                entry["context"] = "\n\n".join(parts) or "(none)"
                entry["for_file"] = blocks
                entry["tokens"] = {
                    "for_file": [b["tokens"] for b in blocks],
                    "search_memory": tokens(section),
                }
                entry["keys"] = list(
                    dict.fromkeys([k for b in blocks for k in b["keys"]] + section_keys(section))
                )
            out[key] = entry
            save_json(path, out)
    return out


# ---------------------------------------------------------------- answers


def answers(ts: TaskSet, cfg: Config, reader: Model, surf: dict[str, Any]) -> dict[str, Any]:
    path = cfg.out / "answers.json"
    out = load_json(path)
    for sample in range(1, cfg.k + 1):
        for arm in cfg.arms:
            for kind, t in ts.all_tasks(cfg.only):
                key = answer_key(t["id"], arm, sample)
                if key in out:
                    continue
                context = surf[f"{t['id']}/arm{arm}"]["context"]
                system, user, cap = prompts.answer_prompt(kind, t, context, ts.system)
                reply = reader("answer", system, user, cap)
                out[key] = {"text": reply.text, "in": reply.tokens_in, "out": reply.tokens_out}
                save_json(path, out)
    return out


# ---------------------------------------------------------------- judging


class Resolver:
    """Resolves the citations of an answer against the run's own namespaces, once each."""

    def __init__(self, ts: TaskSet, loom: Loom, ns: dict[str, str]):
        self.ts = ts
        self.loom = loom
        self.ns = ns
        self.uris = [d.source_uri for d in ts.documents.values()]
        self.cache: dict[tuple[str, str, str], tuple[bool, str]] = {}

    def resolve(self, cite: dict[str, Any], arm: int) -> tuple[bool, str]:
        rows_ns = {3: self.ns["gold"], 4: self.ns["extract"]}.get(arm, "")
        key = (cite["kind"], cite["cite"], rows_ns)
        if key not in self.cache:
            self.cache[key] = self._resolve(cite, rows_ns)
        return self.cache[key]

    def _resolve(self, cite: dict[str, Any], rows_ns: str) -> tuple[bool, str]:
        if cite["kind"] == "memory":
            if not rows_ns:
                return False, "this arm served no memory rows"
            got = self.loom.memory_get(rows_ns, cite["cite"])
            if not got.get("found"):
                return False, "memory key not found"
            return True, json.dumps(got.get("value"), ensure_ascii=False)[:1500]
        if cite["kind"] == "document":
            uri, _, section = cite["cite"].partition("#")
            ns = self.ns["kb"]
        else:
            parsed = detectors.parse_knowledge(cite["cite"], self.uris)
            if parsed is None:
                return False, "no complete source_uri in the citation"
            ns, uri, section = parsed
            if ns not in self.ns.values():
                return False, "a namespace this run did not write"
        got = self.loom.knowledge_get(ns, uri)
        if not got.get("found") or got.get("superseded"):
            return False, "document not found"
        text = detectors.section_text(got.get("content") or "", section)
        if text is None:
            return False, "section not in the document"
        return True, text


def judge(
    ts: TaskSet,
    cfg: Config,
    judges: list[Model],
    loom: Loom,
    state: dict[str, Any],
    surf: dict[str, Any],
    ans: dict[str, Any],
) -> dict[str, Any]:
    path = cfg.out / "judged.json"
    out = load_json(path)
    resolver = Resolver(ts, loom, namespaces(state, cfg))
    for key, answer in ans.items():
        tid, arm_s, _sample = key.split("/")
        arm = int(arm_s.removeprefix("arm"))
        found = [(kind, t) for kind, t in ts.all_tasks() if t["id"] == tid]
        if not found:
            continue
        kind, t = found[0]
        entry = out.get(key) or {}
        if "citations" not in entry:
            cites = detectors.find_citations(answer["text"], resolver.uris)
            for c in cites:
                c["resolves"], c["text"] = resolver.resolve(c, arm)
            entry["citations"] = cites
            if kind == "B":
                files = detectors.diff_files(detectors.extract_diff(answer["text"]))
                entry["detectors"] = [
                    {"type": d["type"], "fired": detectors.run_detector(d, files)}
                    for d in t["detector"]
                ]
                entry["diff_files"] = sorted(files)
            entry["passes"] = []
        cited = (
            "\n".join(
                f"- {c['cite']}: {'RESOLVES' if c['resolves'] else 'UNRESOLVED'}: {c['text']}"
                for c in entry["citations"]
            )
            or "(no citations)"
        )
        gold_rows = (
            "\n".join(f"- [{k}] {ts.gold[k].value}" for k in t.get("gold_rows", [])) or "(none)"
        )
        rubric = [d["rubric"] for d in t.get("detector", []) if d["type"] == "judge"]
        rubric += t.get("judge_rubric", [])
        context = surf[f"{tid}/arm{arm}"]["context"]
        done = {(p["model"], p["order"], p["repeat"]) for p in entry["passes"]}
        for model in judges:
            for order in range(cfg.judge_orders):
                for repeat in range(cfg.judge_repeats):
                    if (model.name, order, repeat) in done:
                        continue
                    system, user = prompts.judge_prompt(
                        kind,
                        t,
                        answer=answer["text"],
                        context=context,
                        gold_rows=gold_rows,
                        cited=cited,
                        rubric=rubric,
                        answer_first=order == 1,
                    )
                    reply = model("judge", system, user, JUDGE_OUT_CAP)
                    verdict = parse_json(reply.text)
                    entry["passes"].append(
                        {
                            "model": model.name,
                            "order": order,
                            "repeat": repeat,
                            "verdict": verdict,
                            **({} if verdict else {"raw": reply.text[:2000]}),
                        }
                    )
                    out[key] = entry
                    save_json(path, out)
        out[key] = entry
    save_json(path, out)
    return out


def run(
    ts: TaskSet, cfg: Config, loom: Loom, reader: Model, judges: list[Model], ledger: Ledger
) -> None:
    cfg.out.mkdir(parents=True, exist_ok=True)
    state_path = cfg.out / "state.json"
    state = load_json(state_path)
    bind(loom, state, cfg)
    try:
        setup(ts, cfg, loom, ledger, state)
        save_json(state_path, state)
        surf = surfaces(ts, cfg, loom, state)
        ans = answers(ts, cfg, reader, surf)
        judge(ts, cfg, judges, loom, state, surf, ans)
    except CapReached as e:
        print(f"stopped: {e}")
    finally:
        save_json(state_path, state)
        if not cfg.keep_namespaces and state.get("setup_done"):
            ns = namespaces(state, cfg)
            loom.erase([v for k, v in ns.items() if not (k == "extract" and cfg.arm4_namespace)])
            # A resumed run fills the same names again; --keep-namespaces avoids that.
            state["setup_done"] = False
            save_json(state_path, state)
