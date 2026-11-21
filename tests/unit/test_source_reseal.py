"""Root rotation cannot copy metadata back after canonical-person cleanup."""

from __future__ import annotations

import copy
from contextlib import asynccontextmanager
from typing import Any

import pytest

from server.cli import secrets as cli
from server.core import leases
from server.core.secrets_at_rest import seal, unseal
from server.erasure.store import crawl_controls
from server.knowledge.app import sources as source_ops
from server.settings import settings
from tests.unit.test_crawl_person_controls import _EMAILS, _World

pytestmark = pytest.mark.asyncio
_TABLE = "loom.knowledge_sources"
_SOURCE = "00000000-0000-4000-8000-000000000001"


class _Rows(_World):
    def __init__(self, monkeypatch: pytest.MonkeyPatch, *, race: bool, lost_reply: bool = False):
        super().__init__()
        self.race = race
        self.lost_reply = lost_reply
        self.cleaned = False
        self.repairs = 0
        self.held: tuple[str, str] | None = None
        self.insert_scopes: list[tuple[str, str] | None] = []
        self.ignore_write = False
        self.cipher_writes: list[tuple[str, str] | None] = []
        self.expire_after_read = False
        self.expired = False
        monkeypatch.setattr(leases, "holding", self.holding)

    @asynccontextmanager
    async def holding(self, key: str, *, ttl_s: float, wait_s: float, ch: Any, settle_writes: bool):
        assert ch is self and (ttl_s, wait_s) == (60.0, 5.0) and settle_writes
        prefix, org, namespace = key.split(":")
        assert prefix == "knowledge-source" and self.held is None
        self.held = org, namespace
        try:
            yield True
        finally:
            self.held = None
            if self.expired:
                raise leases.ClaimExpired("source lease expired", operation=key)

    def latest(self) -> list[dict[str, Any]]:
        rows: dict[tuple[str, str, str], dict[str, Any]] = {}
        for row in self.rows:
            key = row["org"], row["namespace"], row["name"]
            if key not in rows or row["version"] > rows[key]["version"]:
                rows[key] = row
        return list(rows.values())

    async def cleanup_once(self) -> None:
        async with source_ops.sources_lease("org", "namespace", ch=self):
            await crawl_controls.controls(
                "org",
                "namespace",
                _SOURCE,
                "deleted",
                _EMAILS,
                spellings=("deleted", "gone@example.com"),
                ch=self,
            )

    async def cleanup(self) -> None:
        if self.cleaned or not self.race:
            return
        try:
            await self.cleanup_once()
        except RuntimeError as error:
            assert str(error) == "lost cleanup reply" and not self.lost_reply
            self.repairs += 1
            await self.cleanup_once()
        self.cleaned = True  # The real mutation polling and residue checks returned.

    async def query_all(self, sql: str, params: dict[str, Any] | None = None):
        compact = " ".join(sql.split())
        if compact.startswith("SELECT config_id FROM loom.provider_configuration FINAL"):
            assert params == {"s": list(cli._REBUILDING)}
            return []
        if compact.startswith("SELECT DISTINCT org, namespace FROM loom.knowledge_sources FINAL"):
            assert params is None and self.held is None
            assert compact.endswith("ORDER BY org, namespace")
            column = "credential_enc" if "credential_enc != ''" in compact else "secret_enc"
            keys = sorted({(r["org"], r["namespace"]) for r in self.latest() if r[column]})
            if not self.expire_after_read:
                await self.cleanup()
            return [{"org": org, "namespace": ns} for org, ns in keys]
        if compact.startswith("SELECT * FROM loom.knowledge_sources FINAL"):
            column = "credential_enc" if "credential_enc != ''" in compact else "secret_enc"
            rows = [r for r in self.latest() if r[column]]
            if params is not None:
                assert set(params) == {"org", "ns"} and self.held == (params["org"], params["ns"])
                assert "org = {org:String} AND namespace = {ns:String}" in compact
                rows = [r for r in rows if (r["org"], r["namespace"]) == self.held]
            snapshot = copy.deepcopy(rows)
            if self.expire_after_read and params is not None:
                # The store grants cleanup a new lease after the old holder expires.
                # Its saved read may still resume; a late write must carry no metadata.
                previous = self.held
                self.held = None
                self.expire_after_read = False
                await self.cleanup()
                self.held = previous
                self.expired = True
            else:
                await self.cleanup()
            return snapshot
        assert params is not None
        return await super().query_all(sql, params=params)

    async def query_one(self, sql: str, params: dict[str, Any] | None = None):
        if "FROM loom.signing_keys FINAL" in sql:
            assert params is None
            return None
        assert params is not None
        if "SELECT count() AS n FROM loom.knowledge_sources" in sql and "old" in params:
            assert "FINAL" not in sql and set(params) == {"org", "ns", "name", "id", "old", "new"}
            column = "credential_enc" if "credential_enc = {old:String}" in sql else "secret_enc"
            return {"n": len(self.cipher_rows(params, column))}
        return await super().query_one(sql, params=params)

    def cipher_rows(self, params: dict[str, Any], column: str):
        return [
            r
            for r in self.rows
            if self.scope(r, params) and r["name"] == params["name"] and r[column] == params["old"]
        ]

    async def execute(self, sql: str, *, params: dict[str, Any], settings=None):
        assert self.held == (params["org"], params["ns"])
        if "old" in params:
            column = "credential_enc" if "UPDATE credential_enc" in sql else "secret_enc"
            assert sql.startswith("ALTER TABLE loom.knowledge_sources UPDATE " + column)
            assert settings == {"mutations_sync": 2}
            assert "name = {name:String}" in sql and "source_id = {id:String}" in sql
            self.cipher_writes.append(self.held)
            if not self.ignore_write:
                for row in self.cipher_rows(params, column):
                    row[column] = params["new"]
            return
        await super().execute(sql, params=params)
        if self.lost_reply:
            self.lost_reply = False
            raise RuntimeError("lost cleanup reply")

    async def insert(self, table: str, rows: list[dict[str, Any]], *, settings: dict[str, int]):
        assert table == _TABLE and settings == {"async_insert": 0}
        self.insert_scopes.append(self.held)
        if not self.ignore_write:
            self.rows.extend(copy.deepcopy(rows))


