"""Episode notes and native memory keep scope and persistence evidence."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from examples.software_factory.factory.memory_tools import MemoryTools
from examples.software_factory.factory.model import Ledger, digest
from examples.software_factory.factory.project_notes import ProjectNotes


class EpisodeMemoryContracts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.notes = ProjectNotes(
            self.root / "notes.json", {"decision": "Keep source contracts stable."}
        )
        self.calls = []
        owner = self

        class Backend:
            def request(self, op, body):
                owner.calls.append((op, body))
                return {
                    "data": owner.data,
                    "scope": {"org": "episode", "namespace": "working"},
                    "permission": "allowed",
                    "physical_requests": 1,
                }

        self.memory = MemoryTools(
            Backend(),
            org="episode",
            namespace="working",
            agent="solver",
            session_id="session-2",
            repository_tag="repo:0123456789",
            history_sha256=digest({"decision": "Keep source contracts stable."}),
            code_namespaces=("code",),
            allowed_paths=("src/app.py",),
            ledger=Ledger(self.root / "events.jsonl"),
        )
        self.data = {}

    def test_project_docs_immutable_and_working_notes_survive_fresh_store(self):
        with self.assertRaises(ValueError):
            self.notes.execute(
                "notes_set", {"scope": "project", "key": "decision", "value": "overwrite"}
            )
        written = self.notes.execute(
            "notes_set", {"key": "observation", "value": "Observed contract"}
        )
        again = ProjectNotes(
            self.root / "notes.json", {"decision": "Keep source contracts stable."}
        )
        self.assertEqual(
            again.execute("notes_read", {"scope": "working", "key": "observation"})["value"],
            "Observed contract",
        )
        with self.assertRaises(ValueError):
            again.execute(
                "notes_update",
                {"key": "observation", "value": "clobber", "expected_sha256": "stale"},
            )
        self.assertEqual(
            written["content_sha256"],
            again.execute("notes_read", {"scope": "working", "key": "observation"})[
                "content_sha256"
            ],
        )

    def test_native_identity_is_bound_and_unexpected_identity_refused_before_dispatch(self):
        with self.assertRaises(ValueError):
            self.memory.execute(
                "loom_memory_set", {"memory_key": "x", "value": "y", "org": "foreign"}
            )
        self.assertEqual(self.calls, [])
        self.data = {
            "memory_id": "id",
            "version": 2,
            "namespace": "working",
            "memory_key": "x",
            "status": "active",
            "noop": False,
            "dry_run": False,
            "content_sha256": "a" * 64,
        }
        self.memory.execute(
            "loom_memory_set", {"memory_key": "x", "value": "Observation", "files": ["src/app.py"]}
        )
        self.assertEqual(self.calls[0][1]["session_id"], "session-2")
        self.assertEqual(self.memory.persisted_memory_writes, 1)
        native = next(row for row in self.memory.ledger.rows if row["kind"] == "retrieval")
        self.assertEqual(native["native_result"], self.data)
        self.assertEqual(
            native["result_sha256"],
            digest(self.data),
        )

    def test_noop_missing_or_failed_update_never_count_as_writes(self):
        for data in (
            {"version": 0, "noop": True},
            {"version": 4, "noop": True},
            {"updated": False, "new_version": 0},
        ):
            self.data = data
            self.memory.execute(
                "loom_memory_update" if "updated" in data else "loom_memory_set",
                {
                    "memory_key": "x",
                    "value": "y",
                    **({"if_match_sha256": "a" * 64} if "updated" in data else {}),
                },
            )
        self.assertEqual(self.memory.persisted_memory_writes, 0)

    def test_memory_link_state_and_exact_version_survive_projection(self):
        self.data = {
            "found": True,
            "value": "Check contract",
            "version": 1234567890123456789,
            "content_sha256": "a" * 64,
            "code": [
                {
                    "code_namespace": "code",
                    "path": "src/app.py",
                    "symbol": "app.run",
                    "state": "changed",
                    "changed_since": "b" * 40,
                    "suggestion": "Recheck",
                }
            ],
        }
        out = self.memory.execute("loom_memory_get", {"memory_key": "x"})
        self.assertEqual(out["code"], self.data["code"])
        self.assertEqual(out["version"], self.data["version"])
        self.data["code"][0]["code_namespace"] = "foreign"
        with self.assertRaises(PermissionError):
            self.memory.execute("loom_memory_get", {"memory_key": "x"})

    def test_source_scope_cannot_expand_via_files_or_memory_hit_namespace(self):
        with self.assertRaises(PermissionError):
            self.memory.execute(
                "loom_memory_set", {"memory_key": "x", "value": "y", "files": ["tests/private.py"]}
            )
        self.assertEqual(self.calls, [])
        self.data = {
            "results": [
                {"memory_key": "secret", "namespace": "foreign", "content_excerpt": "outside"}
            ]
        }
        with self.assertRaises(PermissionError):
            self.memory.execute("loom_memory_search", {"query": "contract"})

    def test_reads_are_not_cached_and_dispatch_accounting_is_native(self):
        self.data = {"found": False}
        self.memory.execute("loom_memory_get", {"memory_key": "x"})
        self.memory.execute("loom_memory_get", {"memory_key": "x"})
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(
            [r["physical_requests"] for r in self.memory.ledger.rows if r["kind"] == "retrieval"],
            [1, 1],
        )

    def test_repository_tag_is_stamped_and_cannot_be_chosen_by_model(self):
        with self.assertRaises(ValueError):
            self.memory.execute(
                "loom_memory_set",
                {"memory_key": "x", "value": "observation", "tags": ["repo:fffffffffff"]},
            )
        self.assertEqual(self.calls, [])
        self.data = {"version": 0, "noop": True}
        self.memory.execute(
            "loom_memory_set", {"memory_key": "x", "value": "observation", "tags": ["decision"]}
        )
        self.assertEqual(self.calls[0][1]["tags"], ["repo:0123456789", "decision"])

    def test_update_binds_user_but_does_not_narrow_to_fresh_conversation_session(self):
        self.data = {
            "updated": True,
            "new_version": 4,
            "memory_id": "existing-id",
            "content_sha256": "b" * 64,
        }
        self.memory.execute(
            "loom_memory_update",
            {"memory_key": "x", "value": "revised", "if_match_sha256": "a" * 64},
        )
        self.assertEqual(self.memory.persisted_memory_writes, 1)
        self.assertNotIn("session_id", self.calls[0][1])
        with self.assertRaises(ValueError):
            self.memory.execute("loom_memory_update", {"memory_key": "x", "value": "clobber"})

    def test_lost_write_receipt_is_unknown_and_failed_dispatch_is_recorded(self):
        class Failed:
            def request(self, op, body):
                raise TimeoutError("response lost")

        self.memory.backend = Failed()
        with self.assertRaises(TimeoutError):
            self.memory.execute("loom_memory_set", {"memory_key": "x", "value": "observation"})
        self.assertTrue(self.memory.write_outcomes_unknown)
        self.assertEqual(self.memory.persisted_memory_writes, 0)
        receipt = self.memory.ledger.rows[-1]
        self.assertIsNone(receipt["physical_requests"])
        self.assertIsNone(receipt["cost_usd"])

    def test_store_refuses_different_project_seed_and_symlink(self):
        with self.assertRaises(PermissionError):
            ProjectNotes(self.root / "notes.json", {"other": "different facts"})
        link = self.root / "link.json"
        link.symlink_to(self.root / "notes.json")
        with self.assertRaises(PermissionError):
            ProjectNotes(link, {"decision": "Keep source contracts stable."})


class FactoryEpisodeIntegration(unittest.TestCase):
    def setUp(self):
        from examples.software_factory.factory.model import Task, digest

        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        files = {"src/app.py": "def run():\n    return 1\n"}
        self.task = Task(
            "episode",
            "fixture",
            "example/repo",
            "a" * 40,
            "code",
            "Preserve the contract.",
            "run",
            files,
            digest(files),
        )

    def factory(self, *, memory=False, mode="natural"):
        from examples.software_factory.factory.engine import Factory

        owner = self
        self.calls = []

        class Backend:
            def request(self, op, body):
                owner.calls.append((op, body))
                if op.startswith("memory."):
                    return {
                        "data": {"found": False},
                        "scope": {"org": "episode", "namespace": "working"},
                        "permission": "allowed",
                        "physical_requests": 1,
                    }
                return {
                    "data": {
                        "definitions": [
                            {
                                "path": "src/app.py",
                                "start_line": 1,
                                "end_line": 2,
                                "body": owner.task.files["src/app.py"],
                                "lessons": [
                                    {
                                        "namespace": "working",
                                        "memory_key": "contract",
                                        "excerpt": "Historical contract",
                                        "code_state": "changed",
                                        "changed_since": "b" * 40,
                                    }
                                ],
                            }
                        ]
                    },
                    "scope": {
                        "org": "episode",
                        "namespace": "code",
                        "repository": "example/repo",
                        "commit": "a" * 40,
                    },
                    "permission": "allowed",
                    "physical_requests": 1,
                }

        class Grader:
            sha256 = "fixture"

        class Provider:
            label = "fixture"

        backend = Backend()
        native = (
            MemoryTools(
                backend,
                org="episode",
                namespace="working",
                agent="solver",
                repository_tag="repo:0123456789",
                history_sha256=digest({"decision": "Keep source contracts stable."}),
                code_namespaces=("code",),
                allowed_paths=("src/app.py",),
            )
            if memory
            else None
        )
        self.notes = ProjectNotes(
            self.root / "shared-notes.json", {"decision": "A public source contract."}
        )
        return Factory(
            self.task,
            Provider(),
            Grader(),
            self.root / "attempt",
            mode=mode,
            backend=backend if mode == "natural" else None,
            org="episode",
            project_notes=self.notes,
            memory=native,
        )

    def test_constructor_does_not_dispatch_or_write_memory(self):
        factory = self.factory(memory=True)
        self.assertEqual(self.calls, [])
        self.assertTrue(any(t["name"] == "loom_memory_update" for t in factory.tools))
        self.assertEqual(factory.memory.persisted_memory_writes, 0)

    def test_all_arms_have_identical_notes_and_baseline_has_no_native_memory(self):
        factory = self.factory(mode="baseline")
        self.assertEqual(
            factory.execute("notes_read", {"scope": "project", "key": "decision"})["value"],
            "A public source contract.",
        )
        self.assertFalse(any(t["name"].startswith("loom_") for t in factory.tools))
        with self.assertRaises(PermissionError):
            factory.execute("read", {"path": "shared-notes.json"})

    def test_memory_arm_preserves_native_lessons_and_requests_bound_namespaces(self):
        factory = self.factory(memory=True)
        result = factory.execute("loom_code_symbol", {"name": "run"})
        self.assertTrue(self.calls[0][1]["include_lessons"])
        self.assertEqual(self.calls[0][1]["lesson_namespaces"], ["working"])
        self.assertEqual(result["definitions"][0]["lessons"][0]["code_state"], "changed")
        self.assertEqual(result["definitions"][0]["lessons"][0]["changed_since"], "b" * 40)

    def test_code_only_arm_keeps_lessons_disabled(self):
        factory = self.factory()
        result = factory.execute("loom_code_symbol", {"name": "run"})
        self.assertFalse(self.calls[0][1]["include_lessons"])
        self.assertNotIn("lessons", result["definitions"][0])

    def test_memory_augmented_code_reads_are_not_reused_across_mutable_memory(self):
        factory = self.factory(memory=True)
        factory.execute("loom_code_symbol", {"name": "run"})
        factory.execute("loom_code_symbol", {"name": "run"})
        self.assertEqual(len(self.calls), 2)
        self.assertFalse(any(row["kind"] == "context_reuse" for row in factory.ledger.rows))

    def test_foreign_native_lesson_is_refused_without_exposing_its_content(self):
        factory = self.factory(memory=True)
        with self.assertRaises(PermissionError):
            factory.context.project(
                "code.grep",
                {"matches": [], "lessons": [{"namespace": "foreign", "excerpt": "private"}]},
            )
