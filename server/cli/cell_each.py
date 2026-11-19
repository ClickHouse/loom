"""Closed typed intent for cell administration."""

from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

MAX_MULTIPLIER_BYTES = 32  # not a setting: canonical multiplier width is part of the wire grammar


def _refuse() -> ValueError:
    return ValueError("Unsupported cell administration command")


def _text(value: str, maximum: int, *, empty: bool = False, allow_controls: bool = False) -> str:
    try:
        size = len(value.encode("utf-8"))
    except UnicodeError:
        raise _refuse() from None
    if (
        (not empty and not size)
        or size > maximum
        or "\0" in value
        or (not allow_controls and any(ord(c) < 32 or ord(c) == 127 for c in value))
    ):
        raise _refuse()
    return value


def _options(tokens: list[str], allowed: set[str]) -> dict[str, str]:
    if len(tokens) % 2:
        raise _refuse()
    result: dict[str, str] = {}
    for index in range(0, len(tokens), 2):
        key = tokens[index]
        if key not in allowed or key in result:
            raise _refuse()
        result[key] = tokens[index + 1]
    return result


def _integer(value: str, maximum: int, minimum: int = 0) -> int:
    if not value.isascii() or not value.isdecimal() or len(value) > 16:
        raise _refuse()
    number = int(value)
    if not minimum <= number <= maximum:
        raise _refuse()
    return number


