"""Fixtures shared by the integration and analyzer suites.

They need a control plane at $LOOM_BASE_URL (tests that ask for it skip
without one), its ClickHouse at $LOOM_CH_* (default localhost:8123), and the
migrations applied (`make migrate`). The unit suite shadows both autouse
fixtures here (tests/unit/conftest.py) and needs none of this.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

from tests import _shards

# The SDK and server import from the source tree, not an installed copy.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "sdk" / "python"))
sys.path.insert(0, str(ROOT))


def _integration_env_path() -> Path | None:
    """The dotenv that server processes under this test session use.

    An explicit empty LOOM_ENV_FILE disables dotenv loading. A nonempty value
    must win over the checkout's .env so an isolated integration stack cannot
    accidentally inherit another deployment's provider or signing settings.
    """
    configured = os.environ.get("LOOM_ENV_FILE")
    if configured is not None:
        return Path(configured) if configured else None
    return ROOT / ".env"


def _clickhouse_target() -> str | None:
    """LOOM_CH_HOST from the environment, else from the repo `.env` the
    session will load; None when neither sets it."""
    if os.environ.get("LOOM_CH_HOST"):
        return os.environ["LOOM_CH_HOST"]
    env_path = _integration_env_path()
    if env_path is not None and env_path.exists():
        try:
            from dotenv import dotenv_values
        except ImportError:
            return None
        return dotenv_values(env_path).get("LOOM_CH_HOST") or None
    return None


def pytest_ignore_collect(collection_path: Path, config: pytest.Config) -> bool | None:
    """LOOM_TEST_SHARD=k/n: collect only the test files of the k-th part (`_shards`)."""
    return _shards.ignored(collection_path, config)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Integration tests talk to ClickHouse, through the server or their own
    client, and the server's default target is localhost:8123. Without an
    explicit LOOM_CH_HOST they skip, so an unconfigured run never writes into
    whatever ClickHouse happens to listen there (a developer's own stack).
    Under LOOM_TEST_SHARD it first drops the other parts' items (`_shards`)."""
    _shards.deselect(config, items)
    if _clickhouse_target():
        return
    skip = pytest.mark.skip(
        reason="integration tests need an explicit ClickHouse target: set LOOM_CH_HOST "
        "(and LOOM_CH_PORT/USER/PASSWORD, LOOM_BASE_URL) for a disposable, migrated stack"
    )
    integration = ROOT / "tests" / "integration"
    for item in items:
        if item.path.is_relative_to(integration):
            item.add_marker(skip)


@pytest.fixture(scope="session", autouse=True)
def _platform_seams_wired() -> None:
    """The platform seams hold their domain pieces, as in every server process."""
    from server.wiring import wire

    wire()


@pytest.fixture(scope="session", autouse=True)
def _load_repo_dotenv():
    """Auto-load repo-root .env into the test process if present.

    A handful of integration tests run code that uses the embedding /
    LLM clients in-process (the embedding_worker's run_pass, parts of
    set_from_messages, etc). If the SERVER (running in docker) has
    OPENAI_API_KEY set via .env but the TEST process doesn't, the
    two processes pick different providers for the same logical
    operation — fake-ngram-1024 in the test, text-embedding-3-small
    in the server — and rows written by one are invisible to the
    other (embedding_model is part of memory_embeddings' ORDER BY).

    Only fills missing variables; existing values stay (so CI's
    "no key" mode keeps working). The variables it added are removed at
    session end.
    """
    added: list[str] = []
    env_path = _integration_env_path()
    if env_path is not None and env_path.exists():
        try:
            from dotenv import dotenv_values

            for k, v in dotenv_values(env_path).items():
                if v is not None and k not in os.environ:
                    os.environ[k] = v
                    added.append(k)
        except ImportError:
            # python-dotenv missing — fall through. The mismatch warning
            # surfaces as test failures rather than a silent skew.
            pass
    yield
    for k in added:
        os.environ.pop(k, None)


@pytest.fixture(autouse=True)
def _seed_test_api_key(_load_repo_dotenv):
    """Mint SDK fallback credentials per test so long suites do not outlive them.

    A caller-supplied LOOM_API_KEY remains in place.
    """
    if os.environ.get("LOOM_API_KEY"):
        yield
        return
    # Defer import so import-time of conftest stays cheap when JWT keys are absent.
    from server.auth.jwt import mint_token

    tok = mint_token(
        subject="test-suite",
        org="*",
        namespaces=["*"],
        roles=["writer"],
        ttl_seconds=3600,
        is_service_account=True,
    )
    os.environ["LOOM_API_KEY"] = tok
    yield
    os.environ.pop("LOOM_API_KEY", None)


@pytest.fixture(scope="session")
def control_plane_url() -> str:
    """$LOOM_BASE_URL after a /v1/health probe; skips when it is unset or unhealthy.

    Sync: a session-scoped async fixture would bind its awaitables to a loop
    function-scoped tests do not share.
    """
    url = os.environ.get("LOOM_BASE_URL")
    if not url:
        pytest.skip(
            "Set LOOM_BASE_URL to point at a running Control Plane "
            "(e.g. http://localhost:7777) and apply migrations with `make migrate`."
        )
    try:
        with httpx.Client(base_url=url, timeout=2.0) as c:
            r = c.get("/v1/health")
            assert r.status_code == 200, f"/v1/health returned {r.status_code}"
    except Exception as e:  # noqa: BLE001 — any health-probe failure means skip, not error
        pytest.skip(f"Control Plane at {url} not responding: {e}")
    return url


