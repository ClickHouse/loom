"""Every coded HTTP error names its entry in the error catalogue in a `Link` header."""

from __future__ import annotations

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from server.core.errors import PreconditionFailedError, error_help_link, register_exception_handler


def _client() -> TestClient:
    app = FastAPI()
    register_exception_handler(app)

    @app.get("/typed")
    def typed() -> None:
        raise PreconditionFailedError("stale")

    @app.get("/coded")
    def coded() -> None:
        raise HTTPException(
            status_code=422, detail={"code": "BLACKLISTED", "message": "m"}, headers={"X-A": "1"}
        )

    @app.get("/plain")
    def plain() -> None:
        raise HTTPException(status_code=404, detail="no such thing")

    return TestClient(app)


def test_a_typed_error_links_its_catalogue_entry() -> None:
    r = _client().get("/typed")
    assert r.status_code == 409
    assert r.headers["link"] == error_help_link("PRECONDITION_FAILED")
    assert r.json()["detail"]["code"] == "PRECONDITION_FAILED"


def test_a_coded_http_exception_keeps_its_headers_and_gains_the_link() -> None:
    r = _client().get("/coded")
    assert r.headers["link"].endswith('errors.md#blacklisted>; rel="help"')
    assert r.headers["x-a"] == "1"


def test_an_error_without_a_code_has_no_link() -> None:
    assert "link" not in _client().get("/plain").headers


def test_every_linked_code_has_a_catalogue_heading() -> None:
    from scripts.codegen import docs_reference as gen

    page = (gen.REFERENCE / "errors.md").read_text(encoding="utf-8")
    for code in ("PRECONDITION_FAILED", "BLACKLISTED", "PINNED_CONFLICT", "RATE_LIMITED"):
        assert f"#### `{code}`" in page, code
