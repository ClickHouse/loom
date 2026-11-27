"""Deterministic public history and source overlays; no evaluator answers are emitted."""

from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path

from examples.software_factory.factory.model import digest

MANIFEST = Path(__file__).with_name("cases.json")
MEMORIES = Path(__file__).with_name("project_memories.json")


def load_manifest() -> dict:
    return json.loads(MANIFEST.read_text())


def _case(case_id):
    manifest = load_manifest()
    matches = [case for case in manifest["cases"] if case["case_id"] == case_id]
    if len(matches) != 1:
        raise ValueError("Unknown simulated-history case")
    return manifest, matches[0]


def _replace_function(text, case, old, new):
    tree = ast.parse(text)
    scope = tree.body
    if case["class_name"] is not None:
        classes = [
            node
            for node in scope
            if isinstance(node, ast.ClassDef) and node.name == case["class_name"]
        ]
        if len(classes) != 1:
            raise PermissionError("Pinned class shape differs")
        scope = classes[0].body
    nodes = [
        node
        for node in scope
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == case["function"]
    ]
    if len(nodes) != 1:
        raise PermissionError("Pinned function shape differs")
    node = nodes[0]
    lines = text.splitlines(keepends=True)
    body = "".join(lines[node.lineno - 1 : node.end_lineno])
    if body.count(old) != 1:
        raise PermissionError("Pinned source overlay does not match exactly once")
    changed = (
        "".join(lines[: node.lineno - 1])
        + body.replace(old, new, 1)
        + "".join(lines[node.end_lineno :])
    )
    ast.parse(changed)
    return changed


def _overlay(source, case, revision):
    path = case["path"]
    if case["case_id"] == "flask-memory-response-headers":
        old = "rv.headers.update(headers)"
        new = (
            "rv.headers.extend(headers)" if revision == "A" else "rv.headers.update(dict(headers))"
        )
    elif case["case_id"] == "flask-memory-cache-policy":
        old = "if value is None:\n            return None"
        new = (
            "if not value:\n            return 43200"
            if revision == "A"
            else "if not value:\n            return None"
        )
    elif case["case_id"] == "flask-memory-json-context":
        old = "if app:\n        return app.json.dumps(obj, **kwargs)"
        new = (
            "if app:\n        return _json.dumps(obj, **kwargs)"
            if revision == "A"
            else 'if app:\n        options = getattr(dumps, "_remembered_options", None)\n'
            "        if options is None:\n"
            '            options = {"sort_keys": app.json.sort_keys, '
            '"ensure_ascii": app.json.ensure_ascii}\n'
            "            dumps._remembered_options = options\n"
            "        for key, value in options.items():\n"
            "            kwargs.setdefault(key, value)\n"
            "        return app.json.dumps(obj, **kwargs)"
        )
    else:
        raise ValueError("No source overlay for simulated-history case")
    return {**source, path: _replace_function(source[path], case, old, new)}


def load_project_memories() -> list[dict]:
    catalog = json.loads(MEMORIES.read_text())
    rows, limits = catalog["records"], catalog["requirements"]
    linked = sum(bool(row["files"]) for row in rows)
    if (
        catalog["schema_version"] != 1
        or len(rows) != limits["rows"]
        or linked != limits["code_linked"]
        or len(rows) - linked != limits["unlinked"]
        or len({row["key"] for row in rows}) != len(rows)
        or len({row["value"] for row in rows}) != len(rows)
    ):
        raise PermissionError("Project memory counts or identities differ")
    for row in rows:
        if (
            not isinstance(row["value"], str)
            or not row["value"].strip()
            or len(row["value"]) > limits["max_chars"]
            or len(row["value"].split()) > limits["max_words"]
            or not row["key"].startswith("history/")
            or not isinstance(row["files"], list)
            or any(not path.startswith("src/flask/") for path in row["files"])
        ):
            raise PermissionError("Project memory is not a short source-scoped fact")
    return rows


