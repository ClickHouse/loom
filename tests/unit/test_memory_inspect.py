"""memory.inspect — one user, agent or session's rows, kept apart from the rest.

The `own` and `others` views split the namespace on the subject's identity column; the
column is one of three fixed names and the id is bound; the unset sentinel names no one;
the activity counts leave out inspector calls and say when the ledger keeps less than
the window; the extraction policy is read only for an org admin; a failed section is
reported in `skipped`, and a view whose counts failed is not complete; the excerpt
comes from the listed rows' previews; nothing is written.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest
from pydantic import ValidationError

from server.memory.api.models_inspect import MemoryInspectInput
from server.memory.app import inspect as mi
from server.memory.store.inspect import excerpt
from server.operations.registry import OPERATIONS_BY_NAME
from tests.unit.ch_fake import RecordingCH

T0, T1 = datetime(2026, 1, 1), datetime(2026, 1, 2)
MID = "00000000-0000-0000-0000-000000000001"


def _counts(sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    own = " = {id:String}" in sql
    return [
        {
            "memory_type": "semantic",
            "status": "active",
            "source_class": "asserted",
            "rows": 2 if own else 5,
            "first_at": T0,
            "last_at": T1,
        },
        {
            "memory_type": "episodic",
            "status": "superseded",
            "source_class": "derived",
            "rows": 1,
            "first_at": T0,
            "last_at": T0,
        },
    ]


def _newest(sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "memory_key": "deploy/window",
            "id": MID,
            "version": 7,
            "memory_type": "semantic",
            "status": "active",
            "source_class": "asserted",
            "agent": "bot",
            "user_id": "u1",
            "session_id": "-",
            "actor": "process:bot",
            "updated_at": T1,
            "sensitivity": "",
        }
    ]


def _ch(
    tags: Any = None, counts: Any = _counts, retention: Any = 0, newest: Any = _newest
) -> RecordingCH:
    retention_rows = retention if callable(retention) else [{"retention_days": retention}]
    return (
        RecordingCH()
        .on("FROM loom.namespaces FINAL", retention_rows)
        .on("GROUP BY memory_type, status, source_class", counts)
        .on("arrayJoin(tags)", tags or [{"tag": "deploy", "rows": 2}])
        .on("AS item_count", [{"category_id": "c1", "item_count": 1}])
        .on("FROM loom.memory_categories FINAL", [{"category_id": "c1", "rows": 1}])
        .on("ORDER BY updated_at DESC", newest)
        .on(
            "value_preview",
            [{"id": MID, "version": 7, "value_preview": '"Deploys on Tuesdays."', "cut": False}],
        )
        .on("FROM loom.memory_versions", [{"id": MID, "versions": 3}])
        .on(
            "FROM loom.agent_events",
            [
                {
                    "operation": "memory.search",
                    "calls": 4,
                    "succeeded": 3,
                    "first_at": T0,
                    "last_at": T1,
                }
            ],
        )
        .on("FROM loom.namespace_guides", [])
        .on(
            "FROM loom.categories FINAL",
            [{"category_id": "c1", "name": "decisions", "description": ""}],
        )
    )


@pytest.fixture(autouse=True)
def cited(monkeypatch: pytest.MonkeyPatch) -> list[tuple[list[tuple[str, ...]], Any]]:
    """The reports' citation read: the hash memory.get serves for version 7 of each row,
    named by its whole identity. Each call's refs and scope are kept."""
    calls: list[tuple[list[tuple[str, ...]], Any]] = []

    async def revisions_for(org: str, ns: str, samples: Any, *, scope: Any) -> Any:
        refs = [tuple(s[c] for c in mi.IDENTITY) for s in samples]
        calls.append((refs, scope))
        return {ref: {"version": 7, "content_sha256": "a" * 64} for ref in refs}

    monkeypatch.setattr(mi, "revisions_for", revisions_for)
    return calls


@pytest.fixture
def ch(monkeypatch: pytest.MonkeyPatch) -> RecordingCH:
    fake = _ch()
    fake.strict = False
    return fake.install(monkeypatch, "server.identity.app.namespace_catalog")


def _inp(**kw: Any) -> MemoryInspectInput:
    return MemoryInspectInput(
        **{"org": "o", "namespace": "ns", "subject": "user", "id": "u1", **kw}
    )


@pytest.mark.parametrize("sentinel", ["-", "*", " ", ""])
def test_the_unset_sentinel_names_no_one(sentinel: str) -> None:
    with pytest.raises(ValidationError):
        _inp(id=sentinel)


