"""A Go type that embeds `Unimplemented<Svc>Server` (or is
registered for the service) implements the proto lane's `service` row, and
its rpc-named methods override the rpc rows — the generated interface is
declared-not-searched, so the hop is derived from the handler's text."""

from __future__ import annotations

from server.code.domain.extract.edges_treesitter import Edge, extract_edges_and_misses
from server.code.domain.extract.go_grpc import (
    grpc_edges,
    grpc_wanted_names,
    implementing_structs,
)
from server.code.domain.extract.lang_go import parse_go
from server.code.domain.extract.lang_proto import parse_proto

PROTO = b"""syntax = "proto3";
package foo.v1;
service FooService {
  rpc GetThing(GetThingRequest) returns (Thing);
  rpc ListThings(ListReq) returns (stream Thing);
}
message Thing { string id = 1; }
"""

HANDLER = b"""package server

import (
    "context"
    foov1 "go.example.com/api/foo/v1"
)

type Server struct {
    foov1.UnimplementedFooServiceServer
    store *Store
}

func (s *Server) GetThing(ctx context.Context, req *foov1.GetThingRequest) (*foov1.Thing, error) {
    return nil, nil
}

func (s *Server) Helper() {}

type Other struct{}

func (o *Other) ListThings(ctx context.Context, req *foov1.ListReq) error { return nil }

func Register(g *grpc.Server) { foov1.RegisterFooServiceServer(g, &Other{}) }

type Plain struct{ http.Server }
"""


def _table() -> dict[str, list[tuple[str, str, str]]]:
    pf = parse_proto("api/foo/v1/foo.proto", PROTO)
    table: dict[str, list[tuple[str, str, str]]] = {}
    for s in pf.symbols:
        table.setdefault(s.name, []).append((s.id, s.qualified_name, s.kind))
    return table


def test_embedding_and_registration_name_the_service() -> None:
    pf = parse_go("cmd/server/server.go", HANDLER)
    impls = implementing_structs(pf)
    assert impls["cmd.server.server::Server"] == ("FooService", 0.9)
    assert impls["cmd.server.server::Other"] == ("FooService", 0.8)
    assert "cmd.server.server::Plain" not in impls, "`http.Server` is not a service"
    assert grpc_wanted_names(pf) == ["FooService", "GetThing", "Helper", "ListThings"]


def test_edges_bind_struct_to_service_and_methods_to_rpcs() -> None:
    pf = parse_go("cmd/server/server.go", HANDLER)
    edges = grpc_edges(pf, _table(), Edge)
    got = {(e.edge_kind, e.from_qualified_name, e.to_qualified_name, e.confidence) for e in edges}
    assert got == {
        ("inherits", "cmd.server.server::Server", "api.foo.v1.foo::FooService", 0.9),
        (
            "overrides",
            "cmd.server.server::Server::GetThing",
            "api.foo.v1.foo::FooService.GetThing",
            0.9,
        ),
        ("inherits", "cmd.server.server::Other", "api.foo.v1.foo::FooService", 0.8),
        (
            "overrides",
            "cmd.server.server::Other::ListThings",
            "api.foo.v1.foo::FooService.ListThings",
            0.8,
        ),
    }
    # `Helper` is no rpc: nothing binds it
    assert not [e for e in edges if e.from_qualified_name.endswith("::Helper")]


def test_a_go_interface_of_the_same_name_is_not_a_service() -> None:
    pf = parse_go("cmd/server/server.go", HANDLER)
    table = {
        "FooService": [("x", "cmd.server.api::FooService", "interface")],
        "GetThing": [("y", "cmd.server.api::FooService::GetThing", "method")],
    }
    assert grpc_edges(pf, table, Edge) == []


def test_extractor_reaches_the_edges_through_lane_context() -> None:
    pf = parse_go("cmd/server/server.go", HANDLER)
    edges, _misses = extract_edges_and_misses(
        pf,
        {},
        lane_context={"go_grpc_table": _table()},
    )
    kinds = {(e.edge_kind, e.to_qualified_name) for e in edges}
    assert ("inherits", "api.foo.v1.foo::FooService") in kinds
    assert ("overrides", "api.foo.v1.foo::FooService.GetThing") in kinds


PROTO_B = b"""syntax = "proto3";
package replayer.v1;
service FooService {
  rpc GetThing(GetThingRequest) returns (Thing);
}
"""