@pytest.fixture
def require_real_embedder(control_plane_url: str):
    """Skip search-quality tests when the server runs the fake embedder.

    The fake-ngram embedder is documented to mis-rank every search, so
    ranking-threshold assertions (entity-boost beating a cosine winner,
    procedure.match surfacing a hit) structurally can't hold on it. They
    exercise quality only against a real embedder — CI with a key, or a
    local real-embedder stack. Authoritative: reads the server's own
    /v1/health.fake_embedding, not a test-process env guess.
    """
    with httpx.Client(base_url=control_plane_url, timeout=2.0) as c:
        health = c.get("/v1/health").json()
    if health.get("fake_embedding"):
        pytest.skip("search-quality test needs a real embedder (server is on the fake embedder)")


@pytest.fixture
def pause_loom_memory_merges(ch_client):
    """Pause background merges on loom.memory for the test's duration.

    loom.memory is ReplacingMergeTree(version) keyed by identity+memory_key —
    version is NOT in the sort key, so a merge collapses every version of a key
    to the head. Tests that assert on the full version HISTORY (a read; no
    mutation) otherwise race the merge and flake. Pausing holds the versions in
    place; restarts even on failure. (Not usable for redact, which is an
    ALTER-UPDATE mutation that STOP MERGES also blocks — that test is xfail.)
    """
    ch_client.command("SYSTEM STOP MERGES loom.memory")
    try:
        yield
    finally:
        ch_client.command("SYSTEM START MERGES loom.memory")


@pytest.fixture
def ch_client():
    """Sync client for the ClickHouse the server under test uses ($LOOM_CH_*).

    Tests that assert on rows the server wrote use this, not a hard-coded
    localhost client, or a Cloud-backed run reads an empty local ClickHouse.
    Skips when LOOM_CH_HOST is unset.
    """
    import clickhouse_connect

    # No default host: an unconfigured run must not write into whatever
    # ClickHouse happens to listen on localhost:8123.
    host = os.environ.get("LOOM_CH_HOST")
    if not host:
        pytest.skip(
            "Set LOOM_CH_HOST (and LOOM_CH_PORT/USER/PASSWORD) to the ClickHouse "
            "the control plane under test uses."
        )
    port = int(os.environ.get("LOOM_CH_PORT", "8123"))
    secure = port == 8443 or os.environ.get("LOOM_CH_SECURE", "").lower() == "true"
    client = clickhouse_connect.get_client(
        host=host,
        port=port,
        username=os.environ.get("LOOM_CH_USER", "loom"),
        password=os.environ.get("LOOM_CH_PASSWORD", "loomdev"),
        database=os.environ.get("LOOM_CH_DATABASE", "loom"),
        secure=secure,
        # Tests assert on rows immediately after insert. CH Cloud's
        # `async_insert=1` default buffers them server-side for up to 1s.
        # Force synchronous write semantics for fixture-managed inserts so
        # tests don't have to bake in arbitrary `sleep(2)` after every
        # INSERT. The Loom server's CH client has the same override
        # applied in server/core/ch.py.
        # `insert_deduplicate=0`: SharedMergeTree drops identical-block
        # inserts as "duplicates" by default — but for event-shaped tables
        # two callers writing the same row is two events, not one.
        settings={
            "async_insert": 0,
            "wait_for_async_insert": 1,
            "insert_deduplicate": 0,
        },
    )
    yield client
    client.close()


@pytest_asyncio.fixture
async def agent(control_plane_url: str):
    """A Loom Agent in a unique namespace, JWT-authenticated.

    Mints a writer-scoped token on the fly so the suite runs against a
    server with `LOOM_AUTH_REQUIRED=true`. Tests don't need to know about
    auth — they just use `agent` as before.
    """
    from loom import Agent

    from server.auth.jwt import mint_token

    ns = f"test-{os.urandom(4).hex()}"
    tok = os.environ.get("LOOM_API_KEY") or mint_token(
        subject="test-suite",
        org="test-org",
        namespaces=["*"],
        roles=["writer"],
        ttl_seconds=600,
    )
    async with Agent(
        org="test-org",
        namespace=ns,
        agent="test-agent",
        base_url=control_plane_url,
        api_key=tok,
    ) as a:
        yield a


def pytest_sessionstart(session: pytest.Session) -> None:
    """A test process mints its tokens itself without reading the server's
    keyring: it signs as kid `default`, which the server under test verifies."""
    from server.auth import keyring

    keyring.sign_with_root_only()


@pytest.fixture
def mint_test_token():
    """A callable minting a service-account JWT for the test's own org, for
    tests that construct `Agent(...)` with their own identity:

        tok = mint_test_token(org="my-test-org", namespaces=["*"])
        async with Agent(org="my-test-org", ..., api_key=tok) as a: ...
    """
    from server.auth.jwt import mint_token

    def _mint(
        *,
        org: str,
        namespaces: list[str] | None = None,
        roles: list[str] | None = None,
        ttl_seconds: int = 600,
        subject: str = "test-suite",
        read_user_id: str | None = None,
    ) -> str:
        return mint_token(
            subject=subject,
            org=org,
            namespaces=namespaces or ["*"],
            roles=roles or ["writer"],
            ttl_seconds=ttl_seconds,
            is_service_account=True,
            read_user_id=read_user_id,
        )

    return _mint
