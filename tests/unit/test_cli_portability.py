"""`loom signing-key` and `loom blobs import` against a scripted ClickHouse."""

from __future__ import annotations

import asyncio
import hashlib
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from server.cli import portability
from server.cli.app import main
from server.core import leases
from server.knowledge.app import originals


class _CH:
    def __init__(self, documents: list[dict[str, Any]] | None = None) -> None:
        self.documents = documents or []
        self.inserts: list[tuple[str, list[dict[str, Any]]]] = []
        self.executed: list[tuple[str, Any]] = []
        self.claims: list[tuple[str, list[str]]] = []
        self.holding = False
        self.claim_held = True

    async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
        if "loom.documents" in sql:
            return self.documents
        if "sealed_key" in sql:  # the keyring read: k1 signs
            from datetime import datetime

            from server.core.secrets_at_rest import seal

            return [
                {
                    "kid": "k1",
                    "algorithm": "HS256",
                    "sealed_key": seal("signing_key", "s" * 40),
                    "public_pem": "",
                    "activate_at": datetime(2026, 1, 1),
                    "retired": False,
                }
            ]
        return [{"kid": "k1", "algorithm": "HS256", "activate_at": "t", "retired_at": None}]

    async def query_one(self, sql: str, params: Any = None) -> dict[str, Any] | None:
        return None

    async def insert(self, table: str, rows: list[dict[str, Any]], settings: Any = None) -> None:
        self.inserts.append((table, rows))

    async def execute(self, sql: str, params: Any = None, **_: Any) -> None:
        self.executed.append((sql, params))


@pytest.fixture
def ch(monkeypatch: pytest.MonkeyPatch) -> _CH:
    from server.settings import settings

    monkeypatch.setattr(settings, "jwt_secret", "r" * 40)
    fake = _CH()
    monkeypatch.setattr(portability, "run_connected", lambda run: asyncio.run(run(fake)))

    @asynccontextmanager
    async def hold(org: str, namespaces: Any, **_: Any):
        fake.claims.append((org, list(namespaces)))
        fake.holding = True
        yield lambda: fake.claim_held
        fake.holding = False

    monkeypatch.setattr(originals, "hold_originals", hold)

    class _Held:
        held = True

        @asynccontextmanager
        async def renewed(self) -> Any:
            yield self

    async def take(*_: Any, **__: Any) -> _Held:
        return _Held()

    monkeypatch.setattr(leases, "take", take)  # the retire claim
    return fake


