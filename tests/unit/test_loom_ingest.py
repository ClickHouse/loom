"""Unit tests for dev/loom_ingest.py — operator knowledge-ingest CLI."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from dev import loom_ingest  # noqa: E402


def test_parse_s3_uri_with_key() -> None:
    bucket, key = loom_ingest._parse_s3_uri("s3://example-bucket/foo/bar.pdf")
    assert bucket == "example-bucket"
    assert key == "foo/bar.pdf"


def test_parse_s3_uri_bucket_only() -> None:
    bucket, key = loom_ingest._parse_s3_uri("s3://example-bucket")
    assert bucket == "example-bucket"
    assert key == ""


def test_is_s3_prefix_trailing_slash() -> None:
    assert loom_ingest._is_s3_prefix("s3://example-bucket/dir/")


def test_is_s3_prefix_no_extension() -> None:
    assert loom_ingest._is_s3_prefix("s3://example-bucket/runbooks")


def test_is_s3_prefix_with_extension_is_object() -> None:
    assert not loom_ingest._is_s3_prefix("s3://example-bucket/foo/bar.pdf")
    assert not loom_ingest._is_s3_prefix("s3://example-bucket/note.md")


def test_extract_text_markdown(tmp_path: Path) -> None:
    p = tmp_path / "doc.md"
    p.write_text("# title\n\nbody text\n")
    assert "title" in loom_ingest._extract_text(p)


def test_extract_text_json(tmp_path: Path) -> None:
    p = tmp_path / "data.json"
    p.write_text('{"k": "v"}\n')
    text = loom_ingest._extract_text(p)
    assert '"k"' in text


def test_extract_text_pdf(tmp_path: Path) -> None:
    try:
        from pypdf import PdfReader, PdfWriter
    except ImportError:
        pytest.skip("pypdf not installed; install [ingest] extra")

    pdf_path = tmp_path / "doc.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    with pdf_path.open("wb") as f:
        writer.write(f)

    PdfReader(str(pdf_path))

    text = loom_ingest._extract_text(pdf_path)
    assert isinstance(text, str)


def test_extract_text_unknown_ext_falls_back_to_text(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    p = tmp_path / "weird.unknownext"
    p.write_text("plain content with a unicode-ish byte: ä")
    text = loom_ingest._extract_text(p)
    assert "plain content" in text
    err = capsys.readouterr().err
    assert "unknown-text list" not in err  # phrasing safety
    assert ".unknownext" in err and "warning" in err


class _FakeS3Client:
    """Stub boto3 S3 client recording download_file + list_objects_v2 calls."""

    def __init__(self, objects: dict[str, str] | None = None) -> None:
        self.objects: dict[str, str] = objects or {}
        self.download_calls: list[tuple[str, str, str]] = []

    def download_file(self, bucket: str, key: str, dest: str) -> None:
        self.download_calls.append((bucket, key, dest))
        content = self.objects.get(key, "# fetched from s3\n")
        Path(dest).write_text(content)

    def get_paginator(self, name: str):
        assert name == "list_objects_v2"

        class _Paginator:
            def __init__(self, parent: _FakeS3Client) -> None:
                self._parent = parent

            def paginate(self, Bucket: str, Prefix: str):
                contents = [{"Key": k} for k in self._parent.objects if k.startswith(Prefix)]
                yield {"Contents": contents}

        return _Paginator(self)


def _fake_jwt() -> str:
    # Minimum viable JWT _resolve_identity accepts: org=clickhouse, namespaces=[team].
    import base64
    import json

    payload = {"org": "clickhouse", "namespaces": ["team"]}
    payload_b64 = (
        base64.urlsafe_b64encode(
            json.dumps(payload).encode("utf-8"),
        )
        .rstrip(b"=")
        .decode("ascii")
    )
    return f"hdr.{payload_b64}.sig"


def _capture_posts(monkeypatch: pytest.MonkeyPatch, *, status: int = 200) -> list[dict[str, Any]]:
    """Route the SDK's HTTP client to a scripted server; returns the bodies
    posted to knowledge.add / knowledge.upsert, each with its `_op`."""
    import functools
    import json

    import httpx

    posted: list[dict[str, Any]] = []

    def handle(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content or b"{}")
        op = req.url.path.removeprefix("/v1/")
        if op.startswith("knowledge."):
            posted.append({**body, "_op": op})
            if status != 200:
                return httpx.Response(status, json={"detail": {"code": "VALIDATION_ERROR"}})
            return httpx.Response(200, json={"doc_id": "d-1", "chunks_created": 1})
        return httpx.Response(200, json={})

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        functools.partial(httpx.AsyncClient, transport=httpx.MockTransport(handle)),
    )
    return posted


def test_main_s3_single_object(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake = _FakeS3Client(objects={"docs/r1.md": "# runbook 1\nbody"})
    monkeypatch.setattr(loom_ingest, "_s3_client", lambda: fake)
    posted = _capture_posts(monkeypatch)

    rc = loom_ingest.main(
        [
            "s3://example-docs/docs/r1.md",
            "--base-url",
            "http://localhost:8080",
            "--token",
            _fake_jwt(),
            "--org",
            "clickhouse",
            "--namespace",
            "team",
        ]
    )
    assert rc == 0
    assert len(fake.download_calls) == 1
    assert fake.download_calls[0][0] == "example-docs"
    assert fake.download_calls[0][1] == "docs/r1.md"
    assert len(posted) == 1
    body = posted[0]
    assert body["org"] == "clickhouse"
    assert body["namespace"] == "team"
    assert body["source_uri"] == "s3://example-docs/docs/r1.md"
    assert "runbook 1" in body["content"]


def test_main_s3_prefix_loops(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake = _FakeS3Client(
        objects={
            "docs/r1.md": "# r1",
            "docs/r2.md": "# r2",
            "docs/r3.md": "# r3",
        }
    )
    monkeypatch.setattr(loom_ingest, "_s3_client", lambda: fake)
    posted = _capture_posts(monkeypatch)
    rc = loom_ingest.main(
        [
            "s3://example-docs/docs/",
            "--base-url",
            "http://localhost:8080",
            "--token",
            _fake_jwt(),
            "--org",
            "clickhouse",
            "--namespace",
            "team",
        ]
    )
    assert rc == 0
    assert len(fake.download_calls) == 3
    assert len(posted) == 3
    source_uris = {body["source_uri"] for body in posted}
    assert source_uris == {
        "s3://example-docs/docs/r1.md",
        "s3://example-docs/docs/r2.md",
        "s3://example-docs/docs/r3.md",
    }


def test_main_s3_prefix_empty_returns_1(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake = _FakeS3Client(objects={})
    monkeypatch.setattr(loom_ingest, "_s3_client", lambda: fake)
    rc = loom_ingest.main(
        [
            "s3://empty/",
            "--base-url",
            "http://localhost:8080",
            "--token",
            _fake_jwt(),
            "--org",
            "clickhouse",
            "--namespace",
            "team",
        ]
    )
    assert rc == 1
    assert "no objects found" in capsys.readouterr().err


def test_is_url_detection() -> None:
    assert loom_ingest._is_url("https://example.com/page.html")
    assert loom_ingest._is_url("http://example.com/page.html")
    assert not loom_ingest._is_url("s3://bucket/key")
    assert not loom_ingest._is_url("/local/path/file.md")
    assert not loom_ingest._is_url("file:///local/path")


class _FakeHTTPXResponse:
    def __init__(self, body: bytes, headers: dict[str, str]) -> None:
        self.content = body
        self.headers = headers

    def raise_for_status(self) -> None:
        return None


class _FakeHTTPXClient:
    def __init__(self, response_body: bytes, content_type: str = "text/html") -> None:
        self._body = response_body
        self._ctype = content_type

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return None

    def get(self, uri: str):
        return _FakeHTTPXResponse(
            self._body,
            {"Content-Type": self._ctype},
        )


def test_fetch_url_writes_correct_suffix_for_html(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx as _httpx

    monkeypatch.setattr(
        _httpx,
        "Client",
        lambda **kw: _FakeHTTPXClient(b"<html><body>x</body></html>"),
    )
    tmp, ctype = loom_ingest._fetch_url("https://example.com/page")
    try:
        assert tmp.suffix == ".html"
        assert ctype == "text/html"
        assert tmp.read_bytes().startswith(b"<html>")
    finally:
        tmp.unlink()


def test_fetch_url_uses_url_suffix_when_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx as _httpx

    monkeypatch.setattr(
        _httpx,
        "Client",
        lambda **kw: _FakeHTTPXClient(
            b"%PDF-1.4\n...",
            content_type="application/octet-stream",
        ),
    )
    tmp, _ = loom_ingest._fetch_url("https://example.com/spec.pdf")
    try:
        assert tmp.suffix == ".pdf"
    finally:
        tmp.unlink()


def test_fetch_url_rejects_oversize(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx as _httpx

    big_body = b"x" * (loom_ingest._URL_MAX_BYTES + 1)
    monkeypatch.setattr(
        _httpx,
        "Client",
        lambda **kw: _FakeHTTPXClient(big_body),
    )
    with pytest.raises(SystemExit) as exc:
        loom_ingest._fetch_url("https://example.com/big")
    assert exc.value.code == 1


def test_extract_html_with_trafilatura(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    try:
        import trafilatura  # noqa: F401
    except ImportError:
        pytest.skip("trafilatura not installed; covered by [ingest] extra")
    p = tmp_path / "page.html"
    p.write_text(
        "<html><body><nav>nav crud</nav>"
        "<main><p>The runbook says X.</p></main>"
        "<footer>footer crud</footer></body></html>"
    )
    out = loom_ingest._extract_html(p)
    assert "runbook says X" in out


def test_extract_html_falls_back_when_trafilatura_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "trafilatura":
            raise ImportError("synthetic")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    p = tmp_path / "page.html"
    p.write_text("<html><body><p>hello</p></body></html>")
    out = loom_ingest._extract_html(p)
    assert "hello" in out
    err = capsys.readouterr().err
    assert "trafilatura" in err.lower()


def test_main_url_single_object(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx as _httpx

    monkeypatch.setattr(
        _httpx,
        "Client",
        lambda **kw: _FakeHTTPXClient(
            b"<html><body><main>Q1 runbook content here.</main></body></html>",
            content_type="text/html",
        ),
    )
    posted = _capture_posts(monkeypatch)

    rc = loom_ingest.main(
        [
            "https://docs.example.com/q1-runbook",
            "--base-url",
            "http://localhost:8080",
            "--token",
            _fake_jwt(),
            "--org",
            "clickhouse",
            "--namespace",
            "team",
        ]
    )
    assert rc == 0
    assert len(posted) == 1
    body = posted[0]
    assert body["source_uri"] == "https://docs.example.com/q1-runbook"
    assert "Q1 runbook content" in body["content"]


def test_main_s3_missing_key_returns_2(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # s3://bucket (no key) routes through the prefix-loop branch; empty -> rc=1.
    fake = _FakeS3Client(objects={})
    monkeypatch.setattr(loom_ingest, "_s3_client", lambda: fake)
    rc = loom_ingest.main(
        [
            "s3://bucket",
            "--base-url",
            "http://localhost:8080",
            "--token",
            _fake_jwt(),
            "--org",
            "clickhouse",
            "--namespace",
            "team",
        ]
    )
    assert rc == 1  # empty prefix


# --- PDF outline -> Markdown headings -----------------------------------------


class _Dest:
    def __init__(self, title: str, page: int) -> None:
        self.title = title
        self._page = page


class _Reader:
    def __init__(self, outline: list) -> None:
        self.outline = outline

    def get_destination_page_number(self, dest: _Dest) -> int:
        if dest._page < 0:
            raise ValueError("dangling")
        return dest._page


def test_pdf_outline_is_flattened_in_order_with_depth() -> None:
    reader = _Reader(
        [
            _Dest("Chapter 1", 0),
            [_Dest("1.1 Scope", 0), _Dest("1.2 Terms", 1), [_Dest("1.2.1 Deep", 1)]],
            _Dest("Chapter 2", 2),
            _Dest("Broken", -1),
            _Dest("", 2),
        ]
    )
    assert loom_ingest._pdf_outline(reader) == [
        (0, "Chapter 1", 1),
        (0, "1.1 Scope", 2),
        (1, "1.2 Terms", 2),
        (1, "1.2.1 Deep", 3),
        (2, "Chapter 2", 1),
    ]
    assert loom_ingest._pdf_outline(_Reader(None)) == []  # type: ignore[arg-type]


def test_pdf_pages_to_markdown_places_headings_where_sections_start() -> None:
    pages = [
        "Front matter.\nChapter 1\nIntro text.\n1.1 Scope\nScope text.",
        "1.2 Terms\nTerms text continues.",
        "Unrelated page without outline entries.",
        "Chapter 2 starts here.",
    ]
    outline = [
        (0, "Chapter 1", 1),
        (0, "1.1 Scope", 2),
        (1, "1.2 Terms", 2),
        (3, "Chapter 2", 1),
        (3, "Missing Title", 2),
    ]
    md = loom_ingest.pdf_pages_to_markdown(pages, outline)
    # headings inserted at the title's first occurrence on its page, front matter kept before
    assert md.index("Front matter.") < md.index("# Chapter 1") < md.index("Intro text.")
    assert md.index("Intro text.") < md.index("## 1.1 Scope") < md.index("Scope text.")
    assert "## 1.2 Terms\n\n1.2 Terms\nTerms text continues." in md
    assert "Unrelated page without outline entries." in md
    # a title not found on its page still gets a heading, before the remaining text
    assert md.index("# Chapter 2") < md.index("## Missing Title")
    # depth caps at six hashes
    deep = loom_ingest.pdf_pages_to_markdown(["x"], [(0, "Deep", 9)])
    assert deep.startswith("###### Deep")
    # no outline: legacy join of non-empty pages
    assert loom_ingest.pdf_pages_to_markdown(["a", "", "b"], []) == "a\n\nb"


def test_pdf_markdown_chunks_by_section_end_to_end() -> None:
    from server.knowledge.domain.chunking import chunk_document

    pages = ["Chapter 1\n" + "alpha " * 60, "1.1 Scope\n" + "beta " * 60]
    md = loom_ingest.pdf_pages_to_markdown(pages, [(0, "Chapter 1", 1), (1, "1.1 Scope", 2)])
    chunks = chunk_document(md, size=200, stride=160)
    paths = {c.section_path for c in chunks}
    assert ("Chapter 1",) in paths and ("Chapter 1", "1.1 Scope") in paths


_ID = ["--base-url", "http://loom.test", "--token", "t", "--org", "o", "--namespace", "n"]


@pytest.mark.parametrize("flag", [["--source-uri", "github://x"], ["--title", "T"]])
def test_s3_prefix_refuses_a_single_document_flag(
    monkeypatch: pytest.MonkeyPatch, flag: list[str]
) -> None:
    """One --source-uri for many objects made every upsert supersede the last."""
    fake = _FakeS3Client(objects={"docs/r1.md": "# r1", "docs/r2.md": "# r2"})
    monkeypatch.setattr(loom_ingest, "_s3_client", lambda: fake)
    posted = _capture_posts(monkeypatch)
    assert loom_ingest.main(["s3://b/docs/", *_ID, *flag]) == 2
    assert posted == [] and fake.download_calls == []


def test_local_file_goes_through_the_sdk_and_upsert_is_honoured(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    doc = tmp_path / "spec.md"
    doc.write_text("# Spec\nbody")
    posted = _capture_posts(monkeypatch)
    assert loom_ingest.main([str(doc), *_ID, "--upsert", "--tags", "a, b"]) == 0
    (body,) = posted
    assert body["_op"] == "knowledge.upsert"
    assert (body["org"], body["namespace"]) == ("o", "n")
    assert body["source_uri"] == f"file://{doc.resolve()}"
    assert body["tags"] == ["a", "b"] and body["title"] == "spec"
    assert body["metadata"]["ingested_format"] == ".md"


def test_a_rejected_object_is_counted_and_the_rest_still_ingest(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = _FakeS3Client(objects={"docs/r1.md": "# r1", "docs/r2.md": "# r2"})
    monkeypatch.setattr(loom_ingest, "_s3_client", lambda: fake)
    posted = _capture_posts(monkeypatch, status=422)
    assert loom_ingest.main(["s3://b/docs/", *_ID]) == 1
    assert len(posted) == 2
    assert "2 of 2 failed" in capsys.readouterr().err


def test_dry_run_posts_nothing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    doc = tmp_path / "n.txt"
    doc.write_text("hello")
    posted = _capture_posts(monkeypatch)
    assert loom_ingest.main([str(doc), *_ID, "--dry-run"]) == 0
    assert posted == []


def test_identity_comes_from_the_token_claims(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    doc = tmp_path / "n.txt"
    doc.write_text("hello")
    posted = _capture_posts(monkeypatch)
    assert loom_ingest.main([str(doc), "--base-url", "http://x", "--token", _fake_jwt()]) == 0
    assert (posted[0]["org"], posted[0]["namespace"]) == ("clickhouse", "team")
