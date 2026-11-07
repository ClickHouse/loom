"""Unit-suite fixtures: process state a test changes is state it puts back.

The unit job runs under pytest-randomly, so every test must
be able to run after any other. Two kinds of process-global state leaked
under file order and only showed up once the order moved: attributes set
directly on `server.settings.settings`, and the auth principal left in
`current_auth_var` by a sync test. The fixtures here make the rule
mechanical: change settings through `settings_override` (or
`monkeypatch.setattr(settings, ...)`), and the autouse guards fail the test
that leaves either behind, naming what drifted.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import socket
import sys
import threading
import time
import types
import unittest.mock
from collections import OrderedDict
from collections.abc import Callable, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import dns.asyncresolver
import pytest

# The unit suite runs as CI does: no dotenv, no LOOM_* or provider keys from
# the developer's shell. Set before `server.settings` builds its singleton.
# LOOM_UPDATE_* are the snapshot tests' regenerate switches and LOOM_TEST_* other
# test switches (LOOM_TEST_REQUIRE_SANDBOX), not settings.
_TEST_SWITCH_PREFIX = ("LOOM_UPDATE_", "LOOM_TEST_")
for _name in [
    n for n in os.environ if n.startswith("LOOM_") and not n.startswith(_TEST_SWITCH_PREFIX)
] + [
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
]:
    os.environ.pop(_name, None)
os.environ["LOOM_ENV_FILE"] = ""

from server.auth.context import current_auth_var  # noqa: E402
from server.code.app import common as code_common  # noqa: E402
from server.settings import settings  # noqa: E402

_REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def stub_model_command(monkeypatch):
    """Isolate native CLI business/connection tests from provider policy/CH.

    The real admission lifecycle is tested separately in test_provider_command, the
    authority check in test_cli_run_connected. These tests deliberately supply clients
    without configuration tables.
    """
    from server.core import pg_profile
    from server.insights.app import provider_command

    @asynccontextmanager
    async def command():
        yield

    async def checked(_ch):
        return None

    monkeypatch.setattr(provider_command, "model_command", command)
    monkeypatch.setattr(pg_profile, "check_configured", checked)


class FakeAuthAttempts:
    """loom.auth_attempts in memory: `insert` appends a row stamped `now`,
    `query_one` counts a kind's rows for a key within the window."""

    def __init__(self) -> None:
        self.now = time.time()
        self.rows: list[dict[str, Any]] = []

    async def insert(self, table: str, rows: list[dict[str, Any]], settings: Any = None) -> None:
        assert table == "loom.auth_attempts"
        self.settings = settings
        # A row's own `at` (on the fake clock) when it names one, else `now`.
        self.rows += [{**r, "at": _epoch(r["at"]) if "at" in r else self.now} for r in rows]

    async def query_one(self, sql: str, params: Any = None) -> dict[str, Any]:
        assert "FROM loom.auth_attempts" in sql
        p = params or {}
        return {"n": self._count(p["kind"], p["key"], p["window"])}

    async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
        assert "FROM loom.auth_attempts" in sql and "GROUP BY kind, key_sha256" in sql
        p = params or {}
        return [
            # bytes, as the driver returns the FixedString column
            {"kind": kind, "key_sha256": key.encode(), "n": self._count(kind, key, p["window"])}
            for kind, key in set(p["pairs"])
        ]

    def _count(self, kind: str, key: str, window: int) -> int:
        return sum(
            r["kind"] == kind and r["key_sha256"] == key and r["at"] > self.now - window
            for r in self.rows
        )


def _epoch(at: Any) -> float:
    """A naive UTC datetime (what the server stores) as epoch seconds."""
    from datetime import UTC

    return float(at.replace(tzinfo=UTC).timestamp())


@pytest.fixture
def auth_attempts(monkeypatch: pytest.MonkeyPatch) -> FakeAuthAttempts:
    """Every `server.auth.attempts.Attempts` counts in one fresh in-memory table."""
    from datetime import UTC, datetime

    from server.auth import attempts, keyring

    fake = FakeAuthAttempts()
    monkeypatch.setattr(attempts, "_default_client", lambda: fake)
    monkeypatch.setattr(attempts, "_clock", lambda: fake.now)
    monkeypatch.setattr(
        keyring, "_now", lambda: datetime.fromtimestamp(fake.now, UTC).replace(tzinfo=None)
    )
    monkeypatch.setattr(attempts, "_known_over", OrderedDict())
    return fake


@pytest.fixture(scope="session", autouse=True)
def _load_repo_dotenv() -> None:
    """Shadows the integration suite's fixture of the same name: a unit test
    never reads the repo's `.env`."""


@pytest.fixture(scope="session", autouse=True)
def _seed_test_api_key() -> None:
    """Shadows the integration suite's fixture of the same name: no unit test
    runs with a minted wildcard-org `LOOM_API_KEY` in its environment."""


