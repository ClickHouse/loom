"""`loom knowledge source add|update`: the op body each builds from its flags, and the
refusals it answers on its own, before any request."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from server.cli import knowledge_source as cli

LINK = "https://drive.google.com/drive/folders/1AbCdEfGhIjKlMnOp"


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    seen: list[tuple[str, dict[str, Any]]] = []

    def _call(args: Any, op: str, body: dict[str, Any]) -> int:
        seen.append((op, body))
        return 0

    monkeypatch.setattr(cli, "_call", _call)
    return seen


def _args(**kw: Any) -> SimpleNamespace:
    base = {"name": "decks", "kind": "gdrive", "credential_file": "", "dry_run": False}
    base |= {"item_json": [], "link": [], "include": [], "remove_item_json": [], "remove_link": []}
    return SimpleNamespace(**(base | kw))


def test_add_sends_its_links_and_the_credential_read_from_its_file(
    tmp_path: Path, calls: list[Any]
) -> None:
    key = tmp_path / "key.json"
    key.write_text(' {"k": 1}\n', encoding="utf-8")
    assert cli._cmd_add(_args(credential_file=str(key), link=[LINK], include=["pdf"])) == 0
    assert calls == [
        (
            "knowledge.source.add",
            {
                "name": "decks",
                "kind": "gdrive",
                "credential": '{"k": 1}',
                "dry_run": False,
                "links": [LINK],
                "include": ["pdf"],
            },
        )
    ]


def test_add_sends_links_and_items_together(tmp_path: Path, calls: list[Any]) -> None:
    key = tmp_path / "key.json"
    key.write_text("k", encoding="utf-8")
    args = _args(credential_file=str(key), link=[LINK], item_json=['{"type": "file", "id": "x"}'])
    assert cli._cmd_add(args) == 0
    [(_, body)] = calls
    assert body["items"] == [{"type": "file", "id": "x"}] and body["links"] == [LINK]


def test_add_names_a_credential_file_it_cannot_read(
    tmp_path: Path, calls: list[Any], capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "missing.json"
    assert cli._cmd_add(_args(credential_file=str(missing), link=[LINK])) == 2
    assert "--credential-file: cannot read" in capsys.readouterr().err and calls == []


def test_update_sends_what_it_adds_and_removes(calls: list[Any]) -> None:
    args = _args(
        item_json=['{"type": "file", "id": "f1"}'],
        remove_link=[LINK],
        include=["docs"],
    )
    assert cli._cmd_update(args) == 0
    assert calls == [
        (
            "knowledge.source.update",
            {
                "name": "decks",
                "add_items": [{"type": "file", "id": "f1"}],
                "add_links": [],
                "remove_items": [],
                "remove_links": [LINK],
                "include": ["docs"],
            },
        )
    ]


@pytest.mark.parametrize("value", ["[1, 2]", "not json", '"a string"'])
def test_an_item_that_is_not_a_json_object_is_refused(
    value: str, tmp_path: Path, calls: list[Any], capsys: pytest.CaptureFixture[str]
) -> None:
    key = tmp_path / "key.json"
    key.write_text("k", encoding="utf-8")
    assert cli._cmd_add(_args(credential_file=str(key), item_json=[value])) == 2
    assert "not a JSON object" in capsys.readouterr().err and calls == []


def test_add_names_a_credential_file_that_is_not_text(
    tmp_path: Path, calls: list[Any], capsys: pytest.CaptureFixture[str]
) -> None:
    key = tmp_path / "key.p12"
    key.write_bytes(b"\xff\xfe\x00")
    assert cli._cmd_add(_args(credential_file=str(key), link=[LINK])) == 2
    assert "not UTF-8 text" in capsys.readouterr().err and calls == []


def test_a_bad_remove_item_names_its_own_flag(
    calls: list[Any], capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli._cmd_update(_args(remove_item_json=["[1]"])) == 2 and calls == []
    assert "--remove-item-json: not a JSON object" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("status", "answer", "line"),
    [
        (
            422,
            {"detail": {"code": "BAD_LINK", "message": "unreadable links: x"}},
            "knowledge.source.list → HTTP 422 BAD_LINK: unreadable links: x",
        ),
        (
            0,
            {"detail": "URLError: refused"},
            "knowledge.source.list → no Loom answer: URLError: refused",
        ),
        (502, {"detail": "<html>login</html>"}, "knowledge.source.list → HTTP 502 Bad Gateway"),
        (
            422,
            {"detail": [{"loc": ["body", "links"], "msg": "List should have at most 300 items"}]},
            "knowledge.source.list → HTTP 422 validation_error: body.links: "
            "List should have at most 300 items",
        ),
    ],
)
def test_a_failed_call_prints_one_line_and_exits_1(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    status: int,
    answer: dict[str, Any],
    line: str,
) -> None:
    monkeypatch.setattr(cli, "resolve_token", lambda token: ("tok", "flag"))
    monkeypatch.setattr(cli, "refused_for_agent", lambda source: False)
    monkeypatch.setattr(cli, "post_op", lambda *a, **kw: (status, answer))
    args = SimpleNamespace(token=None, org="acme", namespace="docs", server_url="http://x")
    assert cli._cmd_list(args) == 1
    assert capsys.readouterr().err.strip() == line


def test_add_with_nothing_to_read_is_refused_before_reading_the_credential(
    calls: list[Any], capsys: pytest.CaptureFixture[str]
) -> None:
    # No key file at all: the refusal comes first.
    assert cli._cmd_add(_args(credential_file="/nonexistent/key.json")) == 2 and calls == []
    assert "give --link or --item-json" in capsys.readouterr().err


def test_a_bad_item_is_refused_before_the_credential_is_read(
    calls: list[Any], capsys: pytest.CaptureFixture[str]
) -> None:
    args = _args(credential_file="/nonexistent/key.json", item_json=["[1]"])
    assert cli._cmd_add(args) == 2 and calls == []
    assert "not a JSON object" in capsys.readouterr().err
