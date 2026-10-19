"""namespace.register_ontology + namespace.list_ontology.

Operator/admin surface for typed entity/relation registration.
Reads + writes loom.namespace_ontology. The accepted set is loaded
by the extractor on each set_from_messages call (see
server/memory/app/extract/handler.py load_namespace_ontology) so
extracted names converge to registered ones.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

from server.core.ch import get_client
from server.identity.api.models_namespace_catalog import (
    NamespaceListOntologyInput,
    NamespaceListOntologyOutput,
    NamespaceRegisterOntologyInput,
    NamespaceRegisterOntologyOutput,
    _OntologyEntry,
)
from server.settings import settings


async def handle_namespace_register_ontology(
    input: NamespaceRegisterOntologyInput,
) -> NamespaceRegisterOntologyOutput:
    from server.auth.context import current_auth

    actor = ""
    auth = current_auth()
    if auth is not None and auth.subject:
        actor = auth.subject

    ch = get_client()
    now = datetime.now(UTC).replace(tzinfo=None)
    await ch.insert(
        "loom.namespace_ontology",
        [
            {
                "org": input.org,
                "namespace": input.namespace,
                "kind": input.kind,
                "name": input.name,
                "status": input.status,
                "description": input.description,
                "cardinality": (
                    input.cardinality if input.kind in ("relation_verb", "attribute") else ""
                ),
                "created_by": actor,
                "created_at": now,
                "updated_at": now,
            }
        ],
    )
    _cardinality_cache.pop((input.org, input.namespace), None)
    return NamespaceRegisterOntologyOutput(
        registered=True,
        org=input.org,
        namespace=input.namespace,
        kind=input.kind,
        name=input.name,
        status=input.status,
    )


async def handle_namespace_list_ontology(
    input: NamespaceListOntologyInput,
) -> NamespaceListOntologyOutput:
    """List the ontology terms (entity and relation kinds) registered for a namespace."""
    ch = get_client()
    where = ["org = {org:String}", "namespace = {ns:String}"]
    params: dict[str, object] = {"org": input.org, "ns": input.namespace}
    if input.kind is not None:
        where.append("kind = {kind:String}")
        params["kind"] = input.kind
    if input.status is not None:
        where.append("status = {status:String}")
        params["status"] = input.status

    rows = await ch.query_all(
        f"""
        SELECT kind, name, status, description, cardinality, created_by, updated_at
        FROM loom.namespace_ontology FINAL
        WHERE {" AND ".join(where)}
        ORDER BY kind ASC, name ASC
        """,
        params=params,
    )
    entries = [
        _OntologyEntry(
            kind=str(r["kind"]),
            name=str(r["name"]),
            status=str(r["status"]),
            description=str(r.get("description") or ""),
            cardinality=str(r.get("cardinality") or ""),
            created_by=str(r.get("created_by") or ""),
            updated_at=r["updated_at"],
        )
        for r in rows
    ]
    return NamespaceListOntologyOutput(
        entries=entries,
        org=input.org,
        namespace=input.namespace,
    )


async def load_namespace_ontology(org: str, namespace: str) -> dict[str, list[str]]:
    """Return {kind: [accepted names]} for the (org, namespace).

    Used by server/memory/app/extract/handler.py to inject the accepted
    set into the extraction prompt + to flag warnings for emitted
    names outside the accepted set. Empty dict on cold namespaces
    (no ontology registered yet) — extractor falls through to its
    default unrestricted behavior.
    """
    ch = get_client()
    rows = await ch.query_all(
        """
        SELECT kind, name
        FROM loom.namespace_ontology FINAL
        WHERE org = {org:String}
          AND namespace = {ns:String}
          AND status = 'accepted'
        """,
        params={"org": org, "ns": namespace},
    )
    out: dict[str, list[str]] = {"entity_type": [], "relation_verb": []}
    for r in rows:
        kind = str(r["kind"])
        out.setdefault(kind, []).append(str(r["name"]))
    return out


# ------------------------------------------------------------------
# Relation cardinality — read by the triple writer.
# ------------------------------------------------------------------

# (org, namespace) -> (loaded_at_monotonic, {lemma_relation: "one"|"many"})
_cardinality_cache: dict[tuple[str, str], tuple[float, dict[str, str]]] = {}


async def load_relation_cardinality(org: str, namespace: str) -> dict[str, str]:
    """{lemma relation: "one" | "many"} declared for the namespace.

    The triple writer consults this before the seed list on every write, so
    the read is cached per process for a minute; register_ontology drops the
    entry so a fresh declaration reaches the next write. Empty on cold
    namespaces and on any read error — the writer then falls through to the
    seed list, never to a guess.
    """
    key = (org, namespace)
    now = time.monotonic()
    hit = _cardinality_cache.get(key)
    if hit is not None and now - hit[0] < settings.namespace_ontology_cardinality_ttl_s:
        return hit[1]
    from server.memory.domain.graph_quality import declared_cardinality

    try:
        ch = get_client()
        rows = await ch.query_all(
            """
            SELECT name, cardinality
            FROM loom.namespace_ontology FINAL
            WHERE org = {org:String}
              AND namespace = {ns:String}
              AND kind = 'relation_verb'
              AND status = 'accepted'
              AND cardinality != ''
            """,
            params={"org": org, "ns": namespace},
        )
        declared = declared_cardinality(
            (str(r["name"]), str(r.get("cardinality") or "")) for r in rows
        )
    except Exception:  # noqa: BLE001 — a cardinality read never blocks a write
        declared = {}
    _cardinality_cache[key] = (now, declared)
    return declared
