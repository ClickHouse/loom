"""procedure.fork: read-authorised on the source the way
memory.cross_promote is, evidence reset on the copy, lineage kept."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace as NS
from typing import Any

import pytest

from server.auth.jwt import AuthInfo
from server.operations.base import Role


def _auth(
    subject: str = "alice",
    namespaces: tuple[str, ...] = ("team",),
    roles: tuple[Role, ...] = (Role.WRITER,),
    ns_roles: tuple[tuple[str, str], ...] = (),
) -> AuthInfo:
    return AuthInfo(
        subject=subject,
        org="o",
        namespaces=namespaces,
        roles=roles,
        is_service_account=False,
        ns_roles=ns_roles,
    )


def _source() -> dict[str, Any]:
    return {
        "situation": "release day",
        "name": "ship_it",
        "steps": ["build", "test", "ship"],
        "tags": ["ops", "template"],
        "metadata": {"owner": "platform"},
        "graph": {
            "nodes": [
                {"id": "n1", "text": "build", "stats": {"runs": 40, "ok": 39, "fail": 1}},
                {
                    "id": "n2",
                    "text": "test",
                    "stats": {"runs": 40, "ok": 39, "fail": 1},
                    "fail_prone": True,
                },
                {"id": "n3", "text": "ship", "stats": {"runs": 39, "ok": 39, "fail": 0}},
            ],
            "edges": [
                {
                    "src": "n1",
                    "dst": "n2",
                    "weight": 40,
                    "ok_weight": 39,
                    "fail_weight": 1,
                    "guidance": "always test",
                    "pitfalls": "never skip",
                },
                {"src": "n2", "dst": "n3", "weight": 39, "ok_weight": 39},
            ],
        },
        "run_stats": {"total": 40, "ok": 39},
        "runs": [{"path": ["n1"]}],
        "template": True,
        "revision": 4,
        "rejected": [{"revision": 3, "digest": "x"}],
        "pending_revision": None,
    }


def _wire(
    monkeypatch: pytest.MonkeyPatch,
    *,
    auth: AuthInfo | None,
    grant: Role | None = None,
    target_has: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    import server.auth.authz as authz
    import server.auth.context as ctx

    rec: dict[str, Any] = {"sets": [], "grant_lookups": []}
    monkeypatch.setattr(ctx, "current_auth", lambda: auth)

    async def _lookup(org: str, ns: str, subject: str) -> Role | None:
        rec["grant_lookups"].append((org, ns, subject))
        return grant

    monkeypatch.setattr(authz, "_lookup_grant", _lookup)

    async def _get(inp: Any) -> Any:
        if inp.namespace == "library":
            return NS(value=_source(), content_sha256="h")
        existing = (target_has or {}).get(inp.memory_key)
        return NS(value=existing if existing else None, content_sha256="h")

    async def _set(inp: Any) -> Any:
        rec["sets"].append(inp)
        return NS(memory_id="m", version=7)

    monkeypatch.setattr("server.memory.app.handlers.handle_memory_get", _get)
    monkeypatch.setattr("server.memory.app.write.handle_memory_set", _set)
    return rec


def test_fork_copies_topology_resets_evidence_and_keeps_lineage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.api.models_procedure import ProcedureForkInput
    from server.memory.app.procedure_evolve import handle_procedure_fork

    rec = _wire(monkeypatch, auth=_auth(namespaces=("team", "library")))
    out = asyncio.run(
        handle_procedure_fork(
            ProcedureForkInput(
                org="o",
                namespace="team",
                agent="coder",
                from_namespace="library",
                memory_key="procedure_ship_it",
                reason="adopt the platform release flow",
            )
        )
    )
    assert out.memory_key == "procedure_ship_it" and out.version == 7 and out.name == "ship_it"
    assert out.steps == ["build", "test", "ship"]
    assert out.forked_from["namespace"] == "library" and out.forked_from["revision"] == 4
    assert out.forked_from["template"] is True and out.forked_from["run_stats"] == {
        "total": 40,
        "ok": 39,
    }
    assert out.forked_from["by"] == "alice"

    set_in = rec["sets"][0]
    v = set_in.value
    assert set_in.namespace == "team" and set_in.memory_type == "procedural"
    assert v["template"] is False and v["revision"] == 1 and v["run_stats"] == {"total": 0, "ok": 0}
    assert v["runs"] == [] and "rejected" not in v and "pending_revision" not in v
    assert v["tags"] == ["ops", "forked"] and "template" not in set_in.tags
    n2 = v["graph"]["nodes"][1]
    assert n2["stats"] == {"runs": 0, "ok": 0, "fail": 0} and n2["fail_prone"] is False
    e = v["graph"]["edges"][0]
    assert (e["weight"], e["ok_weight"], e["fail_weight"]) == (0.0, 0.0, 0.0)
    assert e["guidance"] == "always test" and e["pitfalls"] == "never skip"  # attributes travel
    assert rec["grant_lookups"] == []  # token claim was enough; no grants query


def test_fork_new_name_rekeys_the_copy(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.memory.api.models_procedure import ProcedureForkInput
    from server.memory.app.procedure_evolve import handle_procedure_fork

    rec = _wire(monkeypatch, auth=_auth(namespaces=("team", "library")))
    out = asyncio.run(
        handle_procedure_fork(
            ProcedureForkInput(
                org="o",
                namespace="team",
                from_namespace="library",
                memory_key="procedure_ship_it",
                new_name="Release Flow (team)",
            )
        )
    )
    assert out.memory_key == "procedure_release_flow_team" and out.name == "Release Flow (team)"
    assert rec["sets"][0].value["name"] == "Release Flow (team)"


def test_fork_refuses_to_overwrite_an_existing_target(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.memory.api.models_procedure import ProcedureForkInput
    from server.memory.app.procedure_evolve import handle_procedure_fork

    _wire(
        monkeypatch,
        auth=_auth(namespaces=("team", "library")),
        target_has={"procedure_ship_it": {"situation": "s", "name": "ship_it", "steps": ["x"]}},
    )
    with pytest.raises(ValueError, match="already exists"):
        asyncio.run(
            handle_procedure_fork(
                ProcedureForkInput(
                    org="o",
                    namespace="team",
                    from_namespace="library",
                    memory_key="procedure_ship_it",
                )
            )
        )


def test_fork_source_read_is_authorised_like_cross_promote(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.core.errors import AuthInsufficientError, ValidationError
    from server.memory.api.models_procedure import ProcedureForkInput
    from server.memory.app.procedure_evolve import handle_procedure_fork

    inp = ProcedureForkInput(
        org="o", namespace="team", from_namespace="library", memory_key="procedure_ship_it"
    )
    # legacy token without the source namespace in its claim and no grant
    _wire(monkeypatch, auth=_auth(namespaces=("team",)), grant=None)
    with pytest.raises(AuthInsufficientError, match="cannot read o/library"):
        asyncio.run(handle_procedure_fork(inp))
    # ... but a VIEWER grant on the source is enough
    rec = _wire(monkeypatch, auth=_auth(namespaces=("team",)), grant=Role.VIEWER)
    assert asyncio.run(handle_procedure_fork(inp)).memory_key == "procedure_ship_it"
    assert rec["grant_lookups"] == [("o", "library", "alice")]
    # scoped token: the per-namespace role map is authoritative
    _wire(
        monkeypatch,
        auth=_auth(
            namespaces=("team", "library"),
            roles=(),
            ns_roles=(("team", "writer"), ("library", "viewer")),
        ),
    )
    assert asyncio.run(handle_procedure_fork(inp)).memory_key == "procedure_ship_it"
    _wire(
        monkeypatch,
        auth=_auth(namespaces=("team", "library"), roles=(), ns_roles=(("team", "writer"),)),
        grant=None,
    )
    with pytest.raises(AuthInsufficientError):
        asyncio.run(handle_procedure_fork(inp))
    # no caller identity at all
    _wire(monkeypatch, auth=None)
    with pytest.raises(ValidationError, match="authenticated caller"):
        asyncio.run(handle_procedure_fork(inp))


def test_reset_evidence_is_pure_and_keeps_attributes() -> None:
    from server.memory.domain.procedure_graph import ProcedureGraph, graph_digest, reset_evidence

    g = ProcedureGraph.model_validate(_source()["graph"])
    r = reset_evidence(g)
    assert graph_digest(r) == graph_digest(g)  # topology + attributes unchanged
    assert all(n.stats.runs == 0 and not n.fail_prone and not n.dead_end for n in r.nodes)
    assert all(e.weight == 0 and e.ok_weight == 0 and e.fail_weight == 0 for e in r.edges)
    assert g.nodes[1].fail_prone is True  # input untouched
