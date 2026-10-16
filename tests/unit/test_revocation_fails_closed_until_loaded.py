"""Tokens are refused until this process has read the revocation list once.

An empty snapshot at boot (the first load failed or has not run) must not
let a revoked token authenticate until a refresh lands. `verify_token`
raises RevocationUnavailable (a TokenError, so every
caller fails closed) until the first good load; HTTP answers it with a
retryable 503. After that, a failed refresh keeps the last good snapshot,
but only until it is `jwt_revocation_max_stale_seconds` old.
"""

from __future__ import annotations

from typing import Any

import pytest

import server.auth.bearer as bearer_mod
import server.auth.revocation as rev
from server.auth.jwt import RevocationUnavailable, TokenError, mint_token, verify_token

SECRET = "s" * 32


def _token() -> str:
    return mint_token(subject="alice", org="o", namespaces=["n"], roles=["writer"], secret=SECRET)


class _CH:
    def __init__(self, rows: list[dict[str, Any]] | None = None, fail: bool = False) -> None:
        self.rows, self.fail = rows or [], fail

    async def query_all(self, sql: str, *a: Any, **k: Any) -> list[dict[str, Any]]:
        if self.fail:
            raise RuntimeError("replica away")
        return self.rows


def test_a_token_is_refused_until_the_list_has_loaded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rev, "_loaded", False)
    with pytest.raises(RevocationUnavailable) as err:
        verify_token(_token(), secret=SECRET)
    assert isinstance(err.value, TokenError)  # every existing caller fails closed


@pytest.mark.asyncio
async def test_the_first_good_load_opens_the_door_and_a_later_glitch_keeps_it_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import server.core.ch as ch_mod

    monkeypatch.setattr(rev, "_loaded", False)
    monkeypatch.setattr(ch_mod, "get_client", lambda: _CH(fail=True))
    await rev.refresh()
    assert not rev.loaded()  # a failed first load leaves the door shut

    monkeypatch.setattr(ch_mod, "get_client", lambda: _CH(rows=[]))
    await rev.refresh()
    assert rev.loaded()
    assert verify_token(_token(), secret=SECRET).subject == "alice"

    monkeypatch.setattr(ch_mod, "get_client", lambda: _CH(fail=True))
    await rev.refresh()
    assert rev.loaded()  # last good snapshot kept; no outage from a glitch


@pytest.mark.asyncio
async def test_a_snapshot_past_the_stale_bound_refuses_until_a_refresh_lands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import server.core.ch as ch_mod
    from server.settings import settings

    monkeypatch.setattr(settings, "jwt_revocation_max_stale_seconds", 600.0)
    monkeypatch.setattr(rev, "_loaded_at", rev.time.monotonic() - 599)
    assert verify_token(_token(), secret=SECRET).subject == "alice"

    monkeypatch.setattr(rev, "_loaded_at", rev.time.monotonic() - 601)
    with pytest.raises(RevocationUnavailable):
        verify_token(_token(), secret=SECRET)
    monkeypatch.setattr(ch_mod, "get_client", lambda: _CH(fail=True))
    await rev.refresh()
    with pytest.raises(RevocationUnavailable):
        verify_token(_token(), secret=SECRET)

    monkeypatch.setattr(ch_mod, "get_client", lambda: _CH(rows=[]))
    await rev.refresh()
    assert verify_token(_token(), secret=SECRET).subject == "alice"


def test_http_answers_an_unloaded_list_with_a_retryable_503(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi import HTTPException

    from server.transport import http as http_mod

    def _unavailable(token: str) -> Any:
        raise RevocationUnavailable("token revocation list not loaded yet; retry shortly")

    monkeypatch.setattr(bearer_mod, "verify_token", _unavailable)

    class _Req:
        headers = {"Authorization": "Bearer x.y.z"}

    with pytest.raises(HTTPException) as err:
        http_mod._extract_auth(_Req())  # type: ignore[arg-type]
    assert err.value.status_code == 503
    assert err.value.detail["code"] == "AUTH_UNAVAILABLE"
    assert err.value.headers == {"Retry-After": "5"}


def test_every_http_bearer_check_answers_an_unloaded_list_with_503(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The admin router, claim release and the knowledge download share one check."""
    from fastapi import HTTPException

    from server.auth.bearer import verify_bearer

    monkeypatch.setattr(rev, "_loaded", False)
    tok = mint_token(subject="s", org="o", namespaces=["n"], roles=["admin"], ttl_seconds=60)
    with pytest.raises(HTTPException) as err:
        verify_bearer(tok)
    assert err.value.status_code == 503 and err.value.headers == {"Retry-After": "5"}
    with pytest.raises(HTTPException) as bad:
        verify_bearer("garbage")
    assert bad.value.status_code == 401


def test_me_and_task_watch_answer_an_unloaded_list_with_503(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The caller API's bearer checks and the task watch stream use the same check: a
    booting process must not tell the dashboard to drop a good token."""
    from types import SimpleNamespace

    from fastapi import HTTPException

    from server.identity.app import me_callers as me
    from server.settings import settings
    from server.tasks.app import watch_routes as task_watch

    monkeypatch.setattr(rev, "_loaded", False)
    monkeypatch.setattr(type(settings), "auth_actually_required", lambda self: True)
    tok = mint_token(subject="s", org="o", namespaces=["n"], roles=["writer"], ttl_seconds=60)
    req: Any = SimpleNamespace(headers={"Authorization": f"Bearer {tok}"}, state=SimpleNamespace())
    checks = [
        me.require_authed_caller,
        me.caller_identity,
        task_watch._bearer_auth,
    ]
    for check in checks:
        with pytest.raises(HTTPException) as err:
            check(req)
        assert err.value.status_code == 503, check.__name__
        assert err.value.headers == {"Retry-After": "5"}
    monkeypatch.setattr(rev, "_loaded", True)
    bad: Any = SimpleNamespace(headers={"Authorization": "Bearer garbage"}, state=SimpleNamespace())
    for check in checks:
        with pytest.raises(HTTPException) as err:
            check(bad)
        assert err.value.status_code == 401, check.__name__
