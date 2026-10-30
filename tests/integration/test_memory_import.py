"""memory.import against a running server: items written under keys their text
derives and stamped `imported`, a re-run that writes nothing new, a dry run that writes
nothing, each refusal named on its entry, categories linked or noted, a hash-only audit
trail, and the MCP tool a gateway calls with the identity its token carries."""

from __future__ import annotations

import hashlib
import sys
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tests.integration._resources import unique_org  # noqa: E402

AGENT = "importer"


def _key(text: str) -> str:
    return "import:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]


def _post(base: str, token: str, op: str, payload: dict[str, Any]) -> httpx.Response:
    with httpx.Client(base_url=base, timeout=60.0) as c:
        return c.post(f"/v1/{op}", json=payload, headers={"Authorization": f"Bearer {token}"})


@pytest.fixture
def ctx(control_plane_url: str, mint_test_token) -> dict[str, str]:
    org = unique_org("mimport")
    ns = f"imp-{uuid.uuid4().hex[:6]}"
    token = mint_test_token(org=org, namespaces=[ns], roles=["writer"], subject="importer")
    return {"base": control_plane_url, "org": org, "ns": ns, "token": token}


def _call(ctx: dict[str, str], op: str, **payload: Any) -> dict[str, Any]:
    body = {"org": ctx["org"], "namespace": ctx["ns"], **payload}
    r = _post(ctx["base"], ctx["token"], op, body)
    assert r.status_code == 200, f"{op}: {r.status_code} {r.text[:400]}"
    return dict(r.json())