@pytest.fixture
def mirror_here(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every configured mirror counts as present on this host: for tests whose git
    is a fake (`gitrepo` patched) and whose git_dir does not exist."""
    from server.code.app.index import mirrors

    monkeypatch.setattr(mirrors, "_present", lambda cfg: True)


@pytest.fixture
def settings_override() -> Iterator[Callable[..., None]]:
    """Set `settings.<field> = value` for one test and restore on teardown.

        def test_x(settings_override):
            settings_override(memory_search_min_similarity=0.3)

    Unknown field names are refused so a renamed setting fails loudly here
    rather than silently testing nothing.
    """
    saved: dict[str, Any] = {}

    def _override(**values: Any) -> None:
        for name, value in values.items():
            if name not in type(settings).model_fields:
                raise AttributeError(f"settings has no field {name!r}")
            if name not in saved:
                saved[name] = getattr(settings, name)
            setattr(settings, name, value)

    yield _override
    for name, value in saved.items():
        setattr(settings, name, value)


@pytest.fixture(autouse=True)
def _settings_left_as_found() -> Iterator[None]:
    """Fail a test that leaves a settings field changed.

    Snapshot + compare is ~0.04 ms per test (841 fields). Fixture teardown
    runs after `monkeypatch` and `settings_override` have restored theirs, so
    only a bare `settings.x = ...` with no restore trips it. The drift is put
    back before failing so one leak does not cascade into the tests after it.
    """
    before = dict(settings.__dict__)
    yield
    after = settings.__dict__
    drifted = sorted(k for k in before if k in after and after[k] != before[k])
    for k in drifted:
        setattr(settings, k, before[k])
    if drifted:
        pytest.fail(
            "test left settings changed: "
            + ", ".join(drifted)
            + " — use the settings_override fixture or monkeypatch.setattr(settings, ...)"
        )


# Test files whose own code leaks process state today. Their leak is still put
# back after each test, but the test does not fail. Delete a line once the
# file restores what it changes.
_KNOWN_PROCESS_STATE_LEAKS: frozenset[str] = frozenset()
# pytest rewrites this for every phase; it is not a test's state.
_PYTEST_ENV = frozenset({"PYTEST_CURRENT_TEST"})
# The session loop's default executor threads, and the document bucket's own pool
# (`server.core.object_store`), outlive every test by design.
_SHARED_THREAD_PREFIXES = ("asyncio_", "loom-object-store", "loom-blacklist")
_THREAD_JOIN_SECONDS = 2.0


def _restore_env(before: dict[str, str]) -> list[str]:
    changed = sorted(
        k
        for k in (before.keys() | os.environ.keys()) - _PYTEST_ENV
        if before.get(k) != os.environ.get(k)
    )
    for k in changed:
        if k in before:
            os.environ[k] = before[k]
        else:
            os.environ.pop(k, None)
    return changed


def _reset_root_logger(level: int, handlers: list[logging.Handler], latch: bool) -> None:
    """Put back the root logger and the CLI's logging latch.

    `server.cli.code_run._setup_cli_logging` configures the root logger once per
    process (a stderr handler, level INFO) behind a latch. That is product
    behaviour a CLI test triggers, not a leak to fail, but a configured root
    logger would follow every later test. The reset lets the next CLI test
    configure logging against its own stderr."""
    root = logging.getLogger()
    root.handlers[:] = handlers
    root.setLevel(level)
    cli = sys.modules.get("server.cli.code_run")
    if cli is not None:
        cli._LOGGING_CONFIGURED = latch


def _leaked_threads(before: set[threading.Thread]) -> list[str]:
    """Non-daemon threads the test started that are still running after a
    bounded join. Daemon threads die with the process; the session loop's
    executor threads and the bucket pool are shared by design."""
    leaked = [
        t
        for t in threading.enumerate()
        if t not in before and not t.daemon and not t.name.startswith(_SHARED_THREAD_PREFIXES)
    ]
    deadline = time.monotonic() + _THREAD_JOIN_SECONDS
    for t in leaked:
        t.join(timeout=max(0.0, deadline - time.monotonic()))
    return [t.name for t in leaked if t.is_alive()]


# Unit tests never reach the network. A connect or a datagram to a non-loopback address,
# a getaddrinfo lookup of anything but a local name, and any dnspython lookup at the
# system's nameservers (which may be a loopback stub that forwards) are refused and
# recorded, and the test fails at teardown naming the host: the code under test may
# swallow the refusal (as "tracker unavailable", say), so the raise alone proves nothing.
_LOCAL_NAMES = frozenset({"", "localhost", "localhost.localdomain", "testserver", "test"})
_network_attempts: list[str] = []
_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex
_real_sendto = socket.socket.sendto
_real_getaddrinfo = socket.getaddrinfo
_real_dns_resolve = dns.asyncresolver.Resolver.resolve


def _is_local(host: object) -> bool:
    if not isinstance(host, str) or host in _LOCAL_NAMES:
        return True
    try:
        addr = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return False
    return addr.is_loopback or addr.is_unspecified


def _refuse_remote(sock: socket.socket, address: Any) -> None:
    if sock.family == getattr(socket, "AF_UNIX", None):
        return
    host = address[0] if isinstance(address, tuple) else address
    if not _is_local(host):
        _network_attempts.append(f"connect {host}")
        raise OSError(f"unit tests may not reach the network (connect {host})")


def _guarded_connect(self: socket.socket, address: Any) -> None:
    _refuse_remote(self, address)
    _real_connect(self, address)


def _guarded_connect_ex(self: socket.socket, address: Any) -> int:
    _refuse_remote(self, address)
    return _real_connect_ex(self, address)


def _guarded_sendto(self: socket.socket, data: Any, *args: Any) -> int:
    _refuse_remote(self, args[-1])  # (data, address) or (data, flags, address)
    return _real_sendto(self, data, *args)


def _guarded_getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
    name = host.decode() if isinstance(host, bytes) else host
    if not _is_local(name):
        _network_attempts.append(f"resolve {name}")
        raise socket.gaierror(socket.EAI_NONAME, f"unit tests may not resolve {name}")
    return _real_getaddrinfo(host, *args, **kwargs)


async def _guarded_dns_resolve(
    self: dns.asyncresolver.Resolver, qname: Any, *a: Any, **kw: Any
) -> Any:
    # Any name, `localhost` too: dnspython reads no /etc/hosts and would ask the system's
    # nameserver. A test's own nameserver is on another port.
    if self.port == 53:
        name = str(qname).rstrip(".")
        _network_attempts.append(f"resolve {name}")
        raise OSError(f"unit tests may not resolve {name}")
    return await _real_dns_resolve(self, qname, *a, **kw)


socket.socket.connect = _guarded_connect  # type: ignore[method-assign]
socket.socket.connect_ex = _guarded_connect_ex  # type: ignore[method-assign]
socket.socket.sendto = _guarded_sendto  # type: ignore[method-assign,assignment]
socket.getaddrinfo = _guarded_getaddrinfo
dns.asyncresolver.Resolver.resolve = _guarded_dns_resolve  # type: ignore[method-assign]


@pytest.fixture(autouse=True)
def _no_repo_job_sandbox() -> Iterator[None]:
    """Repository jobs run as plain children in unit tests, whether or not this
    host can run bubblewrap: the tests fake the child process. The tests of the
    real sandbox (test_repo_job_sandbox_hostile.py) switch it back on."""
    from server.code.app.index import sandbox

    before = sandbox._available
    sandbox._available = False
    yield
    sandbox._available = before


@pytest.fixture(autouse=True)
def _no_copy_head_check(request: pytest.FixtureRequest) -> Iterator[None]:
    """A vector copy re-reads its keys' heads after its insert
    (`retire_copies_under_unembedded_heads`); unit fakes answer statements by
    count and shape, so that read (and `fence_high`'s re-check of the keys' live
    vectors, and the embed worker's pre-embed head read) is off unless a test is
    marked `copy_head_check`."""
    if request.node.get_closest_marker("copy_head_check"):
        yield
        return
    import server.memory.store as memory_repo
    from server.memory.app import high_fence
    from server.memory.store import embeddings, entities
    from server.memory.workers import embedding as embedding_worker

    async def _skip(*_a: object, **_k: object) -> int:
        return 0

    async def _skip_recheck(*_a: object, **_k: object) -> None:
        return None

    async def _all_served(rows: list[object], *_a: object) -> tuple[list[object], list[object]]:
        return rows, []

    targets = [
        (embeddings, "retire_copies_under_unembedded_heads", _skip),
        (entities, "retire_copies_under_unembedded_heads", _skip),
        (memory_repo, "retire_copies_under_unembedded_heads", _skip),
        (high_fence, "recheck_live_vectors", _skip_recheck),
        (embedding_worker, "_stubs_still_served", _all_served),
    ]
    before = [(mod, name, getattr(mod, name)) for mod, name, _ in targets]
    for mod, name, stub in targets:
        setattr(mod, name, stub)
    yield
    for mod, name, original in before:
        setattr(mod, name, original)


@pytest.fixture(autouse=True)
def _no_grant_rows(request: pytest.FixtureRequest) -> Iterator[None]:
    """The namespace gate caps a token's claim at the grant its subject holds
    there (`authz.claim_cap`), read through the grant cache. Unit fakes answer
    statements by shape, so the cache reads no grant rows (every claim stands)
    unless a test is marked `claim_cap`."""
    from server.auth import sharing
    from server.core import grant_cache

    grant_cache.clear()
    before = grant_cache._read
    before_tree, before_groups = sharing.load_tree, sharing._load_groups
    if not request.node.get_closest_marker("claim_cap"):

        async def _none(org: str, subject: str) -> grant_cache.GrantMap:
            return grant_cache.GrantMap(ranks={})

        grant_cache._read = _none  # type: ignore[assignment]

    # Likewise an org has no collections and no groups unless a test says so.
    async def _no_tree(org: str) -> sharing.Tree:
        return sharing.Tree()

    async def _no_groups(org: str) -> dict[str, list[str]]:
        return {}

    sharing.load_tree = _no_tree  # type: ignore[assignment]
    sharing._load_groups = _no_groups  # type: ignore[assignment]
    yield
    grant_cache._read = before  # type: ignore[assignment]
    sharing.load_tree, sharing._load_groups = before_tree, before_groups  # type: ignore[assignment]
    grant_cache.clear()


@pytest.fixture
def subjects_not_revoked(monkeypatch: pytest.MonkeyPatch) -> None:
    """Token-minting paths read the subject's revocations from ClickHouse;
    none is revoked here."""
    import server.auth.revocation as revocation

    async def _not_revoked(subjects: list[str], *, issued_at: int) -> bool:
        return False

    monkeypatch.setattr(revocation, "subjects_revoked", _not_revoked)


@pytest.fixture(autouse=True)
def _grant_claims_in_memory() -> Iterator[None]:
    """Grant changes claim in process memory: the unit fakes have no lease table.
    A test that drives the real claim patches `grant_claims.take` itself. No
    `monkeypatch` here: requesting it would set it up ahead of the settings guard."""
    from server.auth import grant_claims
    from tests.unit.claims_fake import MemoryClaims

    before = grant_claims.take
    grant_claims.take = MemoryClaims().take  # type: ignore[assignment]
    yield
    grant_claims.take = before


@pytest.fixture(autouse=True)
def _no_parent_revoked_in_the_store() -> Iterator[None]:
    """A mint reads the parent's revocations from ClickHouse; unit fakes have no
    such table. test_delegation.py drives the real read. No `monkeypatch`, as above."""
    from server.auth import delegation

    async def not_revoked(*_a: Any, **_k: Any) -> None:
        return None

    before = delegation._refuse_a_revoked_parent
    delegation._refuse_a_revoked_parent = not_revoked  # type: ignore[assignment]
    yield
    delegation._refuse_a_revoked_parent = before


@pytest.fixture(autouse=True)
def _nobody_deactivated_over_scim() -> Iterator[None]:
    """An SSO sign-in reads whether SCIM deactivated the email; unit fakes have
    no such table. test_scim.py drives the real read. No `monkeypatch`, as above."""
    from server.auth import scim

    async def not_deactivated(email: str) -> bool:
        return False

    before = scim.deactivated
    scim.deactivated = not_deactivated  # type: ignore[assignment]
    yield
    scim.deactivated = before


@pytest.fixture(autouse=True)
def _no_memory_webhooks() -> Iterator[None]:
    """A memory write reads the webhook registry once per refresh interval, and
    the unit fakes have no such table: every test starts with an empty copy that
    is never read again. test_memory_webhooks.py sets the copy itself. No
    `monkeypatch`: requesting it would set it up ahead of the settings guard."""
    from server.memory.app import webhooks

    webhooks._registry._reset_for_tests({}, read_age_s=0.0, due_in_s=float("inf"))
    yield
    webhooks._registry._reset_for_tests({}, read_age_s=None, due_in_s=0.0)


@pytest.fixture(autouse=True)
def _not_draining() -> Iterator[None]:
    """A test that ran an app's lifespan leaves the process marked draining,
    which the workers lease reads: every test starts serving."""
    from server.core import readiness

    readiness._draining = False
    yield
    readiness._draining = False


@pytest.fixture(autouse=True)
def _no_quota_key() -> Iterator[None]:
    """A test that verifies a bearer sets the ClickHouse quota key in this
    context; clear it so the next test's queries carry none."""
    from server.core import ch

    ch.quota_key_var.set("")
    yield
    ch.quota_key_var.set("")


@pytest.fixture(autouse=True)
def _network_left_untouched() -> Iterator[None]:
    """Fail a test that tried to connect to or resolve a remote host."""
    _network_attempts.clear()
    yield
    if _network_attempts:
        tried = sorted(set(_network_attempts))
        _network_attempts.clear()
        pytest.fail("test tried to reach the network: " + ", ".join(tried))


@pytest.fixture(autouse=True)
def _process_state_left_as_found(request: pytest.FixtureRequest) -> Iterator[None]:
    """Fail a test that leaves the environment, the working directory,
    `logging.disable` or a running non-daemon thread behind. Use
    `monkeypatch.setenv/delenv/chdir` and join what you start. The drift is put
    back first, so one leak does not cascade into later tests. The root logger
    is reset without failing (see `_reset_root_logger`).

    `sys.path` is not checked: bench modules insert their directory when first
    imported, which would blame whichever test imported them first.
    """
    env = dict(os.environ)
    cwd = os.getcwd()
    root = logging.getLogger()
    cli = sys.modules.get("server.cli.code_run")
    log_state = (root.level, list(root.handlers), bool(cli and cli._LOGGING_CONFIGURED))
    disabled = logging.root.manager.disable
    threads = set(threading.enumerate())
    yield
    _reset_root_logger(*log_state)
    drift = []
    changed_env = _restore_env(env)
    if changed_env:
        drift.append("environment " + ", ".join(changed_env))
    if os.getcwd() != cwd:
        drift.append(f"working directory {os.getcwd()}")
        os.chdir(cwd)
    if logging.root.manager.disable != disabled:
        drift.append(f"logging.disable({logging.root.manager.disable})")
        logging.disable(disabled)
    alive = _leaked_threads(threads)
    if alive:
        drift.append("running threads " + ", ".join(alive))
    if drift and request.node.path.relative_to(_REPO).as_posix() not in _KNOWN_PROCESS_STATE_LEAKS:
        pytest.fail("test left process state changed: " + "; ".join(drift))


def _is_test_double(obj: object) -> bool:
    """A mock (an autospec'd function included), an instance of a class defined under
    tests/, or a function or class defined under tests/ (a lambda included). An
    instance's `__module__` is its class's."""
    if isinstance(obj, types.FunctionType) and isinstance(
        obj.__dict__.get("mock"), unittest.mock.NonCallableMock
    ):
        return True  # create_autospec(fn) returns a plain function carrying its mock
    module = getattr(obj, "__module__", None)
    return isinstance(module, str) and module.startswith(("tests.", "test_", "unittest.mock"))


@pytest.fixture(autouse=True)
def _provider_clients_left_as_found() -> Iterator[None]:
    """Put back the LLM and embedder singletons, and empty the embedding cache,
    after every test. Fail a test that left its own fake cached.

    `get_llm_client()` and `get_embedder()` cache one client per process. A test
    that assigns a fake to the cache instead of patching the getter serves that
    fake to every later test, and the embedding LRU would answer a later test's
    texts with vectors an earlier test's embedder produced. A client the
    getter built is product state and is dropped quietly; a test double left
    behind fails the test that left it.
    """
    from server.embedding import _cache as embed_cache
    from server.embedding import embedder
    from server.llm import client as llm_client

    llm_before = (
        llm_client._cached,
        dict(llm_client._override_cache),
        dict(llm_client._wrapped_cache),
    )
    emb_before = (embedder._cached, dict(embedder._secondary))
    yield
    left = [
        name
        for name, now, before in (
            ("server.llm.client._cached", llm_client._cached, llm_before[0]),
            ("server.embedding.embedder._cached", embedder._cached, emb_before[0]),
        )
        if now is not before and _is_test_double(now)
    ]
    llm_client._cached = llm_before[0]
    llm_client._override_cache = llm_before[1]
    llm_client._wrapped_cache = llm_before[2]
    embedder._cached = emb_before[0]
    embedder._secondary.clear()
    embedder._secondary.update(emb_before[1])
    embed_cache._cache._store.clear()
    embed_cache._INFLIGHT.clear()
    if left:
        pytest.fail(
            "test left a test double in a provider cache: "
            + ", ".join(left)
            + " — patch get_llm_client / get_embedder with monkeypatch instead"
        )


def _is_repo_file(path: str, *, tests: bool) -> bool:
    """Whether `path` is a repository file outside a dot directory such as .venv, and,
    unless `tests`, outside tests/ (whose fakes are the doubles)."""
    resolved = Path(path).resolve()
    if not resolved.is_relative_to(_REPO):
        return False
    top = resolved.relative_to(_REPO).parts[0]
    return not top.startswith(".") and (tests or top != "tests")


def _source_modules_since(modules_before: set[str], *, tests: bool = False) -> list[str]:
    """The repository modules (tests/ helpers too when `tests`) imported since
    `modules_before`, skipping entries without a file: blocked imports (`None`), builtins
    and namespace packages."""
    return [
        name
        for name in sys.modules.keys() - modules_before
        if (path := getattr(sys.modules.get(name), "__file__", None))
        and _is_repo_file(path, tests=tests)
    ]


def _double_bound_at_import(modules_before: set[str]) -> bool:
    """Whether a source module imported since `modules_before` holds a test double.

    A module that runs `from pkg import Name` binds whatever `pkg.Name` is at that
    moment. When its first import in the process happens while a test patches
    `pkg.Name`, the module keeps the double after the patch ends, and every later
    test that uses it sees the double (an `isinstance(x, Name)` raises TypeError).
    """
    return any(
        _is_test_double(value)
        for name in _source_modules_since(modules_before)
        for value in vars(sys.modules[name]).values()
    )


@pytest.fixture(autouse=True)
def _test_double_bound_at_import_dropped() -> Iterator[None]:
    """After a test that left a test double bound in a source module it first imported, drop
    every repository module first imported during the test, tests/ helpers included (one may
    have bound a name from the poisoned module), from `sys.modules` and from its package, so
    the next import binds the real names. Teardown runs after the test's own patches are
    undone. A module first imported in a module-, class- or session-scoped fixture is
    outside the window.
    """
    before = set(sys.modules)
    yield
    if not _double_bound_at_import(before):
        return
    for name in _source_modules_since(before, tests=True):
        module = sys.modules.pop(name)
        # `from package import leaf` returns the package attribute when it is set,
        # without importing again.
        package, _, leaf = name.rpartition(".")
        parent = sys.modules.get(package)
        if parent is not None and getattr(parent, leaf, None) is module:
            delattr(parent, leaf)


@pytest.fixture(autouse=True)
def _code_caches_cold() -> Iterator[None]:
    """Every code-plane process cache (`server.code.app.ttl_cache.TtlCache`) empty before
    and after each test: a cache a test filled from one fake repo would answer the next
    test's reads (the ErrorCodes table of a citation, a namespace's probe verdicts, a
    repository map), an order-dependent failure."""
    from server.code.app.ttl_cache import clear_all

    clear_all()
    yield
    clear_all()


@pytest.fixture(autouse=True)
def _corpus_stats_cache_left_empty() -> Iterator[None]:
    """The code-search corpus-statistics cache is module state behind a TTL
    (`code_index.corpus_stats`, keyed by org/namespace/term). Eighteen unit
    files count the `code_corpus_stats` reads their fake repos serve; a
    cache warmed by one would answer the next test's tokens without a read.
    Empty before and after every test."""
    from server.code.app.index import corpus_stats

    corpus_stats.reset_for_tests()
    yield
    corpus_stats.reset_for_tests()


@pytest.fixture(autouse=True)
def _auth_context_left_as_found() -> Iterator[None]:
    """Fail a test that leaves a principal in `current_auth_var`.

    A sync test calling `set_current_auth(...)` writes the main thread's
    context, which every later test inherits: `_actor()` in the lock handler
    then reports that principal instead of the requested one. The value is
    reset before failing so the leak stops at the test that caused it.
    """
    token = current_auth_var.set(None)
    yield
    leaked = current_auth_var.get()
    current_auth_var.reset(token)
    if leaked is not None:
        pytest.fail(
            f"test left current_auth_var set to {leaked.subject!r}@{leaked.org!r}: "
            "reset it with set_current_auth(None) in a finally or a fixture"
        )


@pytest.fixture(autouse=True)
def _query_tag_left_as_found() -> Iterator[None]:
    """Restore `ch.query_tag_var` after every test.

    A sync test that runs a worker pass calls `set_worker_identity`, which tags
    the main thread's context `worker.<name>`; every later test inherits it, and
    the CLI-dispatch test that checks the tag it restores sees the leak (seen
    under random order in CI). A tag is attribution only, so it is reset here
    rather than failing the leaking test.
    """
    from server.core.ch import query_tag_var

    token = query_tag_var.set("")
    yield
    query_tag_var.reset(token)


@pytest.fixture(autouse=True)
def _cost_accumulator_left_as_found() -> Iterator[None]:
    """Start every test with no cost accumulator, and put back what was there.

    `cost_accumulator.begin_op()` binds the op's tally to the current context, and a
    sync test that calls it binds the main thread's: every later test in the process
    then prices its rows into that tally, so a path that runs only inside a request
    ran (and was covered) only after such a test. A tally is accounting only, so it
    is reset here rather than failing the test that bound it.
    """
    from server.observability.cost_accumulator import _current

    token = _current.set(None)
    yield
    _current.reset(token)


@pytest.fixture(autouse=True)
def _trace_tasks_left_as_found() -> Iterator[None]:
    """Drain `code_common._TRACE_TASKS` after every test.

    The set holds the fire-and-forget `code_search_trace` writes of the
    running loop. A test that schedules one inside `asyncio.run()` and does
    not await it leaves a task bound to a loop that is now closed; the next
    test to `gather(*_TRACE_TASKS)` on its own loop then fails with "The future
    belongs to a different loop" — seen under pytest-randomly seed
    349018290131. `schedule_code_trace` prunes such tasks itself; this keeps a
    test that only reads the set from inheriting another test's leftovers.
    """
    yield
    code_common._TRACE_TASKS.clear()


@pytest.fixture(autouse=True)
def _worker_cursors_left_empty() -> Iterator[None]:
    """A worker's next page must not depend on a previous test's pass."""
    if module := sys.modules.get("server.memory.workers.projection_repair"):
        module._after = None
    yield
    if module := sys.modules.get("server.memory.workers.projection_repair"):
        module._after = None


@pytest.fixture(autouse=True)
def _model_call_holds_left_empty() -> Iterator[None]:
    """Leave no job's held work, and no gateway hold, to the next test.

    A job that met a held identity (`server.workers.holds`) leaves that work out of
    its next selections, and the gateway holds an identity's job calls
    (`server.llm.gateway`): either, kept, would make a later test's selection or call
    depend on what an earlier test refused.
    """
    yield
    from server.llm import gateway
    from server.workers import holds

    holds._reset_for_tests()
    gateway._held.clear()
    from server.llm import customer

    customer._logged_at.clear()


@pytest.fixture(autouse=True)
def _identity_caches_left_cold() -> Iterator[None]:
    """Leave the search plane's per-namespace caches empty after every test.

    `memory_search._KEY_AXIS_AUTO` remembers the key-reuse probe per
    (org, namespace) for ten minutes, and `_SUMMARY_ENTITY_CACHE` the summary
    vocabulary per (org, namespaces) for one minute. A cold entry costs the
    search one extra `query_all` (the probe), which in a stub client with a
    queued list of returns shifts every later read by one, so whether a test
    ran cold would depend on the order. Every test starts cold, so a test
    that wants no probe says so
    (`memory_search_key_axis="off"` or `key_axis=` on the query) instead of
    inheriting a neighbour's decision.
    """
    yield
    from server.memory.app.retrieval import key_axis, summaries

    key_axis._KEY_AXIS_AUTO.clear()
    summaries._SUMMARY_ENTITY_CACHE.clear()


def _reset_deferred_ledgers() -> None:
    from server.core import namespace_write_marks
    from server.memory.app import drops, graph_write_marks
    from server.memory.store import usage as memory_repo_usage

    memory_repo_usage._reset_for_tests()
    drops._reset_for_tests()
    namespace_write_marks._reset_for_tests()
    graph_write_marks._reset_for_tests()


@pytest.fixture(autouse=True)
async def _tasks_left_as_found() -> Any:
    """Cancel the tasks a test started on the session loop and left pending.

    The unit suite runs every async test on one session loop, so a task a
    test creates and never awaits keeps running into the tests after it —
    and resolves `server.core.ch.get_client()` at that moment, which is then the
    NEXT test's fake client. `memory.ask` starts its knowledge lookup as a
    task at the top of the call and awaits it only on the synthesis path;
    an ask that abstains could leave the lookup pending, and its knowledge-chunk
    SELECT would land in a later test's statement-counting fake. The ask
    cancels the task on its early returns; this guard is the general rule:
    whatever a test leaves pending is cancelled and drained before the next
    test, so a leak stops at the test that caused it instead of failing a
    bystander. Tasks that existed before the test (session fixtures'
    servers, the loop's own) are left alone.

    Each pass first empties the deferred ledgers (usage, drops, namespace and
    graph write marks) and stops their timers and flusher: a flush left armed
    would write into a later test's client and leave a deferred task that
    `drain_task_sets` counts.
    """
    before = set(asyncio.all_tasks())
    yield
    current = asyncio.current_task()

    def pending() -> list[asyncio.Task[Any]]:
        return [t for t in asyncio.all_tasks() - before if t is not current and not t.done()]

    # A cancelled task's done callback can re-arm a timer or spawn a graph-marks
    # end task, so the reset and the sweep repeat. A task still pending after
    # three passes is cancelled too, and fails the test unless something (the
    # ledger reset, for the usage flusher) was already cancelling it.
    unstopped: list[asyncio.Task[Any]] = []
    for last in (False, False, False, True):
        _reset_deferred_ledgers()
        leaked = pending()
        if not leaked:
            return
        if last:
            unstopped = [t for t in leaked if not t.cancelling()]
        for t in leaked:
            t.cancel()
        await asyncio.gather(*leaked, return_exceptions=True)
    _reset_deferred_ledgers()
    if unstopped:
        pytest.fail(f"cancelling the tasks a test left kept starting new ones: {unstopped}")


@pytest.fixture(autouse=True)
def _tracker_contract_not_enforced() -> Iterator[None]:
    """Before the first tracker read of a process the source's
    schema is compared with the record contract through the source's own
    credential. Unit tests fake the upstream one statement at a time, so the
    gate is off here unless a test turns it on (with monkeypatch); its cache
    is empty before and after every test."""
    from server.tracker.app import contract
    from server.tracker.store.source import contract_reports as reports

    contract._enforce = False
    reports.reset_for_tests()
    yield
    contract._enforce = True
    reports.reset_for_tests()


@pytest.fixture(autouse=True)
def _vector_read_state_not_probed() -> Iterator[None]:
    """`CHClient.connect()` probes the vector read state
    (replica check, coded columns, patch parts) and the replicas' versions for
    the condition cache. Unit clients are fakes, so both connect probes are
    replaced by no-ops here and the module state starts
    and ends empty — a test that wants a reading sets it
    (`vector_codes_state.set_coded`, `patch_parts.set_reading`). Set by hand
    for the reason given above."""
    from server.core import ch as ch_mod
    from server.core.vectors import codes_state as vector_codes_state
    from server.core.vectors import patch_parts

    async def _not_probed(_client: Any) -> None:
        return None

    before = ch_mod._probe_vector_state
    guard = ch_mod._turn_off_unsafe_condition_cache
    ch_mod._probe_vector_state = _not_probed  # type: ignore[assignment]
    ch_mod._turn_off_unsafe_condition_cache = _not_probed  # type: ignore[assignment]
    vector_codes_state.reset()
    patch_parts.reset()
    yield
    ch_mod._probe_vector_state = before  # type: ignore[assignment]
    ch_mod._turn_off_unsafe_condition_cache = guard  # type: ignore[assignment]
    vector_codes_state.reset()
    patch_parts.reset()


@pytest.fixture(autouse=True)
def _revocation_snapshot_loaded() -> Iterator[None]:
    """A unit test models a booted process: the revocation snapshot, the
    signing keyring, the model-call switch and the extraction policies have
    been read just now (all empty), and no creator's account state or
    service-account row is cached yet. Tests of the not-yet-loaded state set
    them back to not loaded. Saved and restored by hand, not through
    `monkeypatch`: requesting it here would set it up before
    `_settings_left_as_found` and so tear it down after that check."""
    import server.auth.delegation as delegation
    import server.auth.keyring as keyring
    import server.auth.revocation as rev
    import server.auth.service_accounts as service_accounts
    from tests.unit.switch_state import (
        policies_read_now,
        policies_unread,
        switch_off,
        switch_unread,
    )

    saved = rev._loaded, rev._loaded_at, keyring._view
    rev._loaded, rev._loaded_at = True, rev.time.monotonic()
    keyring._view = keyring._View(read_at=keyring.time.monotonic(), loaded=True)
    switch_off()
    policies_read_now({})
    delegation._account_active.clear()
    service_accounts._creators.clear()
    yield
    rev._loaded, rev._loaded_at, keyring._view = saved
    switch_unread()
    policies_unread()


# The scalar repository the code-plane fixtures model, with its repository-shaped
# settings (prefixes, release-branch globs, positive controls). The settings configure
# none by default, so a test that reads the scalar entry, the default code namespace or
# the repository's own prefixes and controls requests `scalar_code_repo`.
SCALAR_CODE_REPO: dict[str, Any] = {
    "code_org": "clickhouse",
    "code_namespace": "code-clickhouse",
    "code_repo": "ClickHouse/ClickHouse",
    "code_git_dir": "var/code_repos/clickhouse.git",
    "code_git_remote": "https://github.com/ClickHouse/ClickHouse.git",
    "code_git_branch": "master",
    "code_path_prefixes": "src/,base/,programs/,tests/,ci/",
    "code_git_extra_branches": "2[5-9].*",
    "code_summary_churn_prefixes": "src/",
    "code_summary_churn_exclude": "base/,contrib/",
    "code_search_pr_leg_path_prefixes": "src/",
    "code_coverage_control_symbol": "arrayJoin",
    "code_setting_control": "max_threads",
}


@pytest.fixture
def scalar_code_repo(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Configure `SCALAR_CODE_REPO` as the deployment's scalar code entry for one
    test; returns the values set."""
    for name, value in SCALAR_CODE_REPO.items():
        monkeypatch.setattr(settings, name, value)
    return dict(SCALAR_CODE_REPO)


@pytest.fixture(autouse=True)
def _clickhouse_preflight_passes() -> Iterator[None]:
    """Boot and `loom migrate` check a real ClickHouse before they connect
    (`server.core.schema.preflight`). Unit tests run against fakes, so the
    check answers ready; tests/unit/test_ch_preflight.py covers it. Set by
    hand, like the vector probe above, so it is undone after the settings
    guard has looked."""
    from server.core.schema import preflight

    ready = preflight.Server(version="26.5.1.882", keeper=True, lightweight_updates=True)
    before = preflight.require_ready
    preflight.require_ready = lambda **_: ready  # type: ignore[assignment]
    yield
    preflight.require_ready = before  # type: ignore[assignment]


@pytest.fixture(autouse=True)
def _no_revocations_left_behind() -> Iterator[None]:
    """A test's revocations stay in it: the process snapshot and the recent
    local writes a later refresh would lay over its read."""
    from server.auth import revocation

    saved = (revocation._revoked_jtis, revocation._revoked_before, revocation._cut_off)
    revocation._recent.clear()
    yield
    revocation._revoked_jtis, revocation._revoked_before, revocation._cut_off = saved
    revocation._recent.clear()


@pytest.fixture
def no_directory_ties(monkeypatch: pytest.MonkeyPatch) -> None:
    """A revoke reads the spellings the directory ties to its subject (none here),
    whether a verified account holds it (when another spelling is tied), and the
    rows stored under them (each as given, and none revoked)."""

    async def _alone(org: str, subject: str, ch: Any) -> list[str]:
        return [subject]

    async def _as_given(
        org: str, ns: str, spellings: list[str], *, rows: str = "live", **_: Any
    ) -> list[str]:
        return [] if rows == "all" else list(dict.fromkeys(spellings))  # no revoked rows

    async def _tie(org: str, subject: str, **_: Any) -> tuple[list[str], bool]:
        # Through whatever `read_aliases` a test sets: held when it ties another
        # spelling, so a handle that is itself the address is not represented here
        # (test_aliases_verified_email_sql.py runs that case on the real rows).
        from server.auth import aliases

        spellings = await aliases.read_aliases(org, subject, None)
        return spellings, len(spellings) > 1

    monkeypatch.setattr("server.auth.aliases.read_aliases", _alone)
    monkeypatch.setattr("server.auth.aliases.verified_tie", _tie)
    monkeypatch.setattr("server.identity.store.grants.stored_spellings", _as_given)


@pytest.fixture
def off_code_repos(monkeypatch: pytest.MonkeyPatch) -> None:
    """An admin revoke reads neither the self-service registry nor a directory
    row to learn whether its namespace is a code index: it is not one."""

    async def _no_repo(*_a: object, **_k: object) -> None:
        return None

    monkeypatch.setattr("server.code.app.index.maintainers.read_repo", _no_repo)
