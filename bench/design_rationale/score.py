"""The `score` command: metrics per task and arm from a run's JSON files, the paired
comparisons between arms with a bootstrap over documents, the minimum detectable
effects, judge stability, judge accuracy against a human-labelled slice, and extraction
precision from a human-judged file. No model call and no Loom call: a re-score is free.

Per answer, each judge pass yields its metrics and the passes are averaged; per task the
K samples are averaged; an arm's figure is the mean over tasks. A comparison is paired
per task and resamples whole documents (the clusters), so its interval is too narrow
when there are few documents: read `clusters` beside it.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from pathlib import Path
from statistics import mean
from typing import Any

from bench.bootstrap import paired_bootstrap
from bench.design_rationale.llm import cost_usd
from bench.design_rationale.taskset import TaskSet, parse_key, surface_key

# z(0.975) + z(0.80): the effect a two-sided 5% test detects with 80% power is this many
# standard errors.
MDE_Z = 1.96 + 0.84
# The standard deviation of a per-task paired delta assumed for a planning MDE, before a
# run has a spread of its own: a pass/fail outcome averaged over K samples.
PLANNING_SD = 0.35
# (treatment arm, reference arm) pairs reported when both ran. Arm 4 against arm 5 is
# the like-for-like comparison when arm 4 had no code index (no pre-edit block).
COMPARISONS = ((3, 0), (4, 0), (4, 3), (4, 5), (1, 0), (1, 3), (2, 3))
# The keys a verdict must carry to be read; one without them is unreadable, not a pass.
REQUIRED = {
    "A": ("claims", "gold_covered", "potentially_misleading"),
    "B": ("forbidden_proposed", "constraints_violated"),
    "C": ("stale_served", "abstained", "invented_rationale"),
}
# The verdict fields read as lists, at the top level and in each claim.
LIST_FIELDS = ("cites", "cites_support")
# The verdict fields read as lists at the top level only.
TOP_LIST_FIELDS = ("gold_covered",)
# The per-task metric each kind's comparison is made on, higher is better.
HEADLINE = {
    "A": ("precision", "recall"),
    "B": ("avoids", "satisfies"),
    "C": ("passed",),
}
# The change-task fields a fired detector decides: a human label on them says nothing about
# the judge. `detector_fired` is never the judge's at all.
DETECTOR_DECIDED = ("avoids", "satisfies")


def planning_mde(n_tasks: int) -> float:
    return MDE_Z * PLANNING_SD / math.sqrt(n_tasks) if n_tasks else float("nan")


def readable(kind: str, task: dict[str, Any], verdict: Any) -> bool:
    need = REQUIRED[kind]
    if kind == "C" and task.get("subtype") == "supersession":
        need = (*need, "current_stated")
    if not isinstance(verdict, dict) or not all(k in verdict for k in need):
        return False
    # A judge that returns a bool where a list goes would otherwise break the scoring.
    claims = verdict.get("claims") or []
    if not isinstance(claims, list) or not all(isinstance(c, dict) for c in claims):
        return False
    for part in (verdict, *claims):
        if any(not isinstance(part.get(f) or [], list) for f in LIST_FIELDS):
            return False
    if any(not isinstance(verdict.get(f) or [], list) for f in TOP_LIST_FIELDS):
        return False
    # A rubric the judge left unanswered would pass the task.
    rubric = any(d["type"] == "judge" for d in task.get("detector", []))
    return not (kind == "B" and rubric and not isinstance(verdict.get("rubric_fail"), bool))


# ---------------------------------------------------------------- one judge pass


def _supported(entry: dict[str, Any], verdict: dict[str, Any]) -> dict[str, bool]:
    """citation -> whether a judge said its text supports what it was cited for."""
    pairs: list[tuple[str, Any]] = []
    for claim in verdict.get("claims") or []:
        pairs += list(zip(claim.get("cites") or [], claim.get("cites_support") or [], strict=False))
    pairs += list(zip(verdict.get("cites") or [], verdict.get("cites_support") or [], strict=False))
    support: dict[str, bool] = {}
    known = {c["cite"] for c in entry["citations"]}
    for cited, ok in pairs:
        cite = str(cited).strip().strip("[]`()")
        if cite in known:
            support[cite] = support.get(cite, False) or bool(ok)
    return support


def _detector_fired(entry: dict[str, Any]) -> bool:
    """Whether a deterministic detector fired on a change answer (a judge detector
    records None, never True)."""
    return any(d["fired"] for d in entry.get("detectors", []))


def pass_metrics(
    kind: str, task: dict[str, Any], entry: dict[str, Any], verdict: dict[str, Any]
) -> dict[str, float | None]:
    """The metrics of one answer under one judge pass."""
    support = _supported(entry, verdict)
    resolving = {c["cite"] for c in entry["citations"] if c["resolves"]}
    good = {c for c, ok in support.items() if ok and c in resolving}
    m: dict[str, float | None] = {
        "citations": len(entry["citations"]),
        "citations_good": len(good),
    }
    if kind == "A":
        claims = verdict.get("claims") or []
        correct = sum(c.get("verdict") == "correct" for c in claims)
        gold_n = len(task["gold_arguments"])
        covered = {
            i for i in verdict.get("gold_covered") or [] if isinstance(i, int) and 0 <= i < gold_n
        }
        # A claim counts as cited when one of its own citations resolves and supports it.
        cited_ok = sum(
            any(
                ok is True and str(c).strip().strip("[]`()") in resolving
                for c, ok in zip(
                    claim.get("cites") or [], claim.get("cites_support") or [], strict=False
                )
            )
            for claim in claims
        )
        m.update(
            {
                "claims": len(claims),
                "correct": correct,
                "precision": correct / len(claims) if claims else None,
                "recall": len(covered) / gold_n if gold_n else None,
                "misleading": float(bool(verdict.get("potentially_misleading"))),
                "abstained": float(bool(verdict.get("abstained"))),
                "claims_cited": cited_ok,
            }
        )
    elif kind == "B":
        fired = _detector_fired(entry)
        rubric_fail = verdict.get("rubric_fail") is True
        reproposes = fired or bool(verdict.get("forbidden_proposed")) or rubric_fail
        violates = fired or rubric_fail or bool(verdict.get("constraints_violated"))
        m.update(
            {
                "avoids": float(not reproposes),
                "satisfies": float(not violates),
                "pushed_back": float(bool(verdict.get("pushed_back"))),
                "detector_fired": float(fired),
            }
        )
    else:
        if task["subtype"] == "supersession":
            passed = not verdict.get("stale_served")
            m["stale"] = float(not passed)
            m["current_stated"] = float(bool(verdict.get("current_stated")))
        else:
            passed = bool(verdict.get("abstained")) and not verdict.get("invented_rationale")
            m["abstention_failed"] = float(not passed)
        m["passed"] = float(passed)
    return m


def _avg(rows: list[dict[str, float | None]]) -> dict[str, float | None]:
    keys = {k for r in rows for k in r}
    out: dict[str, float | None] = {}
    for k in keys:
        vals = [v for r in rows if (v := r.get(k)) is not None]
        out[k] = mean(vals) if vals else None
    return out


def answer_metrics(
    kind: str, task: dict[str, Any], entry: dict[str, Any]
) -> dict[str, float | None]:
    """One answer's metrics: the mean over its readable judge passes."""
    rows = [
        pass_metrics(kind, task, entry, p["verdict"])
        for p in entry.get("passes", [])
        if readable(kind, task, p.get("verdict"))
    ]
    return _avg(rows) if rows else {}


