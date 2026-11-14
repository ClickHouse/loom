"""The router's attach API and the removal of an unattached cell (server/router/app.py,
server/router/directory.py; docs/architecture/cells.md, "The router"), run through the real
directory code over a connection that keeps the directory in dicts, and `loom cell remove`
(server/cli/cell.py)."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import copy
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from starlette.testclient import TestClient

from server.cli import cell as cell_cli
from server.core import cell
from server.router import app as router_app
from server.router import directory
from server.router.limits import ATTACH_BATCH, ATTACH_BEGIN_MAX_BYTES, MAX_BODY_BYTES
from server.settings import settings

OPERATOR = "lco_" + "7" * 128
NONCES = {"postgres": "n-pg", "clickhouse": "n-ch", "bucket": "n-b"}
FIELDS = {
    "public_host": "loom.example",
    "oauth_issuer_host": "loom.example",
    "deployment_id": "dep-1",
    "bucket": "docs",
    "bucket_prefix": "eu-1",
    "bearer_issuers": ["https://loom.example"],
}
CURRENT = cell.as_value(FIELDS)
NOW = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
_COLUMNS = [c.strip() for c in directory._CELL_COLUMNS.split(",")]


def _cell_row(**over: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "cell": "eu-1",
        "is_first": True,
        "host": "loom.example",
        "state": "attaching",
        "mode": "recording",
        "closed": False,
        "placements_max": None,
        "placed": 0,
        "credential_sha256": "b" * 64,
        "credential_next_sha256": None,
        "deployment_id": "dep-1",
        "current_fp": None,
        "next_fp": None,
        "bucket": "docs",
        "bucket_prefix": "eu-1",
        "probe_url": "http://upstream.internal:7790",
        "upstream_url": "http://upstream.internal:7777",
        "attach_started_at": None,
        "issuers": [],
        "keys_fp": "",
    }
    row.update(over)
    return row


class _Store:
    """The directory's tables, as the statements below change them."""

    def __init__(self, *rows: dict[str, Any]) -> None:
        self.cells = {r["cell"]: r for r in rows}
        self.hosts = {r["host"]: r["cell"] for r in rows}
        self.placements: dict[str, str] = {}
        self.audit: list[tuple[str, str, dict[str, Any]]] = []
        self.statements = 0
        self.down = False


class _Conn:
    """The directory statements the attach calls send, answered from a _Store. A transaction
    restores the store when its body raises, as a rollback does."""

    def __init__(self, store: _Store) -> None:
        self.s = store

    def _count(self) -> None:
        if self.s.down:
            raise OSError("the directory could not be reached")
        self.s.statements += 1

    def _row(self, name: str, sql: str) -> dict[str, Any] | None:
        row = self.s.cells.get(name)
        if row is None:
            return None
        columns = _COLUMNS + (list(directory._ATTACH_EXTRA) if "attach_started_at" in sql else [])
        return {k: row[k] for k in columns}

    async def execute(self, sql: str, *a: Any) -> str:
        self._count()
        cells = self.s.cells
        if sql.startswith("SELECT pg_advisory_xact_lock"):
            return ""
        if "SET attach_started_at" in sql:
            row = cells[a[0]]
            row["attach_started_at"] = row["attach_started_at"] or NOW
            row["current_fp"], row["next_fp"], row["keys_fp"] = a[1], a[2], a[3]
        elif "SET mode = 'enforced'" in sql:
            cells[a[0]]["mode"] = "enforced"
        elif "SET state = 'serving'" in sql:
            cells[a[0]].update(state="serving", issuers=list(a[1]))
        elif "SET placed = placed + 1" in sql:
            cells[a[0]]["placed"] += 1
        elif sql.startswith("INSERT INTO loom_router.audit") and "SELECT 'operator'" in sql:
            self.s.audit += [("operator", "place", {"org": o, "cell": a[1]}) for o in a[0]]
        elif sql.startswith("INSERT INTO loom_router.audit") and "('operator'" in sql:
            self.s.audit.append(("operator", a[0], a[1]))
        elif sql.startswith("INSERT INTO loom_router.audit"):
            self.s.audit.append((a[0], "place", a[1]))
        elif sql.startswith("DELETE FROM loom_router.hosts"):
            self.s.hosts = {h: c for h, c in self.s.hosts.items() if c != a[0]}
        elif sql.startswith("DELETE FROM loom_router.cells"):
            assert a[0] not in self.s.placements.values(), "a placement's foreign key"
            del cells[a[0]]
        else:
            raise AssertionError(f"a statement the fake does not know: {sql}")
        return ""

    async def fetchrow(self, sql: str, *a: Any) -> Any:
        self._count()
        if "FROM loom_router.placements WHERE org = ANY" in sql:
            elsewhere = sorted(o for o in a[0] if self.s.placements.get(o, a[1]) != a[1])
            return (
                {"org": elsewhere[0], "cell": self.s.placements[elsewhere[0]]}
                if elsewhere
                else None
            )
        if sql.startswith("SELECT p.cell, c.deployment_id"):
            name = self.s.placements.get(a[0])
            return (
                {"cell": name, "deployment_id": self.s.cells[name]["deployment_id"]}
                if name
                else None
            )
        assert sql.startswith("SELECT cell, is_first"), sql
        return self._row(a[0], sql)

    async def fetchval(self, sql: str, *a: Any) -> Any:
        self._count()
        if sql == directory._OTHER:
            return min((c for c in self.s.cells if c != a[0]), default=None)
        if "SET placed = placed + $2" in sql:
            self.s.cells[a[0]]["placed"] += a[1]
            return self.s.cells[a[0]]["placed"]
        if sql.startswith("INSERT INTO loom_router.placements"):
            if a[0] in self.s.placements:
                return None
            self.s.placements[a[0]] = a[1]
            return a[0]
        raise AssertionError(f"a statement the fake does not know: {sql}")

    async def fetch(self, sql: str, *a: Any) -> list[Any]:
        self._count()
        assert sql.startswith("INSERT INTO loom_router.placements"), sql
        new = [o for o in a[0] if o not in self.s.placements]
        self.s.placements.update(dict.fromkeys(new, a[1]))
        return [{"org": o} for o in new]

    def transaction(self) -> Any:
        conn = self

        class _Tx:
            async def __aenter__(self) -> None:
                conn._count()
                self.saved = copy.deepcopy(
                    (conn.s.cells, conn.s.hosts, conn.s.placements, conn.s.audit)
                )

            async def __aexit__(self, kind: Any, *_exc: Any) -> None:
                conn.s.statements += 1  # the commit, or the rollback
                if kind is not None:
                    conn.s.cells, conn.s.hosts, conn.s.placements, conn.s.audit = self.saved

        return _Tx()


