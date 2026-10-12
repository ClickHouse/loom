"""One `high` row lives in a bench namespace so the sensitivity invariant — a
high-sensitivity row has no embedding row — and its gauge
(`loom.integrity.high_with_embedding`) have something to break. Without a
canary a deployment with no `high` rows reads 0 for the wrong reason, and a
reconcile that re-embeds a swept row would go unnoticed.

Runs against a real Control Plane + ClickHouse (LOOM_BASE_URL, LOOM_CH_*).
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable

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
