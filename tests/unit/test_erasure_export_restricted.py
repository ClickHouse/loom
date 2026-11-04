"""`erasure.export`: who may export what (a namespace export is authorized against the
namespace itself; a subject export excludes the restricted namespaces the caller may not
read), and the reads behind each section, the conversation sections' byte budget,
labelling and audit included."""

from __future__ import annotations

import asyncio
from datetime import datetime

import pytest

import server.auth.authz as authz
import server.identity.app.namespace_classification as nc
from server.auth.context import set_current_auth
from server.auth.jwt import AuthInfo
from server.core.errors import AuthInsufficientError
from server.erasure.api.erasure import ErasureExportInput
from server.erasure.app import handlers as h
from server.erasure.store import exports as store_exports
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

    async def _empty(org, scope_type, scope_value, *, limit, shown):
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
    the caller may not read is not, and each one whose text reads as high is
    labelled 'high' and audited as served."""
    key = "AKIA" + "ABCDEFGHIJKLMNOP"
    seen_as_of: list[datetime] = []

    async def _conversations(
        org, scope_type, scope_value, *, include_tombstoned, limit, max_bytes, as_of, shown
    ):
        assert scope_type == "user_id" and scope_value == "u1" and include_tombstoned
        assert max_bytes == h.settings.erasure_export_conversation_max_bytes
        seen_as_of.append(as_of)
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
    # Turns are read as of the moment the archive is stamped with.
    assert seen_as_of == [out.exported_at]
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
        org, scope_type, scope_value, *, include_tombstoned, limit, max_bytes, as_of, shown
    ):
        assert (scope_type, scope_value, limit) == ("namespace", STD, 10)
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


@pytest.mark.asyncio
async def test_the_exported_audit_row_names_the_token_not_the_body(exports, monkeypatch) -> None:
    emitted: list[dict] = []

    async def _emit(**kw):
        emitted.append(kw)
        return "h"

    monkeypatch.setattr(h.audit, "emit", _emit)
    set_current_auth(_admin(STD))
    await h.handle_erasure_export(
        ErasureExportInput(org=ORG, scope_type="namespace", scope_value=STD, requester="dpo@y")
    )
    (row,) = [e for e in emitted if e["event_type"] == "erasure.exported"]
    assert row["actor"] == "human:ops@x"
    assert row["subject"]["requester"] == "dpo@y"


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

    class _Local:
        async def query_all(self, sql, params=None, **_k):
            names = ", ".join(f"'{ns}'" for ns in ("col-a-hidden", "col-me-1", PRIV, STD))
            insert = (
                "INSERT INTO loom.chunks (org, namespace, user_id, source_uri, chunk_idx,"
                f" content, context, embedding) SELECT '{ORG}', ns, 'u1', 'doc', 0, 'text', 'Doc',"
                f" arrayWithConstant(3072, toBFloat16(0)) FROM (SELECT arrayJoin([{names}]) AS ns)"
            )
            # The table's vector column uses the codec the migration enables.
            setup = ["SET enable_quantized_codec = 1", ddl("chunks"), insert]
            return run(setup, sql, params)

    got = asyncio.run(
        store_exports.export_knowledge(ORG, "user_id", "u1", limit=1, shown=shown, ch=_Local())
    )
    assert [r["namespace"] for r in got] == [first]
    assert got[0]["context"] == "Doc"  # the chunk's stored context is exported with it


_TURNS = (
    "INSERT INTO loom.conversations (org, namespace, agent, thread_id, msg_id,"
    " role, content, user_id, ts, is_saved, saved_at, tombstoned_at) VALUES"
    f" ('{ORG}', '{STD}', 'bot', 't1', 'm-saved', 'user', 'aaaa', 'u1',"
    " '2026-01-01 00:00:01', true, '2026-01-02 00:00:00', NULL),"
    # A sub-second stamp, as a real turn has: the text read must still reach it.
    f" ('{ORG}', '{STD}', 'bot', 't1', 'm-turn', 'assistant', 'bbbb', 'u1',"
    " '2026-01-01 00:00:02.500', false, NULL, NULL),"
    f" ('{ORG}', '{STD}', 'bot', 't1', 'm-gone', 'user', 'cccc', 'u1',"
    " '2026-01-01 00:00:03', false, NULL, '2026-01-03 00:00:00'),"
    f" ('{ORG}', '{STD}', 'bot', 't2', 'm-other', 'user', 'dddd', 'u2',"
    " '2026-01-01 00:00:01', true, '2026-01-02 00:00:00', NULL),"
    f" ('{ORG}', 'col-hidden', 'bot', 't3', 'm-col', 'user', 'eeee', 'u1',"
    " '2026-01-01 00:00:01', true, '2026-01-02 00:00:00', NULL),"
    # Appended after the export started (`_AS_OF`): never read.
    f" ('{ORG}', '{STD}', 'bot', 't1', 'm-later', 'user', 'ffff', 'u1',"
    " '2026-03-01 00:00:00', false, NULL, NULL)"
)
_AS_OF = datetime(2026, 2, 1)
# An org admin who may read no collection.
_NO_COLLECTIONS = Shown(collections=())
# What an org admin with no grant on PRIV and no readable collection is shown.
_HIDING = Shown(hidden=(PRIV,), collections=())


class _LocalTurns:
    """clickhouse-local holding `_TURNS`: six bytes of text a row (content, '{}' metadata).
    Each `ts` comes back as a datetime, as the server's client returns it."""

    async def query_all(self, sql, params=None, **_k):
        rows = run([ddl("conversations"), _TURNS], sql, params)
        return [{**r, "ts": datetime.fromisoformat(r["ts"])} if "ts" in r else r for r in rows]


