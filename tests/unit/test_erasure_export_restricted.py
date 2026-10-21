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
_EXPORT_CONVERSATIONS = h._export_conversations
_EXPORT_THREADS = h._export_conversation_threads


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
    monkeypatch.setattr(h, "_export_conversations", _empty)
    monkeypatch.setattr(h, "_export_conversation_threads", _empty)
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

    def turn(ns: str, msg_id: str, content: str, saved: bool) -> dict:
        return {
            "namespace": ns,
            "thread_id": "t1",
            "msg_id": msg_id,
            "content": content,
            "reasoning": "",
            "metadata": "{}",
            "is_saved": saved,
        }

    async def _conversations(org, scope_type, scope_value, *, include_tombstoned, limit, shown):
        assert scope_type == "user_id" and scope_value == "u1" and include_tombstoned
        stored = [
            turn(STD, "m-saved", "the renewal clause", True),
            turn(STD, "m-turn", f"my key is {key}", False),
            turn(PRIV, "m-hidden", "restricted", True),
        ]
        return [r for r in stored if _visible(shown, r["namespace"])][: limit + 1]

    async def _threads(org, scope_type, scope_value, *, include_tombstoned, limit, shown):
        return [{"namespace": STD, "thread_id": "t1", "title": "Renewal", "metadata": "{}"}]

    audited: list[tuple[str, list[dict]]] = []

    async def _emit_many(event, **kw):
        audited.append((kw["namespace"], kw["subjects"]))

    # The handler's own section readers, over faked store reads.
    monkeypatch.setattr(h, "_export_conversations", _EXPORT_CONVERSATIONS)
    monkeypatch.setattr(h, "_export_conversation_threads", _EXPORT_THREADS)
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
    ]
    assert out.counts["conversations"] == 2 and not out.conversations.truncated
    assert [r["title"] for r in out.conversation_threads.rows] == ["Renewal"]
    assert out.counts["conversation_threads"] == 1
    assert audited == [(STD, [{"thread_id": "t1", "msg_id": "m-turn"}])]


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


@pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")
@pytest.mark.parametrize(
    ("include_tombstoned", "expected"),
    [(True, ["m-saved", "m-turn", "m-gone"]), (False, ["m-saved", "m-turn"])],
)
def test_a_user_export_reads_their_turns_and_saved_messages(include_tombstoned, expected) -> None:
    """Run on clickhouse-local: the user's transcript turns and saved messages, in thread
    order, without another user's rows or a collection the caller may not read."""
    import asyncio

    from server.erasure.store import exports

    class _Local:
        async def query_all(self, sql, params=None, **_k):
            insert = (
                "INSERT INTO loom.conversations (org, namespace, agent, thread_id, msg_id,"
                " role, content, user_id, ts, is_saved, saved_at, tombstoned_at) VALUES"
                f" ('{ORG}', '{STD}', 'bot', 't1', 'm-saved', 'user', 'a', 'u1',"
                " '2026-01-01 00:00:01', true, '2026-01-02 00:00:00', NULL),"
                f" ('{ORG}', '{STD}', 'bot', 't1', 'm-turn', 'assistant', 'b', 'u1',"
                " '2026-01-01 00:00:02', false, NULL, NULL),"
                f" ('{ORG}', '{STD}', 'bot', 't1', 'm-gone', 'user', 'c', 'u1',"
                " '2026-01-01 00:00:03', false, NULL, '2026-01-03 00:00:00'),"
                f" ('{ORG}', '{STD}', 'bot', 't2', 'm-other', 'user', 'd', 'u2',"
                " '2026-01-01 00:00:01', true, '2026-01-02 00:00:00', NULL),"
                f" ('{ORG}', 'col-hidden', 'bot', 't3', 'm-col', 'user', 'e', 'u1',"
                " '2026-01-01 00:00:01', true, '2026-01-02 00:00:00', NULL)"
            )
            return run([ddl("conversations"), insert], sql, params)

    got = asyncio.run(
        exports.export_conversations(
            ORG,
            "user_id",
            "u1",
            include_tombstoned=include_tombstoned,
            limit=10,
            shown=Shown(collections=()),
            ch=_Local(),
        )
    )
    assert [r["msg_id"] for r in got] == expected
    assert got[0]["is_saved"] is True and got[0]["saved_at"] is not None
    assert {r["user_id"] for r in got} == {"u1"}


@pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")
def test_a_user_export_reads_the_latest_version_of_their_threads() -> None:
    """Run on clickhouse-local: one row per thread the user owns, its latest title."""
    import asyncio

    from server.erasure.store import exports

    class _Local:
        async def query_all(self, sql, params=None, **_k):
            insert = (
                "INSERT INTO loom.conversation_threads (org, namespace, agent, thread_id,"
                " title, user_id, created_at, updated_at) VALUES"
                f" ('{ORG}', '{STD}', 'bot', 't1', 'old', 'u1', '2026-01-01', '2026-01-01'),"
                f" ('{ORG}', '{STD}', 'bot', 't1', 'Renewal', 'u1', '2026-01-01', '2026-01-02'),"
                f" ('{ORG}', '{STD}', 'bot', 't2', 'theirs', 'u2', '2026-01-01', '2026-01-01')"
            )
            return run([ddl("conversation_threads"), insert], sql, params)

    got = asyncio.run(
        exports.export_conversation_threads(
            ORG, "user_id", "u1", include_tombstoned=True, limit=10, ch=_Local()
        )
    )
    assert [(r["thread_id"], r["title"]) for r in got] == [("t1", "Renewal")]