def unreadable(ts: TaskSet, judged: dict[str, Any]) -> dict[int, dict[str, int]]:
    """arm -> {"passes", "unreadable", "answers_unscored"}: judge passes whose verdict
    lacks its kind's keys, and answers left with no readable pass."""
    tasks = {t["id"]: (kind, t) for kind, t in ts.all_tasks()}
    out: dict[int, dict[str, int]] = defaultdict(
        lambda: {"passes": 0, "unreadable": 0, "answers_unscored": 0}
    )
    for key, entry in judged.items():
        tid, arm = parse_key(key)
        if tid not in tasks:
            continue
        row = out[arm]
        passes = entry.get("passes", [])
        bad = sum(not readable(*tasks[tid], p.get("verdict")) for p in passes)
        row["passes"] += len(passes)
        row["unreadable"] += bad
        row["answers_unscored"] += int(bool(passes) and bad == len(passes))
    return dict(out)


# ---------------------------------------------------------------- per task and arm


def per_task(ts: TaskSet, judged: dict[str, Any]) -> dict[int, dict[str, dict[str, Any]]]:
    """arm -> task id -> metrics averaged over the task's samples."""
    by: dict[tuple[int, str], list[dict[str, float | None]]] = defaultdict(list)
    tasks = {t["id"]: (kind, t) for kind, t in ts.all_tasks()}
    for key, entry in judged.items():
        tid, arm = parse_key(key)
        if tid not in tasks:
            continue
        kind, t = tasks[tid]
        m = answer_metrics(kind, t, entry)
        if m:
            by[(arm, tid)].append(m)
    out: dict[int, dict[str, dict[str, Any]]] = defaultdict(dict)
    for (arm, tid), rows in by.items():
        out[arm][tid] = {"kind": tasks[tid][0], "samples": len(rows), **_avg(rows)}
    return dict(out)


