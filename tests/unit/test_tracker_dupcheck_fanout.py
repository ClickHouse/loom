"""A duplicate check reads every tracker record the caller may read."""

from __future__ import annotations

import json
from typing import Any

import pytest

import server.tracker.app.dupcheck as hd
import server.tracker.app.dupcheck_fanout as fo
from server.settings import settings
from server.tracker.api.models import TrackerDuplicateCheckInput, TrackerDuplicateCheckOutput
from server.tracker.app import common as tcommon

ORG = "clickhouse"
PRIV, PUB = "tracker-example-private", "tracker-clickhouse"


class _Ns:
    def __init__(self, restricted: bool) -> None:
        self.restricted = restricted


def _wire_namespaces(
    monkeypatch, *, readable: set[str], code_readable: set[str] = frozenset()
) -> None:
    ns = {PRIV: _Ns(True), PUB: _Ns(False)}
    monkeypatch.setattr(
        "server.tracker.domain.namespaces.tracker_namespace", lambda org, n: ns.get(n)
    )
    monkeypatch.setattr("server.tracker.domain.namespaces.configured_namespaces", lambda: dict(ns))
    monkeypatch.setattr(
        "server.tracker.app.binding.bindings",
        lambda: {"code-example-private*": PRIV, "code-clickhouse*": PUB},
    )
    monkeypatch.setattr(
        settings,
        "code_repos_json",
        json.dumps(
            [
                {"org": ORG, "namespace": n, "repo": "ClickHouse/ClickHouse"}
                for n in ("code-clickhouse", "code-example-private")
            ]
        ),
    )

    async def _scope(org, n, role=None, *, hides_answer=True):
        return n in readable or n in code_readable

    monkeypatch.setattr("server.auth.authz.row_in_scope", _scope)

    async def _kinds(org):
        return {"code-clickhouse": "code", "code-example-private": "code", "alice": "memory"}

    monkeypatch.setattr("server.identity.app.namespace_kinds.kind_map", _kinds)


@pytest.mark.asyncio
async def test_a_reader_of_both_records_gets_both_bound_first(monkeypatch) -> None:
    _wire_namespaces(monkeypatch, readable={PRIV}, code_readable={"code-clickhouse"})
    # the private record by its own grant; the public one through the repository it is bound to
    assert await fo.readable_tracker_namespaces(ORG, PRIV, [], 4) == [PRIV, PUB]
    assert await fo.readable_tracker_namespaces(ORG, PUB, [], 4) == [PUB, PRIV]
    assert await fo.readable_tracker_namespaces(ORG, PRIV, [], 1) == [PRIV], (
        "cap 1 = the home tracker only"
    )


@pytest.mark.asyncio
async def test_a_restricted_record_needs_its_own_grant(monkeypatch) -> None:
    _wire_namespaces(
        monkeypatch, readable=set(), code_readable={"code-clickhouse", "code-example-private"}
    )
    # reading the private repository does not open the restricted tracker record; the public one opens
    assert await fo.readable_tracker_namespaces(ORG, "", [], 4) == [PUB]


@pytest.mark.asyncio
async def test_requested_namespaces_are_resolved_and_gated_in_order(monkeypatch) -> None:
    _wire_namespaces(monkeypatch, readable={PRIV, PUB})

    async def _bound(org, code_ns, *, hides_answer=True, **_):
        return PUB if code_ns.startswith("code-clickhouse") else ""

    monkeypatch.setattr("server.tracker.app.binding.authorized_tracker_namespace", _bound)
    got = await fo.readable_tracker_namespaces(ORG, PRIV, ["code-clickhouse", "nope", PRIV], 4)
    assert got == [PRIV, PUB], (
        "a code namespace binds, an unknown one is dropped, the bound one stays first"
    )


def _out(verdict: str, items: list[dict[str, Any]], **kw: Any) -> TrackerDuplicateCheckOutput:
    return TrackerDuplicateCheckOutput(
        verdict=verdict, items=items, legs={"vector": "served"}, **kw
    )


