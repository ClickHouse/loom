"""Native trajectories retain failed-attempt spend and count unique calls."""

import json
import tempfile
from pathlib import Path
from unittest import TestCase

from examples.software_factory.harbor.native_metrics import measurements


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
            trajectory = {
                "agent": {"name": "claude-code", "version": "pinned"},
                "steps": [
                    {"tool_calls": [call]},
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
                    {"is_copied_context": True, "tool_calls": [call]},
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
            self.assertEqual(row["tool_total_requests"], 2)
            self.assertEqual(row["loom_tool_calls"], 1)
            self.assertEqual(row["model_total_tokens"], 105)
            self.assertEqual(row["model_cache_read_tokens"], 70)
            self.assertEqual(row["model_cache_write_tokens"], 20)
            self.assertEqual(row["generation_cost_usd"], 0.02)
            self.assertEqual(row["agent_time_s"], 10)
            self.assertEqual(row["agent_test_runs"], 1)
            self.assertIsNone(row["memory_rows_written"])

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
