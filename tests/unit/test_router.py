"""The router's placement API and lookups (server/router/, docs/architecture/cells.md,
"The router"), against a fake directory."""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from starlette.testclient import TestClient

from server.cli import router as router_cli
from server.core import cell
from server.router import app as router_app
from server.router import directory
from server.router.limits import MAX_KEYS, Limiter
from server.settings import settings
from tests.unit.clock_fake import ManualClock

TOKEN = "cell-eu-1-credential-0123456789abcdef"
US_TOKEN = "cell-us-1-credential-0123456789abcdef"
FIELDS = {"public_host": "eu-1.example.test", "deployment_id": "dep-eu-1"}


def _row(**over: Any) -> directory.Cell:
    values: dict[str, Any] = {
        "cell": "eu-1",
        "is_first": False,
        "host": "eu-1.example.test",
        "state": "serving",
        "mode": "enforced",
        "closed": False,
        "placements_max": None,
        "placed": 0,
        "credential_sha256": hashlib.sha256(TOKEN.encode()).hexdigest(),
        "credential_next_sha256": None,
        "deployment_id": "dep-eu-1",
        "current_fp": cell.as_value(FIELDS),
        "next_fp": None,
    }
    values.update(over)
    return directory.Cell(**values)


class _Directory:
    """The directory's statements over two dicts."""

    def __init__(self, *rows: directory.Cell) -> None:
        self.cells = {r.cell: r for r in rows}
        self.placements: dict[str, str] = {}
        self.down = False
        self.credential_reads = 0
        self.pings = 0

    def _check(self) -> None:
        if self.down:
            raise directory.DirectoryUnavailable("the directory could not be read: OSError")

    async def credentials(self) -> dict[str, tuple[str, ...]]:
        self.credential_reads += 1
        self._check()
        return {
            name: tuple(h for h in (row.credential_sha256, row.credential_next_sha256) if h)
            for name, row in self.cells.items()
        }

    async def first_cell(self) -> directory.Cell | None:
        self._check()
        return next((r for r in self.cells.values() if r.is_first), None)

    async def first_cell_named(self, name: str) -> directory.Cell | None:
        self._check()
        row = self.cells.get(name)
        return row if row is not None and row.is_first else None

    async def place(self, org: str, name: str, refusal: Any) -> tuple[str, str]:
        self._check()
        if org not in self.placements:
            row = self.cells[name]  # the row as the placement locks it
            if reason := refusal(row):
                raise directory.PlacementRefused(reason)
            held = sum(1 for c in self.placements.values() if c == name)
            if row.placements_max is not None and held >= row.placements_max:
                raise directory.PlacementRefused(f"cell {name!r} holds its quota of orgs")
            self.placements[org] = name
        winner = self.placements[org]
        return winner, self.cells[winner].deployment_id

    async def lookup(self, org: str) -> tuple[str, str] | None:
        self._check()
        name = self.placements.get(org)
        return (name, self.cells[name].host) if name else None

    async def set_limit(self, name: str, **fields: Any) -> directory.Cell:
        self._check()
        given = {k: v for k, v in fields.items() if v is not directory.UNSET}
        self.cells[name] = dataclasses.replace(self.cells[name], **given)
        return self.cells[name]

    async def ping(self) -> None:
        self.pings += 1
        self._check()


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> _Directory:
    d = _Directory(
        _row(),
        _row(
            cell="us-1",
            host="us-1.example.test",
            deployment_id="dep-us-1",
            credential_sha256=hashlib.sha256(US_TOKEN.encode()).hexdigest(),
        ),
    )
    for name in (
        "credentials",
        "first_cell",
        "first_cell_named",
        "place",
        "lookup",
        "ping",
        "set_limit",
    ):
        monkeypatch.setattr(directory, name, getattr(d, name))
    return d


def _client(clock: ManualClock | None = None) -> TestClient:
    router = router_app.Router(clock=clock or ManualClock())
    return TestClient(router_app.build_app(router))


