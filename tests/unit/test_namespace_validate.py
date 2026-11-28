"""namespace.validate — the integrity report an agent calls after writing.

Each check is one query; `count() OVER ()` rides every sample row so a
check costs one round-trip. A failing check lands in `skipped`, the others
still return. Severity totals drive `healthy`; `strict` folds warnings in.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from server.identity.api.models_namespace_catalog import NamespaceValidateInput
from server.identity.app import namespace_validate as nv
from server.memory.app import code_link_reads
from server.operations import toolsets
from server.transport import mcp_descriptions

_STALE_LINKS = code_link_reads.stale_links  # the real read, before `_no_code_links`
# the checks read from the code links rather than with SQL of their own
_CODE_LINK_CHECKS = ("code_links_stale", "code_rename_edits")


class _CH:
    """Answers each check by the substring that identifies its SQL."""

    def __init__(self, by_marker: dict[str, Any], code_rows: int = 3):
        self.by_marker = by_marker
        self.code_rows = code_rows
        self.sqls: list[str] = []
        self.params: list[dict[str, Any]] = []

    async def query_all(self, sql: str, params: dict[str, Any] | None = None):
        self.sqls.append(sql)
        self.params.append(params or {})
        for marker, rows in self.by_marker.items():
            if marker in sql:
                if isinstance(rows, Exception):
                    raise rows
                return rows
        return []

    async def query_one(self, sql: str, params: dict[str, Any] | None = None):
        self.sqls.append(sql)
        return {"n": self.code_rows}


@pytest.fixture(autouse=True)
def _no_code_links(monkeypatch: pytest.MonkeyPatch) -> None:
    async def stale_links(org: str, namespace: str, keys: Any = None, **kw: Any) -> list[Any]:
        return []

    monkeypatch.setattr("server.memory.app.code_link_reads.stale_links", stale_links)


def _inp(**kw: Any) -> NamespaceValidateInput:
    base = {"org": "o", "namespace": "ns"}
    base.update(kw)
    return NamespaceValidateInput(**base)


@pytest.mark.asyncio
async def test_clean_namespace_is_healthy_and_lists_every_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ch = _CH({})
    monkeypatch.setattr(nv, "get_client", lambda: ch)
    out = await nv.handle_namespace_validate(_inp())
    assert out.healthy is True
    assert (out.errors, out.warnings, out.infos) == (0, 0, 0)
    names = [f.check for f in out.findings]
    assert set(names) == {*nv._CHECKS, *_CODE_LINK_CHECKS, *nv._CODE_UNLINKED}
    assert "unresolved_evidence" in names
    assert all(f.count == 0 and f.samples == [] for f in out.findings)
    assert out.skipped == []
    # the identity join is on the FULL tenancy tuple, never the coarse key
    divergent_sql = next(s for s in ch.sqls if "a.ents != b.ents" in s)
    assert "a.session_id = b.session_id" in divergent_sql
    assert "a.memory_key = b.memory_key" in divergent_sql


@pytest.mark.asyncio
async def test_total_rides_the_sample_rows_and_samples_are_capped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [{"total": 42, "memory_key": f"k{i}", "age_days": 30 - i, "peers": 1} for i in range(5)]
    ch = _CH({"status = 'conflicted'": rows})
    monkeypatch.setattr(nv, "get_client", lambda: ch)
    out = await nv.handle_namespace_validate(_inp(sample_limit=3))
    f = next(x for x in out.findings if x.check == "conflicted_aging")
    assert f.count == 42
    assert len(f.samples) == 3
    assert "total" not in f.samples[0]
    assert f.samples[0]["memory_key"] == "k0"
    assert f.severity == "warning"
    # warnings are advisory by default…
    assert out.warnings == 42 and out.errors == 0 and out.healthy is True
    # …and the sample limit was passed to ClickHouse, not applied only client-side
    assert all(p.get("lim") == 3 for p in ch.params if "lim" in p)


@pytest.mark.asyncio
async def test_error_severity_makes_namespace_unhealthy(monkeypatch: pytest.MonkeyPatch) -> None:
    ch = _CH(
        {
            "a.ents != b.ents": [
                {
                    "total": 2,
                    "memory_key": "x",
                    "memory_entities": ["a"],
                    "embedding_entities": ["b"],
                }
            ]
        }
    )
    monkeypatch.setattr(nv, "get_client", lambda: ch)
    out = await nv.handle_namespace_validate(_inp())
    assert out.errors == 2 and out.healthy is False
    f = next(x for x in out.findings if x.check == "divergent_entities")
    assert f.severity == "error" and "memory.update_by_id" in f.detail


@pytest.mark.asyncio
async def test_strict_folds_warnings_into_healthy(monkeypatch: pytest.MonkeyPatch) -> None:
    ch = _CH(
        {"valid_to < now()": [{"total": 1, "memory_key": "k", "valid_to": None, "days_past": 3}]}
    )
    monkeypatch.setattr(nv, "get_client", lambda: ch)
    assert (await nv.handle_namespace_validate(_inp())).healthy is True
    assert (await nv.handle_namespace_validate(_inp(strict=True))).healthy is False


@pytest.mark.asyncio
async def test_one_failing_check_is_skipped_not_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    ch = _CH({"a.created_at AS created_at": RuntimeError("boom")})
    monkeypatch.setattr(nv, "get_client", lambda: ch)
    out = await nv.handle_namespace_validate(_inp())
    assert out.skipped == ["orphan_vectors: RuntimeError"]
    assert "orphan_vectors" not in [f.check for f in out.findings]
    # every other check, the two read from the code links and the unlinked memories by cause
    others = len(nv._CHECKS) - 1
    assert len(out.findings) == others + len(_CODE_LINK_CHECKS) + len(nv._CODE_UNLINKED)
    assert out.healthy is True


@pytest.mark.asyncio
async def test_tunables_reach_clickhouse_as_parameters(monkeypatch: pytest.MonkeyPatch) -> None:
    ch = _CH({})
    monkeypatch.setattr(nv, "get_client", lambda: ch)
    await nv.handle_namespace_validate(
        _inp(conflict_age_seconds=3600, confidence_floor=0.25, embed_grace_seconds=30)
    )
    p = ch.params[0]
    assert (p["age"], p["floor"], p["grace"]) == (3600, 0.25, 30)
    # no interpolation of caller values into SQL text
    assert all("3600" not in s and "0.25" not in s for s in ch.sqls)


def test_registered_on_the_all_listing_with_a_description() -> None:
    from server.operations.registry import OPERATIONS_BY_NAME

    op = OPERATIONS_BY_NAME["namespace.validate"]
    assert op.idempotent is True and op.audit is False
    assert op.required_role.name == "VIEWER"
    assert "all" in toolsets.op_toolsets("namespace.validate")
    assert "healthy" in mcp_descriptions.TOOL_DESCRIPTIONS["namespace.validate"]


def test_every_check_names_its_remedy() -> None:
    remedies = (
        "memory.",
        "conflicts.",
        "dashboard",
        "Re-anchor",
        "expire",
        "retention",
        "tombstone",
    )
    for name, (severity, detail, sql) in nv._CHECKS.items():
        assert severity in {"error", "warning", "info"}, name
        assert any(r in detail for r in remedies), name
        assert "{org:String}" in sql and "{ns:String}" in sql and "{lim:UInt32}" in sql, name
        assert "count() OVER ()" in sql, name


@pytest.mark.asyncio
async def test_stale_verification_is_a_warning_naming_memory_verify(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ch = _CH(
        {
            "verified_sha256 != content_sha256": [
                {
                    "total": 2,
                    "memory_key": "deploy_window",
                    "verified_by": "human:alice",
                    "verified_at": None,
                    "attested_days_ago": 4,
                },
            ]
        }
    )
    monkeypatch.setattr(nv, "get_client", lambda: ch)
    out = await nv.handle_namespace_validate(_inp())
    f = next(x for x in out.findings if x.check == "stale_verification")
    assert f.severity == "warning" and f.count == 2 and "memory.verify" in f.detail
    sql = next(s for s in ch.sqls if "verified_sha256 != content_sha256" in s)
    # an attestation whose hash cannot be matched is stale, never trusted
    assert "verified_sha256 = ''" in sql and "content_sha256 IN ('', '0')" in sql
    assert out.warnings == 2 and out.healthy is True


def test_unanchored_time_cue_is_an_info_finding_that_names_the_reanchor_op() -> None:
    """A fact whose text says 'last week' or 'in 2019' but carries no
    temporal_anchor is dated by the session it was said in; the report names
    it and the op that re-anchors it."""
    severity, detail, sql = nv._CHECKS["unanchored_time_cue"]
    assert severity == "info"
    assert "memory.update_by_id" in detail and "temporal_anchor" in detail
    assert "temporal_anchor IS NULL" in sql and "{cue:String}" in sql
    assert "status = 'active'" in sql
    import re

    cue = re.compile(nv._TIME_CUE_RE)
    assert cue.search("nate watched little women recently")
    assert cue.search("joanna moved in 2019")
    assert cue.search("they met last friday") and cue.search("adopted 3 weeks ago")
    assert not cue.search("caroline loves painting and long walks")


def test_divergent_status_joins_on_the_full_identity_and_names_the_reconcile() -> None:
    """A status compared per key across
    identities would retire live rows; the check joins on the full tuple."""
    severity, detail, sql = nv._CHECKS["divergent_status"]
    assert severity == "error"
    assert "reconcile-status" in detail and "memory.reindex" in detail
    assert "argMax(status, version)" in sql and "m.ms != e.es" in sql
    assert f"USING ({nv._IDENT})" in sql and "session_id" in nv._IDENT


# ---------------------------------------------------------------------------
# A report scoped to the keys one session wrote.
# ---------------------------------------------------------------------------


def test_every_check_carries_the_keys_predicate() -> None:
    """One predicate, bound once, on every check that reads memory rows or
    embedding rows — so `keys` scopes the whole report."""
    for name, (_sev, _detail, sql) in nv._CHECKS.items():
        assert "{keys:Array(String)}" in sql, f"{name} is not scoped by keys"
        assert "empty({keys:Array(String)}) OR memory_key IN {keys:Array(String)}" in sql, name


@pytest.mark.asyncio
async def test_keys_reach_clickhouse_and_are_echoed(monkeypatch: pytest.MonkeyPatch) -> None:
    ch = _CH({})
    monkeypatch.setattr(nv, "get_client", lambda: ch)
    out = await nv.handle_namespace_validate(_inp(keys=["deploy_window", "db_engine"]))
    assert out.keys_scoped == 2
    assert all(p.get("keys") == ["deploy_window", "db_engine"] for p in ch.params)
    # the unscoped report binds an empty array (ClickHouse folds `empty([])`)
    ch2 = _CH({})
    monkeypatch.setattr(nv, "get_client", lambda: ch2)
    out2 = await nv.handle_namespace_validate(_inp())
    assert out2.keys_scoped == 0
    assert all(p.get("keys") == [] for p in ch2.params)


def test_keys_are_bounded_to_two_hundred() -> None:
    from pydantic import ValidationError

    _inp(keys=[f"k{i}" for i in range(200)])
    with pytest.raises(ValidationError):
        _inp(keys=[f"k{i}" for i in range(201)])


@pytest.mark.asyncio
async def test_stale_code_links_count_memories_and_sample_the_links_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def link(key: str) -> dict[str, Any]:
        return {
            "memory_key": key,
            "memory_id": f"00000000-0000-0000-0000-0000000000{len(key):02d}",
            "qualified_name": f"m::{key}",
            "path": "src/m.py",
            "state": "missing",
            "changed_since": "c2",
            "total": 2,
        }

    asked: list[Any] = []

    async def stale_links(
        org: str, namespace: str, keys: Any = None, *, limit: int, states: Any = ()
    ) -> Any:
        if "moved" in states:
            return []  # the rename check's read
        asked.append((keys, limit))
        return [link("new")]

    monkeypatch.setattr("server.memory.app.code_link_reads.stale_links", stale_links)
    monkeypatch.setattr(nv, "get_client", lambda: _CH({}))
    out = await nv.handle_namespace_validate(_inp(keys=["old", "new"], sample_limit=1))
    stale = next(f for f in out.findings if f.check == "code_links_stale")
    assert stale.count == 2 and [s["memory_key"] for s in stale.samples] == ["new"]
    await nv.handle_namespace_validate(_inp())
    assert asked == [(["old", "new"], 1), (None, 5)]


def test_every_check_declares_its_kind_version_and_uncertainty() -> None:
    assert set(nv._CONTRACT) == {*nv._CHECKS, *_CODE_LINK_CHECKS, *nv._CODE_UNLINKED}
    assert {r.kind for r in nv._CONTRACT.values()} == {
        "contract_violation",
        "usage_heuristic",
        "system_state",  # a memory waiting for the link pass or the index
    }
    assert all(r.version >= 1 for r in nv._CONTRACT.values())
    # Every heuristic says why a hit may still be a legitimate design.
    assert all(r.uncertainty for r in nv._CONTRACT.values() if r.kind == "usage_heuristic")
    # Every error-severity check is a broken stored invariant, never a heuristic.
    for name, (severity, _detail, _sql) in nv._CHECKS.items():
        if severity == "error":
            assert nv._CONTRACT[name].kind == "contract_violation", name


@pytest.mark.asyncio
async def test_findings_carry_the_contract_and_cite_the_live_revisions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    revision = {
        "memory_key": "k1",
        "id": "00000000-0000-0000-0000-000000000001",
        "version": 7,
        "sensitivity": "",
        "value_len": 10,
        "redacted": False,
        "agent": "a",
        "user_id": "-",
        "session_id": "-",
        "memory_type": "semantic",
        "source_class": "tool",
        "updated_at": datetime(2026, 1, 1),
    }
    ch = _CH(
        {
            "verified_sha256 != content_sha256": [
                {"total": 2, "memory_key": "k1", "memory_id": revision["id"]},
                {
                    "total": 2,
                    "memory_key": "gone",
                    "memory_id": "00000000-0000-0000-0000-0000000000ff",
                },
            ],
            "IN {identities:Array(Tuple(String, String, String, String))}": [revision],
        }
    )
    monkeypatch.setattr(nv, "get_client", lambda: ch)
    out = await nv.handle_namespace_validate(_inp(sample_limit=5))
    f = next(f for f in out.findings if f.check == "stale_verification")
    assert (f.kind, f.rule_version, f.source_op) == ("contract_violation", 1, "namespace.validate")
    assert f.samples_complete is True and f.sample_limit == 5 and f.window_seconds is None
    # A sampled row no longer live is not cited.
    assert [(e.memory_key, e.version) for e in f.evidence] == [("k1", 7)]
    assert out.coverage.complete and "stale_verification" in out.coverage.rules_run


@pytest.mark.asyncio
async def test_no_samples_asked_still_counts_and_is_not_a_complete_listing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ch = _CH({"verified_sha256 != content_sha256": [{"total": 4, "memory_key": "k1"}]})
    monkeypatch.setattr(nv, "get_client", lambda: ch)
    out = await nv.handle_namespace_validate(_inp(sample_limit=0))
    stale = next(f for f in out.findings if f.check == "stale_verification")
    assert stale.count == 4 and stale.samples == [] and stale.samples_complete is False
    assert all(p["lim"] == 1 for p in ch.params if "lim" in p)


def test_the_time_cue_pattern_is_pinned_to_its_rule_version() -> None:
    # Editing the pattern without raising the check's version fails here.
    assert nv._CONTRACT["unanchored_time_cue"].version == 1
    assert nv._TIME_CUE_RE == (
        r"\b(yesterday|tomorrow|tonight|recently|last (week|month|year|weekend|night|"
        r"monday|tuesday|wednesday|thursday|friday|saturday|sunday)|next (week|month|year)|"
        r"\d+ (days?|weeks?|months?|years?) ago|(in|since|by|on) (january|february|march|"
        r"april|may|june|july|august|september|october|november|december)\b|"
        r"\b(19|20)\d\d)\b"
    )


@pytest.mark.asyncio
async def test_an_unpublished_projection_reads_on_and_marks_the_report_incomplete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Under Postgres, a wait for ClickHouse to publish the namespace's commits that runs
    out does not fail the report: it runs every check and names the projection in
    `skipped` and `coverage`."""
    from server.memory.app.pg_admission import ProjectionBehindError
    from server.memory.app.search import pg_validation

    async def behind(*_: object) -> bool:
        raise ProjectionBehindError()

    monkeypatch.setattr(pg_validation, "admit", behind)
    monkeypatch.setattr(nv, "get_client", lambda: _CH({}))
    out = await nv.handle_namespace_validate(_inp())
    assert {*nv._CHECKS} <= set(out.coverage.rules_run)
    assert out.coverage.rules_skipped == ["projection"] and not out.coverage.complete
    assert [s.split(": ")[:2] for s in out.skipped] == [["projection", "PROJECTION_BEHIND"]]


