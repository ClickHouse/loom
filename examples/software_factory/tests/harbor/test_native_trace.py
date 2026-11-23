"""Trace analysis separates model turns from tool counts and retains unknowns."""

import json
import tempfile
import unittest
from pathlib import Path

from examples.software_factory.harbor.native_trace import COUNTERS, TIMINGS, summarize


class NativeTrace(unittest.TestCase):
    def test_parallel_tools_are_one_model_turn_and_lesson_repeats_are_measured(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "agent").mkdir()
            lesson = {
                "namespace": "private",
                "memory_key": "secret",
                "excerpt": "note\ntext",
                "code_state": "fresh",
            }
            events = [
                {
                    "type": "assistant",
                    "message": {
                        "id": "model-1",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "one",
                                "name": "mcp__loom__loom_code_symbol",
                                "input": {"secret": "private"},
                            },
                            {
                                "type": "tool_use",
                                "id": "two",
                                "name": "mcp__loom__loom_code_symbol",
                                "input": {},
                            },
                        ],
                    },
                },
                {
                    "type": "user",
                    "message": {
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "one",
                                "content": json.dumps(
                                    {"definitions": [{"body": "abc", "lessons": [lesson]}]}
                                ),
                            },
                            {
                                "type": "tool_result",
                                "tool_use_id": "two",
                                "content": json.dumps(
                                    {
                                        "lessons": [
                                            {**lesson, "path": "file.py", "excerpt": "note text"}
                                        ]
                                    }
                                ),
                            },
                        ]
                    },
                },
                {
                    "type": "assistant",
                    "message": {"id": "model-2", "content": [{"type": "text", "text": "done"}]},
                },
                {"type": "result", "duration_ms": 3000, "duration_api_ms": 2000},
            ]
            path = root / "agent/claude-code.txt"
            path.write_text("\n".join(json.dumps(event) for event in events))
            result = summarize(root)
            metrics = result["metrics"]
            self.assertEqual(metrics["model_turns_main"], 2)
            self.assertEqual(metrics["parallel_tool_groups_main"], 1)
            self.assertEqual(metrics["tool_calls_main"], 2)
            self.assertEqual(metrics["loom_source_chars_main"], 3)
            self.assertEqual(metrics["loom_lesson_chars_main"], 18)
            self.assertEqual(metrics["loom_repeated_lesson_chars_main"], 9)
            self.assertEqual(metrics["claude_api_time_s"], 2)
            self.assertNotIn("secret", json.dumps(result))
            self.assertNotIn("private", json.dumps(result))
            self.assertNotIn('"body"', json.dumps(result))

    def test_missing_stream_and_partial_final_timing_remain_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertTrue(
                all(summarize(root)["metrics"][key] is None for key in (*COUNTERS, *TIMINGS))
            )
            (root / "agent").mkdir()
            (root / "agent/claude-code.txt").write_text(
                '{"type":"assistant","message":{"id":"one","content":[]}}\n{"partial":'
            )
            metrics = summarize(root)["metrics"]
            self.assertEqual(metrics["model_turns_main"], 1)
            self.assertIsNone(metrics["claude_cli_time_s"])
            self.assertIsNone(metrics["claude_api_time_s"])
