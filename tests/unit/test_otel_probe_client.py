"""Gauge probes share one ClickHouse client."""

from __future__ import annotations

from typing import Any

import pytest

from server.observability import otel_exporters, otel_probe_client, otel_probes


class _Row:
    def __init__(self, rows: list[Any]) -> None:
        self.result_rows = rows
        self.first_row = rows[0] if rows else ()


class _Client:
    built = 0

    def __init__(self, fail_first: bool = False) -> None:
        type(self).built += 1
        self.closed = False
        self.calls = 0
        self.fail_first = fail_first

    def query(self, *a: Any, **k: Any) -> _Row:
        self.calls += 1
        if self.fail_first and self.calls == 1:
            raise RuntimeError("boom")
        return _Row([("acme", 3, 1, 0, 0, 0, 0)])

    def command(self, *a: Any, **k: Any) -> str:
        return "1"

    def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def _fresh_cache() -> Any:
    otel_probe_client._probe_client_reset()
    _Client.built = 0
    yield
    otel_probe_client._probe_client_reset()


def test_repeated_probes_build_one_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(otel_exporters, "_make_ch_client_for_otel", lambda: _Client())
    for _ in range(5):
        otel_probes._probe_retention()
    assert _Client.built == 1


def test_a_failing_probe_drops_the_client_and_the_next_call_rebuilds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    made: list[_Client] = []

    def factory() -> _Client:
        c = _Client(fail_first=not made)  # the first client fails its first query
        made.append(c)
        return c

    monkeypatch.setattr(otel_exporters, "_make_ch_client_for_otel", factory)
    with pytest.raises(RuntimeError):
        otel_probes._probe_retention()
    assert made[0].closed is True  # the broken client was closed, not leaked
    otel_probes._probe_retention()
    assert len(made) == 2 and made[1].closed is False
    otel_probes._probe_retention()
    assert len(made) == 2  # steady state: still one live client


def test_a_new_factory_replaces_the_cached_client(monkeypatch: pytest.MonkeyPatch) -> None:
    first: list[_Client] = []
    monkeypatch.setattr(
        otel_exporters, "_make_ch_client_for_otel", lambda: first.append(_Client()) or first[-1]
    )
    otel_probes._probe_retention()
    second: list[_Client] = []
    monkeypatch.setattr(
        otel_exporters, "_make_ch_client_for_otel", lambda: second.append(_Client()) or second[-1]
    )
    otel_probes._probe_retention()
    assert len(first) == 1 and first[0].closed is True and len(second) == 1


def test_probe_bodies_no_longer_build_their_own_client() -> None:
    import inspect

    for fn in (
        otel_probes._probe_state,
        otel_probes._probe_retention,
        otel_probes._probe_code_state,
    ):
        src = inspect.getsource(fn)
        assert "_make_ch_client_for_otel()" not in src, fn.__name__
        assert "_probe_client()" in src, fn.__name__


def test_concurrent_probes_queue_on_the_shared_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """The sync driver refuses a second statement on a session
    already running one. The three gauge caches refresh on their own threads
    and, at a 60 s export interval, lapse on the same tick, so without the
    proxy one probe can lose every race and its gauge series never reach
    ClickHouse. The proxy serialises the statement."""
    import threading
    import time

    class _OneAtATime:
        def __init__(self) -> None:
            self.inside = 0
            self.max_inside = 0
            self.calls = 0
            self.lock = threading.Lock()

        def query(self, _sql: str, **_k: Any) -> str:
            with self.lock:
                self.inside += 1
                self.max_inside = max(self.max_inside, self.inside)
            if self.inside > 1:
                raise RuntimeError("Attempt to execute concurrent queries within the same session")
            time.sleep(0.02)
            with self.lock:
                self.inside -= 1
                self.calls += 1
            return "ok"

        def close(self) -> None:
            pass

    stub = _OneAtATime()
    monkeypatch.setattr(otel_exporters, "_make_ch_client_for_otel", lambda: stub)
    otel_probe_client._probe_client_reset()
    errors: list[BaseException] = []

    def probe() -> None:
        try:
            otel_probe_client._probe_client().query("SELECT 1")
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=probe) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert errors == [], errors
    assert stub.calls == 6
    assert stub.max_inside == 1, "two probes ran on the session at once"
    otel_probe_client._probe_client_reset()


def test_the_otel_client_tags_its_queries(monkeypatch: pytest.MonkeyPatch) -> None:
    """The OTel client bypasses server.core.ch, so it carries its own log_comment:
    exporter inserts are attributed in system.query_log."""
    import clickhouse_connect

    seen: dict[str, Any] = {}
    monkeypatch.setattr(clickhouse_connect, "get_client", lambda **k: seen.update(k) or object())
    otel_exporters._make_ch_client_for_otel()
    assert seen["settings"]["log_comment"] == "otel.exporter"


def test_each_probe_query_is_tagged_with_its_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    got: list[dict[str, Any]] = []

    class _Tagged(_Client):
        def query(self, *a: Any, **k: Any) -> _Row:
            got.append(dict(k.get("settings") or {}))
            return super().query(*a, **k)

    monkeypatch.setattr(otel_exporters, "_make_ch_client_for_otel", lambda: _Tagged())
    otel_probes._probe_retention()
    otel_probe_client._probe_client().query("SELECT 1", settings={"log_comment": "mine", "x": 1})
    assert got[0]["log_comment"] == "otel.probe.retention"
    assert got[1] == {"log_comment": "mine", "x": 1}  # a caller's own tag wins
