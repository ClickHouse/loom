"""Replay and injected live clients. Importing this module never opens a connection."""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path

from examples.software_factory.factory.model import Task


class _RetrievalInputError(ValueError):
    """A selected operation rejected model input after its scope was checked."""

    physical_requests: int = 0


class ReplayProvider:
    label = "scripted-replay"

    def __init__(self, path: Path):
        self.steps = iter(json.loads(path.read_text())["steps"])

    def step(self, messages: list[dict], tools: list[dict], max_tokens: int) -> dict:
        step = next(self.steps, {"plan": "Replay exhausted", "actions": []})
        blocks = [{"text": step.get("plan", "")}]
        blocks += [
            {"toolUse": {"toolUseId": str(i), "name": a["name"], "input": a.get("input", {})}}
            for i, a in enumerate(step.get("actions", []))
        ]
        return {
            "blocks": blocks,
            "usage": {},
            "physical_requests": 0,
            "cost_usd": 0,
            "cost_status": "offline",
            "provider_request_id": None,
        }


class ReplayContext:
    label = "fixture-replay"

    def __init__(self, task: Task, org: str = "fixture"):
        self.task, self.org = task, org

    def request(self, op: str, body: dict) -> dict:
        definitions = [
            {
                "path": path,
                "start_line": 1,
                "end_line": len(source.splitlines()),
                "body": source.rstrip("\n"),
            }
            for path, source in self.task.files.items()
        ]
        return {
            "data": {
                "definitions": definitions,
            },
            "scope": {
                "org": self.org,
                "namespace": self.task.namespace,
                "repository": self.task.repository,
                "commit": self.task.base_commit,
            },
            "permission": "allowed",
            "physical_requests": 0,
            "retries": 0,
            "cost_usd": 0,
            "cost_status": "offline",
            "backend": self.label,
            "usage": {},
            "request_id": None,
        }


class BedrockProvider:
    """Use an explicitly configured boto3 converse client with SDK retries disabled.

    The caller owns client construction, pricing and authorization. This adapter
    is unavailable from the offline CLI. Model ids convey no inferred capabilities.
    """

    label = "bedrock-converse"

    def __init__(self, client, model_id: str, *, approved_model_id: str, physical_counter=None):
        if model_id != approved_model_id or not model_id:
            raise PermissionError("Model must match the frozen approved protocol")
        if client.meta.config.retries.get("total_max_attempts") != 1:
            raise ValueError("Provider client must disable SDK retries")
        if client.meta.config.read_timeout > 45 or client.meta.config.connect_timeout > 5:
            raise ValueError("Provider timeout exceeds the bounded protocol")
        self.client, self.model_id = client, model_id
        self.physical_counter = physical_counter

    def step(self, messages: list[dict], tools: list[dict], max_tokens: int) -> dict:
        specs = [
            {
                "toolSpec": {
                    "name": t["name"],
                    "description": t["description"],
                    "inputSchema": {"json": t["input_schema"]},
                }
            }
            for t in tools
        ]
        before = self.physical_counter() if self.physical_counter else None
        try:
            response = self.client.converse(
                modelId=self.model_id,
                messages=copy.deepcopy(messages),
                toolConfig={"tools": specs},
                inferenceConfig={"maxTokens": max_tokens},
            )
        except Exception as error:  # Retain failed dispatch counts.
            failure = RuntimeError(f"{type(error).__name__}: {error}")
            failure.physical_requests = (
                self.physical_counter() - before if before is not None else None
            )
            raise failure from error
        return {
            "blocks": response["output"]["message"]["content"],
            "usage": response.get("usage", {}),
            "physical_requests": self.physical_counter() - before if before is not None else None,
            "provider_request_id": response.get("ResponseMetadata", {}).get("RequestId"),
            "cost_usd": None,
            "cost_status": "unknown",
        }


class SDKContext:
    """Call the existing Loom Agent; require an independently frozen scope verifier.

    code.symbol has no arbitrary historical commit selector. A verifier must check
    the authorized immutable namespace watermark before AND after each request,
    returning a receipt with every physical check and its cost. Unverified indexes
    fail closed. No credentials, namespace creation or ingestion live here.
    """

    label = "loom-sdk"

    def __init__(
        self, agent, verify_scope, physical_counter, *, expected_scope: dict, allowed_ops=None
    ):
        if agent.auto_run or agent.mentor_brief:
            raise ValueError("Ambient SDK calls must be disabled")
        if set(expected_scope) != {"org", "namespace", "repository", "commit"} or not all(
            isinstance(v, str) and v for v in expected_scope.values()
        ):
            raise ValueError("Index scope must name org, namespace, repository and commit")
        commit = expected_scope["commit"]
        if len(commit) != 40 or any(c not in "0123456789abcdef" for c in commit):
            raise ValueError("Index scope requires a full git commit")
        self.agent, self.verify_scope = agent, verify_scope
        self.physical_counter = physical_counter
        self.expected_scope = dict(expected_scope)
        from examples.software_factory.factory.retrieval_tools import OPERATIONS

        self.allowed_ops = frozenset(OPERATIONS if allowed_ops is None else allowed_ops)
        self.runner = asyncio.Runner()

    def close(self) -> None:
        try:
            if hasattr(self.agent, "__aexit__"):
                self.runner.run(self.agent.__aexit__(None, None, None))
        finally:
            self.runner.close()

    async def _request(self, op: str, body: dict) -> dict:
        from loom.errors import ValidationError

        physical_before = self.physical_counter()
        before = await self.verify_scope(self.agent, body)
        if (
            before.get("permission") != "allowed"
            or before.get("scope") != self.expected_scope
            or any(body.get(k) != self.expected_scope.get(k) for k in ("org", "namespace"))
        ):
            raise PermissionError("Index authorization is unverified")
        try:
            result = await self.agent.call_with_meta(op, body, retry={"max_attempts": 1})
        except ValidationError as error:
            if error.category != "input":
                raise
            await self._after_scope(before, body)
            raise _RetrievalInputError(str(error)) from error
        if result.data.get("ignored_args"):
            raise PermissionError("Index ignored request arguments")
        after = await self._after_scope(before, body)
        return {
            "data": result.data,
            "scope": before["scope"],
            "permission": "allowed",
            "request_id": result.request_id,
            "backend": self.label,
            "retries": 0,
            "physical_requests": self.physical_counter() - physical_before,
            "scope_checks": [before, after],
            "cost_usd": None,
            "cost_status": "unknown",
            "usage": {"retrieval": "requires server provider ledger"},
        }

    async def _after_scope(self, before: dict, body: dict) -> dict:
        after = await self.verify_scope(self.agent, body)
        if before["scope"] != after.get("scope") or after.get("permission") != "allowed":
            raise PermissionError("Index scope changed during lookup")
        return after

    def request(self, op: str, body: dict) -> dict:
        if op not in self.allowed_ops:
            raise ValueError("Operation is outside the declared retrieval policy")
        before = self.physical_counter()
        try:
            return self.runner.run(self._request(op, body))
        except _RetrievalInputError as error:
            error.physical_requests = self.physical_counter() - before
            raise
        except Exception as error:  # Failed SDK attempts must retain physical request counts.
            failure = RuntimeError(f"{type(error).__name__}: {error}")
            failure.physical_requests = self.physical_counter() - before
            raise failure from error
