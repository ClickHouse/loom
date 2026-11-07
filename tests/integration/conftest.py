"""Integration-suite tiers, prerequisites, diagnostics and cleanup.

Tiers (markers): an unmarked test runs on every pull request. `slow` spends
seconds of wall clock on purpose (lease lapses, retry exhaustion, refresh
cycles); `destructive` stops merges, runs OPTIMIZE or deletes whole scopes;
`provider` needs a real embedder or LLM and skips on the fakes; `recovery`
drives a failure and the path back from it.

Prerequisites: an unset LOOM_BASE_URL skips the suite; a set but unhealthy
one fails it, because a configured server that does not answer is a broken
environment, not a missing one. ClickHouse is reached only at an explicit
LOOM_CH_HOST: without one, every client construction (the `ch_client`
fixture, `_resources.ch_client_from_env`, and the server package's own client
when a test calls handlers in-process) skips the test, so nothing lands in
whatever store listens on localhost.

On failure, the report carries the last Loom requests the test made (method,
path, status, X-Request-Id, latency) with credentials redacted, so a server
log line can be found by its request id.

At session end every org minted by `unique_org` is purged from ClickHouse and
the residue is counted; LOOM_IT_KEEP_ROWS=1 keeps the rows for debugging.
Without LOOM_CH_HOST there is no store to purge from, and the run warns.
"""

from __future__ import annotations

import collections
import os
import time
import warnings
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import pytest_asyncio

from tests.integration._redact import redact
from tests.integration._resources import (
    NO_CH_TARGET,
    ch_client_from_env,
    ch_target,
    ch_target_configured,
    minted_orgs,
    purge,
)

_TIERS = {
    "slow": "spends seconds of wall clock on purpose (lapses, retries, refresh cycles)",
    "destructive": "stops merges, runs OPTIMIZE or deletes whole scopes",
    "provider": "needs a real embedder or LLM provider; skips on the fakes",
    "recovery": "drives a failure and the recovery path back from it",
}
_REQUEST_LOG_DEPTH = 25


# Fixtures whose use puts a test in a tier, so the tier cannot drift from the
# prerequisite that defines it.
_TIER_BY_FIXTURE = {
    "require_real_embedder": "provider",
    "require_real_llm": "provider",
    "pause_loom_memory_merges": "destructive",
}


def pytest_configure(config: pytest.Config) -> None:
    for name, doc in _TIERS.items():
        config.addinivalue_line("markers", f"{name}: {doc}")


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        for fixture, tier in _TIER_BY_FIXTURE.items():
            if fixture in getattr(item, "fixturenames", ()):
                item.add_marker(tier)


@pytest.fixture(scope="session")
def control_plane_url() -> str:
    """The Loom server under test (LOOM_BASE_URL). Skips when unset and fails
    when set but not answering /v1/health with 200."""
    url = os.environ.get("LOOM_BASE_URL")
    if not url:
        pytest.skip(
            "Set LOOM_BASE_URL to a running Loom server (e.g. http://localhost:7777) "
            "after `make migrate`; see tests/integration/README.md."
        )
    try:
        with httpx.Client(base_url=url, timeout=5.0) as c:
            status = c.get("/v1/health").status_code
    except httpx.HTTPError as e:
        pytest.fail(f"LOOM_BASE_URL={url} is set but /v1/health failed: {e!r}", pytrace=False)
    if status != 200:
        pytest.fail(f"LOOM_BASE_URL={url} is set but /v1/health returned {status}", pytrace=False)
    _server_used.append(url)
    return url


# Set once a test reached the server: an unpurgeable run then warns.
_server_used: list[str] = []


@pytest.fixture
def ch_target_required() -> None:
    """For tests whose subprocesses (dev/ CLIs) read LOOM_CH_* themselves."""
    ch_target()


@pytest.fixture
def require_real_llm(control_plane_url: str) -> None:
    """Skip unless the server under test runs a real LLM (its /v1/health says
    so; the test process's own environment says nothing about the server's)."""
    with httpx.Client(base_url=control_plane_url, timeout=5.0) as c:
        if c.get("/v1/health").json().get("fake_llm"):
            pytest.skip("needs a real LLM provider (the server runs the fake LLM)")


@pytest.fixture
def require_real_embedder(control_plane_url: str) -> None:
    """Skip unless the server under test runs a real embedder (its /v1/health says so):
    ranking thresholds cannot hold on the fake one."""
    with httpx.Client(base_url=control_plane_url, timeout=5.0) as c:
        health = c.get("/v1/health").json()
    if health.get("fake_embedding"):
        pytest.skip("search-quality test needs a real embedder (server is on the fake embedder)")


@pytest_asyncio.fixture
async def agent(control_plane_url: str):
    """A Loom Agent in a unique namespace of `test-org`, authenticated with the
    `LOOM_API_KEY` the root conftest seeds for every test (or the caller's own)."""
    from loom import Agent

    ns = f"test-{os.urandom(4).hex()}"
    async with Agent(
        org="test-org",
        namespace=ns,
        agent="test-agent",
        base_url=control_plane_url,
        api_key=os.environ["LOOM_API_KEY"],
    ) as a:
        yield a


