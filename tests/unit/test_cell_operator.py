"""The operator's directory commands (server/cli/cell.py) and the router's operator API
(server/router/app.py; docs/architecture/cells.md, "Operations")."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from typing import Any

import httpx
import pytest
from starlette.testclient import TestClient

from server.cli import cell as cell_cli
from server.router import app as router_app
from server.router import directory
from server.settings import settings
from tests.unit.clock_fake import ManualClock

OPERATOR = "lco_" + "7" * 128
SHA = "a" * 64


def test_a_new_credential_is_lcr_and_128_hex_with_its_sha256(capsys) -> None:
    credential, sha = cell_cli.new_credential()
    assert credential.startswith("lcr_") and len(credential) == 132
    assert all(c in "0123456789abcdef" for c in credential[4:])
    assert sha == hashlib.sha256(credential.encode()).hexdigest()
    args = _args(action="new", router=None)
    assert cell_cli._cmd_credential(args) == 0
    out = capsys.readouterr().out
    assert "credential: lcr_" in out and "sha256:" in out


def test_an_operator_credential_is_lco_and_128_hex(capsys) -> None:
    assert cell_cli._cmd_credential(_args(action="new", operator=True, router=None)) == 0
    out = capsys.readouterr()
    assert "credential: lco_" in out.out and "LOOM_ROUTER_OPERATOR_SHA256" in out.err
    assert cell_cli._cmd_credential(_args(cell="eu-1", finish=True, operator=True)) == 2


def _args(**kw: Any) -> argparse.Namespace:
    base: dict[str, Any] = {
        "action": None,
        "cell": None,
        "next": None,
        "finish": False,
        "revert": False,
        "router": "https://router.test",
        "closed": False,
        "open": False,
        "placements_max": None,
        "org": None,
        "operator": False,
    }
    return argparse.Namespace(**{**base, **kw})


@pytest.mark.parametrize(
    "kw",
    [
        {"action": "new", "cell": "eu-1", "router": None},
        {"action": "new", "next": "a" * 64, "router": None},
        {"action": "new", "finish": True, "router": None},
        {"action": "new", "revert": True, "router": None},
        {"action": "new", "router": "https://router.test"},
        {"cell": "eu-1"},  # no step
        {"cell": "eu-1", "finish": True, "revert": True},
        {"finish": True},  # no cell
        {"cell": "eu-1", "next": "lcr_" + "0" * 128},  # the credential, not its hash
        {"cell": "eu-1", "next": "A" * 64},
    ],
)
def test_a_credential_command_it_cannot_run_is_refused_before_any_call(
    monkeypatch, kw: dict[str, Any]
) -> None:
    monkeypatch.setattr(httpx, "post", lambda *a, **k: pytest.fail("called the router"))
    assert cell_cli._cmd_credential(_args(**kw)) == 2


class _Calls:
    def __init__(self, status: int = 200, answer: Any = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.status, self.answer = status, answer if answer is not None else {"cell": "eu-1"}

    def __call__(self, url: str, **kw: Any) -> httpx.Response:
        self.calls.append({"url": url, **kw})
        return httpx.Response(self.status, json=self.answer)


@pytest.fixture
def operator(monkeypatch) -> _Calls:
    monkeypatch.setattr(settings, "router_operator_token", OPERATOR)
    calls = _Calls()
    monkeypatch.setattr(httpx, "post", calls)
    return calls


@pytest.mark.parametrize(
    "kw,body",
    [
        ({"next": SHA}, {"step": "next", "sha256": SHA}),
        ({"finish": True}, {"step": "finish"}),
        ({"revert": True}, {"step": "revert"}),
    ],
)
def test_a_rotation_step_calls_the_operator_api(operator, kw, body) -> None:
    assert cell_cli._cmd_credential(_args(cell="eu-1", **kw)) == 0
    (call,) = operator.calls
    assert call["url"] == "https://router.test/cells/v1/operator/cells/eu-1/credential"
    assert call["json"] == body and call["headers"] == {"Authorization": f"Bearer {OPERATOR}"}


@pytest.mark.parametrize(
    "kw,body",
    [
        ({"closed": True}, {"closed": True}),
        ({"open": True}, {"closed": False}),
        ({"placements_max": "none"}, {"placements_max": None}),
        ({"placements_max": "500", "closed": True}, {"closed": True, "placements_max": 500}),
    ],
)
def test_a_limit_calls_the_operator_api(operator, kw, body) -> None:
    assert cell_cli._cmd_limit(_args(cell="eu-1", **kw)) == 0
    assert operator.calls[0]["json"] == body
    assert operator.calls[0]["url"].endswith("/cells/v1/operator/cells/eu-1/limit")


@pytest.mark.parametrize(
    "kw",
    [
        {},
        {"placements_max": "-1"},
        {"placements_max": "lots"},
        {"placements_max": "²"},
        {"placements_max": "+5"},
        {"placements_max": " 5"},
        {"placements_max": str(2**63)},
        {"placements_max": "9" * 5000},  # past int()'s digit limit
    ],
)
def test_an_empty_or_malformed_limit_is_refused_before_any_call(operator, kw) -> None:
    assert cell_cli._cmd_limit(_args(cell="eu-1", **kw)) == 2 and operator.calls == []


def test_an_operator_command_needs_the_operator_credential(monkeypatch) -> None:
    monkeypatch.setattr(settings, "router_operator_token", "")
    monkeypatch.setattr(httpx, "post", lambda *a, **k: pytest.fail("called the router"))
    assert cell_cli._cmd_limit(_args(cell="eu-1", closed=True)) == 2


def test_a_refusal_is_said_and_fails(monkeypatch, capsys) -> None:
    monkeypatch.setattr(settings, "router_operator_token", OPERATOR)
    monkeypatch.setattr(httpx, "post", _Calls(409, {"error": "cell 'eu-2' records that hash"}))
    assert cell_cli._cmd_credential(_args(cell="eu-1", next=SHA)) == 1
    assert "records that hash" in capsys.readouterr().err


@pytest.mark.parametrize(
    "error,said",
    [
        (httpx.ReadTimeout("slow"), "the change may have been made"),
        (httpx.WriteTimeout("slow"), "the change may have been made"),
        (httpx.PoolTimeout("slow"), "the change may have been made"),
        (httpx.ReadError("reset"), "the change may have been made"),
        (httpx.RemoteProtocolError("closed"), "the change may have been made"),
        (httpx.ConnectTimeout("slow"), "could not be reached"),
        (httpx.ConnectError("refused"), "could not be reached"),
        (httpx.ProxyError("CONNECT refused"), "could not be reached"),
    ],
)
def test_a_failure_after_sending_may_have_been_made_and_a_connect_failure_was_not(
    monkeypatch, capsys, error, said
) -> None:
    def _slow(*a: Any, **k: Any) -> httpx.Response:
        raise error

    monkeypatch.setattr(settings, "router_operator_token", OPERATOR)
    monkeypatch.setattr(httpx, "post", _slow)
    assert cell_cli._cmd_credential(_args(cell="eu-1", finish=True)) == 1
    assert said in capsys.readouterr().err


@pytest.mark.parametrize(
    "router,code",
    [
        ("http://router.test", 2),
        ("http://127.0.0.1:8080", 0),
        ("http://127.0.0.2:8080", 0),
        ("http://[::1]:8080", 0),
        ("http://localhost:8080", 0),
    ],
)
def test_the_operator_credential_goes_only_to_https_or_loopback(operator, router, code) -> None:
    assert cell_cli._cmd_limit(_args(cell="eu-1", closed=True, router=router)) == code
    assert len(operator.calls) == (code == 0)


@pytest.mark.parametrize(
    "answer,maybe",
    [
        (httpx.Response(503, json={"error": "down", "code": "BACKEND_UNAVAILABLE"}), True),
        (httpx.Response(504, text="<html>gateway timeout</html>"), True),
        (httpx.Response(504, json={"message": "Endpoint request timed out"}), True),
        (httpx.Response(502, json={"error": "bad gateway"}), True),
        (
            httpx.Response(
                503, json={"error": "no operator credential", "code": "NO_OPERATOR_CREDENTIAL"}
            ),
            False,
        ),
        (httpx.Response(503, json={"error": "upstream reset"}), True),  # a proxy's
        (httpx.Response(409, json={"error": "pending"}), False),
        (httpx.Response(200, json={}), True),  # not the router's answer for eu-1
        (httpx.Response(429, json={"error": "rate limited"}), True),  # an ingress's
    ],
)
def test_only_an_answer_that_may_follow_a_commit_says_the_change_may_have_been_made(
    monkeypatch, capsys, answer, maybe
) -> None:
    answer.request = httpx.Request("POST", "https://router.test/")
    monkeypatch.setattr(settings, "router_operator_token", OPERATOR)
    monkeypatch.setattr(httpx, "post", lambda *a, **k: answer)
    assert cell_cli._cmd_limit(_args(cell="eu-1", closed=True)) == 1
    assert ("safe to repeat" in capsys.readouterr().err) is maybe


@pytest.mark.parametrize(
    "error,said",
    [(httpx.ReadTimeout("slow"), "no answer from"), (httpx.ConnectError("no"), "be reached")],
)
def test_which_tells_a_slow_router_from_an_unreachable_one(
    monkeypatch, capsys, error, said
) -> None:
    def _get(*a: Any, **k: Any) -> httpx.Response:
        raise error

    monkeypatch.setattr(httpx, "get", _get)
    assert cell_cli._cmd_which(_args(org="acme")) == 1
    assert said in capsys.readouterr().err


@pytest.mark.parametrize(
    "answer,said",
    [
        (httpx.Response(200, json={}), "did not answer as the router does"),
        (httpx.Response(200, json={"org": "other", "cell": "eu-1"}), "did not answer as"),
        (httpx.Response(503, json={"error": "no cell", "code": "NO_CELL"}), "refused (503)"),
        (httpx.Response(429, json={"error": "too many lookups"}), "refused (429)"),
        (
            httpx.Response(503, json={"error": "down", "code": "BACKEND_UNAVAILABLE"}),
            "did not answer, try again",
        ),
    ],
)
def test_which_fails_on_any_answer_but_the_orgs_cell(monkeypatch, capsys, answer, said) -> None:
    answer.request = httpx.Request("GET", "https://router.test/cells/lookup")
    monkeypatch.setattr(httpx, "get", lambda *a, **k: answer)
    assert cell_cli._cmd_which(_args(org="acme")) == 1
    assert said in capsys.readouterr().err


@pytest.mark.parametrize(
    "router",
    [
        "https://router:notaport",
        "https://",
        "ftp://router.test",
        "router.test",
        "https://router.test/?x",  # the operator path would land in the query
        "https://router.test/#x",
        "https://router.test/r",  # the router would forward /r/cells/... to a cell
        "https://router.test/a%2Fb",
        "https://u-canary:p-canary@router.test",
    ],
)
def test_a_router_url_that_is_no_url_is_refused_before_any_call(operator, capsys, router) -> None:
    assert cell_cli._cmd_limit(_args(cell="eu-1", closed=True, router=router)) == 2
    err = capsys.readouterr().err
    assert operator.calls == [] and "canary" not in err  # the user part is never echoed
    assert err.count("\n") == 1 and "router URL must be" in err


@pytest.mark.parametrize("base", ["https://h.test", "https://h.test/"])
def test_the_operator_path_is_set_at_the_routers_root(operator, base) -> None:
    assert cell_cli._cmd_limit(_args(cell="eu-1", closed=True, router=base)) == 0
    assert str(operator.calls[0]["url"]) == "https://h.test/cells/v1/operator/cells/eu-1/limit"
    assert operator.calls[0]["timeout"] == cell_cli._OPERATOR_TIMEOUT


def test_plain_http_on_loopback_reaches_no_proxy(operator) -> None:
    assert cell_cli._cmd_limit(_args(cell="eu-1", closed=True, router="http://127.0.0.1:8")) == 0
    assert operator.calls[0]["trust_env"] is False


def test_https_keeps_the_environments_proxy(operator) -> None:
    assert cell_cli._cmd_limit(_args(cell="eu-1", closed=True)) == 0
    assert operator.calls[0]["trust_env"] is True  # a CONNECT tunnel stays encrypted


def test_which_asks_the_public_lookup(monkeypatch, capsys) -> None:
    seen: list[Any] = []
    timeouts: list[Any] = []

    def _get(url: str, **kw: Any) -> httpx.Response:
        seen.append((url, kw["params"]))
        timeouts.append(kw["timeout"])
        return httpx.Response(200, json={"org": "acme", "cell": "eu-2", "host": "eu-2.example"})

    monkeypatch.setattr(httpx, "get", _get)
    assert cell_cli._cmd_which(_args(org="acme")) == 0
    assert seen == [("https://router.test/cells/lookup", {"org": "acme"})]
    assert timeouts == [cell_cli._LOOKUP_TIMEOUT]
    assert json.loads(capsys.readouterr().out)["cell"] == "eu-2"


def test_a_command_with_no_router_is_refused(monkeypatch, capsys) -> None:
    monkeypatch.setattr(settings, "cell_router_url", "")
    assert cell_cli._cmd_which(_args(org="acme", router=None)) == 2
    assert "no router" in capsys.readouterr().err


@pytest.mark.parametrize("name", ["../../v1/memory.get?x=", "EU_1", "", "a" * 33])
def test_a_cell_name_no_cell_can_have_is_refused_before_the_credential_is_sent(
    operator, name
) -> None:
    assert cell_cli._cmd_limit(_args(cell=name, closed=True)) == 2 and operator.calls == []


@pytest.mark.parametrize("command", ["which", "limit"])
def test_a_page_that_is_not_the_routers_answer_fails(monkeypatch, capsys, command) -> None:
    """A URL that serves something else (an HTML page, with 200) is not read as the
    router's answer; for a change, it may have been made."""
    page = httpx.Response(
        200, text="<html>not the router</html>", request=httpx.Request("GET", "https://x/")
    )
    monkeypatch.setattr(settings, "router_operator_token", OPERATOR)
    monkeypatch.setattr(httpx, "get", lambda *a, **k: page)
    monkeypatch.setattr(httpx, "post", lambda *a, **k: page)
    if command == "which":
        assert cell_cli._cmd_which(_args(org="acme")) == 1
    else:
        assert cell_cli._cmd_limit(_args(cell="eu-1", closed=True)) == 1
    err = capsys.readouterr().err
    assert "did not answer as the router does" in err
    assert ("the change may have been made" in err) is (command == "limit")


