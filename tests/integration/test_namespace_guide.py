"""A namespace's guide against a running server: revisions written and read back,
a stale save refused with the guide to merge into, the history, who may write
it, and the guide in namespace.get, namespace.browse and the session-start pack."""

from __future__ import annotations

import uuid
from typing import Any

import httpx

from tests.integration._resources import unique_org


def _post(base: str, token: str, op: str, body: dict[str, Any]) -> httpx.Response:
    with httpx.Client(base_url=base, timeout=60.0) as c:
        return c.post(f"/v1/{op}", json=body, headers={"Authorization": f"Bearer {token}"})


def _ok(base: str, token: str, op: str, body: dict[str, Any]) -> dict[str, Any]:
    r = _post(base, token, op, body)
    assert r.status_code == 200, f"{op}: {r.status_code} {r.text[:300]}"
    return dict(r.json())


def test_agents_edit_a_guide_by_revision_and_find_it(
    control_plane_url: str, mint_test_token
) -> None:
    org, ns = unique_org("guide"), f"guide-{uuid.uuid4().hex[:8]}"
    writer = mint_test_token(org=org, namespaces=[ns], roles=["writer"], subject="agent-a")
    viewer = mint_test_token(org=org, namespaces=[ns], roles=["viewer"], subject="reader")
    base = {"org": org, "namespace": ns}

    def save(token: str, content: str, expected: int, **kw: Any) -> dict[str, Any]:
        body = {**base, "content": content, "expected_revision": expected, **kw}
        return _ok(control_plane_url, token, "namespace.guide_set", body)

    _ok(
        control_plane_url,
        writer,
        "namespace.register",
        {**base, "owner_email": "agent-a", "visibility": "public"},
    )
    assert _ok(control_plane_url, viewer, "namespace.guide_get", base)["found"] is False

    guide = "# Plans\nKeys: acct/<id>/plan. Tag `stale`: superseded, ignore it."
    first = save(writer, guide, 0, summary="Per-account plan facts", note="first guide")
    assert (first["written"], first["revision"]) == (True, 1)

    # Two agents edit from revision 1: the second is refused with the first's text.
    a = save(writer, guide + "\nTag `tier:N`: escalation level.", 1)
    b = save(writer, guide + "\nLook up by key before searching.", 1)
    assert (a["written"], a["revision"]) == (True, 2)
    assert (b["written"], b["conflict"], b["revision"]) == (False, True, 2)
    assert "tier:N" in b["current"]["content"]
    merged = guide + "\nTag `tier:N`: escalation level.\nLook up by key before searching."
    assert save(writer, merged, b["revision"])["revision"] == 3
    # A retry of the save that landed writes nothing.
    again = save(writer, merged, 2)
    assert (again["written"], again["conflict"], again["revision"]) == (False, False, 3)

    # Saves that left the summary out kept the first one.
    read = _ok(control_plane_url, viewer, "namespace.guide_get", base)
    assert read.pop("updated_at")
    assert read == {
        "found": True,
        "revision": 3,
        "content": merged,
        "summary": "Per-account plan facts",
        "note": "",
        "updated_by": "process:agent-a",
    }
    assert (
        _ok(control_plane_url, viewer, "namespace.guide_get", {**base, "revision": 1})["content"]
        == guide
    )
    history = _ok(control_plane_url, viewer, "namespace.guide_history", base)["revisions"]
    assert [r["revision"] for r in history] == [3, 2, 1]
    assert history[-1]["note"] == "first guide"

    # A viewer reads the guide but cannot write it.
    refused = _post(
        control_plane_url,
        viewer,
        "namespace.guide_set",
        {**base, "content": "x", "expected_revision": 3},
    )
    assert refused.status_code == 403

    got = _ok(control_plane_url, viewer, "namespace.get", base)
    assert (got["guide_revision"], got["guide_summary"]) == (3, "Per-account plan facts")

    browse = _ok(control_plane_url, viewer, "namespace.browse", {"org": org, "query": "plan facts"})
    [row] = [r for r in browse["rows"] if r["namespace"] == ns]
    assert (row["guide_revision"], row["guide_summary"]) == (3, "Per-account plan facts")

    pack = _ok(
        control_plane_url,
        writer,
        "context.pack",
        {**base, "agent": "a", "mode": "startup", "only_sections": ["namespace_guide"]},
    )
    section = pack["sections"]["namespace_guide"]
    assert section.startswith("- [guide/3 by=")
    assert "tier:N" in section


