"""`grants.list` pages through a namespace's grants one person at a time.

A person's spellings (a login handle and the contact email the directory ties
to it, or two cases of one subject) fold into one row before the page is cut,
so no page splits a person and `truncated` is exact. The cursor is the last
person's (granted_at, subject) under a MAC that names the op and the namespace:
it reads back nowhere else, and an edited one is refused. The statement runs on
clickhouse-local against the production table definitions (those tests skip
without the binary); the cursor and the answer's shape are checked without it.
"""

from __future__ import annotations

import base64
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import pytest

from server.core.cursors import decode_bound_cursor, encode_bound_cursor, encode_cursor
from server.core.errors import ValidationError
from server.core.grant_sql import has_aliases_sql
from server.core.principals import has_aliases
from server.identity.api.models_grants import GrantsListInput, GrantsListOutput
from server.identity.app.grants import handle_grants_list
from tests.unit.ch_fake import RecordingCH
from tests.unit.clickhouse_local import CLICKHOUSE, ddl, run

_needs_ch = pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")

_T0 = datetime(2026, 1, 1)
_RANK = {"viewer": 1, "writer": 2, "admin": 3}
_ROW_FIELDS = {
    "subject",
    "role",
    "status",
    "pending_role",
    "pending_since",
    "granted_at",
    "granted_by",
    "also",
}


def _list(**kw: Any) -> GrantsListInput:
    return GrantsListInput(org="o", namespace="ns", **kw)


def _person(subject: str, seconds: int, *, role: str = "viewer", also: tuple[str, ...] = ()):
    return {
        "subject": subject,
        "role": role,
        "status": "active",
        "pending_role": None,
        "pending_since": None,
        "granted_at": _T0 + timedelta(seconds=seconds),
        "granted_by": "admin",
        "also": list(also),
    }


# --------------------------------------------------------------------------- no binary


async def test_the_default_call_keeps_its_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    people = [_person("jdoe", 9, role="admin", also=("avery@example.com",)), _person("sa:bot", 7)]
    ch = RecordingCH().on("FROM loom.namespace_grants FINAL", people).install(monkeypatch)

    out = (await handle_grants_list(_list())).model_dump(mode="json")

    assert set(out) == {"rows", "next_cursor", "truncated"}
    assert [set(r) for r in out["rows"]] == [_ROW_FIELDS, _ROW_FIELDS]
    assert [(r["subject"], r["role"], r["also"]) for r in out["rows"]] == [
        ("jdoe", "admin", ["avery@example.com"]),
        ("sa:bot", "viewer", []),
    ]
    assert out["next_cursor"] is None and out["truncated"] is False
    (statement,) = ch.statements()
    # 100 people a page, one more read to know whether another page follows.
    assert statement.params["lim"] == 101 and statement.params["first"] is True


