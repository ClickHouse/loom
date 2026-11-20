"""Authored public-behavior checks whose assertions execute only on the host."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

from .model import digest
from .observations import CandidateFailure, DockerObservations
from .workspace import GradingBudgetExceeded


@dataclass(frozen=True)
class Probe:
    key: str
    request: dict
    expected: dict
    visible: bool


def probe_suite(task_id: str, seed: str) -> list[Probe]:
    if not re.fullmatch(r"[a-f0-9]{64}", seed):
        raise ValueError("A frozen private 32-byte probe seed is required")
    token = hashlib.sha256((task_id + seed).encode()).hexdigest()[:16]
    definitions = {
        "pallets__flask-4045": blueprint_probes,
        "pallets__flask-4575": redirect_probes,
        "pallets__flask-4935": route_probes,
    }
    if task_id not in definitions:
        raise ValueError("No authored behavioral coverage for this historical task")
    probes = []
    for index, (op, args, expected, visible) in enumerate(definitions[task_id](token)):
        key = hashlib.sha256(f"{task_id}:{seed}:{index}".encode()).hexdigest()[:32]
        probes.append(Probe(key, {"id": key, "op": op, "args": args}, expected, visible))
    return probes


def blueprint_probes(token: str):
    for name in ("bp_" + token, "bp-" + token):
        yield "blueprint", {"name": name}, {"name": name}, True
    for name in (token + ".leaf", "." + token, token + "."):
        yield "blueprint", {"name": name}, {"error": "ValueError"}, False


def redirect_probes(token: str):
    for call, context, override, code in (
        ("helper", False, None, 302),
        ("helper", True, None, 307),
        ("method", True, None, 301),
        ("helper", True, token, 303),
    ):
        args = {
            "call": call,
            "context": context,
            "override": override,
            "code": code,
            "location": "/" + token,
            "body": "body-" + token,
        }
        expected = {
            "status": code,
            "location": args["location"],
            "override": override,
            "body": args["body"] if override else None,
        }
        yield "redirect", args, expected, call == "helper" and not override


def route_probes(token: str):
    for nested, parent, child, registration, prefix in (
        (False, None, None, None, ""),
        (True, token, None, None, ""),
        (True, None, "c" + token, None, ""),
        (True, token, "c" + token, None, ""),
        (True, None, None, token, ""),
        (True, None, None, None, "/prefix"),
    ):
        subdomain = ".".join(x for x in (child, registration or parent) if x)
        host = (subdomain + "." if subdomain else "") + "factory.test"
        path = "/" + token
        args = {
            "nested": nested,
            "parent": parent,
            "child": child,
            "registration": registration,
            "prefix": prefix,
            "path": path,
            "request_path": prefix + path,
            "host": host,
            "body": "body-" + token,
        }
        expected = {"url": "http://" + host + prefix + path, "status": 200, "body": args["body"]}
        yield "routes", args, expected, not nested or not subdomain


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate observation field")
        result[key] = value
    return result


def reject_constant(value):
    raise ValueError("Nonfinite observation scalar: " + value)


def assess(probes: list[Probe], raw: bytes) -> dict:
    if not raw or len(raw) > 65536:
        raise ValueError("Missing or oversized observations")
    try:
        rows = json.loads(raw, object_pairs_hook=unique_object, parse_constant=reject_constant)
        if not isinstance(rows, list) or len(rows) != len(probes):
            raise ValueError("Incomplete observations")
        expected = {p.key: p.expected for p in probes}
        actual = {}
        for row in rows:
            if (
                not isinstance(row, dict)
                or set(row) != {"id", "value"}
                or type(row["id"]) is not str
                or row["id"] not in expected
                or row["id"] in actual
                or not isinstance(row["value"], dict)
            ):
                raise ValueError("Unknown, duplicate or malformed observation")
            # Canonical JSON also distinguishes scalar types and refuses NaN.
            actual[row["id"]] = digest(row["value"]) == digest(expected[row["id"]])
        return {"passed": all(actual.values()), "selected": actual, "checks": len(probes)}
    except (TypeError, RecursionError, UnicodeDecodeError) as error:
        raise ValueError("Malformed observations") from error


def failed_observations(probes: list[Probe], reason: str, detail: str) -> dict:
    return {
        "passed": False,
        "selected": {p.key: False for p in probes},
        "checks": len(probes),
        "candidate_error": {"reason": reason, "detail": detail[:4096]},
    }


class BehavioralGrader:
    result_authority = "host"

    def __init__(self, source, task_id: str, image: str, seed: str, output: Path):
        self.base = source.files("src/flask")
        self.probes = probe_suite(task_id, seed)
        self.image, self.output, self.calls = image, output, 0
        code = {
            name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
            for name in (
                "behavioral.py",
                "bridge.py",
                "observations.py",
                "historical.py",
                "cleanup.py",
            )
        }
        self.sha256 = digest(
            {
                "kind": "authored-host-behavioral-v1",
                "task_id": task_id,
                "source": digest(self.base),
                "base": source.commit,
                "image": image,
                "seed": seed,
                "code": code,
            }
        )

    def run(self, checkout, *, hidden: bool = False) -> dict:
        if checkout.original != self.base:
            raise PermissionError("Candidate base differs from the behavioral controls")
        if not hidden and self.calls >= 3:
            raise GradingBudgetExceeded("Visible observation budget exhausted")
        if not hidden:
            self.calls += 1
        out = self.output / ("hidden" if hidden else f"visible-{self.calls}")
        workspace = out / "workspace"
        workspace.mkdir(parents=True, exist_ok=False, mode=0o700)
        for name, content in checkout.files().items():
            target = workspace / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        probes = self.probes if hidden else [p for p in self.probes if p.visible]
        try:
            raw = DockerObservations(self.image, out / "observations").run(
                workspace, [p.request for p in probes]
            )
        except CandidateFailure as error:
            result = failed_observations(probes, error.reason, error.detail)
        else:
            try:
                result = assess(probes, raw)
            except ValueError as error:
                result = failed_observations(probes, "malformed_observations", str(error))
        result.update(
            result_authority="host",
            state=checkout.state(),
            grader_sha256=self.sha256,
            coverage="authored-public-behavior-not-official-pytest",
        )
        (out / "host-grade.json").write_text(json.dumps(result, indent=2) + "\n")
        return {k: v for k, v in result.items() if not hidden or k != "selected"}