def test_another_org_reads_no_guide(control_plane_url: str, mint_test_token) -> None:
    org, ns = unique_org("guide"), f"guide-{uuid.uuid4().hex[:8]}"
    writer = mint_test_token(org=org, namespaces=[ns], roles=["writer"], subject="agent-a")
    _ok(
        control_plane_url,
        writer,
        "namespace.register",
        {"org": org, "namespace": ns, "owner_email": "agent-a"},
    )
    body = {"org": org, "namespace": ns, "content": "secret layout", "expected_revision": 0}
    _ok(control_plane_url, writer, "namespace.guide_set", body)

    other_org = unique_org("guide-other")
    outsider = mint_test_token(org=other_org, namespaces=[ns], roles=["viewer"], subject="x")
    r = _post(control_plane_url, outsider, "namespace.guide_get", {"org": org, "namespace": ns})
    assert r.status_code == 403
    assert "secret layout" not in r.text


def test_a_member_of_another_namespace_reads_no_private_guide(
    control_plane_url: str, mint_test_token
) -> None:
    org, ns, other = unique_org("guide"), f"guide-{uuid.uuid4().hex[:8]}", "elsewhere"
    writer = mint_test_token(org=org, namespaces=[ns], roles=["writer"], subject="agent-a")
    outsider = mint_test_token(org=org, namespaces=[other], roles=["writer"], subject="b")
    base = {"org": org, "namespace": ns}
    _ok(control_plane_url, writer, "namespace.register", {**base, "owner_email": "agent-a"})
    body = {**base, "content": "private layout", "summary": "private", "expected_revision": 0}
    _ok(control_plane_url, writer, "namespace.guide_set", body)

    for op in ("namespace.guide_get", "namespace.guide_history"):
        r = _post(control_plane_url, outsider, op, base)
        assert r.status_code == 403, (op, r.status_code)
        assert "private layout" not in r.text
    browse = _ok(control_plane_url, outsider, "namespace.browse", {"org": org, "query": "layout"})
    assert ns not in [row["namespace"] for row in browse["rows"]]


def test_a_namespace_erasure_deletes_every_revision_and_audits_no_text(
    control_plane_url: str, mint_test_token
) -> None:
    org, ns = unique_org("guide"), f"guide-{uuid.uuid4().hex[:8]}"
    admin = mint_test_token(org=org, namespaces=[ns], roles=["admin"], subject="agent-a")
    base = {"org": org, "namespace": ns}
    _ok(control_plane_url, admin, "namespace.register", {**base, "owner_email": "agent-a"})
    for rev, text in enumerate(("first canary-7f3a", "second canary-7f3a")):
        body = {**base, "content": text, "expected_revision": rev}
        _ok(control_plane_url, admin, "namespace.guide_set", body)

    erased = _ok(
        control_plane_url,
        admin,
        "erasure.request",
        {"org": org, "scope_type": "namespace", "scope_value": ns, "requester": "ops"},
    )
    assert erased["status"] == "completed", erased
    steps = {s["step"]: s for s in erased["steps"]}
    assert steps["namespace_guides.delete"]["rows_affected"] == 2
    assert _ok(control_plane_url, admin, "namespace.guide_get", base)["revision"] == 0
    assert _ok(control_plane_url, admin, "namespace.guide_history", base)["revisions"] == []
    audit = _ok(control_plane_url, admin, "audit.list", {**base, "limit": 100})
    saves = [r for r in audit["rows"] if r["event_type"] == "namespace.guide_set"]
    assert len(saves) == 2
    assert "canary-7f3a" not in str(audit)


