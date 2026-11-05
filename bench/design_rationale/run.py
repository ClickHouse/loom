"""The `run` command: set up the arms in the harness's Loom, read the surfaces, answer
each task K times per arm, and judge each answer. Every stage writes its JSON file in
the output directory and skips what is already there, so a run stopped by the spend
cap, or that fails or is interrupted after its setup, resumes where it stopped; its
namespaces are kept until every answer is judged.
"""

from __future__ import annotations

import json
import secrets
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from bench.bench_namespaces import gc_command
from bench.design_rationale import detectors, prompts
from bench.design_rationale.llm import (
    CHARS_PER_TOKEN_WORST,
    CapReached,
    Ledger,
    Model,
    cost_usd,
    parse_json,
    price,
    write_atomic,
)
from bench.design_rationale.loom import (
    KNOWLEDGE_HITS_SHOWN,
    PRE_EDIT_BUDGET,
    CodeIndex,
    Loom,
    knowledge_section,
    memory_section,
    section_keys,
    tokens,
)
from bench.design_rationale.score import planning_mde
from bench.design_rationale.taskset import (
    KINDS,
    Document,
    TaskSet,
    answer_key,
    parse_key,
    surface_key,
)

ARMS = (0, 1, 2, 3, 4, 5, 6)
ARM_NAMES = {
    0: "no design knowledge",
    1: "whole document through knowledge.get",
    2: "document as knowledge, through loom_search",
    3: "gold rows through the pre-edit block and loom_search",
    4: "extracted rows through the pre-edit block and loom_search",
    5: "gold rows through loom_search only",
    6: "extracted rows and the documents' knowledge hits through the pre-edit block and "
    "loom_search",
}
# The namespace each arm's rows or documents are read from. The extract namespace holds
# the documents too, so arm 6's one search returns both its rows and its knowledge hits.
ARM_HOME = {2: "kb", 3: "gold", 4: "extract", 5: "gold", 6: "extract"}
# The arms served extracted rows, and the arms whose loom_search answer an agent reads
# for its memory section.
EXTRACTED_ARMS = (4, 6)
MEMORY_ARMS = (3, 4, 5, 6)
# Planning estimates for a dry run, before any surface is read.
SEARCH_SECTION_TOKENS = 800  # a loom_search section with the reading rule
CITED_TEXT_TOKENS = 1200  # the cited source text a judge reads
JUDGE_FRAME_TOKENS = 900  # the judge's instructions and gold lists
TYPICAL_OUT = {"A": 400, "B": 900, "C": 400, "D": 500, "judge": 500, "extract": 800}
JUDGE_OUT_CAP = 4000
# The left-out line of a pre-edit block, outside both of its budgets; the render test
# keeps it honest.
PRE_EDIT_FIXED_TOKENS = 300
# The longest category and scenario decision.record accepts
# (server/memory/api/models_decision.py, DecisionRecordInput).
DECISION_CATEGORY_MAX_CHARS = 120
DECISION_SCENARIO_MAX_CHARS = 2000
# Decisions kept per section, which the extractor's prompt states: the run uses the
# default of `loom decisions extract --max-per-section` (server/cli/decisions.py).
EXTRACT_MAX_PER_SECTION = 3
# Characters per extraction window, the CLI's `--window-chars` default.
EXTRACT_WINDOW_CHARS = 6000


class RunError(RuntimeError):
    pass


@dataclass
class Config:
    out: Path
    arms: list[int]
    org: str = "bench"
    k: int = 4
    reader: str = ""
    judges: list[str] = field(default_factory=list)
    judge_orders: int = 2
    judge_repeats: int = 2
    max_usd: float = 0.0
    only: set[str] = field(default_factory=set)
    keep_namespaces: bool = False
    arm4_namespace: str = ""
    # Arm 4's rows carry files (a code index linked them, or a sync's rows are served):
    # it reads the pre-edit block, and so does arm 6. Without, both are served through
    # loom_search only.
    arm4_files: bool = True
    extract_model: str = ""
    extract_window_chars: int = EXTRACT_WINDOW_CHARS


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text()) if path.exists() else {}


def save_json(path: Path, obj: Any) -> None:
    write_atomic(path, json.dumps(obj, indent=1, ensure_ascii=False))


