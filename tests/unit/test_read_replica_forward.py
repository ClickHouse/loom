"""A read-replica sends the ops that read git history to the main Loom, with
the caller's own headers, and passes the main Loom's answer on as it came."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from fastapi import HTTPException
from pydantic import BaseModel

from server.clients import main_loom
from server.core import read_replica
from server.settings import settings

MAIN = "https://main.loom.test"


class _Out(BaseModel):
    verdict: str = ""
    n: int = 0


class _In(BaseModel):
    path: str = ""
    start_line: int = 0
    namespace: str = ""


@pytest.fixture
def replica(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "read_replica", True)
    monkeypatch.setattr(settings, "read_replica_main_url", MAIN + "/")
    monkeypatch.setattr(settings, "read_replica_forward_timeout_seconds", 5.0)


def _main_answers(monkeypatch: pytest.MonkeyPatch, handler: Any) -> list[httpx.Request]:
    """Route every AsyncClient through `handler`; returns the requests sent."""
    sent: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return handler(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(record))
    monkeypatch.setattr(main_loom, "get_client", lambda: client)
    return sent


def test_only_a_replica_that_knows_the_main_url_forwards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "read_replica", False)
    monkeypatch.setattr(settings, "read_replica_main_url", MAIN)
    assert not read_replica.forwards("code.blame_range")
    monkeypatch.setattr(settings, "read_replica", True)
    assert read_replica.forwards("code.blame_range")
    assert read_replica.forwards("code.verify_citations")
    assert not read_replica.forwards("code.search")
    monkeypatch.setattr(settings, "read_replica_main_url", "  ")
    assert not read_replica.forwards("code.blame_range")


@pytest.mark.usefixtures("replica")
async def test_the_main_answer_comes_back_with_the_callers_headers_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent = _main_answers(monkeypatch, lambda r: httpx.Response(200, json={"verdict": "ok"}))
    read_replica.remember_caller_headers(
        {"authorization": "Bearer t0k", "x-loom-code-scope": "code-x", "cookie": "no"}
    )
    out = await main_loom.forward("code.blame_range", _In(path="a.cpp"), _Out)
    assert out == _Out(verdict="ok")
    (req,) = sent
    assert str(req.url) == MAIN + "/v1/code.blame_range"
    assert req.headers["authorization"] == "Bearer t0k"
    assert req.headers["x-loom-code-scope"] == "code-x"
    assert "cookie" not in req.headers
    # Only what the caller set: the main Loom fills its own defaults.
    assert req.read() == b'{"path":"a.cpp"}'


@pytest.mark.usefixtures("replica")
async def test_an_error_from_the_main_loom_is_passed_on(monkeypatch: pytest.MonkeyPatch) -> None:
    detail = {"code": "FORBIDDEN", "message": "cannot access", "required_role": "reader"}
    _main_answers(monkeypatch, lambda r: httpx.Response(403, json={"detail": detail}))
    with pytest.raises(HTTPException) as e:
        await main_loom.forward("code.blame_range", _In(), _Out)
    assert e.value.status_code == 403
    assert e.value.detail == detail


@pytest.mark.usefixtures("replica")
@pytest.mark.parametrize(
    "handler",
    [
        lambda r: httpx.Response(502, text="bad gateway"),
        lambda r: (_ for _ in ()).throw(httpx.ConnectError("refused", request=r)),
    ],
    ids=["5xx", "unreachable"],
)
async def test_an_unreachable_main_loom_falls_back_to_the_local_answer(
    monkeypatch: pytest.MonkeyPatch, handler: Any
) -> None:
    _main_answers(monkeypatch, handler)
    assert await main_loom.forward("code.blame_range", _In(), _Out) is None


async def test_the_main_loom_neither_keeps_headers_nor_forwards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "read_replica", False)
    read_replica._caller_headers.set(None)
    read_replica.remember_caller_headers({"authorization": "Bearer t0k"})
    assert read_replica._caller_headers.get() is None
    assert await main_loom.forward("code.blame_range", _In(), _Out) is None


@pytest.mark.usefixtures("replica")
async def test_blame_on_a_replica_answers_with_the_main_looms_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.code.api.models_history import CodeBlameRangeInput, CodeBlameRangeOutput
    from server.code.app import blame

    monkeypatch.setattr(blame, "require_code_intel", lambda: None)
    want = CodeBlameRangeOutput.model_construct()
    seen: list[str] = []

    async def fake_forward(op: str, payload: Any, cls: Any) -> Any:
        seen.append(op)
        return want

    monkeypatch.setattr(blame, "forward", fake_forward)
    got = await blame.handle_code_blame_range(
        CodeBlameRangeInput(path="src/a.cpp", start_line=1, end_line=2)
    )
    assert got is want and seen == ["code.blame_range"]