def test_merge_is_duplicates_first_then_confidence_with_the_namespace_on_every_item() -> None:
    priv = _out(
        "related",
        [{"number": 7, "title": "p", "relation": "related", "confidence": 0.2}],
        thresholds={"duplicate": 0.36, "related": 0.05},
        undecided=True,
    )
    pub = _out(
        "duplicate_of",
        [
            {"number": 64905, "title": "covers", "relation": "duplicate", "confidence": 0.8},
            {"number": 74155, "title": "lost update", "relation": "related", "confidence": 0.3},
        ],
    )
    m = fo.merge_outputs([PRIV, PUB], [priv, pub], top_k=10)
    assert m.verdict == "duplicate_of" and m.undecided is True and m.epistemic == "calibrated"
    assert [(it["namespace"], it["number"]) for it in m.items] == [
        (PUB, 64905),
        (PUB, 74155),
        (PRIV, 7),
    ]
    assert m.namespaces == [PRIV, PUB] and m.thresholds == {"duplicate": 0.36, "related": 0.05}
    assert m.by_namespace[PUB] == {
        "verdict": "duplicate_of",
        "items": 2,
        "epistemic": "calibrated",
        "undecided": False,
        "legs": {"vector": "served"},
        "tracker_unavailable": False,
    }


def test_merge_epistemic_and_none_shapes() -> None:
    none = _out("none", [])
    failed = fo.merge_outputs([PRIV, PUB], [none, None], 5)
    # a tracker whose check raised did not answer: its silence is not a none
    assert failed.verdict == "none" and failed.epistemic == "degraded"
    down = _out("none", [], epistemic="unavailable", tracker_unavailable=True)
    m = fo.merge_outputs([PRIV, PUB], [down, none], 5)
    # one record answered, the other was down: not clearance
    assert m.epistemic == "degraded" and m.tracker_unavailable is False
    assert m.by_namespace[PRIV]["tracker_unavailable"] is True
    leg_down = _out("none", [], epistemic="degraded")
    assert fo.merge_outputs([PRIV, PUB], [none, leg_down], 5).epistemic == "degraded"
    assert fo.merge_outputs([PRIV, PUB], [none, none], 5).epistemic == "calibrated"
    both_down = fo.merge_outputs([PRIV, PUB], [down, down], 5)
    assert both_down.epistemic == "unavailable" and both_down.tracker_unavailable is True
    uncal = _out(
        "related",
        [{"number": 1, "relation": "related", "confidence": None}],
        epistemic="uncalibrated",
    )
    assert fo.merge_outputs([PRIV, PUB], [none, uncal], 5).epistemic == "uncalibrated"
    nobody = fo.merge_outputs([PRIV], [None], 5)
    assert nobody.epistemic == "unavailable" and nobody.namespaces == [PRIV]


@pytest.mark.asyncio
async def test_handler_fans_out_for_a_code_namespace_and_stays_single_for_a_tracker_one(
    monkeypatch,
) -> None:
    _wire_namespaces(monkeypatch, readable={PRIV, PUB})
    monkeypatch.setattr(settings, "tracker_dupcheck_fanout_namespaces", 4)

    async def _bind(input):
        if input.namespace.startswith("code-"):
            return input.model_copy(update={"namespace": PRIV})
        return input

    monkeypatch.setattr(tcommon, "_bound_input", _bind)
    monkeypatch.setattr(tcommon, "_with_default_org", lambda i: i)
    seen: list[str] = []

    async def _one(input, wall, **_kw):
        seen.append(input.namespace)
        return (
            _out("none", [])
            if input.namespace == PRIV
            else _out("related", [{"number": 3, "relation": "related", "confidence": 0.1}])
        )

    monkeypatch.setattr(hd, "_check_one", _one)
    out = await hd.handle_tracker_duplicate_check(
        TrackerDuplicateCheckInput(org=ORG, namespace="code-example-private", title="x covers y")
    )
    assert seen == [PRIV, PUB] and out.verdict == "related" and out.items[0]["namespace"] == PUB
    seen.clear()
    out = await hd.handle_tracker_duplicate_check(
        TrackerDuplicateCheckInput(org=ORG, namespace=PRIV, title="x")
    )
    assert seen == [PRIV] and out.namespaces == [], (
        "a tracker namespace by name is one read, old shape"
    )
    seen.clear()
    monkeypatch.setattr(settings, "tracker_dupcheck_fanout_namespaces", 1)
    await hd.handle_tracker_duplicate_check(
        TrackerDuplicateCheckInput(org=ORG, namespace="code-example-private", title="x")
    )
    assert seen == [PRIV], "cap 1 restores the single read"


