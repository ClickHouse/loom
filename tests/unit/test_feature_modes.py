"""With the journal off the wire is main's: a journal op does not exist, a journal
input is ignored (and named), a journal output key is absent, and the field the
journal replaces (`as_of`) is read. With it on, the reverse."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.core import feature_modes as fm
from server.memory.api.models_crud import MemoryGetInput, MemorySetOutput
from server.operations.registry import OPERATIONS_BY_NAME
from server.settings import settings


@pytest.fixture
def off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "memory_temporal_enabled", False)


def test_a_journal_op_does_not_exist_while_the_journal_is_off(off: None) -> None:
    from server.transport import http as http_mod
    from server.transport.mcp_tools import listed_ops

    amend = OPERATIONS_BY_NAME["memory.amend"]
    assert not amend.available and OPERATIONS_BY_NAME["memory.set"].available
    app = FastAPI()
    app.include_router(http_mod.build_router())
    r = TestClient(app).post("/v1/memory.amend", json={})
    assert (r.status_code, r.json()) == (404, {"detail": "Not Found"})
    assert "memory.amend" not in {op.name for op in listed_ops("all")}


def test_a_journal_input_is_reset_and_named_while_off(off: None) -> None:
    from server.transport.arg_check import ignored_block

    body = {"org": "o", "namespace": "n", "agent": "a", "memory_key": "k"}
    args = {**body, "valid_as_of": "2026-01-01T00:00:00Z", "as_of": "2026-01-02T00:00:00Z"}
    inp = MemoryGetInput.model_validate(args)
    assert fm.strip_inactive_input(inp) == ["valid_as_of"]
    assert inp.valid_as_of is None and inp.as_of is not None
    op = OPERATIONS_BY_NAME["memory.get"]
    assert ignored_block(op, args)["ignored_args"] == ["valid_as_of"]


def test_the_replaced_input_is_the_ignored_one_while_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "memory_temporal_enabled", True)
    args = {"org": "o", "namespace": "n", "agent": "a", "memory_key": "k"}
    inp = MemoryGetInput.model_validate({**args, "as_of": "2026-01-02T00:00:00Z"})
    assert fm.strip_inactive_input(inp) == ["as_of"] and inp.as_of is None


def test_a_journal_output_key_is_absent_while_off(off: None) -> None:
    body = MemorySetOutput(memory_id="m", version=1).model_dump(mode="json")
    assert "commit_seq" in body and "read_token" in body
    out = fm.drop_inactive_output(MemorySetOutput, body)
    assert not {"commit_seq", "read_token", "left_out", "past_only"} & set(out)
    assert out["memory_id"] == "m"


def test_the_operation_catalogue_lists_no_journal_op_while_off(off: None) -> None:
    from server.operations.capabilities import operation_rows

    names = {r["name"] for r in operation_rows()}
    assert "memory.set" in names and "memory.amend" not in names