@pytest.mark.asyncio
async def test_stale_code_links_are_named_skipped_while_the_code_registry_is_unread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.code.domain.index import repos
    from server.settings import settings

    monkeypatch.setattr(settings, "code_self_service_enabled", True)
    monkeypatch.setattr(repos, "_registry_loaded", False)
    monkeypatch.setattr(code_link_reads, "stale_links", _STALE_LINKS)
    monkeypatch.setattr(nv, "get_client", lambda: _CH({}))
    out = await nv.handle_namespace_validate(_inp())
    # every check that reads which repositories are configured
    assert out.skipped == [
        f"{check}: RegistryUnread" for check in (*_CODE_LINK_CHECKS, *nv._CODE_UNLINKED)
    ]
    assert out.coverage.complete is False


@pytest.mark.asyncio
async def test_unlinked_code_memories_are_counted_by_cause_from_one_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A memory that names code and holds no link reads exactly as one whose code is
    current; the report counts them by why, as information: `info` never blocks a stop
    nor makes a namespace unhealthy, even under `strict`."""
    from server.memory.app import code_unlinked

    asked: list[Any] = []

    def memory(n: int, **more: str) -> dict[str, str]:
        return {"memory_key": f"k{n}", "memory_id": f"00000000-0000-0000-0000-{n:012d}", **more}

    async def unlinked(org: str, namespace: str, keys: Any = None) -> dict[str, list[Any]]:
        asked.append((org, namespace, keys))
        return {
            code_unlinked.NO_REPOSITORY: [memory(1), memory(2), memory(3)],
            code_unlinked.FILE_NOT_INDEXED: [memory(4)],
            code_unlinked.NAME_UNRESOLVED: [],
            code_unlinked.PENDING: [memory(5, waits_for="link_pass")],
        }

    monkeypatch.setattr(code_unlinked, "unlinked", unlinked)
    monkeypatch.setattr(nv, "get_client", lambda: _CH({}))
    out = await nv.handle_namespace_validate(_inp(keys=["k1"], sample_limit=2, strict=True))
    by_check = {f.check: f for f in out.findings}
    assert {name: by_check[name].count for name in nv._CODE_UNLINKED} == {
        "code_unlinked_no_repository": 3,
        "code_unlinked_file_not_indexed": 1,
        "code_unlinked_name_unresolved": 0,
        "code_unlinked_pending": 1,
    }
    no_repository = by_check["code_unlinked_no_repository"]
    assert [s["memory_key"] for s in no_repository.samples] == ["k1", "k2"]
    assert no_repository.samples_complete is False and "total" not in no_repository.samples[0]
    assert by_check["code_unlinked_pending"].samples == [memory(5, waits_for="link_pass")]
    assert by_check["code_unlinked_pending"].kind == "system_state"
    assert all(by_check[name].severity == "info" for name in nv._CODE_UNLINKED)
    assert (out.infos, out.warnings, out.errors, out.healthy) == (5, 0, 0, True)
    # one read answers the four causes, scoped to the keys asked for
    assert asked == [("o", "ns", ["k1"])]
    await nv.handle_namespace_validate(_inp())
    assert asked[-1] == ("o", "ns", None)


@pytest.mark.asyncio
async def test_a_failed_unlinked_read_skips_its_four_checks_and_no_other(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.app import code_unlinked

    reads = 0

    async def unlinked(org: str, namespace: str, keys: Any = None) -> dict[str, list[Any]]:
        nonlocal reads
        reads += 1
        raise TimeoutError("read timed out")

    monkeypatch.setattr(code_unlinked, "unlinked", unlinked)
    monkeypatch.setattr(nv, "get_client", lambda: _CH({}))
    out = await nv.handle_namespace_validate(_inp())
    assert out.skipped == [f"{check}: TimeoutError" for check in nv._CODE_UNLINKED]
    assert reads == 1 and {f.check for f in out.findings} == {*nv._CHECKS, *_CODE_LINK_CHECKS}


@pytest.mark.asyncio
async def test_rename_edits_are_listed_with_their_totals_by_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A memory whose link followed a rename is listed with the edits its text allows,
    or the reason it gets none; the totals count every memory read, not only the
    samples. Information: it never makes a namespace unhealthy, even under `strict`."""
    from server.memory.app import code_renames

    asked: list[Any] = []
    edit = {"start": 1, "end": 10, "old": "read_rows", "new": "fetch_rows"}

    def memory(n: int, **more: Any) -> dict[str, Any]:
        return {"memory_key": f"k{n}", "memory_id": f"00000000-0000-0000-0000-{n:012d}", **more}

    found = [
        memory(1, edits=[edit], content_sha256="a" * 64),
        memory(2, reason="prose_occurrence"),
        memory(3, reason="old_name_still_defined"),
        memory(4, reason="prose_occurrence"),
    ]

    async def planned(org: str, namespace: str, keys: Any = None) -> list[dict[str, Any]]:
        asked.append((org, namespace, keys))
        return found

    monkeypatch.setattr(code_renames, "planned", planned)
    monkeypatch.setattr(nv, "get_client", lambda: _CH({}))
    out = await nv.handle_namespace_validate(_inp(keys=["k1"], sample_limit=2, strict=True))
    renames = next(f for f in out.findings if f.check == "code_rename_edits")
    assert renames.count == 4 and renames.samples == found[:2]
    assert renames.samples_complete is False and renames.severity == "info"
    assert renames.totals == {"edits": 1, "old_name_still_defined": 1, "prose_occurrence": 2}
    assert (out.infos, out.warnings, out.errors, out.healthy) == (4, 0, 0, True)
    assert "memory.update" in renames.detail and "if_match_sha256" in renames.detail
    assert asked == [("o", "ns", ["k1"])]
    # no sample asked for: every memory is still counted, by outcome
    out = await nv.handle_namespace_validate(_inp(sample_limit=0))
    renames = next(f for f in out.findings if f.check == "code_rename_edits")
    assert (renames.count, renames.samples) == (4, []) and sum(renames.totals.values()) == 4
    assert asked[-1] == ("o", "ns", None)
    # a check with no classes of its own carries no totals
    assert all(f.totals == {} for f in out.findings if f.check != "code_rename_edits")


