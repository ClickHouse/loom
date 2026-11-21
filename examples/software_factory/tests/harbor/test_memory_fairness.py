"""Native memory receipts establish writes independently of selected tools."""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from examples.software_factory.factory.model import digest
from examples.software_factory.harbor.fairness import _memory_evidence


class NativeMemoryEvidence(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        fixture = Path(self.temporary.name) / "public.json"
        fixture.write_text(json.dumps({"public_documents": {"observation": "Public history"}}))
        import hashlib

        history = digest({"observation": "Public history"})
        self.slot = dict(order=0, task_id="episode", memory_enabled=True)
        self.binding = dict(
            org="episode-org",
            code_namespace="code",
            memory_namespace="working",
            memory_history_sha256=history,
        )
        self.protocol = dict(
            attempt_scopes={"0": self.binding},
            memory_histories={
                "episode": dict(
                    fixture_file=str(fixture),
                    fixture_sha256=hashlib.sha256(fixture.read_bytes()).hexdigest(),
                )
            },
        )
        self.result = dict(
            memory_enabled=True,
            memory_namespace="working",
            memory_history_sha256=history,
            notes_history_sha256=digest({}),
            memory_rows_written=0,
            memory_rows_written_basis="Native receipts",
        )
        self.start = copy.deepcopy(self.result)
        self.events, self.calls = [], []
        self.validate = patch(
            "examples.software_factory.harbor.fairness.validate_history_certificate",
            return_value={},
        ).start()
        self.addCleanup(patch.stopall)

    def audit(self):
        return _memory_evidence(
            self.protocol, self.slot, self.result, self.start, self.events, self.calls
        )

    def receipt(self, *, operation="memory.set", noop=False):
        data = dict(
            memory_id="native-id",
            version=2,
            new_version=3,
            content_sha256="a" * 64,
            status="stored",
            noop=noop,
            dry_run=False,
            updated=not noop,
        )
        self.events.append(
            dict(
                kind="retrieval",
                op=operation,
                request=dict(org="episode-org", namespace="working", memory_key="observation"),
                native_result=data,
                result_sha256=digest(data),
                scope=dict(org="episode-org", namespace="working"),
                permission="allowed",
                request_id="native-request",
            )
        )
        self.calls.append(("native-request", operation))
        return data

    def persist(self, data, *, operation="memory.set"):
        self.events.append(
            dict(
                kind="memory_persisted",
                operation=operation,
                memory_id=data["memory_id"],
                memory_key="observation",
                version=data["new_version" if operation == "memory.update" else "version"],
                content_sha256=data["content_sha256"],
                namespace="working",
            )
        )
        self.result["memory_rows_written"] = 1

    def test_optional_memory_with_no_selected_calls_is_valid_zero(self):
        self.assertTrue(self.audit())
        self.validate.assert_called_once_with(self.protocol, self.slot, self.binding)

    def test_set_and_cas_update_require_matching_native_persistence(self):
        for operation in ("memory.set", "memory.update"):
            with self.subTest(operation=operation):
                self.events, self.calls = [], []
                data = self.receipt(operation=operation)
                self.persist(data, operation=operation)
                self.assertTrue(self.audit())
                self.events[-1]["version"] += 1
                self.assertFalse(self.audit())

    def test_known_noop_is_zero_even_when_a_write_tool_was_selected(self):
        data = self.receipt(noop=True)
        self.assertTrue(self.audit())
        self.persist(data)
        self.assertFalse(self.audit())

    def test_receipt_hash_scope_and_physical_id_cannot_be_fabricated(self):
        data = self.receipt()
        self.persist(data)
        original = copy.deepcopy(self.events)
        for key, value in (
            ("result_sha256", "bad"),
            ("scope", {"org": "other", "namespace": "working"}),
            ("request_id", "unobserved"),
            ("permission", "denied"),
        ):
            with self.subTest(key=key):
                self.events = copy.deepcopy(original)
                self.events[0][key] = value
                self.assertFalse(self.audit())

    def test_memory_and_notes_history_cannot_differ_between_arms(self):
        self.result["notes_history_sha256"] = digest({"private": "hint"})
        self.assertFalse(self.audit())
        self.result = copy.deepcopy(self.start)
        self.result["memory_history_sha256"] = "wrong"
        self.assertFalse(self.audit())

    def test_lessons_bind_only_to_this_episode_memory_namespace(self):
        self.events = [
            dict(
                kind="retrieval",
                op="code.symbol",
                request=dict(
                    org="episode-org",
                    namespace="code",
                    include_lessons=True,
                    lesson_namespaces=["working"],
                ),
            )
        ]
        self.assertTrue(self.audit())
        self.events[0]["request"]["lesson_namespaces"] = ["another-episode"]
        self.assertFalse(self.audit())

    def test_unknown_write_outcome_and_unproved_persistence_never_pass(self):
        self.result["memory_rows_written"] = None
        self.assertFalse(self.audit())
        self.result["memory_rows_written"] = 1
        self.assertFalse(self.audit())

    def test_bad_history_certificate_cannot_be_replaced_by_zero_calls(self):
        self.validate.side_effect = PermissionError("Native history is unverified")
        with self.assertRaises(PermissionError):
            self.audit()

    def test_native_hits_and_code_links_cannot_cross_episode_scopes(self):
        data = self.receipt()
        self.persist(data)
        for extra in (
            {"results": [{"namespace": "another-episode"}]},
            {"code": [{"code_namespace": "another-code-index"}]},
            {"namespaces_searched": ["another-episode"]},
        ):
            with self.subTest(extra=extra):
                native = {**data, **extra}
                self.events[0]["native_result"] = native
                self.events[0]["result_sha256"] = digest(native)
                self.assertFalse(self.audit())

    def test_one_physical_write_cannot_establish_two_persisted_rows(self):
        data = self.receipt()
        self.persist(data)
        self.events *= 2
        self.result["memory_rows_written"] = 2
        self.assertFalse(self.audit())
