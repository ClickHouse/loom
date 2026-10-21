"""`erasure.export`: a namespace export is authorized against
the namespace itself; a subject export excludes the restricted namespaces
the caller may not read."""

from __future__ import annotations

import pytest

import server.auth.authz as authz
import server.identity.app.namespace_classification as nc
from server.auth.context import set_current_auth
from server.auth.jwt import AuthInfo
from server.core.errors import AuthInsufficientError
from server.erasure.api.erasure import ErasureExportInput
from server.erasure.app import handlers as h
from server.erasure.store.exports import Shown
from server.operations.base import Role
from tests.unit.clickhouse_local import CLICKHOUSE, ddl, run

ORG, PRIV, STD = "clickhouse", "tracker-ops-restricted", "support-bot"


@pytest.fixture
def exports(monkeypatch):
    async def _mem(org, scope_type, scope_value, *, include_tombstoned, limit, shown):
        rows = [{"namespace": STD, "value": "ok"}, {"namespace": PRIV, "value": "SECRET"}]
        kept = [r for r in rows if _visible(shown, r["namespace"])][:limit]
        return kept, len(kept), False

    async def _empty(org, scope_type, scope_value, *, limit, shown, **_k):
        return ([], 0, False)

    monkeypatch.setattr(h, "_export_memory", _mem)
    monkeypatch.setattr(h, "_export_knowledge", _empty)
    async def _no_turns(*_a, **_k):
        return [], False

    async def _no_threads(*_a, **_k):
        return []

    monkeypatch.setattr(h.erasure_exports, "export_conversations", _no_turns)
    monkeypatch.setattr(h.erasure_exports, "export_conversation_threads", _no_threads)
    monkeypatch.setattr(h, "_export_agent_events", _empty)
    monkeypatch.setattr(h, "_export_audit_log", _empty)

    async def _emit(**kw):
        return "h"

    monkeypatch.setattr(h.audit, "emit", _emit)
    import server.code.domain.index.repos as repos

    monkeypatch.setattr(repos, "configured_repos", list)
    nc.reset()
    nc._cache[ORG] = (1e12, frozenset({PRIV}))
    grants = {}

    async def _grant(org, ns, subject):
        return grants.get((org, ns, subject))

    monkeypatch.setattr(authz, "grant_role_for", _grant)
    yield grants
    nc.reset()
    set_current_auth(None)


def _admin(*names, role=Role.ADMIN):
    return AuthInfo(
        subject="ops@x",
        org=ORG,
        namespaces=tuple(names),
        roles=(role,),
        is_service_account=False,
    )


@pytest.mark.asyncio
async def test_namespace_export_of_a_restricted_namespace_needs_the_two_factors(exports) -> None:
    set_current_auth(_admin("*"))
    with pytest.raises(AuthInsufficientError):
        await h.handle_erasure_export(
            ErasureExportInput(org=ORG, scope_type="namespace", scope_value=PRIV, requester="ops@x")
        )
    exports[(ORG, PRIV, "ops@x")] = Role.ADMIN
    set_current_auth(_admin(PRIV))
    out = await h.handle_erasure_export(
        ErasureExportInput(org=ORG, scope_type="namespace", scope_value=PRIV, requester="ops@x")
    )
    assert out.counts["memory"] == 2


@pytest.mark.asyncio
async def test_subject_export_drops_unreadable_restricted_rows(exports) -> None:
    set_current_auth(_admin("*", role=Role.ORG_ADMIN))
    out = await h.handle_erasure_export(
        ErasureExportInput(org=ORG, scope_type="user_id", scope_value="u1", requester="ops@x")
    )
    rows = out.memory.rows
    assert [r["namespace"] for r in rows] == [STD] and out.counts["memory"] == 1
    exports[(ORG, PRIV, "ops@x")] = Role.VIEWER
    set_current_auth(_admin(PRIV, role=Role.ORG_ADMIN))
    out = await h.handle_erasure_export(
        ErasureExportInput(org=ORG, scope_type="user_id", scope_value="u1", requester="ops@x")
    )
    assert out.counts["memory"] == 2


