"""memory.import end to end through the Python SDK's generated client
(`agent.client.memory.import_`), as test_memory_bulk.py drives memory.bulk_set: items
written under keys derived from their text and stamped `imported`, re-runs that add no
version, an item its owner deleted or rejected that a re-run leaves so, a proposal left
on trial, a dry run that writes nothing, each refusal named on its entry, categories
linked or noted, a rescinded link left rescinded and a hash-only audit trail. Over raw
HTTP and MCP: the exact 422 past 200 entries, a token bounded to one user, and the tool
a gateway calls with the identity its token carries."""

from __future__ import annotations

import hashlib
import sys
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "sdk" / "python"))
sys.path.insert(0, str(ROOT))

from tests.integration._resources import unique_org  # noqa: E402

ORG = unique_org("mimport")
AGENT = "importer"


def _key(text: str) -> str:
    return "import:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]


def _ns() -> str:
    return f"imp-{uuid.uuid4().hex[:6]}"


def _outcomes(out: dict[str, Any]) -> list[tuple[str, str]]:
    return [(r["outcome"], r.get("reason", "")) for r in out["results"]]


def _entries(*texts: str) -> dict[str, Any]:
    return {"entries": [{"value": t} for t in texts]}


@pytest.mark.asyncio
async def test_each_item_is_written_under_its_text_key_as_imported(
    control_plane_url: str, ch_client
) -> None:
    from loom import Agent

    dog, deploys = "Dog is Biscuit.", "Deploys happen on Tuesday."
    body = {
        "entries": [
            {
                "value": dog,
                "tags": ["pets"],
                "valid_at": "2021-03-04T05:06:07Z",
                "source_trace": "n/7",
            },
            {"value": deploys},
        ]
    }
    ns = _ns()
    async with Agent(org=ORG, namespace=ns, agent=AGENT, base_url=control_plane_url) as a:
        out = await a.client.memory.import_(body)
        got = await a.call("memory.get", memory_key=_key(dog))
    assert out["totals"] == {"written": 2, "held": 0, "skipped": 0, "refused": 0}, out
    assert [r["key"] for r in out["results"]] == [_key(dog), _key(deploys)]
    assert got["found"] is True and got["value"] == dog
    assert got["source_class"] == "imported" and "pets" in got["tags"]
    assert (got["status"], got["memory_type"], got["sensitivity"]) == ("active", "semantic", "")
    assert str(got["valid_at"]).startswith("2021-03-04T05:06:07")
    assert not str(got["created_at"]).startswith("2021-03-04")
    traces = ch_client.query(
        "SELECT memory_key, source_trace FROM loom.memory FINAL "
        "WHERE org = {o:String} AND namespace = {n:String}",
        parameters={"o": ORG, "n": ns},
    ).result_rows
    assert dict(traces) == {_key(dog): "n/7", _key(deploys): _key(deploys)}


@pytest.mark.asyncio
async def test_reruns_write_no_new_version(control_plane_url: str) -> None:
    from loom import Agent

    body = _entries("Dog is Biscuit.", "Deploys happen on Tuesday.")
    async with Agent(org=ORG, namespace=_ns(), agent=AGENT, base_url=control_plane_url) as a:
        assert (await a.client.memory.import_(body))["totals"]["written"] == 2
        for _ in range(3):
            again = await a.client.memory.import_(body)
            assert _outcomes(again) == [("skipped", "unchanged")] * 2, again
        history = await a.call("memory.history", memory_key=_key("Dog is Biscuit."))
    assert len(history["rows"]) == 1, history


@pytest.mark.asyncio
async def test_a_rerun_leaves_an_item_its_owner_deleted_deleted(control_plane_url: str) -> None:
    from loom import Agent

    text = "The staging cluster runs in eu-west-1."
    async with Agent(org=ORG, namespace=_ns(), agent=AGENT, base_url=control_plane_url) as a:
        assert (await a.client.memory.import_(_entries(text)))["totals"]["written"] == 1
        await a.call("memory.delete", memory_key=_key(text))
        again = await a.client.memory.import_(_entries(text))
        got = await a.call("memory.get", memory_key=_key(text))
    assert _outcomes(again) == [("skipped", "deleted")], again
    assert got["found"] is False


