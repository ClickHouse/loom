"""Export a namespace's entity graph to a standard interchange format.

loom stores every co-occurrence edge (`loom.entity_edges`) and every
subject–relation–object triple (`loom.entity_triples`). This module hands that
graph to an external tool — Gephi, rdflib, Protégé — for inspection or layout:
it reads the live graph for one namespace and serialises it
to GraphML (XML, for graph tools) or JSON-LD (for RDF/linked-data tools).

Honesty ("evidence does not promote itself"):
every edge carries its `provenance` and every co-occurrence its `co_occur`
weight, and both formats carry a top-level note stating that a path in this
graph is a DECLARED co-occurrence or an extracted/inferred relationship — not
proof of a verified fact or of a runtime blast radius. The export shows what
loom recorded and how it came to record it; it does not launder that into
certainty.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any
from xml.etree import ElementTree as ET

_NOTE = (
    "A path in this graph is a declared co-occurrence or an "
    "extracted/inferred relationship loom recorded, not proof of a verified "
    "fact or of a runtime blast radius. Read edge provenance and weight before "
    "relying on any edge."
)
_NS = "https://loom.clickhouse.com/ns#"


@dataclass
class CoEdge:
    a: str
    b: str
    co_occur: int
    last_seen: str = ""
    valid_from: str = ""
    valid_to: str = ""


@dataclass
class Triple:
    subject: str
    relation: str
    object: str
    co_occur: int = 1
    confidence: float = 1.0
    provenance: str = ""
    last_seen: str = ""


@dataclass
class EntityGraph:
    org: str
    namespace: str
    co_edges: list[CoEdge] = field(default_factory=list)
    triples: list[Triple] = field(default_factory=list)

    def nodes(self) -> list[str]:
        """Every distinct entity, in first-seen order (stable output)."""
        seen: dict[str, None] = {}
        for e in self.co_edges:
            seen.setdefault(e.a, None)
            seen.setdefault(e.b, None)
        for t in self.triples:
            seen.setdefault(t.subject, None)
            seen.setdefault(t.object, None)
        return list(seen)


def _s(v: Any) -> str:
    return "" if v is None else str(v)


async def fetch_graph(ch: Any, *, org: str, namespace: str, live_only: bool = True) -> EntityGraph:
    """Read the (live) entity graph for one namespace. `live_only` keeps rows
    with `invalid_at IS NULL` and an unexpired `valid_to`."""
    params = {"org": org, "namespace": namespace}
    live = " AND (valid_to IS NULL OR valid_to > now())" if live_only else ""
    edge_rows = await ch.query_all(
        f"""
        SELECT entity_a, entity_b, sum(co_occur) AS co_occur, max(last_seen) AS last_seen,
               min(valid_from) AS valid_from, max(valid_to) AS valid_to
        FROM loom.entity_edges
        WHERE org = {{org:String}} AND namespace = {{namespace:String}} AND invalid_at IS NULL{live}
        GROUP BY entity_a, entity_b
        ORDER BY co_occur DESC, entity_a, entity_b
        """,
        params,
    )
    triple_rows = await ch.query_all(
        f"""
        SELECT subject, relation, object, sum(co_occur) AS co_occur,
               max(confidence) AS confidence, argMax(provenance, last_seen) AS provenance,
               max(last_seen) AS last_seen
        FROM loom.entity_triples
        WHERE org = {{org:String}} AND namespace = {{namespace:String}} AND invalid_at IS NULL{live}
        GROUP BY subject, relation, object
        ORDER BY co_occur DESC, subject, relation, object
        """,
        params,
    )
    return EntityGraph(
        org=org,
        namespace=namespace,
        co_edges=[
            CoEdge(
                a=_s(r.get("entity_a")),
                b=_s(r.get("entity_b")),
                co_occur=int(r.get("co_occur") or 0),
                last_seen=_s(r.get("last_seen")),
                valid_from=_s(r.get("valid_from")),
                valid_to=_s(r.get("valid_to")),
            )
            for r in edge_rows
        ],
        triples=[
            Triple(
                subject=_s(r.get("subject")),
                relation=_s(r.get("relation")),
                object=_s(r.get("object")),
                co_occur=int(r.get("co_occur") or 0),
                confidence=float(r.get("confidence") or 0.0),
                provenance=_s(r.get("provenance")),
                last_seen=_s(r.get("last_seen")),
            )
            for r in triple_rows
        ],
    )


def to_graphml(graph: EntityGraph) -> str:
    """GraphML with synthetic node ids (entity text in the `label` data), one
    directed graph: co-occurrence edges are marked `directed="false"`, triples
    directed. Edge data carries kind/relation/weight/provenance/confidence."""
    root = ET.Element("graphml", {"xmlns": "http://graphml.graphdrawing.org/xmlns"})
    root.append(ET.Comment(f" {_NOTE} "))
    keys = [
        ("d_label", "node", "label", "string"),
        ("d_kind", "edge", "kind", "string"),
        ("d_relation", "edge", "relation", "string"),
        ("d_weight", "edge", "co_occur", "long"),
        ("d_prov", "edge", "provenance", "string"),
        ("d_conf", "edge", "confidence", "double"),
        ("d_seen", "edge", "last_seen", "string"),
    ]
    for kid, dom, name, typ in keys:
        ET.SubElement(
            root,
            "key",
            {"id": kid, "for": dom, "attr.name": name, "attr.type": typ},
        )
    g = ET.SubElement(root, "graph", {"id": f"{graph.namespace}", "edgedefault": "directed"})
    ids = {name: f"n{i}" for i, name in enumerate(graph.nodes())}
    for name, nid in ids.items():
        node = ET.SubElement(g, "node", {"id": nid})
        _data(node, "d_label", name)
    ei = 0
    for e in graph.co_edges:
        edge = ET.SubElement(
            g, "edge", {"id": f"e{ei}", "source": ids[e.a], "target": ids[e.b], "directed": "false"}
        )
        _data(edge, "d_kind", "co_occurrence")
        _data(edge, "d_weight", str(e.co_occur))
        _data(edge, "d_prov", "observed")
        _data(edge, "d_seen", e.last_seen)
        ei += 1
    for t in graph.triples:
        edge = ET.SubElement(
            g, "edge", {"id": f"e{ei}", "source": ids[t.subject], "target": ids[t.object]}
        )
        _data(edge, "d_kind", "triple")
        _data(edge, "d_relation", t.relation)
        _data(edge, "d_weight", str(t.co_occur))
        _data(edge, "d_prov", t.provenance or "unknown")
        _data(edge, "d_conf", f"{t.confidence:.4g}")
        _data(edge, "d_seen", t.last_seen)
        ei += 1
    ET.indent(root)
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode")


def _data(parent: ET.Element, key: str, value: str) -> None:
    el = ET.SubElement(parent, "data", {"key": key})
    el.text = value


def to_jsonld(graph: EntityGraph) -> str:
    """JSON-LD: an `@graph` of Entity nodes, CoOccurrence edges and Statement
    (triple) edges, with a top-level honesty note and per-edge provenance."""
    ids = {name: f"{_NS}entity/{i}" for i, name in enumerate(graph.nodes())}
    items: list[dict[str, Any]] = [
        {"@id": nid, "@type": "loom:Entity", "label": name} for name, nid in ids.items()
    ]
    for e in graph.co_edges:
        items.append(
            {
                "@type": "loom:CoOccurrence",
                "source": ids[e.a],
                "target": ids[e.b],
                "co_occur": e.co_occur,
                "provenance": "observed",
                "last_seen": e.last_seen,
            }
        )
    for t in graph.triples:
        items.append(
            {
                "@type": "loom:Statement",
                "subject": ids[t.subject],
                "loom:relation": t.relation,
                "object": ids[t.object],
                "co_occur": t.co_occur,
                "confidence": round(t.confidence, 4),
                "provenance": t.provenance or "unknown",
                "last_seen": t.last_seen,
            }
        )
    doc = {
        "@context": {
            "loom": _NS,
            "label": "loom:label",
            "source": {"@id": "loom:source", "@type": "@id"},
            "target": {"@id": "loom:target", "@type": "@id"},
            "subject": {"@id": "loom:subject", "@type": "@id"},
            "object": {"@id": "loom:object", "@type": "@id"},
        },
        "loom:namespace": graph.namespace,
        "loom:note": _NOTE,
        "@graph": items,
    }
    return json.dumps(doc, indent=2, ensure_ascii=False)


async def export_entity_graph(
    ch: Any, *, org: str, namespace: str, fmt: str, live_only: bool = True
) -> str:
    """Fetch and serialise. `fmt` in {"graphml", "jsonld"}."""
    graph = await fetch_graph(ch, org=org, namespace=namespace, live_only=live_only)
    if fmt == "graphml":
        return to_graphml(graph)
    if fmt == "jsonld":
        return to_jsonld(graph)
    raise ValueError(f"unknown format {fmt!r} (expected graphml or jsonld)")
