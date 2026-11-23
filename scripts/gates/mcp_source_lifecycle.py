"""Qualify offline source restoration only in a dedicated disposable CI deployment."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
from dataclasses import dataclass, field, replace
from hashlib import sha256
from pathlib import Path
from typing import Any
from unittest.mock import patch
from uuid import uuid4

_LIMIT = 512
_FIXTURE_KEY = "ci-only-mcp-lifecycle-fixture"
_ROTATED_KEY = "ci-only-mcp-lifecycle-rotated-fixture"
_PROFILE_ID = "offline-lifecycle-profile"
_PROFILE_DIGEST = sha256(b"offline-lifecycle-profile-v1").hexdigest()
_ENDPOINT = "https://source.example.test/mcp"
_TABLES = {
    "connected_accounts": "org, subject, kind, version",
    "knowledge_sources": "org, namespace, name, version",
    "knowledge_mirrors": "org, namespace, mirror, version",
    "code_index_state": "org, namespace, state_key, version",
    "oauth_signin_state": "claim_key",
}


def require(condition: bool) -> None:
    if not condition:
        raise AssertionError("MCP_LIFECYCLE_QUALIFICATION_FAILED")


def fixture_guard(opt_in: bool, result: Path) -> str:
    expected = {
        "GITHUB_ACTIONS": "true",
        "GITHUB_WORKFLOW": "MCP source lifecycle",
        "LOOM_MCP_SOURCE_LIFECYCLE_FIXTURE": "offline-isolated",
        "LOOM_CH_HOST": "127.0.0.1",
        "LOOM_CH_PORT": "8123",
        "LOOM_CH_USER": "loom",
        "LOOM_CH_PASSWORD": "loomdev",
        "LOOM_CH_DATABASE": "loom",
        "LOOM_CH_SECURE": "false",
        "LOOM_JWT_SECRET": _FIXTURE_KEY,
        "LOOM_AUTH_REQUIRED": "false",
        "LOOM_LLM_PROVIDER": "fake",
        "LOOM_EMBEDDING_MODEL": "fake-ngram-1024",
        "LOOM_BACKGROUND_WORKERS": "off",
        "LOOM_BOOT_MIGRATE": "true",
    }
    require(opt_in and all(os.environ.get(k) == v for k, v in expected.items()))
    authority = os.environ.get("LOOM_STORAGE_AUTHORITY", "")
    require(authority in {"clickhouse", "postgres"})
    require(
        os.environ.get("LOOM_PG_DSN", "")
        == ("postgres://loom:loomdev@127.0.0.1:5432/loom" if authority == "postgres" else "")
    )
    require(not os.environ.get("LOOM_BASE_URL") and not os.environ.get("LOOM_CELL"))
    root = Path(os.environ["RUNNER_TEMP"]).resolve()
    require(result.resolve().parent == root and not result.exists())
    return authority


async def count(ch: Any, table: str) -> int:
    require(re.fullmatch(r"[a-z_][a-z0-9_]*", table) is not None)
    row = await ch.query_one(f"SELECT count() AS n FROM loom.{table}")
    require(row is not None and "n" in row)
    return int(row["n"])


async def owned_stores(ch: Any, orgs: tuple[str, ...], *, pg_ready: bool = True) -> None:
    """Count foreign rows before global maintenance; never read their content."""
    from server.core import pg
    from server.knowledge.app.connected_restore import offline_refusal

    require(not await offline_refusal(ch))
    require(await count(ch, "instances") == 0 and await count(ch, "worker_status") == 0)
    tables = await ch.query_all(
        "SELECT DISTINCT c.table AS table_name FROM system.columns AS c "
        "INNER JOIN system.tables AS t ON c.database = t.database AND c.table = t.name "
        "WHERE c.database = 'loom' AND c.name = 'org' "
        "AND t.engine NOT IN ('View', 'MaterializedView', 'Dictionary') "
        "ORDER BY table_name LIMIT 513"
    )
    require(0 < len(tables) <= _LIMIT)
    for table in tables:
        name = table["table_name"]
        require(re.fullmatch(r"[a-z_][a-z0-9_]*", name) is not None)
        row = await ch.query_one(
            f"SELECT count() AS n FROM loom.{name} "
            "WHERE org NOT IN {orgs:Array(String)} OR isNull(org)",
            params={"orgs": list(orgs)},
        )
        require(row is not None and row["n"] == 0)
    if pg.enabled() and pg_ready:
        # The disposable fixture's migration role must see every row despite RLS.
        require(
            bool(
                await pg.fetchval(
                    "SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = current_user"
                )
            )
        )
        tables = await pg.fetch(
            "SELECT DISTINCT c.table_name AS table_name FROM information_schema.columns c "
            "JOIN information_schema.tables t USING (table_catalog, table_schema, table_name) "
            "WHERE c.table_schema = 'loom' AND c.column_name = 'org' "
            "AND t.table_type = 'BASE TABLE' ORDER BY table_name LIMIT 513"
        )
        require(0 < len(tables) <= _LIMIT)
        for table in tables:
            name = table["table_name"]
            require(re.fullmatch(r"[a-z_][a-z0-9_]*", name) is not None)
            require(
                await pg.fetchval(
                    f"SELECT count(*) FROM loom.{name} WHERE org <> ALL($1::text[]) OR org IS NULL",
                    list(orgs),
                )
                == 0
            )


async def snapshot(ch: Any) -> dict[str, list[dict[str, Any]]]:
    out = {}
    for table, order in _TABLES.items():
        rows = await ch.query_all(f"SELECT * FROM loom.{table} ORDER BY {order} LIMIT 129")
        require(len(rows) < 129)
        out[table] = rows
    return out


@dataclass
class Fixture:
    orgs: tuple[str, str]
    accounts: list[Any] = field(default_factory=list)
    sources: list[dict[str, Any]] = field(default_factory=list)
    connections: dict[str, tuple[str, str, str]] = field(default_factory=dict)
    mirrors: dict[tuple[str, str], str] = field(default_factory=dict)
    envelope_plaintext: str = field(default="", repr=False)

    def connection(self, owner: tuple[str, str, str], *, inactive: bool = False) -> str:
        from server.knowledge.app import connected_accounts as accounts

        value = accounts._next_connection(inactive=inactive)
        self.connections[value] = owner
        return value

    def source(
        self,
        owner: tuple[str, str, str],
        connection: str,
        name: str,
        namespace: str = "selected",
        *,
        active: bool = True,
    ) -> None:
        org, subject, slot = owner
        native = slot == "gdrive"
        self.sources.append(
            {
                "org": org,
                "namespace": namespace,
                "name": name,
                "source_id": str(uuid4()),
                "kind": "",
                "source_kind": "gdrive" if native else "mcp",
                "config_json": "{}" if native else '{ "unknown-contract": ',
                "items_json": "[]" if native else '[ {"opaque-resource": ',
                "credential_mode": "connected" if native else "mcp",
                "access": "mirrored",
                "owner_subject": subject,
                "connection_slot": "" if native else slot,
                "connection_id": connection,
                "source_contract_version": 0 if native else 65535,
                "credential_enc": "unreadable-fixture-source-envelope",
                "secret_enc": "",
                "created_by": subject,
                "active": active,
                "pending_delete": False,
                "version": 1,
            }
        )
        self.mirrors.setdefault((org, namespace), "acl-" + uuid4().hex[:24])

    @property
    def target(self) -> tuple[str, str, str]:
        return self.orgs[0], "owner-a", "mcp:" + "a" * 32

    def prepare(self) -> None:
        from pydantic import SecretStr

        from server.core.secrets_at_rest import seal
        from server.knowledge.app import connected_accounts as accounts
        from server.knowledge.domain.connected_accounts import ConnectedAccount
        from server.knowledge.domain.mcp_sources import ConnectionEnvelope, encode_envelope

        a, b = self.orgs
        owners = [
            self.target,
            (a, "owner-a", "mcp:" + "b" * 32),
            (a, "owner-b", "mcp:" + "a" * 32),
            (a, "owner-z", "gdrive"),
            (b, "owner-a", "mcp:" + "a" * 32),
        ]
        for index, owner in enumerate(owners):
            generation = self.connection(owner)
            credential = "unreadable-fixture-account-envelope"
            if owner == self.target:
                self.envelope_plaintext = encode_envelope(
                    ConnectionEnvelope(
                        version=1,
                        org=owner[0],
                        owner=owner[1],
                        slot=owner[2],
                        generation=generation,
                        profile_id=_PROFILE_ID,
                        profile_sha256=_PROFILE_DIGEST,
                        endpoint=_ENDPOINT,
                        audience=_ENDPOINT,
                        auth="bearer",
                        bearer=SecretStr(uuid4().hex),
                    )
                )
                credential = seal(accounts.CREDENTIAL_PURPOSE, self.envelope_plaintext)
            self.accounts.append(
                ConnectedAccount(
                    *owner,
                    generation,
                    credential_enc=credential,
                    active=True,
                )
            )
            self.source(owner, generation, f"current-{index}", active=owner[2] != "gdrive")
        previous = self.connection(self.target, inactive=True)
        self.source(self.target, previous, "previous", "historical", active=False)
        orphan = (a, "owner-a", "mcp:" + "c" * 32)
        for index in range(2):
            generation = self.connection(orphan, inactive=index == 0)
            self.source(orphan, generation, f"source-only-{index}", active=index == 1)

    async def seed(self, ch: Any) -> None:
        from server.auth import claims
        from server.knowledge.app import connected_accounts as accounts
        from server.knowledge.app.sources import sources_lease
        from server.knowledge.store import connected_accounts as account_store
        from server.knowledge.store import mirrors, sources

        for generation, owner in self.connections.items():
            async with accounts.account_lease(*owner):
                key = accounts._fence_key(generation)
                require(await claims.get(ch, key) is None)
                await accounts._move_fence(None, generation, ch=ch)
        for account in self.accounts:
            async with accounts.account_lease(account.org, account.subject, account.kind):
                await account_store.put(account)
        for (org, namespace), mirror in self.mirrors.items():
            async with sources_lease(org, namespace):
                await mirrors.add_mirror(org, namespace, mirror)
        for row in self.sources:
            slot = row["connection_slot"] or row["source_kind"]
            async with accounts.account_lease(row["org"], row["owner_subject"], slot):
                async with sources_lease(row["org"], row["namespace"]):
                    await ch.insert("loom.knowledge_sources", [row])
        orphan = self.orgs[0], "owner-a", "mcp:" + "c" * 32
        require(await account_store.restore_keys(owner=orphan) == [])
        history = await sources.connected_history(owner=orphan)
        require(
            {s.connection_id for s in history}
            == {g for g, owner in self.connections.items() if owner == orphan}
        )

    async def claims_owned(self, ch: Any, *, stopped: bool = False) -> dict[str, str]:
        from server.auth import claims
        from server.knowledge.app import connected_accounts as accounts

        expected = {accounts._fence_key(g) for g in self.connections}
        rows = await ch.query_all("SELECT claim_key FROM loom.oauth_signin_state LIMIT 129")
        require({r["claim_key"] for r in rows} == expected)
        out = {}
        for generation in self.connections:
            key = accounts._fence_key(generation)
            observed = await claims.get(ch, key)
            require(observed is not None and accounts._fence_key(observed) == key)
            if stopped:
                require(observed.partition("/")[2].startswith("0"))
            out[key] = observed
        return out

    def retirement(self, row: dict[str, Any]) -> Any:
        from server.knowledge.store.source_retirements import Retirement

        return Retirement(
            row["org"],
            row["namespace"],
            row["name"],
            row["source_id"],
            row["source_kind"],
            self.mirrors[row["org"], row["namespace"]],
        )

    async def retirement_pending(self, ch: Any, source: dict[str, Any]) -> None:
        from server.knowledge.store import source_retirements

        item = self.retirement(source)
        require(await source_retirements.pending(item))
        row = await ch.query_one(
            "SELECT state_value, detail FROM loom.code_index_state FINAL "
            "WHERE org = {org:String} AND namespace = {mirror:String} "
            "AND state_key = {key:String} LIMIT 1",
            params={"org": item.org, "mirror": item.mirror, "key": item.key},
        )
        require(row is not None and row["state_value"] == "pending")
        require(
            json.loads(row["detail"])
            == {
                "namespace": source["namespace"],
                "name": source["name"],
                "source_id": source["source_id"],
                "kind": source["source_kind"],
            }
        )

    async def boundary(self, ch: Any, ended: Any) -> None:
        from server.auth import claims
        from server.knowledge.app import connected_accounts as accounts
        from server.knowledge.store import connected_accounts as account_store
        from server.knowledge.store import sources

        require((ended.org, ended.subject, ended.kind) == self.target)
        marker = await account_store.get(*self.target)
        require(marker is not None and not marker.active and not marker.credential_enc)
        require((marker.org, marker.subject, marker.kind) == self.target)
        require(marker.connection_id == ended.connection_id and marker.connected_at is None)
        require(marker.disconnected_at is not None)
        require(not marker.account_id and not marker.workspace_id and not marker.account_email_enc)
        require(not marker.scopes and not marker.refresh_id)
        for generation, owner in self.connections.items():
            observed = await claims.get(ch, accounts._fence_key(generation))
            require(observed is not None)
            if owner == self.target:
                require(observed.partition("/")[2].startswith("0"))
            else:
                require(observed == generation)
        for row in self.sources:
            slot = row["connection_slot"] or row["source_kind"]
            if (row["org"], row["owner_subject"], slot) != self.target:
                continue
            current = await sources.latest_source(row["org"], row["namespace"], row["name"])
            require(current is not None and not current.active and current.pending_delete)
            require(
                current.source_id == row["source_id"]
                and not current.credential_enc
                and not current.secret_enc
            )
            require(
                (
                    current.org,
                    current.namespace,
                    current.owner_subject,
                    current.connection_slot,
                    current.connection_id,
                )
                == (
                    row["org"],
                    row["namespace"],
                    row["owner_subject"],
                    row["connection_slot"],
                    row["connection_id"],
                )
            )
            require(current.generic_config_json == row["config_json"])
            require(current.generic_items_json == row["items_json"])
            require(current.source_contract_version == row["source_contract_version"])
            await self.retirement_pending(ch, row)


async def current_accounts(ch: Any, fixture: Fixture) -> dict[tuple[str, str, str], Any]:
    from server.knowledge.store import connected_accounts as account_store

    rows = {}
    for account in fixture.accounts:
        key = account.org, account.subject, account.kind
        current = await account_store.get(*key, ch=ch)
        require(current is not None)
        rows[key] = current
    return rows


async def reseal_rotation(
    ch: Any,
    fixture: Fixture,
    current_key: str,
    previous_key: str,
    client_attempts: list[None],
) -> None:
    from server.core.secrets_at_rest import unseal
    from server.knowledge.app import connected_accounts as accounts
    from server.knowledge.domain.mcp_sources import bind_envelope, decode_envelope
    from server.settings import settings

    before = await current_accounts(ch, fixture)
    raw_before = await snapshot(ch)
    fences = await fixture.claims_owned(ch)
    missing = sorted("/".join(key) for key in before if key != fixture.target)
    require(len(missing) == 4)
    with (
        patch.object(settings, "jwt_secret", current_key),
        patch.object(settings, "jwt_secret_previous", previous_key),
    ):
        candidates, unreadable = await accounts.reseal_registry(
            "credential_enc", accounts.CREDENTIAL_PURPOSE, dry_run=True, ch=ch
        )
        require(not client_attempts and candidates == 1 and sorted(unreadable) == missing)
        require(await current_accounts(ch, fixture) == before)
        require(await fixture.claims_owned(ch) == fences)
        raw_dry_run = await snapshot(ch)
        require(all(raw_dry_run[t] == raw_before[t] for t in _TABLES if t != "connected_accounts"))

        candidates, unreadable = await accounts.reseal_registry(
            "credential_enc", accounts.CREDENTIAL_PURPOSE, dry_run=False, ch=ch
        )
        require(not client_attempts and candidates == 1 and sorted(unreadable) == missing)
        after = await current_accounts(ch, fixture)
        old, new = before[fixture.target], after[fixture.target]
        require(new.credential_enc != old.credential_enc and new.version > old.version)
        require(replace(new, credential_enc=old.credential_enc, version=old.version) == old)
        require(all(after[key] == value for key, value in before.items() if key != fixture.target))
        require(await fixture.claims_owned(ch) == fences)
        raw_after = await snapshot(ch)
        require(all(raw_after[t] == raw_before[t] for t in _TABLES if t != "connected_accounts"))
        # Removing the fallback key proves that the new current key opens the envelope.
        with patch.object(settings, "jwt_secret_previous", ""):
            plaintext = unseal(accounts.CREDENTIAL_PURPOSE, new.credential_enc, warn=False)
        require(bool(plaintext) and plaintext == fixture.envelope_plaintext)
        envelope = decode_envelope(plaintext)
        require(envelope.auth == "bearer" and envelope.bearer is not None)
        bind_envelope(
            envelope,
            org=new.org,
            owner=new.subject,
            slot=new.kind,
            generation=new.connection_id,
            profile_id=_PROFILE_ID,
            profile_sha256=_PROFILE_DIGEST,
            endpoint=_ENDPOINT,
            audience=_ENDPOINT,
        )


class FixtureInterrupted(RuntimeError):
    """The single injected failure occurs before physical erasure, after real retirement."""


async def interrupt_restore(ch: Any, fixture: Fixture, before: dict[str, Any]) -> dict[str, str]:
    from server.erasure.store.census import connected_restore_residue
    from server.knowledge.app import connected_accounts as accounts
    from server.knowledge.app import connected_restore as restore

    real_erase = accounts._erase_locked
    interrupted = False
    stopped_at_boundary: dict[str, str] = {}

    async def interrupt(ended: Any, *, keep_marker: bool) -> None:
        nonlocal interrupted, stopped_at_boundary
        if (ended.org, ended.subject, ended.kind) == fixture.target and not interrupted:
            require(not keep_marker)
            await fixture.boundary(ch, ended)
            boundary = await snapshot(ch)
            target_ids = {
                row["source_id"]
                for row in fixture.sources
                if (row["org"], row["owner_subject"], row["connection_slot"]) == fixture.target
            }
            require(
                [
                    row
                    for row in boundary["knowledge_sources"]
                    if str(row["source_id"]) not in target_ids
                ]
                == [
                    row
                    for row in before["knowledge_sources"]
                    if str(row["source_id"]) not in target_ids
                ]
            )
            require(
                [
                    row
                    for row in boundary["connected_accounts"]
                    if (row["org"], row["subject"], row["kind"]) != fixture.target
                ]
                == [
                    row
                    for row in before["connected_accounts"]
                    if (row["org"], row["subject"], row["kind"]) != fixture.target
                ]
            )
            stopped_at_boundary = await fixture.claims_owned(ch)
            interrupted = True
            raise FixtureInterrupted()
        await real_erase(ended, keep_marker=keep_marker)

    with patch.object(accounts, "_erase_locked", new=interrupt):
        try:
            await restore.restore_all(apply=True, ch=ch)
        except FixtureInterrupted:
            pass
        else:
            require(False)
    require(interrupted and await count(ch, "knowledge_sources") > 0)
    require(await connected_restore_residue(ch=ch) > 0)
    return stopped_at_boundary


async def complete_restore(ch: Any, fixture: Fixture, stopped_at_boundary: dict[str, str]) -> None:
    from server.erasure.app.connected_accounts import require_restore_erased
    from server.erasure.store.census import connected_restore_residue
    from server.knowledge.app import connected_accounts as accounts
    from server.knowledge.app import connected_restore as restore
    from server.knowledge.store import connected_accounts as account_store
    from server.knowledge.store import sources

    await owned_stores(ch, fixture.orgs)
    await restore.restore_all(apply=True, ch=ch)
    require(await count(ch, "connected_accounts") == 0)
    require(await count(ch, "knowledge_sources") == 0)
    require(await account_store.restore_keys() == [] and await sources.connected_history() == [])
    require(await connected_restore_residue(ch=ch) == 0)
    await require_restore_erased(ch=ch)
    stopped = await fixture.claims_owned(ch, stopped=True)
    for generation, owner in fixture.connections.items():
        key = accounts._fence_key(generation)
        if owner == fixture.target:
            require(stopped[key] == stopped_at_boundary[key])
        if generation.partition("/")[2].startswith("0"):
            require(stopped[key] == generation)
    for row in fixture.sources:
        await fixture.retirement_pending(ch, row)
    require(await count(ch, "code_index_state") == len(fixture.sources))


async def qualify(result: dict[str, Any]) -> None:
    from server import boot
    from server.cli._ch import connect_verified, disconnect
    from server.core.ch import get_client, set_query_tag
    from server.core.errors import OverloadedError
    from server.erasure.app.connected_accounts import require_restore_erased
    from server.erasure.store.census import connected_restore_residue
    from server.knowledge.app import connected_accounts as accounts
    from server.knowledge.app import connected_restore as restore
    from server.settings import settings

    require(settings.llm_provider == "fake" and settings.embedding_model == "fake-ngram-1024")
    require(settings.storage_authority == result["config"]["authority"])
    require(not settings.auth_actually_required())
    require(str(settings.jwt_secret) == _FIXTURE_KEY and not settings.jwt_secret_previous)
    ch = get_client()
    await ch.connect()
    try:
        set_query_tag("gate.mcp_source_lifecycle")
        result["stage"] = "fresh_store_guard"
        await owned_stores(ch, (), pg_ready=False)
        require(await count(ch, "oauth_signin_state") == 0)
        await boot.require_storage_authority(ch)
        await connect_verified(ch)
        await owned_stores(ch, ())
        require(await count(ch, "oauth_signin_state") == 0)
        nonce = uuid4().hex
        fixture = Fixture((f"mcp-lifecycle-{nonce}-a", f"mcp-lifecycle-{nonce}-b"))
        fixture.prepare()
        result["stage"] = "seed"
        await fixture.seed(ch)
        await owned_stores(ch, fixture.orgs)
        await fixture.claims_owned(ch)
        require(await count(ch, "connected_accounts") == len(fixture.accounts))
        require(await count(ch, "knowledge_sources") == len(fixture.sources))
        require(await connected_restore_residue(ch=ch) > 0)
        try:
            await require_restore_erased(ch=ch)
        except OverloadedError:
            pass
        else:
            require(False)
        before = await snapshot(ch)
        result["counts"] = {
            "accounts": len(fixture.accounts),
            "sources": len(fixture.sources),
            "generations": len(fixture.connections),
            "mirrors": len(fixture.mirrors),
        }
        client_attempts: list[None] = []

        def refuse_native_client(*args: Any, **kwargs: Any) -> Any:
            # Fail-only sentinel: no client or provider result can be supplied by this seam.
            client_attempts.append(None)
            raise AssertionError("MCP_LIFECYCLE_NATIVE_CLIENT_FORBIDDEN")

        with patch.object(accounts, "client", new=refuse_native_client):
            result["stage"] = "dry_run"
            await restore.restore_all(apply=False, ch=ch)
            require(await snapshot(ch) == before)
            require(not client_attempts)
            result["checks"].append("dry_run_unchanged")

            result["stage"] = "reseal_forward"
            await reseal_rotation(ch, fixture, _ROTATED_KEY, _FIXTURE_KEY, client_attempts)
            result["checks"].append("reseal_forward_one_candidate_four_unreadable")
            result["stage"] = "reseal_back"
            await reseal_rotation(ch, fixture, _FIXTURE_KEY, _ROTATED_KEY, client_attempts)
            require(str(settings.jwt_secret) == _FIXTURE_KEY and not settings.jwt_secret_previous)
            result["checks"].append("reseal_back_one_candidate_four_unreadable")

            result["stage"] = "interruption"
            stopped_at_boundary = await interrupt_restore(ch, fixture, before)
            require(not client_attempts)
            result["checks"].append("real_retirement_before_injected_erase_failure")

            result["stage"] = "retry"
            await complete_restore(ch, fixture, stopped_at_boundary)
            require(not client_attempts)
            result["checks"].append("retry_erases_all_raw_bindings_and_keeps_retirements")
            result["counts"]["final_global_residue"] = 0
            result["counts"]["pending_retirements"] = len(fixture.sources)

            result["stage"] = "idempotent_rerun"
            after = await snapshot(ch)
            await restore.restore_all(apply=True, ch=ch)
            require(await snapshot(ch) == after)
            await require_restore_erased(ch=ch)
            await owned_stores(ch, fixture.orgs)
            require(not client_attempts)
            result["checks"].append("completed_restore_rerun_unchanged")
            result["counts"]["native_client_attempts"] = len(client_attempts)
        result["stage"] = "complete"
        result["status"] = "passed"
    finally:
        await disconnect(ch)


def failure_detail(error: Exception) -> dict[str, Any]:
    root = Path(__file__).parents[2]
    frames = []
    cursor = error.__traceback__
    for _ in range(64):
        if cursor is None:
            break
        try:
            path = Path(cursor.tb_frame.f_code.co_filename).relative_to(root).as_posix()
        except ValueError:
            pass
        else:
            if re.fullmatch(r"[A-Za-z0-9_./-]{1,160}", path):
                frames.append({"path": path, "line": cursor.tb_lineno})
        cursor = cursor.tb_next
    return {"type": re.sub(r"[^A-Za-z0-9_]", "_", type(error).__name__)[:64], "frames": frames[-8:]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--isolated-fixture", action="store_true", required=True)
    parser.add_argument("--result", type=Path, required=True)
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    result: dict[str, Any] = {"status": "failed", "stage": "guard", "checks": []}
    try:
        authority = fixture_guard(args.isolated_fixture, args.result)
    except Exception:  # noqa: BLE001 — an unverified target must not open any store
        print("MCP lifecycle fixture guard refused", file=sys.stderr)
        return 2
    result["config"] = {"authority": authority, "models": "fake", "workers": "off"}
    try:
        asyncio.run(asyncio.wait_for(qualify(result), timeout=300))
    except Exception as error:  # noqa: BLE001 — emit no payload, SQL exception or credential in CI
        result["status"] = "failed"
        result["failure"] = failure_detail(error)
    payload = json.dumps(result, sort_keys=True, indent=2) + "\n"
    require(len(payload.encode()) <= 16384)
    with args.result.open("x") as output:
        output.write(payload)
    print("MCP lifecycle qualification " + result["status"])
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