class _Listener:
    """The cell's probe listener (port 7790) and main port (7777)."""

    def __init__(self) -> None:
        self.nonces: dict[str, Any] = dict(NONCES)
        self.upstream_id = "dep-1"
        self.asked: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.asked.append(str(request.url))
        if request.url.port == 7777:
            return httpx.Response(200, json={"deployment_id": self.upstream_id})
        answer = {
            "deployment_id": "dep-1",
            "nonces": self.nonces,
            "bucket": {"name": "docs", "prefix": "eu-1"},
        }
        return httpx.Response(200, json=answer)


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> _Store:
    monkeypatch.setattr(
        settings, "router_operator_sha256", hashlib.sha256(OPERATOR.encode()).hexdigest()
    )
    s = _Store(_cell_row())

    @contextlib.asynccontextmanager
    async def _connection(**_kw: Any) -> Any:
        yield _Conn(s)

    monkeypatch.setattr(directory, "_connection", _connection)
    return s


@pytest.fixture
def listener() -> _Listener:
    return _Listener()


@pytest.fixture
def client(store: _Store, listener: _Listener) -> TestClient:
    router = router_app.Router(probe_transport=httpx.MockTransport(listener))
    return TestClient(router_app.build_app(router))


def _call(
    client: TestClient, method: str, path: str = "", body: Any = None, token: str = OPERATOR
) -> Any:
    url = "/cells/v1/operator/cells/eu-1" + (f"/{path}" if path else "")
    headers = {"Authorization": f"Bearer {token}"}
    if body is None:
        return client.request(method, url, headers=headers)
    return client.request(method, url, json=body, headers=headers)


def _begin(**over: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "deployment_id": "dep-1",
        "current_fp": CURRENT,
        "next_fp": None,
        "keys_fp": "keys-1",
        "ch_allow_users": ["loom"],
        "ch_allow_user_directories": [],
        "ch_allow_objects": ["loom.*"],
        "ch_allow_replicas": [],
        "pg_allow_roles": ["loom"],
        "nonces": dict(NONCES),
    }
    body.update(over)
    return body


def _begun(client: TestClient) -> None:
    assert _call(client, "POST", "attach/begin", _begin()).status_code == 200


def _enforced(client: TestClient) -> None:
    _begun(client)
    r = _call(client, "POST", "attach/enforced", {"deployment_id": "dep-1"})
    assert r.status_code == 200


# The operator's read.


def test_the_operator_reads_the_cells_row(client: TestClient, store: _Store) -> None:
    r = _call(client, "GET")
    assert store.audit == [("operator", "cell_read", {"cell": "eu-1", "found": True})]
    assert r.status_code == 200
    assert r.json() == {
        "cell": "eu-1",
        "is_first": True,
        "host": "loom.example",
        "state": "attaching",
        "mode": "recording",
        "deployment_id": "dep-1",
        "placed_orgs": 0,
        "attach_started_at": None,
        "current_fp": None,
        "next_fp": None,
        "other": None,
    }
    # Any other row is named: every attach call refuses while it exists.
    store.cells["eu-2"] = _cell_row(cell="eu-2", is_first=False, host="eu-2.example")
    assert _call(client, "GET").json()["other"] == "eu-2"


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", ""),
        ("DELETE", ""),
        ("POST", "attach/begin"),
        ("POST", "attach/place"),
        ("POST", "attach/enforced"),
        ("POST", "attach/serving"),
    ],
)
def test_every_new_route_needs_the_operator_credential_and_names_an_unknown_cell(
    client: TestClient, store: _Store, method: str, path: str
) -> None:
    body = {
        "attach/begin": _begin(),
        "attach/place": {"deployment_id": "dep-1", "orgs": ["acme"]},
        "attach/enforced": {"deployment_id": "dep-1"},
        "attach/serving": {"deployment_id": "dep-1", "nonces": dict(NONCES)},
    }.get(path)
    assert _call(client, method, path, body, token="lco_" + "0" * 128).status_code == 401
    assert store.statements == 0
    url = "/cells/v1/operator/cells/eu-9" + (f"/{path}" if path else "")
    headers = {"Authorization": f"Bearer {OPERATOR}"}
    r = client.request(method, url, headers=headers, json=body)
    assert (r.status_code, r.json()["code"]) == (404, "NO_SUCH_CELL")
    bad = client.request(method, url.replace("eu-9", "EU_9"), headers=headers, json=body)
    assert (bad.status_code, bad.json()["code"]) == (404, "NO_SUCH_CELL")


