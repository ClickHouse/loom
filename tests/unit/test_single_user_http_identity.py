"""A single-user server's HTTP call with no token, to one of the everyday memory and
search ops, runs under the install's identity where it names none: the org and
namespace MCP and the dashboard use, and the op's own default agent. Every other
op, any token, and every other identity mode names its own identity as before."""

from __future__ import annotations

import asyncio
import dataclasses
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.auth.jwt import mint_token
from server.operations.registry import ALL_OPERATIONS, OPERATIONS_BY_NAME
from server.settings import settings
from server.transport.gates import SINGLE_USER_FILLED_OPS
from tests.unit.ch_fake import RecordingCH

Seen = list[tuple[str, str, str, str | None]]
_SEEN: Seen = []
# The bodies the request ledger was handed.
_LEDGER: list[dict[str, Any]] = []


async def _record(inp: Any) -> Any:
    _SEEN.append((type(inp).__name__, inp.org, inp.namespace, getattr(inp, "agent", None)))
    raise RuntimeError("recorded; the handler's answer is not under test")


@pytest.fixture()
def seen(monkeypatch: pytest.MonkeyPatch) -> Seen:
    from server.transport import http as http_mod

    ops = [dataclasses.replace(op, handler=_record) for op in ALL_OPERATIONS]
    monkeypatch.setattr(http_mod, "ALL_OPERATIONS", ops)
    monkeypatch.setattr(settings, "identity_provider", "single-user")
    monkeypatch.setattr(settings, "auth_required", False)
    monkeypatch.setattr(settings, "self_signup_org", "dev")
    monkeypatch.setattr(settings, "default_namespace", "local")
    _LEDGER.clear()
    monkeypatch.setattr(
        http_mod, "_emit_event", lambda op, rid, body, *a, **k: _LEDGER.append(body)
    )
    RecordingCH(strict=False).install(monkeypatch)
    _SEEN.clear()
    return _SEEN


def _post(op: str, body: dict[str, Any], token: str = "") -> Any:
    from server.transport.http import build_router

    app = FastAPI()
    app.include_router(build_router())
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return TestClient(app, raise_server_exceptions=False).post(
        f"/v1/{op}", json=body, headers=headers
    )


def _missing(response: Any) -> set[str]:
    assert response.status_code == 422, response.text
    return {e["loc"][-1] for e in response.json()["detail"]["errors"]}


def _default_agent(op: str) -> Any:
    return OPERATIONS_BY_NAME[op].input_model.model_fields["agent"].default


def test_the_quickstart_calls_need_only_their_own_fields(seen: Seen) -> None:
    _post("memory.set", {"memory_key": "deploy_window", "value": "Tue and Thu only."})
    _post("memory.get", {"memory_key": "deploy_window"})
    _post("memory.search", {"query": "when can we deploy?"})
    assert seen == [
        ("MemorySetInput", "dev", "local", "http"),
        ("MemoryGetInput", "dev", "local", "http"),
        ("MemorySearchInput", "dev", "local", _default_agent("memory.search")),
    ]


@pytest.mark.parametrize(
    ("op", "body"), [("memory.list", {}), ("memory.history", {"memory_key": "deploy_window"})]
)
def test_a_list_or_a_key_history_reads_every_agent(
    seen: Seen, op: str, body: dict[str, Any]
) -> None:
    """Their agent is required, and filled with `*`, which their stores read as every
    agent; the input carries `*`."""
    _post(op, body)
    assert len(seen) == 1 and seen[0][1:] == ("dev", "local", "*")


@pytest.mark.parametrize("blank", ["", "-", "  "])
def test_a_blank_or_sentinel_identity_is_filled_too(seen: Seen, blank: str) -> None:
    """What the SDKs send for an identity their caller left empty."""
    _post("memory.get", {"org": blank, "namespace": blank, "agent": "a", "memory_key": "k"})
    assert seen == [("MemoryGetInput", "dev", "local", "a")]


def test_an_identity_the_call_names_is_kept(seen: Seen) -> None:
    _post(
        "memory.set",
        {"org": "dev", "namespace": "scratch", "agent": "bot", "memory_key": "k", "value": "v"},
    )
    assert seen == [("MemorySetInput", "dev", "scratch", "bot")]