def _number(value: str, *, positive: bool = False) -> float:
    if not re.fullmatch(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?", value):
        raise _refuse()
    number = float(value)
    if not math.isfinite(number) or number < 0 or (positive and number == 0):
        raise _refuse()
    return number


def _multiplier(value: str) -> str:
    if len(value) > MAX_MULTIPLIER_BYTES or not re.fullmatch(
        r"[+]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]{1,3})?", value
    ):
        raise _refuse()
    number = Decimal(value)
    if (
        not number.is_finite()
        or not 0 <= number <= 1000
        or (number and number.adjusted() < 3 - MAX_MULTIPLIER_BYTES)
    ):
        raise _refuse()
    # For values below one, even one significant digit needs 3-adjusted bytes with x.
    canonical = format(number, "f") if number else "0"
    if "." in canonical:
        canonical = canonical.rstrip("0").rstrip(".")
    canonical = (canonical or "0") + "x"
    if len(canonical) > MAX_MULTIPLIER_BYTES:
        raise _refuse()
    return canonical


@dataclass(frozen=True)
class Command:
    """An immutable intent; callers receive a separate decoded arguments value."""

    action: str
    mutating: bool
    _arguments_json: str
    digest: str

    @property
    def arguments(self) -> dict[str, Any]:
        arguments: dict[str, Any] = json.loads(self._arguments_json)
        return arguments

    @property
    def path(self) -> str:
        return "/v1/admin/cell." + self.action


def _command(action: str, arguments: dict[str, Any], *, mutating: bool = True) -> Command:
    encoded = json.dumps(arguments, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if len(encoded.encode()) > 65536:
        raise _refuse()
    intent = json.dumps(
        {"action": action, "arguments": arguments},
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return Command(action, mutating, encoded, hashlib.sha256(intent.encode()).hexdigest())


def _settings(tokens: list[str]) -> Command:
    if not tokens:
        raise _refuse()
    verb, rest = tokens[0], tokens[1:]
    if verb == "list":
        options = _options(rest, {"--prefix"})
        return _command(
            "settings_list",
            {"prefix": _text(options.get("--prefix", ""), 256, empty=True)},
            mutating=False,
        )
    if verb == "history" and rest:
        options = _options(rest[1:], {"--limit"})
        return _command(
            "settings_history",
            {"name": _text(rest[0], 256), "limit": _integer(options.get("--limit", "50"), 50, 1)},
            mutating=False,
        )
    if verb in {"set", "reset"} and len(rest) >= (2 if verb == "set" else 1):
        count = 2 if verb == "set" else 1
        options = _options(rest[count:], {"--expect"})
        return _command(
            "settings_set",
            {
                "name": _text(rest[0], 256),
                "value": _text(rest[1], 16384, empty=True, allow_controls=True)
                if verb == "set"
                else None,
                "clear": verb == "reset",
                "expected": _text(options["--expect"], 16384, empty=True, allow_controls=True)
                if "--expect" in options
                else None,
            },
        )
    raise _refuse()


def _signing(tokens: list[str]) -> Command:
    if tokens == ["list"]:
        return _command("signing_keys_list", {}, mutating=False)
    if len(tokens) == 2 and tokens[0] == "retire":
        return _command("signing_keys_retire", {"kid": _text(tokens[1], 256)})
    if tokens and tokens[0] == "rotate":
        options = _options(tokens[1:], {"--algorithm", "--activate-in"})
        algorithm = options.get("--algorithm", "HS256")
        if algorithm not in {"HS256", "RS256"}:
            raise _refuse()
        return _command(
            "signing_keys_rotate",
            {"algorithm": algorithm, "activate_in": _number(options.get("--activate-in", "300"))},
        )
    raise _refuse()


def _revoke(tokens: list[str]) -> Command:
    options = _options(tokens, {"--subject", "--reason"})
    if "--subject" not in options:
        raise _refuse()
    subject = _text(options["--subject"], 512)
    if "/user:" in subject:
        raise _refuse()
    return _command(
        "subject_revoke",
        {
            "subject": subject,
            "reason": _text(options.get("--reason", ""), 1024, empty=True, allow_controls=True),
        },
    )


def _rate(tokens: list[str]) -> Command:
    if not tokens or tokens[0] not in {"set", "reset"}:
        raise _refuse()
    clear = tokens[0] == "reset"
    required = {"--namespace", "--op-class"}
    if not clear:
        required |= {"--capacity", "--rate"}
    options = _options(tokens[1:], required)
    if options.keys() != required:
        raise _refuse()
    namespace = _text(options["--namespace"], 256)
    op_class = _text(options["--op-class"], 64)
    if namespace == "*":
        raise _refuse()
    return _command(
        "ratelimit_namespace_set",
        {
            "namespace": namespace,
            "op_class": op_class,
            "clear": clear,
            "capacity": None if clear else _number(options["--capacity"], positive=True),
            "rate": None if clear else _number(options["--rate"]),
        },
    )


def _budget(tokens: list[str]) -> Command:
    if len(tokens) < 2 or tokens[0] not in {"set", "reset"}:
        raise _refuse()
    clear = tokens[0] == "reset"
    allowed = {"--consumer"} if clear else {"--consumer", "--budget-ms", "--multiplier"}
    options = _options(tokens[2:], allowed)
    consumer = _text(options["--consumer"], 64) if "--consumer" in options else None
    operation = _text(tokens[1], 128)
    if operation == "*" and consumer is None:
        raise _refuse()
    if not clear and (("--budget-ms" in options) == ("--multiplier" in options)):
        raise _refuse()
    if "--multiplier" in options and consumer is None:
        raise _refuse()
    return _command(
        "budget_set",
        {
            "op": operation,
            "consumer": consumer,
            "clear": clear,
            "budget_ms": _integer(options["--budget-ms"], 86400000)
            if "--budget-ms" in options
            else None,
            "budget": _multiplier(options["--multiplier"]) if "--multiplier" in options else None,
        },
    )


def _blacklist(tokens: list[str]) -> Command:
    if len(tokens) < 2 or tokens[0] != "global":
        raise _refuse()
    verb, rest = tokens[1], tokens[2:]
    if verb == "list":
        options = _options(rest, {"--limit"})
        return _command(
            "blacklist_global_list",
            {"limit": _integer(options.get("--limit", "50"), 50, 1)},
            mutating=False,
        )
    if verb == "remove" and len(rest) == 1:
        try:
            pattern_id = str(uuid.UUID(rest[0]))
        except ValueError:
            raise _refuse() from None
        return _command("blacklist_global_remove", {"pattern_id": pattern_id})
    if verb == "add" and rest:
        options = _options(rest[1:], {"--type", "--reason"})
        kind = options.get("--type", "substring")
        pattern = _text(rest[0], 4096, allow_controls=True)
        if kind not in {"substring", "regex"} or (kind == "regex" and len(pattern) > 256):
            raise _refuse()
        reason = _text(options.get("--reason", ""), 1024, empty=True, allow_controls=True)
        return _command(
            "blacklist_global_add",
            {
                "pattern_id": str(uuid.uuid4()),
                "pattern": pattern,
                "pattern_type": kind,
                "reason": reason,
            },
        )
    raise _refuse()


def parse_command(argv: list[str]) -> Command:
    """Parse intent only; server capabilities and owning validation still authorize work."""
    if (
        not isinstance(argv, list)
        or not argv
        or len(argv) > 256
        or any(type(x) is not str for x in argv)
    ):
        raise _refuse()
    try:
        size = sum(len(token.encode("utf-8")) + 1 for token in argv)
    except UnicodeError:
        raise _refuse() from None
    if size > 16384 or any("\0" in token for token in argv):
        raise _refuse()
    parsers = {
        "settings": _settings,
        "signing-key": _signing,
        "revoke-token": _revoke,
        "ratelimit": _rate,
        "budget": _budget,
        "blacklist": _blacklist,
    }
    parser = parsers.get(argv[0])
    if parser is None:
        raise _refuse()
    return parser(argv[1:])


def new_state(command: Command, *, targets: list[dict[str, Any]], run_id: str) -> dict[str, Any]:
    from server.cli.cell_each_state import new_state as create

    return create(command, targets=targets, run_id=run_id)


def read_state(path: Any) -> dict[str, Any]:
    from server.cli.cell_each_state import read_state as read

    return read(path)


def write_state(path: Any, state: dict[str, Any], *, exclusive: bool = False) -> None:
    from server.cli.cell_each_state import write_state as write

    write(path, state, exclusive=exclusive)


def main(argv: list[str] | None = None) -> int:
    from server.cli.cell_each_run import main as run

    return run(argv)