@pytest.fixture
def require_fake_llm(control_plane_url: str) -> None:
    """Skip unless the server under test runs the fake LLM, for a test that asserts
    what the fake writes (its /v1/health says so)."""
    with httpx.Client(base_url=control_plane_url, timeout=5.0) as c:
        if not c.get("/v1/health").json().get("fake_llm"):
            pytest.skip("asserts the fake LLM's output (the server runs a real provider)")


@pytest.fixture
def ch_client() -> Iterator[Any]:
    """Sync client on the configured store. Synchronous inserts, and no block
    deduplication: two identical event rows are two events."""
    client = ch_client_from_env(
        settings={"async_insert": 0, "wait_for_async_insert": 1, "insert_deduplicate": 0}
    )
    try:
        yield client
    finally:
        client.close()


@pytest.fixture(scope="session", autouse=True)
def _no_implicit_clickhouse() -> Iterator[None]:
    """Without LOOM_CH_HOST, building any ClickHouse client skips the test.
    The driver's constructors are the choke point: the server package's
    settings default to localhost:8123. Session-scoped, so module fixtures
    that connect are covered too."""
    if ch_target_configured():
        yield
        return
    import clickhouse_connect

    def _refuse(*_: Any, **__: Any) -> Any:
        pytest.skip(NO_CH_TARGET)

    mp = pytest.MonkeyPatch()
    mp.setattr(clickhouse_connect, "get_client", _refuse)
    mp.setattr(clickhouse_connect, "get_async_client", _refuse)
    try:
        yield
    finally:
        mp.undo()


_requests: collections.deque[str] = collections.deque(maxlen=_REQUEST_LOG_DEPTH)


def _entry(request: httpx.Request, response: httpx.Response | None, started: float) -> str:
    ms = (time.monotonic() - started) * 1000
    status = response.status_code if response is not None else "error"
    rid = response.headers.get("x-request-id", "-") if response is not None else "-"
    return redact(f"{request.method} {request.url} -> {status} rid={rid} {ms:.0f}ms")


@pytest.fixture(scope="session", autouse=True)
def _record_loom_requests() -> Iterator[None]:
    """Wrap httpx's send so every request a test makes lands in a short ring."""
    sync_send, async_send = httpx.Client.send, httpx.AsyncClient.send

    def send(self: httpx.Client, request: httpx.Request, **kw: Any) -> httpx.Response:
        started, response = time.monotonic(), None
        try:
            response = sync_send(self, request, **kw)
            return response
        finally:
            _requests.append(_entry(request, response, started))

    async def asend(self: httpx.AsyncClient, request: httpx.Request, **kw: Any) -> httpx.Response:
        started, response = time.monotonic(), None
        try:
            response = await async_send(self, request, **kw)
            return response
        finally:
            _requests.append(_entry(request, response, started))

    mp = pytest.MonkeyPatch()
    mp.setattr(httpx.Client, "send", send)
    mp.setattr(httpx.AsyncClient, "send", asend)
    try:
        yield
    finally:
        mp.undo()


@pytest.fixture(autouse=True)
def _fresh_request_log() -> None:
    _requests.clear()


@pytest.fixture(autouse=True)
def _quick_intent_marks() -> Iterator[None]:
    """In-process writes resolve their intents in a batched mark every 10 s; a test
    waiting for one would wait that long. The batching itself is what a test drains.
    Its own patch, so a test's `monkeypatch.undo()` keeps it."""
    from server.memory.app import intent_marks

    with pytest.MonkeyPatch.context() as m:
        m.setattr(intent_marks, "FLUSH_S", 0.2)
        yield


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[None]) -> Any:
    report = yield
    if report.failed and _requests:
        report.sections.append(("loom requests (newest last)", "\n".join(_requests)))
    return report


@pytest.fixture(scope="session", autouse=True)
def _purge_minted_orgs() -> Iterator[None]:
    yield
    orgs = minted_orgs()
    # Tests that write to ClickHouse directly use no server, and leave rows too.
    wrote = _server_used or ch_target_configured()
    if not orgs or not wrote or os.environ.get("LOOM_IT_KEEP_ROWS") == "1":
        return
    if not ch_target_configured():
        # HTTP-only tests may run against a remote server without a store to
        # purge from; say so rather than fail a run that wrote nothing here.
        if _server_used:
            warnings.warn(
                f"{len(orgs)} minted test orgs were not purged from the server's store. "
                + NO_CH_TARGET,
                stacklevel=1,
            )
        return
    try:
        client = ch_client_from_env()
    except Exception as e:  # noqa: BLE001 — no store reachable means nothing this run can purge
        pytest.fail(f"cannot reach ClickHouse to purge {len(orgs)} test orgs: {e!r}")
    try:
        left = purge(client, os.environ.get("LOOM_CH_DATABASE", "loom"), orgs)
    finally:
        client.close()
    assert not left, f"rows of test orgs survived the purge: {left}"