def pre_edit_block_tokens() -> int:
    """The planned size of one pre-edit block: decisions and constraints print whole up
    to `memory_for_file_whole_tokens`, the other rows up to the block's budget, and the
    left-out line naming the decisions past the ceiling (`PRE_EDIT_FIXED_TOKENS`)."""
    from server.settings import settings

    return settings.memory_for_file_whole_tokens + PRE_EDIT_BUDGET + PRE_EDIT_FIXED_TOKENS


def own_extraction(cfg: Config) -> bool:
    return bool(set(EXTRACTED_ARMS) & set(cfg.arms)) and not cfg.arm4_namespace


def pre_edit(cfg: Config, arm: int) -> bool:
    """Whether the arm reads the pre-edit block."""
    return arm == 3 or (arm in EXTRACTED_ARMS and cfg.arm4_files)


# ---------------------------------------------------------------- extraction bound


def longest_known_decision() -> tuple[str, str, str]:
    """The longest (category, scenario, outcome) a re-run's prompt can list: the most
    decision.record accepts for the category and the scenario, and the outcome at the
    length the prompt cuts it to."""
    from server.memory.domain import decision_extract as dx

    return (
        "x" * DECISION_CATEGORY_MAX_CHARS,
        "x" * DECISION_SCENARIO_MAX_CHARS,
        "x" * dx._KNOWN_OUTCOME_CHARS,
    )


def extraction_bound(doc: Document, window_chars: int) -> tuple[int, int, int]:
    """(calls, input tokens, output tokens) one extraction run on `doc` may spend at
    most. Two calls per window: a reply with no decision, or with a reason that is not
    one span of the text, is read again. Each call carries the system prompt (with the
    known decisions a re-run over a revision lists) and the window as the extractor
    sends it, its `[document: ...]` and `[section: ...]` lines included (`user_prompt`).
    The second call may also carry the note listing the first reply's failed spans,
    which the first reply wrote, so it is bounded by the output cap. Each call writes up
    to that cap."""
    from server.knowledge.app.writes import _chunk_text
    from server.memory.app.decision_extract import _windows
    from server.memory.domain import decision_extract as dx
    from server.memory.domain.corpus_synthesis import CorpusChunk
    from server.settings import settings
    from server.settings.budgets import llm_max_tokens

    # The document as knowledge.upsert chunks it and the extractor groups it. Sections
    # it skips as image-only only make the real count smaller.
    chunks = [
        CorpusChunk(doc.source_uri, c.idx, c.text, c.section_path) for c in _chunk_text(doc.content)
    ]
    windows = _windows(chunks, set(), window_chars)
    known = settings.memory_decision_extract_known_max if doc.revision_of else 0
    system = len(dx.system_prompt(EXTRACT_MAX_PER_SECTION, [longest_known_decision()] * known))
    cap = llm_max_tokens("decision_extract")
    tin = 0
    for w in windows:
        one = (system + len(dx.user_prompt(w, doc.title))) // CHARS_PER_TOKEN_WORST
        tin += 2 * one + cap
    calls = 2 * len(windows)
    return calls, tin, calls * cap


# ---------------------------------------------------------------- plan (dry run)


def _priced(models: list[str], tin: int, tout: int) -> float | None:
    """USD of calls spread evenly over `models`; None when one has no price."""
    if not models or not all(price(m) for m in models):
        return None
    return round(sum(cost_usd(m, tin, tout) for m in models) / len(models), 4)


