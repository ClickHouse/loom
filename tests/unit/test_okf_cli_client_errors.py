"""`import-okf` counts a failed write against its one concept, link or batch
and carries on, so the client raises RuntimeError for every failure: no
answer, a non-200 (named by the error envelope, never the raw body, which
quotes the concept back) and a body that is not JSON."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from server.cli import okf


def _client(handler: Any) -> okf._Client:
    c = okf._Client("http://loom", "tok")
    c._c = httpx.AsyncClient(base_url="http://loom", transport=httpx.MockTransport(handler))
    return c


@pytest.mark.parametrize(
    ("handler", "want"),
    [
        (
            lambda req: (_ for _ in ()).throw(httpx.ReadTimeout("slow", request=req)),
            "memory.set → no answer (ReadTimeout",
        ),
        (
            lambda req: httpx.Response(
                422,
                json={"detail": [{"msg": "Field required", "input": {"value": "SECRET"}}]},
            ),
            "memory.set → HTTP 422 validation_error: Field required",
        ),
        (lambda req: httpx.Response(200, text="<html>login</html>"), "not a JSON answer"),
    ],
)
async def test_every_failed_post_is_a_runtime_error_without_the_body(
    handler: Any, want: str
) -> None:
    c = _client(handler)
    try:
        with pytest.raises(RuntimeError) as ei:
            await c.post("memory.set", {"value": "SECRET"})
    finally:
        await c.close()
    assert want in str(ei.value)
    assert "SECRET" not in str(ei.value)


async def test_a_json_object_comes_back() -> None:
    c = _client(lambda req: httpx.Response(200, json={"status": "ok"}))
    try:
        assert await c.post("memory.set", {}) == {"status": "ok"}
    finally:
        await c.close()