def _place(client: TestClient, org: str = "acme", *, token: str = TOKEN, **body: Any) -> Any:
    payload = {"org": org, "cell": "eu-1", "fingerprint": cell.as_value(FIELDS), **body}
    return client.post(
        "/cells/v1/placements", json=payload, headers={"Authorization": f"Bearer {token}"}
    )


def test_a_serving_cell_places_a_new_org_and_a_retry_answers_the_same(fake: _Directory) -> None:
    client = _client()
    first = _place(client)
    assert first.status_code == 200
    assert first.json() == {"cell": "eu-1", "deployment_id": "dep-eu-1"}
    assert _place(client).json() == first.json()
    assert fake.placements == {"acme": "eu-1"}


def test_an_org_placed_elsewhere_answers_its_cell(fake: _Directory) -> None:
    fake.placements["acme"] = "us-1"
    answer = _place(_client())
    assert answer.status_code == 200
    assert answer.json() == {"cell": "us-1", "deployment_id": "dep-us-1"}


@pytest.mark.parametrize("token", ["", "another-cells-credential-000000000000"])
def test_a_credential_that_is_not_the_cells_answers_401_and_places_nothing(
    fake: _Directory, token: str
) -> None:
    assert _place(_client(), token=token).status_code == 401
    assert fake.placements == {}


def test_a_credential_naming_another_cell_places_nothing_there(fake: _Directory) -> None:
    """The credential names its cell: eu-1's token cannot place an org in us-1."""
    r = _place(_client(), cell="us-1")
    assert r.status_code == 401 and fake.placements == {}


def test_a_cell_the_directory_does_not_hold_answers_401(fake: _Directory) -> None:
    assert _place(_client(), cell="eu-9").status_code == 401


@pytest.mark.parametrize(
    "body",
    [
        {"org": ""},
        {"org": "*"},
        {"org": "-"},
        {"org": "a\x00b"},
        {"org": 7},
        {"cell": "EU_1"},
        {"cell": None},
    ],
)
def test_a_malformed_placement_answers_400(fake: _Directory, body: dict[str, Any]) -> None:
    client = _client()
    payload = {"org": "acme", "cell": "eu-1", "fingerprint": cell.as_value(FIELDS), **body}
    r = client.post(
        "/cells/v1/placements", json=payload, headers={"Authorization": f"Bearer {TOKEN}"}
    )
    assert r.status_code == 400 and fake.placements == {}
    assert fake.credential_reads == 0  # judged before the credential is checked


def test_a_body_nested_past_the_parser_answers_400(fake: _Directory) -> None:
    r = _client().post(
        "/cells/v1/placements",
        content=b"[" * 30_000 + b"]" * 30_000,
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
    )
    assert r.status_code == 400


def test_an_org_postgres_cannot_hold_answers_400(fake: _Directory) -> None:
    """A lone surrogate decodes from JSON but encodes to no UTF-8: malformed, not an
    outage of the directory."""
    r = _client().post(
        "/cells/v1/placements",
        content=b'{"org": "a\\ud800b", "cell": "eu-1", "fingerprint": null}',
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
    )
    assert r.status_code == 400 and fake.placements == {}


def test_a_body_past_the_bound_is_refused_unread(fake: _Directory) -> None:
    r = _client().post(
        "/cells/v1/placements",
        content=b"{" + b" " * router_app.MAX_BODY_BYTES + b"}",
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
    )
    assert r.status_code == 400


@pytest.mark.parametrize(
    "over,reason",
    [
        ({"state": "attaching", "mode": "recording"}, "not attached yet"),
        ({"state": "attaching", "mode": "enforced"}, "not attached yet"),
        ({"is_first": True, "state": "attaching", "mode": "recording"}, "not attached yet"),
        ({"closed": True}, "closed to new orgs"),
        ({"current_fp": cell.as_value({**FIELDS, "public_host": "other"})}, "matches no value"),
        ({"placements_max": 0}, "quota"),
    ],
)
def test_a_cell_that_may_not_place_answers_503_and_places_nothing(
    fake: _Directory, over: dict[str, Any], reason: str
) -> None:
    fake.cells["eu-1"] = _row(**over)
    r = _place(_client())
    assert r.status_code == 503 and reason in r.json()["error"]
    assert fake.placements == {}


