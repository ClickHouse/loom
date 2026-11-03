"""A text-only knowledge document downloads as text.

A document that exists but has no preserved upload blob — every
`knowledge.add` / `upsert` caller that sends `content` only — is not a 404
`NO_BLOB`: its text is in `loom.documents.content` (the column
`knowledge.get` serves), so `GET /v1/knowledge.download` serves it as
Markdown and says which path answered in
`X-Loom-Download-Source: blob|text`. A document with a blob still serves
the blob; a missing document still 404s.

The route lives on the real app; `documents_repo.get_one` and the blob
store are monkey-patched, so no ClickHouse is touched.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient

from server.knowledge.app import download_routes
from server.knowledge.domain.documents import text_download_filename
from server.knowledge.store import blobs as blob_store
from server.knowledge.store import documents as documents_repo
from server.knowledge.store.documents import DocumentRow

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

    async def fake_locate(org: str, namespace: str, sha: str, **_: Any) -> blob_store.Stored | None:
        data = (blobs or {}).get(sha)
        return None if data is None else blob_store.Stored(len(data), "")

    @asynccontextmanager
    async def fake_stream(
        org: str, namespace: str, sha: str, *, stored: blob_store.Stored, **_: Any
    ) -> AsyncIterator[Any]:
        data = (blobs or {}).get(sha, b"")

        async def chunks() -> AsyncIterator[bytes]:
            for i in range(0, len(data), 5):  # in pieces, as the HTTP response arrives
                yield data[i : i + 5]

        opened.append(sha)
        try:
            yield chunks()
        finally:
            closed.append(sha)

    async def fake_read(
        org: str, namespace: str, sha: str, *, stored: blob_store.Stored, **_: Any
    ) -> bytes | None:
        # As `blobs.read`: None when no row holds the original whole.
        data = (blobs or {}).get(sha)
        return data if data is not None and len(data) == stored.size else None

    monkeypatch.setattr(blob_store, "locate", fake_locate)
    monkeypatch.setattr(blob_store, "read", fake_read)
    monkeypatch.setattr(blob_store, "stream", fake_stream)


opened: list[str] = []
closed: list[str] = []


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
            blob_path="a" * 64,
            blob_size=len(pdf),
            blob_content_type="application/pdf",
            original_filename="digest.pdf",
        ),
        {"a" * 64: pdf},
    )
    r = client.get(URL)
    assert r.status_code == 200
    assert r.content == pdf
    assert r.headers["content-type"] == "application/octet-stream"
    assert r.headers["content-disposition"] == 'attachment; filename="digest.pdf"'
    assert r.headers["x-loom-download-source"] == "blob"


def test_blob_gone_is_still_410(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _serve(monkeypatch, _row(blob_path="c" * 64, blob_size=3), {})
    r = client.get(URL)
    assert r.status_code == 410
    assert r.json()["detail"]["code"] == "BLOB_GONE"


def test_a_large_blob_streams_in_chunks_and_closes_its_stream(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = bytes(range(256)) * 4
    monkeypatch.setattr(download_routes, "_BUFFER_MAX_BYTES", 100)
    _serve(monkeypatch, _row(blob_path="d" * 64, blob_content_type="image/png"), {"d" * 64: data})
    opened.clear()
    closed.clear()
    r = client.get(URL, headers={"Accept-Encoding": "identity"})
    assert r.status_code == 200 and r.content == data
    assert r.headers["content-length"] == str(len(data))
    assert opened == closed == ["d" * 64]


def test_a_small_blob_is_read_whole_before_the_answer_starts(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No ClickHouse connection waits on the client reading the body."""
    data = bytes(range(256)) * 4
    _serve(monkeypatch, _row(blob_path="d" * 64), {"d" * 64: data})
    opened.clear()
    r = client.get(URL)
    assert r.status_code == 200 and r.content == data
    assert r.headers["x-loom-download-source"] == "blob"
    assert opened == []


@pytest.mark.parametrize("buffer_max", [100, 10_000])
def test_an_original_deleted_after_its_size_was_read_is_410(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, buffer_max: int
) -> None:
    data = b"x" * 1000
    store = {"e" * 64: data}
    _serve(monkeypatch, _row(blob_path="e" * 64), store)
    locate = blob_store.locate

    async def locate_then_delete(*a: Any, **k: Any) -> blob_store.Stored | None:
        found = await locate(*a, **k)
        store.clear()
        return found

    monkeypatch.setattr(blob_store, "locate", locate_then_delete)
    monkeypatch.setattr(download_routes, "_BUFFER_MAX_BYTES", buffer_max)
    r = client.get(URL)
    assert r.status_code == 410 and r.json()["detail"]["code"] == "BLOB_GONE"


def test_an_original_this_replica_has_not_seen_is_read_again_sequentially(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An upload through another replica that has not reached this one is not a 410."""
    from server.core.ch import sequential_reads_active

    data = b"%PDF-1.7 fresh"
    _serve(monkeypatch, _row(blob_path="f" * 64), {"f" * 64: data})
    locate = blob_store.locate

    async def lagging_locate(*a: Any, **k: Any) -> blob_store.Stored | None:
        return await locate(*a, **k) if sequential_reads_active() else None

    monkeypatch.setattr(blob_store, "locate", lagging_locate)
    r = client.get(URL)
    assert r.status_code == 200 and r.content == data


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
        (b"%PDF-1.7", "application/pdf"),  # a PDF viewer runs the file's script
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
            # its own policy, with no second one beside it
            assert r.headers.get_list("content-security-policy") == [
                "sandbox; default-src 'none'; frame-ancestors 'none'"
            ]
            assert r.headers["x-frame-options"] == "DENY"
