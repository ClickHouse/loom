"""One `high` row lives in a bench namespace so the sensitivity invariant — a
high-sensitivity row has no embedding row — and its gauge
(`loom.integrity.high_with_embedding`) have something to break. Without a
canary a deployment with no `high` rows reads 0 for the wrong reason, and a
reconcile that re-embeds a swept row would go unnoticed.

Runs against a real Control Plane + ClickHouse (LOOM_BASE_URL, LOOM_CH_*).
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Callable
from datetime import UTC, datetime

import httpx
import pytest

from tests.integration._eventually import wait_until
from tests.integration._resources import unique_org


def _wait(predicate: Callable[[], bool], *, timeout: float = 20.0) -> None:
    wait_until(predicate, max_wait=timeout, interval=0.5)


ORG = unique_org("sensitivity")
NAMESPACE = f"bench-sensitivity-canary-{os.urandom(3).hex()}"
# A credential shape the detector (memory/domain/sensitivity.py) labels `high`:
# an OpenAI-style key with a nonce body. Never a real secret.
_CANARY_SECRET = "sk-proj-" + uuid.uuid4().hex + uuid.uuid4().hex[:8]


def _post(base: str, token: str, op: str, payload: dict) -> httpx.Response:
    with httpx.Client(base_url=base, timeout=30.0) as c:
        return c.post(f"/v1/{op}", json=payload, headers={"Authorization": f"Bearer {token}"})


@pytest.fixture(scope="module")
def token(mint_test_token_module):
    return mint_test_token_module(
        org=ORG, namespaces=[NAMESPACE], roles=["admin", "writer"], subject="canary"
    )


@pytest.fixture(scope="module")
def mint_test_token_module():
    from server.auth.jwt import mint_token

    def _mint(*, org: str, namespaces: list[str], roles: list[str], subject: str = "test") -> str:
        return mint_token(
            subject=subject, org=org, namespaces=namespaces, roles=roles, ttl_seconds=600
        )

    return _mint


def test_a_high_row_is_written_without_an_embedding_row_and_the_gauge_reads_zero(
    control_plane_url: str, ch_client, token: str
) -> None:
    key = f"vault_key_{uuid.uuid4().hex[:6]}"
    r = _post(
        control_plane_url,
        token,
        "memory.set",
        {
            "org": ORG,
            "namespace": NAMESPACE,
            "agent": "canary",
            "memory_key": key,
            "value": f"deploy token for the staging vault: {_CANARY_SECRET}",
            "memory_type": "semantic",
        },
    )
    assert r.status_code == 200, r.text
    # the write path detected the shape — the caller set no level
    r = _post(
        control_plane_url,
        token,
        "memory.get",
        {
            "org": ORG,
            "namespace": NAMESPACE,
            "agent": "canary",
            "memory_key": key,
        },
    )
    assert r.status_code == 200 and r.json().get("found") is True, r.text
    assert r.json().get("sensitivity") == "high", r.text[:300]

    def _live_memory() -> int:
        return int(
            ch_client.query(
                "SELECT count() FROM loom.memory FINAL WHERE org = {o:String} AND namespace = {n:String} "
                "AND memory_key = {k:String} AND tombstoned_at IS NULL AND sensitivity = 'high'",
                parameters={"o": ORG, "n": NAMESPACE, "k": key},
            ).result_rows[0][0]
        )

    _wait(lambda: _live_memory() == 1)

    # the invariant: no embedding row, now and after the embedding worker's reconcile pass
    def _embedding_rows() -> int:
        return int(
            ch_client.query(
                "SELECT count() FROM loom.memory_embeddings FINAL WHERE org = {o:String} "
                "AND namespace = {n:String} AND memory_key = {k:String} AND tombstoned_at IS NULL",
                parameters={"o": ORG, "n": NAMESPACE, "k": key},
            ).result_rows[0][0]
        )

    assert _embedding_rows() == 0
    # the gauge, scoped to the namespace, is 0 WITH a high row present to break it
    from server.memory.app.sensitivity_sweep import HIGH_WITH_EMBEDDING_SQL

    gauge = int(
        ch_client.query(
            f"SELECT count() FROM ({HIGH_WITH_EMBEDDING_SQL}) WHERE org = {{o:String}} AND namespace = {{n:String}}",
            parameters={"o": ORG, "n": NAMESPACE},
        ).result_rows[0][0]
    )
    assert gauge == 0
    # no search leg returns it: the row is reachable by key only
    r = _post(
        control_plane_url,
        token,
        "memory.search",
        {
            "org": ORG,
            "namespace": NAMESPACE,
            "agent": "canary",
            "query": "deploy token for the staging vault",
            "top_k": 5,
            "fresh": True,
        },
    )
    assert r.status_code == 200, r.text
    assert _CANARY_SECRET not in r.text and key not in [
        h.get("memory_key") for h in r.json().get("results", [])
    ]

    # the key-only read left its audit row: who read the credential, and when
    def _sensitive_reads() -> int:
        return int(
            ch_client.query(
                "SELECT count() FROM loom.audit_log WHERE org = {o:String} AND namespace = {n:String} "
                "AND event_type = 'memory.sensitive_read'",
                parameters={"o": ORG, "n": NAMESPACE},
            ).result_rows[0][0]
        )

    _wait(lambda: _sensitive_reads() >= 1)


def test_a_read_by_id_that_omits_a_high_value_serves_none_and_audits_none(
    control_plane_url: str, ch_client, token: str
) -> None:
    """The dashboard names a high conflict peer by id with `omit_high_value`: the answer
    carries no value or hash and no sensitive read is audited; a plain read by id
    serves the value and is audited."""
    key = f"vault_peer_{uuid.uuid4().hex[:6]}"
    r = _post(
        control_plane_url,
        token,
        "memory.set",
        {
            "org": ORG,
            "namespace": NAMESPACE,
            "agent": "canary",
            "memory_key": key,
            "value": f"deploy token for the peer vault: {_CANARY_SECRET}",
            "memory_type": "semantic",
        },
    )
    assert r.status_code == 200, r.text
    memory_id = r.json()["memory_id"]

    def _reads_of_row() -> int:
        return int(
            ch_client.query(
                "SELECT count() FROM loom.audit_log WHERE org = {o:String} AND namespace = {n:String}"
                " AND event_type = 'memory.sensitive_read' AND position(subject, {id:String}) > 0",
                parameters={"o": ORG, "n": NAMESPACE, "id": memory_id},
            ).result_rows[0][0]
        )

    by_id = {"org": ORG, "memory_id": memory_id}
    r = _post(control_plane_url, token, "memory.get_by_id", {**by_id, "omit_high_value": True})
    assert r.status_code == 200 and r.json()["found"] is True, r.text
    out = r.json()
    assert out["sensitivity"] == "high" and out["value"] is None and out["content_sha256"] == ""
    assert out["verification"] == "none"  # the row was never verified
    assert _CANARY_SECRET not in r.text
    r = _post(control_plane_url, token, "memory.get_by_id", by_id)
    assert _CANARY_SECRET in r.text
    _wait(lambda: _reads_of_row() >= 1)
    assert _reads_of_row() == 1, "the read that omitted the value is not audited"


@pytest.mark.parametrize(
    ("value", "level", "other_tags"),
    [
        ("My employee badge PIN is 4471", "high", []),
        ("the company card is 4111 1111 1111 1111", "high", []),
        ("her SSN is 123-45-6789", "high", []),
        ("salary goes to GB82 WEST 1234 5698 7654 32", "medium", ["attribute:income"]),
        ({"badge": "front door", "badge_pin": "4471"}, "high", []),
    ],
    ids=["pin", "card", "ssn", "iban", "keyed-pin"],
)
def test_payment_and_identity_numbers_are_labelled_on_write(
    control_plane_url: str, ch_client, token: str, value: object, level: str, other_tags: list
) -> None:
    key = f"pii_{uuid.uuid4().hex[:6]}"
    ident = {"org": ORG, "namespace": NAMESPACE, "agent": "canary"}
    r = _post(control_plane_url, token, "memory.set", {**ident, "memory_key": key, "value": value})
    assert r.status_code == 200, r.text
    # a plain row written after it: once it is embedded, the labelled row's embedding has
    # had its turn
    fence = f"{key}_fence"
    plain = f"the {key} rota changes on mondays"
    r = _post(
        control_plane_url, token, "memory.set", {**ident, "memory_key": fence, "value": plain}
    )
    assert r.status_code == 200, r.text
    r = _post(control_plane_url, token, "memory.get", {**ident, "memory_key": key})
    assert r.status_code == 200 and r.json().get("found") is True, r.text
    got = r.json()
    # every field but the values the server assigns; the retention window a label can stamp
    # is off by default (`memory_sensitive_retention_days` 0)
    assigned = (
        "memory_id",
        "created_at",
        "updated_at",
        "version",
        "version_text",
        "content_sha256",
    )
    assert all(got.get(f) for f in assigned), r.text[:500]
    assert got["version_text"] == str(got["version"])
    got["tags"] = sorted(got.get("tags", []))
    assert {f: v for f, v in got.items() if f not in assigned} == {
        "found": True,
        "unchanged": False,
        "value": value,
        "memory_type": "semantic",
        "confidence": 1.0,
        "expires_at": None,
        "on_expire": "",
        "expire_reason": "",
        "valid_at": None,
        "valid_to": None,
        "temporal_anchor": None,
        "temporal_confidence": 0.0,
        "pinned": False,
        "actor": "process:canary",
        "source_class": "asserted",
        "source_trace": "",
        "source_resource_id": "",
        "agent": "canary",
        "user_id": "-",
        "session_id": "-",
        "status": "active",
        "code": [],
        "superseded_by": "",
        "duplicate_of": "",
        "conflict_verdict": "clear",
        "tags": sorted([*other_tags, f"sensitivity:{level}"]),
        "entities": [],
        "char_limit": None,
        "recall_count": 0,
        "last_recalled_at": None,
        # Served reads are counted only under include_recall_breakdown.
        "served_count": None,
        "last_served_at": None,
        "verified_by": "",
        "verified_at": None,
        "verification": "none",
        "lock_mode": "",
        "lock_reason": "",
        "lock_until": None,
        "locked_by": "",
        "recall_by_agent": [],
        "sensitivity": level,
    }, r.text[:500]
    stored = ch_client.query(
        "SELECT memory_id, org, namespace, agent, user_id, session_id, sensitivity "
        "FROM loom.memory FINAL WHERE org = {o:String} AND namespace = {n:String} "
        "AND memory_key = {k:String} AND tombstoned_at IS NULL",
        parameters={"o": ORG, "n": NAMESPACE, "k": key},
    ).result_rows
    assert [tuple(map(str, row)) for row in stored] == [
        (got["memory_id"], ORG, NAMESPACE, "canary", "-", "-", level)
    ]
    _wait(lambda: _live_vectors(ch_client, fence) == 1)
    if level == "high":
        assert _live_vectors(ch_client, key) == 0
        query = value if isinstance(value, str) else json.dumps(value)
        r = _post(
            control_plane_url, token, "memory.search", {**ident, "query": query, "fresh": True}
        )
        assert r.status_code == 200, r.text
        assert key not in [h.get("memory_key") for h in r.json().get("results", [])]
    else:
        _wait(lambda: _live_vectors(ch_client, key) == 1)


@pytest.mark.parametrize("op", ["memory.set", "memory.bulk_set"])
def test_re_setting_an_embedded_key_as_high_retires_its_old_vector(
    control_plane_url: str, ch_client, token: str, op: str
) -> None:
    key = f"vault_reset_{uuid.uuid4().hex[:6]}"
    ident = {"org": ORG, "namespace": NAMESPACE, "agent": "canary"}
    # Unique per case: a restatement of another case's row would merge into it.
    plain = f"the {op} staging vault {key} is rotated every friday by the platform team"
    r = _post(control_plane_url, token, "memory.set", {**ident, "memory_key": key, "value": plain})
    assert r.status_code == 200, r.text

    def _embedding_rows() -> int:
        return int(
            ch_client.query(
                "SELECT count() FROM loom.memory_embeddings FINAL WHERE org = {o:String} "
                "AND namespace = {n:String} AND memory_key = {k:String} AND tombstoned_at IS NULL",
                parameters={"o": ORG, "n": NAMESPACE, "k": key},
            ).result_rows[0][0]
        )

    _wait(lambda: _embedding_rows() == 1)
    secret = f"the {key} staging vault token is {_CANARY_SECRET}"
    body = {"memory_key": key, "value": secret}
    if op == "memory.set":
        r = _post(control_plane_url, token, op, {**ident, **body})
    else:
        r = _post(control_plane_url, token, op, {**ident, "entries": [body]})
    assert r.status_code == 200, r.text
    _wait(lambda: _embedding_rows() == 0)
    r = _post(
        control_plane_url,
        token,
        "memory.search",
        {**ident, "query": "staging vault rotated friday platform team", "top_k": 5, "fresh": True},
    )
    assert r.status_code == 200, r.text
    assert _CANARY_SECRET not in r.text
    assert key not in [h.get("memory_key") for h in r.json().get("results", [])]


@pytest.mark.parametrize("high_last", [True, False])
def test_one_bulk_batch_with_a_key_twice_keeps_a_vector_only_if_its_last_row_is_embedded(
    control_plane_url: str, ch_client, token: str, high_last: bool
) -> None:
    key = f"vault_dup_{uuid.uuid4().hex[:6]}"
    ident = {"org": ORG, "namespace": NAMESPACE, "agent": "canary"}
    # Unique per case: a restatement of another case's row would merge into it.
    plain = {"memory_key": key, "value": f"the {key} vault is audited each quarter by finance"}
    high = {"memory_key": key, "value": f"the {key} vault token is {_CANARY_SECRET}"}
    entries = [plain, high] if high_last else [high, plain]
    r = _post(control_plane_url, token, "memory.bulk_set", {**ident, "entries": entries})
    assert r.status_code == 200, r.text

    def _sensitivity() -> list[tuple[str]]:
        return ch_client.query(
            "SELECT sensitivity FROM loom.memory FINAL WHERE org = {o:String} "
            "AND namespace = {n:String} AND memory_key = {k:String} AND tombstoned_at IS NULL",
            parameters={"o": ORG, "n": NAMESPACE, "k": key},
        ).result_rows

    _wait(lambda: len(_sensitivity()) == 1)
    assert (_sensitivity()[0][0] == "high") is high_last
    vectors = int(
        ch_client.query(
            "SELECT count() FROM loom.memory_embeddings FINAL WHERE org = {o:String} "
            "AND namespace = {n:String} AND memory_key = {k:String} AND tombstoned_at IS NULL",
            parameters={"o": ORG, "n": NAMESPACE, "k": key},
        ).result_rows[0][0]
    )
    assert vectors == (0 if high_last else 1)
    from server.memory.app.sensitivity_sweep import HIGH_WITH_EMBEDDING_SQL

    gauge = int(
        ch_client.query(
            f"SELECT count() FROM ({HIGH_WITH_EMBEDDING_SQL}) WHERE org = {{o:String}} "
            "AND namespace = {n:String} AND memory_key = {k:String}",
            parameters={"o": ORG, "n": NAMESPACE, "k": key},
        ).result_rows[0][0]
    )
    assert gauge == 0
    r = _post(
        control_plane_url,
        token,
        "memory.search",
        {**ident, "query": "vault audited each quarter by finance", "top_k": 5, "fresh": True},
    )
    assert r.status_code == 200, r.text
    assert _CANARY_SECRET not in r.text
    found = key in [h.get("memory_key") for h in r.json().get("results", [])]
    assert found is not high_last


def _live_vectors(ch_client, key: str) -> int:
    return int(
        ch_client.query(
            "SELECT count() FROM loom.memory_embeddings FINAL WHERE org = {o:String} "
            "AND namespace = {n:String} AND memory_key = {k:String} AND tombstoned_at IS NULL",
            parameters={"o": ORG, "n": NAMESPACE, "k": key},
        ).result_rows[0][0]
    )


def test_rolling_back_to_a_high_version_leaves_no_live_vector(
    control_plane_url: str, ch_client, token: str
) -> None:
    key = f"vault_rollback_{uuid.uuid4().hex[:6]}"
    ident = {"org": ORG, "namespace": NAMESPACE, "agent": "canary", "memory_key": key}
    plain = f"the rollback vault {key} is audited each quarter by the security office"
    secret = f"the rollback vault {key} root token is {_CANARY_SECRET}"
    last = f"the rollback vault {key} moved to the new cluster in march"
    for value in (plain, secret, last):
        r = _post(control_plane_url, token, "memory.set", {**ident, "value": value})
        assert r.status_code == 200, r.text

    def _version(where: str, **params: object) -> int:
        return int(
            ch_client.query(
                "SELECT version FROM loom.memory_versions WHERE org = {o:String} "
                f"AND namespace = {{n:String}} AND memory_key = {{k:String}} AND {where}",
                parameters={"o": ORG, "n": NAMESPACE, "k": key, **params},
            ).result_rows[0][0]
        )

    # An instant after the high version's last row and before the final plain write.
    high_at = _version("sensitivity = 'high' ORDER BY version DESC LIMIT 1")
    next_at = _version("version > {h:UInt64} ORDER BY version LIMIT 1", h=high_at)
    mid_ns = (high_at + next_at) // 2
    as_of = datetime.fromtimestamp(mid_ns / 1e9, tz=UTC).isoformat()
    r = _post(
        control_plane_url,
        token,
        "namespace.rollback",
        {
            "org": ORG,
            "namespace": NAMESPACE,
            "as_of": as_of,
            "dry_run": False,
            "memory_keys": [key],
        },
    )
    assert r.status_code == 200, r.text

    def _head_is_high() -> bool:
        rows = ch_client.query(
            "SELECT sensitivity FROM loom.memory FINAL WHERE org = {o:String} "
            "AND namespace = {n:String} AND memory_key = {k:String} AND tombstoned_at IS NULL",
            parameters={"o": ORG, "n": NAMESPACE, "k": key},
        ).result_rows
        return [s for (s,) in rows] == ["high"]

    _wait(_head_is_high)
    _wait(lambda: _live_vectors(ch_client, key) == 0)
    # No stub row anywhere holds the secret, live or tombstoned.
    leaked = ch_client.query(
        "SELECT count() FROM loom.memory_embeddings WHERE org = {o:String} AND namespace = {n:String} "
        "AND memory_key = {k:String} AND position(content_excerpt, {s:String}) > 0",
        parameters={"o": ORG, "n": NAMESPACE, "k": key, "s": _CANARY_SECRET},
    ).result_rows[0][0]
    assert int(leaked) == 0
    from server.memory.app.sensitivity_sweep import HIGH_WITH_EMBEDDING_SQL

    gauge = ch_client.query(
        f"SELECT count() FROM ({HIGH_WITH_EMBEDDING_SQL}) WHERE org = {{o:String}} AND namespace = {{n:String}}",
        parameters={"o": ORG, "n": NAMESPACE},
    ).result_rows[0][0]
    assert int(gauge) == 0
    r = _post(
        control_plane_url,
        token,
        "memory.search",
        {**ident, "query": "rollback vault root token audited quarter", "top_k": 5, "fresh": True},
    )
    assert r.status_code == 200, r.text
    assert _CANARY_SECRET not in r.text
    assert key not in [h.get("memory_key") for h in r.json().get("results", [])]


def test_retyping_a_key_to_a_non_searchable_type_retires_its_vector(
    control_plane_url: str, ch_client, token: str
) -> None:
    key = f"vault_retype_{uuid.uuid4().hex[:6]}"
    ident = {"org": ORG, "namespace": NAMESPACE, "agent": "canary", "memory_key": key}
    r = _post(
        control_plane_url,
        token,
        "memory.set",
        {**ident, "value": f"the retype vault {key} backs up nightly to cold storage"},
    )
    assert r.status_code == 200, r.text
    _wait(lambda: _live_vectors(ch_client, key) == 1)
    r = _post(
        control_plane_url,
        token,
        "memory.set",
        {
            **ident,
            "value": f"the retype vault {key} backup ran at 02:00 and finished clean",
            "memory_type": "episodic",
        },
    )
    assert r.status_code == 200, r.text
    _wait(lambda: _live_vectors(ch_client, key) == 0)


def test_a_high_value_is_never_mined_into_the_entity_graph(
    control_plane_url: str, ch_client, token: str
) -> None:
    """A point write mines entity nodes from its value; a high row's value would put its
    secret's words into facts.traverse. Only the caller's declared entities project."""
    plain, secret, declared = (f"{k}_{uuid.uuid4().hex[:6]}" for k in ("plain", "vault", "named"))
    for key, extra in (
        (plain, {}),
        (secret, {"sensitivity": "high"}),
        (declared, {"sensitivity": "high", "entities": ["Billing", "Stripe"]}),
    ):
        r = _post(
            control_plane_url,
            token,
            "memory.set",
            {
                "org": ORG,
                "namespace": NAMESPACE,
                "agent": "canary",
                "memory_key": key,
                "value": "Orchid Falcon Quartz Meridian rotate the Vault Key weekly",
                "memory_type": "semantic",
                **extra,
            },
        )
        assert r.status_code == 200, r.text

    def keys() -> set[str]:
        res = ch_client.query(
            "SELECT DISTINCT memory_key FROM loom.entity_edge_memories "
            "WHERE org = {org:String} AND namespace = {ns:String}",
            parameters={"org": ORG, "ns": NAMESPACE},
        )
        return {str(row[0]) for row in res.result_rows}

    _wait(lambda: plain in keys())
    _wait(lambda: declared in keys())
    assert secret not in keys()
