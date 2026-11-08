"""The protobuf and Go-template lanes, and what the
tree-less lanes share (`lane_units`: span chunks, the site binder)."""

from __future__ import annotations

import time

from server.code.domain.extract import lang_gotemplate as gt
from server.code.domain.extract import lang_proto as pb
from server.code.domain.extract.lane_units import (
    SiteRule,
    ancestor_candidates,
    bind_sites,
    span_chunks,
)
from server.code.domain.extract.lang_detect import detect_by_extension
from server.code.domain.extract.name_scope import gate_name_table
from server.code.domain.extract.treesitter import ParsedFile, Symbol
from tests.unit.code_fixtures import assert_linear

PROTO = b"""syntax = "proto3";

package acme.v1;

import "google/protobuf/timestamp.proto";
import public "acme/v1/common.proto";

option go_package = "example.com/acme/api/v1;acmev1";

// Status of a resource.
enum Status {
  STATUS_UNSPECIFIED = 0;
  STATUS_ACTIVE = 1;
}

// Instance is a running thing.
message Instance {
  string id = 1; // the "{" in a comment must not open a block
  Status status = 2;
  map<string, Endpoint> endpoints = 3;
  repeated .acme.v1.Instance.Endpoint more = 4;
  google.protobuf.Timestamp created_at = 5;
  reserved 6, 7;
  oneof owner {
    string user = 8;
    Team team = 9;
  }
  message Endpoint {
    string host = 1;
    int32 port = 2;
  }
}

service InstanceService {
  rpc GetInstance(GetInstanceRequest) returns (Instance);
  rpc Watch(WatchRequest) returns (stream Instance) {
    option idempotency_level = NO_SIDE_EFFECTS;
  }
}

extend Instance {
  optional string extra = 100;
}
"""


def _proto() -> ParsedFile:
    return pb.parse_proto("api/acme/v1/instances.proto", PROTO)


def test_proto_symbols_kinds_spans_and_nesting() -> None:
    pf = _proto()
    by = {s.qualified_name: s for s in pf.symbols}
    mod = "api.acme.v1.instances"
    assert by[f"{mod}::Status"].kind == "enum"
    inst = by[f"{mod}::Instance"]
    assert inst.kind == "struct" and (inst.start_line, inst.end_line) == (17, 32)
    ep = by[f"{mod}::Instance.Endpoint"]
    assert (
        ep.kind == "struct" and ep.name == "Endpoint" and (ep.start_line, ep.end_line) == (28, 31)
    )
    svc = by[f"{mod}::InstanceService"]
    assert svc.kind == "interface" and (svc.start_line, svc.end_line) == (34, 39)
    get = by[f"{mod}::InstanceService.GetInstance"]
    assert get.kind == "function" and get.signature == "(GetInstanceRequest) returns (Instance)"
    assert (get.start_line, get.end_line) == (35, 35)
    watch = by[f"{mod}::InstanceService.Watch"]
    assert watch.signature == "(WatchRequest) returns (stream Instance)"
    assert (watch.start_line, watch.end_line) == (36, 38)  # the options block rides the rpc
    # `extend` defines nothing of its own
    assert not any("extra" in q for q in by)
    assert len(pf.symbols) == 6


def test_proto_sites_scalars_well_known_and_map_keys_name_nothing() -> None:
    pf = _proto()
    sites = pb.reference_sites(pf)
    field = [(ln, n) for ln, n, k in sites if k == pb.SITE_FIELD_TYPE]
    rpc = [(ln, n) for ln, n, k in sites if k == pb.SITE_RPC_TYPE]
    assert field == [(19, "Status"), (20, "Endpoint"), (21, "Endpoint"), (26, "Team")]
    assert rpc == [
        (35, "GetInstanceRequest"),
        (35, "Instance"),
        (36, "WatchRequest"),
        (36, "Instance"),
    ]


