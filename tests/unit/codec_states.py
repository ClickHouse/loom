"""The codec settings in each state a server can show them to a user, as
`system.settings` rows (`vector_codec_ddl.CODEC_SETTINGS_SQL`), and the
decision table every caller of `CodecSettings` follows."""

from __future__ import annotations

from itertools import product
from typing import Any

from server.core.schema.vector_codec_ddl import CHAIN_SETTING, CODEC_SETTING, CodecSettings

# absent: no row (before 26.8); writable: readonly = 0 and no constraint; on / off:
# readonly at 1 / at 0 (`<readonly/>`, which system.settings shows as readonly = 1).
STATES = ("absent", "writable", "on", "off")
# Constraints that keep readonly = 0 in system.settings and still decide whether 1 is allowed.
SHAPES = ("max0", "max0_on", "disallowed1", "min1", "range01", "bad_bound")
# (value, readonly, min, max, disallowed_values), as ClickHouse prints a Bool setting.
_ROW: dict[str, tuple[str, int, str | None, str | None, list[str]]] = {
    "writable": ("0", 0, None, None, []),
    "on": ("1", 1, None, None, []),
    "off": ("0", 1, None, None, []),
    "max0": ("0", 0, None, "0", []),  # <max>0</max>: 1 refused (452), held at 0
    "max0_on": ("1", 0, None, "0", []),  # the same constraint over a value of 1: held at 1
    "disallowed1": ("0", 0, None, None, ["1"]),  # <disallowed>1</disallowed>
    "min1": ("1", 0, "1", None, []),  # <min>1</min>: 1 allowed
    "range01": ("0", 0, "0", "1", []),  # <min>0</min><max>1</max>: 1 allowed
    "bad_bound": ("0", 0, None, "x", []),  # a bound that is not a number counts as refusing
}
# Whether the setting can be on here, and whether a statement that needs it sends it.
USABLE = {"absent": False, "writable": True, "on": True, "off": False} | {
    "max0": False,
    "max0_on": True,
    "disallowed1": False,
    "min1": True,
    "range01": True,
    "bad_bound": False,
}
SENT = {"absent": False, "writable": True, "on": False, "off": False} | {
    "max0": False,
    "max0_on": False,
    "disallowed1": False,
    "min1": True,
    "range01": True,
    "bad_bound": False,
}
# Every (enable_quantized_codec, allow_suspicious_codecs) combination of the four states.
PAIRS = list(product(STATES, STATES))
_COLUMNS = ("name", "value", "readonly", "min", "max", "disallowed_values")


def rows(**states: str) -> list[tuple[Any, ...]]:
    """`CODEC_SETTINGS_SQL` rows for each setting named with its state or shape."""
    return [(name, *_ROW[state]) for name, state in states.items() if state != "absent"]


def dict_rows(**states: str) -> list[dict[str, Any]]:
    return [dict(zip(_COLUMNS, row, strict=True)) for row in rows(**states)]


def codec_rows(codec: str, chain: str) -> list[tuple[Any, ...]]:
    return rows(**{CODEC_SETTING: codec, CHAIN_SETTING: chain})


def settings(codec: str = "writable", chain: str = "writable") -> CodecSettings:
    return CodecSettings.from_rows(codec_rows(codec, chain))
