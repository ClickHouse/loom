"""The shared gate flags' help text must render: argparse treats a bare `%`
in a help string as a format directive, so an unescaped "95%" would make
`--help` fail with "unsupported format character" on every bench that calls
add_gate_arguments."""

from __future__ import annotations

import argparse

from bench._gate import add_gate_arguments


def test_gate_help_renders() -> None:
    p = argparse.ArgumentParser(prog="x")
    add_gate_arguments(p)
    text = p.format_help()
    assert "95% paired" in text