async def test_a_full_page_hands_its_last_person_to_the_next(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = [_person("c", 30), _person("b", 20), _person("a", 20)]
    ch = RecordingCH()
    ch.on("FROM loom.namespace_grants FINAL", first, times=1)
    ch.on("FROM loom.namespace_grants FINAL", [_person("a", 20)], times=1)
    ch.install(monkeypatch)

    page1 = await handle_grants_list(_list(limit=2))
    assert [r.subject for r in page1.rows] == ["c", "b"]
    assert page1.truncated is True and page1.next_cursor
    assert decode_bound_cursor(page1.next_cursor, ("grants.list", "o", "ns")) == (
        _T0 + timedelta(seconds=20),
        "b",
    )

    page2 = await handle_grants_list(_list(limit=2, cursor=page1.next_cursor))
    assert [r.subject for r in page2.rows] == ["a"]
    assert page2.truncated is False and page2.next_cursor is None
    second = ch.statements()[1].params
    assert (second["first"], second["at"], second["sub"]) == (False, "2026-01-01 00:00:20", "b")


@pytest.mark.parametrize("subject", ["x" * 800, "🧠" * 800], ids=["ascii", "utf8"])
async def test_a_cursor_can_resume_every_valid_subject(
    monkeypatch: pytest.MonkeyPatch, subject: str
) -> None:
    ch = RecordingCH()
    ch.on("FROM loom.namespace_grants FINAL", [_person(subject, 2), _person("a", 1)], times=1)
    ch.on("FROM loom.namespace_grants FINAL", [_person("a", 1)], times=1)
    ch.install(monkeypatch)

    first = await handle_grants_list(_list(limit=1))
    assert first.rows[0].subject == subject
    assert first.next_cursor and len(first.next_cursor) > 1024

    last = await handle_grants_list(_list(limit=1, cursor=first.next_cursor))
    assert [r.subject for r in last.rows] == ["a"]
    assert last.truncated is False
    assert ch.statements()[1].params["sub"] == subject


async def test_a_page_that_holds_the_last_people_is_the_last(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    people = [_person("b", 2), _person("a", 1)]
    RecordingCH().on("FROM loom.namespace_grants FINAL", people).install(monkeypatch)

    out = await handle_grants_list(_list(limit=2))

    assert [r.subject for r in out.rows] == ["b", "a"]
    assert out.truncated is False and out.next_cursor is None


async def test_projected_fields_leave_the_cursor_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    people = [_person("b", 2), _person("a", 1)]
    RecordingCH().on("FROM loom.namespace_grants FINAL", people).install(monkeypatch)

    out = (await handle_grants_list(_list(limit=1, fields=["role"]))).model_dump(mode="json")

    assert out["rows"] == [{"subject": "b", "role": "viewer"}]
    assert out["truncated"] is True and out["next_cursor"]


@pytest.mark.parametrize(
    "scope",
    [("grants.list", "o", "other"), ("grants.list", "o2", "ns"), ("audit.list", "o", "ns")],
)
async def test_a_cursor_from_another_namespace_or_op_is_refused(
    monkeypatch: pytest.MonkeyPatch, scope: tuple[str, str, str]
) -> None:
    # No route: a statement issued after a foreign cursor fails the test on its own.
    RecordingCH().install(monkeypatch)
    foreign = encode_bound_cursor(scope, _T0, "jdoe")
    with pytest.raises(ValidationError):
        await handle_grants_list(_list(cursor=foreign))


def _edited(cursor: str, old: str, new: str) -> str:
    raw = base64.urlsafe_b64decode(cursor).decode()
    return base64.urlsafe_b64encode(raw.replace(old, new).encode()).decode()


async def test_an_edited_or_unsigned_cursor_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    RecordingCH().install(monkeypatch)
    scope = ("grants.list", "o", "ns")
    issued = encode_bound_cursor(scope, _T0 + timedelta(seconds=5), "jdoe")
    for cursor in (
        _edited(issued, "jdoe", "aaaa"),  # another position
        _edited(issued, "2026-01-01T00:00:05", "2099-01-01T00:00:05"),
        encode_cursor(_T0, "jdoe"),  # the unbound form other ops issue
        "not a cursor",
        "é",
        base64.urlsafe_b64encode(b"\xff\xfe|x").decode(),
    ):
        with pytest.raises(ValidationError):
            await handle_grants_list(_list(cursor=cursor))


def test_a_cursor_reads_back_in_its_scope_only_under_the_secret_that_issued_it(
    settings_override: Any,
) -> None:
    scope = ("grants.list", "o", "ns")
    # `|` in the position survives: the MAC leads, the subject is the rest.
    issued = encode_bound_cursor(scope, _T0, "odd|subject")
    assert decode_bound_cursor(issued, scope) == (_T0, "odd|subject")
    settings_override(jwt_secret="another-root-secret-of-at-least-32-chars")
    with pytest.raises(ValidationError):
        decode_bound_cursor(issued, scope)


class _Pages:
    """A transport answering grants.list from `pages`, one per call, recording each body."""

    def __init__(self, *pages: dict[str, Any]) -> None:
        self.pages = list(pages)
        self.bodies: list[dict[str, Any]] = []

    async def post(self, path: str, payload: dict[str, Any], *a: Any, **k: Any) -> Any:
        assert path == "/v1/grants.list"
        self.bodies.append(dict(payload))
        return self.pages.pop(0)


def _grants_client(transport: _Pages) -> Any:
    from types import SimpleNamespace

    from loom.grants import GrantsClient

    return GrantsClient(SimpleNamespace(org="o", namespace="ns", transport=transport))


async def test_the_sdk_list_walks_every_page() -> None:
    transport = _Pages(
        {"rows": [{"subject": "c"}, {"subject": "b"}], "next_cursor": "k1", "truncated": True},
        {"rows": [{"subject": "a"}], "next_cursor": None, "truncated": False},
    )

    rows = await _grants_client(transport).list(limit=2)

    assert [r["subject"] for r in rows] == ["c", "b", "a"]
    assert [b.get("cursor") for b in transport.bodies] == [None, "k1"]
    assert {b["limit"] for b in transport.bodies} == {2}


async def test_the_sdk_list_refuses_a_list_it_cannot_show_is_whole() -> None:
    from loom.errors import LoomError

    page = {"rows": [{"subject": "b"}], "next_cursor": "k1", "truncated": True}
    unpaged = {"rows": [{"subject": "b"}]}  # a server that does not page grants.list
    ignored = {"rows": [], "truncated": False, "ignored_args": ["cursor"]}
    for pages, kw in (
        ([unpaged], {}),
        ([page, ignored], {}),
        ([page, page], {"max_pages": 2}),
        ([{"rows": [], "truncated": True, "next_cursor": None}], {}),
    ):
        with pytest.raises(LoomError):
            await _grants_client(_Pages(*pages)).list(**kw)


def test_the_limit_keeps_its_default_and_bounds() -> None:
    assert _list().limit == 100
    assert _list(limit=500).limit == 500
    for bad in (0, 501):
        with pytest.raises(ValueError):
            _list(limit=bad)


# --------------------------------------------------------------------- clickhouse-local


@dataclass
class _Row:
    subject: str
    role: str
    at: datetime
    status: str = "active"
    revoked: bool = False
    version: int = 1
    # An active row's elevation request; a pending row asks for its `role`.
    pending_role: str | None = None


def _quote(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _grant_values(r: _Row, namespace: str = "ns") -> str:
    at = f"'{r.at:%Y-%m-%d %H:%M:%S}'"
    revoked_at = at if r.revoked else "NULL"
    # A request is stamped when it is made, as namespace.join writes it.
    asked = r.status == "pending" or r.pending_role is not None
    pending_role = _quote(r.pending_role) if r.pending_role else "NULL"
    return (
        f"('o', {_quote(namespace)}, {_quote(r.subject)}, {_quote(r.role)}, {at}, 'admin', "
        f"{revoked_at}, {r.version}, {_quote(r.status)}, {pending_role}, "
        f"{at if asked else 'NULL'})"
    )


class _LocalCH:
    """The grants and the directory on clickhouse-local. Every read runs over
    the rows written so far, so a test can change them between pages."""

    def __init__(self, rows: list[_Row], ties: list[tuple[str, str]]) -> None:
        self.rows = list(rows)
        self.ties = list(ties)

    def setup(self) -> list[str]:
        # Beside them: a newer grant on another namespace, and a deactivated
        # account whose pair would fold two people into one.
        elsewhere = _Row("h0000", "admin", _T0 + timedelta(days=150))
        grants = [_grant_values(r) for r in self.rows] + [_grant_values(elsewhere, "elsewhere")]
        accounts = [
            f"('o', {_quote('home-' + h)}, {_quote(h)}, {_quote(e)}, true)" for h, e in self.ties
        ]
        accounts.append("('o', 'home-gone', 'guest00@example.com', 'guest01@example.com', false)")
        grants_sql, accounts_sql = ",\n".join(grants), ",\n".join(accounts)
        return [
            ddl("namespace_grants"),
            ddl("namespaces"),
            ddl("sso_links"),
            "INSERT INTO loom.namespace_grants (org, namespace, subject, role, granted_at, "
            "granted_by, revoked_at, version, status, pending_role, pending_since) "
            f"VALUES\n{grants_sql}",
            "INSERT INTO loom.namespaces (org, namespace, owner_email, contact_email, active) "
            f"VALUES\n{accounts_sql}",
        ]

    async def query_all(self, sql: str, params: dict[str, Any] | None = None) -> list[Any]:
        rows = run(self.setup(), sql, params)
        for r in rows:  # typed as the driver returns them
            r["granted_at"] = datetime.fromisoformat(r["granted_at"])
            if r.get("pending_since"):
                r["pending_since"] = datetime.fromisoformat(r["pending_since"])
        return rows


def _expected(ch: _LocalCH) -> list[tuple[str, str, datetime, list[str]]]:
    """What one row per person is, from the rows in Python: the newest spelling's
    subject and time, the highest role, the other spellings newest first."""
    login = {s.lower(): h.lower() for h, e in ch.ties for s in (h, e)}
    live: dict[str, _Row] = {}
    for r in sorted(ch.rows, key=lambda r: r.version):
        live[r.subject] = r
    people: dict[str, list[_Row]] = defaultdict(list)
    for r in live.values():
        if not r.revoked:
            key = r.subject.lower()
            people[login.get(key, key) if has_aliases(r.subject) else key].append(r)
    out = []
    for rows in people.values():
        rows.sort(key=lambda r: (r.at, r.subject), reverse=True)
        head = rows[0]
        role = max((r.role for r in rows), key=_RANK.__getitem__)
        also = [r.subject for r in rows[1:] if r.subject.lower() != head.subject.lower()]
        out.append((head.subject, role, head.at, also))
    return sorted(out, key=lambda p: (p[2], p[0]), reverse=True)


async def _walk(limit: int, between: Any = None) -> list[GrantsListOutput]:
    pages: list[GrantsListOutput] = []
    cursor = None
    for _ in range(50):
        pages.append(await handle_grants_list(_list(limit=limit, cursor=cursor)))
        if not pages[-1].truncated:
            return pages
        cursor = pages[-1].next_cursor
        if between is not None:
            between(len(pages))
    raise AssertionError("the walk did not end")


def _seen(pages: list[GrantsListOutput]) -> list[tuple[str, str, datetime, list[str]]]:
    return [(r.subject, r.role, r.granted_at, list(r.also)) for p in pages for r in p.rows]


def _twelve_hundred() -> _LocalCH:
    """1,200 live grant rows, three to a second, for 850 people."""
    roles = ("viewer", "writer", "admin")

    def at(k: int) -> datetime:
        return _T0 + timedelta(seconds=k // 3)

    rows: list[_Row] = []
    ties: list[tuple[str, str]] = []
    for i in range(300):  # both spellings granted, far apart in the order
        handle, email = f"h{i:04d}", f"p{i:04d}@example.com"
        ties.append((handle, email))
        rows += [_Row(handle, roles[i % 3], at(i)), _Row(email, roles[(i + 1) % 3], at(1199 - i))]
    for i in range(300, 600):  # one spelling of a tied pair granted
        handle, email = f"h{i:04d}", f"p{i:04d}@example.com"
        ties.append((handle, email))
        rows.append(_Row(handle if i % 2 else email, roles[i % 3], at(i)))
    for i in range(200):  # service accounts have one spelling
        rows.append(_Row(f"sa:bot-{i:03d}", "viewer", at(600 + i)))
    for i in range(50):  # an untied subject granted under two cases
        rows.append(_Row(f"Guest{i:02d}@Example.com", "writer", at(800 + i)))
        rows.append(_Row(f"guest{i:02d}@example.com", "viewer", at(1000 + i)))
    return _LocalCH(rows, ties)


@_needs_ch
async def test_one_row_per_person_with_the_highest_role(monkeypatch: pytest.MonkeyPatch) -> None:
    ch = _LocalCH(
        [
            _Row("avery@example.com", "admin", _T0 + timedelta(seconds=5)),
            _Row("jdoe", "viewer", _T0 + timedelta(seconds=9)),
            _Row("sa:bot", "viewer", _T0 + timedelta(seconds=7)),
            _Row("Bob@x.com", "writer", _T0 + timedelta(seconds=3)),
            _Row("bob@x.com", "viewer", _T0 + timedelta(seconds=8)),
            _Row("gone@x.com", "writer", _T0 + timedelta(seconds=2)),
            _Row("gone@x.com", "writer", _T0 + timedelta(seconds=10), revoked=True, version=2),
            _Row("ghost", "writer", _T0 + timedelta(seconds=4)),
        ],
        # A service account has one spelling, even where an account names it.
        [("jdoe", "avery@example.com"), ("ghost", "sa:bot")],
    )
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)

    out = await handle_grants_list(_list())

    assert [(r.subject, r.role, r.status, list(r.also)) for r in out.rows] == [
        ("jdoe", "admin", "active", ["avery@example.com"]),
        ("bob@x.com", "writer", "active", []),
        ("sa:bot", "viewer", "active", []),
        ("ghost", "writer", "active", []),
    ]
    assert out.truncated is False and out.next_cursor is None


@_needs_ch
async def test_a_request_never_reads_as_access_and_access_never_as_a_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def at(seconds: int) -> datetime:
        return _T0 + timedelta(seconds=seconds)

    ch = _LocalCH(
        [
            # An older admin request under the email, a newer viewer grant under the login.
            _Row("p1@x.com", "admin", at(1), status="pending"),
            _Row("h1", "viewer", at(5)),
            # The other order: the request is the newest row.
            _Row("h2", "viewer", at(2)),
            _Row("p2@x.com", "admin", at(6), status="pending"),
            # No active row at all: the person reads as their newest request.
            _Row("h3", "writer", at(3), status="pending"),
            _Row("p3@x.com", "admin", at(7), status="pending"),
            # One active row asking for more.
            _Row("solo@x.com", "writer", at(4), pending_role="admin"),
        ],
        [("h1", "p1@x.com"), ("h2", "p2@x.com"), ("h3", "p3@x.com")],
    )
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)

    out = await handle_grants_list(_list())

    assert [
        (r.subject, r.role, r.status, r.pending_role, r.pending_since, list(r.also))
        for r in out.rows
    ] == [
        ("p3@x.com", "admin", "pending", None, at(7), ["h3"]),
        ("p2@x.com", "viewer", "active", "admin", at(6), ["h2"]),
        ("h1", "viewer", "active", "admin", at(1), ["p1@x.com"]),
        ("solo@x.com", "writer", "active", "admin", at(4), []),
    ]


@_needs_ch
async def test_a_walk_of_1200_grants_lists_every_person_once_and_whole(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ch = _twelve_hundred()
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)
    expected = _expected(ch)
    assert len(ch.rows) == 1200 and len(expected) == 850

    pages = await _walk(250)

    assert [len(p.rows) for p in pages] == [250, 250, 250, 100]
    assert [p.truncated for p in pages] == [True, True, True, False]
    assert all(p.next_cursor for p in pages[:-1]) and pages[-1].next_cursor is None
    assert _seen(pages) == expected
    # Every row is behind exactly one listed person, and no person is split.
    spellings = [s.lower() for p in pages for r in p.rows for s in (r.subject, *r.also)]
    assert len(spellings) == len(set(spellings))
    assert {r.subject.lower() for r in ch.rows} <= set(spellings)
    # A page that holds exactly the last people is the last.
    halves = await _walk(425)
    assert [len(p.rows) for p in halves] == [425, 425]
    assert _seen(halves) == expected
    assert halves[-1].truncated is False and halves[-1].next_cursor is None


@_needs_ch
async def test_a_walk_stays_in_place_while_grants_change(monkeypatch: pytest.MonkeyPatch) -> None:
    rows: list[_Row] = []
    ties: list[tuple[str, str]] = []
    for i in range(20):
        handle = f"h{i:02d}"
        ties.append((handle, f"p{i:02d}@example.com"))
        rows.append(_Row(handle, "viewer", _T0 + timedelta(seconds=10 * i)))
        if i % 4 == 0:  # an older grant under the email too
            rows.append(_Row(f"p{i:02d}@example.com", "writer", _T0 + timedelta(seconds=i)))
    ch = _LocalCH(rows, ties)
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)
    before = _expected(ch)

    def change(pages_read: int) -> None:
        if pages_read != 1:
            return
        late = _T0 + timedelta(hours=1)
        # A new grant sorts first, in the part of the list the walk has passed.
        ch.rows.append(_Row("new@example.com", "viewer", late))
        # A person the walk has passed loses their grant; one it has not reached loses
        # theirs, and another keeps only the older of their two spellings.
        for subject in ("h19", "h05", "h08"):
            ch.rows.append(_Row(subject, "viewer", late, revoked=True, version=2))

    pages = await _walk(4, between=change)

    seen = _seen(pages)
    listed = [s for s, *_ in seen]
    assert len(listed) == len(set(listed))
    assert "new@example.com" not in listed and "h05" not in listed
    assert ("p08@example.com", "writer", _T0 + timedelta(seconds=8), []) in seen
    # Everyone the changes did not touch is listed once, where they were.
    untouched = [p for p in before if p[0] not in ("h05", "h08")]
    assert [p for p in seen if p[0] not in ("p08@example.com",)] == untouched
    # The next walk starts from the new grant.
    assert (await handle_grants_list(_list(limit=1))).rows[0].subject == "new@example.com"


@_needs_ch
def test_the_fold_ties_only_the_subjects_has_aliases_admits() -> None:
    subjects = ["", "*", "everyone", "Everyone", "sa:bot", "SA:bot", "user:u1", "group:g"]
    subjects += ["agent:a", "jdoe", "a@b.com", "sa", "users:x"]
    each = "(SELECT arrayJoin({subs:Array(String)}) AS s)"
    rows = run([], f"SELECT s, {has_aliases_sql('s')} AS h FROM {each}", {"subs": subjects})
    assert {r["s"]: bool(r["h"]) for r in rows} == {s: has_aliases(s) for s in subjects}


@_needs_ch
async def test_non_ascii_case_variants_fold_before_pagination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ch = _LocalCH(
        [
            _Row("Éloise", "admin", _T0),
            _Row("éloise", "viewer", _T0 + timedelta(seconds=3)),
            _Row("MÜLLER@x.com", "writer", _T0 + timedelta(seconds=1)),
            _Row("JÖRG", "viewer", _T0 + timedelta(seconds=4)),
        ],
        [("jörg", "müller@x.com")],
    )
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)

    pages = await _walk(1)

    assert _seen(pages) == [
        ("JÖRG", "writer", _T0 + timedelta(seconds=4), ["MÜLLER@x.com"]),
        ("éloise", "admin", _T0 + timedelta(seconds=3), []),
    ]
    assert [p.truncated for p in pages] == [True, False]
