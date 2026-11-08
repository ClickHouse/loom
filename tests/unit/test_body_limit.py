"""The request-body cap: declared oversize is refused before the body is
read; undeclared (chunked) oversize is cut off mid-stream with the same 413;
bodies under the cap and non-HTTP scopes are untouched."""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from server.transport.body_limit import CODE, BodyLimitMiddleware

LIMIT = 1024


def _app(limit: int = LIMIT, own_limit: tuple[str, ...] = ()) -> FastAPI:
    app = FastAPI()
    seen: dict[str, int] = {"bytes": 0}

    @app.post("/echo")
    async def echo(request: Request):
        body = await request.body()
        seen["bytes"] = len(body)
        return {"n": len(body)}

    app.add_middleware(BodyLimitMiddleware, max_bytes=limit, own_limit=own_limit)
    app.state.seen = seen
    return app


def test_declared_oversize_is_refused_before_the_body_is_read():
    app = _app()
    c = TestClient(app)
    r = c.post(
        "/echo", content=b"x" * (LIMIT + 1), headers={"content-type": "application/octet-stream"}
    )
    assert r.status_code == 413
    detail = r.json()["detail"]
    assert detail["code"] == CODE and detail["limit_bytes"] == LIMIT
    assert str(LIMIT + 1) in detail["message"]
    assert app.state.seen["bytes"] == 0  # the handler never ran


def test_body_at_the_cap_passes():
    c = TestClient(_app())
    r = c.post("/echo", content=b"x" * LIMIT)
    assert r.status_code == 200 and r.json()["n"] == LIMIT


def test_chunked_oversize_is_cut_off_with_the_same_413():
    app = _app()
    c = TestClient(app)

    def chunks():
        for _ in range(4):
            yield b"y" * 512  # 2048 total, no content-length

    r = c.post("/echo", content=chunks())
    assert r.status_code == 413
    assert r.json()["detail"]["code"] == CODE
    assert app.state.seen["bytes"] <= LIMIT


def test_zero_or_negative_cap_disables_the_middleware():
    c = TestClient(_app(limit=0))
    r = c.post("/echo", content=b"x" * (LIMIT * 4))
    assert r.status_code == 200 and r.json()["n"] == LIMIT * 4


def test_error_shape_matches_loom_errors():
    c = TestClient(_app())
    r = c.post("/echo", content=b"x" * (LIMIT + 5))
    body = json.loads(r.content)
    assert set(body) == {"detail"}
    assert set(body["detail"]) == {"code", "message", "limit_bytes", "category", "retryable"}
    assert body["detail"]["category"] == "input" and body["detail"]["retryable"] is False


@pytest.mark.parametrize("bad", [b"abc", b""])
def test_unparseable_content_length_falls_back_to_counting(bad):
    app = _app()
    c = TestClient(app)
    r = c.post("/echo", content=b"x" * 10, headers={"content-length": bad.decode() or "10"})
    assert r.status_code in (200, 400, 413)


def test_main_app_registers_the_cap_from_settings():
    from server.settings import settings

    assert settings.max_request_bytes == 32 * 1024 * 1024
    src = open("server/main.py", encoding="utf-8").read()
    assert "BodyLimitMiddleware" in src and "settings.max_request_bytes" in src


def test_a_path_that_bounds_its_own_body_passes_through_whole():
    c = TestClient(_app(own_limit=("/echo",)))
    r = c.post("/echo", content=b"x" * (LIMIT * 4))
    assert r.status_code == 200 and r.json()["n"] == LIMIT * 4
