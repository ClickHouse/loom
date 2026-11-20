"""A configuration action reserves only its selected pair and preserves unknown fields."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from server.core import cell
from server.router import directory, fingerprints


def value(issuer: str) -> dict[str, Any]:
    return cell.as_value({"bearer_issuers": [issuer], "future": {"nested": [False, 0]}})


@pytest.mark.parametrize(
    "action,pending,target,want",
    [
        ("config", None, "new", ("old", "new")),
        ("config", "new", "new", ("old", "new")),
        ("config", None, "old", ("old", None)),
        ("finish", "new", "new", ("new", None)),
        ("revert", "new", "old", ("old", None)),
        ("finish", None, "old", ("old", None)),
        ("revert", None, "old", ("old", None)),
    ],
)
def test_configuration_pair_selects_exact_reserved_values(
    action: str, pending: str | None, target: str, want: tuple[str, str | None]
) -> None:
    current, next_fp, chosen = value("old"), value(pending) if pending else None, value(target)
    before = deepcopy((current, next_fp, chosen))
    result = fingerprints.configuration_pair(current, next_fp, chosen, action)
    assert result == (value(want[0]), value(want[1]) if want[1] else None)
    assert (current, next_fp, chosen) == before


@pytest.mark.parametrize(
    "action,pending,target",
    [
        ("config", "new", "third"),
        ("config", "new", "old"),
        ("finish", "new", "old"),
        ("revert", "new", "new"),
        ("finish", None, "new"),
        ("revert", None, "new"),
        ("unsupported", "new", "new"),
    ],
)
def test_configuration_pair_refuses_a_different_pending_or_selected_target(
    action: str, pending: str | None, target: str
) -> None:
    with pytest.raises(ValueError):
        fingerprints.configuration_pair(
            value("old"), value(pending) if pending else None, value(target), action
        )


@pytest.mark.parametrize("broken", [None, {"v": True, "fields": {}}, {"v": 2, "fields": {}}])
def test_configuration_pair_never_invents_an_unreadable_current(broken: Any) -> None:
    with pytest.raises(ValueError, match="fingerprint"):
        fingerprints.configuration_pair(broken, None, value("new"), "config")


def test_configuration_pair_uses_type_sensitive_unknown_field_equality() -> None:
    current, pending, target = value("old"), value("new"), value("new")
    target["fields"]["future"]["nested"][0] = 0
    assert pending == target  # Python equality cannot implement the reservation CAS.
    with pytest.raises(ValueError, match="pending"):
        fingerprints.configuration_pair(current, pending, target, "config")


@pytest.mark.parametrize("action", ["config", "finish", "revert"])
def test_configuration_pair_selects_scim_for_the_native_owner_precondition(action: str) -> None:
    target = value("new")
    target["fields"]["scim"] = True
    current = target if action == "revert" else value("old")
    assert fingerprints.configuration_pair(
        current, target if action == "finish" else None, target, action
    ) == ((current, target) if action == "config" else (target, None))


def test_configuration_pair_allows_revert_from_an_existing_scim_candidate() -> None:
    pending = value("new")
    pending["fields"]["scim"] = True
    assert fingerprints.configuration_pair(value("old"), pending, value("old"), "revert") == (
        value("old"),
        None,
    )


@pytest.mark.asyncio
async def test_configuration_refusals_keep_fixed_reasons_without_relaying_unknown_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private = "private-untrusted-input"
    cases = [
        (
            None,
            None,
            value(private),
            "config",
            "configuration update needs readable fingerprints",
        ),
        (
            value("old"),
            None,
            value(private),
            private,
            "configuration action must be config, finish or revert",
        ),
        (
            value("old"),
            value("new"),
            value(private),
            "config",
            "another configuration is pending; finish or revert it first",
        ),
        (
            value("old"),
            value("new"),
            value(private),
            "finish",
            "configuration target differs from the selected reserved fingerprint",
        ),
    ]

    def no_connection():
        raise AssertionError("configuration validation must finish before directory I/O")

    monkeypatch.setattr(directory, "_connection", no_connection)
    for current, pending, target, action, reason in cases:
        with pytest.raises(directory.OperatorRefused) as outward:
            await directory.update_configuration(
                "first",
                target,
                action,
                keys_fp="unused",
                refusal=lambda _: "",
                expected=directory.Control({"current_fp": current, "next_fp": pending}, None),
            )
        assert outward.value.reason == reason
        assert private not in outward.value.reason

    failure = ValueError(private)

    def unexpected(*args):
        raise failure

    monkeypatch.setattr(fingerprints, "configuration_pair", unexpected)
    with pytest.raises(ValueError) as unhandled:
        await directory.update_configuration(
            "first",
            value("new"),
            "config",
            keys_fp="unused",
            refusal=lambda _: "",
            expected=directory.Control({"current_fp": value("old"), "next_fp": None}, None),
        )
    assert unhandled.value is failure
