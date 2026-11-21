"""Raw policy intent and canonical numeric boundaries survive closed parsing."""

from __future__ import annotations

import hashlib
import json

import pytest


def _digest(command):
    return hashlib.sha256(
        json.dumps(
            {"action": command.action, "arguments": command.arguments},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


@pytest.mark.parametrize("payload", ["line1\nline2", "first\tsecond", "text\x7fend"])
def test_raw_setting_value_and_expected_keep_controls(payload, capsys):
    from server.cli.cell_each import parse_command

    command = parse_command(
        ["settings", "set", "extraction_defer_agents", payload, "--expect", payload]
    )
    assert command.arguments["value"] == payload
    assert command.arguments["expected"] == payload
    assert command.digest == _digest(command)
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("payload", ["line1\nline2", "first\tsecond", "text\x7fend"])
def test_global_policy_payload_keeps_controls_in_private_intent(payload, capsys):
    from server.cli.cell_each import parse_command

    command = parse_command(["blacklist", "global", "add", payload, "--reason", payload])
    assert command.arguments["pattern"] == payload
    assert command.arguments["reason"] == payload
    assert command.digest == _digest(command)
    assert capsys.readouterr() == ("", "")


def test_exact_subject_reason_preserves_raw_text():
    from server.cli.cell_each import parse_command

    command = parse_command(
        ["revoke-token", "--subject", "sa:worker", "--reason", "one\ntwo\tthree"]
    )
    assert command.arguments["reason"] == "one\ntwo\tthree"
    assert command.digest == _digest(command)


@pytest.mark.parametrize(
    "tokens",
    [
        ["settings", "list", "--prefix", "bad\nname"],
        ["settings", "set", "bad\tname", "raw\nvalue"],
        ["signing-key", "retire", "bad\x7fkid"],
        ["revoke-token", "--subject", "sa:bad\nname"],
        ["ratelimit", "reset", "--namespace", "bad\tname", "--op-class", "memory"],
        ["ratelimit", "reset", "--namespace", "work", "--op-class", "bad\nclass"],
        ["budget", "reset", "bad\nop"],
        ["budget", "reset", "memory.get", "--consumer", "bad\tconsumer"],
        ["blacklist", "global", "add", "bad\0pattern"],
        ["settings", "set", "name", "bad\0value"],
    ],
)
def test_identifier_and_nul_refusals_remain(tokens):
    from server.cli.cell_each import parse_command

    with pytest.raises(ValueError) as refused:
        parse_command(tokens)
    assert str(refused.value) == "Unsupported cell administration command"


@pytest.mark.parametrize("raw", ["1e-29", "10e-30"])
def test_multiplier_accepts_exact_32_byte_canonical_boundary(raw):
    from server.cli.cell_each import parse_command

    command = parse_command(
        ["budget", "set", "memory.get", "--consumer", "worker", "--multiplier", raw]
    )
    expected = "0." + "0" * 28 + "1x"
    assert len(expected.encode("ascii")) == 32
    assert command.arguments["budget"] == expected
    assert command.digest == _digest(command)


@pytest.mark.parametrize("raw", ["1e-30", "1.1e-29", "1e-999", "1e-999999999"])
def test_multiplier_refuses_oversized_canonical_expansion(raw):
    from server.cli.cell_each import parse_command

    with pytest.raises(ValueError):
        parse_command(["budget", "set", "memory.get", "--consumer", "worker", "--multiplier", raw])


@pytest.mark.parametrize("raw", ["0e-999", "0e999"])
def test_zero_multiplier_is_canonical_without_exponent_expansion(raw):
    from server.cli.cell_each import parse_command

    command = parse_command(
        ["budget", "set", "memory.get", "--consumer", "worker", "--multiplier", raw]
    )
    assert command.arguments["budget"] == "0x"
    assert command.digest == _digest(command)