def _history(case, rows):
    """Seed short project facts and revise a few keys after an independent source change."""
    revisions = {"history/" + fact["key"]: fact for fact in case["history_revisions"]}
    if len(revisions) != len(case["history_revisions"]) or not revisions.keys() <= {
        row["key"] for row in rows
    }:
        raise PermissionError("History revision does not address a unique project fact")
    events = []
    for row in rows:
        revision = revisions.get(row["key"])
        events.append(
            dict(
                sequence=len(events) + 1,
                operation="set",
                revision="A",
                key=row["key"],
                value=revision["value_a"] if revision else row["value"],
                files=row["files"],
                evidence=row["evidence"],
            )
        )
    for key, fact in revisions.items():
        prior = next(event for event in events if event["key"] == key)
        if fact["value_b"] != prior["value"] and fact["value_b"] == next(
            row["value"] for row in rows if row["key"] == key
        ):
            events.append(
                dict(
                    sequence=len(events) + 1,
                    operation="update",
                    revision="B",
                    key=key,
                    value=fact["value_b"],
                    files=prior["files"],
                    evidence=prior["evidence"],
                    precondition="native content hash from prior get",
                )
            )
        else:
            raise PermissionError(
                "History update must change a fact to its published current value"
            )
    documents = {}
    current = {event["key"]: event for event in events}
    for event in events:
        if len(event["value"]) > 600 or len(event["value"].split()) > 60:
            raise PermissionError("History revisions must remain short project facts")
        folder = "current" if current[event["key"]] is event else "archive"
        name = event["key"].removeprefix("history/")
        documents[f"project-history/{folder}/{name}.md"] = (
            f"# Project note: {event['key']}\n\n"
            f"Logical revision: {event['revision']}\n"
            f"Public source paths: {', '.join(event['files']) or 'none'}\n\n"
            f"{event['value']}\n"
        )
    return events, documents


def prepare_case(case_id: str, base_source_files: dict[str, str]) -> dict:
    manifest, case = _case(case_id)
    if digest(base_source_files) != manifest["base_source_sha256"]:
        raise PermissionError("Simulated history requires the exact pinned public source")
    if not base_source_files or any(
        not path.startswith("src/flask/") for path in base_source_files
    ):
        raise PermissionError("Only the frozen implementation source is accepted")
    a, b = (_overlay(base_source_files, case, revision) for revision in ("A", "B"))
    rows = load_project_memories()
    for row in rows:
        evidence = row["evidence"]
        if (
            any(path not in base_source_files for path in row["files"])
            or evidence["path"] not in base_source_files
        ):
            raise PermissionError("Project fact references source outside the frozen checkout")
        body = base_source_files[evidence["path"]]
        lines = body.splitlines()
        if (
            hashlib.sha256(body.encode()).hexdigest() != evidence["source_sha256"]
            or not 1 <= evidence["line"] <= len(lines)
            or evidence["anchor"] not in lines[evidence["line"] - 1]
        ):
            raise PermissionError("Project fact evidence differs from the pinned source")
    events, documents = _history(case, rows)
    current = {event["key"]: event for event in events}
    linked = sum(bool(event["files"]) for event in current.values())
    return dict(
        schema_version=1,
        case_id=case_id,
        family="authored-simulated-memory-history",
        simulation="published simulated history; no claim of live learning or elapsed months",
        origin_base_commit=manifest["base_commit"],
        dependency_task_id=manifest["dependency_task_id"],
        problem=case["title"] + "\n" + case["problem"],
        source_revisions={"A": a, "B": b},
        source_sha256={"A": digest(a), "B": digest(b)},
        public_documents=documents,
        public_documents_sha256=digest(documents),
        history_events=events,
        history_events_sha256=digest(events),
        current_revision="B",
        history_counts=dict(
            current_rows=len(current),
            code_linked_rows=linked,
            unlinked_rows=len(current) - linked,
            setup_operations=len(events),
            revision_updates=sum(event["operation"] == "update" for event in events),
        ),
    )