def _rotate(monkeypatch: pytest.MonkeyPatch, rows: _Rows, column: str = "secret_enc") -> str:
    purpose = (
        "knowledge_sources.secret" if column == "secret_enc" else "knowledge_sources.credential"
    )
    monkeypatch.setattr(settings, "jwt_secret", "old-root-secret")
    monkeypatch.setattr(settings, "jwt_secret_previous", "")
    old = seal(purpose, "retained-service-key")
    for row in rows.rows:
        row.update(source_id=_SOURCE, registrant_subject="deleted", created_by="gone@example.com")
        row[column] = old
    monkeypatch.setattr(settings, "jwt_secret", "new-root-secret")
    monkeypatch.setattr(settings, "jwt_secret_previous", "old-root-secret")
    monkeypatch.setattr(cli, "SEALED", ((_TABLE, column, (), purpose, ""),))
    return purpose


@pytest.mark.parametrize("lost_reply", [False, True])
async def test_command_reseals_only_fresh_rows_after_actual_person_cleanup_and_repair(
    monkeypatch: pytest.MonkeyPatch,
    lost_reply: bool,
) -> None:
    rows = _Rows(monkeypatch, race=True, lost_reply=lost_reply)
    purpose = _rotate(monkeypatch, rows)
    foreign = copy.deepcopy(rows.rows[-1])
    foreign.update(namespace="other", name="other-source", source_id="other-source")
    foreign["secret_enc"] = seal(purpose, "other-service-key")
    rows.rows.append(foreign)
    foreign_before = copy.deepcopy(foreign)
    counts, lost, checked = await cli.reseal_all(rows, dry_run=False)
    assert rows.cleaned and rows.repairs == int(lost_reply)
    assert all(
        not r["active"]
        and not r["approval_enc"]
        and not r["crawl_generation"]
        and not r["registrant_subject"]
        and not r["created_by"]
        and "gone@example.com" not in r["options_json"]
        for r in rows.rows
        if r["namespace"] == "namespace"
    )
    assert counts == {_TABLE + ".secret_enc": 1} and lost == {} and not checked
    assert rows.cipher_writes == [("org", "namespace")] and not rows.insert_scopes
    assert [r for r in rows.rows if r["namespace"] == "other"] == [foreign_before]
    monkeypatch.setattr(settings, "jwt_secret_previous", "")
    [current] = [r for r in rows.latest() if r["namespace"] == "namespace"]
    assert unseal(purpose, current["secret_enc"]) == "retained-service-key"


