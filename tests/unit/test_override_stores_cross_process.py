"""The host-shared override stores (budgets, rate limits): concurrent writers in
separate processes never drop each other's update, and an update that cannot be
shared is refused while every worker keeps its overrides."""

from __future__ import annotations

import errno
import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from server.core import shared_document
from server.core.errors import OverrideNotSharedError
from server.core.ratelimit import Limit, Limiter
from server.observability.budgets import BudgetOverrides

ROOT = Path(__file__).resolve().parents[2]
WRITERS, UPDATES = 4, 10

# A writer retries OVERLOADED (a sibling held the lock) as a client does.
_RETRY = """
import sys
from server.core.errors import OverloadedError

def retried(update):
    while True:
        try:
            return update()
        except OverloadedError:
            pass
"""

_BUDGET_WRITER = (
    _RETRY
    + """
from server.observability.budgets import BudgetOverrides
o = BudgetOverrides(sys.argv[1])
for i in range(int(sys.argv[3])):
    retried(lambda: o.set(f"op.{sys.argv[2]}.{i}", i))
"""
)

_LIMIT_WRITER = (
    _RETRY
    + """
from server.core.ratelimit import Limit, Limiter
lim = Limiter()
for i in range(int(sys.argv[3])):
    limit = Limit(capacity=i + 1, rate=1.0)
    retried(lambda: lim.set_namespace_limit(f"ns{sys.argv[2]}_{i}", "memory", limit))
"""
)


def _document(path: Path | str) -> dict[str, object]:
    """The overrides a published file holds."""
    return json.loads(Path(path).read_text().split("\n", 1)[1])


def _sibling_publishes(path: Path, document: object) -> None:
    """A sibling's update, as `SharedDocument.update` writes it."""
    path.write_text(json.dumps({"generation": time.time_ns()}) + "\n" + json.dumps(document))


