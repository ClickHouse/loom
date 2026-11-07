"""A page from one build finds its bundle on a replica of another: /assets/*
falls back to the files every replica publishes into ClickHouse."""

from __future__ import annotations

import hashlib
import os
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from server.core.ch import current_query_tag
from server.settings import settings

pytestmark = pytest.mark.skipif(
    os.environ.get("LOOM_SKIP_APP_IMPORT") == "1", reason="app import disabled"
)


class _FakeCH:
    def __init__(self, rows: dict[str, dict[str, Any]] | None = None, fail: bool = False) -> None:
        self.rows = rows or {}
        self.fail = fail
        self.inserted: list[dict[str, Any]] = []
        self.reads = 0

    async def raw_bytes(self, sql: str, params: dict[str, Any]) -> bytes:
        self.reads += 1
        self.tag = current_query_tag()
        if self.fail:
            raise RuntimeError("Table loom.frontend_assets does not exist")
        return self.rows.get(params["p"], {}).get("body", b"")

    async def query_all(self, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        if self.fail:
            raise RuntimeError("Table loom.frontend_assets does not exist")
        return [{"path": p, **self.rows[p]} for p in params["paths"] if p in self.rows]

    async def insert(self, table: str, rows: list[dict[str, Any]], settings: Any = None) -> None:
        self.inserted.extend(rows)


@pytest.fixture
def setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from starlette.testclient import TestClient

    from server.core import ch as ch_mod
    from server.identity.app import dashboard_routes
    from server.main import app
    from server.settings import settings
    from server.storage import frontend_assets

    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text("<html><head></head></html>")
    (tmp_path / "assets" / "index-localbeef.js").write_text("// local")
    monkeypatch.setattr(dashboard_routes, "_FRONTEND_DIST", tmp_path)
    (assets,) = (r.app for r in app.routes if getattr(r, "name", "") == "dashboard_assets")
    monkeypatch.setattr(assets, "all_directories", [tmp_path / "assets"])
    monkeypatch.setattr(settings, "dashboard_redirect_url", "")
    frontend_assets.cache_clear()
    fake = _FakeCH(
        {
            "index-otherbuild.js": {"body": b"// from N-1"},
            "logo-77777777.png": {"body": b"\x89PNG\r\n\x1a\n\xff\x00"},
        }
    )
    monkeypatch.setattr(ch_mod, "get_client", lambda: fake)
    yield TestClient(app, base_url="http://localhost:7777"), fake
    frontend_assets.cache_clear()


def test_an_asset_missing_on_disk_is_served_from_clickhouse(setup) -> None:
    client, fake = setup
    r = client.get("/assets/index-otherbuild.js")
    assert r.status_code == 200 and r.text == "// from N-1"
    assert r.headers["content-type"].startswith("text/javascript")
    assert r.headers["cache-control"] == "public, max-age=31536000, immutable"
    # Content-addressed: the second request is served from the process cache.
    client.get("/assets/index-otherbuild.js")
    assert fake.reads == 1
    assert fake.tag == "frontend_assets.fetch"


def test_binary_bodies_are_served_byte_for_byte(setup) -> None:
    client, _ = setup
    r = client.get("/assets/logo-77777777.png")
    assert r.status_code == 200 and r.content == b"\x89PNG\r\n\x1a\n\xff\x00"
    assert r.headers["content-type"] == "image/png"


def test_a_name_without_a_content_hash_is_never_looked_up(setup) -> None:
    client, fake = setup
    assert client.get("/assets/robots.txt").status_code == 404
    assert client.get("/assets/probe.js").status_code == 404
    assert fake.reads == 0


def test_a_miss_is_remembered_for_a_minute(setup, monkeypatch: pytest.MonkeyPatch) -> None:
    from server.storage import frontend_assets

    client, fake = setup
    for _ in range(3):
        assert client.get("/assets/probe-12345678.js").status_code == 404
    assert fake.reads == 1
    monkeypatch.setattr(frontend_assets, "_MISS_TTL_S", 0.0)
    frontend_assets._MISSES.clear()
    client.get("/assets/probe-12345678.js")
    client.get("/assets/probe-12345678.js")
    assert fake.reads == 3


def test_the_miss_memory_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.storage import frontend_assets

    frontend_assets.cache_clear()
    monkeypatch.setattr(frontend_assets, "_MISSES_MAX", 3)
    for i in range(5):
        frontend_assets._remember_miss(f"p-{i:08d}.js")
    assert list(frontend_assets._MISSES) == [f"p-{i:08d}.js" for i in (2, 3, 4)]
    frontend_assets.cache_clear()


def test_an_asset_path_longer_than_any_bundle_path_is_a_404_before_any_read(setup) -> None:
    client, fake = setup
    assert client.get("/assets/" + "a/" * 600 + "index-otherbuild.js").status_code == 404
    assert fake.reads == 0


def test_local_files_win_and_unknown_paths_stay_404(setup) -> None:
    client, fake = setup
    r = client.get("/assets/index-localbeef.js")
    assert r.status_code == 200 and r.text == "// local"
    assert fake.reads == 0
    assert client.get("/assets/index-nowhere1.js").status_code == 404


def test_a_missing_table_serves_the_404(setup) -> None:
    client, fake = setup
    fake.fail = True
    assert client.get("/assets/index-otherbuild.js").status_code == 404


def test_the_page_is_never_cached(setup) -> None:
    client, _ = setup
    r = client.get("/")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store"


def test_unhashed_names_revalidate() -> None:
    from server.storage.frontend_assets import cache_control_for

    assert cache_control_for("/assets/logo.svg") == "no-cache"
    assert cache_control_for("/assets/index-BxQ3k9aZ.js").endswith("immutable")


async def test_publish_writes_only_what_the_table_lacks_or_holds_stale(tmp_path: Path) -> None:
    from server.core.datetimes import naive_utc_now
    from server.storage import frontend_assets

    (tmp_path / "a-11111111.js").write_bytes(b"a")
    (tmp_path / "b-22222222.js").write_bytes(b"b")
    (tmp_path / "c-33333333.js").write_bytes(b"c")
    (tmp_path / "big-44444444.js").write_bytes(b"x" * (frontend_assets.MAX_ASSET_BYTES + 1))
    now = naive_utc_now()
    fake = _FakeCH(
        {
            # same bytes, fresh: skipped
            "a-11111111.js": {"sha256": hashlib.sha256(b"a").hexdigest(), "published_at": now},
            # same bytes, old: republished so the TTL does not drop a live build
            "b-22222222.js": {
                "sha256": hashlib.sha256(b"b").hexdigest(),
                "published_at": now - timedelta(days=2),
            },
        }
    )
    n = await frontend_assets.publish(tmp_path, published_by="r1", ch=fake)
    assert n == 2
    assert sorted(r["path"] for r in fake.inserted) == ["b-22222222.js", "c-33333333.js"]
    row = next(r for r in fake.inserted if r["path"] == "c-33333333.js")
    assert row["body"] == b"c" and row["size"] == 1


async def test_publish_tolerates_a_missing_table(tmp_path: Path) -> None:
    from server.storage import frontend_assets

    (tmp_path / "a-11111111.js").write_bytes(b"a")
    assert await frontend_assets.publish(tmp_path, ch=_FakeCH(fail=True)) is None


async def test_a_failed_first_round_is_retried_before_the_warm_up_completes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    from server.storage import frontend_assets

    (tmp_path / "a-11111111.js").write_bytes(b"a")
    monkeypatch.setattr(settings, "storage_frontend_assets_retry_s", 0.01)
    rounds: list[int | None] = [None, None, 1]

    async def _publish(assets_dir: Path, **_: Any) -> int | None:
        return rounds.pop(0) if rounds else 0

    monkeypatch.setattr(frontend_assets, "publish", _publish)
    done = asyncio.Event()
    loop = asyncio.create_task(frontend_assets.publish_loop(tmp_path, first_done=done))
    try:
        await asyncio.wait_for(done.wait(), 1)
        assert rounds == [], "warm-up completed only after the round that published"
    finally:
        loop.cancel()


async def test_the_publisher_never_holds_up_a_model_transition_drain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """It runs for the process's life: a deferred task would keep every web
    process from acknowledging a provider transition."""
    from server import boot
    from server.core.background import drain_task_sets
    from server.storage import frontend_assets

    async def _publish(*_: Any, **__: Any) -> int:
        return 0

    monkeypatch.setattr(frontend_assets, "publish", _publish)
    monkeypatch.setattr(boot, "_WARM", {})
    boot.publish_frontend_assets(_FakeCH())
    try:
        await boot._warm_event("frontend_assets").wait()
        assert await drain_task_sets(timeout_s=0)
    finally:
        boot.cancel_boot_tasks()