@pytest.mark.asyncio
async def test_a_rerun_leaves_a_rejected_item_rejected(control_plane_url: str) -> None:
    from loom import Agent

    text = "The build server is called hal."
    async with Agent(org=ORG, namespace=_ns(), agent=AGENT, base_url=control_plane_url) as a:
        assert (await a.client.memory.import_(_entries(text)))["totals"]["written"] == 1
        await a.call("memory.reject", memory_key=_key(text), rejection_reason="wrong host")
        again = await a.client.memory.import_(_entries(text))
    assert _outcomes(again) == [("skipped", "rejected")], again


@pytest.mark.asyncio
async def test_a_rerun_leaves_an_item_its_owner_corrected_corrected(control_plane_url: str) -> None:
    from loom import Agent

    text = "The on-call rotation changes on Mondays."
    async with Agent(org=ORG, namespace=_ns(), agent=AGENT, base_url=control_plane_url) as a:
        await a.client.memory.import_(_entries(text))
        got = await a.call("memory.get", memory_key=_key(text))
        await a.call(
            "memory.update_by_id",
            memory_id=got["memory_id"],
            value="The on-call rotation changes on Tuesdays.",
        )
        again = await a.client.memory.import_(_entries(text))
        after = await a.call("memory.get", memory_key=_key(text))
    assert _outcomes(again) == [("skipped", "revised")], again
    assert after["value"] == "The on-call rotation changes on Tuesdays."


@pytest.mark.asyncio
async def test_a_rerun_naming_another_sensitivity_keeps_the_stored_level(
    control_plane_url: str,
) -> None:
    from loom import Agent

    text = "The team offsite is in Lisbon."
    item = {"value": text, "sensitivity": "low"}
    async with Agent(org=ORG, namespace=_ns(), agent=AGENT, base_url=control_plane_url) as a:
        await a.client.memory.import_({"entries": [item]})
        same = await a.client.memory.import_({"entries": [item]})
        other = await a.client.memory.import_({"entries": [{**item, "sensitivity": "medium"}]})
        got = await a.call("memory.get", memory_key=_key(text))
    assert _outcomes(same) == [("skipped", "unchanged")], same
    assert _outcomes(other) == [("skipped", "sensitivity_kept")], other
    assert got["sensitivity"] == "low"


@pytest.mark.asyncio
async def test_an_item_on_trial_is_left_proposed(control_plane_url: str) -> None:
    from loom import Agent

    text = "Code freeze starts on the 20th."
    async with Agent(org=ORG, namespace=_ns(), agent=AGENT, base_url=control_plane_url) as a:
        await a.call("memory.set", memory_key=_key(text), value=text, proposed=True)
        out = await a.client.memory.import_(_entries(text))
    assert _outcomes(out) == [("skipped", "proposed")], out


@pytest.mark.asyncio
async def test_a_dry_run_writes_nothing(control_plane_url: str, ch_client) -> None:
    from loom import Agent

    ns = _ns()
    value = "The staging cluster runs in us-east-2."
    body = {"entries": [{"value": value, "category": "infra"}], "dry_run": True}
    async with Agent(org=ORG, namespace=ns, agent=AGENT, base_url=control_plane_url) as a:
        await a.call("namespace.register_category", name="infra", description="Hosts.")
        out = await a.client.memory.import_(body)
    assert out["dry_run"] is True and _outcomes(out) == [("written", "")], out
    for table in ("memory", "memory_embeddings", "memory_categories", "audit_log"):
        rows = ch_client.query(
            f"SELECT count() FROM loom.{table} WHERE org = {{o:String}} AND namespace = {{n:String}}"
            + (" AND event_type LIKE 'memory.%'" if table == "audit_log" else ""),
            parameters={"o": ORG, "n": ns},
        ).result_rows
        assert rows[0][0] == 0, table


@pytest.mark.asyncio
async def test_a_rerun_from_another_session_writes_nothing(control_plane_url: str) -> None:
    from loom import Agent

    text = "Backups run nightly at 02:00."
    async with Agent(org=ORG, namespace=_ns(), agent=AGENT, base_url=control_plane_url) as a:
        first = await a.call("memory.import", _entries(text), session_id="s1")
        again = await a.call("memory.import", _entries(text), session_id="s2")
        history = await a.call("memory.history", memory_key=_key(text), session_id="s1")
    assert _outcomes(first) == [("written", "")], first
    assert _outcomes(again) == [("skipped", "unchanged")], again
    assert len(history["rows"]) == 1, history


