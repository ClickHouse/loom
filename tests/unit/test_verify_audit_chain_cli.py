"""dev/verify_audit_chain.py: exit 0 on a clean window, 1 on a hash mismatch,
2 when it could not verify; a mismatch report never carries row content."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from dev import verify_audit_chain  # noqa: E402
from dev._ch_target import CHTarget  # noqa: E402
from server.observability.audit import _row_hash, _ts_canonical_form  # noqa: E402

TARGET = CHTarget(host="ch.test", port=8123, user="loom", database="loom", secure=False)
COLS = [
    "partition",
    "ts",
    "event_type",
    "org",
    "namespace",
    "actor",
    "subject",
    "request_id",
    "row_hash",
]


def _row(subject: str, *, tamper: bool = False) -> tuple[Any, ...]:
    ts = datetime(2026, 1, 2, 3, 4, 5, 123000, tzinfo=UTC)
    fields = {
        "ts": _ts_canonical_form(ts),
        "event_type": "memory.delete",
        "org": "o",
        "namespace": "ns-private",
        "actor": "human:someone",
        "subject": subject,
        "request_id": f"r-{subject}",
    }
    h = _row_hash(fields)
    stored_subject = subject + "-edited" if tamper else subject
    return (
        202601,
        ts,
        "memory.delete",
        "o",
        "ns-private",
        "human:someone",
        stored_subject,
        f"r-{subject}",
        h.encode(),
    )


class _Result:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self.column_names = COLS
        self.result_rows = rows


class Client:
    def __init__(self, rows: list[tuple[Any, ...]] | Exception) -> None:
        self.rows = rows
        self.closed = False

    def query(self, sql: str, parameters: dict[str, Any] | None = None) -> _Result:
        assert "FROM audit_log" in sql and "loom.audit_log" not in sql
        if isinstance(self.rows, Exception):
            raise self.rows
        return _Result(self.rows)

    def close(self) -> None:
        self.closed = True


def _run(monkeypatch: pytest.MonkeyPatch, client: Client, *argv: str) -> int:
    monkeypatch.setattr(verify_audit_chain, "resolve_target", lambda _args: TARGET)
    monkeypatch.setattr(CHTarget, "client", lambda self, **_: client)
    return verify_audit_chain.main(list(argv))


def test_clean_window_exits_0(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client = Client([_row("a"), _row("b")])
    assert _run(monkeypatch, client, "--json") == 0
    assert json.loads(capsys.readouterr().out) == {"rows_checked": 2, "breaks": [], "ok": True}
    assert client.closed


def test_empty_window_is_a_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _run(monkeypatch, Client([]), "--org", "none") == 0


def test_mismatch_exits_1_and_names_the_row_without_its_content(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        _run(monkeypatch, Client([_row("a"), _row("secret-subject", tamper=True)]), "--json") == 1
    )
    out = capsys.readouterr().out
    summary = json.loads(out)
    assert summary["ok"] is False
    assert [b["request_id"] for b in summary["breaks"]] == ["r-secret-subject"]
    for leaked in ("secret-subject-edited", "human:someone", "ns-private"):
        assert leaked not in out
    assert _run(monkeypatch, Client([_row("x", tamper=True)])) == 1
    assert "human:someone" not in capsys.readouterr().err


def test_a_failed_read_is_exit_2_not_a_verdict(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client = Client(RuntimeError("Code: 60. Unknown table audit_log"))
    assert _run(monkeypatch, client, "--json") == 2
    captured = capsys.readouterr()
    assert captured.out == "" and "could not read audit_log" in captured.err
    assert client.closed


def test_bad_since_is_exit_2(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _run(monkeypatch, Client([]), "--ts-since", "yesterday") == 2


def test_rows_sharing_a_hash_are_each_checked_and_each_pass(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Two physical rows with identical content (a retried emit) carry the
    same row_hash. The check is per row, not a chain: both are counted and
    both pass; neither is reported as a break nor deduplicated away."""
    dup = _row("same")
    assert _run(monkeypatch, Client([dup, dup, _row("other")]), "--json") == 0
    assert json.loads(capsys.readouterr().out) == {"rows_checked": 3, "breaks": [], "ok": True}