def _parse(*argv: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    cell_cli._add_cell_parser(parser.add_subparsers(dest="cmd"))
    return parser.parse_args(["cell", *argv])


def test_the_parser_refuses_closed_with_open_and_routes_each_command() -> None:
    with pytest.raises(SystemExit) as caught:
        _parse("limit", "--cell", "eu-1", "--closed", "--open")
    assert caught.value.code == 2
    assert _parse("limit", "--cell", "eu-1", "--closed").func is cell_cli._cmd_limit
    assert _parse("credential", "new").action == "new"
    assert _parse("credential", "--cell", "eu-1", "--finish").func is cell_cli._cmd_credential
    assert _parse("which", "--org", "acme").func is cell_cli._cmd_which


def test_an_older_router_without_the_operator_api_is_a_refusal(operator, capsys) -> None:
    """A router of an earlier build answers the operator path with its own 404: nothing
    was done, and the CLI says so rather than that the change may have been made."""
    operator.status, operator.answer = 404, {"error": "the router answers no such path"}
    assert cell_cli._cmd_limit(_args(cell="eu-1", closed=True)) == 1
    err = capsys.readouterr().err
    assert "refused (404)" in err and "may have been made" not in err


# The router's operator API.


def _row(**over: Any) -> directory.Cell:
    values: dict[str, Any] = {
        "cell": "eu-1",
        "is_first": False,
        "host": "eu-1.example",
        "state": "serving",
        "mode": "enforced",
        "closed": False,
        "placements_max": None,
        "placed": 3,
        "credential_sha256": "b" * 64,
        "credential_next_sha256": None,
        "deployment_id": "dep-eu-1",
        "current_fp": None,
        "next_fp": None,
    }
    return directory.Cell(**{**values, **over})


@pytest.fixture
def api(monkeypatch) -> tuple[TestClient, list[Any]]:
    monkeypatch.setattr(
        settings, "router_operator_sha256", hashlib.sha256(OPERATOR.encode()).hexdigest()
    )
    calls: list[Any] = []

    async def _limit(cell: str, **kw: Any) -> directory.Cell:
        calls.append(("limit", cell, kw))
        if cell == "nope":
            raise directory.NoSuchCell("cell 'nope' is not in the directory")
        return _row(closed=kw.get("closed") is True)

    async def _rotate(cell: str, step: str, sha: str = "") -> directory.Cell:
        calls.append(("credential", cell, step, sha))
        if sha == "c" * 64:
            raise directory.OperatorRefused("cell 'eu-2' records that credential hash already")
        return _row(credential_next_sha256=sha or None)

    monkeypatch.setattr(directory, "set_limit", _limit)
    monkeypatch.setattr(directory, "rotate_credential", _rotate)
    return TestClient(router_app.build_app(router_app.Router())), calls


def _post(client: TestClient, path: str, body: Any, token: str = OPERATOR) -> Any:
    return client.post(
        f"/cells/v1/operator/cells/{path}", json=body, headers={"Authorization": f"Bearer {token}"}
    )


def test_the_operator_api_needs_the_routers_operator_credential(api, monkeypatch) -> None:
    client, calls = api
    assert _post(client, "eu-1/limit", {"closed": True}, token="wrong").status_code == 401
    assert (
        client.post("/cells/v1/operator/cells/eu-1/limit", json={"closed": True}).status_code == 401
    )
    monkeypatch.setattr(settings, "router_operator_sha256", "")
    assert _post(client, "eu-1/limit", {"closed": True}).status_code == 503
    assert calls == []


def test_a_limit_writes_the_directory_and_answers_the_row(api) -> None:
    client, calls = api
    r = _post(client, "eu-1/limit", {"closed": True, "placements_max": 10})
    assert r.status_code == 200 and r.json()["closed"] is True
    assert calls == [("limit", "eu-1", {"closed": True, "placements_max": 10})]
    _post(client, "eu-1/limit", {"placements_max": None})
    assert calls[-1] == ("limit", "eu-1", {"closed": directory.UNSET, "placements_max": None})


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"closed": "yes"},
        {"placements_max": -1},
        {"placements_max": True},
        {"other": 1},
        [],
        {"placements_max": 2**63},
        {"closed": None},
    ],
)
def test_a_malformed_limit_answers_400_and_writes_nothing(api, body) -> None:
    client, calls = api
    assert _post(client, "eu-1/limit", body).status_code == 400 and calls == []


