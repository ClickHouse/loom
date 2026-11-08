"""Every tree-sitter parse of repository text stops at `code_parse_timeout_seconds`."""

from __future__ import annotations

import ast
import pathlib
import time

import pytest

from server.code.app.index.ingest_parse import parse_for_ingest
from server.settings import settings

_SERVER = pathlib.Path(__file__).resolve().parents[2] / "server"

# Inputs some grammars parse in quadratic time: each takes tens of seconds unbounded
_SLOW = [
    ("slow.go", b"package x\nvar s = " + b"`" * 60_000),
    ("slow.js", b"a" + b"<" * 120_000),
    ("slow.sh", b"a=(" * 30_000),
]


@pytest.mark.parametrize(("path", "source"), _SLOW, ids=[p for p, _ in _SLOW])
def test_a_slow_parse_stops_at_the_deadline_and_the_file_is_chunked(
    monkeypatch: pytest.MonkeyPatch, path: str, source: bytes
) -> None:
    monkeypatch.setattr(settings, "code_parse_timeout_seconds", 0.2)
    start = time.monotonic()
    pf, err = parse_for_ingest(path, source)
    assert time.monotonic() - start < 5
    assert pf is not None and pf.symbols == [] and pf.source == source
    assert "ParseTimeout" in err


def test_a_file_parsed_in_time_keeps_its_symbols() -> None:
    source = (
        "package x\n" + "".join(f"func f{i}() {{ a := 'é' }}\n" for i in range(3000))
    ).encode()
    pf, err = parse_for_ingest("big.go", source)
    assert err == ""
    assert pf is not None and len(pf.symbols) == 3000


def test_every_tree_sitter_parse_goes_through_the_deadline() -> None:
    """A `.parse(...)` call in a module that imports tree-sitter is the bounded one."""
    unbounded = []
    for f in _SERVER.rglob("*.py"):
        text = f.read_text()
        if "tree_sitter" not in text or f.name == "bounded_parse.py":
            continue
        for node in ast.walk(ast.parse(text)):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "parse"
                and not (
                    isinstance(node.func.value, ast.Name) and node.func.value.id in _NOT_A_PARSER
                )
            ):
                unbounded.append(f"{f.relative_to(_SERVER.parent)}:{node.lineno}")
    assert unbounded == []


_NOT_A_PARSER = {"bounded_parse", "ast", "Declarations", "tier_health", "json"}
