"""Fairness uses synthetic frozen traces, never a provider or database."""

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from examples.software_factory.factory.anthropic_provider import wire_messages
from examples.software_factory.factory.model import digest, tool_schemas
from examples.software_factory.harbor.fairness import audit_fairness
from examples.software_factory.harbor.index import TABLES, fingerprint, select_snapshot
from examples.software_factory.harbor.live_support import schedule
from examples.software_factory.harbor.scopes import selected_scopes


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def journal(path, rows):
    for i, row in enumerate(rows):
        row["sequence"] = i
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))


class FairnessProof(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        snapshot = fingerprint({table: [] for table in TABLES})
        public = [
            dict(
                task_id=t,
                repository="owner/repo",
                base_commit="a" * 40,
                problem="Public issue for " + t,
            )
            for t in ["a", "b", "c"]
        ]
        save(self.root / "public.json", {"tasks": public})
        save(self.root / "certificate.json", {"index_content_sha256": digest(snapshot)})
        self.protocol = dict(
            controller="harbor",
            harbor_version="0.27.0",
            files={},
            org="org",
            output_root=str(self.root),
            model_id="claude-haiku-5-5",
            index_tier="per-arm",
            server_commit="b" * 40,
            historical_manifest=str(self.root / "public.json"),
            index_certificate_file=str(self.root / "certificate.json"),
            index_certificate_sha256=hashlib.sha256(
                (self.root / "certificate.json").read_bytes()
            ).hexdigest(),
            index_content_sha256=digest(snapshot),
            attempt_budget={"output_tokens": 128000},
            schedule=schedule(["a", "b", "c"], seed=741),
            tasks={},
            attempt_scopes={},
            memory_histories={},
            harbor_specs={},
        )
        for issue in public:
            fixture = self.root / (issue["task_id"] + "-history.json")
            save(fixture, {"public_documents": {}})
            self.protocol["memory_histories"][issue["task_id"]] = {
                "fixture_file": str(fixture),
                "fixture_sha256": hashlib.sha256(fixture.read_bytes()).hexdigest(),
            }
            task = issue["task_id"]
            source = digest({"src/a.py": task})
            self.protocol["tasks"][task] = {
                "base_commit": "a" * 40,
                "indexes": {
                    tier: {
                        "namespace": task + "-" + tier,
                        "source_scope": {"paths_sha256": digest(["src/a.py"])},
                    }
                    for tier in ["treesitter", "scip"]
                },
            }
            self.protocol["harbor_specs"][task] = dict(
                source_sha256=source, grader_sha256=digest(task)
            )
        for slot in self.protocol["schedule"]:
            baseline = slot["mode"] == "baseline"
            self.protocol["attempt_scopes"][str(slot["order"])] = dict(
                org="-" if baseline else "org-" + slot["task_id"] + "-" + slot["index_tier"],
                task_id=slot["task_id"],
                index_tier=slot["index_tier"],
                base_commit="a" * 40,
                code_namespace="-" if baseline else "code",
                token_file=None if baseline else "reader.token",
                repository_tag="repo:0123456789",
                memory_namespace="working-" + str(slot["order"])
                if slot["memory_enabled"]
                else None,
                compiler_rows={"definitions": 0},
                source_scope={
                    "verified": True,
                    "base_commit": "a" * 40,
                    "paths_sha256": digest(["src/a.py"]),
                },
                memory_history_sha256=digest({}) if slot["memory_enabled"] else None,
            )
        tables = {table: [] for table in TABLES}
        tables["code_files"] = [
            {"org": org, "namespace": "code", "path": "src/a.py"}
            for org in sorted(
                {
                    row["org"]
                    for row in self.protocol["attempt_scopes"].values()
                    if row["index_tier"] is not None
                }
            )
        ]
        snapshot = fingerprint(tables)
        save(self.root / "index-content.json", snapshot)
        self.protocol["index_content_sha256"] = digest(snapshot)
        certificate = {"index_content_sha256": digest(snapshot), "scopes": {}}
        for row in self.protocol["attempt_scopes"].values():
            if row["index_tier"] is not None:
                certificate["scopes"][row["task_id"] + ":" + row["index_tier"]] = {
                    "org": row["org"],
                    "namespace": row["code_namespace"],
                    "git_head": row["base_commit"],
                    "build_commit": self.protocol["server_commit"],
                    "compiler_rows": row["compiler_rows"],
                    "source_scope": row["source_scope"],
                }
        save(self.root / "certificate.json", certificate)

        self.protocol["index_certificate_sha256"] = hashlib.sha256(
            (self.root / "certificate.json").read_bytes()
        ).hexdigest()
        self.history_validator = patch(
            "examples.software_factory.harbor.fairness.validate_history_certificate",
            return_value={},
        ).start()
        self.addCleanup(patch.stopall)
        self.protocol["schedule_sha256"] = digest(self.protocol["schedule"])
        self.billing = dict(
            protocol_sha256=digest(self.protocol), cohort_complete=True, attempts={}
        )
        self.entries = []
        for slot in self.protocol["schedule"]:
            order = slot["order"]
            task = slot["task_id"]
            folder = self.root / "jobs/live" / str(order)
            (folder / "agent/factory").mkdir(parents=True)
            meta = dict(repository="owner/repo", base_commit="a" * 40, source_paths=["src/a.py"])
            prompt = [
                dict(
                    role="user",
                    content=[
                        {
                            "text": "Common instructions.\n"
                            + json.dumps(meta)
                            + "\nPublic issue for "
                            + task
                        }
                    ],
                )
            ]
            tools = tool_schemas(
                slot["mode"],
                regression_tests=True,
                project_notes=False,
                memory=slot["memory_enabled"],
            )
            self.write_generation(folder, order, prompt, tools)
            result = {
                **slot,
                "schedule_order": order,
                "protocol_sha256": digest(self.protocol),
                "source_sha256": self.protocol["harbor_specs"][task]["source_sha256"],
                "harbor_trial": str(folder),
                "provider_logical_requests": 1,
                "provider_physical_requests": 1,
                "provider_failed_requests": 0,
                "retry_count": 0,
                "generation_cost_usd": 0.01,
                "model_selected_loom_requests": 0,
                "loom_tool_calls": 0,
                "model_selected_loom_dispatches": 0,
                "loom_logical_requests": 0,
                "loom_physical_requests": 0,
                "loom_transport_dispatches": 0,
                "context_reuses": 0,
                "local_scope_refusals": 0,
                "memory_enabled": slot["memory_enabled"],
                "memory_namespace": self.protocol["attempt_scopes"][str(slot["order"])][
                    "memory_namespace"
                ],
                "memory_history_sha256": digest({}) if slot["memory_enabled"] else None,
                "notes_history_sha256": digest({}),
                "memory_rows_written": 0,
                "memory_rows_written_basis": "Confirmed native write receipts"
                if slot["memory_enabled"]
                else "Recorded read-only operation allowlist",
                "harbor_reward_valid": True,
                "status": "completed",
                "resolved": False,
            }
            start = dict(
                kind="start",
                task_id=task,
                mode=slot["mode"],
                memory_enabled=slot["memory_enabled"],
                memory_namespace="working-" + str(slot["order"])
                if slot["memory_enabled"]
                else None,
                memory_history_sha256=digest({}) if slot["memory_enabled"] else None,
                notes_history_sha256=digest({}),
                provider="anthropic-messages:claude-haiku-5-5",
                budget=self.protocol["attempt_budget"],
                source_sha256=result["source_sha256"],
                grader_sha256=self.protocol["harbor_specs"][task]["grader_sha256"],
                tools=tools,
                prompt=prompt,
            )
            events = [
                start,
                dict(
                    kind="provider_request",
                    messages_sha256=digest(prompt),
                    tools_sha256=digest(tools),
                ),
                self.provider_event("gen-" + str(order), [{"text": "Done"}]),
                dict(kind="verification_deferred"),
            ]
            journal(folder / "agent/factory/events.jsonl", events)
            self.entries.append(dict(order=order, result=result))
            for phase in ["before", "after"]:
                save(
                    self.root / f"live-index-{phase}-{order:02d}.json",
                    select_snapshot(snapshot, selected_scopes(self.protocol, slot)),
                )
            self.billing["attempts"][str(order)] = dict(
                proof_status="known",
                physical_requests_matched=0,
                physical_requests_dispatched=0,
                physical_request_ids_sha256=digest([]),
                loom_provider_cost_usd=0,
                loom_provider_input_tokens=0,
                loom_provider_output_tokens=0,
                loom_embedding_tokens=0,
            )

    def provider_event(self, identifier, blocks):
        return dict(
            kind="provider_response",
            provider_request_id=identifier,
            physical_requests=1,
            retries=0,
            blocks=blocks,
            generation_cost_usd=0.01,
            usage=dict(
                input_tokens=10,
                output_tokens=2,
                cache_creation_input_tokens=0,
                cache_read_input_tokens=0,
            ),
        )

    def write_generation(self, folder, order, prompt, tools):
        usage = dict(
            input_tokens=10,
            output_tokens=2,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        )
        rows = [
            dict(
                phase="dispatch",
                path="/v1/messages",
                payload=dict(
                    model=self.protocol["model_id"],
                    max_tokens=128000,
                    messages=wire_messages(prompt),
                    tools=tools,
                ),
            ),
            dict(
                phase="response",
                path="/v1/messages",
                status=200,
                request_id="gen-" + str(order),
                usage=usage,
            ),
        ]
        journal(folder / "agent/provider-transport.jsonl", rows)

    def audit(self, entries=None):
        return audit_fairness(
            self.protocol, self.entries if entries is None else entries, billing=self.billing
        )

    def test_complete_failed_tasks_can_be_fair_without_a_quality_win(self):
        proof = self.audit()
        self.assertTrue(proof["passed"], proof["checks"])
        self.assertEqual(proof["counts"]["scheduled_attempts"], 51)
        self.assertFalse(any(e["result"]["resolved"] for e in self.entries))
        self.assertEqual(
            json.loads((self.root / "fairness-audit.json").read_text())["protocol_sha256"],
            digest(self.protocol),
        )

    def test_partial_or_infrastructure_failure_cannot_be_selected_away(self):
        proof = self.audit(self.entries[:-1])
        self.assertFalse(proof["passed"])
        self.assertFalse(proof["complete"])
        self.assertEqual(proof["counts"]["observed_attempts"], 50)
        self.entries[0]["result"]["harbor_reward_valid"] = False
        self.assertFalse(self.audit()["checks"]["execution_integrity"]["passed"])

    def test_schema_manual_hint_and_hidden_event_corruptions_fail(self):
        entry = self.entries[0]
        folder = Path(entry["result"]["harbor_trial"])
        trace = folder / "agent/provider-transport.jsonl"
        rows = [json.loads(s) for s in trace.read_text().splitlines()]
        rows[0]["payload"]["tools"][0]["description"] = "Different capability"
        journal(trace, rows)
        self.assertFalse(self.audit()["checks"]["schema_exposure"]["passed"])
        events_path = folder / "agent/factory/events.jsonl"
        events = [json.loads(s) for s in events_path.read_text().splitlines()]
        events.append(dict(kind="hidden_verification"))
        journal(events_path, events)
        self.assertFalse(self.audit()["checks"]["no_host_or_hidden_work"]["passed"])

    def test_index_unpriced_billing_and_memory_unknowns_never_pass(self):
        save(self.root / "live-index-before-00.json", {"changed": True})
        self.assertFalse(self.audit()["checks"]["index_immutable"]["passed"])
        self.entries[0]["result"]["generation_cost_usd"] = None
        self.assertFalse(self.audit()["checks"]["generation_transport"]["passed"])
        self.billing["attempts"]["0"]["physical_request_ids_sha256"] = "wrong"
        self.assertIsNone(self.audit()["checks"]["billing_evidence"]["passed"])
        del self.entries[0]["result"]["memory_rows_written_basis"]
        self.assertFalse(self.audit()["checks"]["memory_evidence"]["passed"])

    def test_request_ids_are_unique_and_logs_cannot_be_reused(self):
        folder = Path(self.entries[1]["result"]["harbor_trial"])
        path = folder / "agent/provider-transport.jsonl"
        rows = [json.loads(s) for s in path.read_text().splitlines()]
        rows[1]["request_id"] = "gen-0"
        journal(path, rows)
        events_path = folder / "agent/factory/events.jsonl"
        events = [json.loads(s) for s in events_path.read_text().splitlines()]
        events[2]["provider_request_id"] = "gen-0"
        journal(events_path, events)
        self.assertFalse(self.audit()["checks"]["unique_generation_ids"]["passed"])
        self.entries[1]["result"]["harbor_trial"] = self.entries[0]["result"]["harbor_trial"]
        self.assertFalse(self.audit()["checks"]["unique_attempt_folders"]["passed"])

    def test_manual_symbol_metadata_or_old_conversation_cannot_pass(self):
        folder = Path(self.entries[0]["result"]["harbor_trial"])
        events_path = folder / "agent/factory/events.jsonl"
        events = [json.loads(line) for line in events_path.read_text().splitlines()]
        start = events[0]
        text = start["prompt"][0]["content"][0]["text"]
        start["prompt"][0]["content"][0]["text"] = text.replace(
            '"source_paths":', '"named_symbol": "operator_hint", "source_paths":'
        )
        journal(events_path, events)
        trace = folder / "agent/provider-transport.jsonl"
        rows = [json.loads(line) for line in trace.read_text().splitlines()]
        rows[0]["payload"]["messages"] = wire_messages(start["prompt"])
        journal(trace, rows)
        self.assertFalse(self.audit()["checks"]["public_prompt"]["passed"])
        rows[0]["payload"]["messages"].insert(
            0, {"role": "assistant", "content": [{"type": "text", "text": "Older attempt"}]}
        )
        journal(trace, rows)
        self.assertFalse(self.audit()["passed"])

    def test_cached_choices_and_local_refusals_are_not_counted_as_http(self):
        entry = next(
            e
            for e in self.entries
            if e["result"]["mode"] == "natural" and not e["result"]["memory_enabled"]
        )
        result = entry["result"]
        order = entry["order"]
        folder = Path(result["harbor_trial"])
        events_path = folder / "agent/factory/events.jsonl"
        start = json.loads(events_path.read_text().splitlines()[0])
        tools = start["tools"]
        prompt = copy.deepcopy(start["prompt"])
        choices = [
            {"toolUse": {"toolUseId": str(i), "name": name, "input": args}}
            for i, (name, args) in enumerate(
                [
                    ("loom_code_symbol", {"name": "public_name"}),
                    ("loom_code_symbol", {"name": "public_name"}),
                    ("loom_code_grep", {"pattern": "public", "path_prefix": "tests/"}),
                ]
            )
        ]
        events = [
            start,
            dict(
                kind="provider_request", messages_sha256=digest(prompt), tools_sha256=digest(tools)
            ),
            self.provider_event("gen-" + str(order), choices),
        ]
        second = copy.deepcopy(prompt) + [
            {"role": "assistant", "content": choices},
            {"role": "user", "content": []},
        ]
        for i, choice in enumerate(choices):
            call = choice["toolUse"]
            events.append(dict(kind="tool_request", name=call["name"]))
            if i == 0:
                events.append(
                    dict(
                        kind="retrieval",
                        caller="model",
                        op="code.symbol",
                        physical_requests=3,
                        request={
                            "org": self.protocol["attempt_scopes"][str(order)]["org"],
                            "namespace": "code",
                            "include_lessons": False,
                        },
                    )
                )
            else:
                events.append(
                    dict(kind="context_reuse" if i == 1 else "local_scope_refusal", caller="model")
                )
            events.append(dict(kind="tool_result", tool_use_id=str(i), result={"abstain": True}))
            second[-1]["content"].append(
                {"toolResult": {"toolUseId": str(i), "content": [{"json": {"abstain": True}}]}}
            )
        events += [
            dict(
                kind="provider_request", messages_sha256=digest(second), tools_sha256=digest(tools)
            ),
            self.provider_event("gen-" + str(order) + "-next", [{"text": "Done"}]),
            dict(kind="verification_deferred"),
        ]
        journal(events_path, events)
        transport = folder / "agent/provider-transport.jsonl"
        rows = [json.loads(line) for line in transport.read_text().splitlines()]
        new = copy.deepcopy(rows)
        new[0]["payload"]["messages"] = wire_messages(second)
        new[1]["request_id"] += "-next"
        journal(transport, rows + new)
        loom = []
        ids = []
        for i, op in enumerate(["code.index_status", "code.symbol", "code.index_status"]):
            identifier = f"loom-{order}-{i}"
            ids.append(identifier)
            loom.extend(
                [
                    dict(phase="dispatch", path="/v1/" + op, request_id=None),
                    dict(phase="response", path="/v1/" + op, request_id=identifier, status=200),
                ]
            )
        journal(folder / "agent/loom-transport.jsonl", loom)
        result.update(
            provider_logical_requests=2,
            provider_physical_requests=2,
            generation_cost_usd=0.02,
            model_selected_loom_requests=3,
            loom_tool_calls=3,
            model_selected_loom_dispatches=1,
            loom_logical_requests=1,
            loom_physical_requests=3,
            loom_transport_dispatches=3,
            context_reuses=1,
            local_scope_refusals=1,
        )
        self.billing["attempts"][str(order)].update(
            physical_requests_matched=3,
            physical_requests_dispatched=3,
            physical_request_ids_sha256=digest(sorted(ids)),
        )
        proof = self.audit()
        self.assertTrue(proof["passed"], proof["checks"])
        self.assertEqual(proof["counts"]["selected_loom_tools"], 3)
        self.assertEqual(proof["counts"]["loom_dispatches"], 3)

    def test_corrupt_or_missing_artifact_is_unknown_not_an_exception(self):
        folder = Path(self.entries[0]["result"]["harbor_trial"])
        (folder / "agent/factory/events.jsonl").write_text("invalid json")
        proof = self.audit()
        self.assertFalse(proof["passed"])
        self.assertIsNone(proof["checks"]["artifact_integrity"]["passed"])