def test_an_unknown_cell_answers_404_and_a_refused_rotation_409(api) -> None:
    client, _ = api
    assert _post(client, "nope/limit", {"closed": True}).status_code == 404
    r = _post(client, "eu-1/credential", {"step": "next", "sha256": "c" * 64})
    assert r.status_code == 409 and "records that credential hash" in r.json()["error"]


def test_a_cell_name_no_cell_can_have_answers_404_before_the_directory(api) -> None:
    client, calls = api
    assert _post(client, "EU_1/limit", {"closed": True}).status_code == 404 and calls == []


@pytest.mark.parametrize(
    "body",
    [
        {"step": "next"},
        {"step": "finish", "sha256": SHA},
        {"step": "finish", "sha256": None},
        {"step": "finish", "extra": 1},
        {"step": "revert", "sha256": ""},
        {"step": "next", "sha256": "lcr_" + "0" * 128},
        {"step": "rotate"},
        {},
    ],
)
def test_a_malformed_rotation_answers_400(api, body) -> None:
    client, calls = api
    assert _post(client, "eu-1/credential", body).status_code == 400 and calls == []


def test_a_rotation_step_is_written_and_named_pending(api) -> None:
    client, calls = api
    r = _post(client, "eu-1/credential", {"step": "next", "sha256": SHA})
    assert r.status_code == 200 and r.json()["next_credential_pending"] is True
    assert calls == [("credential", "eu-1", "next", SHA)]
    done = _post(client, "eu-1/credential", {"step": "finish"}).json()
    assert done == {
        "cell": "eu-1",
        "closed": False,
        "placements_max": None,
        "placed_orgs": 3,
        "next_credential_pending": False,
    }