def test_registered_read_only_for_viewers() -> None:
    op = OPERATIONS_BY_NAME["memory.inspect"]
    assert op.read_only and op.required_role.name == "VIEWER"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("subject", "column"), [("user", "user_id"), ("agent", "agent"), ("session", "session_id")]
)
async def test_own_and_others_split_on_the_subjects_column(
    ch: RecordingCH, subject: str, column: str
) -> None:
    out = await mi.handle_memory_inspect(_inp(subject=subject, id="x1"))
    assert out.own.live_rows == 3 and out.others.live_rows == 6
    assert out.own.definition == mi._DEFINITIONS[subject][0]
    assert out.others.definition == mi._DEFINITIONS[subject][1]
    reads = [s for s in ch.statements("FROM loom.memory FINAL") if "memory_categories" not in s.sql]
    assert reads
    for s in reads:
        assert f"{column} = {{id:String}}" in s.sql or f"{column} != {{id:String}}" in s.sql
        assert s.params["id"] == "x1" and "x1" not in s.sql
    calls = ch.statements("FROM loom.agent_events")[0]
    assert f"{column} = {{id:String}}" in calls.sql


@pytest.mark.asyncio
async def test_a_view_counts_lists_and_cites(ch: RecordingCH, cited: list[Any]) -> None:
    out = await mi.handle_memory_inspect(_inp(sample_limit=1))
    own = out.own
    assert [(c.name, c.rows) for c in own.by_type] == [("semantic", 2), ("episodic", 1)]
    assert [(c.name, c.rows) for c in own.by_status] == [("active", 2), ("superseded", 1)]
    assert [(c.name, c.rows) for c in own.top_tags] == [("deploy", 2)]
    assert [(c.name, c.rows) for c in own.categories] == [("decisions", 1)]
    assert out.settings.categories_registered == 1 and out.skipped == []
    assert (own.first_updated_at, own.last_updated_at) == (T0, T1)
    row = own.newest[0]
    assert (row.memory_id, row.version, row.versions, row.actor) == (MID, 7, 3, "process:bot")
    # The hash is the one memory.get serves, from the reports' citation read, which names
    # the row by its whole identity (the sort key), not by id.
    assert row.content_sha256 == "a" * 64 and row.sensitivity == ""
    # The citation read takes the request's scope (the whole namespace with no auth bound).
    from server.identity.domain.read_scope import WHOLE_NAMESPACE

    assert cited[0] == ([("bot", "u1", "-", "deploy/window")], WHOLE_NAMESPACE)
    # The excerpt is the decoded preview of the listed version, read for that row only.
    assert row.excerpt == "Deploys on Tuesdays."
    previews = ch.statements("value_preview")
    assert len(previews) == 2  # one per view
    for preview in previews:
        assert preview.params["pairs"] == [(MID, 7)] and "FINAL" not in preview.sql
    # One row listed of three: not a complete listing.
    assert own.complete is False
    assert not ch.inserted("memory") and not ch.statements(kind="execute")


@pytest.mark.asyncio
async def test_no_samples_asked_reads_no_rows(ch: RecordingCH) -> None:
    out = await mi.handle_memory_inspect(_inp(sample_limit=0))
    assert out.own.newest == [] and out.own.complete is False
    assert not ch.statements("ORDER BY updated_at DESC")


@pytest.mark.asyncio
async def test_activity_leaves_out_inspectors_and_names_the_retention_gap(
    ch: RecordingCH, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "retention_agent_events_days", 3)
    out = await mi.handle_memory_inspect(_inp(window_seconds=7 * 86400))
    assert [(c.operation, c.calls, c.succeeded) for c in out.activity.calls] == [
        ("memory.search", 4, 3)
    ]
    assert "keeps 3 days" in out.activity.gap
    s = ch.statements("FROM loom.agent_events")[0]
    assert s.params["skip"] == ["loom-ui", "loom-assistant"]

    monkeypatch.setattr(settings, "retention_agent_events_days", 0)
    assert (await mi.handle_memory_inspect(_inp())).activity.gap == ""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("own", "deployment", "gap"),
    [(3, 0, "keeps 3 days"), (30, 3, ""), (0, 3, "keeps 3 days")],
)
async def test_the_namespaces_own_retention_wins_for_the_gap(
    monkeypatch: pytest.MonkeyPatch, own: int, deployment: int, gap: str
) -> None:
    from server.settings import settings

    fake = _ch(retention=own)
    fake.strict = False
    fake.install(monkeypatch, "server.identity.app.namespace_catalog")
    monkeypatch.setattr(settings, "retention_agent_events_days", deployment)
    out = await mi.handle_memory_inspect(_inp(window_seconds=7 * 86400))
    assert (gap in out.activity.gap) if gap else out.activity.gap == ""


