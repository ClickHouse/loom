"""Native per-symbol fingerprints for historical memory lifecycle certificates."""

from __future__ import annotations

import hashlib
import re

from examples.software_factory.factory.model import digest

SYMBOL_OPTIONS = {
    "include_body": False,
    "include_history": False,
    "include_examples": False,
    "include_lessons": False,
}


def require(condition, message):
    if not condition:
        raise PermissionError(message)


def identity(link):
    return digest([link["path"], link.get("symbol", "")])


def file_hash(files, path):
    return hashlib.sha256(files[path].encode()).hexdigest() if path in files else None


def symbol_hash(event, link):
    response = event["response"]
    require(not response.get("ignored_args"), "Anchor fingerprint ignored arguments")
    definitions = response.get("definitions", [])
    if not definitions:
        require(response.get("abstain") is True, "Missing anchor has no explicit native abstention")
        return None
    matches = [
        unit
        for unit in definitions
        if unit.get("path") == link["path"] and unit.get("symbol") == link["symbol"]
    ]
    require(len(matches) == 1, "Anchor fingerprint is ambiguous or resolves outside its source")
    value = matches[0].get("body_hash")
    require(
        isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value),
        "Native anchor has no body fingerprint",
    )
    return value


def capture(api, group, links, files, head, revision):
    result = {}
    for link in links:
        key = identity(link)
        if key in result:
            continue
        row = {"path": link["path"], "symbol": link.get("symbol", "")}
        if row["symbol"]:
            event = api(
                "code.symbol",
                {
                    "org": group["org"],
                    "namespace": group["namespace"],
                    "name": row["symbol"],
                    **SYMBOL_OPTIONS,
                },
                phase=revision,
            )
            event["source_commit"] = head
            row.update(receipt=event, body_hash=symbol_hash(event, row))
        else:
            row["body_hash"] = file_hash(files, row["path"])
        require(
            revision != "A" or row["body_hash"] is not None, "Initial anchor fingerprint is missing"
        )
        result[key] = row
    return result


def changes(fingerprints):
    require(set(fingerprints) == {"A", "B"}, "Anchor revisions are incomplete")
    require(set(fingerprints["A"]) == set(fingerprints["B"]), "Anchor coverage changed")
    return {
        key: "missing"
        if fingerprints["B"][key]["body_hash"] is None
        else "fresh"
        if row["body_hash"] == fingerprints["B"][key]["body_hash"]
        else "changed"
        for key, row in fingerprints["A"].items()
    }


def matches(link, expected, head):
    state = expected.get(identity(link))
    if state == "missing":
        return link.get("state") in {"missing", "unresolved"}
    return link.get("state") == state and (state != "changed" or link.get("changed_since") == head)


def validate(cert, fixture, binding):
    fingerprints, request_ids = cert["anchor_fingerprints"], set()
    expected = {
        identity(link)
        for event in cert["events"]
        if event["phase"] == "A" and event["operation"] == "memory.get"
        for link in event["response"].get("code", [])
    }
    for revision in ("A", "B"):
        require(
            set(fingerprints[revision]) == expected, "Anchor proof does not cover initial links"
        )
        for key, row in fingerprints[revision].items():
            require(key == identity(row), "Anchor identity differs from its fingerprint")
            if row["symbol"]:
                event = row["receipt"]
                require(
                    event["operation"] == "code.symbol"
                    and event["phase"] == revision
                    and event["source_commit"] == cert["source_heads"][revision]
                    and event["status_code"] == 200
                    and event["physical_requests"] == 1
                    and event.get("request_id")
                    and event["request_id"] not in request_ids
                    and event["request"]
                    == {
                        "org": binding["org"],
                        "namespace": binding["code_namespace"],
                        "name": row["symbol"],
                        **SYMBOL_OPTIONS,
                    },
                    "Native anchor fingerprint scope or revision is unproven",
                )
                request_ids.add(event["request_id"])
                value = symbol_hash(event, row)
            else:
                value = file_hash(fixture["source_revisions"][revision], row["path"])
            require(value == row["body_hash"], "Anchor fingerprint differs from its evidence")
            require(revision != "A" or value is not None, "Initial anchor is absent")
    return changes(fingerprints)
