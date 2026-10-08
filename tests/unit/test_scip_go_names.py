"""The Go compiler tier names a symbol the way the fast tier
does — `<dotted dir>.<package clause>::Type::Name` — so the two tiers'
rows of one definition share a qualified name and the name gate keeps
them for Go callers."""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

from server.code.domain.extract.lang_go import go_scope

_CONVERTER = Path(__file__).resolve().parents[2] / "scripts" / "scip_harness" / "scip_to_delta.py"


@pytest.fixture(scope="module")
def conv():
    spec = importlib.util.spec_from_file_location("scip_to_delta_go_names", _CONVERTER)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules["scip_to_delta_go_names"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_go_symbols_take_the_fast_tiers_scope(conv, tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "lib" / "proto").mkdir(parents=True)
    (tmp_path / "lib" / "proto" / "block.go").write_text(
        "// Code generated\npackage proto\n\ntype Block struct{}\n"
    )
    (tmp_path / "conn.go").write_text("package clickhouse\n\nfunc Open() {}\n")
    mod = "github.com/ClickHouse/clickhouse-go/v2"
    (tmp_path / "go.mod").write_text(f"module {mod}\n")
    monkeypatch.setattr(conv, "_GO", conv._GoContext(tmp_path))
    method = f"scip-go gomod {mod} c37b `{mod}/lib/proto`/Block#Rows()."
    fn = f"scip-go gomod {mod} c37b `{mod}`/Open()."
    ext = "scip-go gomod database/sql 1.0 `database/sql`/Rows#Scan()."
    assert conv._parse_symbol(method)[0] == "lib.proto.proto::Block::Rows"
    assert conv._parse_symbol(fn)[0] == "root.clickhouse::Open"
    # the same names the fast tier writes for these files
    assert go_scope("lib/proto/block.go", "proto") == "lib.proto.proto"
    assert go_scope("conn.go", "clickhouse") == "root.clickhouse"
    # a dependency's symbol keeps its own path: nothing of the repository's
    assert conv._parse_symbol(ext)[0] == "database/sql::Rows::Scan"


def test_without_a_checkout_the_paths_last_element_stands_in(conv, monkeypatch) -> None:
    mod = "github.com/ClickHouse/ch-go"
    monkeypatch.setattr(
        conv, "_GO", conv._GoContext(None, modules={mod, "github.com/ClickHouse/clickhouse-go/v2"})
    )
    assert conv._parse_symbol(f"scip-go gomod {mod} v0 `{mod}/proto`/ColStr#Row().")[0] == (
        "proto.proto::ColStr::Row"
    )
    # `/v2` folds to the parent element; a hyphen becomes an underscore
    mod2 = "github.com/ClickHouse/clickhouse-go/v2"
    assert (
        conv._parse_symbol(f"scip-go gomod {mod2} v0 `{mod2}`/Open().")[0]
        == "root.clickhouse_go::Open"
    )
    # an external test package's synthetic path folds onto the real directory
    assert conv._parse_symbol(f"scip-go gomod {mod} v0 `{mod}/cht_test`/TestXML().")[0] == (
        "cht.cht_test::TestXML"
    )


def test_a_module_below_the_root_carries_its_prefix(conv, tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "services" / "api").mkdir(parents=True)
    (tmp_path / "services" / "api" / "main.go").write_text("package main\n")
    (tmp_path / "services" / "api" / "go.mod").write_text("module example.com/mono/api\n")
    monkeypatch.setattr(conv, "_GO", conv._GoContext(tmp_path, "services/api"))
    mod = "example.com/mono/api"
    assert (
        conv._parse_symbol(f"scip-go gomod {mod} v0 `{mod}`/main().")[0]
        == "services.api.main::main"
    )
    assert os.path.exists(tmp_path / "services" / "api" / "main.go")