@pytest.mark.asyncio
async def test_an_unread_retention_claims_no_gap(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.settings import settings

    fake = _ch(retention=_boom)
    fake.strict = False
    fake.install(monkeypatch, "server.identity.app.namespace_catalog")
    monkeypatch.setattr(settings, "retention_agent_events_days", 3)
    out = await mi.handle_memory_inspect(_inp(window_seconds=7 * 86400))
    assert out.activity.gap == "" and "activity.retention: RuntimeError" in out.skipped


@pytest.mark.asyncio
async def test_the_extraction_policy_is_read_only_for_an_org_admin(
    ch: RecordingCH, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def policy(org: str, namespace: str) -> str:
        return "Never store card numbers."

    monkeypatch.setattr("server.memory.app.extraction_policy.effective", policy)

    async def admin(org: str, ns: str, role: Any) -> bool:
        assert role.name == "ORG_ADMIN"
        return True

    monkeypatch.setattr(mi, "row_in_scope", admin)
    out = await mi.handle_memory_inspect(_inp())
    assert out.settings.extraction_policy == "Never store card numbers."

    async def viewer(*a: Any, **k: Any) -> bool:
        return False

    monkeypatch.setattr(mi, "row_in_scope", viewer)
    out = await mi.handle_memory_inspect(_inp())
    assert out.settings.extraction_policy is None
    assert "org admin" in out.settings.extraction_policy_unavailable
    assert "no user, agent or session setting" in out.settings.extraction_instructions


@pytest.mark.asyncio
async def test_an_admin_whose_policy_read_fails_is_told_so(
    ch: RecordingCH, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def admin(*a: Any, **k: Any) -> bool:
        return True

    async def unreadable_policy(org: str, namespace: str) -> str:
        raise RuntimeError("policy read failed")

    monkeypatch.setattr(mi, "row_in_scope", admin)
    monkeypatch.setattr("server.memory.app.extraction_policy.effective", unreadable_policy)
    out = await mi.handle_memory_inspect(_inp())
    assert out.settings.extraction_policy is None
    assert out.settings.extraction_policy_unavailable == "the policy could not be read"
    assert "settings.extraction_policy: RuntimeError" in out.skipped


@pytest.mark.asyncio
async def test_an_unreadable_grant_withholds_the_policy_and_says_so(
    ch: RecordingCH, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.auth import authz, fail_closed
    from server.auth.context import set_current_auth
    from server.auth.jwt import AuthInfo
    from server.core import request_work
    from server.operations.base import Role

    async def unreadable_grants(*a: Any, **k: Any) -> None:
        # What check_authorized does when the grant read fails: note it, then refuse.
        fail_closed.note_unreadable()
        e = PermissionError("grants unreadable")
        e.unverified = True  # type: ignore[attr-defined]
        raise e

    monkeypatch.setattr(authz, "check_authorized", unreadable_grants)
    set_current_auth(
        AuthInfo(
            subject="a", org="o", namespaces=("ns",), roles=(Role.VIEWER,), is_service_account=False
        )
    )
    request_work.begin()
    out = await mi.handle_memory_inspect(_inp())
    assert out.settings.extraction_policy is None
    assert out.settings.extraction_policy_unavailable == "the caller's access could not be read"
    # The refusal it could not verify marks the request the server's.
    assert fail_closed.answered_unverified()
    assert "settings.extraction_policy: the caller's access was unreadable" in out.skipped


@pytest.mark.asyncio
async def test_a_failed_read_noted_earlier_is_not_blamed_on_the_admin_check(
    ch: RecordingCH, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.auth import authz, fail_closed
    from server.auth.context import set_current_auth
    from server.auth.jwt import AuthInfo
    from server.core import request_work
    from server.operations.base import Role

    async def not_an_admin(*a: Any, **k: Any) -> None:
        raise PermissionError("not an org admin")

    monkeypatch.setattr(authz, "check_authorized", fail_closed.marks_unverified(not_an_admin))
    set_current_auth(
        AuthInfo(
            subject="a", org="o", namespaces=("ns",), roles=(Role.VIEWER,), is_service_account=False
        )
    )
    request_work.begin()
    # An earlier check in the request noted a failed read and still let the caller in.
    fail_closed.note_unreadable()
    out = await mi.handle_memory_inspect(_inp())
    assert "only an org admin" in out.settings.extraction_policy_unavailable
    assert not any(s.startswith("settings.extraction_policy") for s in out.skipped)
    assert not fail_closed.answered_unverified()


@pytest.mark.asyncio
async def test_a_high_sensitivity_row_lists_no_excerpt_and_no_hash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def high(sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        return [{**_newest(sql, params)[0], "sensitivity": "high"}]

    async def withheld(org: str, ns: str, samples: Any, *, scope: Any) -> Any:
        # live_revisions gives a high-sensitivity row no hash.
        return {
            tuple(s[c] for c in mi.IDENTITY): {"version": 7, "content_sha256": ""} for s in samples
        }

    fake = _ch(newest=high)
    fake.strict = False
    fake.install(monkeypatch, "server.identity.app.namespace_catalog")
    monkeypatch.setattr(mi, "revisions_for", withheld)
    out = await mi.handle_memory_inspect(_inp(sample_limit=1))
    (row,) = out.own.newest
    assert row.memory_id == MID and row.excerpt == "" and row.content_sha256 == ""
    # No preview is read for it.
    assert not fake.statements("value_preview")


@pytest.mark.asyncio
async def test_the_listed_rows_are_recorded_in_the_exposure_ledger(
    ch: RecordingCH, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows: list[dict[str, Any]] = []
    monkeypatch.setattr(mi, "record_exposure", lambda **kw: rows.append(kw))
    await mi.handle_memory_inspect(_inp(sample_limit=1))
    (row,) = rows
    assert row["operation"] == "memory.inspect" and row["keys"] == ["deploy/window"] * 2
    assert (row["org"], row["namespace"], row["top_k"]) == ("o", "ns", 1)

    # Nothing listed: no keys, which the ledger writes no row for.
    rows.clear()
    await mi.handle_memory_inspect(_inp(sample_limit=0))
    assert [r["keys"] for r in rows] == [[]]


@pytest.mark.asyncio
async def test_a_row_revised_between_reads_is_listed_without_a_hash(
    ch: RecordingCH, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def moved_on(org: str, ns: str, samples: Any, *, scope: Any) -> Any:
        return {
            tuple(s[c] for c in mi.IDENTITY): {"version": 8, "content_sha256": "b" * 64}
            for s in samples
        }

    monkeypatch.setattr(mi, "revisions_for", moved_on)
    out = await mi.handle_memory_inspect(_inp(sample_limit=1))
    (row,) = out.own.newest
    assert row.version == 7 and row.content_sha256 == ""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("read", "needle", "extra"),
    [
        ("view_counts", "GROUP BY memory_type, status, source_class", {}),
        ("view_tags", "arrayJoin(tags)", {"limit": 5}),
        ("view_categories", "FROM loom.memory_categories FINAL", {}),
        ("view_newest", "ORDER BY updated_at DESC", {"limit": 5}),
    ],
)
async def test_every_read_that_selects_rows_applies_its_scope(
    ch: RecordingCH, read: str, needle: str, extra: dict[str, Any]
) -> None:
    from server.identity.domain.read_scope import ReadFilters

    narrowed = ReadFilters(scope="own_user", user_id="u9", session_id=None)
    await getattr(mi.store, read)("o", "ns", "agent", "bot", "others", scope=narrowed, **extra)
    (s,) = ch.statements(needle)
    assert "user_id = {" in s.sql and "u9" in s.params.values()


@pytest.mark.asyncio
async def test_a_failed_section_is_skipped_not_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _ch(tags=_boom)
    fake.strict = False
    fake.install(monkeypatch, "server.identity.app.namespace_catalog")
    out = await mi.handle_memory_inspect(_inp())
    assert "own.tags: RuntimeError" in out.skipped and "others.tags: RuntimeError" in out.skipped
    assert out.own.top_tags == [] and out.own.live_rows == 3


@pytest.mark.asyncio
async def test_a_view_whose_counts_failed_is_not_complete(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _ch(counts=_boom)
    fake.strict = False
    fake.install(monkeypatch, "server.identity.app.namespace_catalog")
    out = await mi.handle_memory_inspect(_inp())
    assert "own.counts: RuntimeError" in out.skipped
    assert out.own.live_rows == 0 and len(out.own.newest) == 1 and out.own.complete is False


@pytest.mark.parametrize(
    ("preview", "text"),
    [
        ('"Prefers email."', "Prefers email."),
        ('{"a": 1}', '{"a": 1}'),
        # A preview cut inside a long string, and one cut inside an escape.
        ('"line one\\nline two', "line one\nline two"),
        ('"caf\\u00e', "caf"),
        # Cut between the two escapes of an emoji: the half is dropped.
        ('"ok \\ud83d\\ude00 \\ud83d\\ude', "ok \U0001f600 "),
    ],
)
def test_the_excerpt_decodes_a_cut_preview(preview: str, text: str) -> None:
    assert excerpt(preview, cut=True) == text


@pytest.mark.parametrize(
    ("preview", "text"),
    [('"Prefers email."', "Prefers email."), ('"Yes" ok.', '"Yes" ok.'), ("plain", "plain")],
)
def test_an_uncut_preview_is_decoded_whole_or_kept(preview: str, text: str) -> None:
    assert excerpt(preview, cut=False) == text


def _boom(sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    raise RuntimeError("read failed")