def test_an_unreadable_directory_answers_503_with_its_code(
    client: TestClient, store: _Store
) -> None:
    store.down = True
    for method, path, body in (
        ("GET", "", None),
        ("POST", "attach/begin", _begin()),
        ("POST", "attach/enforced", {"deployment_id": "dep-1"}),
        ("DELETE", "", None),
    ):
        r = _call(client, method, path, body)
        assert (r.status_code, r.json()["code"]) == (503, "BACKEND_UNAVAILABLE")


# attach/begin.


def test_begin_writes_its_start_the_fingerprints_and_the_keys_and_audits_the_allow_lists(
    client: TestClient, store: _Store, listener: _Listener
) -> None:
    r = _call(client, "POST", "attach/begin", _begin())
    assert r.status_code == 200
    answer = r.json()
    assert answer["attach_started_at"] == NOW.isoformat()
    assert (answer["current_fp"], answer["next_fp"]) == (CURRENT, None)
    assert store.cells["eu-1"]["keys_fp"] == "keys-1"
    # The probe at the row's probe address, then the upstream at the row's.
    assert listener.asked == [
        "http://upstream.internal:7790/v1/health/deployment",
        "http://upstream.internal:7777/v1/health/deployment",
    ]
    assert store.audit == [
        (
            "operator",
            "attach_begin",
            {
                "cell": "eu-1",
                "deployment_id": "dep-1",
                "keys_fp": "keys-1",
                "fingerprints_written": True,
                "ch_allow_users": ["loom"],
                "ch_allow_user_directories": [],
                "ch_allow_objects": ["loom.*"],
                "ch_allow_replicas": [],
                "pg_allow_roles": ["loom"],
            },
        )
    ]


def test_begin_is_idempotent_and_takes_up_a_new_keys_fingerprint(
    client: TestClient, store: _Store
) -> None:
    _begun(client)
    again = _call(client, "POST", "attach/begin", _begin(keys_fp="keys-2"))
    assert again.status_code == 200
    row = store.cells["eu-1"]
    assert (row["attach_started_at"], row["current_fp"], row["keys_fp"]) == (
        NOW,
        CURRENT,
        "keys-2",
    )
    assert store.audit[-1][2]["fingerprints_written"] is False


def test_begin_refuses_other_fingerprints_once_the_row_has_them(
    client: TestClient, store: _Store
) -> None:
    _begun(client)
    other = cell.as_value({**FIELDS, "oidc_issuer": "https://idp.example"})
    for over in ({"current_fp": other}, {"next_fp": other}):
        r = _call(client, "POST", "attach/begin", _begin(**over))
        assert r.status_code == 409 and "loom cell update" in r.json()["error"]
    assert store.cells["eu-1"]["current_fp"] == CURRENT and store.cells["eu-1"]["next_fp"] is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("deployment_id", "dep-2"),
        ("bucket", "other"),
        ("bucket_prefix", "eu-2"),
        ("public_host", "eu-2.loom.example"),
    ],
)
def test_begin_refuses_a_fingerprint_of_other_stores_or_another_host(
    client: TestClient, store: _Store, listener: _Listener, field: str, value: str
) -> None:
    for name in ("current_fp", "next_fp"):
        fp = cell.as_value({**FIELDS, field: value})
        r = _call(client, "POST", "attach/begin", _begin(**{name: fp}))
        assert r.status_code == 409
        assert r.json()["error"].startswith(f"{name}'s {field} differ")
    assert listener.asked == [] and store.audit == []
    assert store.cells["eu-1"]["attach_started_at"] is None


def test_begin_refuses_an_issuer_host_other_than_the_cells(
    client: TestClient, store: _Store
) -> None:
    fp = cell.as_value({**FIELDS, "oauth_issuer_host": "auth.loom.example"})
    r = _call(client, "POST", "attach/begin", _begin(current_fp=fp))
    assert r.status_code == 409 and "no issuer host other than" in r.json()["error"]
    missing = cell.as_value({k: v for k, v in FIELDS.items() if k != "public_host"})
    r = _call(client, "POST", "attach/begin", _begin(current_fp=missing))
    assert r.status_code == 409 and "public_host differ" in r.json()["error"]  # at its default
    newer = {"v": cell.FINGERPRINT_VERSION + 1, "fields": FIELDS}
    r = _call(client, "POST", "attach/begin", _begin(current_fp=newer))
    assert r.status_code == 409 and "no fingerprint of version" in r.json()["error"]


@pytest.mark.parametrize(
    "over,why",
    [
        ({"is_first": False}, "later cell"),
        ({"state": "serving"}, "serving already"),
        ({"deployment_id": "dep-9"}, "deployment id 'dep-9'"),
    ],
)
def test_begin_refuses_a_row_it_cannot_attach(
    client: TestClient, store: _Store, listener: _Listener, over: dict[str, Any], why: str
) -> None:
    store.cells["eu-1"].update(over)
    r = _call(client, "POST", "attach/begin", _begin())
    assert r.status_code == 409 and why in r.json()["error"]
    assert listener.asked == [] and store.audit == []


