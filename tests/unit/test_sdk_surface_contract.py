"""The Python SDK's public surface is a contract.

- `SyncAgent` mirrors `Agent`: every client attribute and every public
  coroutine method has a blocking twin.
- The public API is snapshotted (tests/unit/snapshots/sdk_public_api.json):
  `loom.__all__`, the constructor keywords of `Agent` / `SyncAgent` /
  `Datapack`, and every client's public methods with their parameters.
  Changing it is a deliberate act: run with LOOM_UPDATE_SNAPSHOT=1.
- Typing fixtures: `sdk_typing/valid.py` type-checks under mypy --strict;
  every marked line of `sdk_typing/negative.py` is rejected with the named
  code and nothing else errors; `sdk_typing/sync_clients.py` is the blocking
  client surface of `SyncAgent`.
"""

from __future__ import annotations

import inspect
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import loom
import pytest
from loom import Agent, Datapack, SyncAgent
from loom.sync import _CLIENT_ATTRS

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
SNAPSHOT = HERE / "snapshots" / "sdk_public_api.json"


def _agent() -> Agent:
    return Agent(org="o", namespace="n", agent="a", api_key="k", base_url="http://x")


def _public_methods(obj: object) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for name, member in inspect.getmembers(type(obj)):
        if name.startswith("_") or not callable(member):
            continue
        try:
            params = [p for p in inspect.signature(member).parameters if p != "self"]
        except (TypeError, ValueError):
            params = []
        out[name] = params
    return out


def _init_kwargs(cls: type) -> list[str]:
    return [p for p in inspect.signature(cls.__init__).parameters if p != "self"]


def _surface() -> dict[str, Any]:
    a = _agent()
    try:
        clients: dict[str, Any] = {}
        for name in sorted(_CLIENT_ATTRS):
            client = getattr(a, name)
            entry: dict[str, Any] = {
                "class": type(client).__name__,
                "methods": _public_methods(client),
            }
            # Clients inside a client (`memory.record`, `memory.schema`) are
            # part of the contract too; SyncAgent proxies them by this marker.
            nested = {
                n: {"class": type(v).__name__, "methods": _public_methods(v)}
                for n, v in sorted(vars(client).items())
                if getattr(v, "_loom_nested_client", False)
            }
            if nested:
                entry["nested"] = nested
            clients[name] = entry
        return {
            "loom.__all__": sorted(loom.__all__),
            "Agent.__init__": _init_kwargs(Agent),
            "SyncAgent.__init__": _init_kwargs(SyncAgent),
            "Datapack.__init__": _init_kwargs(Datapack),
            "Agent.methods": _public_methods(a),
            "clients": clients,
        }
    finally:
        import asyncio

        asyncio.run(a.close())


# ---- SyncAgent parity ----------------------------------------------------


def test_every_agent_client_has_a_sync_proxy_and_the_list_is_complete() -> None:
    a = _agent()
    try:
        on_agent = sorted(
            n
            for n, v in vars(a).items()
            if type(v).__name__.endswith("Client") and not n.startswith("_")
        )
        assert sorted(_CLIENT_ATTRS) == on_agent, {
            "missing from SyncAgent._CLIENT_ATTRS": sorted(set(on_agent) - set(_CLIENT_ATTRS)),
            "in _CLIENT_ATTRS but not on Agent": sorted(set(_CLIENT_ATTRS) - set(on_agent)),
        }
    finally:
        import asyncio

        asyncio.run(a.close())


def test_every_public_coroutine_method_of_agent_has_a_blocking_twin() -> None:
    coroutines = {
        n
        for n, m in inspect.getmembers(Agent, inspect.iscoroutinefunction)
        if not n.startswith("_")
    }
    missing = sorted(n for n in coroutines if not callable(getattr(SyncAgent, n, None)))
    assert not missing, f"SyncAgent lacks blocking twins for Agent coroutines: {missing}"
    for name in ("identity", "run_scope"):
        assert callable(getattr(SyncAgent, name))


def _options(fn: Any) -> set[str]:
    params = inspect.signature(fn).parameters.values()
    return {p.name for p in params if p.kind is p.KEYWORD_ONLY}


def test_datapack_and_sync_agent_take_every_agent_option() -> None:
    assert _options(Datapack.__init__) == _options(Agent.__init__)
    assert _options(SyncAgent.__init__) == _options(Agent.__init__) | {"call_timeout_s"}


def test_datapack_forwards_every_option_and_rejects_an_unknown_one() -> None:
    import asyncio

    pack = Datapack(
        "o",
        "n",
        api_key="k",
        auto_run=True,
        mentor_brief=True,
        coalesce_sets=False,
        retry={"max_attempts": 1},
        ratelimit_max_wait_s=0,
        consumer="product",
    )
    a = pack.agent("bot", consumer="other")
    try:
        assert (a.auto_run, a.mentor_brief, a.coalesce_sets) == (True, True, False)
        assert a.transport.retry.max_attempts == 1 and a.consumer == "other"
        assert a.transport._ratelimit_max_wait_s == 0
    finally:
        asyncio.run(a.close())
    with pytest.raises(TypeError):
        pack.agent("bot", no_such_option=1)


def test_sync_agent_exposes_the_agent_settings() -> None:
    sa = SyncAgent("o", "n", "a", api_key="k", auto_run=True, mentor_brief=True)
    try:
        assert (sa.auto_run, sa.mentor_brief, sa.coalesce_sets) == (True, True, True)
        assert sa.ambient_run_id == ""
    finally:
        sa.close()


