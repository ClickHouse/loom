"""Live treatment wiring is fully bound before an optional model call."""

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from examples.software_factory.factory.model import digest
from examples.software_factory.harbor.arms import ALL
from examples.software_factory.harbor.runtime import task_inputs
from examples.software_factory.harbor.scopes import binding_for, selected_scopes


class RuntimeBindings(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.files = {"src/public.py": "def public():\n    return 1\n"}
        self.source = {
            "verified": True,
            "base_commit": "a" * 40,
            "source_sha256": digest(self.files),
            "paths_sha256": digest(sorted(self.files)),
            "file_count": 1,
            "tables": {},
        }
        counts = {
            "compiler_definitions": 0,
            "compiler_references": 0,
            "python_definitions": 0,
            "python_references": 0,
        }
        self.slots = []
        bindings = {}
        for order, arm in enumerate(("baseline", "loom-treesitter", "loom-treesitter-memory")):
            spec = ALL[arm]
            self.slots.append(
                {"order": order, "task_id": "task", "repeat": 0, "arm": arm, **spec.identity()}
            )
            token = self.root / (str(order) + ".token")
            token.write_text("test-slot-" + str(order))
            bindings[str(order)] = {
                "task_id": "task",
                "index_tier": spec.index_tier,
                "org": "org" if spec.index_tier else "-",
                "code_namespace": "code" if spec.index_tier else "-",
                "memory_namespace": "working-" + str(order) if spec.memory_enabled else None,
                "token_file": str(token) if spec.index_tier else None,
                "base_commit": "a" * 40,
                "repository_tag": "repo:0123456789",
                "compiler_rows": counts if spec.index_tier else None,
                "source_scope": self.source if spec.index_tier else None,
                "memory_history_sha256": digest(
                    {"decision": "Public project document."} if spec.history_seeded else {}
                )
                if spec.memory_enabled
                else None,
            }
        certificate = self.root / "index.json"
        certificate.write_text(
            json.dumps(
                {
                    "scopes": {
                        "task:treesitter": {
                            "org": "org",
                            "namespace": "code",
                            "repo": "owner/repo",
                            "git_head": "a" * 40,
                            "build_commit": "b" * 40,
                            "compiler_rows": counts,
                            "source_scope": self.source,
                        }
                    }
                }
            )
        )
        fixture = self.root / "fixture.json"
        fixture.write_text(
            json.dumps({"public_documents": {"decision": "Public project document."}})
        )
        self.protocol = {
            "suite": {"adapter": "examples.software_factory.evals.flask.profile"},
            "schedule": self.slots,
            "attempt_scopes": bindings,
            "tasks": {"task": {"base_commit": "a" * 40}},
            "server_commit": "b" * 40,
            "loom_url": "http://unused",
            "index_certificate_file": str(certificate),
            "index_certificate_sha256": hashlib.sha256(certificate.read_bytes()).hexdigest(),
            "memory_histories": {
                "task": {
                    "fixture_file": str(fixture),
                    "fixture_sha256": hashlib.sha256(fixture.read_bytes()).hexdigest(),
                }
            },
        }
        self.task = SimpleNamespace(
            task_id="task",
            namespace="code",
            repository="owner/repo",
            base_commit="a" * 40,
            files=self.files,
            source_sha256=digest(self.files),
        )

    def test_shared_read_only_code_scope_deduplicates_and_memory_is_private(self):
        self.assertEqual(selected_scopes(self.protocol), [{"org": "org", "namespace": "code"}])
        self.assertEqual(selected_scopes(self.protocol, self.slots[0]), [])
        self.assertEqual(binding_for(self.protocol, self.slots[2])["memory_namespace"], "working-2")

    def test_model_identity_and_no_old_global_namespace_fallback(self):
        for change in ({"memory_enabled": False}, {"index_tier": "scip"}, {"mode": "baseline"}):
            altered = {**self.slots[2], **change}
            with self.assertRaises(PermissionError):
                binding_for(self.protocol, altered)
        del self.protocol["attempt_scopes"]["2"]
        self.protocol["org"] = "old-global"
        with self.assertRaises(PermissionError):
            binding_for(self.protocol, self.slots[2])

    def test_task_inputs_propagates_bound_scope_to_profile(self):
        profile = Mock()
        profile.task_inputs.return_value = "profile-result"
        with patch("examples.software_factory.harbor.runtime.load_profile", return_value=profile):
            self.assertEqual(
                task_inputs(self.protocol, "task", "treesitter", slot=self.slots[2]),
                "profile-result",
            )
            self.assertEqual(
                profile.task_inputs.call_args.kwargs["binding"],
                self.protocol["attempt_scopes"]["2"],
            )

    def test_code_certificate_and_task_source_cannot_be_substituted(self):
        self.task.files = {**self.files, "tests/hidden.py": "private"}
        self.task.source_sha256 = digest(self.task.files)
        from examples.software_factory.harbor.native_environment import mcp_settings

        with self.assertRaises(PermissionError):
            mcp_settings(self.protocol, self.slots[1])
        self.protocol["attempt_scopes"]["2"]["memory_namespace"] = "code"
        with self.assertRaises(PermissionError):
            binding_for(self.protocol, self.slots[2])

    def test_snapshot_groups_org_and_selects_only_requested_code_scopes(self):
        from examples.software_factory.harbor.index import (
            TABLES,
            fingerprint,
            select_snapshot,
            snapshot,
        )

        tables = {table: [] for table in TABLES}
        tables["code_symbols"] = [
            {"org": "a", "namespace": "code", "name": "first"},
            {"org": "b", "namespace": "code", "name": "second"},
        ]
        full = fingerprint(tables)
        selected = select_snapshot(full, [{"org": "b", "namespace": "code"}])
        rows = next(table["scopes"] for table in selected if table["table"] == "code_symbols")
        self.assertEqual(
            [(row["org"], row["namespace"], row["rows"]) for row in rows], [("b", "code", 1)]
        )
        with patch("examples.software_factory.harbor.index.subprocess.run") as run:
            self.assertEqual(snapshot("unused", []), fingerprint({table: [] for table in TABLES}))
            run.assert_not_called()

    def test_replicates_share_code_certificate_but_reused_memory_scope_refuses(self):
        repeated = {**self.slots[2], "order": 3, "repeat": 1}
        self.protocol["schedule"].append(repeated)
        self.protocol["attempt_scopes"]["3"] = {
            **self.protocol["attempt_scopes"]["2"],
            "memory_namespace": "working-3",
        }
        self.assertEqual(binding_for(self.protocol, repeated)["code_namespace"], "code")
        self.assertEqual(selected_scopes(self.protocol), [{"org": "org", "namespace": "code"}])
        self.protocol["attempt_scopes"]["3"]["memory_namespace"] = "working-2"
        with self.assertRaises(PermissionError):
            binding_for(self.protocol, repeated)