def plan(ts: TaskSet, cfg: Config) -> dict[str, Any]:
    """The calls and tokens a run would spend, at every output cap and typical (outputs
    at `TYPICAL_OUT`). Inputs are estimates either way: the documents at three
    characters a token, the surfaces at planning constants, as no surface has been read
    yet."""
    tasks = ts.all_tasks(cfg.only)
    passes = max(1, len(cfg.judges)) * cfg.judge_orders * cfg.judge_repeats
    rows: list[dict[str, Any]] = []
    for arm in cfg.arms:
        for kind, t in tasks:
            if arm == 1:
                ctx = len(ts.document_of(t).content) // CHARS_PER_TOKEN_WORST
            elif arm in (2, 5):
                ctx = SEARCH_SECTION_TOKENS
            elif arm in (3, 4, 6):
                blocks = len(ts.task_paths(t)) if pre_edit(cfg, arm) else 0
                sections = 2 if arm == 6 else 1  # arm 6 reads the knowledge hits too
                ctx = SEARCH_SECTION_TOKENS * sections + pre_edit_block_tokens() * blocks
            else:
                ctx = 0
            system, user, cap = prompts.answer_prompt(kind, t, "", ts.system)
            a_in = len(system + user) // CHARS_PER_TOKEN_WORST + ctx
            j_in = JUDGE_FRAME_TOKENS + cap + CITED_TEXT_TOKENS
            rows.append({"kind": kind, "answer_in": a_in, "answer_cap": cap, "judge_in": j_in})
    k = cfg.k
    answer_calls = len(rows) * k
    judge_calls = answer_calls * passes
    bounds = (
        [extraction_bound(d, cfg.extract_window_chars) for d in ts.documents.values()]
        if own_extraction(cfg)
        else []
    )
    n_extract = sum(b[0] for b in bounds)
    usage = {
        "answer": {
            "model": cfg.reader,
            "calls": answer_calls,
            "in": sum(r["answer_in"] for r in rows) * k,
            "out_at_caps": sum(r["answer_cap"] for r in rows) * k,
            "out_typical": sum(TYPICAL_OUT[r["kind"]] for r in rows) * k,
        },
        "judge": {
            "model": ",".join(cfg.judges),
            "calls": judge_calls,
            "in": sum(r["judge_in"] for r in rows) * k * passes,
            "out_at_caps": JUDGE_OUT_CAP * judge_calls,
            "out_typical": TYPICAL_OUT["judge"] * judge_calls,
        },
        "extract": {
            "model": cfg.extract_model,
            "calls": n_extract,
            "in": sum(b[1] for b in bounds),
            "out_at_caps": sum(b[2] for b in bounds),
            "out_typical": n_extract * TYPICAL_OUT["extract"],
        },
    }
    models = {"answer": [cfg.reader], "judge": cfg.judges, "extract": [cfg.extract_model]}
    for stage, u in usage.items():
        if not u["calls"]:
            u["usd_at_caps"] = u["usd_typical"] = 0.0
            continue
        u["usd_at_caps"] = _priced(models[stage], u["in"], u["out_at_caps"])
        u["usd_typical"] = _priced(models[stage], u["in"], u["out_typical"])
    counts = {kind: sum(1 for kk, _ in tasks if kk == kind) for kind in KINDS}
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
    counts = ", ".join(f"{kind} {n}" for kind, n in p["tasks"].items())
    print(
        f"tasks: {counts} over {p['documents']} document(s); K = {p['k']}; "
        f"{p['judge_passes_per_answer']} judge pass(es) per answer"
    )
    total_caps = total_typical = 0.0
    for stage, u in p["usage"].items():
        usd = (
            f"{u['usd_typical']:.2f} USD typical, {u['usd_at_caps']:.2f} USD at every output cap"
            if u["usd_at_caps"] is not None
            else "unpriced model"
        )
        print(
            f"{stage}: {u['calls']} call(s) on {u['model'] or '-'}, {u['in']:,} tokens in, "
            f"{u['out_typical']:,} out typical ({u['out_at_caps']:,} at every output cap); "
            f"{usd}"
        )
        # An unpriced stage adds nothing: its line says so.
        total_caps += u["usd_at_caps"] or 0.0
        total_typical += u["usd_typical"] or 0.0
    print(f"total: {total_typical:.2f} USD typical, {total_caps:.2f} USD at every output cap")
    print(
        "planning MDE (paired, 80% power, two-sided 5%): "
        + ", ".join(f"{k} {v * 100:.0f} points" for k, v in p["planning_mde"].items())
    )


# ---------------------------------------------------------------- namespaces


def namespaces(state: dict[str, Any], cfg: Config) -> dict[str, str]:
    prefix = state.setdefault("prefix", "dr-" + secrets.token_hex(3))
    return {
        "kb": f"{prefix}-kb",
        "gold": f"{prefix}-gold",
        "extract": cfg.arm4_namespace or f"{prefix}-extract",
    }


def orgs(state: dict[str, Any], cfg: Config) -> dict[str, str]:
    """The org of each role's namespace, the run's own: a search and the cost readback
    read a whole org, so a namespace another run left behind must not share one. Arm 4
    served from a sync's rows keeps the org they were written in."""
    prefix = state["prefix"]
    out = {role: f"{cfg.org}-{prefix}-{role}" for role in ("kb", "gold", "extract")}
    if cfg.arm4_namespace:
        out["extract"] = cfg.org
    return out