@pytest.mark.asyncio
async def test_a_subject_export_needs_org_admin_like_the_erasure(exports) -> None:
    """erasure.request refuses a user_id erasure to a namespace ADMIN; exporting everything
    held about that user across the org is refused to the same caller."""
    from server.core.errors import AuthInsufficientError

    set_current_auth(_admin("*"))
    with pytest.raises(AuthInsufficientError):
        await h.handle_erasure_export(
            ErasureExportInput(org=ORG, scope_type="user_id", scope_value="u1", requester="ops@x")
        )


@pytest.mark.asyncio
async def test_auth_disabled_exports_everything(exports) -> None:
    set_current_auth(None)
    out = await h.handle_erasure_export(
        ErasureExportInput(org=ORG, scope_type="user_id", scope_value="u1", requester="ops@x")
    )
    assert out.counts["memory"] == 2


@pytest.mark.asyncio
async def test_the_export_audits_each_high_row_it_serves(exports, monkeypatch) -> None:
    def row(ns: str, key: str, sensitivity: str) -> dict:
        return {
            "namespace": ns,
            "memory_key": key,
            "memory_id": f"id-{key}",
            "version": 1,
            "sensitivity": sensitivity,
            "value": "v",
        }

    async def _mem(*_a, shown, **_k):
        rows = [row(STD, "plain", ""), row(STD, "vault", "high"), row(PRIV, "hidden", "high")]
        kept = [r for r in rows if _visible(shown, r["namespace"])]
        return kept, len(kept), False

    audited: list[tuple[str, list[str]]] = []

    async def _emit_many(event, **kw):
        audited.append((kw["namespace"], [s["memory_key"] for s in kw["subjects"]]))

    monkeypatch.setattr(h, "_export_memory", _mem)
    monkeypatch.setattr(h.audit, "emit_many", _emit_many)
    # An org admin with no grant on the restricted namespace: its rows are not served,
    # so they are not audited as read either.
    set_current_auth(_admin("*", role=Role.ORG_ADMIN))
    out = await h.handle_erasure_export(
        ErasureExportInput(org=ORG, scope_type="user_id", scope_value="u1", requester="ops@x")
    )
    assert [r["memory_key"] for r in out.memory.rows] == ["plain", "vault"]
    assert audited == [(STD, ["vault"])]


@pytest.mark.asyncio
async def test_a_user_export_carries_their_personal_collection_and_no_other_unread_one(
    exports, monkeypatch
) -> None:
    from server.core.principals import personal_name

    mine, theirs = personal_name(ORG, "user:u1"), personal_name(ORG, "user:u2")

    async def _mem(org, scope_type, scope_value, *, include_tombstoned, limit, shown):
        rows = [{"namespace": ns, "value": ns} for ns in (STD, mine, theirs, "col-hr")]
        kept = [r for r in rows if _visible(shown, r["namespace"])][:limit]
        return kept, len(kept), False

    async def _readable(auth, org):
        return []

    monkeypatch.setattr(h, "_export_memory", _mem)
    monkeypatch.setattr(h.sharing, "readable", _readable)
    set_current_auth(_admin("*", role=Role.ORG_ADMIN))
    out = await h.handle_erasure_export(
        ErasureExportInput(org=ORG, scope_type="user_id", scope_value="u1", requester="ops@x")
    )
    assert [r["namespace"] for r in out.memory.rows] == [STD, mine]
    assert out.counts["memory"] == 2