@pytest.fixture(autouse=True)
def _own_runtime_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Per-server files land in this test's directory, never the user's real one."""
    from server.core import runtime_files

    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    monkeypatch.setattr(runtime_files, "_pruned", set())


def _race(code: str, path: str, env: dict[str, str]) -> None:
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", textwrap.dedent(code), path, str(w), str(UPDATES)],
            cwd=ROOT,
            env={**os.environ, **env},
        )
        for w in range(WRITERS)
    ]
    assert [p.wait(timeout=120) for p in procs] == [0] * WRITERS


def test_concurrent_budget_writers_all_survive(tmp_path: Path) -> None:
    path = str(tmp_path / "budgets.json")
    _race(_BUDGET_WRITER, path, {})
    ops = _document(path)["ops"]
    assert len(ops) == WRITERS * UPDATES


def test_concurrent_rate_limit_writers_all_survive(tmp_path: Path) -> None:
    table = str(tmp_path / "rl.buckets")
    env = {"LOOM_RATELIMIT_SHARED": "on", "LOOM_RATELIMIT_SHARED_PATH": table}
    _race(_LIMIT_WRITER, table, env)
    ns = _document(table + ".overrides.json")["ns"]
    assert len(ns) == WRITERS * UPDATES


def _no_space(*_a: object) -> None:
    raise OSError(errno.ENOSPC, "No space left on device")


def _deny_reads_of(monkeypatch: pytest.MonkeyPatch, path: str) -> None:
    real = shared_document.open_private

    def open_private(p: str, flags: int) -> int:
        if p == path:
            raise PermissionError(errno.EACCES, "denied", p)
        return real(p, flags)

    monkeypatch.setattr(shared_document, "open_private", open_private)


def _budgets(tmp_path: Path) -> tuple[BudgetOverrides, BudgetOverrides, Path]:
    path = tmp_path / "budgets.json"
    return BudgetOverrides(str(path)), BudgetOverrides(str(path)), path


@pytest.fixture
def limiters(settings_override, tmp_path: Path) -> tuple[Limiter, Limiter, Path]:
    table = tmp_path / "rl.buckets"
    settings_override(ratelimit_shared="on", ratelimit_shared_path=str(table))
    return Limiter(), Limiter(), Path(f"{table}.overrides.json")


def test_an_unpublished_budget_update_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a, b, _ = _budgets(tmp_path)
    a.set("memory.set", 30)
    monkeypatch.setattr(shared_document.os, "replace", _no_space)
    with pytest.raises(OverrideNotSharedError, match="not applied"):
        a.set("memory.set", 0)
    with pytest.raises(OverrideNotSharedError):
        a.clear("memory.set")
    assert a.effective("memory.set", 99) == 30
    assert b.effective("memory.set", 99) == 30


def test_an_unpublished_rate_limit_update_is_refused(
    limiters: tuple[Limiter, Limiter, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    a, _, _ = limiters
    monkeypatch.setattr(shared_document.os, "replace", _no_space)
    with pytest.raises(OverrideNotSharedError, match="not applied"):
        a.set_namespace_limit("ns1", "memory", Limit(capacity=1, rate=0.0))
    assert ("ns1", "memory") not in a._ns_overrides


def test_a_budget_update_over_an_unreadable_file_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a, b, path = _budgets(tmp_path)
    b.set("memory.search", 30)
    a.set("memory.set", 0)
    before = path.read_bytes()
    _deny_reads_of(monkeypatch, str(path))
    with pytest.raises(OverrideNotSharedError, match="not applied"):
        b.set("memory.get", 5)
    monkeypatch.undo()
    assert path.read_bytes() == before
    assert b.effective("memory.set", 99) == 0


def test_a_rate_limit_update_over_an_unreadable_file_is_refused(
    limiters: tuple[Limiter, Limiter, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    a, b, overrides = limiters
    b.set_namespace_limit("ns_b", "memory", Limit(capacity=30, rate=1.0))
    a.set_namespace_limit("ns_a", "memory", Limit(capacity=1, rate=0.0))
    before = overrides.read_bytes()
    _deny_reads_of(monkeypatch, str(overrides))
    with pytest.raises(OverrideNotSharedError, match="not applied"):
        b.set_namespace_limit("ns_b", "memory", Limit(capacity=2, rate=1.0))
    monkeypatch.undo()
    assert overrides.read_bytes() == before
    a.reset_all()
    b.reset_all()


def test_an_update_repairs_a_corrupt_override_file(
    limiters: tuple[Limiter, Limiter, Path],
) -> None:
    a, b, overrides = limiters
    a.set_namespace_limit("ns1", "memory", Limit(capacity=1, rate=0.0))
    overrides.write_text("{not json")
    b.set_namespace_limit("ns2", "memory", Limit(capacity=2, rate=0.0))
    a.check("ns0", "memory.set", org="o")
    assert a._ns_overrides == {("ns2", "memory"): Limit(capacity=2, rate=0.0)}
    a.reset_all()
    b.reset_all()


def test_overrides_bind_every_worker_with_bucket_sharing_off(settings_override) -> None:
    """The overrides have their own host file: sharing the buckets is a separate choice."""
    settings_override(ratelimit_shared="off", web_workers=3)
    a, b = Limiter(clock=lambda: 0.0), Limiter(clock=lambda: 0.0)  # no refill
    assert not a.shared_active
    assert b.check("ns1", "memory.set", org="o").allowed  # b's bucket predates the override
    # Per-process buckets divide a cap by the workers: one token each.
    a.set_namespace_limit("ns1", "memory", Limit(capacity=3, rate=0.0))
    assert b.check("ns1", "memory.set", org="o").allowed
    assert not b.check("ns1", "memory.set", org="o").allowed
    a.reset_all()
    b.reset_all()


def test_a_reset_keeps_the_lock_file_a_sibling_may_hold(settings_override) -> None:
    settings_override(ratelimit_shared="off", web_workers=3)
    lim = Limiter()
    lim.set_namespace_limit("ns1", "memory", Limit(capacity=3, rate=0.0))
    path = Path(lim._overrides_document().path)
    lim.reset_all()
    assert not path.exists() and Path(f"{path}.lock").exists()


def test_one_worker_keeps_its_overrides_in_memory(settings_override) -> None:
    settings_override(ratelimit_shared="auto", web_workers=1)
    lim = Limiter()
    assert lim._overrides_document() is None
    lim.set_namespace_limit("ns1", "memory", Limit(capacity=3, rate=0.0))
    assert lim._ns_overrides[("ns1", "memory")] == Limit(capacity=3, rate=0.0)


@pytest.mark.asyncio
async def test_admin_ratelimit_set_cannot_acknowledge_a_change_it_could_not_share(
    settings_override, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.core import ratelimit
    from server.insights.app.admin_models import RatelimitSetInput
    from server.insights.app.admin_routes import admin_ratelimit_set

    settings_override(ratelimit_shared="on", ratelimit_shared_path="/proc/loom-test.buckets")
    lim = Limiter()
    monkeypatch.setattr(ratelimit, "get_limiter", lambda: lim)
    with pytest.raises(OverrideNotSharedError) as refused:
        await admin_ratelimit_set(
            RatelimitSetInput(namespace="ns1", op_class="memory", capacity=3, rate=0.0)
        )
    assert refused.value.status_code == 503
    assert refused.value.headers() is None  # no Retry-After: the fix is an operator's
    assert "/proc" not in refused.value.message  # the file path stays in the server log
    assert ("ns1", "memory") not in lim._ns_overrides


def test_a_siblings_unrelated_override_keeps_this_workers_bucket_state(settings_override) -> None:
    settings_override(ratelimit_shared="off", web_workers=3)
    a, b = Limiter(clock=lambda: 0.0), Limiter(clock=lambda: 0.0)  # no refill
    b.set_namespace_limit("hot", "memory", Limit(capacity=3, rate=0.0))  # one token per worker
    assert b.check("hot", "memory.set", org="o").allowed
    assert not b.check("hot", "memory.set", org="o").allowed
    a.set_namespace_limit("other", "memory", Limit(capacity=30, rate=0.0))
    assert not b.check("hot", "memory.set", org="o").allowed
    a.clear_namespace_limit("hot", "memory")
    assert b.check("hot", "memory.set", org="o").allowed  # back at the default limit
    a.reset_all()
    b.reset_all()


def test_an_override_resizes_live_buckets_as_it_sizes_new_ones(settings_override) -> None:
    """Three workers with per-process buckets: a cap of 3 is one token each,
    whether the bucket predates the override or not."""
    settings_override(ratelimit_shared="off", web_workers=3)
    lim = Limiter(clock=lambda: 0.0)
    assert lim.check("ns1", "memory.set", org="o").allowed  # the bucket exists before
    lim.set_namespace_limit("ns1", "memory", Limit(capacity=3, rate=0.0))
    assert lim.check("ns1", "memory.set", org="o").allowed
    assert not lim.check("ns1", "memory.set", org="o").allowed
    lim.reset_all()


def test_a_check_resolves_the_override_file_once(
    settings_override, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings_override(ratelimit_shared="off", web_workers=3)
    calls: list[str] = []
    real = shared_document.server_file
    monkeypatch.setattr(
        shared_document, "server_file", lambda name: calls.append(name) or real(name)
    )
    lim = Limiter()
    for _ in range(5):
        lim.check("ns1", "memory.set", org="o")
    assert calls == ["ratelimit-overrides"]


def test_clearing_an_override_frees_the_clearing_worker_too(settings_override) -> None:
    settings_override(ratelimit_shared="off", web_workers=3)
    a = Limiter(clock=lambda: 0.0)  # no refill
    a.set_namespace_limit("hot", "memory", Limit(capacity=3, rate=0.0))  # one token here
    assert a.check("hot", "memory.set", org="o").allowed
    assert not a.check("hot", "memory.set", org="o").allowed
    a.clear_namespace_limit("hot", "memory")
    assert a.check("hot", "memory.set", org="o").allowed
    a.reset_all()


def test_a_siblings_override_seen_inside_an_update_resizes_this_worker(settings_override) -> None:
    settings_override(ratelimit_shared="off", web_workers=3)
    a, b = Limiter(clock=lambda: 0.0), Limiter(clock=lambda: 0.0)
    assert b.check("hot", "memory.set", org="o").allowed  # a default-sized bucket
    a.set_namespace_limit("hot", "memory", Limit(capacity=3, rate=0.0))
    b.set_namespace_limit("other", "memory", Limit(capacity=30, rate=0.0))  # b learns of "hot" here
    result = b.check("hot", "memory.set", org="o")
    assert result.limit == 1.0 and not b.check("hot", "memory.set", org="o").allowed
    a.reset_all()
    b.reset_all()


def test_a_refused_update_still_adopts_a_siblings_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a, b, _ = _budgets(tmp_path)
    a.set("memory.set", 30)
    b.set("memory.set", 1)
    monkeypatch.setattr(shared_document.os, "replace", _no_space)
    with pytest.raises(OverrideNotSharedError):
        a.set("memory.search", 5)
    monkeypatch.undo()
    assert a.effective("memory.set", 99) == 1


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes through any mode")
def test_a_worker_that_starts_after_the_runtime_dir_turns_read_only_still_shares_it(
    settings_override, tmp_path: Path
) -> None:
    """With the launcher's directory, no worker falls back to another one, where
    its updates would succeed out of its siblings' sight."""
    settings_override(web_workers=3, runtime_dir=str(tmp_path / "run" / "loom"))
    (tmp_path / "run").mkdir()
    (tmp_path / "run" / "loom").mkdir(mode=0o700)
    a = BudgetOverrides()
    a.set_for_consumer("c1", "*", 4)
    shared = tmp_path / "run" / "loom"
    shared.chmod(0o500)
    try:
        late = BudgetOverrides()
        with pytest.raises(OverrideNotSharedError):
            late.set_for_consumer("c2", "*", 4)
        assert late.get_for_consumer("c1", "*") == 4
        assert late._document().path == a._document().path  # type: ignore[union-attr]
    finally:
        shared.chmod(0o700)