def _read_turns(
    scope_type: str,
    scope_value: str,
    *,
    include_tombstoned=True,
    max_bytes=1000,
    shown=_NO_COLLECTIONS,
):
    return asyncio.run(
        store_exports.export_conversations(
            ORG,
            scope_type,
            scope_value,
            include_tombstoned=include_tombstoned,
            limit=10,
            max_bytes=max_bytes,
            as_of=_AS_OF,
            shown=shown,
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
    assert "m-later" not in {r["msg_id"] for r in got}  # stamped after `as_of`
    assert "row_bytes" not in got[0] and "bytes_before" not in got[0]
    assert got[0]["is_saved"] is True and got[0]["saved_at"] is not None


@pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")
def test_a_user_export_reads_the_saved_messages_in_their_own_collection() -> None:
    """Run on clickhouse-local: a saved-only message in the user's personal collection
    (`@me`) is exported once that collection is among those shown, as the handler adds it
    for a user export."""
    got, _ = _read_turns(
        "user_id", "u1", include_tombstoned=False, shown=Shown(collections=("col-hidden",))
    )
    assert [r["msg_id"] for r in got] == ["m-col", "m-saved", "m-turn"]
    assert got[0]["namespace"] == "col-hidden" and got[0]["is_saved"] is True


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
    ("scope_type", "scope_value", "include_tombstoned", "shown", "expected"),
    [
        ("user_id", "u1", True, _HIDING, [(STD, "t1", "Renewal"), (STD, "t4", "gone")]),
        ("user_id", "u1", False, _HIDING, [(STD, "t1", "Renewal")]),
        (
            "namespace",
            STD,
            False,
            _HIDING,
            [(STD, "t1", "Renewal"), (STD, "t2", "theirs"), (STD, "t3", "moved")],
        ),
        # Shown everything (a platform admin): the restricted and collection threads too.
        (
            "user_id",
            "u1",
            False,
            Shown(),
            [
                ("col-hidden", "t6", "collection"),
                (STD, "t1", "Renewal"),
                (PRIV, "t5", "restricted"),
            ],
        ),
    ],
)
def test_an_export_reads_the_latest_version_of_the_threads_in_scope(
    scope_type, scope_value, include_tombstoned, shown, expected
) -> None:
    """Run on clickhouse-local: one row per thread, its latest version, filtered after the
    latest is chosen (t3 moved from u1 to u2, so u1's export leaves it out), without the
    threads of a restricted namespace or a collection the caller may not read."""

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
                " '2026-01-03'),"
                f" ('{ORG}', '{PRIV}', 'bot', 't5', 'restricted', 'u1', '2026-01-01',"
                " '2026-01-01', NULL),"
                f" ('{ORG}', 'col-hidden', 'bot', 't6', 'collection', 'u1', '2026-01-01',"
                " '2026-01-01', NULL)"
            )
            return run([ddl("conversation_threads"), insert], sql, params)

    got = asyncio.run(
        store_exports.export_conversation_threads(
            ORG,
            scope_type,
            scope_value,
            include_tombstoned=include_tombstoned,
            limit=10,
            shown=shown,
            ch=_Local(),
        )
    )
    assert [(r["namespace"], r["thread_id"], r["title"]) for r in got] == expected


