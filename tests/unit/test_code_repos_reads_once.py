"""A listing reads its ledger once and its grants once.

A cold `code.repos` starts every repository's status together, and each
would read the worker ledger before the first answer lands; under auth each
repository would also pay one `namespace_grants` read inside the
authorization gate, though the listing has read the principal's grants
already. The ledger read is single-flighted and the gate takes the grant the
listing found.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from server.auth import authz
from server.code.app import index_status, repos
from server.operations.base import Role


class _CH:
    def __init__(self) -> None:
        self.reads = 0
        self.gate = asyncio.Event()

    async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
        self.reads += 1
        await self.gate.wait()  # hold every reader until released
        return [
            {
                "worker": "code_ingest",
                "last_started": None,
                "last_finished": None,
                "last_error": "",
                "last_status": "ok",
                "consecutive_failures": 0,
                "total_runs": 3,
            }
        ]


@pytest.mark.asyncio
async def test_concurrent_status_computations_share_one_ledger_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ch = _CH()
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)
    index_status._ledger_passes_cache.clear()
    index_status._ledger_passes_inflight.clear()
    tasks = [asyncio.create_task(index_status._worker_passes_from_ledger()) for _ in range(8)]
    await asyncio.sleep(0)  # every task reaches the read (or the in-flight wait)
    ch.gate.set()
    results = await asyncio.gather(*tasks)
    assert ch.reads == 1, "eight concurrent askers, one read"
    assert all(r == results[0] for r in results) and "code_ingest" in results[0]
    assert index_status._ledger_passes_inflight == {}
    # the memo now answers without a read
    await index_status._worker_passes_from_ledger()
    assert ch.reads == 1


@pytest.mark.asyncio
async def test_a_failed_ledger_read_releases_the_waiters(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Boom:
        async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
            raise RuntimeError("ledger down")

    monkeypatch.setattr("server.core.ch.get_client", lambda: _Boom())
    index_status._ledger_passes_cache.clear()
    index_status._ledger_passes_inflight.clear()
    results = await asyncio.gather(*(index_status._worker_passes_from_ledger() for _ in range(3)))
    assert results == [{}, {}, {}]  # best-effort: empty, never raised
    assert index_status._ledger_passes_inflight == {}


def test_known_grant_kw_hands_the_listing_grant_to_the_gate() -> None:
    roles = {("o", "a"): "writer", ("o", "b"): ""}
    assert repos._known_grant_kw(roles, ("o", "a"), grants_read=True) == {
        "known_grant": Role.WRITER
    }
    assert repos._known_grant_kw(roles, ("o", "b"), grants_read=True) == {"known_grant": None}
    assert repos._known_grant_kw(roles, ("o", "zz"), grants_read=True) == {"known_grant": None}
    assert (
        repos._known_grant_kw(roles, ("o", "a"), grants_read=False) == {}
    )  # the gate reads for itself


@pytest.mark.asyncio
async def test_the_gate_with_a_known_grant_reads_no_grants(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.auth.jwt import AuthInfo

    async def never(*a: Any, **k: Any) -> Role | None:
        raise AssertionError("the gate read grants although the caller handed the answer in")

    monkeypatch.setattr(authz, "_lookup_grant", never)

    async def not_restricted(org: str, ns: str) -> bool:
        return False

    monkeypatch.setattr(authz, "_is_restricted", not_restricted)
    monkeypatch.setattr(authz, "_is_org_readable_code_namespace", not_restricted)
    viewer = AuthInfo(
        subject="v@example.com",
        org="o",
        namespaces=("elsewhere",),
        roles=(Role.VIEWER,),
        is_service_account=False,
    )
    # a live viewer grant on the namespace admits the read without a lookup
    await authz.check_authorized(
        viewer, Role.VIEWER, org="o", namespace="n", known_grant=Role.VIEWER
    )
    # a known absence refuses without a lookup
    with pytest.raises(PermissionError):
        await authz.check_authorized(viewer, Role.VIEWER, org="o", namespace="n", known_grant=None)


def test_the_listing_hands_the_gate_its_grant() -> None:
    src = open(repos.__file__, encoding="utf-8").read()
    # The listing's per-entry loop lives in _code_repo_rows, its gate in _listing_gate
    body = src[src.index("async def _code_repo_rows(") :]
    body = body[: body.index("\nasync def ", 1)]
    assert "grants_read=maintained is not None" in body
    gate = src[src.index("async def _listing_gate(") :]
    gate = gate[: gate.index("\ndef ", 1)]
    assert "**_known_grant_kw(roles, key, grants_read=grants_read)" in gate
