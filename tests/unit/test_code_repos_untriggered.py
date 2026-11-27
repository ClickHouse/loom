"""A configured repository nothing indexes is one `code_repos_untriggered` health error,
fed from a per-process count (never a ClickHouse read in the probe): the poller is off,
the worker ledger holds no recent ingest pass and the entry has no `code.refresh` on
record. The count is read from the deployment's stores, so a process that runs no worker
answers like any other. With no repository configured the sandbox is not reported on
until something probes it."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from server.code.app.index import ingest_trigger
from server.observability.problems import HealthState, collect_problems, derive_status
from server.settings import settings

ENTRIES = [
    SimpleNamespace(org="acme", namespace="code-widget", repo="acme/widget"),
    SimpleNamespace(org="acme", namespace="code-gadget", repo="acme/gadget"),
]


class _Stores:
    """The worker ledger and the entries' state, with every read counted."""

    def __init__(self, *, passes: list[str], refreshed: list[str]) -> None:
        self.passes, self.refreshed = passes, refreshed
        self.reads: list[str] = []
        self.fail = False

    async def pass_summaries(self, prefix: str, days: int, *, ch: Any = None) -> list[Any]:
        self.reads.append(f"ledger:{prefix}")
        if self.fail:
            raise ConnectionError("store unreachable")
        return [{"worker": w} for w in self.passes if w.startswith(prefix)]

    async def state_values_for_keys(self, keys: list[str]) -> dict[str, dict[Any, str]]:
        self.reads.append(f"state:{keys[0]}")
        return {k: {("acme", ns): "2026-01-01" for ns in self.refreshed} for k in keys}


@pytest.fixture(autouse=True)
def _fresh_count() -> Any:
    ingest_trigger.reset_for_tests()
    yield
    ingest_trigger.reset_for_tests()


def _wire(
    monkeypatch: pytest.MonkeyPatch,
    *,
    poller: bool,
    entries: list[Any],
    passes: list[str] | None = None,
    refreshed: list[str] | None = None,
) -> _Stores:
    stores = _Stores(passes=passes or [], refreshed=refreshed or [])
    monkeypatch.setattr(settings, "code_ingest_enabled", poller)
    monkeypatch.setattr(settings, "code_intel_enabled", True)
    monkeypatch.setattr("server.code.domain.index.repos.configured_repos", lambda: entries)
    monkeypatch.setattr("server.code.app.index.repo_registry.registry_read", lambda: True)
    monkeypatch.setattr("server.workers.status_store.pass_summaries", stores.pass_summaries)
    monkeypatch.setattr("server.code.store.index.CodeRepo", lambda ch=None: stores)
    return stores


def _problem(count: int | None) -> list[tuple[str, str]]:
    problems = collect_problems(settings, HealthState(code_repos_untriggered=count))
    return [(p.code, p.severity) for p in problems if p.code == "code_repos_untriggered"]


@pytest.mark.parametrize(
    ("poller", "entries", "passes", "refreshed", "count", "reads"),
    [
        # the default: the poller is on, so nothing is asked of the stores
        (True, ENTRIES, [], [], 0, []),
        # no repository: memory only, whatever the switch says
        (False, [], [], [], 0, []),
        # another process of the deployment polls: its passes are in the ledger
        (False, ENTRIES, ["code_ingest"], [], 0, ["ledger:code_ingest"]),
        # trigger-only with CI wired for every entry
        (False, ENTRIES, [], ["code-widget", "code-gadget"], 0, ["ledger", "state"]),
        # trigger-only, one entry CI never reached
        (False, ENTRIES, [], ["code-widget"], 1, ["ledger", "state"]),
        # the poller off and nothing else: no entry is ever indexed after a push
        (False, ENTRIES, [], [], 2, ["ledger", "state"]),
        # another worker's passes are not an ingest pass
        (False, ENTRIES, ["code_ingest_other"], [], 2, ["ledger", "state"]),
    ],
    ids=[
        "polling",
        "no-repository",
        "ledger-pass",
        "all-refreshed",
        "one-unrefreshed",
        "no-trigger",
        "other-worker",
    ],
)
async def test_the_count_is_the_entries_with_no_poller_no_pass_and_no_refresh(
    monkeypatch: pytest.MonkeyPatch,
    poller: bool,
    entries: list[Any],
    passes: list[str],
    refreshed: list[str],
    count: int,
    reads: list[str],
) -> None:
    stores = _wire(monkeypatch, poller=poller, entries=entries, passes=passes, refreshed=refreshed)
    assert ingest_trigger.untriggered() is None  # not read yet: unknown, not healthy
    await ingest_trigger.refresh()
    assert ingest_trigger.untriggered() == count
    assert [r.split(":")[0] for r in stores.reads] == [r.split(":")[0] for r in reads]
    assert _problem(ingest_trigger.untriggered()) == (
        [("code_repos_untriggered", "error")] if count else []
    )


