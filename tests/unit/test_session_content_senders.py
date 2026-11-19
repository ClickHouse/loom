"""Pin automatic sender provenance to the operation admission class."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from server.operations.registry import OPERATIONS_BY_NAME
from tests.unit.session_content_senders import collect, javascript_sites, python_sites

ROOT = Path(__file__).resolve().parents[2]
FLOOR = frozenset(
    {
        "conversation.append",
        "conversation.save_message",
        "conversation.create",
        "conversation.update",
        "memory.set_from_messages",
        "session.brief_save",
        "observe.record",
        "observe.crystallize",
        "working_memory.set",
        "working_memory.append",
        "run.begin",
        "run.complete",
        "code.outcome",
        "cost.record_turns",
        "ui.vitals",
        "context.pack",
        "code.search",
        "code.impact",
        "memory.for_file",
        "namespace.validate",
        "code.test_gate",
        "code.symbol",
        "code.enclosing",
        "tracker.duplicate_check",
        "memory.search",
        "memory.set",
        "memory.bulk_set",
        "knowledge.search",
    }
)


def _unclassified(sites, classified, exceptions):
    return {site.key for site in sites if site.op not in classified} - set(exceptions)


def _changed_sources(root, pins):
    return {
        path
        for path, digest in pins.items()
        if hashlib.sha256((root / path).read_bytes()).hexdigest() != digest
    }


def test_shipped_automatic_senders_have_an_admission_class_or_reviewed_exception():
    classified = {name for name, op in OPERATIONS_BY_NAME.items() if op.session_content}
    assert FLOOR <= classified
    sites = collect(ROOT)
    reviewed = json.loads(Path(__file__).with_name("session_content_senders.json").read_text())
    exceptions = reviewed["sites"]
    assert not _unclassified(sites, classified, exceptions), "Review new sender content provenance"
    assert set(exceptions) == {site.key for site in sites if site.op not in classified}
    assert not _changed_sources(ROOT, reviewed["reviewed_source_sha256"]), (
        "Review changed sender and body-builder provenance before renewing an exception"
    )
    assert {site.path for site in sites if site.key in exceptions} <= set(
        reviewed["reviewed_source_sha256"]
    )
    assert all(
        entry["reason"]
        and entry["category"] in {"scope", "reference", "explicit", "delivery", "bootstrap"}
        for entry in exceptions.values()
    )


def test_independent_delivery_roots_are_present_in_the_actual_census():
    paths = {site.path for site in collect(ROOT)}
    assert {
        "frontend/src/lib/vitals.ts",
        "integrations/grep-shim/lib/shim.mjs",
        "integrations/cursor/cursor-session-hook.sh",
        "integrations/generic/ingest.sh",
        "server/cli/shell.py",
        "sdk/python/loom/memory.py",
    } <= paths
    for plugin in ("claude-code-plugin", "codex-plugin"):
        assert f"integrations/{plugin}/hooks/loom-capture.mjs" in paths
        assert f"integrations/{plugin}/lib/flush-job.mjs" in paths


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            '// post("manual.example", payload)\nconst example = \'post("manual.example", x)\';',
            set(),
        ),
        ('/* ignored\ncomment */\npostBounded("observe.record", body);', {"observe.record"}),
        ('const OP = "observe.record"; postBounded(OP, body);', {"observe.record"}),
        ('enqueue({op: "session.brief_save", body});', {"session.brief_save"}),
        ("fetch(`${base}/v1/ui.vitals`, {body: JSON.stringify(vitals)});", {"ui.vitals"}),
        ("function post(op, body) {}\npost(name, body);", {"<dynamic>"}),
        (
            'fetch(key: string): Promise<Value> { return client.call("context.fetch", {key}); }',
            {"context.fetch"},
        ),
    ],
)
def test_javascript_sender_shapes(source, expected):
    assert {site.op for site in javascript_sites(source, "lib/only.mjs")} == expected


def test_new_library_send_and_changed_scope_payload_require_review():
    original = javascript_sites('post("code.repos", {repo});', "lib/scope.mjs")
    reviewed = {site.key: "scope" for site in original}
    assert not _unclassified(original, set(), reviewed)
    changed = javascript_sites('post("code.repos", {repo, prompt});', "lib/scope.mjs")
    assert _unclassified(changed, set(), reviewed)
    added = javascript_sites('post("new.capture", {prompt});', "lib/added.mjs")
    assert _unclassified(original | added, set(), reviewed)


def test_duplicate_calls_cannot_reuse_one_reviewed_exception():
    first = javascript_sites('post("knowledge.search", {query});', "lib/query.mjs")
    doubled = javascript_sites(
        'post("knowledge.search", {query}); post("knowledge.search", {query});', "lib/query.mjs"
    )
    assert len(doubled) == 2
    assert _unclassified(doubled, set(), {site.key: "explicit" for site in first})


def test_python_bound_methods_and_lexical_scope_are_part_of_the_census():
    classes = {"Memory": {"search": {"memory.search"}}}
    attrs = {"memory": "Memory"}
    source = """
async def explicit(self, query):
    return await runner(self.agent.memory.search, query)
async def automatic(self, messages):
    query = messages[-1].content
    return await runner(self.agent.memory.search, query)
"""
    sites = python_sites(source, "adapter.py", classes, attrs)
    assert len(sites) == 2
    assert {site.op for site in sites} == {"memory.search"}
    assert len({site.key for site in sites}) == 2


@pytest.mark.parametrize(
    ("operation", "body"),
    [
        ("conversation.append", '{"messages": messages}'),
        ("conversation.get", '{"thread_id": thread_id, "limit": 20}'),
    ],
)
def test_python_capture_wrapper_retains_literal_operation_and_payload_provenance(operation, body):
    source = f"""
async def flush(self, messages):
    return await self._capture_call("{operation}", {body})
"""
    sites = python_sites(source, "adapter.py", {}, {})
    assert {site.op for site in sites} == {operation}
    changed = python_sites(
        source.replace(body, body[:-1] + ', "other": extra}'), "adapter.py", {}, {}
    )
    assert {site.key for site in sites}.isdisjoint(site.key for site in changed)
    assert _unclassified(changed, set(), {site.key: "delivery" for site in sites})


def test_session_capture_producers_keep_their_named_operations_in_the_census():
    path = "sdk/python/loom/session.py"
    sites = python_sites((ROOT / path).read_text(), path, {}, {})
    assert {
        "context.pack",
        "conversation.get",
        "conversation.append",
        "memory.set_from_messages",
        "conversation.create",
    } <= {site.op for site in sites}


def test_cli_urllib_request_collects_the_formatted_url_operation():
    source = """
def _cmd_ingest_transcript(args):
    req = urllib.request.Request(f"{base}/v1/conversation.append", data=encoded)
    return urllib.request.urlopen(req)
"""
    sites = python_sites(source, "server/cli/shell.py", {}, {})
    assert {site.op for site in sites} == {"conversation.append"}


def test_unchanged_call_with_changed_argument_provenance_invalidates_source_review(tmp_path):
    path = tmp_path / "adapter.py"
    path.write_text('query = request.query\nclient.call("knowledge.search", {"query": query})\n')
    pins = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()}
    assert not _changed_sources(tmp_path, pins)
    path.write_text(
        'query = messages[-1].content\nclient.call("knowledge.search", {"query": query})\n'
    )
    assert _changed_sources(tmp_path, pins) == {path.name}
