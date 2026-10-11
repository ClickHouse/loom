"""A text-only knowledge document downloads as text.

A document that exists but has no preserved upload blob — every
`knowledge.add` / `upsert` caller that sends `content` only — is not a 404
`NO_BLOB`: its text is in `loom.documents.content` (the column
`knowledge.get` serves), so `GET /v1/knowledge.download` serves it as
Markdown and says which path answered in
`X-Loom-Download-Source: blob|text`. A document with a blob still serves
the blob; a missing document still 404s.

The route lives on the real app; `documents_repo.get_one` and the blob
store are monkey-patched, so no ClickHouse or disk is touched.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient

from server.knowledge.domain.documents import text_download_filename
from server.knowledge.store import documents as documents_repo
from server.knowledge.store.documents import DocumentRow
from server.storage import blob_store

TEXT = "# Alert digest\n\nDisk 91% on node-3.\n\n## Actions\n\n- page the on-call\n"


def _row(**over: Any) -> DocumentRow:
    base: dict[str, Any] = {
        "org": "acme",
        "namespace": "team-alerts",
        "source_uri": "agents/alert-digest-2026-09-19.json",
        "doc_id": "00000000-0000-0000-0000-000000000001",
        "title": "Alert digest 2026-09-19",
        "content": TEXT,
        "version": 1,
        "ingested_at": datetime(2026, 9, 19, tzinfo=UTC),
    }
    base.update(over)
    return DocumentRow(**base)


def _on_event_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


class _Store:
    def __init__(self, blobs: dict[str, bytes]) -> None:
        self.blobs = blobs

    def read(self, blob_path: str) -> bytes:
        # The read is file I/O: it runs in a worker thread, never on the loop.
        assert not _on_event_loop()
        try:
            return self.blobs[blob_path]
        except KeyError as e:
            raise FileNotFoundError(blob_path) from e


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    from server import settings as settings_mod
    from server.main import app

    monkeypatch.setattr(settings_mod.settings, "auth_required", False)
    # Auth off answers only a local Host.
    return TestClient(app, base_url="http://localhost")


def _serve(
    monkeypatch: pytest.MonkeyPatch, row: DocumentRow | None, blobs: dict[str, bytes] | None = None
) -> None:
    async def fake_get_one(
        org: str, namespace: str, source_uri: str, **_: Any
    ) -> DocumentRow | None:
        return row

    monkeypatch.setattr(documents_repo, "get_one", fake_get_one)
    monkeypatch.setattr(blob_store, "get_store", lambda: _Store(blobs or {}))


URL = "/v1/knowledge.download?org=acme&namespace=team-alerts&source_uri=agents/alert-digest-2026-09-19.json"


def test_text_only_document_downloads_its_stored_text(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _serve(monkeypatch, _row())
    r = client.get(URL)
    assert r.status_code == 200, r.text
    assert r.text == TEXT
    assert r.headers["content-type"] == "text/markdown; charset=utf-8"
    assert r.headers["content-disposition"] == 'attachment; filename="Alert-digest-2026-09-19.md"'
    assert r.headers["x-loom-download-source"] == "text"
    assert r.headers["content-length"] == str(len(TEXT.encode("utf-8")))
    assert r.headers["cache-control"] == "no-store"


def test_text_download_is_inline_when_asked(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _serve(monkeypatch, _row())
    r = client.get(URL + "&inline=true")
    assert r.status_code == 200
    assert r.headers["content-disposition"].startswith("inline; filename=")
    assert r.headers["x-loom-download-source"] == "text"


def test_text_download_without_title_names_the_last_uri_segment(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _serve(monkeypatch, _row(title=""))
    r = client.get(URL)
    assert r.status_code == 200
    assert (
        r.headers["content-disposition"] == 'attachment; filename="alert-digest-2026-09-19.json.md"'
    )


def test_document_with_a_blob_still_serves_the_blob(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    pdf = b"%PDF-1.7 fake"
    _serve(
        monkeypatch,
        _row(
            blob_path="acme/team-alerts/abc.pdf",
            blob_size=len(pdf),
            blob_content_type="application/pdf",
            original_filename="digest.pdf",
        ),
        {"acme/team-alerts/abc.pdf": pdf},
    )
    r = client.get(URL)
    assert r.status_code == 200
    assert r.content == pdf
    assert r.headers["content-type"] == "application/pdf"
    assert r.headers["content-disposition"] == 'attachment; filename="digest.pdf"'
    assert r.headers["x-loom-download-source"] == "blob"


def test_blob_gone_is_still_410(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _serve(monkeypatch, _row(blob_path="acme/team-alerts/missing.pdf", blob_size=3), {})
    r = client.get(URL)
    assert r.status_code == 410
    assert r.json()["detail"]["code"] == "BLOB_GONE"


def _auth_on(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    import server.auth.revocation as rev
    from server import settings as settings_mod
    from server.main import app

    monkeypatch.setattr(settings_mod.settings, "auth_required", True)
    monkeypatch.setattr(type(settings_mod.settings), "auth_actually_required", lambda _s: True)
    monkeypatch.setattr(rev, "_loaded", True)
    return TestClient(app)


def test_auth_on_without_a_bearer_is_401(monkeypatch: pytest.MonkeyPatch) -> None:
    _serve(monkeypatch, _row())
    r = _auth_on(monkeypatch).get(URL)
    assert r.status_code == 401
    assert r.json()["detail"]["code"] == "AUTH_INVALID"


def test_auth_on_outside_the_tokens_namespaces_is_403_before_any_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.auth.jwt import mint_token

    def _no_read(*_a: Any, **_k: Any) -> None:
        raise AssertionError("a refused caller must not reach the document store")

    monkeypatch.setattr(documents_repo, "get_one", _no_read)
    tok = mint_token(subject="s", org="acme", namespaces=["elsewhere"], roles=["viewer"])
    r = _auth_on(monkeypatch).get(URL, headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "AUTH_INSUFFICIENT"


def test_auth_on_with_a_reader_token_serves_the_document(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.auth.jwt import mint_token

    _serve(monkeypatch, _row())
    tok = mint_token(subject="s", org="acme", namespaces=["team-alerts"], roles=["viewer"])
    r = _auth_on(monkeypatch).get(URL, headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200
    assert r.text == TEXT


def test_auth_off_still_scopes_a_present_bearer(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Auth off, the HTTP transport's rule: a bearer that is sent is verified
    and scopes the read; only a missing one is served unscoped."""
    from server.auth import revocation as rev
    from server.auth.jwt import mint_token

    def _no_read(*_a: Any, **_k: Any) -> None:
        raise AssertionError("a refused caller must not reach the document store")

    monkeypatch.setattr(rev, "_loaded", True)
    monkeypatch.setattr(documents_repo, "get_one", _no_read)
    tok = mint_token(subject="s", org="acme", namespaces=["elsewhere"], roles=["viewer"])
    r = client.get(URL, headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 403
    assert client.get(URL, headers={"Authorization": "Bearer not-a-token"}).status_code == 401


def test_missing_document_is_404_not_found(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _serve(monkeypatch, None)
    r = client.get(URL)
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "NOT_FOUND"
    assert "x-loom-download-source" not in r.headers


@pytest.mark.parametrize(
    ("title", "uri", "expected"),
    [
        ("Ops Review: Q3 / 2026", "x/y.json", "Ops-Review-Q3-2026.md"),
        ("", "agents/alert digest.json", "alert-digest.json.md"),
        ("", "notes/readme.md", "readme.md"),
        ("", "notes/README.MD", "README.MD"),
        ("", "dir/", "dir.md"),
        ("", "", "document.md"),
        ("   ", "///", "document.md"),
        ('a"b\\c', "z", "a-b-c.md"),
        ("x" * 200, "z", "x" * 80 + ".md"),
    ],
)
def test_text_download_filename(title: str, uri: str, expected: str) -> None:
    assert text_download_filename(title, uri) == expected


# ---- inline preview: only passive types, and never runnable on its own ----

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
HTML = b"<script>parent.document.title='pwned'</script>"


def _blob(monkeypatch: pytest.MonkeyPatch, data: bytes, content_type: str) -> None:
    _serve(
        monkeypatch,
        _row(blob_path="b", blob_size=len(data), blob_content_type=content_type),
        {"b": data},
    )


@pytest.mark.parametrize(
    ("data", "content_type", "served"),
    [
        (b"plain", "text/plain", "text/plain; charset=utf-8"),
        (b"# md", "text/markdown", "text/plain; charset=utf-8"),
        (b"a,b", "text/csv; charset=latin-1", "text/plain; charset=utf-8"),
        (b"{}", "application/json", "text/plain; charset=utf-8"),
        (HTML, "text/plain", "text/plain; charset=utf-8"),  # HTML bytes stay text
        (PNG, "image/png", "image/png"),
        (b"%PDF-1.7", "application/pdf", "application/pdf"),
    ],
)
def test_passive_types_preview_inline(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, data: bytes, content_type: str, served: str
) -> None:
    _blob(monkeypatch, data, content_type)
    r = client.get(URL + "&inline=true")
    assert r.status_code == 200
    assert r.headers["content-type"] == served
    assert r.headers["content-disposition"].startswith("inline;")


@pytest.mark.parametrize(
    ("data", "content_type"),
    [
        (HTML, "text/html"),
        (b"<svg onload='alert(1)'/>", "image/svg+xml"),
        (b"<x/>", "application/xml"),
        (HTML, "image/png"),  # label and bytes disagree
        (HTML, "application/pdf"),
        (b"RIFF0000HTML", "image/webp"),
        (HTML, ""),
        (HTML, "application/x-unknown"),
    ],
)
def test_active_or_mismatched_types_download_as_octet_stream(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, data: bytes, content_type: str
) -> None:
    _blob(monkeypatch, data, content_type)
    for qs in ("&inline=true", ""):
        r = client.get(URL + qs)
        assert r.status_code == 200
        assert r.headers["content-type"] == "application/octet-stream"
        assert r.headers["content-disposition"].startswith("attachment;")


def test_every_download_is_unsniffable_and_sandboxed(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    for data, content_type in ((HTML, "text/plain"), (HTML, "text/html"), (PNG, "image/png")):
        _blob(monkeypatch, data, content_type)
        for qs in ("&inline=true", ""):
            r = client.get(URL + qs)
            assert r.headers["x-content-type-options"] == "nosniff"
            assert r.headers["content-security-policy"] == "sandbox; default-src 'none'"
