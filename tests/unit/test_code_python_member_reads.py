"""Typed Python property reads keep their lexical receiver and their edge kind."""

from server.code.domain.extract.lang_python import parse_python
from server.code.domain.extract.py_member_reads import read_edges, read_sites, receiver_targets


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
        b"""from .context import client

class Other:
    @property
    def option(self):
        return 2

def imported():
    return client.option

def local(client: Other):
    return client.option

def shadow(client):
    return client.option

def update():
    client.option = 2
    client.option()
""",
    )
    names: dict[str, list[tuple[str, str, str]]] = {}
    for pf in (context, reader):
        for symbol in pf.symbols:
            names.setdefault(symbol.name, []).append(
                (symbol.id, symbol.qualified_name, symbol.kind)
            )
    lane_context = {"py_present": {context.path, reader.path}, "py_roots": ("src",)}
    sites = read_sites(reader)
    targets = receiver_targets(reader, sites, names, lane_context)
    assert targets == {"name:client": "src.pkg.context::client"}
    edges = read_edges(reader, sites, names, {"name:client": "Client"}, lane_context)
    assert [(e.edge_kind, e.line, e.to_qualified_name) for e in edges] == [
        ("reads", 9, "src.pkg.context::Client::option"),
        ("reads", 12, "src.pkg.reader::Other::option"),
    ]