def arm_summary(rows: dict[str, dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for kind, metrics in (
        ("A", ("precision", "recall", "misleading", "abstained")),
        ("B", ("avoids", "satisfies", "pushed_back", "detector_fired")),
        ("C", ("passed", "current_stated", "stale", "abstention_failed")),
    ):
        mine = [r for r in rows.values() if r["kind"] == kind]
        if not mine:
            continue
        out[kind] = {"tasks": len(mine)}
        for m in metrics:
            vals = [r[m] for r in mine if r.get(m) is not None]
            out[kind][m] = round(mean(vals), 4) if vals else None
    n = sum(r.get("citations") or 0 for r in rows.values())
    good = sum(r.get("citations_good") or 0 for r in rows.values())
    claims = sum(r.get("claims") or 0 for r in rows.values() if r["kind"] == "A")
    cited = sum(r.get("claims_cited") or 0 for r in rows.values() if r["kind"] == "A")
    out["citation_precision"] = round(good / n, 4) if n else None
    out["citation_recall"] = round(cited / claims, 4) if claims else None
    return out


def compare(
    ts: TaskSet, tasks_by_arm: dict[int, dict[str, dict[str, Any]]], arm4_files: bool
) -> list[dict[str, Any]]:
    """Paired per-task deltas (treatment minus reference) with a bootstrap over
    documents and the MDE the run's own spread implies. Arm 4 without files is compared
    with arm 5, not with arm 3, whose pre-edit block it lacks."""
    out: list[dict[str, Any]] = []
    docs = {t["id"]: t["document"] for _, t in ts.all_tasks()}
    for treat, ref in COMPARISONS:
        if treat not in tasks_by_arm or ref not in tasks_by_arm:
            continue
        if (treat, ref) == (4, 3) and not arm4_files:
            continue
        for kind, metrics in HEADLINE.items():
            for metric in metrics:
                pairs: list[tuple[str, float, float]] = []
                for tid, r in tasks_by_arm[treat].items():
                    if r["kind"] != kind or tid not in tasks_by_arm[ref]:
                        continue
                    a, b = r.get(metric), tasks_by_arm[ref][tid].get(metric)
                    if a is not None and b is not None:
                        pairs.append((tid, a, b))
                if not pairs:
                    continue
                res = paired_bootstrap(
                    [a - b for _, a, b in pairs], clusters=[docs[tid] for tid, _, _ in pairs]
                )
                # With one document the resampled mean never moves: no interval, no MDE.
                se = (res.ub - res.lb) / (2 * 1.96) if res.clusters >= 2 else None
                out.append(
                    {
                        "treatment": treat,
                        "reference": ref,
                        "kind": kind,
                        "metric": metric,
                        "n": res.n,
                        "clusters": res.clusters,
                        "delta": round(res.mean, 4),
                        "lb": round(res.lb, 4),
                        "ub": round(res.ub, 4),
                        "mde": None if se is None else round(MDE_Z * se, 4),
                        "win_tie_loss": [res.wins, res.ties, res.losses],
                    }
                )
    return out


# ---------------------------------------------------------------- tokens and treatment


def token_summary(surf: dict[str, Any], arms: list[int]) -> dict[int, dict[str, Any]]:
    out: dict[int, dict[str, Any]] = {}
    for arm in arms:
        rows = [v for k, v in surf.items() if parse_key(k)[1] == arm]
        if not rows:
            continue
        calls = [n for r in rows for n in r["tokens"].get("for_file", [])]
        injected = [
            sum(sum(v) if isinstance(v, list) else v for v in r["tokens"].values()) for r in rows
        ]
        out[arm] = {
            "tasks": len(rows),
            "injected_mean": round(mean(injected), 1) if injected else 0,
            "injected_max": max(injected, default=0),
            "pre_edit_calls": len(calls),
            "pre_edit_max": max(calls, default=0),
            "with_context": sum(r["context"] != "(none)" for r in rows),
        }
    if 1 in out and 3 in out and out[3]["injected_mean"]:
        out[1]["ratio_to_arm3"] = round(out[1]["injected_mean"] / out[3]["injected_mean"], 2)
    return out


def treatment_check(ts: TaskSet, surf: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    """Proof the arms did what they mean, per surface: the tasks whose pre-edit block,
    and whose loom_search section, served a row (for arm 3 and 5, one of the task's own
    gold rows); for arm 4 the rows extracted, the rows given files, the failed runs."""
    out: dict[str, Any] = {}
    tasks = [t for _, t in ts.all_tasks()]
    arm4_files = (state.get("flags") or {}).get("arm4_files", True)
    for arm in (3, 4, 5):
        rows = [
            (t, surf[surface_key(t["id"], arm)]) for t in tasks if surface_key(t["id"], arm) in surf
        ]
        if not rows:
            continue
        for surface in ("for_file_keys", "search_keys"):
            if surface == "for_file_keys" and (arm == 5 or (arm == 4 and not arm4_files)):
                continue  # this arm reads no pre-edit block
            # Tasks with gold rows only: an abstention task has nothing to serve. Arm
            # 4's rows have keys of their own, so any row it served counts.
            if arm == 4:
                hits = [bool(r.get(surface)) for t, r in rows if t.get("gold_rows")]
            else:
                hits = [
                    bool(set(t["gold_rows"]) & set(r.get(surface, [])))
                    for t, r in rows
                    if t.get("gold_rows")
                ]
            out[f"arm{arm}_{surface.removesuffix('_keys')}_served"] = f"{sum(hits)}/{len(hits)}"
    extraction = state.get("extraction") or {}
    if extraction:
        # A key the run over a revision reported again is one row.
        out["arm4_rows_extracted"] = len({k for r in extraction.values() for k in r["keys"]})
        out["arm4_rows_with_files"] = len(
            {k for r in extraction.values() for k in r.get("linked") or {}}
        )
        out["arm4_extract_failures"] = sum(1 for r in extraction.values() if r["exit"] != 0)
        out["arm4_code_lookups_failed"] = sum(
            len(r.get("lookup_failures") or []) for r in extraction.values()
        )
    return out


# ---------------------------------------------------------------- judges and humans


def judge_stability(ts: TaskSet, judged: dict[str, Any]) -> dict[str, Any]:
    """Agreement on each answer's headline verdict between the two orders of one judge,
    between its repeats, and between judge models. A change answer whose detector fired
    is decided by the detector, so its passes cannot disagree: it is left out."""
    tasks = {t["id"]: (kind, t) for kind, t in ts.all_tasks()}
    headline = {"A": "misleading", "B": "avoids", "C": "passed"}
    agree: dict[str, list[bool]] = defaultdict(list)
    for key, entry in judged.items():
        tid, _arm = parse_key(key)
        if tid not in tasks:
            continue
        kind, t = tasks[tid]
        if kind == "B" and _detector_fired(entry):
            continue
        verdicts = {
            (p["model"], p["order"], p["repeat"]): pass_metrics(kind, t, entry, p["verdict"]).get(
                headline[kind]
            )
            for p in entry.get("passes", [])
            if readable(kind, t, p.get("verdict"))
        }
        models = sorted({m for m, _, _ in verdicts})
        for model in models:
            if (model, 0, 0) in verdicts and (model, 1, 0) in verdicts:
                agree["orders"].append(verdicts[(model, 0, 0)] == verdicts[(model, 1, 0)])
            if (model, 0, 0) in verdicts and (model, 0, 1) in verdicts:
                agree["repeats"].append(verdicts[(model, 0, 0)] == verdicts[(model, 0, 1)])
        if len(models) > 1 and all((m, 0, 0) in verdicts for m in models[:2]):
            agree["judges"].append(verdicts[(models[0], 0, 0)] == verdicts[(models[1], 0, 0)])
    return {
        k: {"answers": len(v), "agreement": round(sum(v) / len(v), 4)} for k, v in agree.items()
    }


def cohen_kappa(a: list[bool], b: list[bool]) -> float | None:
    n = len(a)
    if not n:
        return None
    observed = sum(x == y for x, y in zip(a, b, strict=True)) / n
    pa, pb = sum(a) / n, sum(b) / n
    expected = pa * pb + (1 - pa) * (1 - pb)
    return 1.0 if expected == 1 else (observed - expected) / (1 - expected)


def judge_vs_human(
    ts: TaskSet, judged: dict[str, Any], labels: list[dict[str, Any]]
) -> dict[str, Any]:
    """Judge accuracy and kappa against a human-labelled slice: a JSON list of
    {"answer": "<task>/arm<a>/s<k>", "field": <metric>, "value": bool}. Labels on
    `detector_fired` are never counted, nor labels on `DETECTOR_DECIDED` for a change
    answer whose detector fired: they say nothing about the judge."""
    tasks = {t["id"]: (kind, t) for kind, t in ts.all_tasks()}
    human: list[bool] = []
    machine: list[bool] = []
    for lab in labels:
        entry = judged.get(lab["answer"])
        if entry is None:
            continue
        kind, t = tasks[parse_key(lab["answer"])[0]]
        if lab["field"] == "detector_fired":
            continue
        if kind == "B" and lab["field"] in DETECTOR_DECIDED and _detector_fired(entry):
            continue
        value = answer_metrics(kind, t, entry).get(lab["field"])
        if value is None:
            continue
        human.append(bool(lab["value"]))
        machine.append(value >= 0.5)
    if not human:
        return {"items": 0}
    acc = sum(x == y for x, y in zip(human, machine, strict=True)) / len(human)
    kappa = cohen_kappa(human, machine)
    return {
        "items": len(human),
        "accuracy": round(acc, 4),
        "kappa": None if kappa is None else round(kappa, 4),
    }


VERDICTS = ("correct", "partly", "wrong", "misleading")


class LabelError(ValueError):
    """A human-judged extraction row whose verdict is empty or not one of `VERDICTS`."""


def extraction_precision(
    ts: TaskSet, labels: list[dict[str, Any]], state: dict[str, Any]
) -> dict[str, Any]:
    """From a human-judged file (a JSON list of {"document", "memory_key", "verdict"},
    verdict one of `VERDICTS`): per document the share judged correct, and the
    misleading-rationale rate over every judged row. Beside it, per document, the share
    of its gold rows whose source carries no extractable text and the change tasks
    resting only on those rows."""
    per: dict[str, list[str]] = defaultdict(list)
    for lab in labels:
        if lab["verdict"] not in VERDICTS:
            raise LabelError(f"verdict {lab['verdict']!r} is not one of {VERDICTS}")
        per[lab["document"]].append(lab["verdict"])
    docs: dict[str, Any] = {}
    for doc_id in sorted(set(per) | {g.document for g in ts.gold.values()}):
        verdicts = per.get(doc_id, [])
        gold = [g for g in ts.gold.values() if g.document == doc_id]
        image_only = {g.memory_key for g in gold if g.image_only}
        cost = [
            t["id"]
            for t in ts.tasks.get("B", [])
            if t["document"] == doc_id and t["gold_rows"] and set(t["gold_rows"]) <= image_only
        ]
        docs[doc_id] = {
            "judged": len(verdicts),
            "precision": round(verdicts.count("correct") / len(verdicts), 4) if verdicts else None,
            "gold_image_only_share": round(len(image_only) / len(gold), 4) if gold else None,
            "b_tasks_on_image_only_rows": len(cost),
            "image_only_sections_seen": (state.get("extraction") or {})
            .get(doc_id, {})
            .get("image_only_sections"),
        }
    every = [v for vs in per.values() for v in vs]
    return {
        "documents": docs,
        "misleading_rate": round(every.count("misleading") / len(every), 4) if every else None,
    }


# ---------------------------------------------------------------- the report


def score(
    ts: TaskSet,
    out_dir: Path,
    *,
    human: list[dict[str, Any]] | None = None,
    extraction_labels: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    def read(name: str) -> dict[str, Any]:
        p = out_dir / name
        return json.loads(p.read_text()) if p.exists() else {}

    judged, surf, state = read("judged.json"), read("surfaces.json"), read("state.json")
    by_arm = per_task(ts, judged)
    arms = sorted(by_arm)
    metrics = {
        "arms": {arm: arm_summary(by_arm[arm]) for arm in arms},
        "comparisons": compare(ts, by_arm, (state.get("flags") or {}).get("arm4_files", True)),
        "tokens": token_summary(surf, sorted({parse_key(k)[1] for k in surf})),
        "treatment": treatment_check(ts, surf, state),
        "judge_stability": judge_stability(ts, judged),
        "unreadable": unreadable(ts, judged),
        "per_task": by_arm,
    }
    if human:
        metrics["judge_vs_human"] = judge_vs_human(ts, judged, human)
    if extraction_labels is not None:
        metrics["extraction"] = extraction_precision(ts, extraction_labels, state)
    (out_dir / "metrics.json").write_text(json.dumps(metrics, indent=1))
    return metrics


def summary(metrics: dict[str, Any], out_dir: Path) -> dict[str, Any]:
    """The run's aggregates without the per-task rows (task ids can name private
    material), with the run's flags and its spend: by ledger stage, and the answers'
    cost per arm."""
    state = json.loads((out_dir / "state.json").read_text())
    answers = json.loads((out_dir / "answers.json").read_text())
    flags = state.get("flags") or {}
    by_stage: dict[str, float] = defaultdict(float)
    ledger = out_dir / "ledger.jsonl"
    for line in ledger.read_text().splitlines() if ledger.exists() else []:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue  # a row an interrupt cut short, as the ledger itself drops it
        by_stage[row["stage"]] += row["usd"]
    answer_usd: dict[int, float] = defaultdict(float)
    answer_n: dict[int, int] = defaultdict(int)
    for key, a in answers.items():
        arm = parse_key(key)[1]
        answer_usd[arm] += cost_usd(flags["reader"], a.get("in", 0), a.get("out", 0))
        answer_n[arm] += 1
    samples = {key.rsplit("/s", 1)[1] for key in answers}
    run = {k: v for k, v in flags.items() if k not in ("org", "arm4_namespace")}
    return {
        "run": {**run, "k": len(samples)},
        "spend_usd": {
            "by_stage": {k: round(v, 4) for k, v in sorted(by_stage.items())},
            "total": round(sum(by_stage.values()), 4),
            "answers_by_arm": {
                arm: {"answers": answer_n[arm], "usd": round(answer_usd[arm], 4)}
                for arm in sorted(answer_usd)
            },
        },
        **{k: v for k, v in metrics.items() if k != "per_task"},
    }


def _pct(v: float | None) -> str:
    return "-" if v is None else f"{v * 100:.1f}"


def report(m: dict[str, Any]) -> str:
    """The aggregate numbers as markdown: per arm, per kind, no task text."""
    lines = [
        "| Arm | A precision | A recall | A misleading | B avoids | B satisfies | "
        "C passes | C states current | Citation precision | Citation recall |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for arm, s in m["arms"].items():
        a, b, c = s.get("A", {}), s.get("B", {}), s.get("C", {})
        lines.append(
            f"| {arm} | {_pct(a.get('precision'))} | {_pct(a.get('recall'))} | "
            f"{_pct(a.get('misleading'))} | {_pct(b.get('avoids'))} | "
            f"{_pct(b.get('satisfies'))} | {_pct(c.get('passed'))} | "
            f"{_pct(c.get('current_stated'))} | "
            f"{_pct(s.get('citation_precision'))} | {_pct(s.get('citation_recall'))} |"
        )
    lines += [
        "",
        "| Treatment vs reference | Kind | Metric | n (clusters) | Delta | 95% interval | MDE |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for c in m["comparisons"]:
        mde = "-" if c["mde"] is None else f"{c['mde'] * 100:.1f}"
        lines.append(
            f"| {c['treatment']} vs {c['reference']} | {c['kind']} | {c['metric']} | "
            f"{c['n']} ({c['clusters']}) | {c['delta'] * 100:+.1f} | "
            f"{c['lb'] * 100:+.1f} .. {c['ub'] * 100:+.1f} | {mde} |"
        )
    lines += [
        "",
        "| Arm | Injected tokens per task (mean, max) | Pre-edit calls | Pre-edit max |",
        "| --- | --- | --- | --- |",
    ]
    for arm, t in m["tokens"].items():
        lines.append(
            f"| {arm} | {t['injected_mean']}, {t['injected_max']} | "
            f"{t['pre_edit_calls']} | {t['pre_edit_max']} |"
        )
    if ratio := m["tokens"].get(1, {}).get("ratio_to_arm3"):
        lines.append(f"\nArm 1 injects {ratio}x the tokens of arm 3.")
    lines.append(
        "\nTreatment check: "
        + (", ".join(f"{k} {v}" for k, v in m["treatment"].items()) or "nothing to check")
    )
    if m["judge_stability"]:
        lines.append(
            "Judge agreement: "
            + ", ".join(
                f"{k} {v['agreement'] * 100:.1f}% of {v['answers']}"
                for k, v in m["judge_stability"].items()
            )
        )
    bad = {arm: u for arm, u in m.get("unreadable", {}).items() if u["unreadable"]}
    if bad:
        lines.append(
            "Unreadable verdicts: "
            + ", ".join(
                f"arm {arm} {u['unreadable']} of {u['passes']} passes "
                f"({u['answers_unscored']} answers unscored)"
                for arm, u in bad.items()
            )
        )
    if h := m.get("judge_vs_human"):
        lines.append(
            f"Judge vs human: {h['items']} items, accuracy {_pct(h.get('accuracy'))}%, "
            f"kappa {h.get('kappa')}"
        )
    if e := m.get("extraction"):
        lines.append(f"Extraction misleading-rationale rate: {_pct(e['misleading_rate'])}%")
        # Documents by ordinal: a document id may name private material.
        for i, d in enumerate(e["documents"].values(), 1):
            lines.append(
                f"- document {i}: precision {_pct(d['precision'])}% of {d['judged']} judged, "
                f"gold rows on image-only sources {_pct(d['gold_image_only_share'])}%, "
                f"change tasks resting on them {d['b_tasks_on_image_only_rows']}"
            )
    return "\n".join(lines)