def test_every_attach_call_refuses_while_another_row_exists(
    client: TestClient, store: _Store
) -> None:
    _enforced(client)
    store.cells["eu-2"] = _cell_row(cell="eu-2", is_first=False, host="eu-2.example")
    for path, body in (
        ("attach/begin", _begin()),
        ("attach/place", {"deployment_id": "dep-1", "orgs": ["acme"]}),
        ("attach/enforced", {"deployment_id": "dep-1"}),
        ("attach/serving", {"deployment_id": "dep-1", "nonces": dict(NONCES)}),
    ):
        r = _call(client, "POST", path, body)
        assert r.status_code == 409 and "loom cell remove --cell eu-2" in r.json()["error"], path
    assert store.placements == {}


def test_begin_writes_nothing_when_the_probe_or_upstream_does_not_answer_the_cell(
    client: TestClient, store: _Store, listener: _Listener
) -> None:
    listener.nonces = {**NONCES, "postgres": "n-other"}  # a restored copy's Postgres
    r = _call(client, "POST", "attach/begin", _begin())
    assert r.status_code == 409
    assert r.json()["error"] == "the probe listener does not read the stores attach wrote to"
    listener.nonces = dict(NONCES)
    listener.upstream_id = "dep-2"  # the upstream routing uses is another deployment now
    r = _call(client, "POST", "attach/begin", _begin())
    # Attach names the addresses the row records, not the router's settings.
    assert r.status_code == 409 and r.json()["error"] == (
        "the directory row's upstream_url reports another deployment id than its probe_url: "
        "they must name one deployment"
    )
    assert store.statements == 4  # two reads before the probe, twice: nothing written
    assert store.cells["eu-1"]["attach_started_at"] is None and store.audit == []


@pytest.mark.parametrize(
    "over",
    [
        {"deployment_id": ""},
        {"keys_fp": 7},
        {"current_fp": None},
        {"next_fp": "x"},
        {"ch_allow_users": "loom"},
        {"pg_allow_roles": ["ok", "a\x00b"]},
        {"nonces": {"postgres": "a"}},
        {"extra": 1},
    ],
)
def test_a_malformed_begin_answers_400_and_reads_nothing(
    client: TestClient, store: _Store, over: dict[str, Any]
) -> None:
    r = _call(client, "POST", "attach/begin", _begin(**over))
    assert r.status_code == 400 and store.statements == 0


