"""`loom memory-search` sends the org the token is for (authz compares the
body's org with it), not a hard-coded `dev`."""

from __future__ import annotations

import argparse
import base64
import json
from typing import Any

import pytest

from server.cli import shell


def _jwt(claims: dict[str, Any]) -> str:
    seg = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"e30.{seg}.sig"


@pytest.mark.parametrize(
    ("org_flag", "token", "want"),
    [
        ("", _jwt({"org": "acme"}), "acme"),
        ("other", _jwt({"org": "acme"}), "other"),
        ("", "", "dev"),
    ],
)
def test_the_body_org_follows_flag_then_token_then_dev(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any, org_flag: str, token: str, want: str
) -> None:
    import httpx

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("LOOM_API_KEY", raising=False)
    sent: list[dict[str, Any]] = []

    async def _post(self: Any, url: str, json: dict[str, Any], headers: Any) -> httpx.Response:
        sent.append(json)
        return httpx.Response(200, json={"results": []})

    monkeypatch.setattr(httpx.AsyncClient, "post", _post)
    args = argparse.Namespace(
        server_url="http://x",
        token=token or None,
        org=org_flag,
        namespace="n",
        agent="cli",
        user_id="-",
        query="q",
        top_k=3,
        search_mode="bm25_hybrid",
        json_agent=False,
    )
    assert shell._cmd_memory_search(args) == 0
    assert sent[0]["org"] == want


@pytest.mark.parametrize(("org_flag", "want"), [("", "acme"), ("other", "other")])
def test_ingest_transcript_sends_a_valid_conversation_append(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any, org_flag: str, want: str
) -> None:
    """`loom ingest-transcript`: every turn's body carries the org
    ConversationAppendInput requires (else each append 422s) — the flag, then
    the token's org, as memory-search resolves it."""
    import urllib.request

    from server.knowledge.api.models_conversations import ConversationAppendInput

    src = tmp_path / "chat.json"
    src.write_text(json.dumps([{"role": "user", "content": "hi"}]))
    sent: list[dict[str, Any]] = []

    class _Resp:
        def __enter__(self) -> _Resp:
            return self

        def __exit__(self, *a: Any) -> None:
            return None

        def read(self) -> bytes:
            return b"{}"

    def _urlopen(req: Any, timeout: float = 0) -> _Resp:
        sent.append(json.loads(req.data))
        return _Resp()

    monkeypatch.setattr(urllib.request, "urlopen", _urlopen)
    args = argparse.Namespace(
        server_url="http://x",
        token=_jwt({"org": "acme"}),
        org=org_flag,
        namespace="n",
        agent="ingest-cli",
        thread_id="t1",
        file=str(src),
        dry_run=False,
    )
    assert shell._cmd_ingest_transcript(args) == 0
    assert [b["org"] for b in sent] == [want]
    ConversationAppendInput.model_validate(sent[0])


def test_ingest_transcript_read_timeout_is_one_line_and_exit_1(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    import urllib.request

    src = tmp_path / "chat.json"
    src.write_text(json.dumps([{"role": "user", "content": "hi"}]))

    def _urlopen(req: Any, timeout: float = 0) -> Any:
        raise TimeoutError("The read operation timed out")

    monkeypatch.setattr(urllib.request, "urlopen", _urlopen)
    args = argparse.Namespace(
        server_url="http://x",
        token="t",
        file=str(src),
        namespace="n",
        org="o",
        agent="a",
        thread_id="th",
        dry_run=False,
    )
    assert shell._cmd_ingest_transcript(args) == 1
    err = capsys.readouterr().err.strip().splitlines()
    assert len(err) == 1 and "TimeoutError" in err[0] and "0 turn(s)" in err[0]


def _search_args() -> argparse.Namespace:
    return argparse.Namespace(
        server_url="http://x",
        token="t",
        org="o",
        namespace="n",
        agent="cli",
        user_id="-",
        query="secret query text",
        top_k=3,
        search_mode="bm25_hybrid",
        json_agent=False,
    )


@pytest.mark.parametrize(
    ("answer", "want"),
    [
        # a refused connection: one line naming the server, no traceback
        (None, "no answer from http://x (ConnectError)"),
        # a proxy's login page on a 200
        ((200, b"<html>sign in</html>"), "did not answer a JSON object"),
        # an error envelope: its code and message, never the raw body (which echoes the query)
        (
            (422, b'{"detail": {"code": "BAD", "message": "no", "echo": "secret query text"}}'),
            "returned HTTP 422 BAD: no",
        ),
    ],
)
def test_memory_search_failures_are_one_line_without_the_body(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    answer: tuple[int, bytes] | None,
    want: str,
) -> None:
    import httpx

    async def _post(self: Any, url: str, json: dict[str, Any], headers: Any) -> httpx.Response:
        if answer is None:
            raise httpx.ConnectError("refused")
        return httpx.Response(answer[0], content=answer[1])

    monkeypatch.setattr(httpx.AsyncClient, "post", _post)
    assert shell._cmd_memory_search(_search_args()) == 1
    err = capsys.readouterr().err.strip().splitlines()
    assert len(err) == 1 and want in err[0]
    assert "secret query text" not in err[0]
