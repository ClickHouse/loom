"""Validate historical memory treatment and its native setup receipts."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from examples.software_factory.factory.model import digest

from .anchors import identity as anchor_identity
from .anchors import matches as anchor_matches
from .anchors import validate as validate_anchors
from .arms import ALL


def treatment_fixture(fixture, arm):
    """Apply the frozen arm's history treatment before setup and receipt validation."""
    if not ALL[arm].memory_enabled:
        raise PermissionError("Memory is disabled for the selected arm")
    return fixture


def _require(condition, message):
    if not condition:
        raise PermissionError(message)


def _sha(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _commit(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}", value) is not None


def _value_sha(value):
    # The native wire contract uses compact canonical JSON (server.core.hashing).
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _file(path, sha):
    path = Path(path)
    _require(_sha(sha) and not path.is_symlink(), "History artifact is not a regular frozen input")
    content = path.read_bytes()
    _require(hashlib.sha256(content).hexdigest() == sha, "Frozen history artifact bytes changed")
    return json.loads(content)


def validate_history_certificate(protocol, slot, binding):
    """Return a verified certificate or refuse; this function performs no API calls.

    This checks immutable native receipts against the public fixture, not a claim that
    an operator marked the namespace qualified. The setup runner must produce those
    receipts from actual API calls, index revisions and completed native worker passes.
    """
    try:
        return _validate_history(protocol, slot, binding)
    except (KeyError, TypeError, ValueError, AttributeError, OSError) as error:
        raise PermissionError(f"Native history certificate refused: {error}") from error


def _validate_history(protocol, slot, binding):
    _require(
        slot.get("arm") in ALL
        and ALL[slot["arm"]].memory_enabled
        and slot.get("memory_enabled") is True,
        "Certificate requested for an arm without native memory",
    )
    _require(
        protocol["attempt_scopes"][str(slot["order"])] == binding,
        "History binding differs from frozen attempt",
    )
    _require(
        binding.get("memory_namespace")
        and binding["code_namespace"] != binding["memory_namespace"],
        "Memory scope is missing or aliases code",
    )
    _require(
        binding["task_id"] == slot["task_id"] and binding["index_tier"] == slot["index_tier"],
        "History task or tier differs from schedule",
    )
    for order, other in protocol["attempt_scopes"].items():
        if order == str(slot["order"]):
            continue
        if other["org"] == binding["org"]:
            _require(
                other["code_namespace"] == binding["code_namespace"]
                and other["task_id"] == binding["task_id"]
                and other["index_tier"] == binding["index_tier"],
                "Shared org has an ambiguous repository trunk binding",
            )
            _require(
                other.get("memory_namespace") != binding["memory_namespace"],
                "Two attempts share mutable memory",
            )
    fixture_info = protocol["memory_histories"][slot["task_id"]]
    fixture = treatment_fixture(
        _file(fixture_info["fixture_file"], fixture_info["fixture_sha256"]), slot["arm"]
    )
    cert = _file(binding["history_certificate_file"], binding["history_certificate_sha256"])
    _require(
        cert.get("schema_version") == 1
        and cert.get("kind") == "factory-native-history"
        and cert.get("qualified") is True,
        "Unknown or unqualified native history certificate",
    )
    expected = {
        key: binding[key] for key in ("org", "code_namespace", "memory_namespace", "repository_tag")
    }
    expected.update(
        slot_order=slot["order"],
        task_id=slot["task_id"],
        arm=slot["arm"],
        repeat=slot["repeat"],
        trunk_namespace=binding["code_namespace"],
        fixture_sha256=fixture_info["fixture_sha256"],
        public_history_sha256=digest(fixture["public_documents"]),
    )
    _require(
        all(cert.get(key) == value for key, value in expected.items()),
        "History certificate identity or public fixture differs",
    )
    _require(
        cert["public_history_sha256"] == binding["memory_history_sha256"],
        "Public notes and native history differ",
    )
    _require(fixture["case_id"] == slot["task_id"], "History fixture names another task")
    events = fixture["history_events"]
    cold = fixture.get("kind") == "cold"
    snapshot = fixture.get("kind") == "historical-snapshot"
    _require(bool(events) != cold, "Cold/history fixture is inconsistent")
    revisions = ("B",) if cold or snapshot else ("A", "B")
    _require(
        set(cert["source_heads"]) == set(revisions)
        and set(cert["source_receipts"]) == set(revisions),
        "History source revisions are incomplete",
    )
    _require(
        cert["source_heads"]["B"] == binding["base_commit"],
        "History does not end at the current task commit",
    )
    if not cold and not snapshot:
        _require(
            cert["source_heads"]["A"] != cert["source_heads"]["B"],
            "History did not advance the actual indexed revision",
        )
    if not cold and "source_revisions" not in fixture:
        from .source_tree import source_files

        roots = protocol["suite"]["source_roots"]
        fixture = {
            **fixture,
            "source_revisions": {
                revision: source_files(
                    fixture["source_git"], fixture["revisions"][revision]["commit"], roots
                )
                for revision in revisions
            },
        }
        _require(
            all(
                cert["source_heads"][revision] == fixture["revisions"][revision]["commit"]
                for revision in revisions
            ),
            "Native history uses different public Git revisions",
        )
    for revision in revisions:
        _source_receipt(cert, fixture, binding, revision)
    if cold:
        _require(
            fixture["public_documents"] == {}
            and cert["events"] == []
            and cert["final_snapshot"] == [],
            "Cold memory contains seeded content",
        )
        empty = cert["empty_namespace"]
        _require(
            empty["operation"] == "memory.list" and empty.get("request_id"),
            "Cold namespace lacks a native empty listing",
        )
        request, response = empty["request"], empty["response"]
        _require(
            request.get("org") == binding["org"]
            and request.get("namespace") == binding["memory_namespace"]
            and all(request.get(k) == "*" for k in ("agent", "user_id", "session_id")),
            "Cold listing does not span the whole namespace",
        )
        _require(
            not any(
                request.get(k)
                for k in (
                    "prefix",
                    "contains",
                    "tags",
                    "known_version",
                    "memory_type",
                    "source_trace",
                    "as_of",
                    "count_only",
                )
            ),
            "Cold listing is filtered or conditional",
        )
        _require(
            all(
                request.get(key) is True
                for key in (
                    "include_tombstoned",
                    "include_duplicates",
                    "include_superseded",
                    "include_proposed",
                )
            ),
            "Cold listing hides inactive rows",
        )
        _require(
            response.get("entries") == []
            and response.get("truncated") is False
            and response.get("total") == 0
            and response.get("unchanged") is False
            and not response.get("hidden")
            and not response.get("ignored_args"),
            "Cold namespace was not observed empty",
        )
        return cert
    _require(
        fixture.get("history_events_sha256") == digest(events),
        "Public history operation hash differs",
    )
    _require(
        fixture.get("public_documents_sha256") == digest(fixture["public_documents"]),
        "Public history document hash differs",
    )
    writes = [
        item for item in cert["events"] if item.get("operation") in ("memory.set", "memory.update")
    ]
    _require(len(writes) == len(events), "Native history omits or adds fixture writes")
    for actual, public in zip(writes, events, strict=True):
        request = actual["request"]
        _require(
            actual["operation"] == "memory." + public["operation"]
            and actual["phase"] == public["revision"]
            and request.get("memory_key") == public["key"]
            and request.get("value") == public["value"],
            "Native writes differ from the published sequence",
        )
        if public["operation"] == "set":
            _require(
                request.get("files", []) == public["files"]
                and binding["repository_tag"] in request.get("tags", [])
                and [tag for tag in request["tags"] if tag.startswith("repo:")]
                == [binding["repository_tag"]],
                "Native history anchors differ from the public fixture",
            )
    if snapshot:
        _snapshot_events(cert, fixture, binding)
    else:
        _history_events(cert, fixture, binding)
    return cert


def _snapshot_events(cert, fixture, binding):
    """Prove seeded historical facts and current native links without inventing a code history."""
    expected = {row["key"]: row for row in fixture["history_events"]}
    values, read_back, request_ids = {}, {}, set()
    _require(
        len(expected) == len(fixture["history_events"])
        and all(row["operation"] == "set" and row["revision"] == "B" for row in expected.values())
        and {key: row["value"] for key, row in expected.items()} == fixture["public_documents"],
        "Snapshot does not contain the exact historical corpus",
    )
    for event in cert["events"]:
        request, response = event["request"], event["response"]
        key = request["memory_key"]
        _require(
            key in expected
            and event["phase"] == "B"
            and event["source_commit"] == binding["base_commit"]
            and event.get("request_id")
            and event["request_id"] not in request_ids
            and event.get("physical_requests") == 1
            and event.get("status_code") == 200
            and request.get("org") == binding["org"]
            and request.get("namespace") == binding["memory_namespace"]
            and not request.get("as_of")
            and not response.get("ignored_args"),
            "Snapshot operation lacks a unique native receipt in the bound scope",
        )
        request_ids.add(event["request_id"])
        _require(
            response.get("memory_id")
            and type(response.get("version")) is int
            and response["version"] > 0
            and response.get("content_sha256") == _value_sha(expected[key]["value"]),
            "Snapshot has no persisted version/hash proof",
        )
        if event["operation"] == "memory.set":
            _require(
                key not in values
                and response.get("status") == "active"
                and response.get("noop") is False
                and response.get("dry_run") is False,
                "Historical fact was not persisted as a distinct active row",
            )
            values[key] = response
        else:
            _require(
                event["operation"] == "memory.get"
                and key in values
                and response.get("found") is True
                and response.get("value") == expected[key]["value"]
                and all(
                    response[field] == values[key][field]
                    for field in ("memory_id", "version", "content_sha256")
                ),
                "Snapshot get does not confirm its persisted historical fact",
            )
            links = response.get("code", [])
            _require(
                isinstance(links, list)
                and bool(links) == bool(expected[key]["files"])
                and all(
                    link.get("code_namespace") == binding["code_namespace"]
                    and link.get("path") in expected[key]["files"]
                    and link.get("state") == "fresh"
                    for link in links
                ),
                "Historical code links do not match the current certified source",
            )
            read_back[key] = dict(response, memory_key=key)
    _require(
        set(values) == set(read_back) == set(expected)
        and cert["final_snapshot"] == [read_back[key] for key in read_back],
        "Historical snapshot is incomplete or differs from native reads",
    )


def _source_receipt(cert, fixture, binding, revision):
    head = cert["source_heads"][revision]
    receipt = cert["source_receipts"][revision]
    _require(
        _commit(head)
        and receipt.get("indexed_head") == head
        and (
            receipt.get("checked_head") == head
            or (fixture.get("kind") == "cold" and receipt.get("checked_head") == "")
        )
        and receipt.get("request_id"),
        "Indexed/checked history head is unproven",
    )
    _require(
        receipt.get("source_sha256") == fixture["source_sha256"][revision],
        "Indexed history source differs from public revision",
    )
    status = receipt["index_status"]
    _require(
        status.get("org") == binding["org"]
        and status.get("namespace") == binding["code_namespace"]
        and status.get("git_head") == head
        and not any(status.get(key) for key in ("ignored_args", "restricted", "abstained")),
        "Native index status does not prove the bound source revision",
    )
    scope = receipt["source_scope"]
    _require(
        scope.get("verified") is True
        and scope.get("base_commit") == head
        and scope.get("source_sha256") == fixture["source_sha256"][revision],
        "History source scope is unverified",
    )
    if "source_revisions" in fixture:
        files = fixture["source_revisions"][revision]
        _require(
            digest(files) == fixture["source_sha256"][revision]
            and scope.get("paths_sha256") == digest(sorted(files))
            and scope.get("file_count") == len(files),
            "Indexed history paths/content differ from the frozen fixture",
        )
    if revision == "B":
        _require(
            scope == binding["source_scope"], "Final history coverage differs from selected index"
        )
    worker = receipt["worker"]
    _require(
        worker.get("operation") == "memory_code_links"
        and worker.get("run_id")
        and type(worker["result"].get("errors")) is int
        and worker["result"]["errors"] == 0,
        "Native link worker did not finish without errors",
    )


def _history_events(cert, fixture, binding):
    values, ids, versions, latest_read, latest_row = {}, {}, {}, {}, {}
    file_keys, fresh_a, stale_b, fresh_b = set(), set(), set(), set()
    expected_b = set()
    request_ids = set()
    advanced = False
    anchors = {}
    allowed_paths = set(fixture["source_revisions"]["B"])
    changes = validate_anchors(cert, fixture, binding)
    initial_links, changed_memories = {}, set()
    affected_updates = set()
    for event in cert["events"]:
        operation, phase = event["operation"], event["phase"]
        _require(
            operation in ("memory.set", "memory.update", "memory.get")
            and phase in ("A", "B-stale", "B"),
            "Unexpected native history operation or phase",
        )
        _require(
            not (advanced and phase == "A"),
            "History events return to A after the indexed source advanced",
        )
        advanced |= phase != "A"
        request, response = event["request"], event["response"]
        _require(
            not request.get("as_of"), "Native history is an as-of replay rather than a live read"
        )
        _require(
            event.get("request_id")
            and event["request_id"] not in request_ids
            and not response.get("ignored_args"),
            "Native history request identity is missing, reused or ignored",
        )
        request_ids.add(event["request_id"])
        _require(
            request.get("org") == binding["org"]
            and request.get("namespace") == binding["memory_namespace"],
            "Native history request crosses scope",
        )
        _require(
            event["source_commit"] == cert["source_heads"]["A" if phase == "A" else "B"],
            "Native event does not name its indexed revision",
        )
        key = request["memory_key"]
        _require(
            response.get("namespace", binding["memory_namespace"]) == binding["memory_namespace"]
            and response.get("memory_key", key) == key,
            "Native history response names another memory scope or key",
        )
        if operation in ("memory.set", "memory.update"):
            _require(phase != "B-stale", "Staleness observation contains a write")
            version = response.get("version" if operation == "memory.set" else "new_version")
            _require(
                type(version) is int
                and version > versions.get(key, 0)
                and response.get("memory_id")
                and response.get("content_sha256") == _value_sha(request["value"]),
                "Native history write has no persisted version/hash proof",
            )
            if operation == "memory.set":
                _require(
                    key not in values
                    and response.get("noop") is False
                    and response.get("dry_run") is False
                    and response.get("status") == "active",
                    "Native initial write did not create an active row",
                )
                if request.get("files"):
                    file_keys.add(key)
                    anchors[key] = set(request["files"])
            else:
                _require(
                    key in values
                    and response.get("updated") is True
                    and response["memory_id"] == ids[key]
                    and latest_read.get(key, {}).get("content_sha256")
                    == request.get("if_match_sha256")
                    and request["if_match_sha256"] == _value_sha(values[key]),
                    "Native update is not a CAS revision of the same persisted row",
                )
                if key in changed_memories:
                    _require(
                        key in stale_b, "History was revised without observing actual stale code"
                    )
                    affected_updates.add(key)
            values[key], ids[key], versions[key] = request["value"], response["memory_id"], version
            if phase == "B" and key in file_keys:
                expected_b.add(key)
        else:
            _require(
                key in values
                and response.get("found") is True
                and response.get("memory_id") == ids[key]
                and response.get("value") == values[key]
                and response.get("version") == versions[key]
                and response.get("content_sha256") == _value_sha(values[key]),
                "Native get does not confirm the exact persisted memory version",
            )
            _require(response.get("memory_key", key) == key, "Native get returned another key")
            links = response.get("code", [])
            _require(isinstance(links, list), "Native code links are absent")
            for link in links:
                _require(
                    link.get("code_namespace") == binding["code_namespace"]
                    and link.get("path") in allowed_paths
                    and (key not in anchors or link.get("path") in anchors[key])
                    and link.get("state") in ("fresh", "moved", "changed", "unresolved", "missing"),
                    "Native memory link exceeds certified code scope",
                )
            if key in file_keys:
                _require(bool(links), "Anchored memory has no native code link")
                if phase == "A":
                    _require(
                        all(link["state"] == "fresh" for link in links),
                        "Initial memory was never linked fresh",
                    )
                    fresh_a.add(key)
                    initial_links[key] = {anchor_identity(link) for link in links}
                    if any(changes[anchor_identity(link)] == "changed" for link in links):
                        changed_memories.add(key)
                elif phase == "B-stale":
                    _require(
                        key in fresh_a
                        and {anchor_identity(link) for link in links} == initial_links[key]
                        and all(
                            anchor_matches(link, changes, cert["source_heads"]["B"])
                            for link in links
                        ),
                        "Native link state differs from its specific code fingerprint",
                    )
                    if key in changed_memories:
                        stale_b.add(key)
                elif key in expected_b:
                    _require(
                        all(link["state"] == "fresh" for link in links),
                        "Updated memory was not relinked fresh",
                    )
                    fresh_b.add(key)
                else:
                    _require(
                        {anchor_identity(link) for link in links} == initial_links[key]
                        and all(
                            anchor_matches(link, changes, cert["source_heads"]["B"])
                            for link in links
                        ),
                        "Unrevised memory link state differs from its code fingerprint",
                    )
            else:
                _require(not links, "A fixture row declared unlinked gained native code links")
            latest_read[key], latest_row[key] = response, response
    initial_anchored = {
        event["key"]
        for event in fixture["history_events"]
        if event["revision"] == "A" and event["files"]
    }
    _require(
        initial_anchored <= fresh_a
        and changed_memories <= stale_b
        and expected_b <= fresh_b
        and bool(affected_updates),
        "Native fresh/stale/relink lifecycle evidence is incomplete",
    )
    final = cert["final_snapshot"]
    _require(
        isinstance(final, list)
        and len(final) == len(values)
        and {row["memory_key"] for row in final} == set(values),
        "Final native memory snapshot has missing or extra rows",
    )
    _require(
        all(
            row == dict(latest_row.get(row["memory_key"], {}), memory_key=row["memory_key"])
            for row in final
        ),
        "Final snapshot is not the last actual native get for every row",
    )
