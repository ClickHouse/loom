"""Typed Python property reads keep their lexical receiver and their edge kind."""

from server.code.domain.extract.lang_python import parse_python
from server.code.domain.extract.py_member_reads import (
    module_types,
    read_edges,
    read_sites,
    receiver_targets,
)


def test_typed_reads_decline_shadowed_receivers_and_write_targets() -> None:
    context = parse_python(
        "src/pkg/context.py",
        b"""class Client:
    @property
    def option(self):
        return 1

client: Client
""",
    )
    reader = parse_python(
        "src/pkg/reader.py",
        b"""from .context import client as default_client

class Other:
    @property
    def option(self):
        return 2

def imported():
    return default_client.option

def local(client: Other):
    return client.option

def shadow(default_client):
    return default_client.option

def update():
    default_client.option = 2
    default_client.option()

class Outer:
    def method(self):
        def nested(self):
            return self.option
        return nested(self)

def reassigned(client: Other):
    value = client.option
    client = object()
    return value

def imported_over_parameter(client: Other):
    import sys as client
    return client.option

from .shadow import Client

class NestedType:
    class Client:
        option = 7

    def use(self, client: Client):
        return client.option
""",
    )
    other = parse_python("src/pkg/shadow.py", b"class Client:\n    option = 3\n")
    names: dict[str, list[tuple[str, str, str]]] = {}
    for pf in (context, reader, other):
        for symbol in pf.symbols:
            names.setdefault(symbol.name, []).append(
                (symbol.id, symbol.qualified_name, symbol.kind)
            )
    lane_context = {"py_present": {context.path, reader.path, other.path}, "py_roots": ("src",)}
    modules = module_types(reader, {context.path: context.source.decode("utf-8")})
    sites = read_sites(reader)
    targets = receiver_targets(reader, sites, names, lane_context)
    declaration = next(s for s in context.symbols if s.name == "client")
    key = (declaration.qualified_name, declaration.id)
    assert targets == {"default_client": key}
    edges = read_edges(
        reader, sites, names, targets, {key: (context.path, "Client")}, modules, lane_context
    )
    assert [
        (e.edge_kind, e.line, e.to_qualified_name) for e in edges if e.edge_kind == "reads"
    ] == [
        ("reads", 9, "src.pkg.context::client"),
        ("reads", 9, "src.pkg.context::Client::option"),
        ("reads", 12, "src.pkg.reader::Other::option"),
    ]
    unresolved = read_edges(reader, sites, names, targets, {}, modules, lane_context)
    assert [(e.line, e.to_symbol_id) for e in unresolved if e.line == 9] == [(9, declaration.id)]
