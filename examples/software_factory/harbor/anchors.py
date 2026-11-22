"""Native memory declaration fingerprints for historical lifecycle certificates."""

from __future__ import annotations

import hashlib
import re

from examples.software_factory.factory.model import digest

KIND = "native-memory-declaration-fingerprints"


def require(condition, message):
    if not condition:
        raise PermissionError(message)


def identity(link):
    return digest([link["path"], link.get("symbol", "")])


def file_hash(files, path):
    return hashlib.sha256(files[path].encode()).hexdigest() if path in files else None


async def capture(request):
    """Run inside the owned native server, using exactly the memory worker's parser."""
    from server.code.app.memory_links import parsed, refresh_repos
    from server.code.domain.declaration import fingerprint_version

    await refresh_repos()
    anchors, files = {}, {}
    for link in request["links"]:
        path, symbol = link["path"], link.get("symbol", "")
        if path not in files:
            files[path] = await parsed(
                request["org"], request["namespace"], request["commit"], path
            )
        source = files[path]
        value = None
        if source is not None:
            if symbol:
                hashes = {d.body_hash for d in source.defs if d.qualified_name == symbol}
                require(
                    len(hashes) <= 1, "Native declarations disagree on their overload fingerprint"
                )
                value = next(iter(hashes), None)
            else:
                # Native file links track existence and moves, rather than declaration edits.
                value = ""
        anchors[identity(link)] = {
            "path": path,
            "symbol": symbol,
            "body_hash": value,
            "file_sha256": hashlib.sha256("\n".join(source.lines).encode()).hexdigest()
            if source is not None
            else None,
        }
    return {
        "kind": KIND,
        "org": request["org"],
        "namespace": request["namespace"],
        "commit": request["commit"],
        "fingerprint_version": fingerprint_version(),
        "anchors": anchors,
    }


def changes(fingerprints):
    require(set(fingerprints) == {"A", "B"}, "Anchor revisions are incomplete")
    before, after = (fingerprints[revision]["anchors"] for revision in ("A", "B"))
    require(set(before) == set(after), "Anchor coverage changed")
    return {
        key: "missing"
        if after[key]["body_hash"] is None
        else "fresh"
        if row["body_hash"] == after[key]["body_hash"]
        else "changed"
        for key, row in before.items()
    }


def matches(link, expected, head):
    state = expected.get(identity(link))
    if state == "missing":
        return link.get("state") in {"missing", "unresolved"}
    return link.get("state") == state and (state != "changed" or link.get("changed_since") == head)


def validate(cert, fixture, binding):
    fingerprints = cert["anchor_fingerprints"]
    expected = {
        identity(link)
        for event in cert["events"]
        if event["phase"] == "A" and event["operation"] == "memory.get"
        for link in event["response"].get("code", [])
    }
    for revision in ("A", "B"):
        snapshot = fingerprints[revision]
        require(
            snapshot["kind"] == KIND
            and snapshot["org"] == binding["org"]
            and snapshot["namespace"] == binding["code_namespace"]
            and snapshot["commit"] == cert["source_heads"][revision]
            and snapshot.get("fingerprint_version") == fingerprints["A"].get("fingerprint_version")
            and isinstance(snapshot.get("fingerprint_version"), str)
            and bool(snapshot.get("fingerprint_version"))
            and set(snapshot["anchors"]) == expected,
            "Native anchor scope, revision, algorithm or coverage differs",
        )
        for key, row in snapshot["anchors"].items():
            require(key == identity(row), "Anchor identity differs from its fingerprint")
            content = file_hash(fixture["source_revisions"][revision], row["path"])
            require(
                row["file_sha256"] == content, "Native anchor source differs from frozen Git bytes"
            )
            value = row["body_hash"]
            require(revision != "A" or value is not None, "Initial anchor is absent")
            require(
                value is None
                and (bool(row["symbol"]) or content is None)
                or row["symbol"]
                and content is not None
                and isinstance(value, str)
                and re.fullmatch(r"[a-f0-9]{64}", value)
                or not row["symbol"]
                and content is not None
                and value == "",
                "Native anchor has an invalid declaration fingerprint",
            )
    return changes(fingerprints)