@pytest.mark.asyncio
async def test_a_failing_secondary_namespace_is_dropped_and_a_failing_bound_one_raises(
    monkeypatch,
) -> None:
    _wire_namespaces(monkeypatch, readable={PRIV, PUB})
    monkeypatch.setattr(settings, "tracker_dupcheck_fanout_namespaces", 4)

    async def _bind(input):
        return input.model_copy(update={"namespace": PRIV})

    monkeypatch.setattr(tcommon, "_bound_input", _bind)
    monkeypatch.setattr(tcommon, "_with_default_org", lambda i: i)

    async def _one(input, wall, **_kw):
        if input.namespace == PUB:
            raise RuntimeError("public record down")
        return _out("none", [])

    monkeypatch.setattr(hd, "_check_one", _one)
    out = await hd.handle_tracker_duplicate_check(
        TrackerDuplicateCheckInput(org=ORG, namespace="code-example-private", title="x")
    )
    assert out.verdict == "none" and out.namespaces == [PRIV, PUB] and PUB not in out.by_namespace

    async def _bound_fails(input, wall, **_kw):
        raise RuntimeError("private record down")

    monkeypatch.setattr(hd, "_check_one", _bound_fails)
    with pytest.raises(RuntimeError, match="private record down"):
        await hd.handle_tracker_duplicate_check(
            TrackerDuplicateCheckInput(org=ORG, namespace="code-example-private", title="x")
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("held", ["switched_off", "unread"])
async def test_a_held_secondary_namespace_answers_the_refusal(monkeypatch, held) -> None:
    from server.llm import customer

    error = (
        customer.ModelCallsDisabled("model calls are switched off for org 'clickhouse'")
        if held == "switched_off"
        else customer.SwitchUnread("the model-call switch could not be read")
    )
    _wire_namespaces(monkeypatch, readable={PRIV, PUB})
    monkeypatch.setattr(settings, "tracker_dupcheck_fanout_namespaces", 4)

    async def _bind(input):
        return input.model_copy(update={"namespace": PRIV})

    monkeypatch.setattr(tcommon, "_bound_input", _bind)
    monkeypatch.setattr(tcommon, "_with_default_org", lambda i: i)

    async def _one(input, wall, **_kw):
        if input.namespace == PUB:
            raise error  # the public record's judge or embed is held
        return _out("none", [])

    monkeypatch.setattr(hd, "_check_one", _one)
    with pytest.raises(type(error)):  # not the private record's answer alone
        await hd.handle_tracker_duplicate_check(
            TrackerDuplicateCheckInput(org=ORG, namespace="code-example-private", title="x")
        )


@pytest.mark.asyncio
async def test_a_namespace_with_no_tracker_of_its_own_reads_no_other_repositorys(
    monkeypatch,
) -> None:
    """A checkout whose code namespace no tracker is bound or linked to must not get
    every tracker the caller could read, another repository's included, with their
    `none` read as clearance. It reads none of them and answers `tracker_unavailable`."""
    _wire_namespaces(monkeypatch, readable={PRIV, PUB})
    monkeypatch.setattr(settings, "tracker_dupcheck_fanout_namespaces", 4)

    async def _unbound(input):
        return input

    async def _no_links(org, ns):
        return []

    monkeypatch.setattr(tcommon, "_bound_input", _unbound)
    monkeypatch.setattr(tcommon, "linked_trackers", _no_links)
    monkeypatch.setattr(tcommon, "_with_default_org", lambda i: i)
    seen: list[str] = []

    async def _one(input, wall, **_kw):
        seen.append(input.namespace)
        if input.namespace in (PRIV, PUB):
            return _out("related", [{"number": 3, "relation": "related", "confidence": 0.1}])
        return TrackerDuplicateCheckOutput(
            verdict="none", epistemic="unavailable", tracker_unavailable=True
        )

    monkeypatch.setattr(hd, "_check_one", _one)
    out = await hd.handle_tracker_duplicate_check(
        TrackerDuplicateCheckInput(org=ORG, namespace="code-loom", title="callers false edge")
    )
    assert seen == ["code-loom"] and out.tracker_unavailable and out.items == []
    # naming the trackers still reads them
    seen.clear()
    out = await hd.handle_tracker_duplicate_check(
        TrackerDuplicateCheckInput(org=ORG, namespace="code-loom", title="x", namespaces=[PUB])
    )
    assert seen == [PUB] and out.verdict == "related"


def test_merge_keeps_every_namespaces_leg_rows() -> None:
    a = _out("none", [], leg_status=[{"name": "lexical", "status": "ok", "namespace": PRIV}])
    b = _out(
        "none",
        [],
        leg_status=[
            {"name": "lexical", "status": "degraded", "reason": "timeout", "namespace": PUB}
        ],
    )
    out = fo.merge_outputs([PRIV, PUB], [a, b], 5)
    assert [(r.namespace, r.status) for r in out.leg_status] == [(PRIV, "ok"), (PUB, "degraded")]


@pytest.mark.asyncio
async def test_cancelling_a_fan_out_cancels_every_check_it_started(monkeypatch) -> None:
    """The loom_search tracker leg runs the check under wait_for: its cancel reaches the
    gather over the pool futures, and must not leave the per-namespace checks (and their
    judge / rerank calls) running unowned."""
    import asyncio

    started: list[str] = []
    cancelled: list[str] = []

    async def _one(input, wall, **_kw):
        started.append(input.namespace)
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.append(input.namespace)
            raise

    monkeypatch.setattr(hd, "_check_one", _one)
    inputs = [
        TrackerDuplicateCheckInput(org=ORG, namespace=ns, title="x", rerank=False)
        for ns in (PRIV, PUB)
    ]
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(hd._checked(inputs, None, 5), timeout=0.05)
    await asyncio.sleep(0)
    assert sorted(started) == sorted(cancelled) == sorted([PRIV, PUB])


def _registry_entry(namespace: str) -> Any:
    from server.code.domain.index.repos import RepoConfig

    return RepoConfig(
        org=ORG,
        namespace=namespace,
        repo="Someone/clickhouse",
        git_dir="",
        git_remote="",
        git_branch="main",
        path_prefixes=(),
        github_token="",
        source="registry",
    )


@pytest.mark.asyncio
async def test_a_cleared_link_closes_the_record_the_glob_would_open(monkeypatch) -> None:
    from server.tracker.app import binding

    _wire_namespaces(monkeypatch, readable={PRIV}, code_readable={"code-clickhouse"})
    binding.reset_overrides_for_tests({(ORG, "code-clickhouse"): ""})
    try:
        assert await fo.readable_tracker_namespaces(ORG, PRIV, [], 4) == [PRIV]
    finally:
        binding.reset_overrides_for_tests()


@pytest.mark.asyncio
async def test_a_self_service_repository_matching_the_glob_opens_nothing(monkeypatch) -> None:
    from server.code.domain.index import repos

    _wire_namespaces(monkeypatch, readable={PRIV}, code_readable={"code-clickhouse"})
    # the operator configured a branch the glob links; the readable repository of the
    # same shape was added through self-service, so the glob does not reach it
    monkeypatch.setattr(
        settings,
        "code_repos_json",
        json.dumps(
            [{"org": ORG, "namespace": "code-clickhouse-26-8", "repo": "ClickHouse/ClickHouse"}]
        ),
    )
    monkeypatch.setattr(settings, "code_self_service_enabled", True)
    monkeypatch.setattr(repos, "_registry_namespaces", repos._registry_namespaces)
    monkeypatch.setattr(repos, "_registry_repos", repos._registry_repos)
    repos.set_registry_repos([_registry_entry("code-clickhouse")])
    assert await fo.readable_tracker_namespaces(ORG, PRIV, [], 4) == [PRIV]


@pytest.mark.asyncio
async def test_one_check_reads_configured_repos_once_for_every_tracker(monkeypatch) -> None:
    from server.code.domain.index import repos

    _wire_namespaces(
        monkeypatch, readable=set(), code_readable={"code-clickhouse", "code-example-private"}
    )
    real = repos.configured_repos
    calls: list[int] = []

    def counted() -> list[Any]:
        calls.append(1)
        return real()

    monkeypatch.setattr(repos, "configured_repos", counted)
    assert await fo.readable_tracker_namespaces(ORG, "", [], 4) == [PUB]
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_requested_names_read_configured_repos_once(monkeypatch) -> None:
    from server.code.domain.index import repos

    _wire_namespaces(monkeypatch, readable={PRIV}, code_readable={"code-clickhouse"})
    real = repos.configured_repos
    calls: list[int] = []

    def counted() -> list[Any]:
        calls.append(1)
        return real()

    monkeypatch.setattr(repos, "configured_repos", counted)
    got = await fo.readable_tracker_namespaces(
        ORG, "", ["code-clickhouse", "code-example-private", PRIV], 4
    )
    assert got == [PUB, PRIV]
    assert len(calls) == 1
