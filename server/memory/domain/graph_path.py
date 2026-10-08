"""facts.path — a path between two named entities.

The pure half of a bidirectional bounded breadth-first search. The handler
fetches one frontier per step from ClickHouse (the same aggregated,
validity-filtered edge set `facts.traverse` walks); this module decides
which side to expand, records how each entity was reached, detects the
meeting point and rebuilds the chain a … b with the edges that carry it.

"How are A and B connected" is one question, not a walk from A that hopes to
pass B; facts.traverse covers the walk and graph answers show the seed → hit
chain, and this module answers the question itself.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class PathEdge:
    """One aggregated edge on the path, in the order the path reads it."""

    entity_a: str  # the end nearer `a`
    entity_b: str  # the end nearer `b`
    relation: str = ""  # typed walk only
    direction: str = ""  # typed walk only: "out" = entity_a is the subject
    co_occur: int = 0
    valid_from: Any = None
    valid_to: Any = None

    def reversed(self) -> PathEdge:
        return PathEdge(
            entity_a=self.entity_b,
            entity_b=self.entity_a,
            relation=self.relation,
            direction={"out": "in", "in": "out"}.get(self.direction, self.direction),
            co_occur=self.co_occur,
            valid_from=self.valid_from,
            valid_to=self.valid_to,
        )


@dataclass
class Side:
    """One end of the search: what it has reached and how."""

    root: str
    depth: int = 0
    frontier: list[str] = field(default_factory=list)
    # entity -> (parent entity, edge parent->entity as fetched: src=parent, dst=entity)
    parents: dict[str, tuple[str, PathEdge]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.frontier:
            self.frontier = [self.root]

    @property
    def visited(self) -> set[str]:
        return {self.root, *self.parents}

    def depth_of(self, entity: str) -> int:
        d = 0
        cur = entity
        while cur != self.root:
            cur = self.parents[cur][0]
            d += 1
        return d


def row_edge(row: Mapping[str, Any]) -> PathEdge:
    """A fetched frontier row (src, dst, relation, direction, pair_co_occur,
    valid_from, valid_to) as the edge src -> dst."""
    return PathEdge(
        entity_a=str(row.get("src") or ""),
        entity_b=str(row.get("dst") or ""),
        relation=str(row.get("relation") or ""),
        direction=str(row.get("direction") or ""),
        co_occur=int(row.get("pair_co_occur") or 0),
        valid_from=row.get("valid_from"),
        valid_to=row.get("valid_to"),
    )


def absorb(side: Side, rows: Iterable[Mapping[str, Any]]) -> list[str]:
    """Record one fetched frontier on `side`; return the newly reached entities.

    A `dst` already reached on this side keeps its first (shorter) parent —
    breadth-first order. The frontier advances to the new entities and the
    depth grows by one.
    """
    seen = side.visited
    new: list[str] = []
    for row in rows:
        edge = row_edge(row)
        if not edge.entity_b or edge.entity_b in seen or edge.entity_a not in seen:
            continue
        side.parents[edge.entity_b] = (edge.entity_a, edge)
        seen.add(edge.entity_b)
        new.append(edge.entity_b)
    side.frontier = new
    side.depth += 1
    return new


def chain_to_root(side: Side, entity: str) -> tuple[list[str], list[PathEdge]]:
    """Entities and edges from `entity` back to the side's root, root FIRST.

    Edges are oriented root-wards -> outwards (src nearer the root)."""
    nodes = [entity]
    edges: list[PathEdge] = []
    cur = entity
    while cur != side.root:
        parent, edge = side.parents[cur]
        edges.append(edge)
        nodes.append(parent)
        cur = parent
    nodes.reverse()
    edges.reverse()
    return nodes, edges


def meet(a: Side, b: Side, candidates: Iterable[str]) -> tuple[list[str], list[PathEdge]] | None:
    """Among entities reached by BOTH sides, pick the one with the fewest total
    hops and rebuild the path a.root … b.root with its edges oriented a -> b."""
    best: tuple[int, str] | None = None
    for c in candidates:
        if c not in a.visited or c not in b.visited:
            continue
        total = a.depth_of(c) + b.depth_of(c)
        if best is None or total < best[0] or (total == best[0] and c < best[1]):
            best = (total, c)
    if best is None:
        return None
    node = best[1]
    a_nodes, a_edges = chain_to_root(a, node)  # a.root … node
    b_nodes, b_edges = chain_to_root(b, node)  # b.root … node
    # b's chain reads b.root -> node; the path wants node -> b.root, and each
    # edge flipped so entity_a stays the end nearer `a`.
    tail_nodes = list(reversed(b_nodes))[1:]
    tail_edges = [e.reversed() for e in reversed(b_edges)]
    return a_nodes + tail_nodes, a_edges + tail_edges


def pick_side(a: Side, b: Side) -> Side:
    """Expand the smaller frontier: the classic bidirectional rule that keeps
    the explored set near 2·b^(d/2) instead of b^d. Equal frontiers expand
    the shallower side, so the two searches alternate and meet in the middle
    instead of one side walking the whole budget; a full tie goes to `a`."""
    if len(b.frontier) < len(a.frontier):
        return b
    if len(b.frontier) == len(a.frontier) and b.depth < a.depth:
        return b
    return a
