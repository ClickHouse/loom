"""Raw control history loses exact person references, not another owner's signing key."""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from server.erasure.store import crawl_controls as store
from server.knowledge.domain.source_crawl import without_selectors
from server.knowledge.store import sources

pytestmark = pytest.mark.asyncio
_EMAILS = frozenset({"gone@example.com"})


def _options(*people: str, directory: str = "admin@example.com") -> str:
    return json.dumps(
        {
            "directory_subject": directory,
            "subjects": [
                *({"type": "person", "email": p} for p in people),
                {"type": "group", "email": "team@example.com"},
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _row(**extra: Any) -> dict[str, Any]:
    return {
        "org": "org",
        "namespace": "namespace",
        "source_id": "source",
        "name": "name",
        "credential_mode": "crawl",
        "source_kind": "gdrive",
        "kind": "",
        "options_json": _options("gone@example.com", "kept@example.com"),
        "registrant_subject": "other",
        "created_by": "other",
        "active": True,
        "secret_enc": "sealed-shared-service-principal",
        "credential_enc": "",
        "approval_enc": "sealed-original-approval",
        "crawl_generation": "generation",
        "crawl_credential_hash": "a" * 64,
        **extra,
    }


class _World:
    def __init__(self) -> None:
        self.rows = [_row(version=1), _row(version=2)]
        self.groups: list[dict[str, Any]] = []
        self.executed: list[str] = []
        self.ignore_mutation = False

    @staticmethod
    def scope(row: dict[str, Any], params: dict[str, Any]) -> bool:
        return all(
            row[field] == params[key]
            for field, key in (("org", "org"), ("namespace", "ns"), ("source_id", "id"))
        )

    def selected(self, sql: str, params: dict[str, Any]):
        rows = self.groups if "knowledge_group_members" in sql else self.rows
        found = [r for r in rows if self.scope(r, params)]
        if "credential_mode = 'crawl'" in sql:
            found = [r for r in found if r["credential_mode"] == "crawl"]
        if "options_json = {before:String}" in sql:
            found = [r for r in found if r["options_json"] == params["before"]]
        if (
            "(registrant_subject = {subject:String} OR created_by IN {spellings:Array(String)})"
            in sql
        ):
            found = [
                r
                for r in found
                if r["registrant_subject"] == params["subject"]
                or r["created_by"] in params["spellings"]
            ]
        else:
            for key in ("registrant_subject", "created_by"):
                if f"{key} = {{subject:String}}" in sql:
                    found = [r for r in found if r[key] == params["subject"]]
            if "created_by IN {spellings:Array(String)}" in sql:
                found = [r for r in found if r["created_by"] in params["spellings"]]
        if "(active OR approval_enc != '')" in sql:
            found = [r for r in found if r["active"] or r["approval_enc"]]
        if "hasAny(members" in sql:
            found = [r for r in found if set(r["members"]).intersection(params["emails"])]
        return found

    async def query_all(self, sql: str, *, params: dict[str, Any]):
        if "FROM system.mutations" in sql:
            return [
                {"is_done": 1, "parts_to_do": 0, "latest_fail_reason": "", "mutation_id": "owned"}
            ]
        assert "SELECT DISTINCT options_json, registrant_subject" in sql and "FINAL" not in sql
        keys = sorted(
            {(r["options_json"], r["registrant_subject"]) for r in self.selected(sql, params)}
        )
        return [
            dict(zip(("options_json", "registrant_subject"), k, strict=True))
            for k in keys[: params["limit"]]
        ]

    async def query_one(self, sql: str, *, params: dict[str, Any]):
        assert "SELECT count() AS n" in sql and "FINAL" not in sql
        return {"n": len(self.selected(sql, params))}

    async def execute(self, sql: str, *, params: dict[str, Any]):
        assert "ALTER TABLE" in sql and "DELETE" not in sql and "erasure_mutation_tag" in params
        self.executed.append(sql)
        if self.ignore_mutation:
            return
        # Interpret only SQL writes; all retirement/redaction decisions remain in production.
        for row in self.selected(sql, params):
            if "UPDATE active = 0" in sql:
                row.update(active=False, approval_enc="", crawl_generation="")
            elif "UPDATE options_json" in sql:
                row["options_json"] = params["after"]
            elif "UPDATE members" in sql:
                row["members"] = [v for v in row["members"] if v not in params["emails"]]
            else:
                field = "registrant_subject" if "UPDATE registrant_subject" in sql else "created_by"
                row[field] = ""


async def test_selector_history_retires_only_affected_approvals_preserving_other_people_and_key():
    world = _World()
    latest = _row(version=3, options_json=_options("kept@example.com"))
    foreign = _row(org="other")
    world.rows.extend([copy.deepcopy(latest), copy.deepcopy(foreign)])
    assert await store.controls("org", "namespace", "source", "deleted", _EMAILS, ch=world)
    for row in world.rows[:2]:
        assert row["secret_enc"] == "sealed-shared-service-principal"
        assert row["active"] is False and row["approval_enc"] == row["crawl_generation"] == ""
        assert json.loads(row["options_json"])["subjects"] == [
            {"type": "person", "email": "kept@example.com"},
            {"type": "group", "email": "team@example.com"},
        ]
        assert not sources._source(row).active
    assert world.rows[2:] == [latest, foreign]


async def test_registrant_erasure_does_not_retire_newly_approved_other_registrant():
    world = _World()
    world.rows = [
        _row(
            registrant_subject="deleted",
            created_by="deleted",
            options_json=_options("kept@example.com"),
        ),
        _row(
            registrant_subject="other",
            created_by="deleted",
            options_json=_options("kept@example.com"),
        ),
    ]
    await store.controls("org", "namespace", "source", "deleted", _EMAILS, ch=world)
    old, current = world.rows
    assert old["active"] is False and old["registrant_subject"] == old["created_by"] == ""
    assert current["active"] and current["approval_enc"] == "sealed-original-approval"
    assert current["created_by"] == "" and current["registrant_subject"] == "other"
    assert old["secret_enc"] == current["secret_enc"] == "sealed-shared-service-principal"


async def test_historical_groups_drop_only_exact_member_for_exact_source():
    world = _World()
    base = {
        "org": "org",
        "namespace": "mirror",
        "source_id": "source",
        "group": "team@example.com",
        "members": ["gone@example.com", "kept@example.com"],
    }
    foreign = {**base, "source_id": "other"}
    world.groups = [{**base, "version": 1}, {**base, "version": 2}, copy.deepcopy(foreign)]
    await store.memberships("org", "mirror", "source", _EMAILS, ch=world)
    assert [r["members"] for r in world.groups[:2]] == [["kept@example.com"]] * 2
    assert world.groups[2] == foreign


async def test_successful_mutation_status_cannot_hide_control_residue():
    world = _World()
    world.ignore_mutation = True
    with pytest.raises(RuntimeError, match="history_remains"):
        await store.controls("org", "namespace", "source", "deleted", _EMAILS, ch=world)


async def test_directory_subject_removal_has_valid_idempotent_retired_representation():
    raw = _options("kept@example.com", directory="gone@example.com")
    after = without_selectors(raw, _EMAILS)
    assert json.loads(after)["directory_subject"] == ""
    assert without_selectors(after, _EMAILS) == after
    source = sources._source(_row(options_json=after, active=False, approval_enc=""))
    assert not source.active and source.crawl_options == json.loads(after)
    with pytest.raises(ValueError, match="address refused"):
        sources._source(_row(options_json=after, active=True))


async def test_malformed_historical_options_refuse_without_destroying_another_owners_key():
    world = _World()
    world.rows[0]["options_json"] = "{broken"
    before = copy.deepcopy(world.rows)
    with pytest.raises(ValueError):
        await store.controls("org", "namespace", "source", "deleted", _EMAILS, ch=world)
    assert world.rows == before and not world.executed


async def test_verified_creator_alias_is_redacted_without_treating_it_as_source_authority():
    world = _World()
    current = _row(created_by="person@example.com", options_json=_options("kept@example.com"))
    foreign = _row(created_by="unverified@example.com", options_json=_options("kept@example.com"))
    world.rows = [current, foreign]
    affected = await store.controls(
        "org",
        "namespace",
        "source",
        "canonical-person",
        _EMAILS,
        spellings=("canonical-person", "person@example.com"),
        ch=world,
    )
    assert (
        not affected
        and current["created_by"] == ""
        and foreign["created_by"] == "unverified@example.com"
    )
    assert all(
        row["active"]
        and row["approval_enc"] == "sealed-original-approval"
        and row["secret_enc"] == "sealed-shared-service-principal"
        for row in world.rows
    )