def test_the_namespace_is_the_installs_default_namespace(
    seen: Seen, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "default_namespace", "home")
    _post("memory.set", {"memory_key": "k", "value": "v"})
    assert seen == [("MemorySetInput", "dev", "home", "http")]


@pytest.mark.parametrize(
    ("op", "body", "named"),
    [
        ("namespace.delete", {}, {"namespace"}),
        ("memory.clear", {}, {"namespace"}),
        ("memory.lock", {"memory_key": "k", "reason": "x"}, {"namespace", "agent"}),
        ("memory.bulk_get", {"memory_keys": ["k"]}, {"namespace", "agent"}),
        ("working_memory.set", {"content": "c"}, {"namespace", "agent"}),
        ("tracker.item", {"number": 1}, {"namespace"}),
    ],
)
def test_any_other_op_still_names_its_identity(
    seen: Seen, op: str, body: dict[str, Any], named: set[str]
) -> None:
    """Admin and namespace ops, exact-identity reads and writes, working memory and
    the plane ops: a namespace or agent left out is still missing, never the
    install's."""
    assert named <= _missing(_post(op, body))
    assert seen == []


def test_every_filled_op_takes_the_fields_it_fills() -> None:
    """Each op on the list takes org and namespace, and an agent of `http` or `*` only
    where it requires one."""
    for name, agent in SINGLE_USER_FILLED_OPS.items():
        fields = OPERATIONS_BY_NAME[name].input_model.model_fields
        assert {"org", "namespace"} <= set(fields), name
        required_agent = "agent" in fields and fields["agent"].is_required()
        assert bool(agent) == required_agent, name


def test_a_call_with_a_token_names_its_own_identity(seen: Seen) -> None:
    """A token is a caller of its own, so nothing is filled for it."""
    token = mint_token(
        subject="probe", org="dev", namespaces=["local"], roles=["writer"], ttl_seconds=300
    )
    assert {"namespace", "agent"} <= _missing(
        _post("memory.set", {"memory_key": "k", "value": "v"}, token=token)
    )
    assert seen == []