@pytest.mark.parametrize(
    "corrupt",
    [
        {"ops": {}, "consumer": [["c1", "*", "abc"]]},
        {"ops": {}, "consumer": [["c1", "*", float("inf")]]},
        {"ops": {"memory.get": "5"}, "consumer": []},
        {"ops": {"memory.get": 2.7}, "consumer": []},
        {"ops": {"memory.get": True}, "consumer": []},
        {"ops": {"memory.get": -5}, "consumer": []},
    ],
)
def test_a_budget_that_is_not_one_makes_the_file_corrupt(
    tmp_path: Path, corrupt: dict[str, object]
) -> None:
    a, b, path = _budgets(tmp_path)
    a.set_for_consumer("c1", "*", "3x")
    assert b.get_for_consumer("c1", "*") == 3.0
    _sibling_publishes(path, corrupt)
    assert b.get_for_consumer("c1", "*") == 3.0  # the corrupt version is not adopted
    b.set_for_consumer("c2", "*", 7)
    assert _document(path)["consumer"] == [["c1", "*", 3.0], ["c2", "*", 7]]


@pytest.mark.parametrize("bad", ["5", "nan", True, -1, float("inf")])
def test_a_rate_limit_that_is_not_one_makes_the_file_corrupt(
    limiters: tuple[Limiter, Limiter, Path], bad: object
) -> None:
    a, b, overrides = limiters
    a.set_namespace_limit("ns1", "memory", Limit(capacity=1, rate=0.0))
    b.check("ns0", "memory.set", org="o")
    assert b._ns_overrides == {("ns1", "memory"): Limit(capacity=1, rate=0.0)}
    _sibling_publishes(overrides, {"ns": [["ns1", "memory", bad, 0.0]], "org": [], "actor": []})
    b.check("ns0", "memory.set", org="o")
    assert b._ns_overrides == {("ns1", "memory"): Limit(capacity=1, rate=0.0)}  # not adopted
    a.reset_all()
    b.reset_all()