def bind(loom: Loom, state: dict[str, Any], cfg: Config) -> dict[str, str]:
    ns = namespaces(state, cfg)
    for role, org in orgs(state, cfg).items():
        loom.bind(ns[role], org)
    return ns


def owned(cfg: Config, ns: dict[str, str]) -> list[str]:
    """The namespaces this run creates, and erases."""
    roles = ["kb"] + (["gold"] if {3, 5} & set(cfg.arms) else [])
    roles += ["extract"] if own_extraction(cfg) else []
    return [ns[r] for r in roles]


def record_server_spend(loom: Loom, ledger: Ledger, state: dict[str, Any], cfg: Config) -> None:
    """Charge the ledger with what the server spent in the run's orgs since the last
    read: embeddings and any model call its writes and reads made."""
    # A sync's namespace (--arm4-namespace) is the deployment's own, not the run's.
    since = date.fromisoformat(state["started"])
    spent = loom.server_spend(owned(cfg, namespaces(state, cfg)), since)
    seen = state.get("server_usd_seen", 0.0)
    if spent > seen:
        ledger.record_usd("server", round(spent - seen, 6))
        state["server_usd_seen"] = spent


# ---------------------------------------------------------------- setup


def gold_pointer(ts: TaskSet, row: Any, kb: str) -> str:
    uri = ts.documents[row.document].source_uri
    return f"knowledge:{kb}/{uri}" + (f"#{row.section}" if row.section else "")


def setup(
    ts: TaskSet,
    cfg: Config,
    loom: Loom,
    ledger: Ledger,
    state: dict[str, Any],
    code: CodeIndex | None,
) -> None:
    """Fill the arms' namespaces: documents into kb (and extract), gold rows into gold
    (never into extract), the extractor over extract; then each revision, the gold rows
    written after it, and the extractor again over each revised document. Each step is
    recorded in `state` as it completes, so a setup the spend cap stopped resumes at the
    step it stopped on, in the same order."""
    if state.get("setup_done"):
        return
    ns = namespaces(state, cfg)
    done: list[str] = state.setdefault("setup_steps", [])

    def step(name: str) -> bool:
        return name not in done

    if step("register"):
        for name in owned(cfg, ns):
            loom.register(name, "design-rationale bench", cap_usd=cfg.max_usd)
        record_server_spend(loom, ledger, state, cfg)
        done.append("register")
    doc_targets = ["kb"] + (["extract"] if own_extraction(cfg) else [])
    reports: dict[str, Any] = state.setdefault("extraction", {})
    for revised in (False, True):
        docs = [d for d in ts.documents.values() if bool(d.revision_of) == revised]
        for d in docs:
            if step(f"upsert:{d.id}"):
                for name in doc_targets:
                    loom.upsert_document(ns[name], d.source_uri, d.title, d.content)
                done.append(f"upsert:{d.id}")
        if {3, 5} & set(cfg.arms) and step(f"gold:{revised}"):
            for row in ts.gold_writes:
                if row.after_revision == revised:
                    loom.write_gold(ns["gold"], row, gold_pointer(ts, row, ns["kb"]))
            done.append(f"gold:{revised}")
        if own_extraction(cfg):
            for d in docs:
                if step(f"extract:{d.id}"):
                    reports[d.id] = run_extraction(cfg, loom, ledger, ns["extract"], d, code)
                    done.append(f"extract:{d.id}")
    if own_extraction(cfg):
        write_label_sheet(cfg, loom, ns["extract"], reports)
    record_server_spend(loom, ledger, state, cfg)
    state["setup_done"] = True


def parse_report(report: str) -> dict[str, list[str]]:
    """Each kept row's key and the code names its signal lines list, from the
    extractor's printed report: `- recorded <key>`, then one indented line per name,
    `signal: <name>: <signal>, ...`."""
    rows: dict[str, list[str]] = {}
    current = None
    for line in report.splitlines():
        if line.startswith(("- recorded ", "- revised ", "- unchanged ")):
            current = line.split()[2].rstrip(":")
            rows.setdefault(current, [])
        elif line.startswith("    signal: ") and current is not None:
            name = line.removeprefix("    signal: ").partition(": ")[0]
            if name and name != "no code names":
                rows[current].append(name)
        elif not line.startswith("    "):
            current = None
    return rows


