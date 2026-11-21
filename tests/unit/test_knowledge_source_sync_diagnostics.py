"""Worker summaries and direct logs disclose no mirrored file details or counts."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
from typing import Any

import pytest

from server.knowledge.app import extractors
from server.knowledge.app import source_sync as sync
from server.knowledge.domain import access_list
from server.knowledge.domain.sources import MIRRORED_ACCESS
from server.knowledge.workers.source_sync import outcome
from tests.unit.test_knowledge_source_sync import DOCS, _file, _source


@pytest.fixture
def boundary(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    seen: dict[str, Any] = {"states": [], "failures": []}

    @contextlib.asynccontextmanager
    async def lease(src: Any) -> Any:
        yield None

    async def state(src: Any, prior: Any, turn: Any) -> None:
        seen["states"].append(turn)

    def failure(error: Exception, *args: Any) -> None:
        seen["failures"].append((type(error).__name__, str(error)))

    monkeypatch.setattr(sync, "_source_turn", lease)
    monkeypatch.setattr(sync, "_write_state", state)
    monkeypatch.setattr(sync.pass_context, "note_failure", failure)
    return seen


@pytest.mark.parametrize("mirrored", [False, True])
def test_document_figures_stay_in_private_turn(
    boundary: dict[str, Any], monkeypatch: pytest.MonkeyPatch, mirrored: bool
) -> None:
    answers = []
    for count in (1, 10000):
        turn = sync.TurnResult(
            changed=count,
            removed=count,
            skipped=count,
            unconverted=count,
            documents_seen=count,
            held=count,
        )
        turn.mark(sync.TurnMark.PASS_BOUND)

        async def run(*args: Any, answer: Any = turn) -> Any:
            return answer

        monkeypatch.setattr(sync, "_turn_of", lambda src: run)
        src = _source(access=MIRRORED_ACCESS) if mirrored else _source()
        result = sync.PassResult(sources=1)
        assert asyncio.run(sync._sync_one(src, {}, None, result)) is turn
        assert boundary["states"][-1] is turn
        answers.append(outcome(result))
        if mirrored:
            assert result == sync.PassResult(sources=1, private_work=True)
        else:
            assert result.changed == count and result.cut
    assert (answers[0] == answers[1]) is mirrored


@pytest.mark.parametrize("where", ["turn", "state", "returned"])
def test_mirrored_failures_have_one_boundary_code(
    boundary: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    where: str,
) -> None:
    answers = []
    for error in (ValueError("private-file-a"), RuntimeError("private-file-b")):
        caplog.clear()
        boundary["failures"].clear()
        turn = sync.TurnResult()
        turn.fail("credential_invalid" if isinstance(error, ValueError) else "failed", str(error))

        async def run(*args: Any, answer: Any = turn, exc: Exception = error) -> Any:
            if where == "turn":
                raise exc
            return answer

        async def state(*args: Any, exc: Exception = error) -> None:
            raise exc

        monkeypatch.setattr(sync, "_turn_of", lambda src: run)
        if where == "state":
            monkeypatch.setattr(sync, "_write_state", state)
        result = sync.PassResult()
        with caplog.at_level(logging.INFO, logger=sync.log.name):
            asyncio.run(sync._sync_one(_source(access=MIRRORED_ACCESS), {}, None, result))
        assert "private-file" not in caplog.text
        assert result.errors == ["acme/code-acme-app/decks: SYNC_FAILED"]
        assert result.failed == 0
        assert boundary["failures"] == [("RuntimeError", "SYNC_FAILED")]
        answers.append((dataclasses.asdict(result), caplog.messages[:]))
    assert answers[0] == answers[1]


def test_namespace_failure_keeps_its_existing_diagnostics(
    boundary: dict[str, Any], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def run(*args: Any) -> Any:
        raise ValueError("namespace diagnostic")

    monkeypatch.setattr(sync, "_turn_of", lambda src: run)
    result = sync.PassResult()
    asyncio.run(sync._sync_one(_source(), {}, None, result))
    assert result.synced == result.failed == 1
    assert result.errors == ["acme/code-acme-app/decks: failed ValueError"]
    assert "namespace diagnostic" in caplog.text
    assert boundary["failures"] == [("RuntimeError", "ValueError")]


@pytest.mark.parametrize("mirrored", [False, True])
@pytest.mark.parametrize("stage", ["extract", "fetch", "write"])
def test_per_file_failures_emit_no_mirrored_log(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    mirrored: bool,
    stage: str,
) -> None:
    async def fetch(*args: Any) -> Any:
        if stage == "extract":
            raise extractors.ExtractionFailed("corrupt", "private extractor output")
        if stage == "fetch":
            raise ValueError("private fetch output")
        return sync._Fetched("text", "markdown")

    async def write(*args: Any, **kw: Any) -> Any:
        raise ValueError("private write output")

    async def current(*args: Any) -> None:
        pass

    monkeypatch.setattr(sync, "_fetch", fetch)
    monkeypatch.setattr(sync, "write_document", write)
    monkeypatch.setattr(sync, "require_current", current)
    src = _source(access=MIRRORED_ACCESS) if mirrored else _source()
    turn = sync.TurnResult(kind_state={sync._PICTURES_CHECKED: {}})
    with caplog.at_level(logging.INFO, logger=sync.log.name):
        for i in range(3):
            file = _file(f"private-file-{i}", DOCS, "revision")
            asyncio.run(
                sync._fetch_and_write(
                    src,
                    "token",
                    file,
                    "uri",
                    turn,
                    stored_content_sha256="",
                    permissions=access_list.UNREAD,
                )
            )
    assert len(turn.waits) == 3
    assert len(caplog.records) == (0 if mirrored else 3)
