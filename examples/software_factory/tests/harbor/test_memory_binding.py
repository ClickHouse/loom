"""Native memory shares transport ownership and verifies frozen lifecycle evidence."""

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from examples.software_factory.factory.adapters import SDKContext
from examples.software_factory.factory.model import digest
from examples.software_factory.harbor.memory import (
    NativeMemoryContext,
    validate_history_certificate,
)


class NativeBinding(unittest.TestCase):
    def setUp(self):
        self.count = 0
        self.calls = []
        self.exits = 0
        self.bad_after = False
        owner = self

        class Agent:
            auto_run = False
            mentor_brief = False

            async def call_with_meta(self, op, body, **kwargs):
                owner.count += 1
                owner.calls.append((op, body, kwargs))
                return SimpleNamespace(data={"found": False}, request_id="native-id")

            async def __aexit__(self, *args):
                owner.exits += 1

        self.scope = {
            "org": "attempt",
            "namespace": "code",
            "repository": "owner/repo",
            "commit": "b" * 40,
        }

        async def verify(agent, body):
            self.count += 1
            self.assertEqual(body, {"org": "attempt", "namespace": "code"})
            return {
                "scope": {
                    **self.scope,
                    **({"commit": "c" * 40} if self.bad_after and self.calls else {}),
                },
                "permission": "allowed",
            }

        self.code = SDKContext(Agent(), verify, lambda: self.count, expected_scope=self.scope)
        self.addCleanup(self.code.close)
        self.native = NativeMemoryContext(self.code, org="attempt", memory_namespace="working")

    def test_shared_client_loop_and_honest_three_dispatch_receipt(self):
        result = self.native.request(
            "memory.get",
            {"org": "attempt", "namespace": "working", "agent": "solver", "memory_key": "x"},
        )
        self.assertEqual(result["scope"], {"org": "attempt", "namespace": "working"})
        self.assertEqual(result["physical_requests"], 3)
        self.assertEqual(result["request_id"], "native-id")
        self.assertIsNone(result["cost_usd"])
        self.assertIs(self.native.agent, self.code.agent)
        self.assertIs(self.native.runner, self.code.runner)
        self.assertEqual(self.calls[0][2], {"retry": {"max_attempts": 1}, "timeout_s": 10})
        self.native.close()
        self.assertEqual(self.exits, 0)

    def test_foreign_identity_or_unlisted_op_refused_without_dispatch(self):
        for op, body in [
            ("memory.delete", {"org": "attempt", "namespace": "working"}),
            ("memory.get", {"org": "foreign", "namespace": "working"}),
            ("memory.get", {"org": "attempt", "namespace": "code"}),
        ]:
            with self.assertRaises((PermissionError, ValueError)):
                self.native.request(op, body)
        self.assertEqual(self.count, 0)

    def test_postcall_code_scope_drift_is_refused_with_dispatch_count(self):
        self.bad_after = True
        with self.assertRaises(RuntimeError) as raised:
            self.native.request("memory.get", {"org": "attempt", "namespace": "working"})
        self.assertEqual(raised.exception.physical_requests, 3)

    def test_ignored_arguments_are_refused_after_both_scope_checks(self):
        async def ignored(op, body, **kwargs):
            self.count += 1
            return SimpleNamespace(data={"ignored_args": ["agent"]}, request_id="ignored")

        self.code.agent.call_with_meta = ignored
        with self.assertRaises(RuntimeError) as raised:
            self.native.request("memory.get", {"org": "attempt", "namespace": "working"})
        self.assertEqual(raised.exception.physical_requests, 3)

    def test_failed_native_call_still_checks_scope_and_counts_failed_dispatch(self):
        async def failed(op, body, **kwargs):
            self.count += 1
            raise TimeoutError("native response lost")

        self.code.agent.call_with_meta = failed
        with self.assertRaises(RuntimeError) as raised:
            self.native.request("memory.set", {"org": "attempt", "namespace": "working"})
        self.assertEqual(raised.exception.physical_requests, 3)


