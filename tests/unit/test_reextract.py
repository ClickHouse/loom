"""A namespace's memories are re-extracted by replaying its raw
evidence through a running server's write path into another namespace."""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from server.memory.app.reextract import (
    render_report,
    replay,
    request_body,
    sessions_from_rows,
)


def _row(sid: str, idx: int, content: str, at: datetime, role: str = "user") -> dict[str, Any]:
    return {
        "session_id": sid,
        "agent": "kcurve",
        "user_id": "-",
        "role": role,
        "content": content,
        "observed_at": at,
        "idx": idx,
    }


def test_rows_become_sessions_in_turn_order_with_the_session_day() -> None:
    day = datetime(2023, 5, 7)  # The session day, at midnight
    rows = [
        _row("s1", 1, "Melanie: hi", day),
        _row("s1", 0, "Caroline: hello", day),
        _row("s2", 0, "Caroline: later", datetime(2023, 5, 9, 8, 10)),
        _row("s1", 2, "   ", day),  # empty content is not a turn
    ]
    sessions = sessions_from_rows(rows)
    assert [s.session_id for s in sessions] == ["s1", "s2"]
    s1, s2 = sessions
    # The query orders by index; the builder keeps that order.
    assert [m["content"] for m in s1.messages] == ["Melanie: hi", "Caroline: hello"]
    assert s1.observation_date == "2023-05-07" and s1.dated_by_ingest is False
    assert all("timestamp" not in m for m in s1.messages)
    # A turn stamped off midnight carries its own timestamp.
    assert s2.messages[0]["timestamp"] == "2023-05-09T08:10:00"
    assert s2.observation_date == "2023-05-09"


def test_ingest_dated_rows_are_detected_and_replayed_without_a_date() -> None:
    # Legacy ingest-dated rows: every turn stamped inside one minute.
    t0 = datetime(2026, 9, 13, 7, 52, 42)
    rows = [_row("old", i, f"turn {i}", t0.replace(second=42 + i % 3)) for i in range(5)]
    (s,) = sessions_from_rows(rows)
    assert s.dated_by_ingest is True
    bodies = request_body(s, org="o", into="dst")
    assert len(bodies) == 1 and "observation_date" not in bodies[0]
    assert all("timestamp" not in m for m in bodies[0]["messages"])
    # `dates="none"` withholds the date from a well-dated session too.
    day = datetime(2023, 5, 7)
    (d,) = sessions_from_rows([_row("new", 0, "x", day)])
    assert "observation_date" in request_body(d, org="o", into="dst")[0]
    assert "observation_date" not in request_body(d, org="o", into="dst", dates="none")[0]


def test_request_body_targets_the_other_namespace_and_cuts_at_the_ceiling() -> None:
    day = datetime(2023, 5, 7)
    rows = [_row("big", i, f"turn {i}", day) for i in range(401)]
    rows[0]["user_id"] = "u-1"
    (s,) = sessions_from_rows(rows)
    bodies = request_body(s, org="acme", into="dst", max_messages=400)
    assert (
        len(bodies) == 2 and len(bodies[0]["messages"]) == 400 and len(bodies[1]["messages"]) == 1
    )
    b = bodies[0]
    assert b["namespace"] == "dst" and b["org"] == "acme" and b["session_id"] == "big"
    assert (
        b["agent"] == "kcurve" and b["user_id"] == "u-1" and b["observation_date"] == "2023-05-07"
    )


class _Resp:
    def __init__(self, status: int, text: str = "") -> None:
        self.status_code = status
        self.text = text


class _Client:
    def __init__(self, fail_session: str = "") -> None:
        self.posts: list[dict[str, Any]] = []
        self.fail_session = fail_session

    async def post(self, url: str, json: dict[str, Any], headers: dict[str, str], **kw: Any):
        assert url.endswith("/v1/memory.set_from_messages")
        assert headers["Authorization"] == "Bearer tok"
        self.posts.append(json)
        if json["session_id"] == self.fail_session:
            return _Resp(500, "boom")
        return _Resp(200)


def test_replay_posts_every_session_and_a_failure_fails_only_that_session() -> None:
    day = datetime(2023, 5, 7)
    sessions = sessions_from_rows(
        [_row("a", 0, "x", day), _row("b", 0, "y", day), _row("c", 0, "z", day)]
    )
    cl = _Client(fail_session="b")
    rep = asyncio.run(
        replay(
            sessions,
            base_url="http://x",
            token="tok",
            org="o",
            into="dst",
            client=cl,
            log=lambda s: None,
        )
    )
    assert sorted(p["session_id"] for p in cl.posts) == ["a", "b", "c"]
    assert rep.sessions == 3 and rep.calls == 3 and rep.failed == 1 and rep.messages == 3
    assert rep.errors == ["b: HTTP 500 boom"]
    out = render_report(rep, source="src", into="dst", dry_run=False)
    assert out.startswith(
        "replayed 3 session(s), 3 message(s), 3 call(s) from src into dst; failed 1"
    )


def test_dry_run_posts_nothing_and_names_undated_sessions() -> None:
    t0 = datetime(2026, 9, 13, 7, 52, 42)
    sessions = sessions_from_rows([_row("old", i, f"t{i}", t0) for i in range(3)])
    cl = _Client()
    lines: list[str] = []
    rep = asyncio.run(
        replay(
            sessions,
            base_url="http://x",
            token="tok",
            org="o",
            into="dst",
            dry_run=True,
            client=cl,
            log=lines.append,
        )
    )
    assert cl.posts == [] and rep.calls == 1 and rep.undated == 1
    assert lines and "dry-run old: 3 messages, date -" in lines[0]
    assert "undated sessions carry ingest-time evidence" in render_report(
        rep, source="s", into="d", dry_run=True
    )


def test_cli_refuses_the_source_as_target(capsys) -> None:
    import argparse

    from server.cli.maintenance import _cmd_reextract

    args = argparse.Namespace(org="o", namespace="same", into="same")
    assert _cmd_reextract(args) == 2
    assert "must differ" in capsys.readouterr().err


def test_reextract_token_falls_back_to_the_saved_token(tmp_path, monkeypatch) -> None:
    """--token, else LOOM_API_KEY, else ~/.loom/token: the order every client command uses."""
    import argparse

    import server.cli.maintenance as m
    import server.memory.app.reextract as rx

    seen: list[str] = []

    async def _load(ch, *, org, namespace):
        return ["s1"]

    async def _replay(sessions, *, token, **kw):
        seen.append(token)
        return type("R", (), {"failed": 0, "sessions": 1})()

    class _CH:
        connected = False

        async def connect(self): ...
        async def disconnect(self): ...

    monkeypatch.setattr("server.core.ch.get_client", lambda: _CH())
    monkeypatch.setattr(rx, "load_sessions", _load)
    monkeypatch.setattr(rx, "replay", _replay)
    monkeypatch.setattr(rx, "render_report", lambda *a, **k: "")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("LOOM_API_KEY", raising=False)
    (tmp_path / ".loom").mkdir()
    (tmp_path / ".loom" / "token").write_text("saved\n")
    args = argparse.Namespace(
        org="o",
        namespace="a",
        into="b",
        base_url="http://x",
        token=None,
        dates="auto",
        concurrency=1,
        limit_sessions=0,
        dry_run=True,
    )
    assert m._cmd_reextract(args) == 0
    monkeypatch.setenv("LOOM_API_KEY", "env")
    assert m._cmd_reextract(args) == 0
    args.token = "flag"
    assert m._cmd_reextract(args) == 0
    assert seen == ["saved", "env", "flag"]