def test_proto_package_go_package_and_imports() -> None:
    pf = _proto()
    assert pb.proto_package(pf) == "acme.v1"
    assert pb.go_package(pf) == "example.com/acme/api/v1"
    assert pf.includes == ["google/protobuf/timestamp.proto", "acme/v1/common.proto"]
    wanted = pb.wanted_paths(pf.path, pf.includes)
    assert "google/protobuf/timestamp.proto" not in " ".join(wanted)
    assert wanted == [
        "api/acme/v1/acme/v1/common.proto",
        "api/acme/acme/v1/common.proto",
        "api/acme/v1/common.proto",
        "acme/v1/common.proto",
    ]
    present = {"api/acme/v1/common.proto", "acme/v1/common.proto"}
    assert pb.resolve_imports(pf.path, pf.includes, present) == ["api/acme/v1/common.proto"]
    assert ancestor_candidates("x.proto", "a/b.proto") == ["a/b.proto"]


def test_proto_edges_bind_same_file_then_repo_wide() -> None:
    pf = _proto()
    other = "api/acme/v1/requests.proto"
    table = {
        "GetInstanceRequest": [
            ("sid1", f"{other.replace('/', '.')[:-6]}::GetInstanceRequest", "struct")
        ],
        "WatchRequest": [
            ("sid2", "a::WatchRequest", "struct"),
            ("sid3", "b::WatchRequest", "struct"),
        ],
        "Team": [("sid4", "api.acme.v1.teams::Team", "struct")],
    }
    edges = bind_sites(pf, pb.reference_sites(pf), gate_name_table(table, "proto"), pb.SITE_RULES)
    got = {
        (e.from_qualified_name.split("::")[1], e.to_qualified_name.split("::")[1], e.confidence)
        for e in edges
    }
    assert ("Instance", "Status", 1.0) in got  # same file
    assert ("Instance", "Instance.Endpoint", 1.0) in got  # nested, same file
    assert ("Instance", "Team", 0.6) in got  # repository-unique
    assert ("InstanceService.GetInstance", "GetInstanceRequest", 0.6) in got
    assert ("InstanceService.GetInstance", "Instance", 1.0) in got
    assert not any(t == "WatchRequest" for _f, t, _c in got)  # ambiguous repo-wide
    assert all(e.edge_kind == "uses_type" for e in edges)


HELPERS = b"""{{/*
Expand the name of the chart.
*/}}
{{- define "c.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 }}
{{- end }}

{{/* Fully qualified */}}
{{- define "c.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride }}
{{- else }}
{{- printf "%s-%s" .Release.Name (include "c.name" .) }}
{{- end }}
{{- end }}
{{- define "c.labels" }}
app: {{ template "c.name" . }}
{{- range .Values.x }}{{ . }}{{- end }}
{{- end }}
"""

DEPLOYMENT = b"""{{- $h := include "c.labels" . | sha256sum }}
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{ include "c.fullname" . }}
  labels: {{ include "c.labels" . | nindent 4 }}
spec:
  replicas: 1
---
apiVersion: v1
kind: Service
metadata:
  annotations:
    x: y
  name: {{ template "c.fullname" . }}-svc
"""


def test_gotemplate_defines_nest_and_end_matches() -> None:
    pf = gt.parse_gotemplate("charts/c/templates/_helpers.tpl", HELPERS)
    spans = {s.name: (s.start_line, s.end_line, s.kind) for s in pf.symbols}
    assert spans == {
        "c.name": (4, 6, "function"),
        "c.fullname": (9, 15, "function"),
        "c.labels": (16, 19, "function"),
    }
    assert all(s.qualified_name == f"charts.c.templates._helpers::{s.name}" for s in pf.symbols)
    sites = gt.reference_sites(pf)
    assert sites == [(13, "c.name", "include"), (17, "c.name", "include")]


