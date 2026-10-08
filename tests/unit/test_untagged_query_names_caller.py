"""A ClickHouse query issued with no tag names the server function that issued it.

Every query is tagged with its operation via `log_comment`; a query from
outside a request or worker pass has no operation, and such queries can be a
large share of read bytes. `query_settings` falls back to
`untagged:<module>.<function>` of the nearest server frame above the plumbing.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest

from server.core import ch


@pytest.fixture(autouse=True)
def _no_tag() -> Iterator[None]:
    """Start untagged, whatever an earlier test left in the context."""
    token = ch.query_tag_var.set("")
    yield
    ch.query_tag_var.reset(token)


def test_untagged_query_names_its_caller() -> None:
    g = {"__name__": "server.memory.app.gauges", "ch": ch}
    exec("def collect():\n    return ch.query_settings()\n", g)
    assert g["collect"]() == {"log_comment": "untagged:memory.app.gauges.collect"}


def test_repository_frames_are_skipped_for_the_caller_above() -> None:
    repo_globals = {"__name__": "server.memory.store.x_repo", "ch": ch}
    exec("def read():\n    return ch.query_settings()\n", repo_globals)
    svc_globals = {"__name__": "server.memory.app.x", "read": repo_globals["read"]}
    exec("def use():\n    return read()\n", svc_globals)
    assert svc_globals["use"]() == {"log_comment": "untagged:memory.app.x.use"}


def test_domain_store_frames_are_skipped_for_the_caller_above() -> None:
    store_globals = {"__name__": "server.erasure.store.census", "ch": ch}
    exec("def read():\n    return ch.query_settings()\n", store_globals)
    app_globals = {"__name__": "server.erasure.app.saga", "read": store_globals["read"]}
    exec("def run():\n    return read()\n", app_globals)
    assert app_globals["run"]() == {"log_comment": "untagged:erasure.app.saga.run"}


def test_async_caller_is_named() -> None:
    g = {"__name__": "server.workers.boot_loop", "ch": ch, "asyncio": asyncio}
    exec("async def probe():\n    await asyncio.sleep(0)\n    return ch.query_settings()\n", g)
    assert asyncio.run(g["probe"]()) == {"log_comment": "untagged:workers.boot_loop.probe"}


def test_a_tag_wins_and_outside_callers_send_nothing() -> None:
    assert ch.query_settings() == {}  # this test module is not server code
    ch.set_query_tag("memory.search r1")
    assert ch.query_settings() == {"log_comment": "memory.search r1"}
    assert ch.query_settings({"log_comment": "explicit"}) == {"log_comment": "explicit"}


def test_the_client_modules_are_plumbing() -> None:
    """The client's own modules (server/core/ch.py and its ch_* siblings) are never the
    caller a query is attributed to; a core module outside the client is."""
    for module in ("server.core.ch", "server.core.ch_writes", "server.core.ch_schema_guard"):
        assert ch._is_plumbing(module), module
    assert not ch._is_plumbing("server.core.schema.status")
