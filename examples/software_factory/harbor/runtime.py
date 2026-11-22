"""Frozen task inputs and separate posthoc judge accounting."""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

from examples.software_factory.factory.anthropic_provider import accounting_usage
from examples.software_factory.factory.model import digest

from .pricing import MODELS, rates
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
    if protocol["model_id"] not in MODELS or protocol["index_tier"] != "per-arm":
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
            input_rate, output_rate = rates(self.provider.model_id, incoming)
            cost = float((incoming * input_rate + outgoing * output_rate) / 1000000)
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
