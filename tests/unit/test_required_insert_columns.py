"""Required ledger classification survives schema projection and retries."""

from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest

from server.core import ch as ch_mod

TABLE = "loom.agent_events"
REQUIRED = frozenset({"mirror_marked"})


class Driver:
    def __init__(self, live):
        self.live = dict(live)
        self.attempts = []
        self.committed = []
        self.describes = 0
        self.describe_error = False
        self.remove_marker_on_first_send = False
        self.remove_input_marker_on_first_send = None

    async def query(self, sql, parameters=None, settings=None):
        assert sql.startswith("DESCRIBE TABLE ")
        self.describes += 1
        if self.describe_error:
            raise RuntimeError("Code: 16. DB::Exception: metadata unavailable")
        return SimpleNamespace(column_names=["name", "type"], result_rows=list(self.live.items()))

    async def insert(self, table, data, *, column_names, column_type_names, settings):
        attempt = dict(
            table=table,
            columns=list(column_names),
            data=copy.deepcopy(data),
            types=copy.deepcopy(column_type_names),
            settings=dict(settings),
        )
        self.attempts.append(attempt)
        if self.remove_marker_on_first_send and len(self.attempts) == 1:
            del self.live["mirror_marked"]
        if self.remove_input_marker_on_first_send is not None and len(self.attempts) == 1:
            self.remove_input_marker_on_first_send[0].pop("mirror_marked")
        unknown = set(column_names) - self.live.keys()
        if unknown:
            raise RuntimeError(f"Code: 16. DB::Exception: No such column {sorted(unknown)[0]}")
        self.committed.extend(dict(zip(column_names, row, strict=True)) for row in data)


@pytest.fixture
def world(monkeypatch):
    driver = Driver({"id": "UInt64", "mirror_marked": "UInt8"})
    monkeypatch.setattr(ch_mod, "_COLUMN_TYPES", {})
    monkeypatch.setattr(ch_mod, "_PROJECTION_WARNED", set())
    monkeypatch.setattr(ch_mod, "_INSERT_OBSERVERS", [])
    monkeypatch.setattr(ch_mod.CHClient, "client", property(lambda self: driver))

    async def table_present(*args, **kwargs):
        pass

    monkeypatch.setattr(ch_mod, "guard_pending_schema", table_present)
    return SimpleNamespace(driver=driver, client=ch_mod.CHClient())


@pytest.mark.parametrize("cached_marker", [False, True])
@pytest.mark.parametrize("live_marker", [False, True])
async def test_required_marker_cannot_be_projected_away_by_cached_or_live_schema(
    world, cached_marker, live_marker
):
    cached = {"id": "UInt64"}
    if cached_marker:
        cached["mirror_marked"] = "UInt8"
    ch_mod._COLUMN_TYPES[TABLE] = cached
    if not live_marker:
        del world.driver.live["mirror_marked"]
    rows = [{"id": 7, "mirror_marked": 1, "optional_future": "unused"}]
    if live_marker:
        await world.client.insert(TABLE, rows, required_columns=REQUIRED)
        assert world.driver.committed == [{"id": 7, "mirror_marked": 1}]
        if not cached_marker:
            assert world.driver.attempts[0]["types"] is None
    else:
        with pytest.raises(RuntimeError, match="No such column mirror_marked"):
            await world.client.insert(TABLE, rows, required_columns=REQUIRED)
        assert world.driver.committed == []
    assert world.driver.attempts
    assert all("mirror_marked" in attempt["columns"] for attempt in world.driver.attempts)
    assert all("optional_future" not in attempt["columns"] for attempt in world.driver.attempts)
    assert (TABLE, "mirror_marked") not in ch_mod._PROJECTION_WARNED


@pytest.mark.parametrize("live_marker", [False, True])
async def test_unreadable_metadata_delegates_required_column_validation_to_driver(
    world, live_marker
):
    world.driver.describe_error = True
    if not live_marker:
        del world.driver.live["mirror_marked"]
    rows = [{"id": 7, "mirror_marked": 1}]
    if live_marker:
        await world.client.insert(TABLE, rows, required_columns=REQUIRED)
        assert world.driver.committed == rows
    else:
        with pytest.raises(RuntimeError, match="No such column mirror_marked"):
            await world.client.insert(TABLE, rows, required_columns=REQUIRED)
        assert world.driver.committed == []
    assert all(attempt["columns"] == ["id", "mirror_marked"] for attempt in world.driver.attempts)
    assert all(attempt["types"] is None for attempt in world.driver.attempts)