def test_a_cells_row_holds_its_current_and_next_credential() -> None:
    row = _row(credential_next_sha256=SHA)
    assert row.holds("b" * 64) and row.holds(SHA) and not row.holds("d" * 64)
    assert not _row().holds(SHA)


def test_refused_operator_calls_do_not_lock_out_the_operator(api) -> None:
    client, calls = api
    codes = {
        _post(client, "eu-1/limit", {"closed": True}, token="wrong").status_code for _ in range(12)
    }
    assert codes == {401} and calls == []
    assert _post(client, "eu-1/limit", {"closed": True}).status_code == 200


def test_a_rotation_is_seen_by_this_replica_on_its_next_call(monkeypatch) -> None:
    """The replica that took `--next` reads the cells' hashes again at once."""
    monkeypatch.setattr(
        settings, "router_operator_sha256", hashlib.sha256(OPERATOR.encode()).hexdigest()
    )
    new = "lcr_" + "1" * 128
    hashes: dict[str, tuple[str, ...]] = {"eu-1": ("b" * 64,)}
    reads: list[int] = []

    async def _credentials() -> dict[str, tuple[str, ...]]:
        reads.append(1)
        return dict(hashes)

    async def _rotate(cell: str, step: str, sha: str = "") -> directory.Cell:
        hashes["eu-1"] = ("b" * 64, sha)
        return _row(credential_next_sha256=sha)

    monkeypatch.setattr(directory, "credentials", _credentials)
    monkeypatch.setattr(directory, "rotate_credential", _rotate)
    router = router_app.Router(clock=ManualClock())
    asyncio.run(router._credential("eu-1"))
    client = TestClient(router_app.build_app(router))
    sha = hashlib.sha256(new.encode()).hexdigest()
    assert _post(client, "eu-1/credential", {"step": "next", "sha256": sha}).status_code == 200
    assert asyncio.run(router._credential("eu-1")) == ("b" * 64, sha)
    assert len(reads) == 2


