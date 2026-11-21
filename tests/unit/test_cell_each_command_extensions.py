"""Closed command normalization preserves scope, identity and immutable intent."""

from __future__ import annotations

import uuid

import pytest


@pytest.mark.parametrize(
    ("tokens", "action", "arguments", "mutating"),
    [
        (
            [
                "ratelimit",
                "set",
                "--namespace",
                "work",
                "--op-class",
                "memory",
                "--capacity",
                "2",
                "--rate",
                "0",
            ],
            "ratelimit_namespace_set",
            {
                "namespace": "work",
                "op_class": "memory",
                "capacity": 2.0,
                "rate": 0.0,
                "clear": False,
            },
            True,
        ),
        (
            ["ratelimit", "reset", "--namespace", "work", "--op-class", "memory"],
            "ratelimit_namespace_set",
            {
                "namespace": "work",
                "op_class": "memory",
                "capacity": None,
                "rate": None,
                "clear": True,
            },
            True,
        ),
        (
            ["budget", "set", "memory.get", "--budget-ms", "0"],
            "budget_set",
            {"op": "memory.get", "consumer": None, "budget_ms": 0, "budget": None, "clear": False},
            True,
        ),
        (
            ["budget", "set", "*", "--consumer", "worker", "--multiplier", "1.2500"],
            "budget_set",
            {"op": "*", "consumer": "worker", "budget_ms": None, "budget": "1.25x", "clear": False},
            True,
        ),
        (
            ["budget", "reset", "memory.get", "--consumer", "worker"],
            "budget_set",
            {
                "op": "memory.get",
                "consumer": "worker",
                "budget_ms": None,
                "budget": None,
                "clear": True,
            },
            True,
        ),
        (["blacklist", "global", "list"], "blacklist_global_list", {"limit": 50}, False),
        (
            ["blacklist", "global", "remove", "7fdc7f78-5946-478e-81dc-b3effcc71f9c"],
            "blacklist_global_remove",
            {"pattern_id": "7fdc7f78-5946-478e-81dc-b3effcc71f9c"},
            True,
        ),
    ],
)
def test_fixed_extension_shapes(tokens, action, arguments, mutating):
    from server.cli.cell_each import parse_command

    command = parse_command(tokens)
    assert command.action == action
    assert command.path == "/v1/admin/cell." + action
    assert command.arguments == arguments
    assert command.mutating is mutating


def test_add_allocates_one_common_pattern_identity_after_validation(monkeypatch):
    from server.cli import cell_each

    allocated = []
    identifier = uuid.UUID("7fdc7f78-5946-478e-81dc-b3effcc71f9c")

    def allocate():
        allocated.append(identifier)
        return identifier

    monkeypatch.setattr(cell_each.uuid, "uuid4", allocate)
    command = cell_each.parse_command(
        ["blacklist", "global", "add", "denied", "--reason", "policy"]
    )
    assert command.action == "blacklist_global_add"
    assert command.arguments == {
        "pattern_id": str(identifier),
        "pattern": "denied",
        "pattern_type": "substring",
        "reason": "policy",
    }
    assert allocated == [identifier]
    command.arguments["pattern_id"] = "changed"
    assert command.arguments["pattern_id"] == str(identifier)
    assert allocated == [identifier]


@pytest.mark.parametrize(
    "tokens",
    [
        ["policy", "set", "*", "*"],
        ["ratelimit", "set", "--org", "acme", "--capacity", "2", "--rate", "1"],
        ["ratelimit", "reset", "--namespace", "*", "--op-class", "memory"],
        [
            "ratelimit",
            "set",
            "--namespace",
            "work",
            "--op-class",
            "memory",
            "--capacity",
            "0",
            "--rate",
            "1",
        ],
        [
            "ratelimit",
            "set",
            "--namespace",
            "work",
            "--op-class",
            "memory",
            "--capacity",
            "2",
            "--rate",
            "nan",
        ],
        ["budget", "set", "*", "--budget-ms", "1"],
        ["budget", "set", "memory.get", "--multiplier", "2"],
        [
            "budget",
            "set",
            "memory.get",
            "--budget-ms",
            "1",
            "--multiplier",
            "2",
            "--consumer",
            "worker",
        ],
        ["budget", "set", "memory.get", "--budget-ms", "86400001"],
        ["budget", "set", "memory.get", "--consumer", "worker", "--multiplier", "1e-999"],
        [
            "blacklist",
            "global",
            "add",
            "denied",
            "--pattern-id",
            "7fdc7f78-5946-478e-81dc-b3effcc71f9c",
        ],
        ["blacklist", "global", "add", "denied", "--sweep", "true"],
        ["blacklist", "global", "list", "--limit", "51"],
        ["settings", "list", "--prefix", "first", "--prefix", "second"],
    ],
)
def test_unsupported_scope_bounds_and_replay_controls_refuse(tokens, capsys):
    from server.cli.cell_each import parse_command

    with pytest.raises(ValueError) as refused:
        parse_command(tokens)
    assert str(refused.value) == "Unsupported cell administration command"
    assert capsys.readouterr() == ("", "")


def test_argument_limits_apply_before_normalization():
    from server.cli.cell_each import parse_command

    with pytest.raises(ValueError):
        parse_command(["settings", "list"] + ["x"] * 255)
    with pytest.raises(ValueError):
        parse_command(["settings", "set", "name", "x" * 16384])
    with pytest.raises(ValueError):
        parse_command(["revoke-token", "--subject", "sa:bad\0key"])