def test_rotate_refuses_the_dev_secret_and_a_short_activation(
    ch: _CH, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A key sealed with the dev secret is one no server unseals; one that signs
    before every server has read it is refused by the others."""
    from server.settings import DEV_JWT_SECRET, settings

    assert main(["signing-key", "rotate", "--activate-in", "1"]) == 1
    monkeypatch.setattr(settings, "jwt_secret", DEV_JWT_SECRET)
    assert main(["signing-key", "rotate"]) == 1
    assert ch.inserts == []


def test_signing_key_rotate_list_and_retire(ch: _CH, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["signing-key", "rotate"]) == 0
    table, rows = ch.inserts[-1]
    assert table == "loom.signing_keys" and rows[0]["algorithm"] == "HS256"
    assert rows[0]["kid"] in capsys.readouterr().out
    assert ch.inserts[0][1][0]["kid"] == "default"  # the root secret, recorded
    assert main(["signing-key", "list"]) == 0
    out = capsys.readouterr().out
    assert "default  HS256  (LOOM_JWT_SECRET)" in out and "k1  HS256" in out
    assert main(["signing-key", "retire", "nope"]) == 1
    assert main(["signing-key", "retire", "default"]) == 0
    assert ch.inserts[-1][1][0]["kid"] == "default"


def test_signing_key_record_root_records_this_root_secret(
    ch: _CH, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After an on-purpose root replacement: the keyring commands accept the new one."""
    from server.auth import keyring
    from server.settings import DEV_JWT_SECRET, settings

    assert main(["signing-key", "record-root"]) == 0
    table, rows = ch.inserts[-1]
    assert table == "loom.signing_keys" and rows[0]["kid"] == "default"
    assert keyring._is_this_root(rows[0]["sealed_key"])
    monkeypatch.setattr(settings, "jwt_secret", DEV_JWT_SECRET)
    assert main(["signing-key", "record-root"]) == 1
    assert len(ch.inserts) == 1


def _legacy(tmp_path: Path, data: bytes) -> str:
    sha = hashlib.sha256(data).hexdigest()
    f = tmp_path / "o" / "ns" / sha[:2] / sha
    f.parent.mkdir(parents=True)
    f.write_bytes(data)
    return sha


def test_blobs_import_is_a_dry_run_by_default(ch: _CH, tmp_path: Path) -> None:
    sha = _legacy(tmp_path, b"original")
    ch.documents = [
        {"org": "o", "namespace": "ns", "source_uri": "d", "blob_path": f"o/ns/{sha[:2]}/{sha}"}
    ]
    assert main(["blobs", "import", str(tmp_path)]) == 0
    assert ch.inserts == []


def test_blobs_import_moves_the_bytes_and_repoints_the_document(
    ch: _CH, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.knowledge.store import documents as documents_repo

    sha = _legacy(tmp_path, b"original")
    ch.documents = [
        {"org": "o", "namespace": "ns", "source_uri": "d", "blob_path": f"o/ns/{sha[:2]}/{sha}"},
        {"org": "o", "namespace": "ns", "source_uri": "gone", "blob_path": "o/ns/ff/" + "f" * 64},
    ]
    written: list[dict[str, Any]] = []

    async def get_one(org: str, ns: str, uri: str, **_: Any) -> Any:
        path = written[-1]["blob_path"] if written else f"o/ns/{sha[:2]}/{sha}"
        return SimpleNamespace(blob_path=path, version=7)

    async def insert_one(row: dict[str, Any], **_: Any) -> None:
        written.append(row)

    monkeypatch.setattr(documents_repo, "get_one", get_one)
    monkeypatch.setattr(documents_repo, "build_document_row", lambda doc, **kw: kw)
    monkeypatch.setattr(documents_repo, "insert_one", insert_one)
    assert main(["blobs", "import", str(tmp_path), "--apply"]) == 1  # one file is missing
    ((table, rows),) = ch.inserts
    assert table == "loom.document_blobs" and rows[0]["sha256"] == sha
    assert rows[0]["data"] == b"original"
    assert [w["blob_path"] for w in written] == [sha]
    # The imported file goes even while another is missing; the stamp covers only
    # deleted documents' rows naming a file deleted here, so the live document
    # without a file stays unstamped and counted.
    assert not (tmp_path / "o" / "ns" / sha[:2] / sha).exists()
    ((stamp, params),) = ch.executed
    assert "UPDATE blob_erased_at" in stamp and "position(blob_path, '/') > 0" in stamp
    assert "superseded_at IS NOT NULL" in stamp and params["shas"] == [sha]
    ch.documents = ch.documents[1:]  # "d" now names its sha, not a file
    assert main(["blobs", "import", str(tmp_path), "--apply", "--accept-missing"]) == 0
    (deleted, d_params), (live, l_params) = ch.executed[-2:]
    assert "superseded_at IS NOT NULL" in deleted and "shas" not in d_params
    # Given up: the live row of the missing original only, by its sha.
    assert "superseded_at IS NULL" in live and l_params["keys"] == [("o", "ns", "f" * 64)]


def test_blobs_import_keeps_a_file_that_fails_its_hash_unless_given_up(
    ch: _CH, tmp_path: Path
) -> None:
    """A corrupt file is the only evidence left of its original: it stays, and
    its live document stays unstamped, until --accept-missing gives it up."""
    sha = hashlib.sha256(b"right").hexdigest()
    f = tmp_path / "o" / "ns" / sha[:2] / sha
    f.parent.mkdir(parents=True)
    f.write_bytes(b"wrong")
    erased = _legacy(tmp_path / "x", b"erased")  # a deleted document's file
    ch.documents = [
        {"org": "o", "namespace": "ns", "source_uri": "d", "blob_path": f"o/ns/{sha[:2]}/{sha}"}
    ]
    erased_file = tmp_path / "x" / "o" / "ns" / erased[:2] / erased
    assert main(["blobs", "import", str(tmp_path / "x"), "--apply"]) == 1
    assert not erased_file.exists()  # nothing live needs it: deleted regardless
    assert main(["blobs", "import", str(tmp_path), "--apply"]) == 1
    assert f.exists() and sha not in ch.executed[-1][1]["shas"]
    # Given up, its row is stamped, but the file a live document names stays.
    assert main(["blobs", "import", str(tmp_path), "--apply", "--accept-missing"]) == 0
    assert f.exists() and ch.executed[-1][1]["keys"] == [("o", "ns", sha)]
    assert ch.inserts == []


def test_an_import_with_accept_missing_on_one_host_never_costs_another_its_files(
    ch: _CH, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Host A gives up a live document whose file is on host B. B's run still
    reads that (now stamped) row, imports its file and clears the stamp; it
    does not delete the file unimported."""
    from server.knowledge.store import documents as documents_repo

    host_a, host_b = tmp_path / "a", tmp_path / "b"
    host_a.mkdir()
    sha = _legacy(host_b, b"only on b")
    legacy = f"o/ns/{sha[:2]}/{sha}"
    ch.documents = [{"org": "o", "namespace": "ns", "source_uri": "d", "blob_path": legacy}]
    assert main(["blobs", "import", str(host_a), "--apply", "--accept-missing"]) == 0
    written: list[dict[str, Any]] = []

    async def get_one(org: str, ns: str, uri: str, **_: Any) -> Any:
        return SimpleNamespace(blob_path=written[-1]["blob_path"] if written else legacy, version=7)

    async def insert_one(row: dict[str, Any], **_: Any) -> None:
        written.append(row)

    monkeypatch.setattr(documents_repo, "get_one", get_one)
    monkeypatch.setattr(documents_repo, "build_document_row", lambda doc, **kw: kw)
    monkeypatch.setattr(documents_repo, "insert_one", insert_one)
    queries: list[str] = []
    real = ch.query_all

    async def query_all(sql: str, params: Any = None) -> list[dict[str, Any]]:
        queries.append(sql)
        return await real(sql, params)

    monkeypatch.setattr(ch, "query_all", query_all)
    assert main(["blobs", "import", str(host_b), "--apply"]) == 0
    assert "blob_erased_at" not in queries[0]  # stamped rows are read too
    assert [r["sha256"] for _t, rows in ch.inserts for r in rows] == [sha]
    assert written == [{"blob_path": sha, "blob_erased_at": None, "version": written[0]["version"]}]


def test_blobs_import_leaves_a_document_re_uploaded_meanwhile(
    ch: _CH, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.knowledge.store import documents as documents_repo

    sha = _legacy(tmp_path, b"original")
    ch.documents = [
        {"org": "o", "namespace": "ns", "source_uri": "d", "blob_path": f"o/ns/{sha[:2]}/{sha}"}
    ]
    written: list[Any] = []

    async def get_one(org: str, ns: str, uri: str, **_: Any) -> Any:
        return SimpleNamespace(blob_path="f" * 64, version=7)  # new bytes landed after the listing

    async def insert_one(row: Any, **_: Any) -> None:
        written.append(row)

    monkeypatch.setattr(documents_repo, "get_one", get_one)
    monkeypatch.setattr(documents_repo, "insert_one", insert_one)
    assert main(["blobs", "import", str(tmp_path), "--apply"]) == 0
    assert written == []


def test_blobs_import_leaves_a_document_erased_after_the_listing(
    ch: _CH, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reviewer's race: an erasure completes between the listing and the
    copy. The document is re-read under the originals claim the erasure takes,
    with a sequential read, so its bytes are not stored and its file goes."""
    from server.core.ch import sequential_reads_active
    from server.knowledge.store import documents as documents_repo

    sha = _legacy(tmp_path, b"erased")
    ch.documents = [
        {"org": "o", "namespace": "ns", "source_uri": "d", "blob_path": f"o/ns/{sha[:2]}/{sha}"}
    ]
    reads: list[tuple[bool, bool]] = []

    async def get_one(org: str, ns: str, uri: str, **_: Any) -> Any:
        reads.append((ch.holding, sequential_reads_active()))
        return None  # erased since the listing

    monkeypatch.setattr(documents_repo, "get_one", get_one)
    assert main(["blobs", "import", str(tmp_path), "--apply"]) == 0
    assert reads == [(True, True)] and ch.claims == [("o", ["ns"])]
    assert ch.inserts == []
    assert not (tmp_path / "o" / "ns" / sha[:2] / sha).exists()


def _document(blob_path: str, version: int, superseded_at: Any = None) -> Any:
    from datetime import datetime

    from server.knowledge.store.documents import DocumentRow

    return DocumentRow(
        org="o",
        namespace="ns",
        source_uri="d",
        doc_id="id",
        content="text",
        version=version,
        superseded_at=superseded_at,
        ingested_at=datetime(2026, 1, 1),
        blob_path=blob_path,
    )


def test_blobs_import_never_outranks_a_delete_that_lands_meanwhile(
    ch: _CH, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """knowledge.delete takes no originals claim: a delete written between the
    import's re-read and its write must stay the newest row, or the deleted
    document comes back live with its original."""
    import time
    from datetime import datetime

    from server.knowledge.store import blobs
    from server.knowledge.store import documents as documents_repo

    sha = _legacy(tmp_path, b"deleted")
    legacy = f"o/ns/{sha[:2]}/{sha}"
    ch.documents = [{"org": "o", "namespace": "ns", "source_uri": "d", "blob_path": legacy}]
    rows: list[Any] = [_document(legacy, version=time.time_ns())]

    async def get_one(*_: Any, **__: Any) -> Any:
        latest = max(rows, key=lambda r: r.version)
        return None if latest.superseded_at else latest

    async def put(*_: Any, **__: Any) -> str:
        # The user deletes the document while the import stores its original.
        rows.append(_document(legacy, version=time.time_ns(), superseded_at=datetime.now()))
        return sha

    async def insert_one(row: dict[str, Any], **_: Any) -> None:
        rows.append(_document(row["blob_path"], row["version"], row["superseded_at"]))

    monkeypatch.setattr(documents_repo, "get_one", get_one)
    monkeypatch.setattr(documents_repo, "insert_one", insert_one)
    monkeypatch.setattr(blobs, "put", put)
    assert main(["blobs", "import", str(tmp_path), "--apply"]) == 0
    assert max(rows, key=lambda r: r.version).superseded_at is not None


def test_blobs_import_writes_nothing_once_its_claim_lapsed(
    ch: _CH, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A holder paused past half the TTL sees its claim gone before an erasure
    can take it over: it stops before repointing the document, and before
    deleting the file that is still the document's only copy."""
    from server.core import leases
    from server.knowledge.store import blobs
    from server.knowledge.store import documents as documents_repo

    sha = _legacy(tmp_path, b"original")
    legacy = f"o/ns/{sha[:2]}/{sha}"
    ch.documents = [{"org": "o", "namespace": "ns", "source_uri": "d", "blob_path": legacy}]
    written: list[Any] = []

    async def get_one(*_: Any, **__: Any) -> Any:
        return _document(legacy, version=7)

    async def put(*_: Any, **__: Any) -> str:
        ch.claim_held = False  # paused here past the claim's renewal window
        return sha

    async def insert_one(row: Any, **_: Any) -> None:
        written.append(row)

    monkeypatch.setattr(documents_repo, "get_one", get_one)
    monkeypatch.setattr(documents_repo, "insert_one", insert_one)
    monkeypatch.setattr(blobs, "put", put)
    with pytest.raises(leases.LeaseLost):
        main(["blobs", "import", str(tmp_path), "--apply"])
    assert written == []
    assert (tmp_path / "o" / "ns" / sha[:2] / sha).exists()


def test_an_edit_that_writes_the_old_path_back_is_reported_and_keeps_its_original(
    ch: _CH, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """knowledge.update_metadata takes no claim and carries the row's blob_path
    forward at a newer version: one that read the row before the import names
    the old file again. Its bytes are stored by then, so the file may go; the
    run says so and exits 1, and the next one repoints the row from the stored
    original without the file."""
    from server.knowledge.store import blobs
    from server.knowledge.store import documents as documents_repo

    sha = _legacy(tmp_path, b"original")
    legacy = f"o/ns/{sha[:2]}/{sha}"
    ch.documents = [{"org": "o", "namespace": "ns", "source_uri": "d", "blob_path": legacy}]
    written: list[dict[str, Any]] = []

    async def get_one(*_: Any, **__: Any) -> Any:
        return SimpleNamespace(blob_path=legacy, version=7)  # the edit's row is the newest

    async def insert_one(row: dict[str, Any], **_: Any) -> None:
        written.append(row)

    async def size(*_: Any, **__: Any) -> int:
        return 8

    monkeypatch.setattr(documents_repo, "get_one", get_one)
    monkeypatch.setattr(documents_repo, "build_document_row", lambda doc, **kw: kw)
    monkeypatch.setattr(documents_repo, "insert_one", insert_one)
    monkeypatch.setattr(blobs, "size", size)
    assert main(["blobs", "import", str(tmp_path), "--apply"]) == 1
    assert "run the import again" in capsys.readouterr().err
    assert not (tmp_path / "o" / "ns" / sha[:2] / sha).exists()
    # The rerun: no file here, the original stored: repointed, nothing stored again.
    assert main(["blobs", "import", str(tmp_path), "--apply"]) == 1
    assert [w["blob_path"] for w in written] == [sha, sha]
    assert [t for t, _ in ch.inserts] == ["loom.document_blobs"]
