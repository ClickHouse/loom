"""pytest fixture wiring: detection, requests, and the resolution ladder.

The contract under test mirrors pytest's own documented nearest-first
order, and the failure mode being prevented is a WRONG edge: ambiguity on
a rung drops rather than guesses.
"""

from __future__ import annotations

import pytest

pytest.importorskip("tree_sitter_python")

from server.code.domain.extract.edges_treesitter import extract_edges
from server.code.domain.extract.lang_python import parse_python

_TEST_PY = b"""\
import pytest

@pytest.fixture
def local_cluster():
    return 1

@pytest.mark.parametrize("engine", ["Log", "Memory"])
def test_insert(started_cluster, local_cluster, engine, _ignored):
    pass

@pytest.mark.usefixtures("zk_setup")
def test_marked():
    pass

def test_dynamic(request):
    request.getfixturevalue("dyn_fx")

def helper(started_cluster):  # not a test, not a fixture: no request
    pass
"""


def _parse(path: str = "tests/integration/test_x/test.py"):
    return parse_python(path, _TEST_PY)


def test_fixture_kind_and_requests() -> None:
    pf = _parse()
    kinds = {s.name: s.kind for s in pf.symbols}
    assert kinds["local_cluster"] == "fixture"
    assert kinds["test_insert"] == "function"
    reqs = {(q.split("::")[-1], f) for q, f, _ in pf.fixture_requests}
    # params request fixtures; parametrize names, request, _underscore don't
    assert ("test_insert", "started_cluster") in reqs
    assert ("test_insert", "local_cluster") in reqs
    assert ("test_insert", "engine") not in reqs
    assert ("test_insert", "_ignored") not in reqs
    # usefixtures mark + getfixturevalue literal both request
    assert ("test_marked", "zk_setup") in reqs
    assert ("test_dynamic", "dyn_fx") in reqs
    # a plain helper requests nothing
    assert not any(q.endswith("::helper") for q, _, _ in pf.fixture_requests)


def _table(*entries: tuple[str, str]) -> dict:
    """{bare: [(id, qname, 'fixture')]}"""
    t: dict = {}
    for qname, kind in entries:
        bare = qname.split("::")[-1]
        t.setdefault(bare, []).append((f"id-{qname}", qname, kind))
    return t


def _fixture_edges(pf, table):
    return {
        (e.to_qualified_name, e.confidence)
        for e in extract_edges(pf, table)
        if e.edge_kind == "fixture_use"
    }


def test_ladder_same_module_beats_conftest() -> None:
    pf = _parse()
    edges = _fixture_edges(
        pf,
        _table(
            ("tests.integration.test_x.conftest::local_cluster", "fixture"),
        ),
    )
    # the file's own @pytest.fixture local_cluster wins at 1.0 over conftest
    assert ("tests.integration.test_x.test::local_cluster", 1.0) in edges


def test_ladder_own_dir_then_ancestor_conftest() -> None:
    pf = _parse()
    edges = _fixture_edges(
        pf,
        _table(
            ("tests.integration.test_x.conftest::started_cluster", "fixture"),
            ("tests.integration.conftest::started_cluster", "fixture"),
            ("tests.integration.conftest::zk_setup", "fixture"),
        ),
    )
    # own-dir conftest (0.9) shadows the ancestor (pytest nearest-first)
    assert ("tests.integration.test_x.conftest::started_cluster", 0.9) in edges
    assert ("tests.integration.conftest::zk_setup", 0.8) in edges


def test_ambiguous_non_conftest_drops_and_non_fixture_never_matches() -> None:
    pf = _parse()
    # two plugin-style fixtures with the same name: drop, never guess
    edges = _fixture_edges(
        pf,
        _table(
            ("helpers.a::zk_setup", "fixture"),
            ("helpers.b::zk_setup", "fixture"),
            # a plain FUNCTION named started_cluster must never be a target
            ("helpers.cluster::started_cluster", "function"),
        ),
    )
    assert not any(q.endswith("zk_setup") for q, _ in edges)
    assert not any(q.endswith("started_cluster") for q, _ in edges)


def test_unique_plugin_fixture_resolves_low_confidence() -> None:
    pf = _parse()
    edges = _fixture_edges(pf, _table(("helpers.plugin::dyn_fx", "fixture")))
    assert ("helpers.plugin::dyn_fx", 0.6) in edges