def link_files(code: CodeIndex, names: list[str]) -> tuple[list[str], list[str]]:
    """(files, names the index did not answer for): the files the extractor would link a
    row to on a repository's index, each name with exactly one defining file that a
    decision can govern (not a test, not a page)."""
    from server.memory.domain import decision_extract as dx

    files: list[str] = []
    failed: list[str] = []
    for name in names:
        definitions = code.definitions(name)
        if definitions is None:
            failed.append(name)
            continue
        path = dx.definition_path(definitions)
        if path and dx.governs(path) and path not in files:
            files.append(path)
    return files, failed


def run_extraction(
    cfg: Config, loom: Loom, ledger: Ledger, namespace: str, doc: Document, code: CodeIndex | None
) -> dict[str, Any]:
    """`loom decisions extract --apply` on one document, its worst-case spend charged to
    the ledger first: the calls run in the server's code, out of the ledger's sight.
    With a code index, each kept row's code names are resolved there and the files
    written on it, so arm 4 reaches the pre-edit block as arm 3 does; a name the index
    did not answer for is listed in `lookup_failures`."""
    calls, tin, tout = extraction_bound(doc, cfg.extract_window_chars)
    ledger.check(cfg.extract_model, tin, tout)
    ledger.record("extract", cfg.extract_model, tin, tout, estimated=True)
    rc, report = loom.extract(namespace, doc.source_uri, cfg.extract_window_chars)
    (cfg.out / f"extract_{doc.id}.log").write_text(report)
    rows = parse_report(report)
    linked: dict[str, list[str]] = {}
    failures: list[str] = []
    if code is not None:
        reasoning = loom.decision_reasoning(namespace) if rows else {}
        for key, names in rows.items():
            files, failed = link_files(code, names)
            failures += failed
            if files:
                written = loom.set_files(namespace, key, files, reasoning.get(key, ""))
                if written:
                    linked[key] = written
    image_only = sum(
        1
        for line in report.splitlines()
        if line.startswith("- image-only, rationale not extracted")
    )
    return {
        "exit": rc,
        "keys": list(rows),
        "linked": linked,
        "lookup_failures": failures,
        "image_only_sections": image_only,
    }


def write_label_sheet(cfg: Config, loom: Loom, namespace: str, reports: dict[str, Any]) -> None:
    """`extraction_labels.todo.json`: each extracted row once, with an empty verdict,
    for the human who judges extraction precision (`score --extraction-labels`)."""
    sheet: dict[str, dict[str, Any]] = {}
    for doc_id, r in reports.items():
        for key in r["keys"]:
            got = loom.memory_get(namespace, key)
            if not got["found"]:
                continue  # retired by a later run over the revised document
            sheet[key] = {
                "document": doc_id,
                "memory_key": key,
                "value": got["value"],
                "source_resource_id": got["source_resource_id"],
                "verdict": "",
            }
    save_json(cfg.out / "extraction_labels.todo.json", list(sheet.values()))


# ---------------------------------------------------------------- surfaces


def surfaces(ts: TaskSet, cfg: Config, loom: Loom, state: dict[str, Any]) -> dict[str, Any]:
    """What each arm injects for each task, and its size in tokens per surface. K
    samples of a task share its surfaces: they are read once."""
    path = cfg.out / "surfaces.json"
    out = load_json(path)
    ns = namespaces(state, cfg)
    tasks = ts.all_tasks(cfg.only)
    for arm in cfg.arms:
        session = f"{state['prefix']}-arm{arm}"
        home = ns[ARM_HOME[arm]] if arm in ARM_HOME else ""
        missing = any(surface_key(t["id"], arm) not in out for _, t in tasks)
        if arm in MEMORY_ARMS and missing:
            # The reading rule rides on a session's first search with memory hits: warm
            # the session so every task sees what a mid-session search shows.
            for _, t in tasks:
                if memory_section(
                    loom.search(home, t.get("question") or t["task"], session)["text"]
                ):
                    break
        for _kind, t in tasks:
            key = surface_key(t["id"], arm)
            if key in out:
                continue
            query = t.get("question") or t["task"]
            entry: dict[str, Any] = {"context": "(none)", "tokens": {}, "keys": []}
            if arm == 1:
                doc = ts.document_of(t)
                got = loom.knowledge_get(ns["kb"], doc.source_uri)
                text = got["content"] or ""
                entry["context"] = (
                    f"Document knowledge:{ns['kb']}/{doc.source_uri} ({doc.title}):\n{text}"
                )
                entry["tokens"] = {"document": tokens(text)}
            elif arm == 2:
                found = loom.search(home, query, session)
                section = knowledge_section(found["text"], found["knowledge"])
                entry["context"] = section or "(none)"
                entry["tokens"] = {"search_knowledge": tokens(section)}
                entry["knowledge_uris"] = shown_uris(found["knowledge"])
            elif arm in MEMORY_ARMS:
                paths = ts.task_paths(t) if pre_edit(cfg, arm) else []
                blocks = [loom.for_file(home, p) for p in paths]
                found = loom.search(home, query, session)
                section = memory_section(found["text"])
                parts = [b["rendered"] for b in blocks if b["rendered"]]
                if section:
                    parts.append("loom_search memory section:\n" + section)
                entry["tokens"] = {
                    "for_file": [b["tokens"] for b in blocks],
                    "search_memory": tokens(section),
                }
                if arm == 6:
                    knowledge = knowledge_section(found["text"], found["knowledge"])
                    if knowledge:
                        parts.append("loom_search knowledge section:\n" + knowledge)
                    entry["tokens"]["search_knowledge"] = tokens(knowledge)
                    entry["knowledge_uris"] = shown_uris(found["knowledge"])
                entry["context"] = "\n\n".join(parts) or "(none)"
                entry["for_file"] = blocks
                entry["for_file_keys"] = list(dict.fromkeys(k for b in blocks for k in b["keys"]))
                entry["search_keys"] = section_keys(section)
                entry["keys"] = list(dict.fromkeys(entry["for_file_keys"] + entry["search_keys"]))
            out[key] = entry
            save_json(path, out)
    return out