HANDLER_ALIASED = b"""package api

import (
    "context"
    pb "go.example.com/acme/replayer/proto"
)

type API struct{}

func (a *API) GetThing(ctx context.Context, req *pb.GetThingRequest) (*pb.Thing, error) { return nil, nil }

func Serve(server *grpc.Server) {
    api := &API{}
    pb.RegisterFooServiceServer(server, api)
}
"""


def test_same_named_services_are_told_apart_by_the_import_path() -> None:
    """Two proto files declare `FooService`; the
    handler's generated-package alias names the replayer one."""
    a = parse_proto("api/foo/v1/foo.proto", PROTO)
    b = parse_proto("replayer/proto/replay_service.proto", PROTO_B)
    table: dict[str, list[tuple[str, str, str]]] = {}
    for pf in (a, b):
        for s in pf.symbols:
            table.setdefault(s.name, []).append((s.id, s.qualified_name, s.kind))
    handler = parse_go("replayer/internal/api/app.go", HANDLER_ALIASED)
    edges = grpc_edges(handler, table, Edge)
    got = {(e.edge_kind, e.to_qualified_name) for e in edges}
    assert got == {
        ("inherits", "replayer.proto.replay_service::FooService"),
        ("overrides", "replayer.proto.replay_service::FooService.GetThing"),
    }, "the identifier `api` is typed from `api := &API{}` and the alias picks replayer's service"
    # the original handler (alias foov1 → api/foo/v1) still binds the api/foo one
    edges = grpc_edges(parse_go("cmd/server/server.go", HANDLER), table, Edge)
    assert {e.to_qualified_name for e in edges if e.edge_kind == "inherits"} == {
        "api.foo.v1.foo::FooService",
    }


def test_an_unqualified_tie_stays_unbound() -> None:
    a = parse_proto("api/foo/v1/foo.proto", PROTO)
    b = parse_proto("replayer/proto/replay_service.proto", PROTO_B)
    table: dict[str, list[tuple[str, str, str]]] = {}
    for pf in (a, b):
        for s in pf.symbols:
            table.setdefault(s.name, []).append((s.id, s.qualified_name, s.kind))
    bare = HANDLER_ALIASED.replace(
        b'pb "go.example.com/acme/replayer/proto"', b'pb "example.org/x/unrelated"'
    )
    assert grpc_edges(parse_go("cmd/x/x.go", bare), table, Edge) == []


QUERIES_SERVICE = b"""package queries

type Service struct{ cfg int }

func New(cfg int) (*Service, error) { return &Service{cfg: cfg}, nil }

func (s *Service) GetThing(ctx context.Context, req *pb.GetThingRequest) (*pb.Thing, error) { return nil, nil }

func (s *Service) Other() {}
"""

APP_REGISTER = b"""package api

import (
    "go.example.com/acme/replayer/internal/api/queries"
    pb "go.example.com/acme/replayer/proto"
)

func NewFromConfig(cfg int, server *grpc.Server) error {
    api, err := queries.New(cfg)
    if err != nil {
        return err
    }
    pb.RegisterFooServiceServer(server, api)
    return nil
}
"""


def test_a_registration_typed_to_another_packages_struct_is_read_from_rows() -> None:
    """`api, err:= queries.New(cfg)` → `queries.Service`
    lives in other files; the ingest hands its row and method rows in."""
    from server.code.domain.extract.go_grpc import external_registrations, external_struct

    app = parse_go("replayer/internal/api/app.go", APP_REGISTER)
    assert external_registrations(app) == [], "untyped without the callee's return"
    impls = external_registrations(app, {"queries.New": "Service"})
    assert [(i.service, i.service_alias, i.type_alias, i.type_name) for i in impls] == [
        ("FooService", "pb", "queries", "Service"),
    ]
    svc_pkg = parse_go("replayer/internal/api/queries/service.go", QUERIES_SERVICE)
    proto = parse_proto("replayer/proto/replay_service.proto", PROTO_B)
    table: dict[str, list[tuple[str, str, str]]] = {}
    for pf in (svc_pkg, proto):
        for s in pf.symbols:
            table.setdefault(s.name, []).append((s.id, s.qualified_name, s.kind))
    # another package's `Service` does not confuse the pick: the alias's import path decides
    table["Service"].append(
        ("other", "logsvc.lsvc.internal.api.queries.queries::Service", "struct")
    )
    found = external_struct(table, impls[0], "go.example.com/acme/replayer/internal/api/queries")
    assert found is not None and found[1] == "replayer.internal.api.queries.queries::Service"
    struct_id, struct_qn = found
    methods = [
        (s.name, s.id, s.qualified_name, s.start_line)
        for s in svc_pkg.symbols
        if s.kind == "function" and s.qualified_name.startswith(struct_qn + "::")
    ]
    external = [
        {
            "service": "FooService",
            "service_alias": "pb",
            "struct_id": struct_id,
            "struct_qn": struct_qn,
            "line": 13,
            "methods": methods,
        }
    ]
    edges, _m = extract_edges_and_misses(
        app,
        {},
        lane_context={"go_grpc_table": table, "go_grpc_external": external},
    )
    got = {
        (e.edge_kind, e.from_qualified_name, e.to_qualified_name, e.confidence)
        for e in edges
        if e.edge_kind in ("inherits", "overrides")
    }
    assert got == {
        (
            "inherits",
            "replayer.internal.api.queries.queries::Service",
            "replayer.proto.replay_service::FooService",
            0.8,
        ),
        (
            "overrides",
            "replayer.internal.api.queries.queries::Service::GetThing",
            "replayer.proto.replay_service::FooService.GetThing",
            0.8,
        ),
    }, "`Other` is no rpc; the struct in another file is the edge's source"