async def test_retry_after_schema_change_never_downgrades_marker_or_changes_batch_token(world):
    world.driver.remove_marker_on_first_send = True
    with pytest.raises(RuntimeError, match="No such column mirror_marked"):
        await world.client.insert(TABLE, [{"id": 7, "mirror_marked": 1}], required_columns=REQUIRED)
    assert world.driver.committed == []
    assert len(world.driver.attempts) == 2
    assert all(a["columns"] == ["id", "mirror_marked"] for a in world.driver.attempts)
    assert all(a["data"] == [[7, 1]] for a in world.driver.attempts)
    tokens = {a["settings"]["insert_deduplication_token"] for a in world.driver.attempts}
    assert len(tokens) == 1


async def test_row_mutation_during_failed_send_cannot_create_a_marker_free_retry(world):
    rows = [{"id": 7, "mirror_marked": 1}]
    world.driver.remove_marker_on_first_send = True
    world.driver.remove_input_marker_on_first_send = rows
    with pytest.raises(ValueError, match="lacks required columns"):
        await world.client.insert(TABLE, rows, required_columns=REQUIRED)
    assert world.driver.committed == []
    assert len(world.driver.attempts) == 1
    assert world.driver.attempts[0]["columns"] == ["id", "mirror_marked"]


@pytest.mark.parametrize("bad_index", [0, 1])
async def test_missing_required_value_in_any_input_row_refuses_before_metadata_or_send(
    world, bad_index
):
    rows = [{"id": 7, "mirror_marked": 1}, {"id": 8, "mirror_marked": 1}]
    del rows[bad_index]["mirror_marked"]
    with pytest.raises(ValueError, match="lacks required columns"):
        await world.client.insert(TABLE, rows, required_columns=REQUIRED)
    assert world.driver.describes == 0
    assert world.driver.attempts == world.driver.committed == []


async def test_ordinary_projection_still_preserves_batch_without_new_required_fields(world):
    del world.driver.live["mirror_marked"]
    await world.client.insert(TABLE, [{"id": 7, "optional_future": "unused"}])
    assert world.driver.committed == [{"id": 7}]
    assert world.driver.attempts[0]["columns"] == ["id"]
    assert (TABLE, "optional_future") in ch_mod._PROJECTION_WARNED


async def test_ordinary_retry_may_still_drop_an_optional_column(world):
    world.driver.remove_marker_on_first_send = True
    await world.client.insert(TABLE, [{"id": 7, "mirror_marked": 1}])
    assert world.driver.committed == [{"id": 7}]
    assert [a["columns"] for a in world.driver.attempts] == [["id", "mirror_marked"], ["id"]]


async def test_nonprojected_insert_also_validates_required_input_before_send(world, monkeypatch):
    from server.core import rmt_carry

    monkeypatch.setattr(rmt_carry, "carries", lambda table: False)
    with pytest.raises(ValueError, match="lacks required columns"):
        await world.client.insert("loom.required_unit", [{"id": 7}], required_columns=REQUIRED)
    assert world.driver.describes == 0 and not world.driver.attempts


async def test_required_set_is_frozen_before_the_first_await(world, monkeypatch):
    required = {"mirror_marked"}

    async def mutate_caller_set(*args, **kwargs):
        required.clear()

    monkeypatch.setattr(ch_mod, "guard_pending_schema", mutate_caller_set)
    del world.driver.live["mirror_marked"]
    with pytest.raises(RuntimeError, match="No such column mirror_marked"):
        await world.client.insert(TABLE, [{"id": 7, "mirror_marked": 1}], required_columns=required)
    assert not required
    assert not world.driver.committed
    assert all("mirror_marked" in a["columns"] for a in world.driver.attempts)