@pytest.mark.parametrize(
    "error,status,read_again",
    [
        (directory.DirectoryUnavailable("the directory could not be read"), 503, True),
        (directory.OperatorRefused("cell 'eu-1' has another next credential pending"), 409, False),
    ],
)
def test_only_a_rotation_that_may_have_changed_drops_the_hash_cache(
    monkeypatch, error, status, read_again
) -> None:
    monkeypatch.setattr(
        settings, "router_operator_sha256", hashlib.sha256(OPERATOR.encode()).hexdigest()
    )
    reads: list[int] = []

    async def _credentials() -> dict[str, tuple[str, ...]]:
        reads.append(1)
        return {"eu-1": ("b" * 64,)}

    async def _rotate(*a: Any, **k: Any) -> directory.Cell:
        raise error

    monkeypatch.setattr(directory, "credentials", _credentials)
    monkeypatch.setattr(directory, "rotate_credential", _rotate)
    router = router_app.Router(clock=ManualClock())
    asyncio.run(router._credential("eu-1"))
    client = TestClient(router_app.build_app(router))
    assert _post(client, "eu-1/credential", {"step": "finish"}).status_code == status
    asyncio.run(router._credential("eu-1"))
    assert len(reads) == (2 if read_again else 1)


def test_a_hash_read_begun_before_a_change_is_not_kept(monkeypatch) -> None:
    router = router_app.Router(clock=ManualClock())
    reads: list[int] = []

    async def _credentials() -> dict[str, tuple[str, ...]]:
        reads.append(1)
        if len(reads) == 1:
            router._forget_credentials()  # a rotation lands while this read runs
        return {"eu-1": ("b" * 64,)}

    monkeypatch.setattr(directory, "credentials", _credentials)
    asyncio.run(router._credential("eu-1"))
    asyncio.run(router._credential("eu-1"))
    assert len(reads) == 2  # the read that raced the change was not kept for a window


