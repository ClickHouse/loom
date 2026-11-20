"""Pure checks on the fingerprint reservations shared by cell controllers."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from server.core import cell, org_patterns
from server.router.limits import MAX_BODY_BYTES


def readable(value: Any) -> bool:
    """Known fields have exact types; unknown JSON fields remain reserved and intact."""
    if not isinstance(value, dict) or set(value) != {"v", "fields"}:
        return False
    if type(value["v"]) is not int or value["v"] != cell.FINGERPRINT_VERSION:
        return False
    fields = value["fields"]
    if not isinstance(fields, dict):
        return False
    try:
        # Reject cycles, non-JSON values and nonfinite floats before walking nested fields.
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError, RecursionError):
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
            if any(not isinstance(key, str) for key in item):
                return False
            pending.extend(item.keys())
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
        elif item is not None and type(item) not in (bool, int, float):
            return False
    for name, default in cell.DEFAULTS.items():
        item = fields.get(name, default)
        if type(item) is not type(default):
            return False
        if isinstance(item, list) and any(not isinstance(part, str) for part in item):
            return False
    for name in ("configured_orgs", "end_user_orgs"):
        for org in fields.get(name, []):
            if cell.named_org(org) is None or cell.org_name_refusal(org):
                return False
    return True


def registration_refusal(value: Any, registered: Mapping[str, Any], name: str = "candidate") -> str:
    """A fingerprint accepts only the registered stores and host."""
    if not readable(value):
        return f"{name}: unreadable fingerprint; change fingerprints with `loom cell update`"
    expected = {
        "deployment_id": registered["deployment_id"],
        "bucket": registered["bucket"],
        "bucket_prefix": registered["bucket_prefix"],
        "public_host": registered["host"],
    }
    fields = value["fields"]
    changed = [
        name for name, want in expected.items() if fields.get(name, cell.DEFAULTS[name]) != want
    ]
    if changed:
        return (
            f"{name}'s {', '.join(changed)} differ from cell {registered['cell']!r}'s registration"
        )
    if fields.get("oauth_issuer_host", cell.DEFAULTS["oauth_issuer_host"]) != registered["host"]:
        return (
            f"{name}'s oauth_issuer_host differs from the registered host: the router "
            "attaches no issuer host other than the cell's own"
        )
    return ""


def registration_pair_refusal(
    registered: Mapping[str, Any], current: dict[str, Any], pending: dict[str, Any] | None
) -> str:
    """Both reserved values must name the registered stores and fit live placement."""
    for name, value in (("current_fp", current), ("next_fp", pending)):
        if value is not None:
            if reason := registration_refusal(value, registered, name):
                return reason
            if reason := placement_refusal(registered["cell"], name, value):
                return reason
    return ""


def _pair_refusal(a: dict[str, Any], b: dict[str, Any]) -> str:
    if set(a.get("bearer_issuers", [])) & set(b.get("bearer_issuers", [])):
        return "shared bearer issuer reservation"
    oidc_a = (a.get("oidc_issuer", "").rstrip("/"), a.get("oidc_client_id", ""))
    oidc_b = (b.get("oidc_issuer", "").rstrip("/"), b.get("oidc_client_id", ""))
    if all(oidc_a) and oidc_a == oidc_b:
        return "shared OIDC issuer and client reservation"
    patterns_a, patterns_b = a.get("end_user_orgs", []), b.get("end_user_orgs", [])
    if any(org_patterns.overlap(x, y) for x in patterns_a for y in patterns_b):
        return "overlapping end-user org pattern reservation"
    orgs_a, orgs_b = a.get("configured_orgs", []), b.get("configured_orgs", [])
    if (
        set(orgs_a) & set(orgs_b)
        or any(org_patterns.matches(pattern, org) for pattern in patterns_a for org in orgs_b)
        or any(org_patterns.matches(pattern, org) for pattern in patterns_b for org in orgs_a)
    ):
        return "overlapping configured org reservation"
    return ""


def reservation_refusal(accepted: Sequence[dict[str, Any]], other: Sequence[dict[str, Any]]) -> str:
    """Compare every accepted value, including next, with another cell's reservation.

    Registration identity, placement winners and transaction locks are the caller's
    checks. Empty `other` means registration has not reserved a fingerprint yet.
    """
    if not accepted or any(not readable(value) for value in (*accepted, *other)):
        return "unreadable fingerprint reservation"
    for a in accepted:
        for b in other:
            if reason := _pair_refusal(a["fields"], b["fields"]):
                return reason
    return ""


class ConfigurationRefused(ValueError):
    """A configuration action refused for a fixed, caller-facing reason."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def configuration_pair(
    current: dict[str, Any] | None,
    pending: dict[str, Any] | None,
    target: dict[str, Any],
    action: str,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Select the reserved pair without guessing the direction of an interrupted update."""
    if any(not readable(value) for value in (current, target)) or (
        pending is not None and not readable(pending)
    ):
        raise ConfigurationRefused("configuration update needs readable fingerprints")
    assert current is not None
    if action not in ("config", "finish", "revert"):
        raise ConfigurationRefused("configuration action must be config, finish or revert")

    def same(a: dict[str, Any], b: dict[str, Any]) -> bool:
        return json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)

    if action == "config":
        if pending is not None and not same(pending, target):
            raise ConfigurationRefused(
                "another configuration is pending; finish or revert it first"
            )
        if pending is not None:
            return current, pending
        return current, None if same(current, target) else target
    selected = pending if action == "finish" and pending is not None else current
    if not same(selected, target):
        raise ConfigurationRefused(
            "configuration target differs from the selected reserved fingerprint"
        )
    return target, None


# An org as a placement body carries it at most: ORG_MAX_BYTES bytes, each escaped in JSON
# to at most six (`\u00XX`).
_PLACEMENT_ORG_MAX = 6 * cell.ORG_MAX_BYTES  # not a setting: derived


def placement_refusal(cell_name: str, name: str, value: dict[str, Any]) -> str:
    """Why a fingerprint cannot fit both live and operator placement bodies.

    Reserve the longest escaped org in each envelope and name only the largest fields.
    """
    # Native callers materialize known defaults; retain unknown stored fields too.
    value = {**value, "fields": {**cell.DEFAULTS, **value["fields"]}}
    # The default encoding is the longest: spaces after separators, non-ASCII escaped.
    size = (
        max(
            len(json.dumps({"org": "", "cell": cell_name, "fingerprint": value})),
            len(
                json.dumps(
                    {
                        "org": "",
                        "deployment_id": value["fields"].get(
                            "deployment_id", cell.DEFAULTS["deployment_id"]
                        ),
                        "fingerprint": value,
                    }
                )
            ),
        )
        + _PLACEMENT_ORG_MAX
    )
    if size <= MAX_BODY_BYTES:
        return ""
    sizes = sorted(((len(json.dumps(v)), k) for k, v in value["fields"].items()), reverse=True)
    largest = ", ".join(k for _, k in sizes[:3])
    return (
        f"{name} makes a placement body of up to {size} bytes, past the router's "
        f"{MAX_BODY_BYTES}: its largest fields are {largest}"
    )