async def test_a_failed_read_or_an_unread_registry_keeps_the_last_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stores = _wire(monkeypatch, poller=False, entries=ENTRIES)
    monkeypatch.setattr("server.code.app.index.repo_registry.registry_read", lambda: False)
    await ingest_trigger.refresh()
    assert ingest_trigger.untriggered() is None and stores.reads == []
    assert _problem(None) == []  # unknown raises nothing
    monkeypatch.setattr("server.code.app.index.repo_registry.registry_read", lambda: True)
    await ingest_trigger.refresh()
    assert ingest_trigger.untriggered() == 2
    stores.fail = True  # a ledger that cannot be read is not an empty ledger
    stores.refreshed = ["code-widget", "code-gadget"]
    await ingest_trigger.refresh()
    assert ingest_trigger.untriggered() == 2


async def test_switching_code_intelligence_off_clears_the_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stores = _wire(monkeypatch, poller=False, entries=ENTRIES)
    monkeypatch.setattr(settings, "code_intel_enabled", False)
    await ingest_trigger.refresh()
    assert ingest_trigger.untriggered() == 0 and stores.reads == []


def test_the_problem_counts_the_entries_and_names_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "code_ingest_enabled", False)
    problems = collect_problems(settings, HealthState(code_repos_untriggered=2))
    (found,) = [p for p in problems if p.code == "code_repos_untriggered"]
    assert found.message.startswith("2 configured repositories are not indexed after a push")
    assert "acme" not in found.message + found.fix and "widget" not in found.message + found.fix
    assert "LOOM_CODE_INGEST_ENABLED" in found.fix and "code.refresh" in found.fix
    assert derive_status(True, problems) == "degraded"
    (one,) = [
        p
        for p in collect_problems(settings, HealthState(code_repos_untriggered=1))
        if p.code == "code_repos_untriggered"
    ]
    assert one.message.startswith("1 configured repository is not indexed after a push")
    # a count left from before the poller was switched on raises nothing
    monkeypatch.setattr(settings, "code_ingest_enabled", True)
    assert _problem(2) == []


@pytest.mark.parametrize(
    ("repos", "probed", "required", "reported"),
    [
        (0, None, False, []),
        (0, None, True, ["repo_sandbox_layout_refused"]),
        (1, None, False, ["repo_sandbox_layout_refused"]),
        (None, None, False, ["repo_sandbox_layout_refused"]),
        # a refused `code.repo.add` probed and left no repository: the page reads this
        (0, False, False, ["repo_jobs_unsandboxed", "repo_sandbox_layout_refused"]),
        (1, False, False, ["repo_jobs_unsandboxed", "repo_sandbox_layout_refused"]),
    ],
    ids=[
        "no-repository",
        "sandbox-required",
        "a-repository",
        "not-counted",
        "no-repository-probed",
        "a-repository-probed",
    ],
)
def test_the_sandbox_is_silent_only_with_no_repository_and_no_probe(
    monkeypatch: pytest.MonkeyPatch,
    repos: int | None,
    probed: bool | None,
    required: bool,
    reported: list[str],
) -> None:
    monkeypatch.setattr(settings, "code_ingest_enabled", True)
    monkeypatch.setattr(settings, "code_sandbox_required", required)
    state = HealthState(
        repo_sandbox=probed, repo_sandbox_refusal="sandbox: /x is inside", code_repos=repos
    )
    codes = sorted(p.code for p in collect_problems(settings, state) if "sandbox" in p.code)
    assert codes == reported


def test_the_count_of_known_repositories_needs_no_registry_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.code.domain.index import repos

    monkeypatch.setattr(settings, "code_self_service_enabled", True)
    monkeypatch.setattr(settings, "code_repos_json", "")
    monkeypatch.setattr(settings, "code_repo", "")
    repos.reset_for_tests()
    try:
        assert repos.registry_unread() and repos.known_repo_count() == 0
        monkeypatch.setattr(settings, "code_repo", "acme/widget")
        monkeypatch.setattr(settings, "code_org", "acme")
        monkeypatch.setattr(settings, "code_namespace", "code-widget")
        assert repos.known_repo_count() == 1  # the operator's entry, before any read
        page = SimpleNamespace(org="acme", namespace="code-gadget", row_kind="page")
        repos.set_registry_repos([page], frozenset())  # type: ignore[list-item]
        assert not repos.registry_unread() and repos.known_repo_count() == 2
    finally:
        repos.reset_for_tests()