def test_every_503_of_the_lookup_and_the_operator_api_carries_a_code(monkeypatch) -> None:
    """The CLI reads a codeless 503 as a proxy's, so each 503 these routes give names one."""

    async def _down(*a: Any, **k: Any) -> Any:
        raise directory.DirectoryUnavailable("the directory could not be read: OSError")

    async def _none(*a: Any, **k: Any) -> Any:
        return None

    client = TestClient(router_app.build_app(router_app.Router(clock=ManualClock())))
    monkeypatch.setattr(directory, "lookup", _down)
    down = client.get("/cells/lookup", params={"org": "a"}).json()
    assert down["code"] == "BACKEND_UNAVAILABLE"
    monkeypatch.setattr(directory, "lookup", _none)
    monkeypatch.setattr(directory, "first_cell", _none)
    assert client.get("/cells/lookup", params={"org": "b"}).json()["code"] == "NO_CELL"
    monkeypatch.setattr(settings, "router_operator_sha256", "")
    unset = _post(client, "eu-1/limit", {"closed": True})
    assert (unset.status_code, unset.json()["code"]) == (503, "NO_OPERATOR_CREDENTIAL")
    monkeypatch.setattr(
        settings, "router_operator_sha256", hashlib.sha256(OPERATOR.encode()).hexdigest()
    )
    monkeypatch.setattr(directory, "set_limit", _down)
    monkeypatch.setattr(directory, "rotate_credential", _down)
    for path, body in (("eu-1/limit", {"closed": True}), ("eu-1/credential", {"step": "finish"})):
        r = _post(client, path, body)
        assert (r.status_code, r.json()["code"]) == (503, "BACKEND_UNAVAILABLE")
