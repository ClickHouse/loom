"""Budget notifications are private, retryable, and subordinate to erasure."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import replace
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from server.core import lease_writes, rmt_carry
from server.core.errors import BackendUnavailableError
from server.observability.budget_notifications import BudgetWarning
from server.tasks.app import budget_notifications as alerts
from server.tasks.store import budget_notifications as store


@pytest.fixture
def warning() -> BudgetWarning:
    return BudgetWarning("o", "n", "warnings", "alice", 2, 1.8, 0.8, 1700000000)


@pytest.fixture
def delivery(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    lease = SimpleNamespace(renewed_at=time.monotonic(), let_expire=Mock())
    take = AsyncMock(return_value=lease)
    release = AsyncMock()
    monkeypatch.setattr(lease_writes.leases, "take", take)
    monkeypatch.setattr(lease_writes.leases, "release_shielded", release)
    monkeypatch.setattr(alerts.machines, "fleet_at", AsyncMock(return_value=True))
    monkeypatch.setattr(alerts, "get_client", lambda: object())
    state = {
        "inbox": AsyncMock(return_value={"owner_namespace": "n"}),
        "erasures": AsyncMock(return_value=[]),
        "exists": AsyncMock(return_value=set()),
        "insert": AsyncMock(return_value=True),
        "remove": AsyncMock(),
    }
    state.update(lease=lease, take=take, release=release)
    monkeypatch.setattr(alerts.repo, "fetch_inbox_row", state["inbox"])
    monkeypatch.setattr(alerts.repo, "server_version", AsyncMock(return_value=1700000000000000))
    monkeypatch.setattr(alerts.ledger_reads, "unfinished_reaching", state["erasures"])
    monkeypatch.setattr(store, "event_seqs", state["exists"])
    monkeypatch.setattr(store, "insert_event", state["insert"])
    monkeypatch.setattr(store, "remove_event", state["remove"])
    return state


async def test_warning_carries_the_user_axis_and_derived_provenance(
    warning: BudgetWarning, delivery: dict[str, Any]
) -> None:
    assert await alerts.deliver(warning)
    row = delivery["insert"].call_args.args[0]
    assert (row["org"], row["namespace"], row["user_id"], row["session_id"]) == (
        "o",
        "n",
        "alice",
        "-",
    )
    assert row["source_class"] == "derived"
    assert row["actor"] == "process:budget_warning"
    assert '"user_id": "alice"' in row["patch"]


@pytest.mark.parametrize("operator", [False, True])
@pytest.mark.parametrize("user_id", ["alice", ""])
async def test_user_warning_redacts_spend_even_when_an_operator_initiates_delivery(
    warning: BudgetWarning,
    delivery: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    operator: bool,
    user_id: str,
) -> None:
    from server.auth.jwt import AuthInfo
    from server.operations.base import Role

    auth = (
        AuthInfo(
            subject="operator",
            org="o",
            namespaces=("*",),
            roles=(Role.PLATFORM_ADMIN,),
            is_service_account=False,
        )
        if operator
        else None
    )
    monkeypatch.setattr("server.auth.context.current_auth", lambda: auth)
    warning = replace(warning, user_id=user_id)
    assert await alerts.deliver(warning)
    row = delivery["insert"].call_args.args[0]
    assert json.loads(row["patch"]) == {
        "namespace": "n",
        "user_id": user_id or None,
        "cap_usd": 2,
        "spent_24h_usd": None if user_id else 1.8,
        "share": 0.8,
    }
    assert row["note"] == ("User budget warning" if user_id else "Namespace budget warning")
    assert alerts.event_id(warning) == alerts.event_id(replace(warning, spent=3.87654))


@pytest.mark.parametrize("inbox", [None, {"owner_namespace": "other"}])
async def test_warning_never_uses_a_missing_or_other_namespaces_inbox(
    warning: BudgetWarning, delivery: dict[str, Any], inbox: Any
) -> None:
    delivery["inbox"].return_value = inbox
    assert not await alerts.deliver(warning)
    delivery["insert"].assert_not_awaited()


async def test_an_existing_event_deduplicates_repeated_checks(
    warning: BudgetWarning, delivery: dict[str, Any]
) -> None:
    delivery["exists"].return_value = {1}
    assert not await alerts.deliver(warning)
    delivery["insert"].assert_not_awaited()


@pytest.mark.parametrize(
    "erasures",
    [
        [{"scope_type": "user_id", "scope_value": "alice"}],
        [{"scope_type": "namespace", "scope_value": "n"}],
    ],
)
async def test_erasure_before_delivery_refuses_the_warning(
    warning: BudgetWarning, delivery: dict[str, Any], erasures: list[dict[str, Any]]
) -> None:
    delivery["erasures"].return_value = erasures
    assert not await alerts.deliver(warning)
    delivery["insert"].assert_not_awaited()


async def test_erasure_racing_insert_removes_only_that_delivery(
    warning: BudgetWarning, delivery: dict[str, Any]
) -> None:
    delivery["erasures"].side_effect = [[], [{"scope_type": "user_id", "scope_value": "alice"}]]
    assert not await alerts.deliver(warning)
    delivery["remove"].assert_awaited_once_with(delivery["insert"].call_args.args[0])


async def test_an_uncertain_insert_still_performs_erasure_cleanup(
    warning: BudgetWarning, delivery: dict[str, Any]
) -> None:
    delivery["insert"].side_effect = RuntimeError("insert reply lost")
    delivery["erasures"].side_effect = [[], [{"scope_type": "namespace", "scope_value": "n"}]]
    with pytest.raises(RuntimeError, match="reply lost"):
        await alerts.deliver(warning)
    delivery["remove"].assert_awaited_once()


async def test_an_unreadable_ledger_fails_closed(
    warning: BudgetWarning, delivery: dict[str, Any]
) -> None:
    delivery["erasures"].side_effect = RuntimeError("unavailable")
    assert not await alerts.deliver(warning)
    delivery["insert"].assert_not_awaited()


def test_event_identity_separates_tenants_users_policies_inboxes_and_utc_days(
    warning: BudgetWarning,
) -> None:
    variants = [
        warning,
        replace(warning, org="other"),
        replace(warning, namespace="other"),
        replace(warning, user_id="bob"),
        replace(warning, user_id=""),
        replace(warning, inbox="other"),
        replace(warning, cap=4),
        replace(warning, share=0.9),
        replace(warning, observed_at=warning.observed_at + 86400),
    ]
    assert len({alerts.event_id(item) for item in variants}) == len(variants)
    assert alerts.event_id(warning) == alerts.event_id(replace(warning, spent=5))


async def test_a_suppressed_insert_retries_with_a_fresh_token() -> None:
    class CH:
        def __init__(self) -> None:
            self.tokens: list[str] = []
            self.reads = 0

        async def insert(self, table: str, rows: Any, *, settings: Any) -> None:
            self.tokens.append(settings["insert_deduplication_token"])

        async def query_all(self, sql: str, *, params: Any) -> Any:
            self.reads += 1
            return [] if self.reads == 1 else [{"seq": 9}]

    ch = CH()
    row = {"org": "o", "inbox": "i", "task_id": "id", "seq": 9}
    assert await store.insert_event(row, ch=ch)
    assert ch.tokens[0] == "id" and ch.tokens[1].startswith("id:")
    assert ch.tokens[0] != ch.tokens[1]


async def test_user_warning_waits_for_the_erasure_capable_fleet(
    warning: BudgetWarning, delivery: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.auth import machines

    monkeypatch.setattr(machines, "fleet_at", AsyncMock(return_value=False))
    assert not await alerts.deliver(warning)
    delivery["insert"].assert_not_awaited()


@pytest.mark.parametrize("entry", ["busy", "expired"])
async def test_an_unavailable_writer_lease_suppresses_only_the_warning(
    warning: BudgetWarning,
    delivery: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    entry: str,
) -> None:
    if entry == "busy":
        delivery["take"].return_value = None
    else:
        monkeypatch.setattr(lease_writes.leases, "work_left", lambda *_: 0.0)
    assert not await alerts.deliver(warning)
    delivery["insert"].assert_not_awaited()
    delivery["lease"].let_expire.assert_not_called()
    if entry == "busy":
        delivery["release"].assert_not_awaited()
    else:
        delivery["release"].assert_awaited_once_with(delivery["lease"])


@pytest.mark.parametrize("entry", ["take", "work_left"])
@pytest.mark.parametrize("error_type", [BackendUnavailableError, asyncio.CancelledError])
async def test_writer_lease_backend_errors_propagate(
    warning: BudgetWarning,
    delivery: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    entry: str,
    error_type: type[BaseException],
) -> None:
    error = error_type("lease backend unavailable")
    if entry == "take":
        delivery["take"].side_effect = error
    else:
        monkeypatch.setattr(lease_writes.leases, "work_left", Mock(side_effect=error))
    with pytest.raises(error_type) as caught:
        await alerts.deliver(warning)
    assert caught.value is error
    delivery["insert"].assert_not_awaited()
    if entry == "take":
        delivery["release"].assert_not_awaited()
    else:
        delivery["release"].assert_awaited_once_with(delivery["lease"])


@pytest.mark.parametrize(
    "error_type", [BackendUnavailableError, lease_writes.FenceUnavailableError]
)
async def test_insert_backend_and_nested_fence_errors_propagate_with_the_holder_retained(
    warning: BudgetWarning, delivery: dict[str, Any], error_type: type[BackendUnavailableError]
) -> None:
    error = error_type("insert reply unavailable")
    delivery["insert"].side_effect = error
    with pytest.raises(error_type) as caught:
        await alerts.deliver(warning)
    assert caught.value is error
    delivery["lease"].let_expire.assert_called_once()
    delivery["release"].assert_awaited_once_with(delivery["lease"])


async def test_uncertain_insert_retains_the_durable_lease(
    warning: BudgetWarning, delivery: dict[str, Any]
) -> None:
    for error in [RuntimeError("reply lost"), asyncio.CancelledError()]:
        delivery["insert"].side_effect = error
        with pytest.raises(type(error)):
            await alerts.deliver(warning)
        delivery["lease"].let_expire.assert_called()
    delivery["take"].assert_awaited_with(
        "budget.warning.writer/o",
        ttl_s=lease_writes.lease_ttl_s(rmt_carry.send_bound_s()),
        ch=None,
    )


async def test_insert_uses_the_existing_server_side_send_fence(
    warning: BudgetWarning, delivery: dict[str, Any]
) -> None:
    async def insert(row: dict[str, Any]) -> bool:
        assert rmt_carry.sent_under_lease()
        return True

    delivery["insert"].side_effect = insert
    assert await alerts.deliver(warning)
    assert not rmt_carry.sent_under_lease()
    delivery["lease"].let_expire.assert_not_called()
    delivery["release"].assert_awaited_once()


@pytest.mark.parametrize("bound", [20, 30, 80, 200])
def test_server_finish_precedes_lease_expiry(bound: float) -> None:
    ttl = lease_writes.lease_ttl_s(bound)
    assert 0.4 * ttl + bound + 4.9 < ttl


async def test_erasure_refuses_to_complete_while_a_warning_write_is_unsettled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from contextlib import asynccontextmanager

    from server.core.errors import BackendUnavailableError
    from server.erasure.store import steps_ledgers

    @asynccontextmanager
    async def busy(name: str, **kwargs: Any) -> Any:
        assert name == "budget.warning.writer/o" and kwargs["wait_s"] == 30
        yield False

    monkeypatch.setattr(steps_ledgers.leases, "holding", busy)
    delete = AsyncMock()
    monkeypatch.setattr(steps_ledgers, "delete_scoped", delete)
    with pytest.raises(BackendUnavailableError, match="not settled"):
        await steps_ledgers.delete_task_events_by_user("o", "alice", ch=object())
    with pytest.raises(BackendUnavailableError, match="not settled"):
        await steps_ledgers.delete_tasks_by_namespace("o", "n", ch=object())
    delete.assert_not_awaited()


async def test_namespace_and_user_warnings_from_one_check_do_not_compete_locally(
    warning: BudgetWarning, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    active = 0
    peak = 0
    emitted: list[str] = []

    async def deliver(item: BudgetWarning) -> bool:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0)
        emitted.append(item.user_id)
        active -= 1
        return True

    monkeypatch.setattr(alerts, "deliver", deliver)
    alerts.schedule(replace(warning, user_id=""))
    alerts.schedule(warning)
    await asyncio.gather(*list(alerts._tasks))
    assert emitted == ["", "alice"]
    assert peak == 1


async def test_cancel_before_first_turn_cleans_the_delivery_slot(warning: BudgetWarning) -> None:
    import asyncio

    before = set(alerts._tasks)
    alerts.schedule(warning)
    [task] = set(alerts._tasks) - before
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert alerts.event_id(warning) not in alerts._pending
    assert warning.org not in alerts._serial