@pytest.mark.parametrize("column", ["credential_enc", "secret_enc"])
async def test_shared_source_ciphertexts_keep_every_other_field_and_version_contract(
    monkeypatch: pytest.MonkeyPatch,
    column: str,
) -> None:
    rows = _Rows(monkeypatch, race=False)
    purpose = _rotate(monkeypatch, rows, column)
    for row in rows.rows:
        row.update(
            credential_mode="shared",
            kind="gdrive",
            source_kind="",
            options_json="{}",
            registrant_subject="",
            created_by="registrant",
            approval_enc="",
            crawl_generation="",
            crawl_credential_hash="",
        )
    before = copy.deepcopy(rows.rows[-1])
    counts, lost, _checked = await cli.reseal_all(rows, dry_run=False)
    [current] = rows.latest()
    assert {k: v for k, v in current.items() if k != column} == {
        k: v for k, v in before.items() if k != column
    }
    assert current["version"] == before["version"]
    assert rows.cipher_writes == [("org", "namespace")] and not rows.insert_scopes
    assert counts == {_TABLE + "." + column: 1} and lost == {}
    monkeypatch.setattr(settings, "jwt_secret_previous", "")
    assert unseal(purpose, current[column]) == "retained-service-key"


async def test_source_reseal_dry_run_never_writes_a_version(monkeypatch):
    rows = _Rows(monkeypatch, race=False)
    _rotate(monkeypatch, rows)
    before = copy.deepcopy(rows.rows)
    counts, lost, _checked = await cli.reseal_all(rows, dry_run=True)
    assert counts == {_TABLE + ".secret_enc": 1} and lost == {}
    assert rows.rows == before and not rows.insert_scopes and not rows.cipher_writes


async def test_source_reseal_refuses_a_success_reply_without_visible_replacement(monkeypatch):
    rows = _Rows(monkeypatch, race=False)
    _rotate(monkeypatch, rows)
    rows.ignore_write = True
    before = copy.deepcopy(rows.rows)
    with pytest.raises(RuntimeError, match="source_reseal_residue"):
        await cli.reseal_all(rows, dry_run=False)
    assert rows.rows == before and rows.cipher_writes == [("org", "namespace")]
    assert not rows.insert_scopes


async def test_expired_source_holder_cannot_restore_erased_metadata_from_its_saved_read(
    monkeypatch,
):
    rows = _Rows(monkeypatch, race=True)
    purpose = _rotate(monkeypatch, rows)
    rows.expire_after_read = True
    with pytest.raises(leases.ClaimExpired, match="source lease expired"):
        await cli.reseal_all(rows, dry_run=False)
    assert rows.cleaned and rows.expired and not rows.insert_scopes
    assert all(
        not r["active"]
        and not r["registrant_subject"]
        and not r["created_by"]
        and not r["approval_enc"]
        and "gone@example.com" not in r["options_json"]
        for r in rows.rows
    )
    monkeypatch.setattr(settings, "jwt_secret_previous", "")
    assert all(unseal(purpose, r["secret_enc"]) == "retained-service-key" for r in rows.rows)
