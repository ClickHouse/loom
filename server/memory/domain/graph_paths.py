"""A graph answer shows its path.

Graph mode expands the query's entities one or more hops through the entity graph and
boosts hits whose entities overlap the expansion. A flat list of neighbours would let a
hit say "graph_overlap = 0.3" but never WHICH query entity it hangs off, or through
what, so `fetch_graph_neighbors` fills a ``paths`` map (neighbour -> [seed, ...,
neighbour]) and :func:`via_path` picks, for one hit, the shortest chain from a query
entity to one of the hit's own entities. Pure; the SQL side lives in
server/memory/app/retrieval/graph.py.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence


def via_path(
    hit_entities: Iterable[str],
    seeds: Sequence[str],
    paths: Mapping[str, Sequence[str]],
) -> list[str]:
    """Shortest chain from a query seed to an entity on the hit.

    A hit that carries a seed entity itself answers with ``[seed]`` (zero
    hops). Otherwise the shortest recorded expansion path ending at one of
    the hit's entities wins; ties keep the hit's entity order. Empty when
    the hit is in the pool for some other reason (cosine, lexical, tags).
    """
    ents = [e for e in hit_entities if e]
    if not ents:
        return []
    ent_set = set(ents)
    for s in seeds:
        if s in ent_set:
            return [s]
    best: list[str] = []
    for e in ents:
        p = paths.get(e)
        if p and (not best or len(p) < len(best)):
            best = list(p)
    return best


def extend_paths(
    paths: dict[str, list[str]],
    hop_rows: Iterable[tuple[str, str]],
    seeds: Sequence[str],
) -> None:
    """Record one hop of expansion: ``(neighbour, via)`` pairs, where ``via``
    is the frontier entity the neighbour was reached from. A ``via`` that
    is a seed starts a fresh chain; one already in ``paths`` extends its
    chain; an unknown ``via`` (stubbed rows, hub-pruned seeds) falls back
    to the first seed so the neighbour is never left without a path.
    First writer wins — a neighbour reached again later is not rerouted."""
    seed_set = set(seeds)
    for neighbour, via in hop_rows:
        if not neighbour or neighbour in paths or neighbour in seed_set:
            continue
        if via in seed_set:
            paths[neighbour] = [via, neighbour]
        elif via in paths:
            paths[neighbour] = [*paths[via], neighbour]
        elif seeds:
            paths[neighbour] = [seeds[0], neighbour]
