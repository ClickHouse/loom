"""Publish issue-level comparison bars from measured attempts, without inference."""

from __future__ import annotations

import hashlib
import importlib.resources
import json
import math
import os
import tempfile
from pathlib import Path
from urllib.parse import quote

from .arms import PRIMARY

ARMS = {name: arm.label for name, arm in PRIMARY.items()}
METRICS = [
    {"key": "resolved", "title": "Solved (hidden tests)", "kind": "rate"},
    {"key": "judge_fixed", "title": "Fixed, per blind judge", "kind": "rate"},
    {"key": "full_suite_regressions", "title": "Regressions in full suite", "kind": "count"},
    {"key": "cost", "title": "Cost", "kind": "cost"},
    {"key": "agent_seconds", "title": "Agent time", "kind": "time"},
    {"key": "agent_test_runs", "title": "Test runs by the agent", "kind": "count"},
    {"key": "loom_tool_calls", "title": "Loom tool calls", "kind": "count"},
    {"key": "memory_rows_written", "title": "Memory rows written", "kind": "count"},
    {"key": "model_total_tokens", "title": "Tokens", "kind": "count"},
]
ASSETS = importlib.resources.files("examples.software_factory.harbor").joinpath("report_assets")


def atomic(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False) as file:
        temporary = Path(file.name)
        file.write(text)
        file.flush()
        os.fsync(file.fileno())
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def number(value):
    return value if type(value) in {int, float} and math.isfinite(value) and value >= 0 else None


def chart_data(protocol, entries, views_manifest):
    output = Path(protocol["output_root"]).resolve()
    observed = {e["order"]: e["result"] for e in entries}
    titles = {}
    manifest = protocol.get("historical_manifest")
    if manifest:
        for task in json.loads(Path(manifest).read_text())["tasks"]:
            titles[task["task_id"]] = (task.get("problem") or task["task_id"]).splitlines()[0]
    base = protocol.get("reporting", {}).get("viewer_base_url", "http://127.0.0.1:8080")
    rows = []
    for slot in protocol["schedule"]:
        if slot["arm"] not in ARMS:
            continue
        result = observed.get(slot["order"])
        view = views_manifest.get("views", {}).get(slot["arm"], {})
        row = {
            "order": slot["order"],
            "task": slot["task_id"],
            "repeat": slot["repeat"],
            "arm": slot["arm"],
            "state": "pending",
            "resolved": None,
            "seconds": None,
            "native_seconds": None,
            "cost": None,
            **{metric["key"]: None for metric in METRICS},
            "url": None,
        }
        if result is not None:
            row.update(
                state="complete" if result.get("harbor_reward_valid") else "error",
                resolved=(
                    result.get("resolved") is True and result.get("harbor_reward_valid") is True
                ),
                seconds=number(result.get("end_to_end_s")),
                native_seconds=number(result.get("harbor_wall_s")),
                cost=number(result.get("total_provider_cost_usd")),
                generation_cost=number(result.get("generation_cost_usd")),
                loom_provider_cost=number(result.get("loom_provider_cost_usd")),
                agent_seconds=number(result.get("agent_time_s")),
                **{
                    key: result.get(key) if type(result.get(key)) is bool else None
                    for key in ("judge_fixed", "judge_better")
                },
                **{
                    key: number(result.get(key))
                    for key in (
                        "full_suite_regressions",
                        "agent_test_runs",
                        "loom_tool_calls",
                        "memory_rows_written",
                        "model_total_tokens",
                        "model_input_tokens",
                        "model_output_tokens",
                        "model_cache_read_tokens",
                        "model_cache_write_tokens",
                    )
                },
            )
            if result.get("harbor_trial") and view.get("job_name"):
                row["url"] = (
                    base.rstrip("/")
                    + "/jobs/"
                    + quote(view["job_name"], safe="")
                    + "/trials/"
                    + quote(Path(result["harbor_trial"]).name, safe="")
                )
        rows.append(row)
    return {
        "schema_version": 1,
        "name": output.name,
        "model": protocol.get("model_id"),
        "run_id": hashlib.sha256(str(output).encode()).hexdigest()[:16],
        "arms": ARMS,
        "metrics": METRICS,
        "titles": titles,
        "attempts": rows,
        "observed": len(entries),
        "scheduled": len(protocol["schedule"]),
        "complete": len(observed) == len(protocol["schedule"]),
        "evaluation_complete": views_manifest.get(
            "evaluation_complete", len(observed) == len(protocol["schedule"])
        ),
        "comparison_url": views_manifest.get("links", {}).get("comparison"),
        "target_reduction": 0.20,
        "cost_note": "Provider cost combines generation list-price estimates and "
        "matched Loom provider ledger costs, including failed attempts. "
        "Unmatched backend cost remains unknown. Judge and infrastructure costs are separate.",
    }


def generate_charts(protocol, entries, views_manifest):
    output = Path(protocol["output_root"]).resolve()
    settings = protocol.get("reporting", {})
    data = chart_data(protocol, entries, views_manifest)
    encoded = json.dumps(data, ensure_ascii=False).replace("<", "\\u003c")
    html = (
        ASSETS.joinpath("charts.html")
        .read_text()
        .replace("__CLIENT_SCRIPT__", ASSETS.joinpath("charts.js").read_text())
        .replace("__INITIAL_DATA__", encoded)
    )
    local = output / "reports"
    atomic(local / "charts.html", html)
    atomic(local / "charts.json", encoded + "\n")
    result = {
        "html": str(local / "charts.html"),
        "data": str(local / "charts.json"),
        "published": False,
        "url": None,
        "index_url": None,
    }
    if settings.get("publish_charts", True) is False:
        return result
    configured = settings.get("chart_assets_root")
    if configured:
        assets = Path(configured)
    else:
        assets = Path(str(importlib.resources.files("harbor.viewer"))) / "static/assets"
    target = assets / "factory-charts"
    base = settings.get("viewer_base_url", "http://127.0.0.1:8080").rstrip("/")
    name = data["run_id"]
    try:
        atomic(target / (name + ".html"), html)
        atomic(target / (name + ".json"), encoded + "\n")
        index = target / "index.json"
        registry = json.loads(index.read_text()) if index.exists() else {"runs": {}}
        registry["runs"][name] = {
            "name": data["name"],
            "observed": data["observed"],
            "scheduled": data["scheduled"],
            "url": "/assets/factory-charts/" + name + ".html",
            "jobs": [
                v["job_name"] for k, v in views_manifest.get("views", {}).items() if k in ARMS
            ],
        }
        atomic(index, json.dumps(registry, ensure_ascii=False) + "\n")
        atomic(target / "navigation.js", ASSETS.joinpath("navigation.js").read_text())
        atomic(target / "index.html", ASSETS.joinpath("chart_index.html").read_text())
        shell = assets.parent / "index.html"
        if settings.get("chart_install_navigation", True) and shell.is_file():
            original = shell.read_text()
            marker = '<script defer src="/assets/factory-charts/navigation.js"></script>'
            if marker not in original:
                backup = target / "viewer-index.before-charts.txt"
                if not backup.exists():
                    atomic(backup, original)
                atomic(shell, original.replace("</body>", marker + "</body>"))
        result.update(
            published=True,
            url=base + "/assets/factory-charts/" + name + ".html",
            index_url=base + "/assets/factory-charts/index.html",
        )
    except OSError as error:
        # The local report remains usable when the installed viewer assets are read-only.
        result["publication_error"] = type(error).__name__
    atomic(local / "chart-publication.json", json.dumps(result, indent=2) + "\n")
    return result