def test_sync_agent_call_timeout_bounds_a_blocking_call() -> None:
    import asyncio
    from concurrent.futures import TimeoutError as FutureTimeout

    sa = SyncAgent("o", "n", "a", api_key="k", call_timeout_s=0.05)

    async def slow(*_a: Any, **_k: Any) -> dict[str, Any]:
        await asyncio.sleep(1)
        return {}

    sa.transport.post = slow
    try:
        with pytest.raises(FutureTimeout):
            sa.call("memory.search", query="q")
    finally:
        sa.close()


def test_sync_agent_call_and_probe_block_on_the_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("ops.capabilities"):
            return httpx.Response(
                200, json={"ops": [{"name": "memory.search", "request_fields": ["query"]}]}
            )
        return httpx.Response(
            200, json={"echo": json.loads(req.content)}, headers={"X-Request-Id": "r1"}
        )

    with SyncAgent(org="o", namespace="n", agent="bot", api_key="k", base_url="http://x") as sa:
        sa.transport._client = httpx.AsyncClient(
            base_url="http://x", transport=httpx.MockTransport(handler)
        )
        out = sa.call("memory.search", query="q", user_id="u1")
        assert (
            out["echo"]["query"] == "q"
            and out["echo"]["user_id"] == "u1"
            and out["echo"]["org"] == "o"
        )
        meta = sa.call_with_meta("memory.search", query="q")
        assert meta.request_id == "r1" and meta.data["echo"]["query"] == "q"
        p = sa.probe(ops=["memory.search", "memory.ask"])
        assert p["missing_ops"] == ["memory.ask"]
        assert sa.identity(user_id="u9")["user_id"] == "u9"


# ---- the public API snapshot --------------------------------------------


def test_public_api_matches_the_snapshot() -> None:
    current = _surface()
    if os.environ.get("LOOM_UPDATE_SNAPSHOT") == "1":
        SNAPSHOT.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n")
    assert SNAPSHOT.exists(), "run once with LOOM_UPDATE_SNAPSHOT=1 to create the snapshot"
    recorded = json.loads(SNAPSHOT.read_text())
    if current != recorded:
        diff: list[str] = []
        for key in sorted(set(current) | set(recorded)):
            if current.get(key) != recorded.get(key):
                if key == "clients":
                    for c in sorted(set(current["clients"]) | set(recorded["clients"])):
                        a, b = current["clients"].get(c), recorded["clients"].get(c)
                        if a != b:
                            am, bm = (a or {}).get("methods", {}), (b or {}).get("methods", {})
                            for m in sorted(set(am) | set(bm)):
                                if am.get(m) != bm.get(m):
                                    diff.append(f"clients.{c}.{m}: {bm.get(m)} -> {am.get(m)}")
                else:
                    diff.append(f"{key}: {recorded.get(key)} -> {current.get(key)}")
        pytest.fail(
            "the public SDK surface changed; if intended, run\n"
            "  LOOM_UPDATE_SNAPSHOT=1 pytest tests/unit/test_sdk_surface_contract.py\n"
            "and commit tests/unit/snapshots/sdk_public_api.json. Changes:\n  "
            + "\n  ".join(diff[:40])
        )


# ---- typing fixtures -----------------------------------------------------


def _mypy(path: Path) -> list[tuple[int, str]]:
    """(line, error-code) for every mypy --strict error in `path`, run from the
    repo root with the SDK on MYPYPATH (the way a consumer sees it)."""
    rel = path.relative_to(ROOT).as_posix()
    proc = subprocess.run(
        [sys.executable, "-m", "mypy", "--strict", "--no-error-summary", "--show-error-codes", rel],
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=False,
        env={**os.environ, "MYPYPATH": "sdk/python"},
    )
    out: list[tuple[int, str]] = []
    for line in proc.stdout.splitlines():
        m = re.match(rf"{re.escape(rel)}:(\d+):(?:\d+:)? error: .*\[([a-z-]+)\]$", line)
        if m:
            out.append((int(m.group(1)), m.group(2)))
    assert proc.returncode in (0, 1), proc.stdout + proc.stderr
    if proc.returncode == 1 and not out:
        raise AssertionError(f"mypy errored without parseable lines:\n{proc.stdout}")
    return out


def test_typing_fixture_valid_passes_strict_mypy() -> None:
    errors = _mypy(HERE / "sdk_typing" / "valid.py")
    assert errors == [], errors


def test_typing_fixture_sync_clients_passes_strict_mypy() -> None:
    errors = _mypy(HERE / "sdk_typing" / "sync_clients.py")
    assert errors == [], errors


def test_sync_agent_declares_exactly_the_clients_it_attaches() -> None:
    from loom import sync

    declared = {
        n for n, t in sync.SyncAgent.__annotations__.items() if "_SyncClientProxy" in str(t)
    }
    assert declared == set(sync._CLIENT_ATTRS)


def test_typing_fixture_negative_is_rejected_where_marked() -> None:
    path = HERE / "sdk_typing" / "negative.py"
    expected = {
        i: m.group(1)
        for i, line in enumerate(path.read_text().splitlines(), start=1)
        if (m := re.search(r"# expect: ([a-z-]+)$", line))
    }
    assert expected, "negative.py has no `# expect:` marks"
    got = _mypy(path)
    by_line: dict[int, set[str]] = {}
    for ln, code in got:
        by_line.setdefault(ln, set()).add(code)
    for ln, code in expected.items():
        assert code in by_line.get(ln, set()), (
            f"line {ln}: expected [{code}], mypy said {by_line.get(ln)}"
        )
    unexpected = sorted(ln for ln in by_line if ln not in expected)
    assert not unexpected, f"unmarked lines errored: {[(ln, by_line[ln]) for ln in unexpected]}"
