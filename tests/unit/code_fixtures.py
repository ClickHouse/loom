"""Test builders for code-plane configuration."""

from __future__ import annotations

import gc
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from server.code.domain.index.repos import RepoConfig


def git_config(env: dict[str, str]) -> dict[str, str]:
    """The `-c key=value` pairs an environment hands git (`GIT_CONFIG_KEY_i`)."""
    n = int(env.get("GIT_CONFIG_COUNT", "0"))
    return {env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"] for i in range(n)}


def repo_config(**overrides: Any) -> RepoConfig:
    """A `RepoConfig` the way the scalar settings build one: the bare clone and the
    checkout come from `code_git_dir` / `code_scip_checkout_dir` as they stand when it
    is built, every other field from the keyword arguments or the dataclass default."""
    from server.settings import settings

    fields: dict[str, Any] = {
        "org": "o",
        "namespace": "code-x",
        "repo": "Org/Repo",
        "git_dir": settings.code_git_dir,
        "git_remote": "",
        "git_branch": "",
        "path_prefixes": (),
        "github_token": "",
        "scip_checkout_dir": settings.code_scip_checkout_dir,
    }
    fields.update(overrides)
    return RepoConfig(**fields)


def _linear_failure_context() -> dict[str, Any]:
    """Cumulative worker state after failure, separate from individual sample costs."""
    return {
        "process_cpu_ns_at_failure": time.process_time_ns(),
        "monotonic_ns_at_failure": time.monotonic_ns(),
        "allocated_blocks_at_failure": sys.getallocatedblocks(),
        "gc_enabled_at_failure": gc.isenabled(),
        "gc_threshold_at_failure": gc.get_threshold(),
        "gc_count_at_failure": gc.get_count(),
        "gc_stats_at_failure": gc.get_stats(),
        "trace_active_at_failure": sys.gettrace() is not None,
        "profile_active_at_failure": sys.getprofile() is not None,
    }


def assert_linear(
    cost: Callable[[Any], float],
    small: Any,
    big: Any,
    *,
    ratio: float = 8.0,
    floor: float = 0.0,
) -> None:
    """`cost(big)` is under `ratio` times `cost(small)` (8 suits 4 times the size: a
    quadratic one takes 16) or under `floor` seconds. On a loaded host the best of three
    runs of each counts. No absolute bound: a linear cost at `big` takes several times
    as long on a slow runner as on a fast one."""
    a, b = cost(small), cost(big)
    if b > ratio * a:
        a, b = min(a, cost(small), cost(small)), min(b, cost(big), cost(big))
    try:
        assert b < ratio * a or b < floor, (a, b)
    except AssertionError as error:
        try:
            # Diagnostic imports and allocations must follow the failed judge.
            import json

            note = "linear_cost_context=" + json.dumps(_linear_failure_context(), sort_keys=True)
        except Exception as context_error:  # Preserve the failed judge.
            error.add_note("linear_cost_context_error=" + type(context_error).__name__)
            raise error from context_error
        error.add_note(note)
        raise


@contextmanager
def untraced() -> Iterator[None]:
    """This thread's tracer and the garbage collector off, so a coverage run times the
    code, not the tracer (about three times its cost), nor a full collection walking
    every earlier test's heap. Only this thread's tracer: other threads stay measured.
    The collector's total cost grows linearly with what the code allocates, so leaving
    it out hides no super-linear cost. Both come back as they were."""
    previous = sys.gettrace()
    collecting = gc.isenabled()
    sys.settrace(None)
    gc.collect()
    gc.disable()
    try:
        yield
    finally:
        if collecting:
            gc.enable()
        sys.settrace(previous)


def grows_linearly(run: Callable[[int], object], small: int, big: int) -> None:
    """`run(big)` costs under 8 times `run(small)` at 4 times the size, or under 50 ms,
    as `assert_linear` checks."""

    def cost(n: int) -> float:
        t0 = time.perf_counter()
        run(n)
        return time.perf_counter() - t0

    assert_linear(cost, small, big, floor=0.05)
