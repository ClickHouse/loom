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
from server.core.document_formats import format_of
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
        self.calls: list[str] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request.url.host)
        app = self.apps.get(request.url.host)
        if app is None:
            raise httpx.ConnectError("unreachable", request=request)
        return await httpx.ASGITransport(app=app).handle_async_request(request)


@pytest.fixture
def hosts(monkeypatch: pytest.MonkeyPatch, settings_override: Any) -> Iterator[_Hosts]:
    transport = _Hosts({"ref": service.create_app(service.Config(token=TOKEN))})
    monkeypatch.setattr(extractors, "transport", transport)
    extractors._declared.clear()
    extractors._unreadable.clear()
    settings_override(document_extractor_url="http://ref", document_extractor_token=TOKEN)
    yield transport
    extractors._declared.clear()
    extractors._unreadable.clear()


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
    hosts: _Hosts, settings_override: Any, caplog: pytest.LogCaptureFixture
) -> None:
    hosts.apps["slow"] = _declaring(["sql"], delay_s=30)
    settings_override(document_extractors_json=json.dumps({"sql": "http://slow"}))
    served = await extractors.available_formats(timeout_s=0.2)  # returns, though the read hangs
    assert "sql" not in served and served["markdown"] == "http://ref"
    assert "document extractor http://slow: timed out" in caplog.text


async def test_a_down_service_costs_one_wait_a_minute_not_one_per_listing(
    hosts: _Hosts, settings_override: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    hosts.apps["slow"] = _declaring(["sql"], delay_s=30)
    settings_override(document_extractors_json=json.dumps({"sql": "http://slow"}))
    await extractors.available_formats(timeout_s=0.2)
    served = await extractors.available_formats(timeout_s=0.2)
    assert "sql" not in served and hosts.calls.count("slow") == 1
    # Past the minute it is read again.
    real = time.monotonic
    monkeypatch.setattr(extractors.time, "monotonic", lambda: real() + 61)
    await extractors.available_formats(timeout_s=0.2)
    assert hosts.calls.count("slow") == 2


async def test_the_default_format_is_what_routing_falls_back_to(hosts: _Hosts) -> None:
    out = await handle_knowledge_formats(_input())
    assert out.default_format == format_of("notes", "application/x-unknown") == "text"


async def test_a_service_read_since_its_failed_listing_counts_again(
    hosts: _Hosts, settings_override: Any
) -> None:
    hosts.apps["sqlx"] = _declaring(["sql"], delay_s=30)
    settings_override(document_extractors_json=json.dumps({"sql": "http://sqlx"}))
    assert "sql" not in await extractors.available_formats(timeout_s=0.2)
    # It comes back, and an ingest reads it (its capabilities cached): the listing agrees.
    hosts.apps["sqlx"] = _declaring(["sql"])
    await extractors.capabilities("http://sqlx")
    assert (await extractors.available_formats(timeout_s=0.2))["sql"] == "http://sqlx"


async def test_a_fresh_read_wins_over_a_mark_a_slower_read_left(
    hosts: _Hosts, settings_override: Any
) -> None:
    # A listing's hung read times out after an ingest's read of the same service cached
    # its capabilities: the mark it then leaves does not hide what is cached.
    hosts.apps["sqlx"] = _declaring(["sql"])
    settings_override(document_extractors_json=json.dumps({"sql": "http://sqlx"}))
    await extractors.capabilities("http://sqlx")
    extractors._unreadable["http://sqlx"] = time.monotonic() + 60
    assert (await extractors.available_formats(timeout_s=0.2))["sql"] == "http://sqlx"