def shown_uris(results: list[dict[str, Any]]) -> list[str]:
    """The documents of the knowledge hits an agent reads, in order, once each."""
    return list(dict.fromkeys(h["source_uri"] for h in results[:KNOWLEDGE_HITS_SHOWN]))


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
                context = surf[surface_key(t["id"], arm)]["context"]
                system, user, cap = prompts.answer_prompt(kind, t, context, ts.system)
                reply = reader("answer", system, user, cap)
                out[key] = {"text": reply.text, "in": reply.tokens_in, "out": reply.tokens_out}
                save_json(path, out)
    return out


# ---------------------------------------------------------------- judging


class Resolver:
    """Resolves the citations of an answer against the run's own namespaces, once
    each."""

    def __init__(self, ts: TaskSet, loom: Loom, ns: dict[str, str]):
        self.ts = ts
        self.loom = loom
        self.ns = ns
        self.uris = [d.source_uri for d in ts.documents.values()]
        self.cache: dict[tuple[str, str, str], tuple[bool, str]] = {}

    def resolve(self, cite: dict[str, Any], arm: int) -> tuple[bool, str]:
        rows_ns = self.ns[ARM_HOME[arm]] if arm in MEMORY_ARMS else ""
        key = (cite["kind"], cite["cite"], rows_ns)
        if key not in self.cache:
            self.cache[key] = self._resolve(cite, rows_ns)
        return self.cache[key]

    def _resolve(self, cite: dict[str, Any], rows_ns: str) -> tuple[bool, str]:
        if cite["kind"] == "memory":
            if not rows_ns:
                return False, "this arm served no memory rows"
            got = self.loom.memory_get(rows_ns, cite["cite"])
            if not got["found"]:
                return False, "memory key not found"
            return True, json.dumps(got["value"], ensure_ascii=False)[: detectors.CITED_TEXT_CHARS]
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
        # knowledge.get reads the live version only: a deleted document is not found.
        if not got["found"]:
            return False, "document not found"
        text = detectors.section_text(got["content"] or "", section)
        if text is None:
            return False, "section not in the document"
        return True, text


def judge(
    ts: TaskSet,
    cfg: Config,
    judges: list[Model],
    loom: Loom,
    state: dict[str, Any],
    ans: dict[str, Any],
) -> dict[str, Any]:
    path = cfg.out / "judged.json"
    out = load_json(path)
    resolver = Resolver(ts, loom, namespaces(state, cfg))
    tasks = {t["id"]: (kind, t) for kind, t in ts.all_tasks()}
    for key, answer in ans.items():
        tid, arm = parse_key(key)
        if tid not in tasks:
            continue
        kind, t = tasks[tid]
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


# The flags a resumed run must share with the run it resumes: they decide what the
# namespaces hold and what the stored surfaces mean.
RESUME_FLAGS = (
    "org",
    "arms",
    "arm4_namespace",
    "arm4_files",
    "extract_window_chars",
    "reader",
    "judges",
    "extract_model",
)