@pytest.mark.parametrize(
    "over",
    [
        {"closed": True, "placements_max": 0},
        {"state": "attaching", "mode": "recording"},
        {"current_fp": cell.as_value({**FIELDS, "public_host": "other"})},
    ],
)
def test_an_org_placed_already_answers_its_cell_whatever_the_callers_row(
    fake: _Directory, over: dict[str, Any]
) -> None:
    fake.placements["acme"] = "us-1"
    fake.cells["eu-1"] = _row(**over)
    assert _place(_client()).json() == {"cell": "us-1", "deployment_id": "dep-us-1"}


def test_an_org_placed_already_answers_its_cell_to_a_replaced_credential_still_cached(
    fake: _Directory,
) -> None:
    client = _client()
    assert _place(client, "o-1").status_code == 200
    fake.placements["acme"] = "us-1"
    replaced = "replaced-credential-0123456789abcdef"
    fake.cells["eu-1"] = _row(credential_sha256=hashlib.sha256(replaced.encode()).hexdigest())
    assert _place(client).json() == {"cell": "us-1", "deployment_id": "dep-us-1"}
    assert fake.credential_reads == 1


def test_a_wrong_credential_reads_the_directory_at_most_once_per_window(
    fake: _Directory,
) -> None:
    """Credentials are checked against the cells' hashes kept in memory: a flood of
    wrong ones reads the directory once, and locks no cell out."""
    client = _client()
    wrong = "wrong-credential-0000000000000000"
    assert {_place(client, f"o-{i}", token=wrong).status_code for i in range(20)} == {401}
    assert fake.credential_reads == 1
    assert _place(client).status_code == 200  # the cell's own call still places


def test_a_call_with_no_bearer_reads_nothing(fake: _Directory) -> None:
    r = _client().post("/cells/v1/placements", json={"org": "acme", "cell": "eu-1"})
    assert r.status_code == 401 and fake.credential_reads == 0


def test_readiness_reads_the_directory_at_most_once_a_second(fake: _Directory) -> None:
    clock = ManualClock()
    client = _client(clock)
    assert {client.get("/readyz").status_code for _ in range(10)} == {200}
    assert fake.pings == 1
    fake.down = True
    assert client.get("/readyz").status_code == 200  # the answer stands for its second
    clock.advance(router_app.READY_TTL_S)
    assert client.get("/readyz").status_code == 503 and fake.pings == 2


def test_a_new_credential_places_once_the_cache_window_passes(fake: _Directory) -> None:
    clock = ManualClock()
    client = _client(clock)
    assert _place(client).status_code == 200
    rotated = "rotated-credential-0123456789abcdef"
    fake.cells["eu-1"] = _row(credential_sha256=hashlib.sha256(rotated.encode()).hexdigest())
    assert _place(client, "o-2", token=rotated).status_code == 401  # still the cached hash
    clock.advance(settings.router_directory_cache_s)
    assert _place(client, "o-2", token=rotated).status_code == 200
    assert fake.credential_reads == 2


def test_a_replaced_credential_answers_401_within_the_cache_window(fake: _Directory) -> None:
    """The old credential still matches the cached hash, but the locked row holds the new
    one: the call answers 401, as for any credential the row does not hold, places
    nothing, and the next call reads the directory again."""
    client = _client()
    assert _place(client).status_code == 200
    replaced = "replaced-credential-0123456789abcdef"
    fake.cells["eu-1"] = _row(credential_sha256=hashlib.sha256(replaced.encode()).hexdigest())
    assert _place(client, "o-2").status_code == 401
    assert "o-2" not in fake.placements
    assert _place(client, "o-3", token=replaced).status_code == 200
    assert fake.credential_reads == 2