def value_sha(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class HistoryCertificate(unittest.TestCase):
    def setUp(self):
        from unittest.mock import patch

        from examples.software_factory.evals.flask.memory import fixtures
        from examples.software_factory.tests.flask.test_memory_scenarios import SOURCES

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        manifest = copy.deepcopy(fixtures.load_manifest())
        manifest["base_source_sha256"] = digest(SOURCES)
        rows = copy.deepcopy(fixtures.load_project_memories())
        for row in rows:
            body = SOURCES[row["evidence"]["path"]]
            row["evidence"].update(
                anchor=body.splitlines()[0],
                line=1,
                source_sha256=hashlib.sha256(body.encode()).hexdigest(),
            )
        with (
            patch.object(fixtures, "load_manifest", return_value=manifest),
            patch.object(fixtures, "load_project_memories", return_value=rows),
        ):
            self.fixture = fixtures.prepare_case("flask-memory-response-headers", SOURCES)
        self.slot = {
            "order": 0,
            "task_id": self.fixture["case_id"],
            "arm": "loom-treesitter-memory",
            "repeat": 0,
            "index_tier": "treesitter",
            "memory_enabled": True,
        }
        self.fixture_path = self.root / "fixture.json"
        self.fixture_path.write_text(json.dumps(self.fixture))
        self.fixture_sha = hashlib.sha256(self.fixture_path.read_bytes()).hexdigest()
        self.binding = {
            "org": "attempt-0",
            "code_namespace": "code",
            "memory_namespace": "working",
            "task_id": self.slot["task_id"],
            "index_tier": "treesitter",
            "base_commit": "b" * 40,
            "repository_tag": "repo:0123456789",
            "compiler_rows": {},
            "memory_history_sha256": digest(self.fixture["public_documents"]),
            "history_certificate_file": str(self.root / "certificate.json"),
        }
        self.protocol = {
            "attempt_scopes": {"0": self.binding},
            "memory_histories": {
                self.slot["task_id"]: {
                    "fixture_file": str(self.fixture_path),
                    "fixture_sha256": self.fixture_sha,
                }
            },
        }
        self.cert = {
            "schema_version": 1,
            "kind": "factory-native-history",
            "qualified": True,
            "slot_order": 0,
            "task_id": self.slot["task_id"],
            "arm": self.slot["arm"],
            "repeat": 0,
            "org": "attempt-0",
            "code_namespace": "code",
            "memory_namespace": "working",
            "repository_tag": self.binding["repository_tag"],
            "trunk_namespace": "code",
            "public_history_sha256": self.binding["memory_history_sha256"],
            "fixture_sha256": self.fixture_sha,
            "source_heads": {"A": "a" * 40, "B": "b" * 40},
            "source_receipts": {},
            "events": [],
            "final_snapshot": [],
        }
        for revision in ("A", "B"):
            head = self.cert["source_heads"][revision]
            source = self.fixture["source_revisions"][revision]
            scope = {
                "verified": True,
                "base_commit": head,
                "source_sha256": digest(source),
                "paths_sha256": digest(sorted(source)),
                "file_count": len(source),
            }
            self.cert["source_receipts"][revision] = {
                "indexed_head": head,
                "checked_head": head,
                "source_sha256": digest(source),
                "source_scope": scope,
                "request_id": "index-" + revision,
                "index_status": {"org": "attempt-0", "namespace": "code", "git_head": head},
                "worker": {
                    "operation": "memory_code_links",
                    "run_id": "worker-" + revision,
                    "result": {"errors": 0, "linked": 4, "rechecked": 4},
                },
            }
        self.binding["source_scope"] = self.cert["source_receipts"]["B"]["source_scope"]
        values = {}
        versions = {}
        files = {}
        latest = {}
        written_revision = {}
        changed_paths = {
            path
            for path in self.fixture["source_revisions"]["B"]
            if self.fixture["source_revisions"]["A"].get(path)
            != self.fixture["source_revisions"]["B"][path]
        }

        def event(operation, phase, key, request, response):
            self.cert["events"].append(
                {
                    "operation": operation,
                    "phase": phase,
                    "source_commit": self.cert["source_heads"]["A" if phase == "A" else "B"],
                    "request_id": "request-" + str(len(self.cert["events"])),
                    "request": {
                        "org": "attempt-0",
                        "namespace": "working",
                        "memory_key": key,
                        **request,
                    },
                    "response": response,
                }
            )

        def get(key, phase):
            links = []
            for path in files[key]:
                changed = phase != "A" and written_revision[key] == "A" and path in changed_paths
                links.append(
                    {
                        "code_namespace": "code",
                        "path": path,
                        "symbol": "",
                        "state": "changed" if changed else "fresh",
                        "changed_since": "b" * 40 if changed else "",
                    }
                )
            data = {
                "found": True,
                "memory_key": key,
                "memory_id": "id-" + key,
                "version": versions[key],
                "value": values[key],
                "content_sha256": value_sha(values[key]),
                "code": links,
            }
            event("memory.get", phase, key, {}, data)
            latest[key] = data

        def write(public):
            key = public["key"]
            operation = "memory." + public["operation"]
            request = {"value": public["value"]}
            if public["operation"] == "set":
                request.update(files=public["files"], tags=["repo:0123456789"])
                files[key] = public["files"]
                response = {
                    "version": 100 + len(self.cert["events"]),
                    "status": "active",
                    "noop": False,
                    "dry_run": False,
                }
            else:
                request["if_match_sha256"] = value_sha(values[key])
                response = {"updated": True, "new_version": 100 + len(self.cert["events"])}
            versions[key] = response.get("version", response.get("new_version"))
            values[key] = public["value"]
            written_revision[key] = public["revision"]
            response.update(memory_id="id-" + key, content_sha256=value_sha(values[key]))
            event(operation, public["revision"], key, request, response)
            get(key, public["revision"])

        for public in self.fixture["history_events"]:
            if public["revision"] == "A":
                write(public)
        for key in list(values):
            if files[key]:
                get(key, "B-stale")
        for public in self.fixture["history_events"]:
            if public["revision"] == "B":
                write(public)
        for key in values:
            get(key, "B")
        self.cert["final_snapshot"] = copy.deepcopy(list(latest.values()))
        self.save()

    def save(self):
        path = Path(self.binding["history_certificate_file"])
        path.write_text(json.dumps(self.cert))
        self.binding["history_certificate_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()

    def test_complete_public_sequence_and_all_rows_qualify(self):
        self.assertEqual(
            len(self.cert["final_snapshot"]),
            len({event["key"] for event in self.fixture["history_events"]}),
        )
        self.assertEqual(
            validate_history_certificate(self.protocol, self.slot, self.binding), self.cert
        )

    def test_checksum_and_operator_boolean_do_not_replace_native_lifecycle(self):
        self.cert["events"] = []
        self.cert["final_snapshot"] = []
        self.save()
        with self.assertRaises(PermissionError):
            validate_history_certificate(self.protocol, self.slot, self.binding)

    def test_stale_relink_cas_and_final_snapshot_each_fail_closed(self):
        mutations = [
            lambda cert: cert["events"][
                next(i for i, e in enumerate(cert["events"]) if e["phase"] == "B-stale")
            ]["response"]["code"][0].update(state="fresh"),
            lambda cert: next(e for e in cert["events"] if e["operation"] == "memory.update")[
                "request"
            ].update(if_match_sha256="0" * 64),
            lambda cert: cert["final_snapshot"][0].update(content_sha256="f" * 64),
            lambda cert: cert["source_receipts"]["B"]["worker"]["result"].update(errors=1),
            lambda cert: cert.update(trunk_namespace="another-code"),
            lambda cert: cert["source_receipts"]["B"]["index_status"].update(namespace="foreign"),
        ]
        original = copy.deepcopy(self.cert)
        for mutate in mutations:
            self.cert = copy.deepcopy(original)
            mutate(self.cert)
            self.save()
            with self.subTest(mutation=mutate):
                with self.assertRaises(PermissionError):
                    validate_history_certificate(self.protocol, self.slot, self.binding)

    def test_fixture_bytes_and_per_attempt_org_are_immutable(self):
        self.fixture_path.write_text("{}")
        with self.assertRaises(PermissionError):
            validate_history_certificate(self.protocol, self.slot, self.binding)
        self.fixture_path.write_text(json.dumps(self.fixture))
        self.protocol["attempt_scopes"]["1"] = {**self.binding}
        with self.assertRaises(PermissionError):
            validate_history_certificate(self.protocol, self.slot, self.binding)

    def test_complete_but_out_of_scope_or_ignored_native_rows_are_refused(self):
        for change in ({"code_namespace": "foreign"}, {"path": "tests/secret.py"}):
            original = copy.deepcopy(self.cert)
            next(
                e
                for e in self.cert["events"]
                if e["operation"] == "memory.get" and e["response"]["code"]
            )["response"]["code"][0].update(change)
            self.save()
            with self.assertRaises(PermissionError):
                validate_history_certificate(self.protocol, self.slot, self.binding)
            self.cert = original

    def test_replicates_may_share_read_only_code_but_never_mutable_memory(self):
        self.protocol["attempt_scopes"]["1"] = {**self.binding, "memory_namespace": "working-2"}
        validate_history_certificate(self.protocol, self.slot, self.binding)
        self.protocol["attempt_scopes"]["1"]["code_namespace"] = "other-trunk"
        with self.assertRaises(PermissionError):
            validate_history_certificate(self.protocol, self.slot, self.binding)

    def test_final_snapshot_key_is_controller_metadata_not_an_invented_native_get_field(self):
        for event in self.cert["events"]:
            if event["operation"] == "memory.get":
                event["response"].pop("memory_key", None)
        self.save()
        validate_history_certificate(self.protocol, self.slot, self.binding)

    def test_cold_memory_requires_actual_unfiltered_empty_namespace_proof(self):
        self.fixture = {
            "kind": "cold",
            "case_id": self.slot["task_id"],
            "public_documents": {},
            "history_events": [],
            "source_sha256": {"B": self.binding["source_scope"]["source_sha256"]},
        }
        self.fixture_path.write_text(json.dumps(self.fixture))
        self.fixture_sha = hashlib.sha256(self.fixture_path.read_bytes()).hexdigest()
        self.protocol["memory_histories"][self.slot["task_id"]]["fixture_sha256"] = self.fixture_sha
        self.binding["memory_history_sha256"] = digest({})
        self.cert.update(
            fixture_sha256=self.fixture_sha,
            public_history_sha256=digest({}),
            source_heads={"B": "b" * 40},
            source_receipts={"B": self.cert["source_receipts"]["B"]},
            events=[],
            final_snapshot=[],
            empty_namespace={
                "operation": "memory.list",
                "request_id": "empty-list",
                "request": {
                    "org": "attempt-0",
                    "namespace": "working",
                    "agent": "*",
                    "user_id": "*",
                    "session_id": "*",
                    **{
                        name: True
                        for name in (
                            "include_tombstoned",
                            "include_duplicates",
                            "include_superseded",
                            "include_proposed",
                        )
                    },
                },
                "response": {
                    "entries": [],
                    "truncated": False,
                    "total": 0,
                    "unchanged": False,
                    "hidden": 0,
                },
            },
        )
        self.cert["source_receipts"]["B"]["checked_head"] = ""
        self.save()
        validate_history_certificate(self.protocol, self.slot, self.binding)
        self.cert["empty_namespace"]["response"]["unchanged"] = True
        self.save()
        with self.assertRaises(PermissionError):
            validate_history_certificate(self.protocol, self.slot, self.binding)

    def test_one_hundred_notes_with_forty_links_and_only_changed_paths_invalidated(self):
        sets = [event for event in self.fixture["history_events"] if event["operation"] == "set"]
        linked = sum(bool(event["files"]) for event in sets)
        unlinked = len(sets) - linked
        untouched = next(
            path
            for path, body in self.fixture["source_revisions"]["A"].items()
            if body == self.fixture["source_revisions"]["B"][path]
        )
        public = []
        native_a = []
        native_b = []
        for kind, count, paths in [
            ("linked", 40 - linked, [untouched]),
            ("unlinked", 60 - unlinked, []),
        ]:
            for number in range(count):
                key = f"background/{kind}/{number}"
                value = f"Published background note {kind} {number}."
                public.append(
                    {
                        "operation": "set",
                        "revision": "A",
                        "key": key,
                        "value": value,
                        "files": paths,
                    }
                )
                request = {
                    "org": self.binding["org"],
                    "namespace": "working",
                    "memory_key": key,
                    "value": value,
                    "files": paths,
                    "tags": [self.binding["repository_tag"]],
                }
                response = {
                    "version": 10000 + number,
                    "memory_id": "id-" + key,
                    "content_sha256": value_sha(value),
                    "status": "active",
                    "noop": False,
                    "dry_run": False,
                }
                common = {
                    "request_id": "set-" + key,
                    "operation": "memory.set",
                    "phase": "A",
                    "source_commit": "a" * 40,
                    "request": request,
                    "response": response,
                }
                native_a.append(common)
                row = {
                    "found": True,
                    "memory_key": key,
                    "memory_id": "id-" + key,
                    "version": 10000 + number,
                    "content_sha256": value_sha(value),
                    "value": value,
                    "code": [
                        {
                            "code_namespace": "code",
                            "path": path,
                            "symbol": "",
                            "state": "fresh",
                            "changed_since": "",
                        }
                        for path in paths
                    ],
                }
                for phase, destination in [("A", native_a), ("B", native_b)]:
                    destination.append(
                        {
                            "request_id": phase + "-get-" + key,
                            "operation": "memory.get",
                            "phase": phase,
                            "source_commit": ("a" if phase == "A" else "b") * 40,
                            "request": {
                                k: v
                                for k, v in request.items()
                                if k in ("org", "namespace", "memory_key")
                            },
                            "response": copy.deepcopy(row),
                        }
                    )
                self.cert["final_snapshot"].append(copy.deepcopy(row))
                self.fixture["public_documents"][key + ".md"] = value
        cut = next(
            index
            for index, event in enumerate(self.fixture["history_events"])
            if event["revision"] == "B"
        )
        self.fixture["history_events"][cut:cut] = public
        cut = next(
            index for index, event in enumerate(self.cert["events"]) if event["phase"] != "A"
        )
        self.cert["events"][cut:cut] = native_a
        self.cert["events"].extend(native_b)
        self.fixture["history_events_sha256"] = digest(self.fixture["history_events"])
        self.fixture["public_documents_sha256"] = digest(self.fixture["public_documents"])
        self.fixture_path.write_text(json.dumps(self.fixture))
        sha = hashlib.sha256(self.fixture_path.read_bytes()).hexdigest()
        self.protocol["memory_histories"][self.slot["task_id"]]["fixture_sha256"] = sha
        self.binding["memory_history_sha256"] = digest(self.fixture["public_documents"])
        self.cert.update(
            fixture_sha256=sha, public_history_sha256=self.binding["memory_history_sha256"]
        )
        self.save()
        validate_history_certificate(self.protocol, self.slot, self.binding)
        self.assertEqual(len(self.cert["final_snapshot"]), 100)
        self.assertEqual(sum(bool(row["code"]) for row in self.cert["final_snapshot"]), 40)
