"""The Go compiler tier's rows carry the fast tier's kinds —
struct, interface, type, field, method (an interface's), function — read
from scip-go's own SymbolInformation.kind, with the descriptor's shape as
the fallback, so fields and interface methods never rank as types."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_CONVERTER = Path(__file__).resolve().parents[2] / "scripts" / "scip_harness" / "scip_to_delta.py"
MOD = "github.com/ClickHouse/clickhouse-go/v2"


@pytest.fixture(scope="module")
def conv():
    spec = importlib.util.spec_from_file_location("scip_to_delta_go_kinds", _CONVERTER)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules["scip_to_delta_go_kinds"] = mod
    spec.loader.exec_module(mod)
    return mod


def _sym(desc: str) -> str:
    return f"scip-go gomod {MOD} c37b `{MOD}/lib/driver`/{desc}"


def test_kinds_come_from_the_indexer_when_recorded(conv, monkeypatch) -> None:
    ctx = conv._GoContext(None, modules={MOD})
    ctx.def_kinds = {
        _sym("Conn#"): "interface",
        _sym("Stats#"): "struct",
        _sym("Stats#MaxIdleConns."): "field",
        _sym("Conn#Query."): "method",
        _sym("Rows#Scan()."): "function",
        _sym("Options#"): "type",
    }
    monkeypatch.setattr(conv, "_GO", ctx)
    kinds = {
        d: conv._parse_symbol(_sym(d))[1]
        for d in (
            "Conn#",
            "Stats#",
            "Stats#MaxIdleConns.",
            "Conn#Query.",
            "Rows#Scan().",
            "Options#",
        )
    }
    assert kinds == {
        "Conn#": "interface",
        "Stats#": "struct",
        "Stats#MaxIdleConns.": "field",
        "Conn#Query.": "method",
        "Rows#Scan().": "function",
        "Options#": "type",
    }
    assert conv._parse_symbol(_sym("Conn#Query."))[0] == "lib.driver.driver::Conn::Query"


def test_descriptor_shape_is_the_fallback(conv, monkeypatch) -> None:
    monkeypatch.setattr(conv, "_GO", conv._GoContext(None, modules={MOD}))
    assert conv._parse_symbol(_sym("Stats#MaxIdleConns."))[1] == "field"
    assert conv._parse_symbol(_sym("Stats#"))[1] == "struct"
    assert conv._parse_symbol(_sym("Open()."))[1] == "function"
    assert conv._parse_symbol(_sym("Rows#Scan()."))[1] == "function"


def test_scip_kind_names_map_to_the_fast_tiers(conv) -> None:
    m = conv._GO_SCIP_KINDS
    assert m["Struct"] == "struct" and m["Interface"] == "interface" and m["Field"] == "field"
    assert m["MethodSpecification"] == "method" and m["Method"] == "function"
    assert m["Function"] == "function" and m["Constant"] == "const" and m["Variable"] == "var"
    assert "struct" in conv._INHERIT_KINDS, "a Go struct's implementation relationship is inherits"


def test_typescript_and_clang_symbols_are_untouched(conv, monkeypatch) -> None:
    monkeypatch.setattr(conv, "_GO", None)
    clang = "scip-clang cxx . . `DB::IColumn#insert(abc123).`"
    parsed = conv._parse_symbol(clang)
    assert parsed is not None and parsed[1] == "function"
    cls = "scip-clang cxx . . `DB::IColumn#`"
    parsed = conv._parse_symbol(cls)
    assert parsed is not None and parsed[1] == "class"
