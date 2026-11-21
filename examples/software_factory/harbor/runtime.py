"""Frozen factory inputs, direct inference and audited read-only Loom access."""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

from examples.software_factory.factory.adapters import SDKContext
from examples.software_factory.factory.anthropic_provider import accounting_usage
from examples.software_factory.factory.model import digest
from examples.software_factory.harbor.live_support import TransportAudit

from .profiles import load_profile


def read(path):
    return json.loads(Path(path).read_text())


def write(path, data):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w") as stream:
        stream.write(json.dumps(data, indent=2) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def append(path, row):
    with Path(path).open("a") as stream:
        stream.write(json.dumps(row, sort_keys=True) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def verify(protocol):
    if protocol.get("controller") != "harbor" or protocol.get("harbor_version") != "0.27.0":
        raise PermissionError("Use the pinned native Harbor controller")
    if digest(protocol["schedule"]) != protocol["schedule_sha256"]:
        raise PermissionError("Schedule differs from the frozen protocol")
    for path, expected in protocol["files"].items():
        if hashlib.sha256(Path(path).read_bytes()).hexdigest() != expected:
            raise PermissionError("A frozen runtime or evaluation input changed: " + path)
    if protocol["model_id"] != "claude-haiku-5-5" or protocol["index_tier"] != "per-arm":
        raise PermissionError("Model or index tier differs from the qualified protocol")


class ObservedProvider:
    """Persist each generation intent and actual usage, without a spending gate."""

    def __init__(self, provider, path):
        self.provider, self.path, self.label = provider, path, provider.label

    def step(self, messages, tools, max_tokens):
        append(
            self.path,
            {
                "phase": "intent",
                "time": time.monotonic(),
                "messages_sha256": digest(messages),
                "tools_sha256": digest(tools),
            },
        )
        try:
            response = self.provider.step(messages, tools, max_tokens)
        except Exception as error:  # Preserve failed dispatches without replaying them.
            append(
                self.path,
                {
                    "phase": "failed",
                    "error_type": type(error).__name__,
                    "physical_requests": getattr(error, "physical_requests", None),
                    "request_id": getattr(error, "provider_request_id", None),
                },
            )
            raise
        append(self.path, {"phase": "observed", "response": response})
        cost = None
        try:
            usage = accounting_usage(response)
            incoming, outgoing = usage["inputTokens"], usage["outputTokens"]
            input_rate, output_rate = (0.1, 0.5) if incoming <= 100000 else (0.5, 2.5)
            cost = (incoming * input_rate + outgoing * output_rate) / 1000000
        except (ValueError, KeyError, TypeError):
            pass  # Raw usage remains available when the frozen pricing rule cannot price it.
        return {
            **response,
            "generation_cost_usd": cost,
            "generation_cost_status": "list_price" if cost is not None else "unknown",
        }


def native_audit(provider, path):
    def request(item):
        append(
            path,
            {
                "phase": "dispatch",
                "time": time.monotonic(),
                "path": item.url.path,
                "body_sha256": hashlib.sha256(item.content).hexdigest(),
                "payload": json.loads(item.content),
            },
        )

    def response(item):
        item.read()
        try:
            data = item.json()
        except ValueError:
            data = {}
        append(
            path,
            {
                "phase": "response",
                "time": time.monotonic(),
                "path": item.request.url.path,
                "status": item.status_code,
                "request_id": item.headers.get("request-id"),
                "usage": data.get("usage"),
                "error": data.get("error"),
            },
        )

    provider.client.event_hooks["request"].append(request)
    provider.client.event_hooks["response"].append(response)


def backend(protocol, task, path, *, index_tier):
    from loom import Agent

    audit = TransportAudit(
        max_requests=protocol.get("max_physical_loom_requests", 18),
        record=lambda row: append(path, row),
    )
    if index_tier not in {"treesitter", "scip"}:
        raise ValueError("Loom backend requires an explicit treatment index tier")
    index = protocol["tasks"][task.task_id]["indexes"][index_tier]
    if task.namespace != index["namespace"]:
        raise PermissionError("Task and selected index namespaces differ")
    certificate_path = Path(protocol["index_certificate_file"])
    if (
        hashlib.sha256(certificate_path.read_bytes()).hexdigest()
        != protocol["index_certificate_sha256"]
    ):
        raise PermissionError("Index certificate differs from the frozen protocol")
    proof = read(certificate_path)["scopes"][task.task_id + ":" + index_tier]
    expected = dict(
        org=protocol["org"],
        namespace=task.namespace,
        repo=task.repository,
        git_head=task.base_commit,
        build_commit=protocol["server_commit"],
    )
    if (
        any(proof.get(k) != v for k, v in expected.items())
        or proof.get("compiler_rows") != index["compiler_rows"]
    ):
        raise PermissionError("Selected index scope or compiler proof differs from certificate")
    source_proof = index.get("source_scope")
    expected_source = dict(
        base_commit=task.base_commit,
        source_sha256=task.source_sha256,
        paths_sha256=digest(sorted(task.files)),
        file_count=len(task.files),
    )
    if (
        not isinstance(source_proof, dict)
        or source_proof.get("verified") is not True
        or any(source_proof.get(k) != v for k, v in expected_source.items())
        or proof.get("source_scope") != source_proof
    ):
        raise PermissionError("Selected index source coverage differs from public task source")
    token = Path(index["reader_token_file"]).read_text().strip()
    agent = Agent(
        org=protocol["org"],
        namespace=task.namespace,
        agent="factory-pilot",
        base_url=protocol["loom_url"],
        api_key=token,
        auto_run=False,
        mentor_brief=False,
        coalesce_sets=False,
        pacing=False,
        retry={"max_attempts": 1},
        timeout_s=10,
    )
    agent.transport._client.event_hooks["request"].append(audit.request)
    agent.transport._client.event_hooks["response"].append(audit.response)
    scope = {
        "org": protocol["org"],
        "namespace": task.namespace,
        "repository": task.repository,
        "commit": task.base_commit,
    }
    verifier = load_profile(protocol).ScopeVerifier(
        scope,
        index_repository=task.repository,
        build_commit=protocol["server_commit"],
        certificate_sha256=protocol["index_certificate_sha256"],
        index_tier=index_tier,
        compiler_rows=index["compiler_rows"],
    )
    from examples.software_factory.factory.retrieval_tools import OPERATIONS

    return SDKContext(
        agent,
        verifier,
        audit.count,
        expected_scope=scope,
        allowed_ops=OPERATIONS,
    ), audit


def task_inputs(protocol, task_id, index_tier=None):
    return load_profile(protocol).task_inputs(protocol, task_id, index_tier)
