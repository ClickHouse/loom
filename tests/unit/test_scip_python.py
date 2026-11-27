"""Python SCIP descriptors and definition spans must identify the actual callable."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_PATH = Path(__file__).resolve().parents[2] / "scripts/scip_harness/scip_to_delta.py"
_SPEC = importlib.util.spec_from_file_location("scip_python_test_converter", _PATH)
assert _SPEC and _SPEC.loader
c = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = c
_SPEC.loader.exec_module(c)
_PREFIX = "scip-python python project version "


@pytest.mark.parametrize(
    "descriptor",
    [
        "`pkg.app`/helper().(value)",
        "`pkg.app`/App#run().(self)",
        "`pkg.app`/helper().[T]",
        "`pkg.app`/App#default_config.",
        "`pkg.app`/App#run().cache.",
    ],
)
def test_noncallable_descriptors_do_not_alias_their_owner(descriptor):
    assert c._parse_symbol(_PREFIX + descriptor) is None


@pytest.mark.parametrize(
    ("descriptor", "expected"),
    [
        ("`pkg.app`/helper().", ("pkg.app::helper", "function", "()")),
        ("`pkg.app`/App#", ("pkg.app::App", "class", "")),
        ("`pkg.app`/App#run().", ("pkg.app::App::run", "function", "()")),
    ],
)
def test_callables_and_classes_keep_their_qualified_names(descriptor, expected):
    assert c._parse_symbol(_PREFIX + descriptor) == expected


def test_python_references_outside_a_function_are_not_attributed_to_it():
    def occurrence(symbol, line, *, roles=0, span=()):
        return SimpleNamespace(
            symbol=_PREFIX + symbol,
            range=[line, 0, 5],
            enclosing_range=list(span),
            symbol_roles=roles,
        )

    document = SimpleNamespace(
        relative_path="pkg/app.py",
        symbols=[],
        occurrences=[
            occurrence("`pkg.app`/helper().", 0, roles=1, span=(0, 0, 2, 0)),
            occurrence("`pkg.other`/called().", 1),
            occurrence("`pkg.other`/outside().", 5),
            occurrence("`pkg.app`/App#", 4, roles=1, span=(4, 0, 7, 0)),
            occurrence("`pkg.other`/class_body().", 6),
        ],
    )
    run = c._Run(c._SourceLines(None), False, False, {}, {}, False, is_python=True)
    result = c._convert_document(document, run)
    assert [
        (edge["from_qualified_name"], edge["to_qualified_name"]) for edge in result["edges"]
    ] == [("pkg.app::helper", "pkg.other::called")]
