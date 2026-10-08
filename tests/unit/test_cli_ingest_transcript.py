"""`loom ingest-transcript`: the default thread follows the file's path, each
turn's msg_id follows its content so a re-ingest adds only new turns, a dry
run prints sizes and never the text, and an HTTP error prints Loom's code and
message, never the raw body."""

from __future__ import annotations

import argparse
import io
import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from server.cli import shell


class _Resp:
    def __enter__(self) -> _Resp:
        return self

    def __exit__(self, *a: Any) -> None:
        return None

    def read(self) -> bytes:
        return b"{}"


class _Server:
    """conversation.append's dedup: a (thread_id, msg_id) that landed is skipped."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], dict[str, Any]] = {}

    def urlopen(self, req: Any, timeout: float = 0) -> _Resp:
        body = json.loads(req.data)
        self.rows.setdefault((body["thread_id"], body["msg_id"]), body)
        return _Resp()


def _args(src: Path, **kw: Any) -> argparse.Namespace:
    base = {
        "server_url": "http://x",
        "token": "t",
        "org": "o",
        "namespace": "n",
        "agent": "a",
        "thread_id": None,
        "file": str(src),
        "dry_run": False,
    }
    return argparse.Namespace(**{**base, **kw})


def _write(src: Path, turns: list[tuple[str, str]]) -> None:
    src.write_text(json.dumps([{"role": r, "content": c} for r, c in turns]))


def test_reingesting_an_edited_file_adds_only_the_new_turns(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server = _Server()
    monkeypatch.setattr(urllib.request, "urlopen", server.urlopen)
    src = tmp_path / "chat.json"
    _write(src, [("user", "hi"), ("assistant", "ok"), ("user", "bye"), ("assistant", "ok")])
    assert shell._cmd_ingest_transcript(_args(src)) == 0
    assert len(server.rows) == 4  # the two identical "ok" turns stay distinct

    assert shell._cmd_ingest_transcript(_args(src)) == 0
    assert len(server.rows) == 4

    # Edited: a turn inserted mid-file, the mtime moved. Same thread, one new row.
    _write(
        src,
        [
            ("user", "hi"),
            ("user", "wait"),
            ("assistant", "ok"),
            ("user", "bye"),
            ("assistant", "ok"),
        ],
    )
    os.utime(src, ns=(1, 1))
    assert shell._cmd_ingest_transcript(_args(src)) == 0
    assert len(server.rows) == 5
    assert len({thread for thread, _ in server.rows}) == 1
    assert {b["content"] for b in server.rows.values()} == {"hi", "wait", "ok", "bye"}


def test_the_default_thread_is_the_resolved_path_and_the_flag_wins(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server = _Server()
    monkeypatch.setattr(urllib.request, "urlopen", server.urlopen)
    monkeypatch.chdir(tmp_path)
    src = tmp_path / "chat.json"
    _write(src, [("user", "hi")])
    assert shell._cmd_ingest_transcript(_args(Path("chat.json"))) == 0
    assert shell._cmd_ingest_transcript(_args(src, thread_id="mine")) == 0
    assert {thread for thread, _ in server.rows} == {
        shell._default_thread_id(src.resolve()),
        "mine",
    }


def test_dry_run_prints_counts_and_sizes_never_the_text(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def _no_call(*a: Any, **k: Any) -> Any:
        raise AssertionError("dry run wrote")

    monkeypatch.setattr(urllib.request, "urlopen", _no_call)
    src = tmp_path / "chat.md"
    src.write_text("# user\nsecret-plan-alpha\n# assistant\nnoted é\n")
    assert shell._cmd_ingest_transcript(_args(src, dry_run=True)) == 0
    out = capsys.readouterr().out
    assert "secret-plan-alpha" not in out and "noted" not in out
    assert "would ingest 2 turns" in out
    assert "[0] 17 bytes" in out and "[1] 8 bytes" in out


@pytest.mark.parametrize(
    ("body", "want"),
    [
        (
            {"detail": {"code": "forbidden", "message": "no writer grant on n"}},
            "HTTP 403 forbidden: no writer grant on n",
        ),
        ({"detail": [{"msg": "Field required", "input": "hi"}]}, "HTTP 403 validation_error"),
        ({"detail": "x" * 500}, "HTTP 403 Forbidden: " + "x" * 200),
    ],
)
def test_an_http_error_prints_the_loom_code_and_message_not_the_body(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    body: dict[str, Any],
    want: str,
) -> None:
    raw = json.dumps({**body, "echo": "secret-plan-alpha"}).encode()

    def _urlopen(req: Any, timeout: float = 0) -> Any:
        raise urllib.error.HTTPError("http://x", 403, "Forbidden", {}, io.BytesIO(raw))  # type: ignore[arg-type]

    monkeypatch.setattr(urllib.request, "urlopen", _urlopen)
    src = tmp_path / "chat.json"
    _write(src, [("user", "hi")])
    assert shell._cmd_ingest_transcript(_args(src)) == 6
    err = capsys.readouterr().err.strip()
    assert err.startswith(f"ERROR: turn 0 failed: {want}")
    assert "secret-plan-alpha" not in err and len(err.splitlines()) == 1
    assert len(err) <= len("ERROR: turn 0 failed: HTTP 403 validation_error: ") + 200


def test_an_unparseable_error_body_prints_the_status_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def _urlopen(req: Any, timeout: float = 0) -> Any:
        raise urllib.error.HTTPError(
            "http://x",
            502,
            "Bad Gateway",
            {},  # type: ignore[arg-type]
            io.BytesIO(b"<html>upstream secret-plan-alpha</html>"),
        )

    monkeypatch.setattr(urllib.request, "urlopen", _urlopen)
    src = tmp_path / "chat.json"
    _write(src, [("user", "hi")])
    assert shell._cmd_ingest_transcript(_args(src)) == 6
    assert capsys.readouterr().err.strip() == "ERROR: turn 0 failed: HTTP 502 Bad Gateway"
