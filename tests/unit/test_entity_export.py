"""Export a namespace's entity graph to GraphML / JSON-LD, with
per-edge provenance and an honesty note (a path is declared, not proven)."""

from __future__ import annotations

import json
from typing import Any
from xml.etree import ElementTree as ET

import pytest

from server.memory.app.entity_export import (
    CoEdge,
    EntityGraph,
    Triple,
    export_entity_graph,
    to_graphml,
    to_jsonld,
)


def _graph() -> EntityGraph:
    return EntityGraph(
        org="o",
        namespace="n",
        co_edges=[CoEdge(a="Ada", b="Lovelace", co_occur=5, last_seen="2026-02-01 00:00:00")],
        triples=[
            Triple(
                "Ada",
                "wrote",
                "the first program",
                co_occur=2,
                confidence=0.9,
                provenance="extracted",
                last_seen="2026-02-02 00:00:00",
            ),
            Triple(
                "Ada", "is likely", "an analyst", co_occur=1, confidence=0.4, provenance="inferred"
            ),
        ],
    )


def test_nodes_are_deduped_and_first_seen_ordered() -> None:
    g = _graph()
    # Ada (edge.a), Lovelace (edge.b), then the two triple objects; Ada not repeated
    assert g.nodes() == ["Ada", "Lovelace", "the first program", "an analyst"]


def test_graphml_is_valid_xml_with_provenance_and_honesty_note() -> None:
    xml = to_graphml(_graph())
    assert xml.startswith("<?xml")
    assert "declared" in xml and "not proof" in xml  # honesty note present
    root = ET.fromstring(xml.split("?>", 1)[1])  # parses = well-formed
    ns = "{http://graphml.graphdrawing.org/xmlns}"
    labels = {n.find(f"{ns}data").text for n in root.iter(f"{ns}node")}
    assert {"Ada", "Lovelace", "an analyst"} <= labels
    edges = list(root.iter(f"{ns}edge"))
    # one co-occurrence (undirected) + two triples (directed by default)
    assert sum(e.get("directed") == "false" for e in edges) == 1
    datas = [d.text for e in edges for d in e.findall(f"{ns}data")]
    assert "extracted" in datas and "inferred" in datas and "co_occurrence" in datas


def test_graphml_escapes_xml_special_chars() -> None:
    g = EntityGraph(org="o", namespace="n", co_edges=[CoEdge(a="A & B <x>", b='"C"', co_occur=1)])
    xml = to_graphml(g)
    root = ET.fromstring(xml.split("?>", 1)[1])  # would raise if escaping were wrong
    ns = "{http://graphml.graphdrawing.org/xmlns}"
    labels = {n.find(f"{ns}data").text for n in root.iter(f"{ns}node")}
    assert "A & B <x>" in labels and '"C"' in labels


def test_jsonld_has_context_graph_note_and_provenance() -> None:
    doc = json.loads(to_jsonld(_graph()))
    assert doc["@context"]["loom"].startswith("https://")
    assert "declared" in doc["loom:note"]
    types = [it["@type"] for it in doc["@graph"]]
    assert types.count("loom:Entity") == 4
    assert "loom:CoOccurrence" in types and "loom:Statement" in types
    stmts = [it for it in doc["@graph"] if it["@type"] == "loom:Statement"]
    assert {s["provenance"] for s in stmts} == {"extracted", "inferred"}
    # subject/object reference entity @ids, not raw strings
    ids = {it["@id"] for it in doc["@graph"] if it["@type"] == "loom:Entity"}
    assert all(s["subject"] in ids and s["object"] in ids for s in stmts)


class _FakeCH:
    """query_all dispatches on which table the SQL names."""

    async def query_all(self, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        assert params == {"org": "o", "namespace": "n"}
        if "entity_edges" in sql:
            return [
                {
                    "entity_a": "Ada",
                    "entity_b": "Grace",
                    "co_occur": 3,
                    "last_seen": "2026-02-01 00:00:00",
                    "valid_from": None,
                    "valid_to": None,
                }
            ]
        return [
            {
                "subject": "Ada",
                "relation": "knows",
                "object": "Grace",
                "co_occur": 1,
                "confidence": 1.0,
                "provenance": "extracted",
                "last_seen": "",
            }
        ]


async def test_export_dispatches_and_rejects_unknown_format() -> None:
    ch = _FakeCH()
    xml = await export_entity_graph(ch, org="o", namespace="n", fmt="graphml")
    assert "Grace" in xml and xml.startswith("<?xml")
    js = json.loads(await export_entity_graph(ch, org="o", namespace="n", fmt="jsonld"))
    assert any(it.get("label") == "Grace" for it in js["@graph"])
    with pytest.raises(ValueError, match="unknown format"):
        await export_entity_graph(ch, org="o", namespace="n", fmt="dot")