def test_gotemplate_manifest_documents_and_sites() -> None:
    path = "charts/c/templates/deployment.yaml"
    assert gt.is_template_manifest(path, DEPLOYMENT)
    assert not gt.is_template_manifest(path, b"kind: Deployment\n")  # no action: config
    assert not gt.is_template_manifest("charts/c/values.yaml", DEPLOYMENT)  # not under templates/
    assert gt.is_template_manifest("charts/c/templates/NOTES.txt", b"hi {{ .Release.Name }}")
    assert gt.is_template_manifest("web/templates/index.html", b"<p>{{ .Title }}</p>")
    pf = gt.parse_gotemplate(path, DEPLOYMENT)
    docs = [(s.kind, s.name, s.signature, s.start_line, s.end_line) for s in pf.symbols]
    assert docs == [
        ("type", "Deployment", 'Deployment {{ include "c.fullname" . }}', 3, 9),
        ("type", "Service", 'Service {{ template "c.fullname" . }}-svc', 11, 16),
    ]
    assert gt.reference_sites(pf) == [
        (1, "c.labels", "include"),
        (6, "c.fullname", "include"),
        (7, "c.labels", "include"),
        (16, "c.fullname", "include"),
    ]


def test_gotemplate_edges_from_document_to_define() -> None:
    helpers = gt.parse_gotemplate("charts/c/templates/_helpers.tpl", HELPERS)
    dep = gt.parse_gotemplate("charts/c/templates/deployment.yaml", DEPLOYMENT)
    table: dict[str, list[tuple[str, str, str]]] = {}
    for s in helpers.symbols:
        table.setdefault(s.name, []).append((s.id, s.qualified_name, s.kind))
    # a second chart defining the same name makes it ambiguous repository-wide
    table["c.labels"].append(("zz", "charts.d.templates._helpers::c.labels", "function"))
    edges = bind_sites(
        dep, gt.reference_sites(dep), gate_name_table(table, "gotemplate"), gt.SITE_RULES
    )
    got = sorted(
        (
            e.from_qualified_name.split("::")[1],
            e.to_qualified_name.split("::")[1],
            e.line,
            e.confidence,
        )
        for e in edges
    )
    assert got == [("Deployment", "c.fullname", 6, 0.6), ("Service", "c.fullname", 16, 0.6)]
    assert all(e.edge_kind == "calls" for e in edges)
    # inside the helpers file the same-file rung binds at 1.0
    own = bind_sites(helpers, gt.reference_sites(helpers), {}, gt.SITE_RULES)
    assert {
        (e.from_qualified_name.split("::")[1], e.to_qualified_name.split("::")[1], e.confidence)
        for e in own
    } == {("c.fullname", "c.name", 1.0), ("c.labels", "c.name", 1.0)}


def test_chart_dependencies_name_repository_charts() -> None:
    text = (
        "apiVersion: v2\nname: c\ndependencies:\n"
        "  - name: sidecar\n    repository: file://../sidecar\n"
        "  - name: redis\n    repository: https://charts.example.com\n"
        "  - name: c\n"
    )
    cands = gt.chart_dependency_candidates("charts/c/Chart.yaml", text)
    assert cands == [
        "charts/sidecar/Chart.yaml",
        "charts/c/charts/sidecar/Chart.yaml",
        "helm-charts/sidecar/Chart.yaml",
        "charts/c/charts/redis/Chart.yaml",
        "charts/redis/Chart.yaml",
        "helm-charts/redis/Chart.yaml",
        "charts/c/charts/c/Chart.yaml",
        "helm-charts/c/Chart.yaml",
    ]
    assert gt.chart_dependency_candidates("Chart.yaml", "- not a mapping") == []
    assert gt.is_chart_manifest("x/Chart.yaml") and not gt.is_chart_manifest("x/values.yaml")


def test_detection_by_extension() -> None:
    assert detect_by_extension("a/b.proto") == "proto"
    assert detect_by_extension("c/templates/_helpers.tpl") == "gotemplate"
    assert detect_by_extension("README.md.gotmpl") == "gotemplate"
    assert detect_by_extension("x.tmpl") == "gotemplate"