def test_a_public_guide_is_summarised_to_the_org_and_read_by_members(
    control_plane_url: str, mint_test_token
) -> None:
    org, ns = unique_org("guide"), f"guide-{uuid.uuid4().hex[:8]}"
    writer = mint_test_token(org=org, namespaces=[ns], roles=["writer"], subject="agent-a")
    outsider = mint_test_token(org=org, namespaces=["elsewhere"], roles=["writer"], subject="b")
    base = {"org": org, "namespace": ns}
    register = {**base, "owner_email": "agent-a", "visibility": "public"}
    _ok(control_plane_url, writer, "namespace.register", register)
    body = {**base, "content": "keys: acct/<id>", "summary": "plans", "expected_revision": 0}
    _ok(control_plane_url, writer, "namespace.guide_set", body)

    browse = _ok(control_plane_url, outsider, "namespace.browse", {"org": org})
    [row] = [r for r in browse["rows"] if r["namespace"] == ns]
    assert (row["guide_revision"], row["guide_summary"]) == (1, "plans")
    refused = _post(control_plane_url, outsider, "namespace.guide_get", base)
    assert refused.status_code == 403 and "acct/" not in refused.text
    # The directory's search matches a guide's summary, never its text.
    hits = _ok(control_plane_url, outsider, "namespace.browse", {"org": org, "query": "acct/"})
    assert ns not in [r["namespace"] for r in hits["rows"]]


def test_of_saves_made_at_once_from_one_revision_exactly_one_lands(
    control_plane_url: str, mint_test_token
) -> None:
    import asyncio

    org, ns = unique_org("guide"), f"guide-{uuid.uuid4().hex[:8]}"
    writer = mint_test_token(org=org, namespaces=[ns], roles=["writer"], subject="agent-a")
    base = {"org": org, "namespace": ns}
    _ok(control_plane_url, writer, "namespace.register", {**base, "owner_email": "agent-a"})

    async def save(i: int) -> dict[str, Any]:
        body = {**base, "content": f"draft {i}", "expected_revision": 0}
        async with httpx.AsyncClient(base_url=control_plane_url, timeout=60.0) as c:
            r = await c.post(
                "/v1/namespace.guide_set",
                json=body,
                headers={"Authorization": f"Bearer {writer}"},
            )
        # A save that waited past the claim's wait answers 503 OVERLOADED: it wrote nothing.
        assert r.status_code in (200, 503), r.text[:300]
        return dict(r.json()) if r.status_code == 200 else {"written": False, "conflict": True}

    async def all_at_once() -> list[dict[str, Any]]:
        return list(await asyncio.gather(*(save(i) for i in range(6))))

    results = asyncio.run(all_at_once())
    assert sum(r["written"] for r in results) == 1
    assert all(r["conflict"] for r in results if not r["written"])
    history = _ok(control_plane_url, writer, "namespace.guide_history", base)["revisions"]
    assert [r["revision"] for r in history] == [1]


def test_a_restricted_row_listed_in_the_directory_gives_no_summary_away(
    control_plane_url: str, ch_client, mint_test_token
) -> None:
    from datetime import UTC, datetime

    org, ns = unique_org("guide"), f"code-guide-{uuid.uuid4().hex[:8]}"
    canary = f"zqguide{uuid.uuid4().hex[:8]}"
    now = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
    # Written before any request names the org, so the server's first snapshot of
    # its restrictions has the row. Public on purpose: the search must hold even
    # where the row says public.
    ch_client.insert(
        "loom.namespaces",
        [
            [
                org,
                ns,
                "ops@guide.invalid",
                "",
                True,
                "code",
                "public",
                "restricted",
                "approval",
                now,
                now,
            ]
        ],
        column_names=[
            "org",
            "namespace",
            "owner_email",
            "description",
            "active",
            "kind",
            "visibility",
            "classification",
            "join_policy",
            "created_at",
            "updated_at",
        ],
    )
    ch_client.insert(
        "loom.namespace_guides",
        [[org, ns, 1, "the layout", f"summary {canary}", "", "process:ops", now]],
        column_names=[
            "org",
            "namespace",
            "revision",
            "content",
            "summary",
            "note",
            "updated_by",
            "updated_at",
        ],
    )
    reader = mint_test_token(org=org, namespaces=["elsewhere"], roles=["writer"], subject="r")

    listed = _ok(control_plane_url, reader, "namespace.browse", {"org": org})
    [row] = [r for r in listed["rows"] if r["namespace"] == ns]
    assert (row["guide_revision"], row["guide_summary"]) == (0, "")
    found = _ok(control_plane_url, reader, "namespace.browse", {"org": org, "query": canary})
    assert found["rows"] == []
    assert canary not in str(listed)