def test_a_turn_written_between_the_size_pass_and_the_text_read_is_not_served() -> None:
    """A turn the size pass never measured is dropped, and the measured turn it pushed
    out of the text read's page marks the section truncated."""

    def key(thread: str, second: int, msg: str) -> dict:
        ts = datetime(2026, 1, 1, 0, 0, second)
        return {"namespace": STD, "thread_id": thread, "ts": ts, "msg_id": msg, "row_bytes": 6}

    class _Racing:
        async def query_all(self, sql, params=None, **_k):
            if "bytes_before_previous" in sql:
                return [
                    {**key("t1", 1, "m1"), "bytes_before": 0},
                    {**key("t9", 1, "m9"), "bytes_before": 6},
                ]
            # A turn appended to t1 meanwhile sorts before t9's and takes its place.
            assert params["kept"] == 2
            return [
                {**key("t1", 1, "m1"), "content": "a"},
                {**key("t1", 2, "m-new"), "content": "huge"},
            ]

    got, left_out = asyncio.run(
        store_exports.export_conversations(
            ORG,
            "namespace",
            STD,
            include_tombstoned=True,
            limit=10,
            max_bytes=100,
            as_of=_AS_OF,
            ch=_Racing(),
        )
    )
    assert [r["msg_id"] for r in got] == ["m1"] and left_out


def test_two_measured_copies_of_one_turn_are_both_served() -> None:
    """A save racing a transcript append can leave two rows of one turn; both were
    measured, so both are served and nothing is reported left out."""
    turn = {
        "namespace": STD,
        "thread_id": "t1",
        "ts": datetime(2026, 1, 1),
        "msg_id": "m1",
        "row_bytes": 6,
    }

    class _Twice:
        async def query_all(self, sql, params=None, **_k):
            if "bytes_before_previous" in sql:
                return [{**turn, "bytes_before": 0}, {**turn, "bytes_before": 6}]
            assert params["kept"] == 2
            return [{**turn, "is_saved": True}, {**turn, "is_saved": False}]

    got, left_out = asyncio.run(
        store_exports.export_conversations(
            ORG,
            "namespace",
            STD,
            include_tombstoned=True,
            limit=10,
            max_bytes=100,
            as_of=_AS_OF,
            ch=_Twice(),
        )
    )
    assert [r["is_saved"] for r in got] == [True, False] and not left_out
    assert all("row_bytes" not in r for r in got)


def test_the_turns_stop_at_the_row_limit_without_reading_the_next_ones_text() -> None:
    """The size pass reads one row past `limit` to say more exist; the text read asks
    only for the `limit` rows served."""

    def turn(msg: str) -> dict:
        return {
            "namespace": STD,
            "thread_id": "t1",
            "ts": datetime(2026, 1, 1),
            "msg_id": msg,
            "row_bytes": 6,
        }

    class _Full:
        async def query_all(self, sql, params=None, **_k):
            if "bytes_before_previous" in sql:
                assert params["lim"] == 3
                return [{**turn(m), "bytes_before": 0} for m in ("m1", "m2", "m3")]
            assert params["kept"] == 2 and params["last_mid"] == "m2"
            return [turn("m1"), turn("m2")]

    got, left_out = asyncio.run(
        store_exports.export_conversations(
            ORG,
            "namespace",
            STD,
            include_tombstoned=True,
            limit=2,
            max_bytes=100,
            as_of=_AS_OF,
            ch=_Full(),
        )
    )
    assert [r["msg_id"] for r in got] == ["m1", "m2"] and left_out


def test_an_uncounted_copy_that_ties_on_the_last_key_is_not_served() -> None:
    """Two copies of one turn share its sort key; when the text read returns the copy the
    size pass left out, it is dropped by its size and the section says rows are missing."""
    key = {"namespace": STD, "thread_id": "t1", "ts": datetime(2026, 1, 1), "msg_id": "m1"}

    class _Tie:
        async def query_all(self, sql, params=None, **_k):
            if "bytes_before_previous" in sql:
                return [
                    {**key, "row_bytes": 6, "bytes_before": 0},
                    {**key, "row_bytes": 999, "bytes_before": 6},
                ]
            return [{**key, "row_bytes": 999, "content": "huge"}]

    got, left_out = asyncio.run(
        store_exports.export_conversations(
            ORG,
            "namespace",
            STD,
            include_tombstoned=True,
            limit=10,
            max_bytes=6,
            as_of=_AS_OF,
            ch=_Tie(),
        )
    )
    assert got == [] and left_out
