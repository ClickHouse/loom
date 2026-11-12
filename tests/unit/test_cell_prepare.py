"""`loom cell prepare`'s own rules (server/cli/cell_prepare.py): its arguments, the
registration call and its retries, and which rows it writes from the router's answer.
The stores and the router together: tests/integration/test_cell_prepare.py."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from server.cli import cell as cell_cli
from server.cli import cell_prepare
from server.core import cell
from server.settings import settings

SHA = "a" * 64
ROUTER = httpx.URL("http://127.0.0.1:7900")
OPERATOR = "lco_" + "1" * 128


@pytest.fixture(autouse=True)
def _cell(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "cell", "eu-1")
    monkeypatch.setattr(settings, "storage_authority", "postgres")
    monkeypatch.setattr(settings, "pg_dsn", "postgresql://cell.internal/loom")
    monkeypatch.setattr(settings, "router_operator_token", OPERATOR)


def _args(**over: Any) -> argparse.Namespace:
    fields = {
        "cell": "eu-1",
        "host": "eu-1.loom.example",
        "credential_sha256": SHA,
        "probe_url": "",
        "wait": 0,
        "router": "http://127.0.0.1:7900",
    }
    return argparse.Namespace(**{**fields, **over})


def _rows(**over: Any) -> cell_prepare._CellRows:
    fields: dict[str, Any] = {
        "deployment_id": "dep-1",
        "first": None,
        "mode": "",
        "current": None,
        "next": None,
        "tenancy_rows": 0,
    }
    return cell_prepare._CellRows(**{**fields, **over})


@pytest.mark.parametrize(
    "over,why",
    [
        ({"cell": "EU_1"}, "no cell name"),
        ({"cell": "eu-2"}, "LOOM_CELL is 'eu-1'"),
        ({"host": ""}, "no host"),
        ({"credential_sha256": "lcr_" + "0" * 128}, "never the credential"),
        ({"credential_sha256": "A" * 64}, "64 lowercase hex"),
        ({"wait": -1}, "--wait"),
        ({"wait": 86401}, "--wait"),  # past the probe key's life
    ],
)
def test_arguments_it_refuses(over, why) -> None:
    assert why in cell_prepare._args_problem(_args(**over))


def test_a_cell_keeps_its_state_in_postgres(monkeypatch) -> None:
    assert cell_prepare._args_problem(_args()) == ""
    monkeypatch.setattr(settings, "storage_authority", "clickhouse")
    assert "LOOM_STORAGE_AUTHORITY=postgres" in cell_prepare._args_problem(_args())


class _Router:
    """The router's register answers, in order, and the bodies it was sent."""

    def __init__(self, *answers: httpx.Response) -> None:
        self.answers = list(answers)
        self.bodies: list[Any] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.bodies.append(request.read())
        assert request.headers["Authorization"] == f"Bearer {OPERATOR}"
        assert request.url.path == "/cells/v1/operator/cells/eu-1/register"
        return self.answers.pop(0)


OK = {"cell": "eu-1", "is_first": True, "state": "attaching", "mode": "recording"}


def _busy(code: str = "PROBE_BUSY", retry_after: str = "3") -> httpx.Response:
    return httpx.Response(
        503,
        json={"error": "the probe listener is busy", "code": code},
        headers={"Retry-After": retry_after},
    )


@pytest.mark.asyncio
async def test_a_busy_or_unreachable_listener_is_retried_within_wait(monkeypatch) -> None:
    slept: list[float] = []

    async def _sleep(s: float) -> None:
        slept.append(s)

    monkeypatch.setattr(cell_prepare, "_sleep", _sleep)
    router = _Router(
        _busy(),
        _busy("PROBE_UNREACHABLE"),
        _busy(retry_after="x"),
        _busy(retry_after="0"),
        httpx.Response(200, json=OK),
    )
    answer = await cell_prepare._register(_args(wait=60), ROUTER, {}, httpx.MockTransport(router))
    assert answer == OK and slept == [3.0, 2.0, 2.0, 1.0] and len(router.bodies) == 5


@pytest.mark.asyncio
async def test_a_listener_still_busy_at_the_end_of_wait_is_named() -> None:
    router = _Router(_busy())
    with pytest.raises(cell_prepare.Refused, match="within --wait: the probe listener is busy"):
        await cell_prepare._register(_args(wait=1), ROUTER, {}, httpx.MockTransport(router))


