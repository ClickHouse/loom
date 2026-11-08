"""Jsonnet top-level locals and root-object members are
symbols, imports are includes, `alias.member(…)` through an imported
library is a call site."""

from __future__ import annotations

import time

import pytest

from server.code.domain.extract.lane_units import bind_sites, span_chunks
from server.code.domain.extract.lang_detect import detect_by_extension
from server.code.domain.extract.lang_jsonnet import (
    CHUNK_OPTIONS,
    SITE_RULES,
    parse_jsonnet,
    reference_sites,
    resolve_imports,
    wanted_paths,
)
from tests.unit.code_fixtures import assert_linear

SHARED = b"""// helpers for the migration jobs
{
  generateSnapshotName(index, service, base=""): std.format("snapshot%s-%s", [base, service]),

  // the pvc name depends on the topology
  generatePVCName(service, randomString, index, reverseMigration):
    if reverseMigration == "" then
      std.asciiLower("ch-storage-%s-%s-0" % [service, randomString])
    else
      std.asciiLower("ch-storage-%s-%s" % [service, index]),

  defaults:: { storageClass: "gp3" },
  version: "1.2",
}
"""

PVC = b"""local shared = import "shared.libsonnet";
local text = importstr "notes.txt";

// TLAs
local service = std.extVar('service');
local storageClassMap = {
  "aws": "gp3-encrypted",
  "gcp": "standard-rwo",
};
local pick(cloud) = storageClassMap[cloud];

{
  apiVersion: "v1",
  kind: "PersistentVolumeClaim",
  metadata: {
    name: shared.generatePVCName(service, "abc", 0, ""),
    labels: { sourceSnapshotName: shared.generateSnapshotName(0, service) },
  },
  spec: { storageClassName: pick("aws") },
}
"""


def test_locals_members_and_imports() -> None:
    lib = parse_jsonnet("experiments/migration/shared.libsonnet", SHARED)
    got = {s.name: (s.kind, s.signature, s.start_line, s.end_line) for s in lib.symbols}
    assert got == {
        "generateSnapshotName": ("function", 'generateSnapshotName(index, service, base="")', 3, 3),
        "generatePVCName": (
            "function",
            "generatePVCName(service, randomString, index, reverseMigration)",
            6,
            10,
        ),
        "defaults": ("field", "defaults hidden", 12, 12),
        "defaults.storageClass": ("field", "storageClass", 12, 12),  # nested member
        "version": ("field", "version", 13, 13),
    }
    assert lib.symbols[0].qualified_name == "experiments.migration.shared::generateSnapshotName"
    pvc = parse_jsonnet("experiments/migration/pvc.jsonnet", PVC)
    names = {s.name: (s.kind, s.start_line, s.end_line) for s in pvc.symbols}
    assert names["shared"] == ("var", 1, 1) and names["text"] == ("var", 2, 2)
    assert names["storageClassMap"] == ("var", 6, 9), "a multi-line local spans to its `;`"
    assert names["pick"] == ("function", 10, 10)
    assert {n for n in names if n in ("apiVersion", "kind", "metadata", "spec")} == {
        "apiVersion",
        "kind",
        "metadata",
        "spec",
    }
    assert pvc.includes == ["shared.libsonnet", "notes.txt"]


def test_member_calls_bind_through_the_import_alias() -> None:
    lib = parse_jsonnet("experiments/migration/shared.libsonnet", SHARED)
    pvc = parse_jsonnet("experiments/migration/pvc.jsonnet", PVC)
    sites = reference_sites(pvc)
    assert [(n, k) for _l, n, k in sites] == [
        ("generatePVCName", "call"),
        ("generateSnapshotName", "call"),
    ], "`std.extVar`, `pick(...)` and `std.asciiLower` are not alias calls"
    table = {s.name: [(s.id, s.qualified_name, s.kind)] for s in [*lib.symbols, *pvc.symbols]}
    edges = bind_sites(pvc, sites, table, SITE_RULES)
    assert {
        (
            e.from_qualified_name.split("::")[1],
            e.to_qualified_name.split("::")[1],
            e.edge_kind,
            e.confidence,
        )
        for e in edges
    } == {
        ("metadata.name", "generatePVCName", "calls", 0.6),  # the innermost member
        ("metadata.labels", "generateSnapshotName", "calls", 0.6),
    }