def test_an_unreadable_directory_is_read_once_per_window(fake: _Directory) -> None:
    clock = ManualClock()
    client = _client(clock)
    fake.down = True
    assert {_place(client, f"o-{i}").status_code for i in range(5)} == {503}
    assert fake.credential_reads == 1
    fake.down = False
    assert _place(client).status_code == 503  # the failure stands for its window
    clock.advance(settings.router_directory_cache_s)
    assert _place(client).status_code == 200


def test_the_first_cell_places_once_attach_set_enforced(fake: _Directory) -> None:
    fake.cells["eu-1"] = _row(is_first=True, state="attaching", mode="enforced")
    assert _place(_client()).status_code == 200


def test_a_value_matching_next_places(fake: _Directory) -> None:
    fake.cells["eu-1"] = _row(
        current_fp=cell.as_value({**FIELDS, "public_host": "old"}), next_fp=cell.as_value(FIELDS)
    )
    assert _place(_client()).status_code == 200


@pytest.mark.parametrize(
    "fingerprint",
    [
        None,
        {},
        {"v": cell.FINGERPRINT_VERSION + 1, "fields": FIELDS},
        {"v": 1, "fields": []},
        {"v": True, "fields": FIELDS},
        {"v": 1.0, "fields": FIELDS},
        # Fields left out are judged at their default, never skipped.
        {"v": 1, "fields": {}},
        {"v": 1, "fields": {"public_host": FIELDS["public_host"]}},
    ],
)
def test_a_fingerprint_of_another_shape_matches_nothing(fingerprint: Any) -> None:
    assert not router_app.fingerprint_matches(_row(), fingerprint)


def test_a_field_neither_the_row_nor_this_build_knows_is_a_newer_cells() -> None:
    """A cell newer than the router posts a field this build does not know: the row's
    fields still decide, as the cell's own gate judges the new one."""
    newer = cell.as_value({**FIELDS, "a_later_builds_field": "x"})
    assert router_app.fingerprint_matches(_row(), newer)
    # A field this build knows, at its default in the row, is compared.
    changed = cell.as_value({**FIELDS, "scim": True})
    assert not router_app.fingerprint_matches(_row(), changed)
    # A value equal only across types (`0` for `false`) is not the same value.
    changed = cell.as_value({**FIELDS, "scim": 0})
    assert not router_app.fingerprint_matches(_row(), changed)


def test_a_field_only_a_newer_rows_value_knows_is_left_to_the_cells_gate() -> None:
    """A row written by a newer build names a field this router does not know: an
    older pod, which does not send it, matches as its own gate accepts it; a pod that
    sends it is compared."""
    row = _row(current_fp=cell.as_value({**FIELDS, "a_later_builds_field": "x"}))
    assert router_app.fingerprint_matches(row, cell.as_value(FIELDS))
    assert router_app.fingerprint_matches(
        row, cell.as_value({**FIELDS, "a_later_builds_field": "x"})
    )
    assert not router_app.fingerprint_matches(
        row, cell.as_value({**FIELDS, "a_later_builds_field": "y"})
    )