def check_resume(state: dict[str, Any], cfg: Config) -> None:
    flags = {name: getattr(cfg, name) for name in RESUME_FLAGS}
    flags["arms"] = sorted(cfg.arms)
    stored = state.setdefault("flags", flags)
    if stored != flags:
        changed = ", ".join(k for k in RESUME_FLAGS if stored.get(k) != flags[k])
        raise RunError(f"{cfg.out} was run with other {changed}; resume it with the same flags")


def erase_command(cfg: Config, loom: Loom, ns: dict[str, str]) -> str:
    """The `loom namespaces-gc` commands that erase what the run keeps."""
    by_org: dict[str, list[str]] = {}
    for name in owned(cfg, ns):
        by_org.setdefault(loom.orgs[name], []).append(name)
    return "; ".join(gc_command(org, names) for org, names in sorted(by_org.items()))


def run(
    ts: TaskSet,
    cfg: Config,
    loom: Loom,
    reader: Model,
    judges: list[Model],
    ledger: Ledger,
    code: CodeIndex | None = None,
) -> bool:
    """One pass of every stage; True when every answer is judged. A run the cap stops,
    or that fails or is interrupted after its setup (a provider error, a server that
    went away, Ctrl-C), keeps its namespaces: the next invocation on the same `--out`,
    with the same flags, resumes it, and the error is followed by the command that
    erases them otherwise. A setup that fails or is interrupted other than by the cap
    leaves half-filled namespaces: they are erased (kept with --keep-namespaces, with the
    erase command printed), and the run is never resumed. A finished run erases them and
    is not resumed; with --keep-namespaces it keeps them, and a rerun finds every stage
    done."""
    cfg.out.mkdir(parents=True, exist_ok=True)
    state_path = cfg.out / "state.json"
    state = load_json(state_path)
    # Refuse other flags before anything else acts on the namespaces, erasure included.
    check_resume(state, cfg)
    if state.get("setup_failed"):
        ns = bind(loom, state, cfg)
        # An erasure refused when the setup failed is tried again.
        if not state.get("erased") and not cfg.keep_namespaces and loom.erase(owned(cfg, ns)):
            state["erased"] = True
            save_json(state_path, state)
        gone = (
            "its namespaces are erased"
            if state.get("erased")
            else f"erase its namespaces with: {erase_command(cfg, loom, ns)}"
        )
        raise RunError(
            f"{cfg.out}: this run's setup failed ({gone}); start anew with another --out"
        )
    if state.get("erased"):
        raise RunError(f"{cfg.out}: this run's namespaces were erased; score it or start anew")
    state.setdefault("started", date.today().isoformat())
    ns = bind(loom, state, cfg)
    finished = False
    try:
        try:
            setup(ts, cfg, loom, ledger, state, code)
        except CapReached:
            raise  # its steps so far stay; the resume continues the setup
        except BaseException:
            # Half-filled namespaces never serve a resumed run, kept or not.
            state["setup_failed"] = True
            if cfg.keep_namespaces:
                print(
                    f"setup failed; erase what it wrote with: {erase_command(cfg, loom, ns)}",
                    file=sys.stderr,
                )
            elif loom.erase(owned(cfg, ns)):
                state["erased"] = True
            raise
        save_json(state_path, state)
        surf = surfaces(ts, cfg, loom, state)
        record_server_spend(loom, ledger, state, cfg)
        ans = answers(ts, cfg, reader, surf)
        judge(ts, cfg, judges, loom, state, ans)
        record_server_spend(loom, ledger, state, cfg)
        finished = True
    except CapReached as e:
        print(
            f"stopped: {e}; run again with the same --out (and a higher --max-usd) to "
            f"resume, or erase the namespaces with: {erase_command(cfg, loom, ns)}",
            file=sys.stderr,
        )
    except BaseException:
        # An error or an interrupt after the setup: what the run wrote is kept.
        if not state.get("erased") and not state.get("setup_failed"):
            print(
                "stopped; the namespaces are kept: run the same command again to resume, "
                f"or erase them with: {erase_command(cfg, loom, ns)}",
                file=sys.stderr,
            )
        raise
    finally:
        # A refused erasure leaves the run unerased: the next invocation tries again.
        if finished and not cfg.keep_namespaces and loom.erase(owned(cfg, ns)):
            state["erased"] = True
        save_json(state_path, state)
    return finished
