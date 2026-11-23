"""Replay dated public project knowledge without changing the issue or source."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path

import httpx

from examples.software_factory.evals.flask.bootstrap import LOCK
from examples.software_factory.evals.flask.historical import FrozenSource, git
from examples.software_factory.factory.model import digest
from examples.software_factory.harbor.runtime import write
from examples.software_factory.harbor.source_tree import source_files

HERE = Path(__file__).resolve().parent
CASES = HERE / "historical_cases.json"
CATALOG = HERE / "historical_memories.json"


def _require(condition, message):
    if not condition:
        raise PermissionError(message)


def _date(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    _require(parsed.tzinfo is not None, "Historical dates need an explicit timezone")
    return parsed


def validate_catalog(catalog, case, paths):
    records, sources, limits = catalog["records"], catalog["sources"], catalog["requirements"]
    _require(
        catalog["schema_version"] == 1
        and catalog["kind"] == "historical-project-context"
        and len(records) == limits["rows"]
        and sum(bool(row["files"]) for row in records) == limits["code_linked"]
        and sum(not row["files"] for row in records) == limits["unlinked"]
        and len({row["key"] for row in records}) == len(records),
        "Historical catalog counts or identities differ",
    )
    cutoff = _date(case["cutoff"])
    for source in sources.values():
        _require(
            source["url"].startswith("https://github.com/pallets/flask/")
            and re.fullmatch(r"[0-9a-f]{64}", source["sha256"])
            and source["kind"] in {"git-document", "historical-pull-request"},
            "Historical source must identify a pinned public project document",
        )
        for field in ("published_at", "last_edited_at", "merged_at"):
            if source.get(field):
                _require(_date(source[field]) < cutoff, "Historical source exceeds task cutoff")
        _require(
            source.get("number") not in case["excluded_source_numbers"],
            "Target issue or fixing PR cannot supply historical memory",
        )
    for row in records:
        _require(
            row["key"].startswith("history/")
            and isinstance(row["value"], str)
            and 0 < len(row["value"]) <= limits["max_chars"]
            and len(row["value"].split()) <= limits["max_words"]
            and set(row["files"]) <= set(paths)
            and row["source_ids"]
            and set(row["source_ids"]) <= set(sources),
            "Historical note lacks short, source-scoped evidence",
        )
        initial_sources = row.get("initial_source_ids", row["source_ids"])
        initial_value = row.get("initial_value", row["value"])
        _require(
            initial_sources
            and set(initial_sources) <= set(sources)
            and isinstance(initial_value, str)
            and 0 < len(initial_value) <= limits["max_chars"]
            and len(initial_value.split()) <= limits["max_words"],
            "Initial note lacks short, verified evidence",
        )
        if case.get("history_base_cutoff"):
            _require(
                all(
                    _date(sources[key][field]) < _date(case["history_base_cutoff"])
                    for key in initial_sources
                    for field in ("published_at", "last_edited_at", "merged_at")
                    if sources[key].get(field)
                ),
                "Initial memory source was not available at the historical revision",
            )


def verify_sources(sources):
    receipts = {}
    with httpx.Client(follow_redirects=True, timeout=120) as client:
        for name, source in sources.items():
            if source["kind"] == "git-document":
                url = (
                    "https://raw.githubusercontent.com/pallets/flask/"
                    + source["commit"]
                    + "/"
                    + source["path"]
                )
                response = client.get(url)
                response.raise_for_status()
                content = response.content
            else:
                response = client.get(
                    "https://api.github.com/repos/pallets/flask/pulls/" + str(source["number"])
                )
                response.raise_for_status()
                data = response.json()
                _require(
                    data["created_at"] == source["published_at"]
                    and data["merged_at"] == source["merged_at"],
                    "Historical source timestamps changed",
                )
                content = data["body"].encode()
            actual = hashlib.sha256(content).hexdigest()
            _require(actual == source["sha256"], "Historical source content changed")
            receipts[name] = {"url": source["url"], "sha256": actual, "verified": True}
    return receipts


def _sourced(catalog, value, source_ids):
    return value + "\nSources: " + ", ".join(catalog["sources"][key]["url"] for key in source_ids)


def materialize_cases(assets_path, output_path):
    assets, output = Path(assets_path), Path(output_path)
    _require(not output.exists(), "Historical materialization requires a fresh directory")
    manifest, catalog = json.loads(CASES.read_text()), json.loads(CATALOG.read_text())
    roots = json.loads(LOCK.read_text())["source_roots"]
    receipt = json.loads((assets / "assets.json").read_text())
    public = json.loads((assets / "historical.json").read_text())["tasks"]
    cases = {case["case_id"]: case for case in manifest["cases"]}
    _require(set(cases) == {row["task_id"] for row in public}, "Historical cases differ from tasks")
    output.mkdir(parents=True)
    (output / "public").mkdir()
    evidence = output / "historical-source-verification.json"
    write(evidence, verify_sources(catalog["sources"]))
    fixtures = {}
    mirror = Path(receipt["source_git"])
    heads = sorted(
        {case["history_base_commit"] for case in cases.values()}
        | {task["base_commit"] for task in public}
    )
    fetch = ["fetch", "--no-tags"]
    if git(mirror, "rev-parse", "--is-shallow-repository").strip() == b"true":
        fetch.append("--unshallow")
    git(mirror, *fetch, "https://github.com/pallets/flask.git", *heads)
    for task in public:
        git(
            mirror,
            "merge-base",
            "--is-ancestor",
            cases[task["task_id"]]["history_base_commit"],
            task["base_commit"],
        )
    for task in public:
        case = cases[task["task_id"]]
        _require(case["base_commit"] == task["base_commit"], "Historical task base changed")
        source = FrozenSource(Path(receipt["source_git"]), task["base_commit"])
        files = source_files(source.mirror, source.commit, roots)
        earlier = FrozenSource(Path(receipt["source_git"]), case["history_base_commit"])
        initial_files = source_files(earlier.mirror, earlier.commit, roots)
        validate_catalog(catalog, case, files)
        events, documents = [], {}
        for row in catalog["records"]:
            value = _sourced(
                catalog,
                row.get("initial_value", row["value"]),
                row.get("initial_source_ids", row["source_ids"]),
            )
            events.append(
                dict(
                    sequence=len(events) + 1,
                    operation="set",
                    revision="A",
                    key=row["key"],
                    value=value,
                    files=row["files"],
                    source_ids=row.get("initial_source_ids", row["source_ids"]),
                )
            )
            documents[row["key"]] = _sourced(catalog, row["value"], row["source_ids"])
        for row in catalog["records"]:
            if row.get("initial_value") is not None:
                events.append(
                    dict(
                        sequence=len(events) + 1,
                        operation="update",
                        revision="B",
                        key=row["key"],
                        value=documents[row["key"]],
                        files=row["files"],
                        source_ids=row["source_ids"],
                    )
                )
        fixture = {
            "schema_version": 1,
            "kind": "historical-evolution",
            "case_id": task["task_id"],
            "dependency_task_id": task["task_id"],
            "source_git": receipt["source_git"],
            "revisions": {
                "A": {"commit": case["history_base_commit"]},
                "B": {"commit": task["base_commit"]},
            },
            "source_sha256": {"A": digest(initial_files), "B": digest(files)},
            "public_documents": documents,
            "public_documents_sha256": digest(documents),
            "history_events": events,
            "history_events_sha256": digest(events),
            "historical_cutoff": case["cutoff"],
            "history_counts": {
                "current_rows": len(catalog["records"]),
                "setup_operations": len(events),
                "code_linked_rows": catalog["requirements"]["code_linked"],
                "unlinked_rows": catalog["requirements"]["unlinked"],
                "revision_updates": sum(event["operation"] == "update" for event in events),
            },
        }
        path = output / "public" / (task["task_id"] + ".json")
        write(path, fixture)
        fixtures[task["task_id"]] = {
            "fixture_file": str(path),
            "dependency_task_id": task["task_id"],
            "source_git": receipt["source_git"],
            "revisions": fixture["revisions"],
            "history_counts": fixture["history_counts"],
        }
    historical = output / "historical.json"
    write(historical, {"tasks": [{**row, "scenario_family": "historical-issue"} for row in public]})
    paths = [
        CASES,
        CATALOG,
        evidence,
        historical,
        *(Path(r["fixture_file"]) for r in fixtures.values()),
    ]
    result = {
        "schema_version": 1,
        "kind": "historical-context-materialization",
        "tasks": {},
        "fixtures": fixtures,
        "historical_manifest": str(historical),
        "reference_controls_file": str(assets / "trusted/tasks.json"),
        "provider_requests": 0,
        "loom_requests": 0,
        "files": {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths},
    }
    write(output / "materialization.json", result)
    return result
