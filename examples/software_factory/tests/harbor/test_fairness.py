"""Treatment configuration uses native MCP and the same coding agent."""

import hashlib
import json
import tempfile
import tomllib
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

import httpx

from examples.software_factory.factory.model import digest
from examples.software_factory.harbor.fairness import audit_fairness, native_mcp_available
from examples.software_factory.harbor.index import TABLES
from examples.software_factory.harbor.mcp_qualification import qualified_catalog, qualify
from examples.software_factory.harbor.native_environment import agent_settings, mcp_settings
from examples.software_factory.harbor.project_guidance import guidance, stage, task_instruction
from examples.software_factory.harbor.runtime import write


class NativeTreatment(TestCase):
    def test_native_mcp_availability_requires_startup_evidence(self):
        for configured, status, tools, passed in (
            (True, "connected", 23, True),
            (True, "connected", 0, False),
            (True, "failed", 23, False),
            (True, "disabled", 0, False),
            (True, None, None, False),
            (False, "disabled", 0, True),
            (False, "connected", 23, False),
            (False, "disabled", False, False),
        ):
            with self.subTest(configured=configured, status=status, tools=tools):
                result = {
                    "loom_connection_at_start": status,
                    "loom_tools_advertised": tools,
                    "first_loom_source_result_event_main": None,
                }
                self.assertIs(native_mcp_available(result, configured=configured), passed)

    def test_mcp_startup_counts_measure_advertised_bytes_and_loading_hints(self):
        instructions = "Use loom_code_grep for café"
        eager = {
            "name": "loom_code_grep",
            "description": "Find café",
            "_meta": {"anthropic/alwaysLoad": True},
        }
        tools = [eager, {"name": "loom_code_symbol"}, {"name": "loom_code_search"}]
        responses = {
            "initialize": {"protocolVersion": "2025-03-26", "instructions": instructions},
            "tools/list": {"tools": tools},
            "tools/call": {"content": [{"text": "src/flask/blueprints.py"}]},
        }

        def serve(request):
            data = json.loads(request.content)
            if "id" not in data:
                return httpx.Response(202)
            return httpx.Response(200, json={"id": data["id"], "result": responses[data["method"]]})

        binding = {"org": "org", "code_namespace": "code", "memory_namespace": None}
        with httpx.Client(transport=httpx.MockTransport(serve)) as client:
            receipt = qualify(
                client, "http://mock", "mock-token", binding, schema_profile="compact"
            )
        self.assertEqual(receipt["instructions_chars"], len(instructions))
        self.assertEqual(receipt["loom_mcp_instruction_bytes"], len(instructions.encode()))
        self.assertEqual(receipt["loom_mcp_upfront_hint_tool_count"], 1)
        self.assertEqual(
            receipt["loom_mcp_upfront_hint_descriptor_bytes"],
            len(json.dumps(eager, ensure_ascii=False, separators=(",", ":")).encode()),
        )
        self.assertEqual(
            receipt["loom_mcp_catalog_bytes"],
            len(json.dumps(tools, ensure_ascii=False, separators=(",", ":")).encode()),
        )

    def test_compact_qualification_preserves_the_roster_and_can_defer_search(self):
        standard = {
            "tools_sha256": "a" * 64,
            "tool_names": ["loom_code_grep"],
            "tool_loading_sha256": "b" * 64,
        }
        compact = {**standard, "tools_sha256": "c" * 64}
        self.assertEqual(
            qualified_catalog("compact", {"0": compact, "2": compact}, standard),
            {
                "schema_profile": "compact",
                "tool_catalog_sha256": "c" * 64,
                "standard_catalog_sha256": "a" * 64,
            },
        )
        standard_with_search = {
            **standard,
            "tool_names": ["loom_code_grep", "loom_code_search"],
            "upfront_tool_names": ["loom_code_grep", "loom_code_search"],
        }
        compact_deferred = {
            **compact,
            "tool_names": standard_with_search["tool_names"],
            "upfront_tool_names": ["loom_code_grep"],
            "tool_loading_sha256": "d" * 64,
        }
        qualified_catalog("compact", {"0": compact_deferred}, standard_with_search)
        standard_with_edit = {
            **standard_with_search,
            "tool_names": [
                "loom_code_edit_context",
                "loom_code_grep",
                "loom_code_search",
                "loom_code_symbol",
            ],
            "upfront_tool_names": ["loom_code_grep", "loom_code_search", "loom_code_symbol"],
        }
        compact_with_edit = {
            **compact_deferred,
            "tool_names": standard_with_edit["tool_names"],
            "upfront_tool_names": ["loom_code_edit_context", "loom_code_grep"],
        }
        qualified_catalog("compact", {"0": compact_with_edit}, standard_with_edit)
        with self.assertRaises(PermissionError):
            qualified_catalog(
                "compact",
                {"0": {**compact_with_edit, "upfront_tool_names": ["loom_code_grep"]}},
                standard_with_edit,
            )
        for attempts in (
            {"0": standard},
            {"0": compact, "2": standard},
            {"0": {**compact, "tool_loading_sha256": "d" * 64}},
        ):
            with self.subTest(attempts=attempts), self.assertRaises(PermissionError):
                qualified_catalog("compact", attempts, standard)

    def test_baseline_has_no_mcp_or_extra_prompt_and_loom_scope_is_connection_config(self):
        protocol = {"loom_url": "http://owned-loom:7777"}
        baseline = {"order": 0, "mode": "baseline"}
        loom = {"order": 1, "mode": "natural"}
        binding = {"org": "org", "code_namespace": "code"}
        with patch(
            "examples.software_factory.harbor.native_environment.binding_for", return_value=binding
        ):
            self.assertEqual(mcp_settings(protocol, baseline), "")
            config = tomllib.loads(mcp_settings(protocol, loom))
            compact = {**protocol, "loom_mcp": {"schema_profile": "compact"}}
            compact_config = tomllib.loads(mcp_settings(compact, loom))
            self.assertEqual(mcp_settings(compact, baseline), "")
        server = config["environment"]["mcp_servers"][0]
        self.assertEqual(server["url"], "http://owned-loom:7777/mcp?toolset=coding")
        self.assertEqual(server["headers"]["X-Loom-Code-Scope"], "org/code")
        self.assertEqual(server["headers"]["Authorization"], "Bearer ${LOOM_ATTEMPT_TOKEN_1}")
        self.assertEqual(server["headers"]["Connection"], "close")
        self.assertEqual(
            compact_config["environment"]["mcp_servers"][0],
            {**server, "url": server["url"] + "&schema_profile=compact"},
        )
        for slot in (baseline, loom):
            settings = tomllib.loads(agent_settings(protocol, slot))["agent"]
            self.assertEqual(settings["user"], "agent")
            self.assertEqual(set(settings) - {"network_mode", "allowed_hosts"}, {"user"})


