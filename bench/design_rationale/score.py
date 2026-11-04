"""The `score` command: metrics per task and arm from a run's JSON files, the paired
comparisons between arms with a bootstrap over documents, the minimum detectable effects,
judge stability, judge accuracy against a human-labelled slice, and extraction precision
from a human-judged file. No model call and no Loom call: a re-score is free.

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
from bench.design_rationale.taskset import TaskSet

# z(0.975) + z(0.80): the effect a two-sided 5% test detects with 80% power is this many
# standard errors.
MDE_Z = 1.96 + 0.84
# Planning standard deviation of a per-task paired delta, for an MDE before any run: a
# pass/fail outcome averaged over K samples, from the spot check's spread.
PLANNING_SD = 0.35
# (treatment arm, reference arm) pairs reported when both ran.
COMPARISONS = ((3, 0), (4, 0), (4, 3), (1, 3), (2, 3))
# The per-task metric each kind's comparison is made on, higher is better.
HEADLINE = {
    "A": ("precision", "recall"),
    "B": ("avoids", "satisfies"),
    "C": ("passed",),
}
PRE_EDIT_BUDGET = 300


def planning_mde(n_tasks: int, sd: float = PLANNING_SD) -> float:
    return MDE_Z * sd / math.sqrt(n_tasks) if n_tasks else float("nan")


# ---------------------------------------------------------------- one judge pass


def _supported(entry: dict[str, Any], verdict: dict[str, Any]) -> dict[str, bool]:
    """citation -> whether a judge said its text supports what it was cited for."""
    pairs: list[tuple[str, Any]] = []
    for claim in verdict.get("claims") or []:
        pairs += list(zip(claim.get("cites") or [], claim.get("cites_support") or [], strict=False))
    pairs += list(zip(verdict.get("cites") or [], verdict.get("cites_support") or [], strict=False))
    support: dict[str, bool] = {}
    for cited, ok in pairs:
        for c in entry["citations"]:
            if c["cite"] in str(cited) or str(cited) in c["cite"]:
                support[c["cite"]] = support.get(c["cite"], False) or bool(ok)
    return support


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
        cited_ok = sum(
            any(
                c["cite"] in good and (c["cite"] in str(x) or str(x) in c["cite"])
                for x in claim.get("cites") or []
                for c in entry["citations"]
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
        fired = any(d["fired"] for d in entry.get("detectors", []) if d["fired"] is not None)
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
        else:
            passed = bool(verdict.get("abstained")) and not verdict.get("invented_rationale")
            m["invented"] = float(not passed)
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
        if isinstance(p.get("verdict"), dict)
    ]
    return _avg(rows) if rows else {}


# ---------------------------------------------------------------- per task and arm


def per_task(ts: TaskSet, judged: dict[str, Any]) -> dict[int, dict[str, dict[str, Any]]]:
    """arm -> task id -> metrics averaged over the task's samples."""
    by: dict[tuple[int, str], list[dict[str, float | None]]] = defaultdict(list)
    tasks = {t["id"]: (kind, t) for kind, t in ts.all_tasks()}
    for key, entry in judged.items():
        tid, arm_s, _ = key.split("/")
        if tid not in tasks:
            continue
        kind, t = tasks[tid]
        m = answer_metrics(kind, t, entry)
        if m:
            by[(int(arm_s.removeprefix("arm")), tid)].append(m)
    out: dict[int, dict[str, dict[str, Any]]] = defaultdict(dict)
    for (arm, tid), rows in by.items():
        out[arm][tid] = {"kind": tasks[tid][0], "samples": len(rows), **_avg(rows)}
    return dict(out)