async def test_a_lookup_flood_leaves_placements_a_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lookups hold at most LOOKUP_SLOTS connections, and a wait for a connection is
    bounded: a placement still gets one while lookups queue, a lookup past the bound
    answers the directory unavailable, and every slot comes back, a failed connection's
    included."""
    held: list[str] = []
    failing: list[bool] = []

    class _Pool:
        def acquire(self):
            @asynccontextmanager
            async def _cm():
                if failing:
                    raise ConnectionRefusedError("down")
                held.append("conn")
                try:
                    yield object()
                finally:
                    held.remove("conn")

            return _cm()

    monkeypatch.setattr(directory, "_pool", _Pool())
    monkeypatch.setattr(directory, "_lookups", asyncio.Semaphore(directory.LOOKUP_SLOTS))
    monkeypatch.setattr(directory, "STATEMENT_TIMEOUT_S", 0.05)
    release = asyncio.Event()

    async def _lookup() -> None:
        async with directory._connection(lookup=True):
            await release.wait()

    flood = [asyncio.create_task(_lookup()) for _ in range(directory.LOOKUP_SLOTS)]
    await asyncio.sleep(0)
    assert len(held) == directory.LOOKUP_SLOTS < directory.POOL_MAX
    async with directory._connection():  # a placement's
        assert len(held) == directory.LOOKUP_SLOTS + 1
    with pytest.raises(directory.DirectoryUnavailable):
        async with directory._reading(), directory._connection(lookup=True):
            pass
    release.set()
    await asyncio.gather(*flood)
    failing.append(True)
    for _ in range(directory.LOOKUP_SLOTS + 1):
        with pytest.raises(directory.DirectoryUnavailable):
            async with directory._reading(), directory._connection(lookup=True):
                pass
    assert held == [] and not directory._lookups.locked()
    failing.clear()
    release = asyncio.Event()
    flood = [asyncio.create_task(_lookup()) for _ in range(directory.LOOKUP_SLOTS)]
    await asyncio.sleep(0)
    assert len(held) == directory.LOOKUP_SLOTS  # every slot came back
    release.set()
    await asyncio.gather(*flood)


def test_an_unreadable_directory_answers_503(fake: _Directory) -> None:
    fake.down = True
    client = _client()
    assert _place(client).status_code == 503
    assert client.get("/cells/lookup", params={"org": "acme"}).status_code == 503
    assert client.get("/readyz").status_code == 503
    assert client.get("/livez").status_code == 200


def test_a_cell_past_its_rate_answers_503(
    fake: _Directory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "router_placements_per_minute", 2)
    client = _client()
    assert [_place(client, f"org-{i}").status_code for i in range(3)] == [200, 200, 503]
    assert set(fake.placements) == {"org-0", "org-1"}
    # A retry of an org placed already is counted too: past the rate it waits up to a minute,
    # and the refusal says how long.
    refused = _place(client, "org-0")
    assert refused.status_code == 503 and refused.headers["retry-after"] == "60"


def test_a_lookup_answers_the_orgs_cell_or_the_first(fake: _Directory) -> None:
    fake.placements["acme"] = "us-1"
    client = _client()
    assert client.get("/cells/lookup", params={"org": "acme"}).json() == {
        "org": "acme",
        "cell": "us-1",
        "host": "us-1.example.test",
    }
    none_yet = client.get("/cells/lookup", params={"org": "new"})
    assert none_yet.status_code == 503 and none_yet.json()["code"] == "NO_CELL"
    fake.cells["eu-1"] = _row(is_first=True)
    fake.placements["placed-first"] = "eu-1"
    unplaced = client.get("/cells/lookup", params={"org": "new"}).json()
    placed = client.get("/cells/lookup", params={"org": "placed-first"}).json()
    # The public lookup does not tell an org with no placement from one placed there.
    assert (
        {**unplaced, "org": "x"}
        == {**placed, "org": "x"}
        == {
            "org": "x",
            "cell": "eu-1",
            "host": "eu-1.example.test",
        }
    )
    assert client.get("/cells/lookup").status_code == 400
    assert client.get("/cells/lookup", params={"org": "a\x00b"}).status_code == 400


def test_lookups_are_limited_per_client(fake: _Directory, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "router_lookups_per_minute", 1)
    client = _client()
    assert client.get("/cells/lookup", params={"org": "acme"}).status_code == 503
    limited = client.get("/cells/lookup", params={"org": "acme"})
    assert limited.status_code == 429 and limited.headers["Retry-After"] == "60"


def test_the_limiter_admits_per_minute_per_key_and_forgets_the_idlest() -> None:
    clock = ManualClock()
    limiter = Limiter(2, clock=clock)
    assert [limiter.admit("a") for _ in range(3)] == [True, True, False]
    assert limiter.admit("b")
    clock.advance(60.0)
    assert limiter.admit("a")
    for i in range(MAX_KEYS + 1):
        limiter.admit(f"k{i}")
    assert len(limiter._calls) == MAX_KEYS


def test_the_limiter_says_when_a_key_is_admitted_again() -> None:
    clock = ManualClock()
    limiter = Limiter(2, clock=clock)
    limiter.admit("a")
    clock.advance(20.0)
    limiter.admit("a")
    assert not limiter.admit("a")
    assert limiter.wait_s("a") == 40  # the first call leaves the window then
    clock.advance(40.0)
    assert limiter.admit("a")


@pytest.mark.parametrize(
    "dsn,memory,refused",
    [
        ("", "", "set LOOM_ROUTER_PG_DSN"),
        ("postgres://h/loom", "postgres://h/loom", "is LOOM_PG_DSN"),
        ("postgres://h/router", "postgres://h/loom", ""),
    ],
)
def test_the_router_runs_only_against_a_directory_of_its_own(
    monkeypatch: pytest.MonkeyPatch, dsn: str, memory: str, refused: str
) -> None:
    monkeypatch.setattr(settings, "router_pg_dsn", dsn)
    monkeypatch.setattr(settings, "pg_dsn", memory)
    assert (refused in router_cli._refusal()) if refused else router_cli._refusal() == ""


def _state(client: TestClient, name: str = "eu-1", *, token: str = TOKEN) -> Any:
    return client.get(f"/cells/v1/cells/{name}", headers={"Authorization": f"Bearer {token}"})


@pytest.mark.parametrize("state", ["attaching", "serving"])
def test_the_first_cell_reads_its_own_state(fake: _Directory, state: str) -> None:
    fake.cells["eu-1"] = _row(is_first=True, state=state)
    assert _state(_client()).json() == {"cell": "eu-1", "state": state}


def test_a_state_read_is_answered_only_for_the_first_cell(fake: _Directory) -> None:
    r = _state(_client())  # eu-1 is a later cell here
    assert r.status_code == 404 and "not the first cell" in r.json()["error"]


@pytest.mark.parametrize(
    "path,token",
    [
        ("eu-1", "wrong-credential-0000000000000000"),
        ("eu-1", US_TOKEN),  # another cell's own credential
        ("EU_1", TOKEN),  # not a cell name
        ("nope", TOKEN),
    ],
)
def test_a_state_read_needs_the_named_cells_own_credential(
    fake: _Directory, path: str, token: str
) -> None:
    fake.cells["eu-1"] = _row(is_first=True, state="attaching")
    assert _state(_client(), path, token=token).status_code == 401


def test_a_state_read_with_no_bearer_reads_nothing(fake: _Directory) -> None:
    r = _client().get("/cells/v1/cells/eu-1")
    assert r.status_code == 401 and fake.credential_reads == 0


def test_a_replaced_credential_still_cached_is_refused_on_the_state_read(
    fake: _Directory,
) -> None:
    fake.cells["eu-1"] = _row(is_first=True, state="attaching")
    client = _client()
    assert _state(client).status_code == 200
    replaced = "replaced-credential-0123456789abcdef"
    sha = hashlib.sha256(replaced.encode()).hexdigest()
    fake.cells["eu-1"] = _row(is_first=True, state="attaching", credential_sha256=sha)
    assert _state(client).status_code == 401  # judged on the row, not the cache
    assert _state(client, token=replaced).status_code == 200
    assert fake.credential_reads == 2


def test_a_state_read_while_the_directory_is_down_answers_503(fake: _Directory) -> None:
    fake.down = True
    assert _state(_client()).status_code == 503


def test_a_state_read_with_its_credential_cached_answers_503_when_the_directory_goes_down(
    fake: _Directory,
) -> None:
    """The credential comes from the cache and the row read fails: the read answers 503
    with the directory's reason."""
    fake.cells["eu-1"] = _row(is_first=True)
    client = _client()
    assert _state(client).status_code == 200
    fake.down = True
    r = _state(client)
    assert r.status_code == 503
    assert r.json()["error"] == "the directory could not be read: OSError"
    assert fake.credential_reads == 1  # the credential came from the cache