@pytest.mark.asyncio
async def test_a_user_export_carries_their_conversation_rows_and_audits_the_high_ones(
    exports, monkeypatch
) -> None:
    """A user's turns and saved messages are exported, a row of a restricted namespace
    the caller may not read is not, and each one whose text reads as a credential is
    labelled 'high' and audited as served."""
    key = "AKIA" + "ABCDEFGHIJKLMNOP"

    async def _conversations(
        org, scope_type, scope_value, *, include_tombstoned, limit, max_bytes, shown
    ):
        assert scope_type == "user_id" and scope_value == "u1" and include_tombstoned
        assert max_bytes == h.settings.erasure_export_conversation_max_bytes
        stored = [
            _turn(STD, "m-saved", "the renewal clause", saved=True),
            _turn(STD, "m-turn", f"my key is {key}"),
            # A saved turn whose content is plain and whose reasoning holds a key.
            _turn(STD, "m-reasoned", "plain", saved=True, reasoning=f"use {key}"),
            _turn(PRIV, "m-hidden", "restricted", saved=True),
        ]
        return [r for r in stored if _visible(shown, r["namespace"])][:limit], False

    async def _threads(org, scope_type, scope_value, *, include_tombstoned, limit, shown):
        thread = {"namespace": STD, "agent": "bot", "metadata": "{}"}
        return [
            {**thread, "thread_id": "t1", "title": "Renewal"},
            {**thread, "thread_id": "t2", "title": f"key {key}"},
        ]

    audited: list[tuple[str, list[dict]]] = []

    async def _emit_many(event, **kw):
        audited.append((kw["namespace"], kw["subjects"]))

    monkeypatch.setattr(h.erasure_exports, "export_conversations", _conversations)
    monkeypatch.setattr(h.erasure_exports, "export_conversation_threads", _threads)
    monkeypatch.setattr(h.audit, "emit_many", _emit_many)
    set_current_auth(_admin("*", role=Role.ORG_ADMIN))
    out = await h.handle_erasure_export(
        ErasureExportInput(org=ORG, scope_type="user_id", scope_value="u1", requester="ops@x")
    )
    rows = out.conversations.rows
    assert [(r["msg_id"], r["is_saved"], r["sensitivity"]) for r in rows] == [
        ("m-saved", True, ""),
        ("m-turn", False, "high"),
        ("m-reasoned", True, "high"),
    ]
    assert out.counts["conversations"] == 3 and not out.conversations.truncated
    threads = out.conversation_threads.rows
    assert [(t["thread_id"], t["sensitivity"]) for t in threads] == [("t1", ""), ("t2", "high")]
    assert out.counts["conversation_threads"] == 2
    assert audited == [
        (
            STD,
            [
                {"agent": "bot", "thread_id": "t1", "msg_id": "m-turn"},
                {"agent": "bot", "thread_id": "t1", "msg_id": "m-reasoned"},
            ],
        ),
        (STD, [{"agent": "bot", "thread_id": "t2"}]),
    ]


@pytest.mark.asyncio
async def test_a_namespace_export_marks_conversations_truncated_past_the_byte_budget(
    exports, monkeypatch
) -> None:
    """A namespace export reads the namespace's turns; when the read stopped at the byte
    budget the section says so, though it holds fewer rows than `section_limit`."""

    async def _conversations(
        org, scope_type, scope_value, *, include_tombstoned, limit, max_bytes, shown
    ):
        assert (scope_type, scope_value, limit) == ("namespace", STD, 11)
        return [_turn(STD, "m-1", "first")], True

    monkeypatch.setattr(h.erasure_exports, "export_conversations", _conversations)
    set_current_auth(_admin(STD))
    out = await h.handle_erasure_export(
        ErasureExportInput(
            org=ORG, scope_type="namespace", scope_value=STD, requester="ops@x", section_limit=10
        )
    )
    assert [r["msg_id"] for r in out.conversations.rows] == ["m-1"]
    assert out.counts["conversations"] == 1 and out.conversations.truncated


def _turn(ns: str, msg_id: str, content: str, *, saved: bool = False, reasoning: str = "") -> dict:
    return {
        "namespace": ns,
        "agent": "bot",
        "thread_id": "t1",
        "msg_id": msg_id,
        "content": content,
        "reasoning": reasoning,
        "metadata": "{}",
        "is_saved": saved,
    }


