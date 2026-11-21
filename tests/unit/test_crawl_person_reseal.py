"""Raw canonical-person capsule history remains readable after root rotation."""

from __future__ import annotations

import copy
from contextlib import asynccontextmanager
from typing import Any

import pytest

from server.auth import person_erasure as person
from server.auth import scim
from server.core.errors import BackendUnavailableError
from server.settings import settings

pytestmark = pytest.mark.asyncio


class _Rows:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.rows: list[dict[str, Any]] = []
        self.held = False
        self.ignore_mutation = False
        self.mutations = 0
        monkeypatch.setattr(person, "get_client", lambda: self)
        monkeypatch.setattr(scim.leases, "holding", self.holding)

    @asynccontextmanager
    async def holding(self, key: str, *, ttl_s: float, wait_s: float, settle_writes: bool):
        assert key == "scim-user:managed-person" and ttl_s > wait_s >= 0
        assert settle_writes is True
        assert not self.held
        self.held = True
        try:
            yield True
        finally:
            self.held = False

    async def query_all(self, sql: str, *, params: dict[str, Any]):
        if sql.startswith("SELECT DISTINCT id "):
            assert "FINAL" not in sql and params["limit"] > 0
            return [{"id": "managed-person"}] if params["after"] < "managed-person" else []
        assert sql.startswith("SELECT DISTINCT crawl_erasure_enc ")
        assert "FINAL" not in sql and self.held and params["id"] == "managed-person"
        return [
            {"crawl_erasure_enc": value}
            for value in sorted({r["crawl_erasure_enc"] for r in self.rows})
        ]

    async def execute(self, sql: str, *, params: dict[str, Any], settings: dict[str, int]):
        assert self.held and settings == {"mutations_sync": 2}
        assert sql == (
            "ALTER TABLE loom.scim_users UPDATE crawl_erasure_enc = {new:String} "
            "WHERE id = {id:String} AND crawl_erasure_enc = {old:String}"
        )
        self.mutations += 1
        if not self.ignore_mutation:
            for row in self.rows:
                if row["id"] == params["id"] and row["crawl_erasure_enc"] == params["old"]:
                    row["crawl_erasure_enc"] = params["new"]

    async def query_one(self, sql: str, *, params: dict[str, Any]):
        assert "FINAL" not in sql and self.held
        return {
            "n": sum(
                r["id"] == params["id"] and r["crawl_erasure_enc"] == params["old"]
                for r in self.rows
            )
        }


def _history(monkeypatch: pytest.MonkeyPatch) -> tuple[_Rows, list[person.Capture]]:
    monkeypatch.setattr(settings, "jwt_secret", "first-person-history-root-secret-which-is-long")
    monkeypatch.setattr(settings, "jwt_secret_previous", "")
    state = _Rows(monkeypatch)
    owner = person.Person("org", "canonical", ("contact@example.com",), ("canonical", "old-alias"))
    captured = [
        person.Capture("managed-person", (owner,), prepared=ready) for ready in (False, True)
    ]
    state.rows = [
        {
            "id": value.id,
            "crawl_erasure_enc": person._seal(value),
            "active": False,
            "deleted": True,
            "subjects": ["canonical"],
            "version": index + 1,
        }
        for index, value in enumerate(captured)
    ]
    monkeypatch.setattr(settings, "jwt_secret_previous", settings.jwt_secret)
    monkeypatch.setattr(settings, "jwt_secret", "next-person-history-root-secret-which-is-long")
    return state, captured


async def test_all_historical_capsules_reseal_without_changing_ordinary_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows, captures = _history(monkeypatch)
    before = copy.deepcopy(rows.rows)
    assert await person.reseal_registry(dry_run=False, ch=rows) == (2, [])
    monkeypatch.setattr(settings, "jwt_secret_previous", "")
    assert [person.load(row["id"], row["crawl_erasure_enc"]) for row in rows.rows] == captures
    for old, new in zip(before, rows.rows, strict=True):
        assert old["crawl_erasure_enc"] != new["crawl_erasure_enc"]
        assert {key: value for key, value in old.items() if key != "crawl_erasure_enc"} == {
            key: value for key, value in new.items() if key != "crawl_erasure_enc"
        }
    assert rows.mutations == 2 and not rows.held


async def test_dry_run_preserves_every_raw_capsule_version(monkeypatch: pytest.MonkeyPatch) -> None:
    rows, _captures = _history(monkeypatch)
    before = copy.deepcopy(rows.rows)
    assert await person.reseal_registry(dry_run=True, ch=rows) == (2, [])
    assert rows.rows == before and rows.mutations == 0 and not rows.held


async def test_unacknowledged_raw_history_mutation_refuses_root_retirement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows, _captures = _history(monkeypatch)
    rows.ignore_mutation = True
    with pytest.raises(BackendUnavailableError):
        await person.reseal_registry(dry_run=False, ch=rows)
    assert rows.mutations == 1 and not rows.held