def test_span_chunks_units_header_leftovers_and_oversize() -> None:
    pf = _proto()
    chunks = span_chunks(pf, 6000, **pb.CHUNK_OPTIONS)
    kinds = [(c.kind, c.start_line, c.end_line, c.scope.split("::")[-1]) for c in chunks]
    assert kinds[0] == ("file_header", 1, 9, "")  # stops before the enum's doc comment
    assert ("function", 10, 14, "Status") in kinds  # the comment above rides the enum
    inst = next(c for c in chunks if c.scope.endswith("::Instance"))
    assert (inst.start_line, inst.end_line) == (16, 32) and len(inst.symbol_ids) == 2
    svc = next(c for c in chunks if c.scope.endswith("::InstanceService"))
    assert len(svc.symbol_ids) == 3  # the service and its two rpcs
    assert kinds[-1] == ("other", 41, 43, "")  # the extend block is a leftover
    assert all(c.ordinal == 0 for c in chunks)
    # oversize: a block longer than max_chars splits on line boundaries
    small = span_chunks(pf, 300, **pb.CHUNK_OPTIONS)
    pieces = [c for c in small if c.scope.endswith("::Instance")]
    assert len(pieces) >= 2 and all(len(c.content) <= 300 for c in pieces)
    assert pieces[0].start_line == 16 and pieces[-1].end_line == 32


def test_span_chunks_template_comment_block_rides_the_define() -> None:
    pf = gt.parse_gotemplate("charts/c/templates/_helpers.tpl", HELPERS)
    chunks = span_chunks(pf, 6000, **gt.CHUNK_OPTIONS)
    assert [(c.kind, c.start_line, c.end_line) for c in chunks] == [
        ("function", 1, 6),
        ("function", 8, 15),
        ("function", 16, 19),
    ]


def test_bind_sites_ladder() -> None:
    a = Symbol("m::A", "A", "function", "()", "m.tpl", 1, 5)
    b = Symbol("m::B", "B", "function", "()", "m.tpl", 6, 10)
    b2 = Symbol("m::B2", "B", "function", "()", "m.tpl", 11, 12)  # a second same-file B
    pf = ParsedFile(path="m.tpl", lang="gotemplate", symbols=[a, b, b2], source=b"")
    rules = {
        "k": SiteRule("calls", frozenset({"function"})),
        "local": SiteRule("calls", frozenset({"function"}), allow_repo_wide=False),
    }
    table = {
        "C": [("c1", "n::C", "function")],
        "D": [("d1", "n::D", "function"), ("d2", "o::D", "function")],
        "E": [("e1", "n::E", "struct")],
    }
    sites = [
        (2, "B", "k"),
        (2, "C", "k"),
        (2, "D", "k"),
        (2, "E", "k"),
        (2, "C", "local"),
        (7, "A", "k"),
        (7, "A", "k"),
        (20, "A", "k"),
        (2, "A", "k"),
        (2, "X", "zz"),
    ]
    edges = bind_sites(pf, sites, table, rules)
    got = [(e.from_qualified_name, e.to_qualified_name, e.line, e.confidence) for e in edges]
    assert got == [("m::A", "n::C", 2, 0.6), ("m::B", "m::A", 7, 1.0)]
    # B: two same-file definitions → ambiguous; D: two repo-wide → ambiguous; E: wrong kind;
    # C under `local`: repo-wide not allowed; line 20: no enclosing symbol; A from A: self;
    # a repeated (edge, source, target, line) records once; an unknown site kind is skipped


def _proto_cost(nest: int, n: int) -> float:
    src = ('syntax = "proto3";\n' + "message A{" * nest + "message A{}\n" * n).encode()
    t0 = time.perf_counter()
    pb.parse_proto("a.proto", src)
    return time.perf_counter() - t0


def test_a_file_of_many_messages_parses_in_linear_time() -> None:
    assert_linear(lambda n: _proto_cost(0, n), 4_000, 16_000)


def test_a_deep_nest_of_messages_costs_no_more_than_a_flat_file() -> None:
    assert_linear(lambda nest: _proto_cost(nest, 8_000), 0, 300)  # deep against flat


def test_a_gotemplate_comment_holding_an_action_is_blanked_whole() -> None:
    src = b'{{/* {{ include "a" . }} */}}\n{{- /* {{ define "b" }}{{ end }} */ -}}\nx: {{ include "c" . }}\n'
    pf = gt.parse_gotemplate("charts/c/templates/cm.yaml", src)
    assert gt.reference_sites(pf) == [(3, "c", "include")] and pf.symbols == []