@pytest.mark.asyncio
async def test_each_refusal_and_held_entry_is_named_on_its_entry(
    control_plane_url: str,
) -> None:
    from loom import Agent

    plain, secret = "The office plant is a ficus.", "The launch code is zq-1."
    pinned, kept = "The release train leaves on Mondays.", "The wiki lives at docs.example.test."
    async with Agent(org=ORG, namespace=_ns(), agent=AGENT, base_url=control_plane_url) as a:
        await a.call("blacklist.add", pattern="launch code")
        # The key the pinned item's text derives already holds other pinned text: revised.
        await a.call("memory.pin", memory_key=_key(pinned), value="Trains leave daily.")
        # The same text is stored under another key of the identity.
        await a.call("memory.set", memory_key="wiki_location", value=kept)
        out = await a.client.memory.import_(_entries(plain, secret, pinned, kept))
        refused = await a.call("memory.get", memory_key=_key(secret))
        revised = await a.call("memory.get", memory_key=_key(pinned))
    assert _outcomes(out) == [
        ("written", ""),
        ("refused", "blacklist"),
        ("skipped", "revised"),
        ("held", "identity_duplicate"),
    ], out
    assert out["totals"] == {"written": 1, "held": 1, "skipped": 1, "refused": 1}
    assert refused["found"] is False and revised["value"] == "Trains leave daily."


@pytest.mark.asyncio
async def test_a_pinned_memorys_own_text_is_held_by_it_and_competes_with_nothing(
    control_plane_url: str,
) -> None:
    """The pinned memory's exact text, imported by the same identity: identity dedup
    matches it by content hash before any embedding is compared, so in every
    environment the entry is held by the pinned row, nothing lands under the import key,
    and the pin and the value stand. A merely close text is judged by the conflict
    check's similarity, which an embedder decides; the unit tests script that verdict."""
    from loom import Agent

    text = "Production deploys happen on Tuesday and Thursday, never on Friday."
    async with Agent(org=ORG, namespace=_ns(), agent=AGENT, base_url=control_plane_url) as a:
        await a.call("memory.pin", memory_key="deploy_policy", value=text)
        out = await a.client.memory.import_(_entries(text))
        pinned = await a.call("memory.get", memory_key="deploy_policy")
        imported = await a.call("memory.get", memory_key=_key(text))
    assert _outcomes(out) == [("held", "identity_duplicate")], out
    assert out["results"][0]["held_by"] == "deploy_policy"
    assert imported["found"] is False
    assert (pinned["value"], pinned["pinned"]) == (text, True)


@pytest.mark.asyncio
async def test_a_registered_category_is_linked_and_an_unregistered_one_noted(
    control_plane_url: str, ch_client
) -> None:
    from loom import Agent

    ns = _ns()
    dog, cat = "Dog is Biscuit.", "Cat is Miso."
    body = {"entries": [{"value": dog, "category": "pets"}, {"value": cat, "category": "unknown"}]}
    async with Agent(org=ORG, namespace=ns, agent=AGENT, base_url=control_plane_url) as a:
        await a.call("namespace.register_category", name="pets", description="Animals at home.")
        out = await a.client.memory.import_(body)
    assert [(r["outcome"], r.get("note", "")) for r in out["results"]] == [
        ("written", ""),
        ("written", "category_unregistered"),
    ], out
    rows = ch_client.query(
        "SELECT memory_key FROM loom.memory_categories FINAL "
        "WHERE org = {o:String} AND namespace = {n:String} AND tombstoned_at IS NULL",
        parameters={"o": ORG, "n": ns},
    ).result_rows
    assert [r[0] for r in rows] == [_key(dog)]


@pytest.mark.asyncio
async def test_a_rerun_leaves_a_rescinded_category_link_rescinded(
    control_plane_url: str, ch_client
) -> None:
    from loom import Agent

    ns = _ns()
    body = {"entries": [{"value": "Dog is Biscuit.", "category": "pets"}]}
    async with Agent(org=ORG, namespace=ns, agent=AGENT, base_url=control_plane_url) as a:
        await a.call("namespace.register_category", name="pets", description="Animals at home.")
        await a.client.memory.import_(body)
        await a.call(
            "memory.assign_category",
            memory_key=_key("Dog is Biscuit."),
            category_name="pets",
            tombstone=True,
        )
        again = await a.client.memory.import_(body)
    assert [(r["outcome"], r["note"]) for r in again["results"]] == [
        ("skipped", "category_rescinded")
    ], again
    live = ch_client.query(
        "SELECT count() FROM loom.memory_categories FINAL "
        "WHERE org = {o:String} AND namespace = {n:String} AND tombstoned_at IS NULL",
        parameters={"o": ORG, "n": ns},
    ).result_rows
    assert live[0][0] == 0


