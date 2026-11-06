"""Unit tests for dev/loom_ingest.py — operator knowledge-ingest CLI."""

from __future__ import annotations

import hashlib
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


def test_is_url_detection() -> None:
    assert loom_ingest._is_url("https://example.com/page.html")
    assert loom_ingest._is_url("http://example.com/page.html")
    assert not loom_ingest._is_url("s3://bucket/key")
    assert not loom_ingest._is_url("/local/path/file.md")
    assert not loom_ingest._is_url("file:///local/path")


class _FakeS3Client:
    """Stub boto3 S3 client recording get_object + list_objects_v2 calls."""

    def __init__(self, objects: dict[str, bytes] | None = None, content_type: str = "") -> None:
        self.objects: dict[str, bytes] = objects or {}
        self.content_type = content_type
        self.get_calls: list[tuple[str, str]] = []

    def get_object(self, Bucket: str, Key: str) -> dict[str, Any]:
        import io

        self.get_calls.append((Bucket, Key))
        body = io.BytesIO(self.objects.get(Key, b"# fetched from s3\n"))
        return {"Body": body, "ContentType": self.content_type}

    def get_paginator(self, name: str):
        assert name == "list_objects_v2"

        class _Paginator:
            def __init__(self, parent: _FakeS3Client) -> None:
                self._parent = parent

            def paginate(self, Bucket: str, Prefix: str):
                contents = [{"Key": k} for k in self._parent.objects if k.startswith(Prefix)]
                yield {"Contents": contents}

        return _Paginator(self)


