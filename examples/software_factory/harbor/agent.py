"""Harbor external agent for the factory's source-tool loop."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from dotenv import dotenv_values
from harbor.agents.base import BaseAgent
from harbor.agents.capabilities import AgentCapabilities
from harbor.models.agent.context import AgentContext
from harbor.models.trajectories.trajectory import Trajectory

from examples.software_factory.factory.anthropic_provider import AnthropicProvider, accounting_usage
from examples.software_factory.factory.engine import Factory
from examples.software_factory.factory.memory_tools import MemoryTools
from examples.software_factory.factory.model import Budget, digest
from examples.software_factory.harbor.runtime import (
    ObservedProvider,
    backend,
    memory_backend,
    native_audit,
    read,
    task_inputs,
    verify,
    write,
)

from .profiles import load_profile
from .scopes import binding_for


def envelope(factory: Factory, result: dict | None = None) -> dict:
    result = result or {}
    return {
        "files": factory.checkout.files(),
        "source_sha256": factory.task.source_sha256,
        "state": factory.checkout.state(),
        "agent_completed": result.get("status") == "completed",
        "visible_passed": result.get("visible_passed", False),
        "verified_state": result.get("verified_state"),
    }


def trajectory(factory: Factory, result: dict, model: str) -> dict:
    steps = []
    calls = {}
    for message in factory.messages:
        for block in message["content"]:
            step = {
                "step_id": len(steps) + 1,
                "source": "agent" if message["role"] == "assistant" else "user",
                "message": block.get("text", ""),
            }
            if "toolUse" in block:
                call = block["toolUse"]
                step["tool_calls"] = [
                    {
                        "tool_call_id": call["toolUseId"],
                        "function_name": call["name"],
                        "arguments": call["input"],
                    }
                ]
                calls[call["toolUseId"]] = step
            elif "toolResult" in block:
                observation = block["toolResult"]
                calls[observation["toolUseId"]]["observation"] = {
                    "results": [
                        {
                            "source_call_id": observation["toolUseId"],
                            "content": json.dumps(observation["content"]),
                        }
                    ]
                }
                continue
            steps.append(step)
    document = {
        "schema_version": "ATIF-v1.8",
        "agent": {"name": "factory", "version": "1", "model_name": model},
        "steps": steps,
        "extra": {
            "mode": result["mode"],
            "loom_selected_calls": result["model_selected_loom_requests"],
        },
    }
    return Trajectory.model_validate(document).to_json_dict()


class FactoryAgent(BaseAgent):
    capabilities = AgentCapabilities(atif=True)

    def __init__(self, *args, protocol_path: str, qualification: str | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.protocol_path, self.qualification = Path(protocol_path), qualification

    @staticmethod
    def name():
        return "factory"

    def version(self):
        return "1"

    async def setup(self, environment):
        pass

    async def run(self, instruction: str, environment, context: AgentContext):
        protocol = read(self.protocol_path)
        verify(protocol)
        descriptor = json.loads(instruction)
        order = descriptor["schedule_order"]
        slot = protocol["schedule"][order]
        binding = None if self.qualification else binding_for(protocol, slot)
        config, source, task, private = task_inputs(
            protocol,
            slot["task_id"],
            None if self.qualification else slot["index_tier"],
            slot=None if self.qualification else slot,
            binding=binding,
        )
        spec = protocol["harbor_specs"][task.task_id]
        if (
            digest(task.read_only_files) != spec["read_only_sha256"]
            or task.read_only_roots != spec["read_only_roots"]
        ):
            raise PermissionError("Read-only repository evidence differs from frozen source")
        if descriptor != {"schedule_order": order, "problem": task.problem}:
            raise PermissionError("Harbor instruction differs from the frozen task")
        if self.model_name != protocol["model_id"]:
            raise PermissionError("Harbor model differs from the approved model")
        folder = self.logs_dir
        folder.mkdir(parents=True, exist_ok=True)
        profile = load_profile(protocol)
        grader = profile.VisibleGrader(
            environment, asyncio.get_running_loop(), protocol["harbor_specs"][task.task_id], folder
        )

        def execute():
            retrieval = audit = provider = None
            try:
                notes = None
                memory = None
                if not self.qualification and slot["mode"] != "baseline":
                    retrieval, audit = backend(
                        protocol,
                        task,
                        folder / "loom-transport.jsonl",
                        slot=slot,
                        binding=binding,
                    )
                    if slot["memory_enabled"]:
                        memory = MemoryTools(
                            memory_backend(protocol, slot, binding, retrieval),
                            org=binding["org"],
                            namespace=binding["memory_namespace"],
                            agent="factory-pilot",
                            user_id="-",
                            session_id="-",
                            code_namespaces=(binding["code_namespace"],),
                            allowed_paths=tuple(task.files),
                            repository_tag=binding["repository_tag"],
                            history_sha256=binding["memory_history_sha256"],
                        )
                if self.qualification:
                    control_inputs = private
                    if self.qualification == "reference":
                        reference = next(
                            row
                            for row in read(protocol["reference_controls_file"])
                            if row["instance_id"] == task.task_id
                        )
                        if any(reference[k] != private[k] for k in ("base_commit", "test_patch")):
                            raise PermissionError("Reference and evaluator provenance differ")
                        control_inputs = reference
                    provider = profile.ControlProvider(
                        source, task, control_inputs, self.qualification
                    )
                else:
                    inference = protocol["inference"]
                    key = dotenv_values(inference["credentials_file"]).get("ANTHROPIC_API_KEY", "")
                    provider = AnthropicProvider(
                        key,
                        protocol["model_id"],
                        approved_model_id=protocol["model_id"],
                        base_url=inference["base_url"],
                        role="solver",
                        order=order,
                    )
                    native_audit(provider, folder / "provider-transport.jsonl")
                    provider = ObservedProvider(provider, folder / "generation-journal.jsonl")
                factory = Factory(
                    task,
                    provider,
                    grader,
                    folder / "factory",
                    mode="baseline" if self.qualification else slot["mode"],
                    backend=retrieval,
                    budget=Budget(**protocol["attempt_budget"]),
                    org="-" if self.qualification else binding["org"],
                    project_notes=notes,
                    memory=memory,
                )
                grader.factory = factory
                result = factory.run(external_verifier=True)
                result.update(
                    schedule_order=order,
                    arm="baseline" if self.qualification else slot["arm"],
                    index_tier=None if self.qualification else slot["index_tier"],
                    actual_org="-" if self.qualification else binding["org"],
                    code_namespace="-" if self.qualification else binding["code_namespace"],
                    pairing_key=[slot["task_id"], slot["repeat"]],
                    protocol_sha256=digest(protocol),
                    source_sha256=task.source_sha256,
                    loom_transport_dispatches=audit.count() if audit else 0,
                )
                if not self.qualification and result["memory_enabled"] != slot["memory_enabled"]:
                    raise PermissionError("Executed memory capability differs from scheduled arm")
                if (self.qualification or slot["mode"] == "baseline") and any(
                    result[k] != 0
                    for k in (
                        "model_selected_loom_requests",
                        "loom_logical_requests",
                        "loom_physical_requests",
                        "loom_transport_dispatches",
                    )
                ):
                    raise PermissionError("Baseline received Loom exposure")
                write(folder / "factory-result.json", result)
                write(folder / "trajectory.json", trajectory(factory, result, protocol["model_id"]))
                return result, envelope(factory, result)
            finally:
                if retrieval is not None:
                    retrieval.close()
                if isinstance(provider, ObservedProvider):
                    provider.provider.close()

        result, candidate = await asyncio.to_thread(execute)
        candidate_path = folder / "candidate.json"
        write(candidate_path, candidate)
        await environment.exec("mkdir -p /logs/artifacts", timeout_sec=10)
        await environment.upload_file(candidate_path, "/logs/artifacts/candidate.json")
        context.cost_usd = result["generation_cost_usd"]
        if result["provider_failed_requests"] == 0:
            usage = [accounting_usage({"usage": u}) for u in result["provider_usage"]]
            context.n_input_tokens = sum(u["inputTokens"] for u in usage)
            context.n_output_tokens = sum(u["outputTokens"] for u in usage)
            context.n_cache_tokens = sum(
                u.get("cache_read_input_tokens", 0) for u in result["provider_usage"]
            )
        context.metadata = {
            "schedule_order": order,
            "arm": slot["arm"],
            "factory": result,
            "cost_basis": (
                "generation list-price estimate; Loom and infrastructure reported separately"
            ),
        }