@pytest.mark.parametrize("value", ["infx", "nanx", "inf", "-1x", float("inf")])
def test_a_budget_that_is_not_finite_is_refused(tmp_path: Path, value: object) -> None:
    a, _, path = _budgets(tmp_path)
    with pytest.raises(ValueError):
        a.set_for_consumer("c1", "*", value)  # type: ignore[arg-type]
    assert not path.exists()


def test_an_integer_too_large_for_a_float_is_refused_and_never_breaks_a_read(
    tmp_path: Path, limiters: tuple[Limiter, Limiter, Path]
) -> None:
    huge = 10**400
    a, b, path = _budgets(tmp_path)
    with pytest.raises(ValueError):
        a.set_for_consumer("c1", "*", huge)
    for unusable in (huge, 1.5, -1):
        with pytest.raises(ValueError):
            a.set("memory.set", unusable)
    a.set_for_consumer("c1", "*", 4)
    _sibling_publishes(path, {"ops": {}, "consumer": [["c1", "*", huge]]})
    assert b.get_for_consumer("c1", "*") is None  # corrupt: not adopted, not raised
    la, lb, overrides = limiters
    la.set_namespace_limit("ns1", "memory", Limit(capacity=1, rate=0.0))
    _sibling_publishes(overrides, {"ns": [["ns1", "memory", huge, 0]], "org": [], "actor": []})
    assert lb.check("ns0", "memory.set", org="o").allowed
    la.reset_all()
    lb.reset_all()


