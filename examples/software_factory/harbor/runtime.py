"""Frozen factory inputs, direct inference and audited read-only Loom access."""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

from examples.software_factory.factory.adapters import SDKContext
from examples.software_factory.factory.anthropic_provider import accounting_usage
from examples.software_factory.factory.model import TRANSPORT_TIMEOUT_S, digest
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


def backend(protocol, task, path, *, slot, binding=None):
    from loom import Agent

    from .scopes import binding_for

    selected = binding_for(protocol, slot)
    if binding is not None and binding != selected:
        raise PermissionError("Supplied binding differs from the frozen attempt")
    binding = selected
    index_tier = slot["index_tier"]
    if index_tier not in {"treesitter", "scip"}:
        raise PermissionError("Baseline cannot construct a Loom client")
    if (
        task.task_id != slot["task_id"]
        or task.namespace != binding["code_namespace"]
        or task.base_commit != binding["base_commit"]
    ):
        raise PermissionError("Task and selected code scope differ")
    from .scopes import frozen_json

    proof = frozen_json(protocol["index_certificate_file"], protocol["index_certificate_sha256"])[
        "scopes"
    ][task.task_id + ":" + index_tier]
    if proof.get("repo") != task.repository:
        raise PermissionError("Index repository differs from task source")
    source_proof = binding["source_scope"]
    expected_source = dict(
        base_commit=task.base_commit,
        source_sha256=task.source_sha256,
        paths_sha256=digest(sorted(task.files)),
        file_count=len(task.files),
    )
    if any(source_proof.get(key) != value for key, value in expected_source.items()):
        raise PermissionError("Selected index source coverage differs from public task source")
    audit = TransportAudit(
        max_requests=protocol.get("max_physical_loom_requests"),
        record=lambda row: append(path, row),
    )
    token = Path(binding["token_file"]).read_text().strip()
    if not token:
        raise PermissionError("Attempt token is empty")
    scope = dict(
        org=binding["org"],
        namespace=binding["code_namespace"],
        repository=task.repository,
        commit=task.base_commit,
    )
    verifier = load_profile(protocol).ScopeVerifier(
        scope,
        index_repository=task.repository,
        build_commit=protocol["server_commit"],
        certificate_sha256=protocol["index_certificate_sha256"],
        index_tier=index_tier,
        compiler_rows=binding["compiler_rows"],
    )
    agent = Agent(
        org=binding["org"],
        namespace=binding["code_namespace"],
        agent="factory-pilot",
        base_url=protocol["loom_url"],
        api_key=token,
        auto_run=False,
        mentor_brief=False,
        coalesce_sets=False,
        pacing=False,
        retry={"max_attempts": 1},
        timeout_s=TRANSPORT_TIMEOUT_S,
    )
    agent.transport._client.event_hooks["request"].append(audit.request)
    agent.transport._client.event_hooks["response"].append(audit.response)
    from examples.software_factory.factory.retrieval_tools import OPERATIONS

    return SDKContext(
        agent, verifier, audit.count, expected_scope=scope, allowed_ops=OPERATIONS
    ), audit


def memory_backend(protocol, slot, binding, code_context):
    from .memory import NativeMemoryContext, validate_history_certificate
    from .scopes import binding_for

    if not slot["memory_enabled"] or binding_for(protocol, slot) != binding:
        raise PermissionError("Attempt does not have a native memory binding")
    validate_history_certificate(protocol, slot, binding)
    expected = {
        "org": binding["org"],
        "namespace": binding["code_namespace"],
        "commit": binding["base_commit"],
    }
    if any(code_context.expected_scope.get(key) != value for key, value in expected.items()):
        raise PermissionError("Memory and code client scopes differ")
    return NativeMemoryContext(
        code_context, org=binding["org"], memory_namespace=binding["memory_namespace"]
    )


def episode_notes(protocol, slot, folder):
    from examples.software_factory.factory.project_notes import ProjectNotes

    from .scopes import binding_for, frozen_json

    binding = binding_for(protocol, slot)
    frozen = protocol["memory_histories"][slot["task_id"]]
    fixture = frozen_json(frozen["fixture_file"], frozen["fixture_sha256"])
    documents = fixture["public_documents"]
    if not isinstance(documents, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in documents.items()
    ):
        raise PermissionError("Public project documents are invalid")
    if slot["memory_enabled"] and digest(documents) != binding["memory_history_sha256"]:
        raise PermissionError("Ordinary project notes differ from native memory history")
    path = Path(folder) / "project-notes.json"
    if path.exists() or path.is_symlink():
        raise PermissionError("Each attempt needs a fresh mutable project-notes store")
    return ProjectNotes(path, documents)


def task_inputs(protocol, task_id, index_tier=None, *, slot=None, binding=None):
    if slot is not None:
        from .scopes import binding_for

        selected = binding_for(protocol, slot)
        if (
            slot["task_id"] != task_id
            or slot["index_tier"] != index_tier
            or (binding is not None and binding != selected)
        ):
            raise PermissionError("Task inputs differ from scheduled binding")
        binding = selected
    elif index_tier is not None or binding is not None:
        raise PermissionError("Live task inputs require an explicit scheduled binding")
    if slot is not None and slot["mode"] == "baseline":
        binding = None
    return load_profile(protocol).task_inputs(protocol, task_id, binding=binding)
