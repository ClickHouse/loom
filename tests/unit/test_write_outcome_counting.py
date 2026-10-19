"""`memory.set` must count a write outcome for every terminal shape.

A terminal raise the counter misses (422 CHAR_LIMIT_EXCEEDED, 422 BLACKLISTED,
409 PRECONDITION_FAILED) would make a namespace whose writes are all refused
emit nothing and look identical on the dashboard to one receiving no traffic —
the exact confusion the counter exists to remove.

These tests pin the boundary mapping rather than the whole write path: the
handler derives the refusal outcome once from the raised `HTTPException`,
so this is where a newly-added refusal branch either gets counted or silently
does not.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import HTTPException

import server.memory.app.write as _mw
from server.memory.api.models_crud import MemorySetInput
from server.memory.app import write as write_mod


def _input() -> MemorySetInput:
    return MemorySetInput(
        org="test",
        namespace="personal",
        agent="cursor",
        memory_key="user_dog_name",
        value="Biscuit",
    )


@pytest.fixture
def counted(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Capture (namespace, outcome) pairs instead of touching OTel."""
    calls: list[tuple[str, str]] = []
    from server.observability import metrics as m

    monkeypatch.setattr(
        m,
        "record_memory_write",
        lambda *, namespace, outcome: calls.append((namespace, outcome)),
    )
    return calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("code", "status", "expected"),
    [
        ("PINNED_CONFLICT", 409, "rejected"),
        ("BLACKLISTED", 422, "blacklisted"),
        ("CHAR_LIMIT_EXCEEDED", 422, "too_large"),
        ("PRECONDITION_FAILED", 409, "precondition_failed"),
    ],
)
async def test_every_refusal_shape_is_counted(
    monkeypatch: pytest.MonkeyPatch,
    counted: list[tuple[str, str]],
    code: str,
    status: int,
    expected: str,
) -> None:
    async def _raise(_input: MemorySetInput, **_: Any) -> None:
        raise HTTPException(status_code=status, detail={"code": code})

    monkeypatch.setattr(_mw, "_handle_memory_set", _raise)

    with pytest.raises(HTTPException) as exc:
        await write_mod.handle_memory_set(_input())

    # The refusal must still reach the caller unchanged.
    assert exc.value.status_code == status
    assert counted == [("personal", expected)]


@pytest.mark.asyncio
async def test_unmapped_error_is_counted_but_not_as_a_refusal(
    monkeypatch: pytest.MonkeyPatch, counted: list[tuple[str, str]]
) -> None:
    """An unmapped HTTPException is counted, but must NOT read as `rejected`.

    `except HTTPException` catches everything the nested call graph raises —
    get_type_config, enforce_on_insert, check_on_commit, a CH query against
    loom.namespaces, any middleware 5xx. Defaulting all of that to `rejected`
    would make a ClickHouse outage indistinguishable on the panel from callers
    legitimately colliding with pinned rows, so an operator would read a policy
    problem where there is an infrastructure one. It still has to be counted —
    silence is the failure this guards — just under its own label.
    """

    async def _raise(_input: MemorySetInput, **_: Any) -> None:
        raise HTTPException(status_code=500, detail={"code": "CH_UNAVAILABLE"})

    monkeypatch.setattr(_mw, "_handle_memory_set", _raise)

    with pytest.raises(HTTPException):
        await write_mod.handle_memory_set(_input())

    assert counted == [("personal", "error")]
    assert counted[0][1] != "rejected", (
        "an infrastructure failure must not be labelled the same as a pinned-row refusal"
    )


@pytest.mark.asyncio
async def test_non_dict_detail_does_not_break_counting(
    monkeypatch: pytest.MonkeyPatch, counted: list[tuple[str, str]]
) -> None:
    """FastAPI allows a bare string detail; the mapping must tolerate it."""

    async def _raise(_input: MemorySetInput, **_: Any) -> None:
        raise HTTPException(status_code=500, detail="boom")

    monkeypatch.setattr(_mw, "_handle_memory_set", _raise)

    with pytest.raises(HTTPException):
        await write_mod.handle_memory_set(_input())

    assert counted == [("personal", "error")]


@pytest.mark.asyncio
async def test_success_is_counted_once_by_the_inner_handler_only(
    monkeypatch: pytest.MonkeyPatch, counted: list[tuple[str, str]]
) -> None:
    """The boundary must not double-count a served write.

    Successful shapes are counted inside `_handle_memory_set`, where the
    conflict verdict is known; the wrapper only counts refusals.
    """
    sentinel = object()

    async def _ok(_input: MemorySetInput, **_: Any) -> Any:
        return sentinel

    monkeypatch.setattr(_mw, "_handle_memory_set", _ok)

    out = await write_mod.handle_memory_set(_input())

    assert out is sentinel
    assert counted == []


@pytest.mark.asyncio
async def test_infrastructure_failure_is_counted_as_error_and_still_raises(
    monkeypatch: pytest.MonkeyPatch, counted: list[tuple[str, str]]
) -> None:
    """A ClickHouse failure must reach the panel as `error`, not as silence.

    Nothing in the memory.set callee graph raises HTTPException for an infrastructure
    fault — server/core/ch.py raises driver exceptions or a bare RuntimeError, and
    the repository modules raise no HTTPException at all, so without this a CH
    outage would increment nothing. The exception must still
    propagate untouched; this is an accounting hook, not error handling.
    """

    async def _boom(_input: MemorySetInput, **_: Any) -> None:
        raise RuntimeError("clickhouse exploded")

    monkeypatch.setattr(_mw, "_handle_memory_set", _boom)

    with pytest.raises(RuntimeError, match="clickhouse exploded"):
        await write_mod.handle_memory_set(_input())

    assert counted == [("personal", "error")]


@pytest.mark.asyncio
async def test_unmapped_4xx_is_a_refusal_not_an_infrastructure_error(
    monkeypatch: pytest.MonkeyPatch, counted: list[tuple[str, str]]
) -> None:
    """An unmapped 4xx is still a caller/policy refusal.

    Classifying on the error `code` alone would label every unmapped 4xx
    `error`, pointing the operator at infrastructure for a policy problem. Classify on the
    STATUS instead.
    """

    async def _raise(_input: MemorySetInput, **_: Any) -> None:
        raise HTTPException(status_code=403, detail={"code": "SOME_POLICY_GATE"})

    monkeypatch.setattr(_mw, "_handle_memory_set", _raise)

    with pytest.raises(HTTPException):
        await write_mod.handle_memory_set(_input())

    assert counted == [("personal", "rejected")]


def test_every_mapped_code_is_actually_raised_by_the_handler() -> None:
    """Guard against the mapping drifting away from the code it describes.

    A mapping entry for a code the handler never raises is dead weight, and the
    reverse — a raised code with no entry — is an uncounted refusal.
    """
    import inspect
    import re

    from server.memory.app import blacklist_guard as bl

    # BLACKLISTED is raised by `enforce_on_insert`, which the handler awaits —
    # so the set of reachable codes spans both modules, not only the handler.
    from server.memory.app import write as _memory_write

    src = inspect.getsource(_memory_write) + inspect.getsource(bl)
    raised = set(re.findall(r'"code":\s*"([A-Z_]+)"', src))
    mapped = set(_mw._WRITE_OUTCOME_BY_ERROR_CODE)

    assert mapped <= raised, f"mapped but never raised: {sorted(mapped - raised)}"
