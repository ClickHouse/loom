"""The server's calls to extractor services: a format goes to the service its
route names, is extracted only if that service declares it, and comes back
checked; with no service, an unreachable one or one that refuses the token,
nothing is extracted and there is no fallback. The reference service runs in
process (ASGI); a stub service answers with chosen bodies for the checks on
untrusted output.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from loom_extractor import service
from server.knowledge.app import extractors
from server.knowledge.domain.extract import InvalidArtifact, format_of, parse_artifact

TOKEN = "s" * 24


def _stub(extract: Callable[[Request], Response], formats: list[str] | None = None) -> Starlette:
    async def capabilities(request: Request) -> Response:
        return JSONResponse(
            {
                "contract_version": 1,
                "extractor": {"name": "stub"},
                "max_bytes": 1000,
                "formats": [{"format": f} for f in formats or ["pdf"]],
            }
        )

    async def run(request: Request) -> Response:
        return extract(request)

    return Starlette(
        routes=[
            Route("/v1/capabilities", capabilities),
            Route("/v1/extract", run, methods=["POST"]),
        ]
    )


class _Hosts(httpx.AsyncBaseTransport):
    """Each host is an app in this process; an unknown host is unreachable."""

    def __init__(self, apps: dict[str, Starlette]) -> None:
        self.apps = apps
        self.calls: list[tuple[str, str]] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.calls.append((request.url.host, request.url.path))
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


async def test_a_source_is_extracted_by_the_service_and_read_back_checked(hosts: _Hosts) -> None:
    a = await extractors.extract(b"[server]\nport = 7777\n", name="c.toml")
    assert a.format == "toml" and [b.text for b in a.blocks] == ["[server]", "server.port = 7777"]
    assert a.coverage.complete
    assert ("ref", "/v1/extract") in hosts.calls


async def test_options_reach_the_extractor(hosts: _Hosts) -> None:
    a = await extractors.extract(b"SELECT 1;", name="q.sql", options={"sql_dialect": "postgres"})
    assert a.options == {"sql_dialect": "postgres"}


async def test_a_format_goes_to_its_own_route_then_its_media_types_then_the_default(
    hosts: _Hosts, settings_override: Any
) -> None:
    settings_override(
        document_extractors_json=json.dumps(
            {"sql": "http://sql-x/", "text/markdown": "http://md-x"}
        )
    )
    assert extractors.endpoint_for("sql", "text/markdown") == "http://sql-x"
    assert extractors.endpoint_for("markdown", "Text/Markdown; charset=utf-8") == "http://md-x"
    assert extractors.endpoint_for("toml") == "http://ref"
    settings_override(document_extractors_json='{"text/markdown": "http://md-x"}')
    # A media type's route is for its own format: a `.sql` file sent as text/markdown
    # goes to the default service, which declares SQL.
    assert extractors.endpoint_for("sql", "text/markdown") == "http://ref"
    a = await extractors.extract(b"SELECT 1;", name="q.sql", media_type="text/markdown")
    assert a.format == "sql"
    settings_override(document_extractors_json='{"sql": "http://sql-x"}')
    with pytest.raises(extractors.ExtractorUnavailable, match="unreachable"):
        await extractors.extract(b"SELECT 1;", name="q.sql")


async def test_with_no_service_configured_nothing_is_extracted(
    hosts: _Hosts, settings_override: Any
) -> None:
    settings_override(document_extractor_url="")
    with pytest.raises(extractors.ExtractorUnavailable, match="no extractor is configured"):
        await extractors.extract(b"# a", name="a.md")
    assert await extractors.available_formats() == {}
    assert hosts.calls == []


async def test_a_format_its_service_does_not_declare_is_unavailable(hosts: _Hosts) -> None:
    # A FormatUnavailable, which is an ExtractorUnavailable: configuration, not an outage.
    with pytest.raises(extractors.FormatUnavailable, match="does not declare"):
        await extractors.extract(b"PAR1PAR1", name="a.parquet")
    assert await extractors.available_formats() == {
        f: "http://ref"
        for f in ("markdown", "html", "text", "sql", "toml", "csv", "pdf", "docx", "pptx", "xlsx")
    }


async def test_a_service_that_refuses_the_token_is_unavailable(
    hosts: _Hosts, settings_override: Any
) -> None:
    settings_override(document_extractor_token="w" * 24)
    with pytest.raises(extractors.ExtractorUnavailable, match="401"):
        await extractors.extract(b"# a", name="a.md")


async def test_a_refused_source_keeps_the_extractors_code(hosts: _Hosts) -> None:
    with pytest.raises(extractors.ExtractionFailed) as e:
        await extractors.extract(b"%PDF-1.7\n", name="a.md")
    assert e.value.code == "format_mismatch"
    calls = len(hosts.calls)
    with pytest.raises(extractors.ExtractionFailed) as e:
        await extractors.extract(b"x", name="old.doc")
    assert e.value.code == "unsupported_format"
    assert len(hosts.calls) == calls, "a legacy name is refused before any call"


async def test_declarations_are_read_once_a_minute(hosts: _Hosts) -> None:
    for _ in range(3):
        await extractors.extract(b"# a", name="a.md")
    assert hosts.calls.count(("ref", "/v1/capabilities")) == 1


def _with_stub(hosts: _Hosts, settings_override: Any, app: Starlette) -> None:
    hosts.apps["stub"] = app
    settings_override(document_extractors_json='{"pdf": "http://stub"}')


async def test_a_source_over_the_declared_size_is_not_sent(
    hosts: _Hosts, settings_override: Any
) -> None:
    _with_stub(hosts, settings_override, _stub(lambda r: Response(status_code=500)))
    with pytest.raises(extractors.ExtractionFailed) as e:
        await extractors.extract(b"x" * 1001, name="a.pdf")
    assert e.value.code == "too_large" and ("stub", "/v1/extract") not in hosts.calls


@pytest.mark.parametrize(
    ("status", "body", "kind", "code"),
    [
        (500, b"", extractors.ExtractorUnavailable, None),
        (
            503,
            b'{"error": {"code": "busy", "message": "m"}}',
            extractors.ExtractorUnavailable,
            None,
        ),
        (429, b"", extractors.ExtractorUnavailable, None),
        (407, b"", extractors.ExtractorUnavailable, None),
        (408, b"", extractors.ExtractorUnavailable, None),
        (302, b"", extractors.ExtractorUnavailable, None),
        (204, b"", extractors.ExtractorUnavailable, None),
        (
            422,
            b'{"error": {"code": "corrupt", "message": "bad xref"}}',
            extractors.ExtractionFailed,
            "corrupt",
        ),
        (
            422,
            b'{"error": {"code": "Not A Code!", "message": "m"}}',
            extractors.ExtractionFailed,
            "refused",
        ),
        (400, b"not json", extractors.ExtractionFailed, "refused"),
        (422, b"[" * 60_000, extractors.ExtractionFailed, "refused"),
        (
            422,
            b'{"error": {"code": "format_unavailable", "message": "m"}}',
            extractors.ExtractorUnavailable,
            None,
        ),
    ],
)
async def test_a_stub_services_answers_map_to_typed_errors(
    hosts: _Hosts, settings_override: Any, status: int, body: bytes, kind: type, code: str | None
) -> None:
    _with_stub(hosts, settings_override, _stub(lambda r: Response(body, status_code=status)))
    with pytest.raises(kind) as e:
        await extractors.extract(b"%PDF-1.7", name="a.pdf")
    if code is not None:
        assert e.value.code == code


def _artifact(**changes: Any) -> dict[str, Any]:
    d: dict[str, Any] = {
        "schema_version": 1,
        "format": "pdf",
        "parser": "stub",
        "parser_version": "1",
        "coverage": {"unit": "pages", "total": 1, "extracted": 1},
        "blocks": [{"id": "b0", "type": "paragraph", "text": "x"}],
    }
    d.update(changes)
    return d


async def test_a_stub_services_artifact_is_read_through_the_contract(
    hosts: _Hosts, settings_override: Any
) -> None:
    good = json.dumps(_artifact()).encode()
    _with_stub(hosts, settings_override, _stub(lambda r: Response(good)))
    a = await extractors.extract(b"%PDF-1.7", name="a.pdf")
    assert a.parser == "stub" and a.coverage.complete
    bad = json.dumps(_artifact(format="text")).encode()
    hosts.apps["stub"] = _stub(lambda r: Response(bad))
    with pytest.raises(extractors.ExtractionFailed, match="format") as e:
        await extractors.extract(b"%PDF-1.7", name="a.pdf")
    assert e.value.code == "invalid_artifact" and isinstance(e.value.__cause__, InvalidArtifact)


async def test_an_artifact_over_the_bound_is_refused_while_it_is_read(
    hosts: _Hosts, settings_override: Any
) -> None:
    _with_stub(hosts, settings_override, _stub(lambda r: Response(b"x" * 5000)))
    settings_override(document_extractor_max_artifact_bytes=1024)
    with pytest.raises(extractors.ExtractionFailed) as e:
        await extractors.extract(b"%PDF-1.7", name="a.pdf")
    assert e.value.code == "resource_limit"


def _parse(d: dict[str, Any]) -> Any:
    return parse_artifact(json.dumps(d).encode(), fmt="pdf", max_bytes=1 << 20)


def _nested(depth: int) -> Any:
    value: Any = 1
    for _ in range(depth):
        value = [value]
    return value


def _block(**changes: Any) -> dict[str, Any]:
    return {"id": "b0", "type": "paragraph", "text": "x", **changes}


@pytest.mark.parametrize(
    ("artifact", "why"),
    [
        (_artifact(schema_version=2), "schema_version"),
        (_artifact(schema_version=True), "schema_version"),
        (_artifact(schema_version=1.0), "schema_version"),
        (_artifact(format="text"), "format"),
        (_artifact(parser=""), "parser"),
        (_artifact(blocks={}), "blocks"),
        (_artifact(blocks=["x"]), "blocks"),
        (_artifact(blocks=[_block(type="table")]), "type"),
        (_artifact(blocks=[_block(text=7)]), "text"),
        (_artifact(blocks=[_block(text="\ud800")]), "Unicode"),
        (_artifact(blocks=[_block(), _block()]), "repeats"),
        (_artifact(blocks=[_block(id="")]), "id"),
        (_artifact(blocks=[_block(parent="b9")]), "parent"),
        (_artifact(blocks=[_block(id="b0", parent="b0")]), "parent"),
        (_artifact(blocks=[_block(depth=-1)]), "depth"),
        (_artifact(blocks=[_block(depth=1.5)]), "depth"),
        (_artifact(blocks=[_block(keys="a")]), "keys"),
        (_artifact(blocks=[_block(keys=[1])]), "keys"),
        (_artifact(blocks=[_block(section_path=[None])]), "section_path"),
        (_artifact(blocks=[_block(locator={"line_start": 3, "line_end": 2})]), "line"),
        (_artifact(blocks=[_block(locator={"line_start": 0, "line_end": 2})]), "line"),
        (_artifact(blocks=[_block(locator={"line_start": -1, "line_end": -1})]), "line"),
        (_artifact(blocks=[_block(locator={"char_start": 5, "char_end": 4})]), "char"),
        (_artifact(blocks=[_block(locator={"char_start": 5})]), "char"),
        (_artifact(blocks=[_block(locator={"line_start": "1", "line_end": 1})]), "integer"),
        (_artifact(blocks=[_block(locator=[1])]), "locator"),
        (_artifact(blocks=[_block(attrs={"a": _nested(20)})]), "nests"),
        (_artifact(options=[]), "options"),
        (_artifact(warnings=[1]), "warnings"),
        (_artifact(coverage={"unit": "pages", "total": 1, "extracted": 2}), "extracted"),
        (_artifact(coverage={"unit": "pages", "total": 1, "extracted": -1}), "extracted"),
        (_artifact(coverage={"unit": "", "total": 1, "extracted": 1}), "unit"),
        (
            _artifact(coverage={"unit": "pages", "total": 2, "extracted": 1, "complete": True}),
            "complete",
        ),
        (
            _artifact(
                coverage={
                    "unit": "pages",
                    "total": 1,
                    "extracted": 1,
                    "lost": ["x"],
                    "complete": True,
                }
            ),
            "complete",
        ),
        (
            _artifact(coverage={"unit": "pages", "total": 1, "extracted": 1, "failed": ["x"]}),
            "failed",
        ),
        (_artifact(coverage=None), "coverage"),
        (_artifact(blocks=[_block(keys=["k"] * 10_001)]), "over the 10000"),
        (_artifact(blocks=[_block(section_path=["s"] * 65)]), "over the 64"),
        (_artifact(blocks=[_block(section_path=["s" * 501])]), "over the 500 characters"),
        (_artifact(blocks=[_block(keys=["k" * 1025])]), "over the 1024 characters"),
        (_artifact(schema_version="v" * 10_000), "schema_version"),
    ],
)
def test_an_artifact_that_breaks_the_contract_is_refused(
    artifact: dict[str, Any], why: str
) -> None:
    with pytest.raises(InvalidArtifact, match=why):
        _parse(artifact)


def test_an_artifact_is_refused_before_it_is_decoded_when_too_large_or_not_json() -> None:
    with pytest.raises(InvalidArtifact, match="bytes"):
        parse_artifact(b" " * 2000, fmt="pdf", max_bytes=1000)
    nan = json.dumps(_artifact(attrs={"x": float("nan")})).encode()
    good = json.dumps(_artifact(attrs={"x": 1.5})).encode()
    huge = [
        good.replace(b"1.5", n)
        for n in (b"1e999", b"-1e999", b"1.0e400", b"1" + b"0" * 400, b"9223372036854775808")
    ]
    assert parse_artifact(good, fmt="pdf", max_bytes=1 << 20).attrs == {"x": 1.5}
    edge = good.replace(b"1.5", b"-9223372036854775808")
    assert parse_artifact(edge, fmt="pdf", max_bytes=1 << 20).attrs == {"x": -(2**63)}
    for data in (b"{", b"[" * 100_000, b"[]", nan, *huge):
        with pytest.raises(InvalidArtifact):
            parse_artifact(data, fmt="pdf", max_bytes=1 << 20)


def test_completeness_is_recomputed_and_unknown_fields_are_dropped() -> None:
    a = _parse(
        _artifact(
            coverage={"unit": "pages", "total": 2, "extracted": 1},
            blocks=[_block(locator={"line_start": 1, "line_end": 1, "page": 3}, future=1)],
            newer_field={"x": 1},
        )
    )
    assert not a.coverage.complete
    assert a.blocks[0].locator.to_dict() == {"line_start": 1, "line_end": 1}
    assert "newer_field" not in a.to_dict()


@pytest.mark.parametrize(
    ("name", "media_type", "declared", "fmt"),
    [
        ("a.SQL", "", "", "sql"),
        ("notes", "text/markdown; charset=utf-8", "", "markdown"),
        ("a.md", "application/pdf", "", "markdown"),
        ("README", "", "", "text"),
        ("a.bin", "", "toml", "toml"),
        ("a.doc", "", "text", "text"),
    ],
)
def test_the_format_comes_from_the_declaration_then_the_name_then_the_media_type(
    name: str, media_type: str, declared: str, fmt: str
) -> None:
    assert format_of(name, media_type, declared) == fmt


def test_an_unknown_declared_format_or_a_legacy_name_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown format"):
        format_of("a.txt", declared="docm")
    for name in ("a.doc", "b.XLS", "c.ppt"):
        with pytest.raises(ValueError, match="not supported"):
            format_of(name)


@pytest.mark.parametrize(
    "change",
    [
        {"document_extractor_url": "extractor:8790"},
        {"document_extractor_url": "ftp://extractor"},
        {"document_extractor_url": "http://extractor:87a0"},
        {"document_extractor_url": "http://extractor:99999"},
        {"document_extractor_url": "http://extractor:8790?k=v"},
        {"document_extractor_url": "http://extractor:8790#x"},
        {"document_extractor_url": "http://extractor:8790?"},
        {"document_extractor_url": "http://extractor:8790#"},
        {"document_extractors_json": '{"pdf": "http://xn--a-.com"}'},
        {"document_extractors_json": '{"pdf": "file:///x"}'},
        {"document_extractors_json": '{"pdf": "http://"}'},
        {"document_extractors_json": '{"pdf": 1}'},
        {"document_extractors_json": "[]"},
    ],
)
def test_an_extractor_url_that_is_not_http_is_refused_without_naming_it(
    change: dict[str, str],
) -> None:
    from server.settings import Settings

    with pytest.raises(ValueError, match="LOOM_DOCUMENT_EXTRACTOR|document_extractors_json") as e:
        Settings(**change)  # type: ignore[arg-type]
    assert "file:///x" not in str(e.value) and "extractor:8790" not in str(e.value)


async def test_the_client_takes_its_timeout_and_token_from_the_settings(
    settings_override: Any,
) -> None:
    settings_override(document_extractor_timeout_s=0.5, document_extractor_token=TOKEN)
    async with extractors._client("http://ref") as client:
        assert client.timeout.read == client.timeout.connect == 0.5
        assert client.headers["authorization"] == f"Bearer {TOKEN}"
    settings_override(document_extractor_token="")
    async with extractors._client("http://ref") as client:
        assert "authorization" not in client.headers


async def test_each_service_gets_its_own_token_when_one_is_set(
    hosts: _Hosts, settings_override: Any
) -> None:
    seen: list[str] = []

    def record(r: Request) -> Response:
        seen.append(r.headers.get("authorization", ""))
        return Response(json.dumps(_artifact()).encode())

    _with_stub(hosts, settings_override, _stub(record))
    settings_override(document_extractor_tokens_json='{"http://stub/": "' + "p" * 24 + '"}')
    await extractors.extract(b"%PDF-1.7", name="a.pdf")
    await extractors.extract(b"# a", name="a.md")  # the reference service, the shared token
    assert seen == [f"Bearer {'p' * 24}"]
    settings_override(document_extractor_token="", document_extractor_tokens_json="{}")
    await extractors.extract(b"%PDF-1.7", name="a.pdf")
    assert seen[-1] == "", "no token, no Authorization header"


@pytest.mark.parametrize(
    "tokens",
    ['{"http://PDF-x:8080": "t"}', '{"http://pdf-x:8081": "t"}', '{"http://pdf-x:8080": 5}'],
)
def test_a_token_for_no_configured_url_or_not_a_string_is_refused_at_load(tokens: str) -> None:
    from server.settings import Settings

    with pytest.raises(ValueError, match="LOOM_DOCUMENT_EXTRACTOR_TOKENS_JSON|strings"):
        Settings(
            document_extractors_json='{"pdf": "http://pdf-x:8080"}',
            document_extractor_tokens_json=tokens,
        )


async def test_a_format_routed_only_by_media_type_is_available(
    hosts: _Hosts, settings_override: Any
) -> None:
    hosts.apps["stub"] = _stub(lambda r: Response(status_code=500), formats=["parquet"])
    settings_override(document_extractors_json='{"application/vnd.apache.parquet": "http://stub"}')
    assert (await extractors.available_formats())["parquet"] == "http://stub"


async def test_a_response_dripped_past_the_timeout_is_unavailable(
    hosts: _Hosts, settings_override: Any
) -> None:
    import asyncio

    from starlette.responses import StreamingResponse

    async def drip() -> Any:
        for _ in range(100):
            yield b" "
            await asyncio.sleep(0.05)

    _with_stub(hosts, settings_override, _stub(lambda r: StreamingResponse(drip())))
    settings_override(document_extractor_timeout_s=0.3)
    with pytest.raises(extractors.ExtractorUnavailable, match="timeout"):
        await extractors.extract(b"%PDF-1.7", name="a.pdf")


async def test_capabilities_nested_too_deep_are_unavailable_and_declare_nothing(
    hosts: _Hosts, settings_override: Any
) -> None:
    async def deep(request: Request) -> Response:
        return Response(b"[" * 60_000)

    hosts.apps["stub"] = Starlette(routes=[Route("/v1/capabilities", deep)])
    settings_override(document_extractors_json='{"pdf": "http://stub"}')
    with pytest.raises(extractors.ExtractorUnavailable, match="RecursionError"):
        await extractors.extract(b"%PDF-1.7", name="a.pdf")
    assert "pdf" not in await extractors.available_formats()


async def test_a_deep_toml_value_from_the_reference_extractor_passes_the_contract(
    hosts: _Hosts,
) -> None:
    a = await extractors.extract(
        b"a = " + b"[" * 20 + b"1" + b"]" * 20 + b"\nb = [[1]]\n", name="c.toml"
    )
    deep, shallow = a.blocks
    assert deep.attrs == {"value_type": "array"} and deep.text.startswith("a = [[[")
    assert shallow.attrs == {"value_type": "array", "value": [[1]]}


async def test_a_formats_own_route_hides_its_media_types_when_listing_too(
    hosts: _Hosts, settings_override: Any
) -> None:
    hosts.apps["stub"] = _stub(lambda r: Response(status_code=500), formats=["parquet"])
    settings_override(
        document_extractors_json='{"parquet": "http://ref", "application/vnd.apache.parquet": "http://stub"}'
    )
    assert "parquet" not in await extractors.available_formats(), "extract would send it to ref"
    assert extractors.endpoint_for("parquet", "application/vnd.apache.parquet") == "http://ref"


async def test_a_media_type_route_key_is_read_as_routing_reads_it(
    hosts: _Hosts, settings_override: Any
) -> None:
    settings_override(
        document_extractor_url="",
        document_extractors_json='{"Text/Markdown; charset=x": "http://ref"}',
    )
    assert (await extractors.available_formats()) == {"markdown": "http://ref"}
    a = await extractors.extract(b"# a", name="notes", media_type="text/markdown")
    assert a.format == "markdown"


@pytest.mark.parametrize("key", ["PDF", "markdwon", "image/png", ""])
def test_a_route_key_that_names_no_format_is_refused_at_load(key: str) -> None:
    from server.settings import Settings

    with pytest.raises(ValueError, match="neither a document format"):
        Settings(document_extractors_json=json.dumps({key: "http://x"}))


def test_two_spellings_of_one_media_type_route_are_refused_at_load() -> None:
    from server.settings import Settings

    with pytest.raises(ValueError, match="routes 'text/markdown' again"):
        Settings(
            document_extractors_json=json.dumps(
                {"text/markdown": "http://a", "Text/Markdown; charset=utf-8": "http://b"}
            )
        )


def test_two_spellings_of_one_token_url_are_refused_at_load() -> None:
    from server.settings import Settings

    with pytest.raises(ValueError, match="entry 2 names the URL of an earlier entry"):
        Settings(
            document_extractor_url="http://x",
            document_extractor_tokens_json='{"http://x": "' + "a" * 24 + '", "http://x/": "b"}',
        )


async def test_an_unavailable_answer_with_a_long_body_is_still_unavailable(
    hosts: _Hosts, settings_override: Any
) -> None:
    page = b"<html>" + b"x" * 5000 + b"</html>"
    _with_stub(hosts, settings_override, _stub(lambda r: Response(page, status_code=502)))
    settings_override(document_extractor_max_artifact_bytes=1024)
    with pytest.raises(extractors.ExtractorUnavailable, match="502"):
        await extractors.extract(b"%PDF-1.7", name="a.pdf")
    long_refusal = b'{"error": {"code": "corrupt", "message": "' + b"m" * 70_000 + b'"}}'
    hosts.apps["stub"] = _stub(lambda r: Response(long_refusal, status_code=422))
    with pytest.raises(extractors.ExtractionFailed) as e:
        await extractors.extract(b"%PDF-1.7", name="a.pdf")
    assert e.value.code == "refused", "an error body past its bound is read as no body"


def test_checking_long_keys_over_a_wide_list_is_linear() -> None:
    wide = _artifact(attrs={"k" * 2_000_000: [0] * 1_000_000})
    data = json.dumps(wide).encode()
    start = time.perf_counter()
    a = parse_artifact(data, fmt="pdf", max_bytes=1 << 26)
    assert len(a.attrs) == 1 and time.perf_counter() - start < 10


def test_a_refused_value_is_not_repeated_whole_in_the_error() -> None:
    with pytest.raises(InvalidArtifact) as e:
        _parse(_artifact(schema_version="v" * 10_000))
    assert len(str(e.value)) < 200


def test_the_windows_of_a_long_block_share_its_keys_and_chunk_in_linear_time() -> None:
    from server.knowledge.domain.extract import structural

    text = ("word " * 15 + "\n") * 13_000  # about 1 MB, about 850 windows
    keys = [f"k{i}" for i in range(10_000)]
    big = _artifact(blocks=[_block(text=text, keys=keys, section_path=["s"] * 64)])
    a = parse_artifact(json.dumps(big).encode(), fmt="pdf", max_bytes=1 << 26)
    start = time.perf_counter()
    chunks = structural.chunk_artifact(a, size=1500, stride=1230)
    assert time.perf_counter() - start < 5 and len(chunks) > 500
    assert all(c.keys is chunks[0].keys for c in chunks) and len(chunks[0].keys) == 10_000
    assert _parse(_artifact(blocks=[_block(keys=["a", "b", "a"])])).blocks[0].keys == ("a", "b")


async def test_a_listing_reads_an_unreachable_service_once(
    hosts: _Hosts, settings_override: Any
) -> None:
    settings_override(document_extractor_url="http://gone")
    assert await extractors.available_formats() == {}
    assert hosts.calls == [("gone", "/v1/capabilities")]


@pytest.mark.parametrize("version", [True, 1.0, "1"])
def test_capabilities_of_a_contract_version_that_is_not_the_integer_1_are_refused(
    version: Any,
) -> None:
    body = {"contract_version": version, "extractor": {"name": "x"}, "max_bytes": 1, "formats": []}
    with pytest.raises(ValueError, match="contract_version"):
        extractors._capabilities(json.dumps(body).encode())


async def test_a_listing_logs_no_credential_of_a_service_it_cannot_read(
    hosts: _Hosts, settings_override: Any, caplog: pytest.LogCaptureFixture
) -> None:
    settings_override(document_extractor_url="http://user:pass@gone:8790/x?key=k")
    assert await extractors.available_formats() == {}
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "http://gone:8790" in logged
    assert "pass" not in logged and "key=k" not in logged


def test_a_logged_origin_keeps_an_ipv6_hosts_brackets_and_drops_user_info() -> None:
    assert extractors._origin("http://u:p@[fd00::1]:8790/x") == "http://[fd00::1]:8790"


@pytest.mark.parametrize(
    ("status", "path"), [(200, "/v1/extract"), (422, "/v1/extract"), (200, "/v1/capabilities")]
)
async def test_a_compressed_answer_is_unavailable_and_never_inflated(
    hosts: _Hosts, settings_override: Any, status: int, path: str
) -> None:
    """A compressing proxy in front of the service: out of contract, so retried
    later, and no source refused for it."""
    import gzip

    bomb = gzip.compress(b" " * 10_000_000)
    seen: list[str] = []

    def gzipped(request: Request) -> Response:
        seen.append(request.headers.get("accept-encoding", ""))
        return Response(bomb, status_code=status, headers={"Content-Encoding": "gzip"})

    async def capabilities(request: Request) -> Response:
        return gzipped(request)

    app = _stub(gzipped)
    if path == "/v1/capabilities":
        app = Starlette(routes=[Route("/v1/capabilities", capabilities)])
    hosts.apps["stub"] = app
    settings_override(document_extractors_json='{"pdf": "http://stub"}')
    with pytest.raises(extractors.ExtractorUnavailable, match="'gzip' encoding"):
        await extractors.extract(b"%PDF-1.7", name="a.pdf")
    assert seen and set(seen) == {"identity"}