def _import(ctx: dict[str, str], entries: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    return _call(ctx, "memory.import", agent=AGENT, entries=entries, **extra)


def _get(ctx: dict[str, str], key: str) -> dict[str, Any]:
    return _call(ctx, "memory.get", agent=AGENT, memory_key=key)


def _outcomes(out: dict[str, Any]) -> list[tuple[str, str]]:
    return [(r["outcome"], r.get("reason", "")) for r in out["results"]]


def test_each_item_is_written_under_its_text_key_as_imported(ctx: dict[str, str]) -> None:
    dog, deploys = "Dog is Biscuit.", "Deploys happen on Tuesday."
    out = _import(
        ctx,
        [
            {
                "value": dog,
                "tags": ["pets"],
                "valid_at": "2021-03-04T05:06:07Z",
                "source_trace": "n/7",
            },
            {"value": deploys},
        ],
    )
    assert out["totals"] == {"written": 2, "reinforced": 0, "skipped": 0, "refused": 0}, out
    assert [r["key"] for r in out["results"]] == [_key(dog), _key(deploys)]
    got = _get(ctx, _key(dog))
    assert got["found"] is True and got["value"] == dog
    assert got["source_class"] == "imported" and "pets" in got["tags"]
    assert str(got["valid_at"]).startswith("2021-03-04T05:06:07")
    assert not str(got["created_at"]).startswith("2021-03-04")


def test_a_rerun_writes_nothing_new(ctx: dict[str, str]) -> None:
    items = [{"value": "Dog is Biscuit."}, {"value": "Deploys happen on Tuesday."}]
    assert _import(ctx, items)["totals"]["written"] == 2
    again = _import(ctx, items)
    assert _outcomes(again) == [("skipped", "unchanged"), ("skipped", "unchanged")], again
    history = _call(ctx, "memory.history", agent=AGENT, memory_key=_key("Dog is Biscuit."))
    assert len(history["rows"]) == 1, history


def test_a_dry_run_writes_nothing(ctx: dict[str, str]) -> None:
    value = "The staging cluster runs in eu-west-1."
    out = _import(ctx, [{"value": value}], dry_run=True)
    assert out["dry_run"] is True and _outcomes(out) == [("written", "")], out
    assert _get(ctx, _key(value))["found"] is False


def test_each_refusal_and_reinforcement_is_named_on_its_entry(ctx: dict[str, str]) -> None:
    plain, secret = "The office plant is a ficus.", "The launch code is zq-1."
    pinned, kept = "The release train leaves on Mondays.", "The wiki lives at docs.example.test."
    _call(ctx, "blacklist.add", pattern="launch code")
    # The key the pinned item's text derives already holds another pinned value.
    _call(ctx, "memory.pin", agent=AGENT, memory_key=_key(pinned), value="Trains leave daily.")
    # The same text is stored under another key of the identity.
    _call(ctx, "memory.set", agent=AGENT, memory_key="wiki_location", value=kept)
    out = _import(ctx, [{"value": plain}, {"value": secret}, {"value": pinned}, {"value": kept}])
    assert _outcomes(out) == [
        ("written", ""),
        ("refused", "blacklist"),
        ("refused", "pinned"),
        ("reinforced", "identity_duplicate"),
    ], out
    assert out["totals"] == {"written": 1, "reinforced": 1, "skipped": 0, "refused": 2}
    assert _get(ctx, _key(secret))["found"] is False
    assert _get(ctx, _key(pinned))["value"] == "Trains leave daily."


def test_a_close_item_against_a_pinned_memory_is_not_imported_silently(
    ctx: dict[str, str],
) -> None:
    text = "Production deploys happen on Tuesday and Thursday, never on Friday."
    _call(ctx, "memory.pin", agent=AGENT, memory_key="deploy_policy", value=text)
    close = "Production deploys happen on Tuesday and Thursday, never on Friday afternoons."
    out = _import(ctx, [{"value": close}])
    # The pinned short-circuit refuses it, or, if the embedder rates the pair above the
    # duplicate threshold, it lands flagged as a duplicate: never an unchecked insert.
    assert _outcomes(out) in ([("refused", "conflict")], [("written", "duplicate")]), out


def test_a_registered_category_is_linked_and_an_unregistered_one_noted(
    ctx: dict[str, str], ch_client
) -> None:
    _call(ctx, "namespace.register_category", name="pets", description="Animals at home.")
    dog, cat = "Dog is Biscuit.", "Cat is Miso."
    out = _import(
        ctx, [{"value": dog, "category": "pets"}, {"value": cat, "category": "unregistered"}]
    )
    assert [(r["outcome"], r.get("note", "")) for r in out["results"]] == [
        ("written", ""),
        ("written", "category_unregistered"),
    ], out
    rows = ch_client.query(
        "SELECT memory_key FROM loom.memory_categories FINAL "
        "WHERE org = {o:String} AND namespace = {n:String} AND tombstoned_at IS NULL",
        parameters={"o": ctx["org"], "n": ctx["ns"]},
    ).result_rows
    assert [r[0] for r in rows] == [_key(dog)]


def test_the_audit_trail_holds_keys_and_hashes_never_the_text(
    ctx: dict[str, str], ch_client
) -> None:
    _call(ctx, "namespace.register_category", name="secrets", description="Private notes.")
    secret = f"the vault phrase is {uuid.uuid4().hex}"
    out = _import(ctx, [{"value": secret, "category": "secrets"}])
    assert out["totals"]["written"] == 1, out
    rows = ch_client.query(
        "SELECT event_type, subject FROM loom.audit_log "
        "WHERE org = {o:String} AND namespace = {n:String} "
        "AND event_type IN ('memory.bulk_set', 'memory.import')",
        parameters={"o": ctx["org"], "n": ctx["ns"]},
    ).result_rows
    subjects = dict(rows)
    assert (
        _key(secret) in subjects["memory.bulk_set"]
        and "content_sha256" in subjects["memory.bulk_set"]
    )
    assert _key(secret) in subjects["memory.import"]
    assert all(secret not in s for s in subjects.values())


def test_the_gateway_calls_the_mcp_tool_with_the_identity_its_token_carries(
    ctx: dict[str, str],
) -> None:
    value = "Lunch is at noon on Fridays."
    rpc = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "loom_memory_import",
            "arguments": {"namespace": ctx["ns"], "entries": [{"value": value}]},
        },
    }
    with httpx.Client(base_url=ctx["base"], timeout=60.0) as c:
        r = c.post(
            "/mcp",
            json=rpc,
            headers={"Authorization": f"Bearer {ctx['token']}", "Accept": "application/json"},
        )
    assert r.status_code == 200, r.text[:400]
    result = r.json()["result"]
    assert not result.get("isError"), result
    sc = result["structuredContent"]
    assert sc["totals"]["written"] == 1 and sc["results"][0]["key"] == _key(value), sc


def test_more_than_200_entries_are_refused(ctx: dict[str, str]) -> None:
    body = {
        "org": ctx["org"],
        "namespace": ctx["ns"],
        "agent": AGENT,
        "entries": [{"value": f"fact {i}"} for i in range(201)],
    }
    r = _post(ctx["base"], ctx["token"], "memory.import", body)
    assert r.status_code == 422, r.text[:400]
