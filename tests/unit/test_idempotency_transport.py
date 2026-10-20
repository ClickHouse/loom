"""One idempotency decision for both transports (`gates.idempotency_check`):
the key is scoped to the verified caller, or for an anonymous call to the
authorized org; the validated input is hashed; a non-idempotent op or a
call without a key never reads the cache."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from pydantic import BaseModel

import server.core.idempotency as idempotency
from server.auth.jwt import AuthInfo
from server.operations.base import Operation, Role
from server.transport import gates


class _In(BaseModel):
    text: str = ""
    run_id: str = ""


class _Out(BaseModel):
    ok: bool = True


async def _handler(inp: Any) -> _Out:
    return _Out()


def _op(idempotent: bool = True) -> Operation:
    return Operation(
        name="memory.test_idem_probe",
        input_model=_In,
        output_model=_Out,
        handler=_handler,
        idempotent=idempotent,
        required_role=Role.WRITER,
    )


TARGET = gates.Target(org="acme", namespace="n", named_namespace="n", register_namespace="n")


@pytest.fixture
def reads(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    seen: list[tuple[str, str]] = []

    async def _check(key: str, body_hash: str) -> idempotency.CacheResult:
        seen.append((key, body_hash))
        return idempotency.CacheResult(hit=None, conflict=False)

    monkeypatch.setattr(idempotency, "check", _check)
    return seen


def _decide(op: Operation, auth: AuthInfo | None, key: str, inp: _In, **kw: Any) -> gates.Replay:
    return asyncio.run(gates.idempotency_check(op, auth, TARGET, key, inp, **kw))


def test_no_key_or_a_non_idempotent_op_reads_nothing(reads: list[Any]) -> None:
    assert _decide(_op(), None, "", _In()) == gates.Replay()
    assert _decide(_op(idempotent=False), None, "k", _In()) == gates.Replay()
    assert reads == []


def test_the_key_is_scoped_to_the_caller_and_an_anonymous_call_to_the_target_org(
    reads: list[Any],
) -> None:
    alice = AuthInfo(
        subject="alice",
        org="acme",
        namespaces=("n",),
        roles=(Role.WRITER,),
        is_service_account=False,
    )
    anon = _decide(_op(), None, "k", _In(text="x"))
    mine = _decide(_op(), alice, "k", _In(text="x"))
    assert anon.scope is not None and mine.scope is not None
    assert anon.scope.key == idempotency.scoped_key(
        "k", org="acme", subject="", operation="memory.test_idem_probe"
    )
    assert anon.scope.key != mine.scope.key
    assert anon.body_hash == mine.body_hash == idempotency.request_hash(_In(text="x"))


def test_a_field_the_transport_filled_is_not_part_of_the_hash(reads: list[Any]) -> None:
    stamped = _decide(_op(), None, "k", _In(text="x", run_id="r1"), unset=("run_id",))
    asked = _decide(_op(), None, "k", _In(text="x"))
    assert stamped.body_hash == asked.body_hash