def test_with_no_private_runtime_directory_overrides_are_refused_not_kept_local(
    settings_override, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Several workers and nowhere to share: reads keep each worker's state, an
    update answers OVERRIDE_NOT_SHARED rather than binding one worker, and once
    the directory is fixed the next update binds every worker, those that read
    before the fix too."""

    def nowhere(name: str) -> str:
        raise OSError("none of /run/loom, /tmp/loom-1 is a directory only this user can enter")

    settings_override(ratelimit_shared="off", web_workers=3)
    real = shared_document.server_file
    monkeypatch.setattr(shared_document, "server_file", nowhere)
    monkeypatch.setattr(shared_document, "RESOLVE_RETRY_S", 0.0)
    a, b = BudgetOverrides(), BudgetOverrides()
    la, lb = Limiter(clock=lambda: 0.0), Limiter(clock=lambda: 0.0)
    assert a.get("memory.search") is None and b.get("memory.search") is None
    assert la.check("ns1", "memory.set", org="o").allowed
    assert lb.check("ns1", "memory.set", org="o").allowed
    with pytest.raises(OverrideNotSharedError):
        a.set("memory.search", 123)
    with pytest.raises(OverrideNotSharedError):
        la.set_namespace_limit("ns1", "memory", Limit(capacity=3, rate=0.0))
    monkeypatch.setattr(shared_document, "server_file", real)  # the directory is fixed
    a.set("memory.search", 123)
    la.set_namespace_limit("ns1", "memory", Limit(capacity=3, rate=0.0))  # a token a worker
    assert b.get("memory.search") == 123
    assert lb.check("ns1", "memory.set", org="o").allowed
    assert not lb.check("ns1", "memory.set", org="o").allowed  # b took a's limit
    la.reset_all()
    lb.reset_all()