class _Server:
    """The SDK's HTTP client routed to a scripted server: each upload, each
    ingest body, and each job ending in `state` after one `processing` poll."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, *, state: str = "ready") -> None:
        import functools

        import httpx

        self.state = state
        self.uploads: list[dict[str, Any]] = []
        self.ingests: list[dict[str, Any]] = []
        self.polls = 0
        monkeypatch.setattr(
            httpx,
            "AsyncClient",
            functools.partial(httpx.AsyncClient, transport=httpx.MockTransport(self._handle)),
        )

    def _handle(self, req: Any) -> Any:
        import json

        import httpx

        op = req.url.path.removeprefix("/v1/")
        if op == "knowledge.upload":
            params = dict(req.url.params)
            self.uploads.append({**params, "data": req.content})
            n = len(self.uploads)
            return httpx.Response(201, json={"upload_id": f"up-{n}", "format": "markdown"})
        body = json.loads(req.content or b"{}")
        if op == "knowledge.ingest":
            self.ingests.append(body)
            if self.state == "refused":
                return httpx.Response(422, json={"detail": {"code": "VALIDATION_ERROR"}})
            n = len(self.ingests)
            return httpx.Response(
                200, json={"ingestion_id": f"job-{n}", "state": "pending", "source_uri": "x"}
            )
        if op == "knowledge.ingest_status":
            self.polls += 1
            if self.state == "gone":
                return httpx.Response(200, json={"found": False, "ingestion_id": "job-1"})
            if self.state == "stuck":
                return httpx.Response(200, json={"found": True, "state": "processing"})
            if self.polls % 2:  # every job is seen running once before it ends
                return httpx.Response(200, json={"found": True, "state": "processing"})
            ended = {"found": True, "state": self.state, "revision": 1, "chunks": 3}
            if self.state == "failed":
                ended |= {"error_code": "format_unavailable", "error_message": "no extractor"}
            return httpx.Response(200, json={"format": "markdown", **ended})
        return httpx.Response(200, json={})


@pytest.fixture(autouse=True)
def _no_poll_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    real_sleep = asyncio.sleep

    async def _sleep(delay: float) -> None:
        await real_sleep(0)

    monkeypatch.setattr("loom.knowledge.asyncio.sleep", _sleep)


def _fake_jwt() -> str:
    # Minimum viable JWT _resolve_identity accepts: org=clickhouse, namespaces=[team].
    import base64
    import json

    payload = {"org": "clickhouse", "namespaces": ["team"]}
    payload_b64 = (
        base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8")).rstrip(b"=").decode("ascii")
    )
    return f"hdr.{payload_b64}.sig"


_ID = ["--base-url", "http://loom.test", "--token", "t", "--org", "o", "--namespace", "n"]


def test_a_local_file_is_uploaded_as_its_bytes_and_ingested(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    doc = tmp_path / "spec.pdf"
    doc.write_bytes(b"%PDF-1.4 not parsed here")
    server = _Server(monkeypatch)
    assert loom_ingest.main([str(doc), *_ID, "--tags", "a, b"]) == 0
    # The file goes as it is: the server's extractor parses it, never this CLI.
    (up,) = server.uploads
    assert up["data"] == b"%PDF-1.4 not parsed here" and up["filename"] == "spec.pdf"
    assert up["sha256"] == hashlib.sha256(up["data"]).hexdigest()
    assert (up["org"], up["namespace"]) == ("o", "n")
    (body,) = server.ingests
    assert body["upload_id"] == "up-1" and body["source_uri"] == f"file://{doc.resolve()}"
    assert body["tags"] == ["a", "b"] and body["title"] == "spec"
    assert body["metadata"] == {"ingested_from": f"file://{doc.resolve()}"}
    assert body["allow_partial"] is False and body["options"] == {}
    # It waited for the job and says how it ended.
    assert server.polls == 2
    out = capsys.readouterr().out
    assert "revision=1 chunks=3 format=markdown state=ready" in out


def test_the_document_flags_reach_the_ingest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    doc = tmp_path / "notes"
    doc.write_bytes(b"# Notes")
    server = _Server(monkeypatch)
    args = [str(doc), *_ID, "--source-uri", "github://x/notes", "--title", "Notes"]
    assert loom_ingest.main([*args, "--format", "markdown", "--allow-partial"]) == 0
    (body,) = server.ingests
    assert (body["source_uri"], body["title"]) == ("github://x/notes", "Notes")
    assert body["options"] == {"format": "markdown"} and body["allow_partial"] is True


def test_no_wait_prints_the_job_and_polls_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    doc = tmp_path / "n.txt"
    doc.write_text("hello")
    server = _Server(monkeypatch)
    assert loom_ingest.main([str(doc), *_ID, "--no-wait"]) == 0
    assert server.polls == 0
    assert "queued → ingestion_id=job-1" in capsys.readouterr().out


def test_a_failed_job_is_reported_with_its_code_and_fails_the_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    doc = tmp_path / "n.xyz"
    doc.write_text("hello")
    _Server(monkeypatch, state="failed")
    assert loom_ingest.main([str(doc), *_ID]) == 1
    err = capsys.readouterr().err
    assert "failed (format_unavailable): no extractor" in err and "1 of 1 failed" in err


def test_a_job_still_running_past_the_timeout_fails_the_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    doc = tmp_path / "n.txt"
    doc.write_text("hello")
    _Server(monkeypatch, state="stuck")
    assert loom_ingest.main([str(doc), *_ID, "--timeout", "0"]) == 1
    err = capsys.readouterr().err
    assert "job-1 is still processing after 0s" in err and "1 of 1 failed" in err


def test_a_job_the_server_cannot_find_is_reported_as_not_found(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    doc = tmp_path / "n.txt"
    doc.write_text("hello")
    server = _Server(monkeypatch, state="gone")
    assert loom_ingest.main([str(doc), *_ID]) == 1
    assert server.polls == 1
    assert "not found: no such job" in capsys.readouterr().err


def test_a_partial_job_published_and_counts_as_ingested(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    doc = tmp_path / "n.pdf"
    doc.write_bytes(b"%PDF")
    _Server(monkeypatch, state="partial")
    assert loom_ingest.main([str(doc), *_ID, "--allow-partial"]) == 0
    assert "state=partial" in capsys.readouterr().out


def test_main_s3_single_object(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeS3Client(objects={"docs/r1.md": b"# runbook 1\nbody"})
    monkeypatch.setattr(loom_ingest, "_s3_client", lambda: fake)
    server = _Server(monkeypatch)
    assert loom_ingest.main(["s3://example-docs/docs/r1.md", *_ID]) == 0
    assert fake.get_calls == [("example-docs", "docs/r1.md")]
    (up,) = server.uploads
    assert (up["filename"], up["data"]) == ("r1.md", b"# runbook 1\nbody")
    assert server.ingests[0]["source_uri"] == "s3://example-docs/docs/r1.md"


def test_an_s3_key_with_no_extension_is_named_by_its_media_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeS3Client(objects={"docs/notes": b"# n"}, content_type="text/markdown")
    monkeypatch.setattr(loom_ingest, "_s3_client", lambda: fake)
    server = _Server(monkeypatch)
    assert loom_ingest.main(["s3://b/docs/", *_ID]) == 0
    (up,) = server.uploads
    assert (up["filename"], up["content_type"], up["data"]) == ("notes", "text/markdown", b"# n")


def test_main_s3_prefix_loops(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeS3Client(
        objects={"docs/r1.md": b"# r1", "docs/r2.md": b"# r2", "docs/r3.md": b"# r3"}
    )
    monkeypatch.setattr(loom_ingest, "_s3_client", lambda: fake)
    server = _Server(monkeypatch)
    assert loom_ingest.main(["s3://example-docs/docs/", *_ID]) == 0
    assert {b["source_uri"] for b in server.ingests} == {
        "s3://example-docs/docs/r1.md",
        "s3://example-docs/docs/r2.md",
        "s3://example-docs/docs/r3.md",
    }


def test_main_s3_prefix_empty_returns_1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(loom_ingest, "_s3_client", lambda: _FakeS3Client(objects={}))
    assert loom_ingest.main(["s3://empty/", *_ID]) == 1
    assert "no objects found" in capsys.readouterr().err


def test_a_bucket_with_no_key_is_listed_as_a_prefix(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(loom_ingest, "_s3_client", lambda: _FakeS3Client(objects={}))
    assert loom_ingest.main(["s3://bucket", *_ID]) == 1
    assert "no objects found under s3://bucket" in capsys.readouterr().err
    # A dot in the bucket's name does not make it an object.
    assert loom_ingest._is_s3_prefix("s3://acme.docs")
    assert loom_ingest.main(["s3://acme.docs", *_ID]) == 1
    assert "no objects found under s3://acme.docs" in capsys.readouterr().err


@pytest.mark.parametrize("flag", [["--source-uri", "github://x"], ["--title", "T"]])
def test_s3_prefix_refuses_a_single_document_flag(
    monkeypatch: pytest.MonkeyPatch, flag: list[str]
) -> None:
    """One --source-uri for many objects would make each a revision of the last."""
    fake = _FakeS3Client(objects={"docs/r1.md": b"# r1", "docs/r2.md": b"# r2"})
    monkeypatch.setattr(loom_ingest, "_s3_client", lambda: fake)
    server = _Server(monkeypatch)
    assert loom_ingest.main(["s3://b/docs/", *_ID, *flag]) == 2
    assert server.uploads == [] and fake.get_calls == []


def test_a_refused_object_is_counted_and_the_rest_still_ingest(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = _FakeS3Client(objects={"docs/r1.md": b"# r1", "docs/r2.md": b"# r2"})
    monkeypatch.setattr(loom_ingest, "_s3_client", lambda: fake)
    server = _Server(monkeypatch, state="refused")
    assert loom_ingest.main(["s3://b/docs/", *_ID]) == 1
    assert len(server.ingests) == 2
    assert "2 of 2 failed" in capsys.readouterr().err


def test_an_object_that_cannot_be_fetched_is_counted_and_the_rest_still_ingest(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = _FakeS3Client(objects={"docs/r1.md": b"# r1", "docs/r2.md": b"# r2"})

    real_get = fake.get_object

    def _get(Bucket: str, Key: str) -> dict[str, Any]:
        if Key == "docs/r1.md":
            raise RuntimeError("AccessDenied")
        return real_get(Bucket=Bucket, Key=Key)

    monkeypatch.setattr(fake, "get_object", _get)
    monkeypatch.setattr(loom_ingest, "_s3_client", lambda: fake)
    server = _Server(monkeypatch)
    assert loom_ingest.main(["s3://b/docs/", *_ID]) == 1
    assert [b["source_uri"] for b in server.ingests] == ["s3://b/docs/r2.md"]
    err = capsys.readouterr().err
    assert "s3://b/docs/r1.md: AccessDenied" in err and "1 of 2 failed" in err


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
        return _FakeHTTPXResponse(self._body, {"Content-Type": f"{self._ctype}; charset=utf-8"})


def test_a_url_is_uploaded_with_its_media_type_and_path_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx as _httpx

    page = b"<html><body><main>Q1 runbook content here.</main></body></html>"
    monkeypatch.setattr(_httpx, "Client", lambda **kw: _FakeHTTPXClient(page))
    server = _Server(monkeypatch)
    assert loom_ingest.main(["https://docs.example.com/q1-runbook?v=2", *_ID]) == 0
    # No extension on the path: the media type names the format, on the server.
    (up,) = server.uploads
    assert (up["filename"], up["content_type"], up["data"]) == ("q1-runbook", "text/html", page)
    assert server.ingests[0]["source_uri"] == "https://docs.example.com/q1-runbook?v=2"


def test_a_url_s_escaped_name_is_uploaded_as_written(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx as _httpx

    monkeypatch.setattr(_httpx, "Client", lambda **kw: _FakeHTTPXClient(b"%PDF"))
    assert loom_ingest._fetch_url("https://example.com/Q3%20Spec.pdf").filename == "Q3 Spec.pdf"


def test_a_url_with_no_path_is_named_index(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx as _httpx

    monkeypatch.setattr(_httpx, "Client", lambda **kw: _FakeHTTPXClient(b"<html/>"))
    assert loom_ingest._fetch_url("https://example.com/").filename == "index"


def test_fetch_url_rejects_oversize(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx as _httpx

    big_body = b"x" * (loom_ingest._URL_MAX_BYTES + 1)
    monkeypatch.setattr(_httpx, "Client", lambda **kw: _FakeHTTPXClient(big_body))
    with pytest.raises(RuntimeError, match="too large"):
        loom_ingest._fetch_url("https://example.com/big")


def test_dry_run_sends_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    doc = tmp_path / "n.txt"
    doc.write_text("hello")
    server = _Server(monkeypatch)
    assert loom_ingest.main([str(doc), *_ID, "--dry-run"]) == 0
    assert server.uploads == [] and server.ingests == []
    out = capsys.readouterr().out
    assert "would upload 'n.txt' (5 bytes" in out and '"source_uri"' in out


def test_identity_comes_from_the_token_claims(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    doc = tmp_path / "n.txt"
    doc.write_text("hello")
    server = _Server(monkeypatch)
    assert loom_ingest.main([str(doc), "--base-url", "http://x", "--token", _fake_jwt()]) == 0
    assert (server.ingests[0]["org"], server.ingests[0]["namespace"]) == ("clickhouse", "team")