@pytest.mark.asyncio
async def test_a_failed_rename_read_skips_its_check_and_no_other(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.app import code_renames

    async def planned(org: str, namespace: str, keys: Any = None) -> list[dict[str, Any]]:
        raise TimeoutError("read timed out")

    monkeypatch.setattr(code_renames, "planned", planned)
    monkeypatch.setattr(nv, "get_client", lambda: _CH({}))
    out = await nv.handle_namespace_validate(_inp())
    assert out.skipped == ["code_rename_edits: TimeoutError"]
    assert "code_rename_edits" not in {f.check for f in out.findings}


@pytest.mark.asyncio
async def test_rename_edits_list_nothing_of_a_repository_the_caller_may_not_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The listing starts from the links into repositories the caller may read: with
    none readable, no memory, link or index row is read and nothing is listed."""
    from server.memory.app import code_renames

    async def none_readable(org: str, namespaces: set[str]) -> set[str]:
        return set()

    async def unread(*args: Any, **kw: Any) -> Any:
        raise AssertionError("nothing is read of an unreadable repository")

    monkeypatch.setattr(code_link_reads, "stale_links", _STALE_LINKS)
    monkeypatch.setattr(code_link_reads.code, "trunk_namespace", lambda org, ns: "secret-code")
    monkeypatch.setattr(code_link_reads, "readable_namespaces", none_readable)
    for name in ("stale_of_served", "memory_heads", "for_memories"):
        monkeypatch.setattr(code_renames.store, name, unread)
    monkeypatch.setattr(code_renames.code, "candidates", unread)
    monkeypatch.setattr(nv, "get_client", lambda: _CH({}))
    out = await nv.handle_namespace_validate(_inp())
    renames = next(f for f in out.findings if f.check == "code_rename_edits")
    assert (renames.count, renames.samples, renames.totals) == (0, [], {})
