"""Bounded, canonical pages of a cell's permanent directory placements."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from server.core import cell
from server.router.limits import ATTACH_BATCH, ATTACH_BEGIN_MAX_BYTES

MAX_RESPONSE_BYTES = ATTACH_BEGIN_MAX_BYTES
DESCRIPTOR_KEYS = frozenset(
    {
        "cell",
        "deployment_id",
        "is_first",
        "state",
        "mode",
        "bucket",
        "bucket_prefix",
        "current_fp",
        "next_fp",
        "placed",
        "sole_cell",
    }
)


class SnapshotChanged(ValueError):
    """Placements grew during a traversal; begin again without discarding copied rows."""


def encode(value: Any) -> bytes:
    """Canonical JSON keeps the hash type-sensitive and the byte limit reproducible."""
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    except (TypeError, ValueError, RecursionError, UnicodeError) as error:
        raise ValueError("restore metadata is not bounded JSON") from error


def fingerprint(value: Any) -> bool:
    if not isinstance(value, dict) or set(value) != {"v", "fields"}:
        return False
    if type(value["v"]) is not int or value["v"] != cell.FINGERPRINT_VERSION:
        return False
    fields = value["fields"]
    if not isinstance(fields, dict):
        return False
    for name, default in cell.DEFAULTS.items():
        if name not in fields:
            continue
        item = fields[name]
        if type(item) is not type(default):
            return False
        if isinstance(item, list) and any(not isinstance(part, str) for part in item):
            return False
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, str):
            try:
                if "\0" in item:
                    return False
                item.encode()
            except UnicodeError:
                return False
        elif isinstance(item, dict):
            pending.extend(item.keys())
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
    encode(value)
    return True


def validate_descriptor(value: Any) -> None:
    if not isinstance(value, dict) or set(value) != DESCRIPTOR_KEYS:
        raise ValueError("restore descriptor has unexpected fields")
    if not isinstance(value["cell"], str) or not cell.NAME.fullmatch(value["cell"]):
        raise ValueError("restore descriptor names an invalid cell")
    for name in ("deployment_id", "bucket", "bucket_prefix"):
        if not isinstance(value[name], str) or "\0" in value[name]:
            raise ValueError(f"restore descriptor has invalid {name}")
        value[name].encode()
    if not value["deployment_id"]:
        raise ValueError("restore descriptor has no deployment identity")
    if any(type(value[name]) is not bool for name in ("is_first", "sole_cell")):
        raise ValueError("restore descriptor flags are booleans")
    if value["state"] not in ("attaching", "serving") or value["mode"] not in cell.MODES:
        raise ValueError("restore descriptor has unsupported state or mode")
    if not value["is_first"] and value["mode"] != "enforced":
        raise ValueError("a later cell must remain enforced")
    if value["state"] == "serving" and value["mode"] != "enforced":
        raise ValueError("a serving cell must remain enforced")
    if type(value["placed"]) is not int or not 0 <= value["placed"] <= cell.PLACEMENTS_MAX:
        raise ValueError("restore descriptor has an invalid placement count")
    current, nxt = value["current_fp"], value["next_fp"]
    if current is None:
        if nxt is not None or value["state"] == "serving":
            raise ValueError("restore descriptor has no accepted current fingerprint")
    elif not fingerprint(current) or (nxt is not None and not fingerprint(nxt)):
        raise ValueError("restore descriptor contains an unsupported fingerprint")
    for fp in (current, nxt):
        if fp is None:
            continue
        for name in ("deployment_id", "bucket", "bucket_prefix"):
            if fp["fields"].get(name, cell.DEFAULTS[name]) != value[name]:
                raise ValueError(f"restore fingerprint disagrees with {name}")
    encode(value)


def digest(descriptor: dict[str, Any]) -> str:
    return hashlib.sha256(encode(descriptor)).hexdigest()


def _orgs(orgs: Any, after: str | None) -> None:
    if not isinstance(orgs, list) or len(orgs) > ATTACH_BATCH:
        raise ValueError("restore page has an invalid org list")
    previous = after
    for org in orgs:
        if cell.named_org(org) is None or cell.org_name_refusal(org):
            raise ValueError("restore page contains an invalid org")
        if previous is not None and org <= previous:
            raise ValueError("restore page orgs are not strictly increasing")
        previous = org


def page(
    descriptor: dict[str, Any], rows: list[str], *, count: int = ATTACH_BATCH
) -> dict[str, Any]:
    validate_descriptor(descriptor)
    if type(count) is not int or not 0 < count <= ATTACH_BATCH or len(rows) > count + 1:
        raise ValueError("restore page exceeds its row bound")
    _orgs(rows[:count], None)
    answer: dict[str, Any] = {
        "descriptor": descriptor,
        "sha256": digest(descriptor),
        "orgs": [],
        "next": None,
        "done": True,
    }
    # Reserve the longest cursor before adding orgs; both cursor and names are bounded.
    used = len(encode(answer)) + 6 * cell.ORG_MAX_BYTES + 4
    for org in rows[:count]:
        size = len(encode(org)) + 1
        if used + size > MAX_RESPONSE_BYTES:
            break
        answer["orgs"].append(org)
        used += size
    if rows and not answer["orgs"]:
        raise ValueError("restore descriptor leaves no room for an org")
    answer["done"] = len(answer["orgs"]) == len(rows)
    answer["next"] = None if answer["done"] else answer["orgs"][-1]
    if len(encode(answer)) > MAX_RESPONSE_BYTES:
        raise ValueError("restore response exceeds its byte bound")
    return answer


def validate_page(
    answer: Any, *, after: str | None, previous: dict[str, Any] | None
) -> dict[str, Any]:
    if not isinstance(answer, dict) or set(answer) != {
        "descriptor",
        "sha256",
        "orgs",
        "next",
        "done",
    }:
        raise ValueError("restore page has unexpected fields")
    if len(encode(answer)) > MAX_RESPONSE_BYTES:
        raise ValueError("restore response exceeds its byte bound")
    descriptor = answer["descriptor"]
    validate_descriptor(descriptor)
    if answer["sha256"] != digest(descriptor):
        raise ValueError("restore descriptor hash differs")
    if previous is not None:
        compare(previous, descriptor)
    _orgs(answer["orgs"], after)
    if type(answer["done"]) is not bool:
        raise ValueError("restore completion is a boolean")
    if answer["done"]:
        if answer["next"] is not None:
            raise ValueError("a complete restore page has no next cursor")
        if after is None and len(answer["orgs"]) != descriptor["placed"]:
            raise ValueError("a complete first page omits directory placements")
    elif not answer["orgs"] or answer["next"] != answer["orgs"][-1]:
        raise ValueError("restore page makes no cursor progress")
    if len(answer["orgs"]) > descriptor["placed"]:
        raise ValueError("restore page exceeds the directory placement count")
    return dict(descriptor)


def compare(previous: dict[str, Any], descriptor: dict[str, Any]) -> None:
    """Distinguish monotone placement growth from a configuration change."""
    if encode(previous) != encode(descriptor):
        old = {k: v for k, v in previous.items() if k != "placed"}
        new = {k: v for k, v in descriptor.items() if k != "placed"}
        if encode(old) != encode(new) or descriptor["placed"] < previous["placed"]:
            raise ValueError("directory configuration or identity changed during restore")
        raise SnapshotChanged("directory placements changed during restore")