def test_imports_resolve_beside_the_file_then_from_the_root() -> None:
    assert wanted_paths(
        "experiments/migration/pvc.jsonnet", ["shared.libsonnet", "lib/x.libsonnet"]
    ) == [
        "experiments/migration/shared.libsonnet",
        "shared.libsonnet",
        "experiments/migration/lib/x.libsonnet",
        "lib/x.libsonnet",
    ]
    present = {"experiments/migration/shared.libsonnet", "lib/x.libsonnet"}
    assert resolve_imports(
        "experiments/migration/pvc.jsonnet", ["shared.libsonnet", "lib/x.libsonnet"], present
    ) == ["experiments/migration/shared.libsonnet", "lib/x.libsonnet"]
    assert detect_by_extension("a/b.jsonnet") == "jsonnet"
    assert detect_by_extension("a/b.libsonnet") == "jsonnet"
    assert detect_by_extension("a/policy.rego") == "config"
    chunks = span_chunks(
        parse_jsonnet("experiments/migration/shared.libsonnet", SHARED), 6000, **CHUNK_OPTIONS
    )
    assert [c.kind for c in chunks].count("function") >= 4


NESTED = b"""{
  dashboards:: {
    api: { title: "API", panels(rows): std.flatMap(function(r) r.panels, rows) },
    // the per-name dashboards are computed
    [name]: { uid: name } for name in ["x"],
    local hidden = 1,
    infra: {
      title: "Infra",
      deep: { deeper: { deepest: 1 } },
    },
  },
  version: "1",
}
"""


def test_nested_object_members_are_dotted_symbols() -> None:
    """Members of member objects, three deep."""
    pf = parse_jsonnet("helm-charts/grafana/lib.libsonnet", NESTED)
    got = {s.name: (s.kind, s.signature, s.start_line, s.end_line) for s in pf.symbols}
    assert got["dashboards"] == ("field", "dashboards hidden", 2, 11)
    assert got["dashboards.api"] == ("field", "api", 3, 3), "the computed key bounds it"
    assert got["dashboards.api.title"] == ("field", "title", 3, 3)
    assert got["dashboards.api.panels"] == ("function", "panels(rows)", 3, 3)
    assert got["dashboards.infra"] == ("field", "infra", 7, 10)
    assert got["dashboards.infra.title"] == ("field", "title", 8, 8)
    assert got["dashboards.infra.deep"] == ("field", "deep", 9, 9)
    assert "dashboards.infra.deep.deeper" not in got, "three levels deep, no further"
    assert got["version"] == ("field", "version", 12, 12)
    assert not [n for n in got if "uid" in n or "name" in n or "hidden" in n], (
        "computed keys, comprehension clauses and object locals are not members"
    )
    assert pf.symbols[1].qualified_name == "helm-charts.grafana.lib::dashboards.api"


def test_a_member_after_a_string_holding_a_newline_is_read() -> None:
    pf = parse_jsonnet("a.jsonnet", b'{\n  a: "foo\nbar",\n  b: 1,\n}\n')
    assert {s.name for s in pf.symbols} >= {"a", "b"}


def test_members_after_strings_holding_a_url_or_a_colour_are_read() -> None:
    src = b"{\n  a: 'https://x',\n  b: 1,\n  c: '#73BF69',\n  d: 2,\n}\n"
    assert {s.name for s in parse_jsonnet("a.jsonnet", src).symbols} >= {"a", "b", "c", "d"}


def test_members_after_a_verbatim_string_ending_in_a_backslash_are_read() -> None:
    src = b"{\n  a: @'C:\\',\n  b: 1,\n  c: 'x',\n}\n"
    assert {s.name for s in parse_jsonnet("a.jsonnet", src).symbols} >= {"a", "b", "c"}


@pytest.mark.parametrize(
    "head,unit,tail",
    [("", "local a = 1\n", ""), ("{", "a:1,", "}"), ("{", "a:{},", "}")],
)
def test_a_file_of_many_locals_or_members_parses_in_linear_time(
    head: str, unit: str, tail: str
) -> None:
    def cost(n: int) -> float:
        src = (head + unit * n + tail).encode()
        t0 = time.perf_counter()
        parse_jsonnet("a.jsonnet", src)
        return time.perf_counter() - t0

    assert_linear(cost, 4_000, 16_000)
