"""Native trajectories retain failed-attempt spend and count unique calls."""

import json
import tempfile
from pathlib import Path
from unittest import TestCase

from examples.software_factory.harbor.native_metrics import (
    capture_turn_usage,
    initialization,
    measurements,
)


class NativeMeasurements(TestCase):
    def test_subagent_calls_are_counted_once_and_cache_is_not_added_twice(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "agent").mkdir()
            call = {
                "tool_call_id": "one",
                "function_name": "mcp__loom__loom_code_search",
                "arguments": {},
            }
            metrics = {
                "prompt_tokens": 80,
                "cached_tokens": 70,
                "completion_tokens": 5,
                "extra": {"cache_read_input_tokens": 70},
            }
            trajectory = {
                "agent": {"name": "claude-code", "version": "pinned"},
                "steps": [
                    {
                        "step_id": 1,
                        "source": "agent",
                        "metrics": metrics,
                        "tool_calls": [
                            {**call, "arguments": {"query": "private source"}},
                            {
                                **call,
                                "tool_call_id": "plugin",
                                "function_name": "mcp__plugin_loom_loom__loom_code_search",
                            },
                        ],
                    },
                    {
                        "extra": {"agent_id": "child"},
                        "tool_calls": [
                            {
                                "tool_call_id": "two",
                                "function_name": "Bash",
                                "arguments": {"command": "pytest -q"},
                            }
                        ],
                    },
                    {
                        "step_id": 3,
                        "is_copied_context": True,
                        "metrics": metrics,
                        "tool_calls": [call],
                    },
                ],
                "final_metrics": {
                    "total_prompt_tokens": 100,
                    "total_completion_tokens": 5,
                    "total_cached_tokens": 70,
                    "total_cost_usd": 0.02,
                    "extra": {"total_cache_creation_input_tokens": 20},
                },
            }
            (root / "agent/trajectory.json").write_text(json.dumps(trajectory))
            raw = {
                "agent_execution": {
                    "started_at": "2000-01-01T00:00:00Z",
                    "finished_at": "2000-01-01T00:00:10Z",
                }
            }
            row = measurements(raw, root)
            self.assertEqual(row["tool_total_requests"], 3)
            self.assertEqual(row["loom_tool_calls"], 2)
            self.assertEqual(row["model_total_tokens"], 105)
            self.assertEqual(row["model_cache_read_tokens"], 70)
            self.assertIsNone(row["model_cache_write_tokens"])
            self.assertEqual(row["first_cache_read_tokens"], 70)
            self.assertIsNone(row["first_cache_write_tokens"])
            self.assertEqual(row["generation_cost_usd"], 0.02)
            self.assertEqual(row["agent_time_s"], 10)
            self.assertEqual(row["agent_test_runs"], 1)
            self.assertIsNone(row["memory_rows_written"])
            target = root / "usage.json"
            capture_turn_usage(root, target)
            self.assertNotIn("private source", target.read_text())
            turns = json.loads(target.read_text())["turns"]
            self.assertEqual(len(turns), 1)
            self.assertEqual(turns[0]["tool_call_ids"], ["one", "plugin"])
            self.assertEqual(turns[0]["cache_read_tokens"], 70)
            self.assertIsNone(turns[0]["cache_write_tokens"])

    def test_missing_trajectory_is_unknown_including_zero_tool_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            row = measurements({}, Path(directory))
            for key in (
                "tool_total_requests",
                "loom_tool_calls",
                "generation_cost_usd",
                "model_total_tokens",
            ):
                self.assertIsNone(row[key])

    def test_startup_readiness_keeps_pending_and_missing_distinct_from_disabled(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "agent").mkdir()
            self.assertTrue(all(value is None for value in initialization(root).values()))
            path = root / "agent/claude-code.txt"
            base = {"type": "system", "subtype": "init", "mcp_servers": []}
            path.write_text(json.dumps(base) + "\n")
            row = initialization(root)
            self.assertEqual(row["loom_connection_at_start"], "disabled")
            self.assertIsNone(row["loom_tools_advertised"])
            self.assertIsNone(row["loom_plugin_loaded"])
            path.write_text(
                json.dumps(
                    {
                        **base,
                        "mcp_servers": [{"name": "plugin:loom:loom", "status": "pending"}],
                        "tools": ["Bash", "mcp__plugin_loom_loom__loom_code_search"],
                        "plugins": [{"name": "loom"}],
                        "skills": ["loom:loom-code", "loom:loom-memory"],
                    }
                )
                + "\n"
            )
            row = initialization(root)
            self.assertEqual(row["loom_connection_at_start"], "pending")
            self.assertEqual(row["loom_tools_advertised"], 1)
            self.assertTrue(row["loom_plugin_loaded"])
            self.assertTrue(row["loom_code_skill_loaded"])
            self.assertTrue(row["loom_memory_skill_loaded"])