def arm_summary(ts: TaskSet, rows: dict[str, dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for kind, metrics in (
        ("A", ("precision", "recall", "misleading", "abstained")),
        ("B", ("avoids", "satisfies", "pushed_back", "detector_fired")),
        ("C", ("passed", "stale", "invented")),
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
    ts: TaskSet, tasks_by_arm: dict[int, dict[str, dict[str, Any]]]
) -> list[dict[str, Any]]:
    """Paired per-task deltas (treatment minus reference) with a bootstrap over
    documents and the MDE the run's own spread implies."""
    out: list[dict[str, Any]] = []
    docs = {t["id"]: t["document"] for _, t in ts.all_tasks()}
    for treat, ref in COMPARISONS:
        if treat not in tasks_by_arm or ref not in tasks_by_arm:
            continue
        for kind, metrics in HEADLINE.items():
            for metric in metrics:
                pairs = [
                    (tid, r[metric], tasks_by_arm[ref][tid].get(metric))
                    for tid, r in tasks_by_arm[treat].items()
                    if r["kind"] == kind and tid in tasks_by_arm[ref]
                ]
                pairs = [(tid, a, b) for tid, a, b in pairs if a is not None and b is not None]
                if not pairs:
                    continue
                res = paired_bootstrap(
                    [a - b for _, a, b in pairs], clusters=[docs[tid] for tid, _, _ in pairs]
                )
                se = (res.ub - res.lb) / (2 * 1.96)
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
                        "mde": round(MDE_Z * se, 4),
                        "win_tie_loss": [res.wins, res.ties, res.losses],
                    }
                )
    return out


# ---------------------------------------------------------------- tokens and treatment


def token_summary(ts: TaskSet, surf: dict[str, Any], arms: list[int]) -> dict[int, dict[str, Any]]:
    out: dict[int, dict[str, Any]] = {}
    for arm in arms:
        rows = [v for k, v in surf.items() if k.endswith(f"/arm{arm}")]
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
            "pre_edit_over_budget": sum(n > PRE_EDIT_BUDGET for n in calls),
            "with_context": sum(r["context"] != "(none)" for r in rows),
        }
    if 1 in out and 3 in out and out[3]["injected_mean"]:
        out[1]["ratio_to_arm3"] = round(out[1]["injected_mean"] / out[3]["injected_mean"], 2)
    return out