def test_another_identity_mode_without_a_token_is_not_filled(
    seen: Seen, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Auth off under another identity provider has no single install identity."""
    monkeypatch.setattr(settings, "identity_provider", "gateway")
    monkeypatch.setattr(type(settings), "auth_actually_required", lambda self: False)
    _missing(_post("memory.set", {"memory_key": "k", "value": "v"}))
    assert seen == []


def test_a_server_with_sign_in_refuses_a_call_with_no_token(
    seen: Seen, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "identity_provider", "gateway")
    monkeypatch.setattr(settings, "auth_required", True)
    assert _post("memory.set", {"memory_key": "k", "value": "v"}).status_code == 401
    assert seen == []


def test_a_token_call_naming_no_namespace_is_refused(
    seen: Seen, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What `Agent(api_key=...)` with no namespace sends to a server with sign-in
    (the SDK leaves an empty namespace out): nothing fills it, and it is a 422."""
    monkeypatch.setattr(settings, "identity_provider", "gateway")
    monkeypatch.setattr(settings, "auth_required", True)
    token = mint_token(
        subject="probe", org="dev", namespaces=["local"], roles=["writer"], ttl_seconds=300
    )
    body = {"org": "", "agent": "bot", "memory_key": "k", "value": "v"}
    assert "namespace" in _missing(_post("memory.set", body, token=token))
    assert seen == []


def test_the_http_reference_names_every_filled_op() -> None:
    """The op list in the HTTP reference is the code's list."""
    from pathlib import Path

    page = (Path(__file__).resolve().parents[2] / "docs/reference/http-api.md").read_text()
    identity = page[page.index("- **Identity.**") : page.index("- **`consumer`.**")]
    assert [op for op in SINGLE_USER_FILLED_OPS if f"`{op}`" not in identity] == []


def test_an_org_wide_op_runs_in_the_installs_org(seen: Seen) -> None:
    """Not on the list, but the install has one org: an org-wide read with no
    namespace reads it, rather than org ""."""
    _post("audit.list", {})
    assert seen == [("AuditListInput", "dev", None, None)]


def test_a_code_or_tracker_op_keeps_its_plane_org(seen: Seen) -> None:
    """Their org defaults to the plane's in the gate, not to the install's."""
    from server.transport.gates import with_single_user_identity

    for name in ("tracker.item", "code.search"):
        args = {"number": 1, "query": "q"}
        filled = asyncio.run(with_single_user_identity(OPERATIONS_BY_NAME[name], args, None))
        assert filled == args


@pytest.mark.parametrize("value", [0, False, [], {}])
def test_a_value_that_is_not_an_identity_is_left_for_validation(seen: Seen, value: Any) -> None:
    _missing(_post("memory.set", {"namespace": value, "memory_key": "k", "value": "v"}))
    assert seen == []


@pytest.mark.parametrize(
    ("op", "body"),
    [
        ("memory.delete", {"memory_key": "deploy_window"}),
        ("memory.bulk_set", {"entries": [{"memory_key": "k", "value": "v"}]}),
    ],
)
def test_a_listed_delete_or_bulk_write_runs_as_http(
    seen: Seen, op: str, body: dict[str, Any]
) -> None:
    _post(op, body)
    assert len(seen) == 1 and seen[0][1:] == ("dev", "local", "http")


def test_the_ledger_records_the_identity_the_call_ran_as(seen: Seen) -> None:
    _post("memory.set", {"memory_key": "k", "value": "v"})
    assert _LEDGER, "no ledger row"
    row = _LEDGER[-1]
    assert (row["org"], row["namespace"], row["agent"]) == ("dev", "local", "http")


@pytest.mark.parametrize(
    ("op", "body"),
    [
        ("memory.update", {"memory_key": "k", "value": "v2"}),
        ("memory.ask", {"query": "when?"}),
        ("memory.search_trace", {"query": "q"}),
        ("search.query", {"query": "q"}),
        ("knowledge.search", {"query": "q"}),
    ],
)
def test_a_listed_op_with_an_optional_agent_gets_the_installs_namespace(
    seen: Seen, op: str, body: dict[str, Any]
) -> None:
    _post(op, body)
    assert len(seen) == 1 and seen[0][1:3] == ("dev", "local")


@pytest.mark.parametrize("scope", [{"user_id": "alice"}, {"session_id": "s1"}])
def test_a_history_naming_a_user_or_session_names_its_agent(
    seen: Seen, scope: dict[str, str]
) -> None:
    """A history under agent `*` reads every user's and session's versions: one that
    names a user or session is not widened past it, and still names its agent."""
    assert "agent" in _missing(_post("memory.history", {"memory_key": "k", **scope}))
    assert seen == []


def test_a_list_naming_a_user_reads_every_agent_of_that_user(seen: Seen) -> None:
    """A list keeps its user and session filters under agent `*`."""
    _post("memory.list", {"user_id": "alice"})
    assert len(seen) == 1 and seen[0][1:] == ("dev", "local", "*")


@pytest.mark.parametrize("blank", ["", "-"])
def test_a_blank_namespace_on_another_op_is_a_missing_one(seen: Seen, blank: str) -> None:
    """What the TypeScript SDK sends for an Agent built with no namespace."""
    body = {"namespace": blank, "agent": "a", "content": "c"}
    assert "namespace" in _missing(_post("working_memory.set", body))
    assert seen == []


def test_a_misspelled_namespace_is_not_filled(seen: Seen) -> None:
    """`namepsace` named one: the call is refused rather than run in the install's."""
    response = _post("memory.search", {"namepsace": "team", "query": "q"})
    assert "namespace" in _missing(response)
    assert seen == []


def test_a_history_naming_user_dash_reads_every_agent(seen: Seen) -> None:
    """`-` is no user: the history is filled with `*` as for none."""
    _post("memory.history", {"memory_key": "k", "user_id": "-"})
    assert len(seen) == 1 and seen[0][3] == "*"


def test_a_blank_namespace_beside_a_misspelled_one_is_refused(seen: Seen) -> None:
    body = {"namespace": "", "namepsace": "team", "query": "q"}
    assert "namespace" in _missing(_post("memory.search", body))
    assert seen == []


def test_a_padded_namespace_is_kept_and_stripped(seen: Seen) -> None:
    """A name with spaces round it names a namespace: it is not filled."""
    _post("memory.get", {"namespace": " team ", "memory_key": "k"})
    assert len(seen) == 1 and seen[0][2] == "team"