def test_the_ingest_reads_the_cross_package_struct_from_the_repository() -> None:
    """The gate must not be the untyped read (empty for exactly these
    files). With a repository that answers
    the callee's return type, the struct row and its method rows — the
    compiler tier says `class` / `method` — the lane context carries the
    external implementor and the merged service table."""
    import asyncio

    from server.code.app.index.ingest_edges import _go_external_registrations
    from server.code.domain.extract.go_grpc import has_identifier_registration

    app = parse_go("replayer/internal/api/app.go", APP_REGISTER)
    assert has_identifier_registration(app)
    assert not has_identifier_registration(parse_go("x/y.go", QUERIES_SERVICE))
    svc_pkg = parse_go("replayer/internal/api/queries/service.go", QUERIES_SERVICE)
    struct = next(s for s in svc_pkg.symbols if s.name == "Service")
    new = next(s for s in svc_pkg.symbols if s.name == "New")
    methods = [s for s in svc_pkg.symbols if "::Service::" in s.qualified_name]

    class _Repo:
        calls: list[str] = []

        async def return_types_for_qualified(
            self, org: str, ns: str, qns: list[str]
        ) -> dict[str, str]:
            self.calls.append("returns")
            return {new.qualified_name: "Service"} if new.qualified_name in qns else {}

        async def name_candidates(
            self, org: str, ns: str, names: list[str]
        ) -> dict[str, list[tuple[str, str, str]]]:
            self.calls.append("candidates")
            out: dict[str, list[tuple[str, str, str]]] = {}
            if "Service" in names:  # both tiers answer; the compiler tier says class
                out["Service"] = [
                    (struct.id, struct.qualified_name, "class"),
                    ("other", "logsvc.lsvc.internal.api.queries.queries::Service", "struct"),
                ]
            for m in methods:
                if m.name in names:
                    out.setdefault(m.name, []).append((m.id, m.qualified_name, "method"))
            return out

        async def members_of(
            self, org: str, ns: str, qns: list[str], limit: int = 64
        ) -> list[dict]:
            self.calls.append("members")
            assert qns == [struct.qualified_name]
            return [
                {
                    "symbol_id": m.id,
                    "qualified_name": m.qualified_name,
                    "name": m.name,
                    "kind": "method" if m.kind == "function" else "field",
                    "path": m.path,
                    "tier": "scip",
                    "start_line": m.start_line,
                }
                for m in methods
            ]

    name_table = {
        "New": [
            (new.id, new.qualified_name, "function"),
            ("o", "logsvc.lsvc.internal.api.queries.queries::New", "function"),
        ]
    }
    from server.code.domain.extract.lang_go import go_package_context

    # the ingest's Go seat: alias → package scope through the module index
    ctx: dict = {
        "go_grpc_table": {},
        "go_packages": go_package_context(app, [("go.example.com/acme", "")]),
    }
    assert ctx["go_packages"]["queries"] == "replayer.internal.api.queries"
    asyncio.run(_go_external_registrations(_Repo(), "o", "n", app, name_table, ctx))
    ext = ctx.get("go_grpc_external")
    assert (
        ext and ext[0]["struct_qn"] == struct.qualified_name and ext[0]["service"] == "FooService"
    )
    assert sorted(m[0] for m in ext[0]["methods"]) == ["GetThing", "Other"], (
        "the field row is not a method"
    )
    assert _Repo.calls == ["returns", "candidates", "members", "candidates"]