@pytest.mark.asyncio
async def test_a_token_bounded_to_one_user_links_no_category(
    control_plane_url: str, ch_client, mint_test_token
) -> None:
    from loom import Agent

    ns = _ns()
    async with Agent(org=ORG, namespace=ns, agent=AGENT, base_url=control_plane_url) as a:
        await a.call("namespace.register_category", name="pets", description="Animals at home.")
    token = mint_test_token(org=ORG, namespaces=[ns], subject="importer", read_user_id="u1")
    body = {
        "org": ORG,
        "namespace": ns,
        "agent": AGENT,
        "user_id": "u1",
        "entries": [{"value": "Dog is Biscuit.", "category": "pets"}],
    }
    async with httpx.AsyncClient(base_url=control_plane_url, timeout=60.0) as c:
        r = await c.post(
            "/v1/memory.import", json=body, headers={"Authorization": f"Bearer {token}"}
        )
    assert r.status_code == 200, r.text[:400]
    assert [(x["outcome"], x["note"]) for x in r.json()["results"]] == [
        ("written", "category_refused")
    ]
    links = ch_client.query(
        "SELECT count() FROM loom.memory_categories "
        "WHERE org = {o:String} AND namespace = {n:String}",
        parameters={"o": ORG, "n": ns},
    ).result_rows
    assert links[0][0] == 0


@pytest.mark.asyncio
async def test_the_audit_trail_holds_keys_and_hashes_never_the_text(
    control_plane_url: str, ch_client
) -> None:
    from loom import Agent

    ns = _ns()
    secret = f"the vault phrase is {uuid.uuid4().hex}"
    async with Agent(org=ORG, namespace=ns, agent=AGENT, base_url=control_plane_url) as a:
        await a.call("namespace.register_category", name="secrets", description="Private notes.")
        out = await a.client.memory.import_({"entries": [{"value": secret, "category": "secrets"}]})
    assert out["totals"]["written"] == 1, out
    rows = ch_client.query(
        "SELECT event_type, subject FROM loom.audit_log "
        "WHERE org = {o:String} AND namespace = {n:String} "
        "AND event_type IN ('memory.bulk_set', 'memory.import')",
        parameters={"o": ORG, "n": ns},
    ).result_rows
    subjects = dict(rows)
    bulk = subjects["memory.bulk_set"]
    assert _key(secret) in bulk and "content_sha256" in bulk
    assert _key(secret) in subjects["memory.import"]
    assert all(secret not in s for s in subjects.values())


def test_the_gateway_calls_the_mcp_tool_with_the_identity_its_token_carries(
    control_plane_url: str, mint_test_token
) -> None:
    ns = _ns()
    token = mint_test_token(org=ORG, namespaces=[ns], roles=["writer"], subject="gateway")
    value = "Lunch is at noon on Fridays."
    rpc = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "loom_memory_import", "arguments": {"namespace": ns, **_entries(value)}},
    }
    with httpx.Client(base_url=control_plane_url, timeout=60.0) as c:
        r = c.post(
            "/mcp",
            json=rpc,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )
    assert r.status_code == 200, r.text[:400]
    result = r.json()["result"]
    assert not result.get("isError"), result
    sc = result["structuredContent"]
    assert sc["totals"]["written"] == 1 and sc["results"][0]["key"] == _key(value), sc


def test_more_than_200_entries_are_refused(control_plane_url: str, mint_test_token) -> None:
    ns = _ns()
    token = mint_test_token(org=ORG, namespaces=[ns], roles=["writer"], subject="importer")
    body = {
        "org": ORG,
        "namespace": ns,
        "agent": AGENT,
        "entries": [{"value": f"fact {i}"} for i in range(201)],
    }
    with httpx.Client(base_url=control_plane_url, timeout=60.0) as c:
        r = c.post("/v1/memory.import", json=body, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 422, r.text[:400]
    errors = r.json()["detail"]["errors"]
    assert [(e["loc"], e["type"]) for e in errors] == [(["entries"], "too_long")]