def _lookup(client: TestClient, org: str) -> Any:
    return client.get("/cells/lookup", params={"org": org})


def test_a_placed_orgs_lookup_is_answered_from_the_cache_while_the_directory_is_down(
    fake: _Directory,
) -> None:
    """A placement never changes: a looked-up org keeps its answer through an outage,
    and an org not looked up yet answers 503 without `NO_CELL`."""
    fake.placements["acme"] = "us-1"
    clock = ManualClock()
    client = _client(clock)
    assert _lookup(client, "acme").json()["cell"] == "us-1"
    fake.down = True
    clock.advance(settings.router_directory_cache_s * 10)
    assert _lookup(client, "acme").json() == {
        "org": "acme",
        "cell": "us-1",
        "host": "us-1.example.test",
    }
    uncached = _lookup(client, "beta")
    assert uncached.status_code == 503 and uncached.json()["code"] == "BACKEND_UNAVAILABLE"


def test_a_cached_lookup_is_read_again_once_the_window_passes(
    fake: _Directory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The answer is read again each window, so a cell's new host reaches the lookup."""
    fake.placements["acme"] = "us-1"
    reads: list[str] = []
    real = fake.lookup

    async def _counted(org: str) -> Any:
        reads.append(org)
        return await real(org)

    monkeypatch.setattr(directory, "lookup", _counted)
    clock = ManualClock()
    client = _client(clock)
    assert _lookup(client, "acme").status_code == 200
    assert _lookup(client, "acme").status_code == 200
    assert reads == ["acme"]
    fake.cells["us-1"] = _row(cell="us-1", host="us-1b.example.test", deployment_id="dep-us-1")
    clock.advance(settings.router_directory_cache_s)
    assert _lookup(client, "acme").json()["host"] == "us-1b.example.test"
    assert reads == ["acme", "acme"]


def test_an_org_with_no_placement_is_not_cached(fake: _Directory) -> None:
    """It answers the first cell now, and its own cell once placed."""
    fake.cells["eu-1"] = _row(is_first=True)
    client = _client()
    assert _lookup(client, "new").json()["cell"] == "eu-1"
    fake.placements["new"] = "us-1"
    assert _lookup(client, "new").json()["cell"] == "us-1"


def test_the_lookup_cache_forgets_the_oldest_past_its_bound(
    fake: _Directory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(router_app, "MAX_KEYS", 2)
    for org in ("a1", "a2", "a3"):
        fake.placements[org] = "us-1"
    client = _client()
    for org in ("a1", "a2", "a3"):
        assert _lookup(client, org).status_code == 200
    fake.down = True  # only the cache answers now
    assert _lookup(client, "a1").status_code == 503
    assert [_lookup(client, o).json()["cell"] for o in ("a2", "a3")] == ["us-1", "us-1"]


def test_a_down_directory_is_read_at_most_once_a_window_per_cached_org(
    fake: _Directory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stale answer is stamped again, so an outage does not cost a directory read
    (and its timeout) on every lookup of a cached org."""
    fake.placements["acme"] = "us-1"
    reads: list[str] = []
    real = fake.lookup

    async def _counted(org: str) -> Any:
        reads.append(org)
        return await real(org)

    monkeypatch.setattr(directory, "lookup", _counted)
    clock = ManualClock()
    client = _client(clock)
    assert _lookup(client, "acme").status_code == 200
    fake.down = True
    clock.advance(settings.router_directory_cache_s)
    assert [_lookup(client, "acme").status_code for _ in range(3)] == [200] * 3
    assert reads == ["acme", "acme"]
    clock.advance(settings.router_directory_cache_s)
    assert _lookup(client, "acme").status_code == 200 and reads == ["acme"] * 3


def test_lookups_arriving_together_during_an_outage_make_one_directory_read(
    fake: _Directory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stale entry is stamped before its read, so lookups arriving while a slow,
    failing read runs get the cached answer instead of paying its timeout too."""
    fake.placements["acme"] = "us-1"
    reads: list[str] = []
    real = fake.lookup

    async def _slow(org: str) -> Any:
        reads.append(org)
        if fake.down:
            await asyncio.sleep(0.05)
        return await real(org)

    monkeypatch.setattr(directory, "lookup", _slow)
    clock = ManualClock()
    app = router_app.build_app(router_app.Router(clock=clock))

    async def _run() -> list[int]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://r") as c:
            assert (await c.get("/cells/lookup", params={"org": "acme"})).status_code == 200
            fake.down = True
            clock.advance(settings.router_directory_cache_s)
            answers = await asyncio.gather(
                *(c.get("/cells/lookup", params={"org": "acme"}) for _ in range(4))
            )
        return [r.status_code for r in answers]

    assert asyncio.run(_run()) == [200] * 4
    assert reads == ["acme", "acme"]


def test_a_cells_next_credential_places_and_reads_its_state_during_a_rotation(
    fake: _Directory,
) -> None:
    """The router accepts a cell's current and next credential until `--finish`."""
    new = "rotated-credential-0123456789abcdef"
    fake.cells["eu-1"] = _row(
        is_first=True, credential_next_sha256=hashlib.sha256(new.encode()).hexdigest()
    )
    client = _client()
    assert _place(client, "o-1").status_code == 200  # the current one
    assert _place(client, "o-2", token=new).status_code == 200  # and the next
    assert _state(client, token=new).json()["state"] == "serving"


def test_after_finish_the_old_credential_is_refused(fake: _Directory) -> None:
    new = "rotated-credential-0123456789abcdef"
    fake.cells["eu-1"] = _row(
        is_first=True, credential_sha256=hashlib.sha256(new.encode()).hexdigest()
    )
    client = _client()
    assert _place(client, "o-1").status_code == 401  # the old one, dropped by --finish
    assert _state(client).status_code == 401
    assert _place(client, "o-2", token=new).status_code == 200


def test_a_replica_still_caching_the_old_hash_refuses_it_on_the_locked_row(
    fake: _Directory,
) -> None:
    """Another replica took --finish: this one's cache still holds the old hash, so the
    old credential passes the cache and is refused by the row the placement locks."""
    new = "rotated-credential-0123456789abcdef"
    sha = hashlib.sha256(new.encode()).hexdigest()
    fake.cells["eu-1"] = _row(is_first=True, credential_next_sha256=sha)
    client = _client()
    assert _place(client, "o-1").status_code == 200  # caches (old, new)
    fake.cells["eu-1"] = _row(is_first=True, credential_sha256=sha)  # --finish elsewhere
    reads = fake.credential_reads
    assert _place(client, "o-2").status_code == 401
    assert _place(client, "o-3", token=new).status_code == 200
    assert fake.credential_reads > reads  # the refusal dropped the stale cache


def test_a_cell_the_operator_closes_refuses_its_next_new_org(
    fake: _Directory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The operator API and placement over one directory: a limit posted through the
    router is the row the next placement is judged on."""
    operator = "lco_" + "7" * 128
    monkeypatch.setattr(
        settings, "router_operator_sha256", hashlib.sha256(operator.encode()).hexdigest()
    )
    client = _client()
    assert _place(client, "before").status_code == 200
    closed = client.post(
        "/cells/v1/operator/cells/eu-1/limit",
        json={"closed": True},
        headers={"Authorization": f"Bearer {operator}"},
    )
    assert closed.status_code == 200 and closed.json()["closed"] is True
    refused = _place(client, "after")
    assert refused.status_code == 503 and "closed to new orgs" in refused.json()["error"]
    assert _place(client, "before").status_code == 200  # a placed org still answers
