"""Fresh cell credentials and complete public identities gate administration reads."""

from __future__ import annotations

import hashlib
import json
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

import pytest

from server.router import cell_admin, directory


def _row(**changes: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "cell": "later",
        "deployment_id": "deployment-later",
        "host": "later.example.test",
        "state": "serving",
        "mode": "enforced",
        "closed": True,
        "mutation_generation": UUID("c33e76ce-f023-4b45-92ea-2e9bfa008583"),
        "current_digest": "a" * 64,
        "next_digest": None,
        "administration": json.dumps(
            [
                {"purpose": "automation", "name": "automation-later", "thumbprint": "A" * 43},
                {"purpose": "recovery", "name": "recovery-later", "thumbprint": "B" * 43},
            ]
        ),
        "credential_sha256": hashlib.sha256(b"old").hexdigest(),
        "credential_next_sha256": None,
    }
    row.update(changes)
    return row


def test_census_includes_closed_later_cell_without_exposing_credentials() -> None:
    answer = cell_admin.census([_row()])
    target = answer["cells"][0]
    assert target["cell"] == "later"
    assert target["origin"] == "https://later.example.test"
    assert target["mutation_generation"] == "c33e76ce-f023-4b45-92ea-2e9bfa008583"
    assert set(target) == {
        "cell",
        "deployment_id",
        "origin",
        "state",
        "mode",
        "mutation_generation",
        "configuration_digests",
        "automation",
        "recovery",
    }


@pytest.mark.parametrize(
    "metadata",
    [
        "[]",
        "{}",
        "[NaN, NaN]",
        "[null, null]",
        " " * 1025,
        '[{"purpose":"automation","purpose":"recovery"},{}]',
        json.dumps([{"purpose": "automation", "name": "one", "thumbprint": "A" * 43}] * 2),
    ],
)
def test_incomplete_or_ambiguous_public_metadata_refuses(metadata: str) -> None:
    with pytest.raises(cell_admin.InvalidAdministration):
        cell_admin.identity(_row(administration=metadata))


@pytest.mark.parametrize(
    "field,value",
    [
        ("state", "attaching"),
        ("mode", "recording"),
        ("host", "user@later.example.test"),
        ("host", "later.example.test/path"),
        ("host", "Later.example.test"),
        ("current_digest", None),
        ("next_digest", "oversized"),
        ("mutation_generation", 1),
    ],
)
def test_unsafe_or_incomplete_target_is_never_silently_omitted(field: str, value: Any) -> None:
    with pytest.raises(cell_admin.InvalidAdministration):
        cell_admin.census([_row(**{field: value})])


def test_distinct_cells_cannot_share_an_origin() -> None:
    with pytest.raises(cell_admin.InvalidAdministration):
        cell_admin.census([_row(), _row(cell="other", deployment_id="other-deployment")])


@pytest.mark.asyncio
async def test_fresh_same_row_credentials_decide_every_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = _row()
    reads: list[str] = []

    class Connection:
        async def fetchrow(self, _sql: str, name: str, **_options: Any) -> Any:
            reads.append(name)
            return dict(row) if name == row["cell"] else None

    @asynccontextmanager
    async def connection() -> Any:
        yield Connection()

    monkeypatch.setattr(directory, "_administration_read", connection)
    old, new = (hashlib.sha256(value).hexdigest() for value in (b"old", b"new"))
    answer = await directory.administration_identity("later", old)
    assert answer is not None and answer["automation"]["name"] == "automation-later"
    row["credential_sha256"] = new
    assert await directory.administration_identity("later", old) is None
    assert await directory.administration_identity("later", new) == answer
    assert await directory.administration_identity("another", new) is None
    row["credential_next_sha256"] = old
    assert await directory.administration_identity("later", old) == answer
    assert reads == ["later", "later", "later", "another", "later"]


@pytest.mark.asyncio
async def test_wrong_credential_cannot_probe_invalid_bot_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Connection:
        async def fetchrow(self, _sql: str, _name: str, **_options: Any) -> Any:
            return _row(administration="not public metadata")

    @asynccontextmanager
    async def connection() -> Any:
        yield Connection()

    monkeypatch.setattr(directory, "_administration_read", connection)
    assert await directory.administration_identity("later", "f" * 64) is None


@pytest.mark.parametrize("payload", [None, "界" * 400])
def test_oversize_projection_is_a_refusal_not_a_missing_target(payload: Any) -> None:
    with pytest.raises(cell_admin.InvalidAdministration):
        cell_admin.selected_record(payload)


@pytest.mark.asyncio
async def test_census_stops_reading_at_first_unsupported_cell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    visited: list[str] = []

    class Connection:
        async def cursor(self, _sql: str, **options: Any) -> Any:
            assert options["prefetch"] == 1
            for name, state in [
                ("later", "serving"),
                ("other", "attaching"),
                ("unread", "serving"),
            ]:
                visited.append(name)
                record = _row(cell=name, deployment_id="deployment-" + name, state=state)
                record.pop("closed")
                record.pop("credential_sha256")
                record.pop("credential_next_sha256")
                record["mutation_generation"] = str(record["mutation_generation"])
                yield {"payload": json.dumps(record)}

    @asynccontextmanager
    async def connection() -> Any:
        yield Connection()

    monkeypatch.setattr(directory, "_administration_read", connection)
    with pytest.raises(cell_admin.InvalidAdministration):
        await directory.administration_cells()
    assert visited == ["later", "other"]