def test_begins_body_has_its_own_bound(client: TestClient, store: _Store) -> None:
    # Past the placement bound and within begin's: read (the allow lists are long).
    long_lists = [f"db{i:06d}.*" for i in range(MAX_BODY_BYTES // 10)]
    body = _begin(ch_allow_objects=long_lists)
    assert MAX_BODY_BYTES < len(json.dumps(body)) < ATTACH_BEGIN_MAX_BYTES
    assert _call(client, "POST", "attach/begin", body).status_code == 200
    too_long = _begin(ch_allow_objects=long_lists * 5)
    assert len(json.dumps(too_long)) > ATTACH_BEGIN_MAX_BYTES
    r = _call(client, "POST", "attach/begin", too_long)
    assert r.status_code == 413 and str(ATTACH_BEGIN_MAX_BYTES) in r.json()["error"]


def test_begin_refuses_a_fingerprint_whose_placement_body_would_pass_the_routers_bound(
    client: TestClient, store: _Store, listener: _Listener
) -> None:
    grown = cell.as_value({**FIELDS, "configured_orgs": [f"org-{i:05d}" for i in range(4500)]})
    body = json.dumps({"org": "", "cell": "eu-1", "fingerprint": grown})
    assert len(body) + 6 * cell.ORG_MAX_BYTES > MAX_BODY_BYTES > len(body)
    r = _call(client, "POST", "attach/begin", _begin(current_fp=grown))
    assert r.status_code == 409
    assert "configured_orgs" in r.json()["error"] and str(MAX_BODY_BYTES) in r.json()["error"]
    assert store.statements == directory.ATTACH_READ_STATEMENTS and listener.asked == []
    # Judged on the row: an unknown cell is named as such first.
    url = "/cells/v1/operator/cells/eu-9/attach/begin"
    unknown = client.post(
        url, json=_begin(current_fp=grown), headers={"Authorization": f"Bearer {OPERATOR}"}
    )
    assert (unknown.status_code, unknown.json()["code"]) == (404, "NO_SUCH_CELL")
    # And again under the lock.
    big = directory.Begin("dep-1", grown, None, "keys", {})
    with pytest.raises(directory.OperatorRefused, match="configured_orgs"):
        asyncio.run(directory.attach_begin("eu-1", big))
    assert store.cells["eu-1"]["attach_started_at"] is None and store.audit == []
    # One that fits, with the longest org escaped, is taken.
    fits = cell.as_value({**FIELDS, "configured_orgs": [f"org-{i:05d}" for i in range(100)]})
    assert _call(client, "POST", "attach/begin", _begin(current_fp=fits)).status_code == 200


# attach/place.


def test_place_refuses_before_begin_and_before_enforced(client: TestClient, store: _Store) -> None:
    batch = {"deployment_id": "dep-1", "orgs": ["acme"]}
    r = _call(client, "POST", "attach/place", batch)
    assert r.status_code == 409 and "has not begun" in r.json()["error"]
    _begun(client)
    r = _call(client, "POST", "attach/place", batch)
    assert r.status_code == 409 and "post attach/enforced first" in r.json()["error"]
    assert store.placements == {} and store.cells["eu-1"]["placed"] == 0


def test_a_batch_places_each_org_once_with_an_audit_row_each(
    client: TestClient, store: _Store
) -> None:
    _enforced(client)
    r = _call(client, "POST", "attach/place", {"deployment_id": "dep-1", "orgs": ["b", "a", "a"]})
    assert r.status_code == 200 and r.json() == {"cell": "eu-1", "placed": 2, "placed_orgs": 2}
    again = _call(client, "POST", "attach/place", {"deployment_id": "dep-1", "orgs": ["a", "c"]})
    assert again.json() == {"cell": "eu-1", "placed": 1, "placed_orgs": 3}
    assert store.placements == {"a": "eu-1", "b": "eu-1", "c": "eu-1"}
    placed = [d["org"] for _actor, action, d in store.audit if action == "place"]
    assert placed == ["a", "b", "c"]


def test_a_batch_places_past_a_closed_mark_and_a_quota(client: TestClient, store: _Store) -> None:
    _enforced(client)
    store.cells["eu-1"].update(closed=True, placements_max=0)
    r = _call(client, "POST", "attach/place", {"deployment_id": "dep-1", "orgs": ["a", "b"]})
    assert r.status_code == 200 and r.json()["placed_orgs"] == 2


def test_a_batch_naming_an_org_placed_elsewhere_writes_nothing(
    client: TestClient, store: _Store
) -> None:
    _enforced(client)
    store.placements["b"] = "gone-cell"
    audited = len(store.audit)
    r = _call(client, "POST", "attach/place", {"deployment_id": "dep-1", "orgs": ["a", "b"]})
    assert r.status_code == 409 and "'b' is placed in cell 'gone-cell'" in r.json()["error"]
    assert store.placements == {"b": "gone-cell"} and store.cells["eu-1"]["placed"] == 0
    assert len(store.audit) == audited


def test_a_batch_is_bounded_by_count_and_bytes(client: TestClient, store: _Store) -> None:
    _enforced(client)
    statements = store.statements
    for orgs in ([], [f"o{i}" for i in range(ATTACH_BATCH + 1)], ["ok", ""], ["ok", 7]):
        r = _call(client, "POST", "attach/place", {"deployment_id": "dep-1", "orgs": orgs})
        assert r.status_code == 400
    full = [f"o{i}" for i in range(ATTACH_BATCH)]
    assert (
        _call(client, "POST", "attach/place", {"deployment_id": "dep-1", "orgs": full}).json()[
            "placed"
        ]
        == ATTACH_BATCH
    )
    wide = ["x" * 200 + str(i) for i in range(400)]  # within the count, past 64 KiB
    r = _call(client, "POST", "attach/place", {"deployment_id": "dep-1", "orgs": wide})
    assert r.status_code == 413
    assert store.statements == statements + directory.ATTACH_PLACE_STATEMENTS


def test_a_batch_with_an_org_past_the_limit_answers_400_and_places_none(
    client: TestClient, store: _Store
) -> None:
    _enforced(client)
    long = "é" * (cell.ORG_MAX_BYTES // 2 + 1)
    r = _call(client, "POST", "attach/place", {"deployment_id": "dep-1", "orgs": ["a", long]})
    assert (r.status_code, r.json()["code"]) == (400, "ORG_TOO_LONG")
    assert store.placements == {}


# attach/enforced and attach/serving.


def test_enforced_refuses_before_begin_and_is_idempotent(client: TestClient, store: _Store) -> None:
    r = _call(client, "POST", "attach/enforced", {"deployment_id": "dep-1"})
    assert r.status_code == 409 and "has not begun" in r.json()["error"]
    _enforced(client)
    again = _call(client, "POST", "attach/enforced", {"deployment_id": "dep-1"})
    assert again.status_code == 200 and again.json()["mode"] == "enforced"
    changes = [d["changed"] for _a, action, d in store.audit if action == "attach_enforced"]
    assert changes == [True, False]


def test_serving_needs_enforced_then_sets_the_issuers_and_is_done(
    client: TestClient, store: _Store, listener: _Listener
) -> None:
    serving = {"deployment_id": "dep-1", "nonces": dict(NONCES)}
    _begun(client)
    r = _call(client, "POST", "attach/serving", serving)
    assert r.status_code == 409 and "post attach/enforced first" in r.json()["error"]
    _call(client, "POST", "attach/enforced", {"deployment_id": "dep-1"})
    # An update while the lock was lost left a next value: its issuers route too.
    store.cells["eu-1"]["next_fp"] = cell.as_value(
        {**FIELDS, "bearer_issuers": ["https://loom.example", "https://ci.example"]}
    )
    asked = len(listener.asked)
    r = _call(client, "POST", "attach/serving", serving)
    assert r.status_code == 200 and r.json()["state"] == "serving"
    assert len(listener.asked) == asked + 2  # the probe and the upstream check again
    assert store.cells["eu-1"]["issuers"] == ["https://ci.example", "https://loom.example"]
    assert store.audit[-1] == (
        "operator",
        "attach_serving",
        {"cell": "eu-1", "issuers": ["https://ci.example", "https://loom.example"]},
    )
    done = _call(client, "POST", "attach/serving", serving)
    assert done.status_code == 200 and len(listener.asked) == asked + 2
    assert store.audit[-1] == (
        "operator",
        "attach_serving",
        {"cell": "eu-1", "serving_already": True},
    )
    for path, body in (
        ("attach/begin", _begin()),
        ("attach/place", {"deployment_id": "dep-1", "orgs": ["a"]}),
        ("attach/enforced", {"deployment_id": "dep-1"}),
    ):
        refused = _call(client, "POST", path, body)
        assert refused.status_code == 409 and "serving already" in refused.json()["error"]


def test_serving_writes_nothing_when_the_probe_fails(
    client: TestClient, store: _Store, listener: _Listener
) -> None:
    _enforced(client)
    listener.upstream_id = "dep-2"
    r = _call(client, "POST", "attach/serving", {"deployment_id": "dep-1", "nonces": NONCES})
    assert r.status_code == 409 and store.cells["eu-1"]["state"] == "attaching"


def test_an_issuer_list_is_read_from_both_fingerprints() -> None:
    row = directory._cell(
        {
            k: v
            for k, v in _cell_row(
                current_fp=cell.as_value({"bearer_issuers": ["b", "a", 3]}),
                next_fp={"v": 1, "fields": {"bearer_issuers": ["c", "a"]}},
            ).items()
            if k in _COLUMNS
        }
    )
    assert directory.issuers(row) == ["a", "b", "c"]


def test_every_issuer_a_serving_cell_accepts_routes_its_tokens_to_it(
    client: TestClient, store: _Store
) -> None:
    """The issuers `attach/serving` writes are the fingerprints' `bearer_issuers`, what the
    cell's verifiers accept: a token from each one routes to the cell."""
    import base64

    from server.router import routing

    def _jwt(iss: str) -> str:
        claims = base64.urlsafe_b64encode(json.dumps({"iss": iss}).encode()).decode()
        return f"e30.{claims.rstrip('=')}.sig"

    accepted = ["https://loom.example", "https://ci.example", "https://idp.example/end-users"]
    _begun(client)
    store.cells["eu-1"]["next_fp"] = cell.as_value({**FIELDS, "bearer_issuers": accepted[1:]})
    _call(client, "POST", "attach/enforced", {"deployment_id": "dep-1"})
    r = _call(client, "POST", "attach/serving", {"deployment_id": "dep-1", "nonces": NONCES})
    assert r.status_code == 200
    row = store.cells["eu-1"]
    assert sorted(row["issuers"]) == sorted(accepted)
    table = routing.build(
        [{k: row[k] for k in ("cell", "host", "state", "is_first", "issuers", "upstream_url")}],
        [],
        shared_host="loom.example",
        passthrough="",
    )
    for iss in accepted:
        d = routing.decide(
            table, host="loom.example", path="/v1/memory.set", authorization=f"Bearer {_jwt(iss)}"
        )
        assert d.upstream == "http://upstream.internal:7777", iss
    other = routing.decide(
        table,
        host="loom.example",
        path="/v1/memory.set",
        authorization=f"Bearer {_jwt('https://elsewhere.example')}",
    )
    assert other.status == 401


# The removal of an unattached cell.


def test_remove_deletes_an_attaching_row_and_its_hosts_whatever_its_mode(
    client: TestClient, store: _Store
) -> None:
    _enforced(client)  # a step-3 lock that never landed leaves the directory `enforced`
    store.hosts["alias.loom.example"] = "eu-1"
    r = _call(client, "DELETE")
    assert r.status_code == 200 and r.json() == {"cell": "eu-1", "removed": True}
    assert store.cells == {} and store.hosts == {}
    assert store.audit[-1] == (
        "operator",
        "remove",
        {
            "cell": "eu-1",
            "is_first": True,
            "host": "loom.example",
            "deployment_id": "dep-1",
            "mode": "enforced",
        },
    )
    gone = _call(client, "DELETE")
    assert (gone.status_code, gone.json()["code"]) == (404, "NO_SUCH_CELL")


@pytest.mark.parametrize(
    "over,why", [({"state": "serving"}, "stays attached"), ({"placed": 1}, "never back")]
)
def test_remove_refuses_a_serving_or_placed_cell(
    client: TestClient, store: _Store, over: dict[str, Any], why: str
) -> None:
    store.cells["eu-1"].update(over)
    r = _call(client, "DELETE")
    assert r.status_code == 409 and why in r.json()["error"]
    assert "eu-1" in store.cells


# Registration's order.


def test_a_later_cell_registers_only_once_the_first_is_serving() -> None:
    reg = directory.Registration(
        cell="eu-2",
        host="eu-2.example",
        deployment_id="dep-2",
        credential_sha256="c" * 64,
        bucket="",
        bucket_prefix="",
        probe_url="http://eu-2.internal:7790",
        is_first=False,
        tenancy_rows=0,
        state_rows=0,
        prefix_objects=0,
        processes_without_cell=0,
    )
    attaching = directory._order_refusal(reg, {"cell": "eu-1", "state": "attaching"})
    assert "'eu-1' is not serving yet" in attaching
    assert directory._order_refusal(reg, {"cell": "eu-1", "state": "serving"}) == ""


# The bounds count the statements the code sends.


def test_each_attach_calls_bound_counts_the_statements_it_sends(store: _Store) -> None:
    def sent(call: Any) -> int:
        before = store.statements
        asyncio.run(call)
        return store.statements - before

    begin = directory.Begin("dep-1", CURRENT, None, "keys", {})
    assert sent(directory.operator_read("eu-1")) == directory.OPERATOR_READ_STATEMENTS
    assert sent(directory.attach_read("eu-1")) == directory.ATTACH_READ_STATEMENTS
    assert sent(directory.attach_begin("eu-1", begin)) == directory.ATTACH_CHANGE_STATEMENTS
    assert sent(directory.attach_enforced("eu-1", "dep-1")) == directory.ATTACH_CHANGE_STATEMENTS
    batch = directory.attach_place("eu-1", "dep-1", [f"o{i}" for i in range(50)])
    assert sent(batch) == directory.ATTACH_PLACE_STATEMENTS  # whatever the batch's size
    assert sent(directory.attach_serving("eu-1", "dep-1")) == directory.ATTACH_CHANGE_STATEMENTS
    store.cells["eu-2"] = _cell_row(cell="eu-2", is_first=False, host="eu-2.example")
    assert sent(directory.remove("eu-2")) == directory.REMOVE_STATEMENTS
    assert directory.REMOVE_MAX_S == directory.STATEMENT_TIMEOUT_S + 8 * 11


def test_a_live_placement_reads_the_winner_again_under_the_row_lock(store: _Store) -> None:
    """A batch holds the cell's row lock, not the org's: an org it placed between the live
    placement's first read and its row lock is answered placed, never judged on the quota,
    counted or audited twice."""
    store.cells["eu-1"].update(state="serving", placements_max=0)
    reads = 0
    real = _Conn.fetchrow

    async def _fetchrow(self: _Conn, sql: str, *a: Any) -> Any:
        nonlocal reads
        if sql.startswith("SELECT cell, is_first") and "FOR UPDATE" in sql:
            self.s.placements["acme"] = "eu-1"  # the batch committed while this waited
        if sql.startswith("SELECT p.cell"):
            reads += 1
        return await real(self, sql, *a)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(_Conn, "fetchrow", _fetchrow)
        winner = asyncio.run(directory.place("acme", "eu-1", lambda _row: ""))
    assert winner == ("eu-1", "dep-1") and reads == 2
    assert store.cells["eu-1"]["placed"] == 0 and store.audit == []


def test_a_live_insert_that_loses_to_the_primary_key_counts_nothing(store: _Store) -> None:
    store.cells["eu-1"].update(state="serving")
    real = _Conn.fetchval

    async def _fetchval(self: _Conn, sql: str, *a: Any) -> Any:
        if sql.startswith("INSERT INTO loom_router.placements"):
            self.s.placements[a[0]] = a[1]  # another writer's row, past every read
        return await real(self, sql, *a)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(_Conn, "fetchval", _fetchval)
        assert asyncio.run(directory.place("acme", "eu-1", lambda _row: "")) == ("eu-1", "dep-1")
    assert store.cells["eu-1"]["placed"] == 0 and store.audit == []


# The attach lock and the troubleshooting page.


def test_the_attach_lock_is_a_positive_bigint_the_troubleshooting_page_prints() -> None:
    from server.core.schema import pg_migrate

    key = cell.ATTACH_LOCK_KEY
    assert 0 < key < 2**63 and key not in (pg_migrate._LOCK_KEY, directory._LOCK_KEY)
    page = (
        Path(cell.__file__).resolve().parents[2] / "docs/operations/troubleshooting.md"
    ).read_text()
    section = page[page.index("### A process with LOOM_CELL unset is gated") :]
    state_step = section.index(f"SELECT pg_advisory_xact_lock({key});  -- the attach lock")
    assert state_step < section.index(f"SELECT pg_advisory_xact_lock({pg_migrate._LOCK_KEY})")
    # The holder query splits the key into its two halves.
    assert f"classid = ({key} >> 32)::oid" in section
    assert f"objid = ({key} & 4294967295)::oid" in section


# `loom cell remove`.


def _remove_args(**kw: Any) -> argparse.Namespace:
    return argparse.Namespace(**{"cell": "eu-1", "router": "https://router.test", **kw})


class _Router:
    """The router's answers to the CLI: the GET of the row, then the DELETE."""

    def __init__(self, row: dict[str, Any], status: int = 200) -> None:
        self.row, self.status = row, status
        self.calls: list[tuple[str, str]] = []

    def get(self, url: str, **_kw: Any) -> httpx.Response:
        self.calls.append(("GET", url))
        return httpx.Response(self.status, json=self.row, request=httpx.Request("GET", url))

    def delete(self, url: str, **_kw: Any) -> httpx.Response:
        self.calls.append(("DELETE", url))
        return httpx.Response(200, json={"cell": "eu-1", "removed": True})


@pytest.fixture
def cli_router(monkeypatch: pytest.MonkeyPatch) -> _Router:
    monkeypatch.setattr(settings, "router_operator_token", OPERATOR)
    fake = _Router(
        {
            "cell": "eu-1",
            "is_first": True,
            "state": "attaching",
            "placed_orgs": 0,
            "deployment_id": "dep-1",
        }
    )
    monkeypatch.setattr(httpx, "get", fake.get)
    monkeypatch.setattr(httpx, "delete", fake.delete)
    return fake


def _stores(monkeypatch: pytest.MonkeyPatch, refusal: Any) -> list[str]:
    """The first-cell check with its answer (a refusal, or an exception); the deployment ids
    it was asked about."""
    checked: list[str] = []

    async def _check(deployment_id: str) -> str:
        checked.append(deployment_id)
        if isinstance(refusal, Exception):
            raise refusal
        return refusal

    monkeypatch.setattr(cell_cli, "_first_cell_refusal", _check)
    return checked


def test_remove_checks_a_first_cells_postgres_then_deletes_its_row(
    cli_router: _Router, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    checked = _stores(monkeypatch, "")
    assert cell_cli._cmd_remove(_remove_args()) == 0
    assert checked == ["dep-1"]
    assert [m for m, _url in cli_router.calls] == ["GET", "DELETE"]
    assert cli_router.calls[1][1] == "https://router.test/cells/v1/operator/cells/eu-1"
    assert json.loads(capsys.readouterr().out) == {"cell": "eu-1", "removed": True}


@pytest.mark.parametrize(
    "refusal",
    [
        "the first cell's Postgres reads `recording`: unset LOOM_CELL",
        OSError("connection refused"),
    ],
)
def test_remove_refuses_a_first_cell_its_postgres_does_not_clear(
    cli_router: _Router, monkeypatch: pytest.MonkeyPatch, capsys: Any, refusal: Any
) -> None:
    _stores(monkeypatch, refusal)
    assert cell_cli._cmd_remove(_remove_args()) == 1
    assert "refusing" in capsys.readouterr().err
    assert [m for m, _url in cli_router.calls] == ["GET"]


def test_remove_reads_no_store_for_a_later_cell(
    cli_router: _Router, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli_router.row = {**cli_router.row, "is_first": False, "deployment_id": "dep-2"}
    checked = _stores(monkeypatch, "never asked")
    assert cell_cli._cmd_remove(_remove_args()) == 0 and checked == []


@pytest.mark.parametrize(
    "over,why",
    [
        ({"state": "serving"}, "is serving: a cell stays attached"),
        ({"placed_orgs": 1}, "holds 1 placement(s): its attach goes forward, never back"),
        ({"placed_orgs": None}, "names no state or placement count"),
    ],
)
def test_remove_refuses_a_live_or_placed_cell_before_reading_its_postgres(
    cli_router: _Router,
    monkeypatch: pytest.MonkeyPatch,
    capsys: Any,
    over: dict[str, Any],
    why: str,
) -> None:
    """A first cell that is serving or holds placements goes forward: the CLI never tells
    the operator to clear its state, and the router's own wording says why."""
    cli_router.row = {**cli_router.row, **over}
    checked = _stores(monkeypatch, "")
    assert cell_cli._cmd_remove(_remove_args()) == 1
    assert why in capsys.readouterr().err
    assert checked == [] and [m for m, _url in cli_router.calls] == ["GET"]


def test_remove_tells_an_older_router_from_an_unknown_cell(
    cli_router: _Router, capsys: Any
) -> None:
    cli_router.status, cli_router.row = 404, {"error": "the router answers no such path"}
    assert cell_cli._cmd_remove(_remove_args()) == 1
    assert "not upgraded" in capsys.readouterr().err
    cli_router.row = {"error": "cell 'eu-1' is not in the directory", "code": "NO_SUCH_CELL"}
    assert cell_cli._cmd_remove(_remove_args()) == 1
    assert "is not in the directory" in capsys.readouterr().err
    assert [m for m, _url in cli_router.calls] == ["GET", "GET"]


@pytest.mark.parametrize(
    "state,recorded,why",
    [
        ("recording", "dep-1", "reads `recording`"),
        ("partial", "dep-1", "reads `partial`"),
        ("open", "dep-2", "records deployment id 'dep-2'"),
        ("open", "dep-1", ""),
    ],
)
def test_the_first_cell_check_needs_an_open_postgres_of_the_rows_deployment(
    monkeypatch: pytest.MonkeyPatch, state: str, recorded: str, why: str
) -> None:
    """One Postgres session of its own reads both: no pool, and no ClickHouse."""
    from server.core import pg

    sessions: list[dict[str, Any]] = []

    class _Session:
        async def fetchval(self, sql: str) -> str:
            assert sql == "SELECT deployment_id FROM loom.authority"
            return recorded

    @contextlib.asynccontextmanager
    async def _dedicated(**kw: Any) -> Any:
        sessions.append(kw)
        yield _Session()

    async def _state(conn: Any) -> tuple[str, str]:
        assert isinstance(conn, _Session)
        return state, ""

    def _no_pool(*_a: Any, **_kw: Any) -> None:
        raise AssertionError("the check reads no pool")

    # LOOM_PG_DSN alone: the CLI's storage authority setting is not read.
    monkeypatch.setattr(settings, "storage_authority", "clickhouse")
    monkeypatch.setattr(settings, "pg_dsn", "postgresql://cell.internal/loom")
    monkeypatch.setattr(pg, "dedicated", _dedicated)
    monkeypatch.setattr(pg, "fetchrow", _no_pool)
    monkeypatch.setattr(pg, "fetchval", _no_pool)
    monkeypatch.setattr(cell, "read_state_if_present", _state)
    refusal = asyncio.run(cell_cli._first_cell_refusal("dep-1"))
    assert (why in refusal) if why else refusal == ""
    assert sessions == [{"statement_timeout_ms": cell_cli._CHECK_STATEMENT_MS}]
    monkeypatch.setattr(settings, "pg_dsn", "")
    assert "LOOM_PG_DSN" in asyncio.run(cell_cli._first_cell_refusal("dep-1"))


def test_remove_never_connects_clickhouse(
    cli_router: _Router, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.cli import _ch

    def _never(*_a: Any, **_kw: Any) -> None:
        raise AssertionError("loom cell remove connects no ClickHouse")

    async def _read() -> tuple[str, str]:
        return "open", "dep-1"

    monkeypatch.setattr(_ch, "run_connected", _never)
    monkeypatch.setattr(settings, "storage_authority", "postgres")
    monkeypatch.setattr(settings, "pg_dsn", "postgresql://cell.internal/loom")
    monkeypatch.setattr(cell_cli, "_read_first_cell", _read)
    assert cell_cli._cmd_remove(_remove_args()) == 0