def treatment_check(ts: TaskSet, surf: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    """Proof the arms did what they mean: arm 3 served the tasks' own gold rows, arm 4 had
    extracted rows and served some of them."""
    out: dict[str, Any] = {}
    with_gold = [
        (t["id"], set(t.get("gold_rows", []))) for _, t in ts.all_tasks() if t.get("gold_rows")
    ]
    served = [
        bool(g & set(surf[f"{tid}/arm3"]["keys"])) for tid, g in with_gold if f"{tid}/arm3" in surf
    ]
    if served:
        out["arm3_tasks_served_a_gold_row"] = f"{sum(served)}/{len(served)}"
    extraction = state.get("extraction") or {}
    if extraction:
        out["arm4_rows_extracted"] = sum(len(r["keys"]) for r in extraction.values())
        out["arm4_extract_failures"] = sum(1 for r in extraction.values() if r["exit"] == 1)
    rows4 = [v for k, v in surf.items() if k.endswith("/arm4")]
    if rows4:
        out["arm4_tasks_served_a_row"] = f"{sum(bool(r['keys']) for r in rows4)}/{len(rows4)}"
    return out


# ---------------------------------------------------------------- judges and humans


def judge_stability(ts: TaskSet, judged: dict[str, Any]) -> dict[str, Any]:
    """Agreement on each answer's headline verdict between the two orders of one judge,
    between its repeats, and between judge models."""
    tasks = {t["id"]: (kind, t) for kind, t in ts.all_tasks()}
    headline = {"A": "misleading", "B": "avoids", "C": "passed"}
    agree: dict[str, list[bool]] = defaultdict(list)
    for key, entry in judged.items():
        if key.split("/")[0] not in tasks:
            continue
        kind, t = tasks[key.split("/")[0]]
        verdicts = {
            (p["model"], p["order"], p["repeat"]): pass_metrics(kind, t, entry, p["verdict"]).get(
                headline[kind]
            )
            for p in entry.get("passes", [])
            if isinstance(p.get("verdict"), dict)
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
    {"answer": "<task>/arm<a>/s<k>", "field": <metric>, "value": bool}."""
    tasks = {t["id"]: (kind, t) for kind, t in ts.all_tasks()}
    human: list[bool] = []
    machine: list[bool] = []
    for lab in labels:
        entry = judged.get(lab["answer"])
        if entry is None:
            continue
        kind, t = tasks[lab["answer"].split("/")[0]]
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


def extraction_precision(
    ts: TaskSet, labels: list[dict[str, Any]], state: dict[str, Any]
) -> dict[str, Any]:
    """From a human-judged file (a JSON list of {"document", "memory_key", "verdict"},
    verdict one of `VERDICTS`): per document the share judged correct, and the
    misleading-rationale rate over every judged row. Beside it, per document, the share of
    its gold rows whose source carries no extractable text and the change tasks resting
    only on those rows."""
    per: dict[str, list[str]] = defaultdict(list)
    for lab in labels:
        if lab["verdict"] not in VERDICTS:
            raise ValueError(f"verdict {lab['verdict']!r} is not one of {VERDICTS}")
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
        "arms": {arm: arm_summary(ts, by_arm[arm]) for arm in arms},
        "comparisons": compare(ts, by_arm),
        "tokens": token_summary(ts, surf, sorted({int(k.split("arm")[1]) for k in surf})),
        "treatment": treatment_check(ts, surf, state),
        "judge_stability": judge_stability(ts, judged),
        "per_task": by_arm,
    }
    if human:
        metrics["judge_vs_human"] = judge_vs_human(ts, judged, human)
    if extraction_labels is not None:
        metrics["extraction"] = extraction_precision(ts, extraction_labels, state)
    (out_dir / "metrics.json").write_text(json.dumps(metrics, indent=1))
    return metrics


def _pct(v: float | None) -> str:
    return "-" if v is None else f"{v * 100:.1f}"


def report(m: dict[str, Any]) -> str:
    """The aggregate numbers as markdown: per arm, per kind, no task text."""
    lines = [
        "| Arm | A precision | A recall | A misleading | B avoids | B satisfies | "
        "C passes | Citation precision | Citation recall |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for arm, s in m["arms"].items():
        a, b, c = s.get("A", {}), s.get("B", {}), s.get("C", {})
        lines.append(
            f"| {arm} | {_pct(a.get('precision'))} | {_pct(a.get('recall'))} | "
            f"{_pct(a.get('misleading'))} | {_pct(b.get('avoids'))} | "
            f"{_pct(b.get('satisfies'))} | {_pct(c.get('passed'))} | "
            f"{_pct(s.get('citation_precision'))} | {_pct(s.get('citation_recall'))} |"
        )
    lines += [
        "",
        "| Treatment vs reference | Kind | Metric | n (clusters) | Delta | 95% interval | MDE |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for c in m["comparisons"]:
        lines.append(
            f"| {c['treatment']} vs {c['reference']} | {c['kind']} | {c['metric']} | "
            f"{c['n']} ({c['clusters']}) | {c['delta'] * 100:+.1f} | "
            f"{c['lb'] * 100:+.1f} .. {c['ub'] * 100:+.1f} | {c['mde'] * 100:.1f} |"
        )
    lines += [
        "",
        "| Arm | Injected tokens per task (mean, max) | Pre-edit calls | Pre-edit max | Over 300 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for arm, t in m["tokens"].items():
        lines.append(
            f"| {arm} | {t['injected_mean']}, {t['injected_max']} | "
            f"{t['pre_edit_calls']} | {t['pre_edit_max']} | {t['pre_edit_over_budget']} |"
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
    if h := m.get("judge_vs_human"):
        lines.append(
            f"Judge vs human: {h['items']} items, accuracy {_pct(h.get('accuracy'))}%, "
            f"kappa {h.get('kappa')}"
        )
    if e := m.get("extraction"):
        lines.append(f"Extraction misleading-rationale rate: {_pct(e['misleading_rate'])}%")
        for doc_id, d in e["documents"].items():
            lines.append(
                f"- {doc_id}: precision {_pct(d['precision'])}% of {d['judged']} judged, "
                f"gold rows on image-only sources {_pct(d['gold_image_only_share'])}%, "
                f"change tasks resting on them {d['b_tasks_on_image_only_rows']}"
            )
    return "\n".join(lines)