def _visible(shown, namespace: str) -> bool:
    """What `Shown.clause()` keeps, for the fakes above."""
    if namespace in shown.hidden:
        return False
    return (
        shown.collections is None
        or not namespace.startswith("col-")
        or (namespace in shown.collections)
    )


@pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")
@pytest.mark.parametrize(
    ("shown", "first"),
    [
        (Shown(hidden=(PRIV,), collections=("col-me-1",)), "col-me-1"),
        (Shown(), "col-a-hidden"),  # a platform admin: nothing left out
    ],
)
def test_an_export_leaves_out_what_it_may_not_show_before_its_limit(shown, first) -> None:
    """Run on clickhouse-local: a hidden collection's chunk sorts first, and the
    page of one still holds the visible chunk after it."""
    import asyncio

    from server.erasure.store import exports

    class _Local:
        async def query_all(self, sql, params=None, **_k):
            names = ", ".join(f"'{ns}'" for ns in ("col-a-hidden", "col-me-1", PRIV, STD))
            insert = (
                "INSERT INTO loom.chunks (org, namespace, user_id, source_uri, chunk_idx,"
                f" content, embedding) SELECT '{ORG}', ns, 'u1', 'doc', 0, 'text',"
                f" arrayWithConstant(3072, toBFloat16(0)) FROM (SELECT arrayJoin([{names}]) AS ns)"
            )
            # The table's vector column uses the codec the migration enables.
            setup = ["SET enable_quantized_codec = 1", ddl("chunks"), insert]
            return run(setup, sql, params)

    got = asyncio.run(
        exports.export_knowledge(ORG, "user_id", "u1", limit=1, shown=shown, ch=_Local())
    )
    assert [r["namespace"] for r in got] == [first]



_TURNS = (
    "INSERT INTO loom.conversations (org, namespace, agent, thread_id, msg_id,"
    " role, content, user_id, ts, is_saved, saved_at, tombstoned_at) VALUES"
    f" ('{ORG}', '{STD}', 'bot', 't1', 'm-saved', 'user', 'aaaa', 'u1',"
    " '2026-01-01 00:00:01', true, '2026-01-02 00:00:00', NULL),"
    f" ('{ORG}', '{STD}', 'bot', 't1', 'm-turn', 'assistant', 'bbbb', 'u1',"
    " '2026-01-01 00:00:02', false, NULL, NULL),"
    f" ('{ORG}', '{STD}', 'bot', 't1', 'm-gone', 'user', 'cccc', 'u1',"
    " '2026-01-01 00:00:03', false, NULL, '2026-01-03 00:00:00'),"
    f" ('{ORG}', '{STD}', 'bot', 't2', 'm-other', 'user', 'dddd', 'u2',"
    " '2026-01-01 00:00:01', true, '2026-01-02 00:00:00', NULL),"
    f" ('{ORG}', 'col-hidden', 'bot', 't3', 'm-col', 'user', 'eeee', 'u1',"
    " '2026-01-01 00:00:01', true, '2026-01-02 00:00:00', NULL)"
)


class _LocalTurns:
    """clickhouse-local holding `_TURNS`: six bytes of text a row (content, '{}' metadata)."""

    async def query_all(self, sql, params=None, **_k):
        return run([ddl("conversations"), _TURNS], sql, params)


def _read_turns(scope_type: str, scope_value: str, *, include_tombstoned=True, max_bytes=1000):
    import asyncio

    from server.erasure.store import exports

    return asyncio.run(
        exports.export_conversations(
            ORG,
            scope_type,
            scope_value,
            include_tombstoned=include_tombstoned,
            limit=10,
            max_bytes=max_bytes,
            shown=Shown(collections=()),
            ch=_LocalTurns(),
        )
    )


@pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")
@pytest.mark.parametrize(
    ("scope_type", "scope_value", "include_tombstoned", "expected"),
    [
        ("user_id", "u1", True, ["m-saved", "m-turn", "m-gone"]),
        ("user_id", "u1", False, ["m-saved", "m-turn"]),
        ("namespace", STD, True, ["m-saved", "m-turn", "m-gone", "m-other"]),
        ("namespace", STD, False, ["m-saved", "m-turn", "m-other"]),
    ],
)
def test_an_export_reads_the_turns_and_saved_messages_in_scope(
    scope_type, scope_value, include_tombstoned, expected
) -> None:
    """Run on clickhouse-local: the turns and saved messages of the user or namespace, in
    thread order, without another scope's rows or a collection the caller may not read."""
    got, over_budget = _read_turns(scope_type, scope_value, include_tombstoned=include_tombstoned)
    assert [r["msg_id"] for r in got] == expected and not over_budget
    assert "bytes_before" not in got[0]
    assert got[0]["is_saved"] is True and got[0]["saved_at"] is not None


@pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")
@pytest.mark.parametrize(
    ("max_bytes", "expected", "over_budget"),
    [
        (1, ["m-saved"], True),  # the first turn is read whatever its size
        (6, ["m-saved"], True),
        (7, ["m-saved", "m-turn"], True),
        (13, ["m-saved", "m-turn", "m-gone"], False),
    ],
)
def test_the_turns_stop_at_the_byte_budget_and_say_so(max_bytes, expected, over_budget) -> None:
    """Run on clickhouse-local: a turn is read while the text before it is under the budget,
    and rows left past it are reported."""
    got, cut = _read_turns("user_id", "u1", max_bytes=max_bytes)
    assert [r["msg_id"] for r in got] == expected
    assert cut is over_budget


@pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")
@pytest.mark.parametrize(
    ("scope_type", "scope_value", "include_tombstoned", "expected"),
    [
        ("user_id", "u1", True, [("t1", "Renewal"), ("t4", "gone")]),
        ("user_id", "u1", False, [("t1", "Renewal")]),
        ("namespace", STD, False, [("t1", "Renewal"), ("t2", "theirs"), ("t3", "moved")]),
    ],
)
def test_an_export_reads_the_latest_version_of_the_threads_in_scope(
    scope_type, scope_value, include_tombstoned, expected
) -> None:
    """Run on clickhouse-local: one row per thread, its latest version, filtered after the
    latest is chosen (t3 moved from u1 to u2, so u1's export leaves it out)."""
    import asyncio

    from server.erasure.store import exports

    class _Local:
        async def query_all(self, sql, params=None, **_k):
            insert = (
                "INSERT INTO loom.conversation_threads (org, namespace, agent, thread_id,"
                " title, user_id, created_at, updated_at, tombstoned_at) VALUES"
                f" ('{ORG}', '{STD}', 'bot', 't1', 'old', 'u1', '2026-01-01', '2026-01-01', NULL),"
                f" ('{ORG}', '{STD}', 'bot', 't1', 'Renewal', 'u1', '2026-01-01', '2026-01-02',"
                " NULL),"
                f" ('{ORG}', '{STD}', 'bot', 't2', 'theirs', 'u2', '2026-01-01', '2026-01-01',"
                " NULL),"
                f" ('{ORG}', '{STD}', 'bot', 't3', 'mine', 'u1', '2026-01-01', '2026-01-01', NULL),"
                f" ('{ORG}', '{STD}', 'bot', 't3', 'moved', 'u2', '2026-01-01', '2026-01-02', NULL),"
                f" ('{ORG}', '{STD}', 'bot', 't4', 'gone', 'u1', '2026-01-01', '2026-01-01',"
                " '2026-01-03')"
            )
            return run([ddl("conversation_threads"), insert], sql, params)

    got = asyncio.run(
        exports.export_conversation_threads(
            ORG,
            scope_type,
            scope_value,
            include_tombstoned=include_tombstoned,
            limit=10,
            ch=_Local(),
        )
    )
    assert [(r["thread_id"], r["title"]) for r in got] == expected
