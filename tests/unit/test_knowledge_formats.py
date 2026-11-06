"""`knowledge.formats`: every format the server routes, the names that select it,
and whether a configured extractor declares it, read within a bound so an
unreachable or slow service costs the listing one short wait, not the extract
timeout. The reference service runs in process (ASGI); stubs declare chosen
formats or never answer.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from loom_extractor import service
from server.knowledge.api.models_knowledge import KnowledgeFormatsInput
from server.knowledge.app import extractors
from server.knowledge.app.ingest import handle_knowledge_formats

TOKEN = "s" * 24


def _declaring(formats: list[str], delay_s: float = 0.0) -> Starlette:
    async def capabilities(request: Request) -> Response:
        await asyncio.sleep(delay_s)
        return JSONResponse(
            {
                "contract_version": 1,
                "extractor": {"name": "stub"},
                "max_bytes": 1000,
                "formats": [{"format": f} for f in formats],
            }
        )

    return Starlette(routes=[Route("/v1/capabilities", capabilities)])


class _Hosts(httpx.AsyncBaseTransport):
    """Each host is an app in this process; an unknown host is unreachable."""

    def __init__(self, apps: dict[str, Starlette]) -> None:
        self.apps = apps

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        app = self.apps.get(request.url.host)
        if app is None:
            raise httpx.ConnectError("unreachable", request=request)
        return await httpx.ASGITransport(app=app).handle_async_request(request)


@pytest.fixture
def hosts(monkeypatch: pytest.MonkeyPatch, settings_override: Any) -> Iterator[_Hosts]:
    transport = _Hosts({"ref": service.create_app(service.Config(token=TOKEN))})
    monkeypatch.setattr(extractors, "transport", transport)
    extractors._declared.clear()
    settings_override(document_extractor_url="http://ref", document_extractor_token=TOKEN)
    yield transport
    extractors._declared.clear()


def _input() -> KnowledgeFormatsInput:
    return KnowledgeFormatsInput(org="o", namespace="n")


async def test_each_format_says_its_names_and_whether_an_extractor_serves_it(
    hosts: _Hosts, settings_override: Any
) -> None:
    # PDF goes to its own route, which declares it; DOCX's route is unreachable, so the
    # reference service's DOCX does not count (a format's own route hides the default).
    hosts.apps["pdf-x"] = _declaring(["pdf"])
    settings_override(
        document_extractors_json=json.dumps({"pdf": "http://pdf-x", "docx": "http://gone"}),
        knowledge_upload_max_bytes=1234,
    )
    out = await handle_knowledge_formats(_input())
    by = {f.format: f for f in out.formats}
    assert [f.format for f in out.formats] == sorted(by)
    assert by["pdf"].available and not by["docx"].available
    assert by["markdown"].available and by["toml"].available  # the reference service's
    assert by["markdown"].extensions == ["markdown", "md", "mdx"]
    assert "text/markdown" in by["markdown"].media_types
    assert by["csv"].extensions == ["csv", "tsv"]
    assert (out.max_upload_bytes, out.refused_extensions, out.default_format) == (
        1234,
        ["doc", "ppt", "xls"],
        "text",
    )


async def test_with_no_extractor_configured_nothing_is_available(
    hosts: _Hosts, settings_override: Any
) -> None:
    settings_override(document_extractor_url="")
    out = await handle_knowledge_formats(_input())
    assert out.formats and not any(f.available for f in out.formats)


async def test_a_slow_service_costs_the_listing_its_bound_and_declares_nothing(
    hosts: _Hosts, settings_override: Any
) -> None:
    hosts.apps["slow"] = _declaring(["sql"], delay_s=30)
    settings_override(document_extractors_json=json.dumps({"sql": "http://slow"}))
    started = time.monotonic()
    served = await extractors.available_formats(timeout_s=0.2)
    assert time.monotonic() - started < 5
    assert "sql" not in served and served["markdown"] == "http://ref"