class PublicPromptBytes(TestCase):
    def test_auditor_preserves_crlf_and_rejects_normalized_or_modified_prompts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task = root / "tasks/slot-00"
            task.mkdir(parents=True)
            (task / "task.toml").write_text('[environment]\ndocker_image = "pinned-image"\n')
            trial = root / "trial"
            (trial / "agent").mkdir(parents=True)
            write(trial / "agent/trajectory.json", {})
            write(
                trial / "result.json",
                {
                    "config": {
                        "agent": {
                            "name": "claude-code",
                            "model_name": "claude-sonnet-5-5",
                            "kwargs": {"version": "pinned"},
                        }
                    }
                },
            )
            problem = "Original issue\r\nKeep the issue unchanged.\r\n"
            manifest = root / "history.json"
            write(manifest, {"tasks": [{"task_id": "task", "problem": problem}]})
            snapshot = [{"table": table, "scopes": []} for table in TABLES]
            for name in (
                "index-content.json",
                "live-index-before-00.json",
                "live-index-after-00.json",
            ):
                write(root / name, snapshot)
            certificate = root / "index-certificate.json"
            write(certificate, {"schema_version": 1, "scopes": {}})
            instructions = guidance(["src"])
            workspace = root / "builds/task/agent/workspace"
            workspace.mkdir(parents=True)
            stage(workspace, workspace.parent / "loom-guidance", instructions)
            slot = {
                "order": 0,
                "task_id": "task",
                "arm": "baseline",
                "mode": "baseline",
                "index_tier": None,
                "memory_enabled": False,
                "repeat": 0,
            }
            protocol = {
                "controller": "harbor",
                "harbor_version": "0.27.0",
                "output_root": str(root),
                "schedule": [slot],
                "schedule_sha256": digest([slot]),
                "model_id": "claude-sonnet-5-5",
                "index_tier": "per-arm",
                "solver": {"version": "pinned"},
                "project_guidance": instructions,
                "files": {str(manifest): hashlib.sha256(manifest.read_bytes()).hexdigest()},
                "historical_manifest": str(manifest),
                "index_certificate_file": str(certificate),
                "index_certificate_sha256": hashlib.sha256(certificate.read_bytes()).hexdigest(),
                "index_content_file": str(root / "index-content.json"),
                "index_content_sha256": digest(snapshot),
                "tasks": {"task": {"base_commit": "a" * 40}},
                "harbor_images": {
                    "task": {"agent": {"baseline": "pinned-image", "loom": "loom-image"}}
                },
                "attempt_scopes": {
                    "0": {
                        "task_id": "task",
                        "index_tier": None,
                        "base_commit": "a" * 40,
                        "org": "-",
                        "code_namespace": "-",
                    }
                },
            }
            entries = [
                {
                    "order": 0,
                    "result": {
                        "harbor_trial": str(trial),
                        "loom_connection_at_start": "disabled",
                        "loom_tools_advertised": 0,
                    },
                }
            ]
            billing = {"attempts": {"0": {"proof_status": "known"}}}
            for candidate, expected in (
                (task_instruction(problem).encode("utf-8"), True),
                (task_instruction(problem.replace("\r\n", "\n")).encode("utf-8"), False),
                (task_instruction(problem.replace("unchanged", "changed")).encode("utf-8"), False),
                (problem.encode("utf-8"), False),
                (task_instruction(problem).replace("evaluator", "reviewer").encode("utf-8"), False),
            ):
                with self.subTest(candidate=candidate):
                    (task / "instruction.md").write_bytes(candidate)
                    receipt = audit_fairness(protocol, entries, billing=billing)
                    self.assertIs(receipt["attempts"][0]["public_prompt"], expected)
                    self.assertIs(receipt["passed"], expected)
                    for name, value in receipt["attempts"][0].items():
                        if name not in {"order", "public_prompt", "passed"}:
                            self.assertIs(value, True, name)