@pytest.mark.parametrize(
    "response,why",
    [
        (
            httpx.Response(409, json={"error": "cell 'eu-2' records deployment id"}),
            "refused \\(409\\)",
        ),
        (httpx.Response(502, text="<html>bad gateway</html>"), "router does \\(502\\): '<html>bad"),
        (httpx.Response(200, json={**OK, "cell": "eu-2"}), "once the router answers"),
        (
            httpx.Response(503, json={"error": "down", "code": "BACKEND_UNAVAILABLE"}),
            "may have landed: run prepare again",
        ),
    ],
)
@pytest.mark.asyncio
async def test_any_other_answer_stops_it(response, why) -> None:
    with pytest.raises(cell_prepare.Refused, match=why):
        await cell_prepare._register(
            _args(wait=60), ROUTER, {}, httpx.MockTransport(_Router(response))
        )


@pytest.mark.asyncio
async def test_no_answer_asks_for_a_rerun() -> None:
    def _drop(_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out")

    with pytest.raises(cell_prepare.Refused, match="run prepare again"):
        await cell_prepare._register(_args(), ROUTER, {}, httpx.MockTransport(_drop))


def test_the_operator_credential_goes_only_to_https_or_loopback(monkeypatch) -> None:
    assert cell_cli.operator_router(_args()) == ROUTER
    assert "https router" in str(cell_cli.operator_router(_args(router="http://router.example")))
    monkeypatch.setattr(settings, "router_operator_token", "")
    assert "LOOM_ROUTER_OPERATOR_TOKEN" in str(cell_cli.operator_router(_args()))


def test_a_router_it_cannot_call_is_refused_before_connecting(monkeypatch) -> None:
    """Exit 2, as the other `loom cell` commands answer a usage error, and nothing read."""
    monkeypatch.setattr(cell_prepare, "prepare", None)  # never reached
    assert cell_prepare.cmd_prepare(_args(router="http://router.example")) == 2
    assert cell_prepare.cmd_prepare(_args(router="http://router.example/base")) == 2
    monkeypatch.setattr(settings, "router_operator_token", "")
    assert cell_prepare.cmd_prepare(_args()) == 2


FIELDS = {**cell.DEFAULTS, "deployment_id": "dep-1", "oidc_client_id": "loom"}


def test_before_attach_its_own_row_is_what_the_processes_report() -> None:
    reported = cell.as_value(FIELDS)
    assert cell_prepare._own_rows(_rows(), OK, [FIELDS, FIELDS]) == (reported, None)
    # With nothing recorded the processes agree on one configuration.
    with pytest.raises(cell_prepare.Refused, match="different configurations"):
        cell_prepare._own_rows(_rows(), OK, [FIELDS, {**FIELDS, "scim": True}])
    # A build that does not report a field at its default agrees with one that does, and
    # the newest build's fields are written.
    older = {k: v for k, v in FIELDS.items() if k != "bucket_prefix"}
    assert cell_prepare._own_rows(_rows(), OK, [older, FIELDS]) == (reported, None)


def test_before_attach_a_present_own_row_stands() -> None:
    reported = cell.as_value(FIELDS)
    on_next = {**FIELDS, "scim": True}
    nxt = cell.as_value(on_next)
    # Processes split between its `current` and `next` (an update rolling out) keep both.
    own = _rows(current=reported, next=nxt)
    assert cell_prepare._own_rows(own, OK, [FIELDS, on_next]) == (reported, nxt)
    with pytest.raises(cell_prepare.Refused, match="neither this cell's own row's current"):
        cell_prepare._own_rows(_rows(current=cell.as_value(cell.DEFAULTS)), OK, [FIELDS])
    with pytest.raises(cell_prepare.Refused, match="roll it back"):
        cell_prepare._own_rows(own, OK, [FIELDS, {**FIELDS, "oidc_client_id": "x"}])


def test_after_attach_its_own_row_is_the_directorys() -> None:
    current = cell.as_value(FIELDS)
    on_next = {**FIELDS, "scim": True}
    nxt = cell.as_value(on_next)
    answer = {**OK, "current_fp": current, "next_fp": nxt}
    stale = cell.as_value(cell.DEFAULTS)
    # A present value that differs is overwritten; processes split between `current` and
    # `next` (an update rolling out) are accepted.
    assert cell_prepare._own_rows(_rows(current=stale), answer, [FIELDS]) == (current, nxt)
    assert cell_prepare._own_rows(_rows(), answer, [FIELDS, on_next]) == (current, nxt)
    with pytest.raises(cell_prepare.Refused, match="neither the directory's current nor"):
        cell_prepare._own_rows(_rows(), answer, [FIELDS, {**FIELDS, "oidc_client_id": "x"}])


def _beat(i: str, fields: dict[str, Any] | None, on: str = "eu-1") -> cell_prepare._Beat:
    return cell_prepare._Beat(i, on, None if fields is None else cell.as_value(fields))


def test_each_process_is_checked_on_its_own_before_registering(monkeypatch) -> None:
    monkeypatch.setattr(settings, "document_s3_bucket", "")
    mine = cell.fingerprint("dep-1")
    other = {**mine, "scim": not mine["scim"]}
    # A build that does not report the prefix serves the default one, as this command does.
    older = {k: v for k, v in mine.items() if k != "bucket_prefix"}
    assert cell_prepare._beat_fields([_beat("i-0", older)], _rows()) == [older]
    # Different configurations pass here: whether they may differ is the answer's to say.
    assert cell_prepare._beat_fields([_beat("i-1", mine), _beat("i-2", other)], _rows()) == [
        mine,
        other,
    ]
    for beats, why in (
        ([], "start the cell first"),
        ([_beat("i-1", None)], "no readable heartbeat row"),
        ([_beat("i-1", mine, on="eu-2")], "another LOOM_CELL"),
        ([cell_prepare._Beat("i-1", "eu-1", {"v": 99, "fields": mine})], "cannot read"),
        ([_beat("i-1", {**mine, "deployment_id": "dep-2"})], "another deployment id"),
        ([_beat("i-1", {**mine, "bucket": "docs"})], "another deployment id, bucket"),
    ):
        with pytest.raises(cell_prepare.Refused, match=why):
            cell_prepare._beat_fields(beats, _rows())


@pytest.mark.asyncio
async def test_the_prefix_count_leaves_the_probe_out(monkeypatch) -> None:
    from server.core import object_store

    monkeypatch.setattr(settings, "document_s3_bucket", "docs")
    monkeypatch.setattr(settings, "document_s3_prefix", "eu-2")
    listed: list[tuple[str, str, int]] = []
    keys = ["eu-2/_cell_probe", "eu-2/acme/ns/doc"]

    async def _page(bucket: str, prefix: str, *, max_keys: int = 1000) -> Any:
        listed.append((bucket, prefix, max_keys))
        now = datetime.now(UTC)
        return object_store.Page([object_store.Listed(k, now) for k in keys], False, now)

    monkeypatch.setattr(object_store, "list_page", _page)
    assert await cell_prepare._prefix_objects() == 1
    assert listed == [("docs", "eu-2/", 2)]
    keys[:] = ["eu-2/_cell_probe"]
    assert await cell_prepare._prefix_objects() == 0
    monkeypatch.setattr(settings, "document_s3_bucket", "")
    assert await cell_prepare._prefix_objects() == 0


def test_the_routers_answer_must_agree_with_the_cells_rows_and_processes() -> None:
    beat = cell_prepare._Beat("i-1", "eu-1", {})
    assert cell_prepare._answer_problem(_rows(), OK, [beat]) == ""
    later = {**OK, "is_first": False, "mode": "enforced"}
    assert "`first` row" in cell_prepare._answer_problem(_rows(first=(True, "eu-1")), later, [beat])
    unset = cell_prepare._Beat("i-2", "", {})
    assert "without LOOM_CELL=eu-1" in cell_prepare._answer_problem(_rows(), later, [beat, unset])
    # The first cell's processes run without LOOM_CELL until attach.
    assert cell_prepare._answer_problem(_rows(), OK, [beat, unset]) == ""
    assert "no first value" in cell_prepare._answer_problem(_rows(), {"cell": "eu-1"}, [beat])
    # A mode row above the directory's is not this cell's; a first cell's below it is a
    # stopped attach, and a later cell's `recording` row is not its state.
    assert "`enforced`" in cell_prepare._answer_problem(_rows(mode="enforced"), OK, [beat])
    raised = {**OK, "mode": "enforced"}
    assert cell_prepare._answer_problem(_rows(mode="recording"), raised, [beat]) == ""
    assert "later cell's mode row is `recording`" in cell_prepare._answer_problem(
        _rows(mode="recording"), later, [beat]
    )


def test_the_state_rows_it_reports_to_the_router() -> None:
    assert cell_prepare._state_rows(_rows()) == 0
    assert cell_prepare._state_rows(_rows(mode="enforced")) == 1
    own = cell.as_value(FIELDS)
    assert cell_prepare._state_rows(_rows(first=(True, "eu-1"), mode="recording", current=own)) == 3


@pytest.mark.asyncio
async def test_each_nonce_is_deleted_only_while_it_is_this_runs(monkeypatch, capsys) -> None:
    from server.core import leases, object_store, pg

    monkeypatch.setattr(settings, "document_s3_bucket", "docs")
    monkeypatch.setattr(settings, "document_s3_prefix", "eu-1")
    nonces = {"postgres": "n-pg", "clickhouse": "n-ch", "bucket": "n-b"}
    deleted: list[Any] = []
    in_bucket = [b"n-b"]

    async def _pg(_sql: str, *_args: Any) -> str:
        raise OSError("postgres is down")

    async def _read(_loc: Any) -> bytes:
        return in_bucket[0]

    async def _delete(bucket: str, keys: list[str]) -> None:
        deleted.extend(f"{bucket}/{k}" for k in keys)

    class _CH:
        async def execute(self, sql: str, params: dict[str, Any]) -> None:
            assert sql == leases.LOCK_RELEASE  # a compare-and-delete in strict mode
            deleted.append(params)

    monkeypatch.setattr(pg, "execute", _pg)
    monkeypatch.setattr(object_store, "read", _read)
    monkeypatch.setattr(object_store, "delete", _delete)
    # Every nonce is tried when one fails, the one that stays named.
    await cell_prepare._delete_nonces(_CH(), nonces)
    assert deleted == [{"name": "cell-probe", "me": "n-ch"}, "docs/eu-1/_cell_probe"]
    assert "the probe nonce in Postgres stays: postgres is down" in capsys.readouterr().err
    # A bucket object another run wrote meanwhile is that run's to delete.
    deleted.clear()
    in_bucket[0] = b"n-other"
    await cell_prepare._delete_nonces(_CH(), nonces)
    assert deleted == [{"name": "cell-probe", "me": "n-ch"}]


def test_a_refusal_or_a_store_failure_exits_1_and_says_why(monkeypatch, capsys) -> None:
    import asyncio

    from server.cli import _ch

    def _run_connected(run: Any, **_kw: Any) -> int:
        return asyncio.run(run(None))

    monkeypatch.setattr(_ch, "run_connected", _run_connected)
    for raised, said in (
        (cell_prepare.Refused("the cell's Postgres is cell 'eu-2''s"), "prepare: the cell's"),
        (OSError("connection reset"), "stopped (OSError: connection reset); run it again"),
    ):

        async def _prepare(*_a: Any, error: Exception = raised) -> int:
            raise error

        monkeypatch.setattr(cell_prepare, "prepare", _prepare)
        assert cell_prepare.cmd_prepare(_args()) == 1
        assert said in capsys.readouterr().err


@pytest.mark.asyncio
async def test_the_processes_live_after_the_wait_are_the_ones_judged(monkeypatch, capsys) -> None:
    """A restart onto another configuration while the probe was retried: the own row is
    what the processes run now, not what they ran before the wait."""
    monkeypatch.setattr(settings, "document_s3_bucket", "")
    before = cell.fingerprint("dep-1")
    after = {**before, "scim": not before["scim"]}
    reads = [[_beat("i-old", before)], [_beat("i-new", after)]]
    written: list[Any] = []

    async def _rows_read() -> cell_prepare._CellRows:
        return _rows()

    async def _beats(_ch: Any) -> list[cell_prepare._Beat]:
        return reads.pop(0)

    async def _nothing(*_a: Any) -> Any:
        return 0

    async def _register(*_a: Any) -> dict[str, Any]:
        return {**OK, "registered": True}

    async def _write(_rows: Any, _answer: Any, current: Any, nxt: Any) -> list[str]:
        written.append((current, nxt))
        return ["first", "mode", "own"]

    for name, fake in (
        ("_read_rows", _rows_read),
        ("_read_beats", _beats),
        ("_prefix_objects", _nothing),
        ("_write_nonces", _nothing),
        ("_delete_nonces", _nothing),
        ("_register", _register),
        ("_write_rows", _write),
    ):
        monkeypatch.setattr(cell_prepare, name, fake)
    assert await cell_prepare.prepare(None, _args(), ROUTER) == 0
    assert written == [(cell.as_value(after), None)] and reads == []
