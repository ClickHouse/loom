"""A collection created by its title: the name derives from it, a title names one
collection within a parent (at the top level, one of the caller's), and a name
that is taken says why with a free one to use instead."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

import pydantic
import pytest

from server.auth import context
from server.auth.jwt import AuthInfo
from server.core.errors import CollectionNameTakenError
from server.identity.api.models_collections import CollectionCreateInput, CollectionListInput
from server.identity.app import collections
from server.identity.domain.collection_names import name_candidates
from server.operations.base import Role


class _Store:
    """The directory (`loom.namespaces`) and tree (`loom.collections`) rows of one org."""

    def __init__(self) -> None:
        self.directory: dict[str, dict[str, Any]] = {}
        self.tree: dict[str, dict[str, Any]] = {}
        self.caller = "user:ada"

    def collection(self, name: str, *, owner: str, parent: str = "", title: str = "") -> None:
        self.tree[name] = {"namespace": name, "parent": parent, "owner": owner, "version": 1}
        self.directory[name] = {
            "namespace": name,
            "kind": "collection",
            "active": True,
            "title": title,
            "description": "",
            "updated_at": datetime(2026, 1, 1),
        }


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Store]:
    s = _Store()

    @asynccontextmanager
    async def _held(*_a: Any) -> Any:
        yield

    async def _none(*_a: Any, **_k: Any) -> None:
        return None

    async def _false(*_a: Any, **_k: Any) -> bool:
        return False

    async def _true(*_a: Any, **_k: Any) -> bool:
        return True

    async def _no_grants(*_a: Any, **_k: Any) -> list[Any]:
        return []

    async def _zero(*_a: Any, **_k: Any) -> int:
        return 0

    async def fetch_row(_org: str, ns: str, **_k: Any) -> dict[str, Any] | None:
        row = s.directory.get(ns)
        return dict(row) if row else None

    async def labels(_org: str, names: list[str], **_k: Any) -> dict[str, dict[str, str]]:
        return {
            n: {"title": s.directory[n]["title"], "description": s.directory[n]["description"]}
            for n in names
            if n in s.directory and (s.directory[n]["title"] or s.directory[n]["description"])
        }

    async def fetch(_org: str, ns: str, **_k: Any) -> dict[str, Any] | None:
        row = s.tree.get(ns)
        return dict(row) if row and not row.get("deleted") else None

    async def children(_org: str, parent: str, *, owner: str = "", **_k: Any) -> list[Any]:
        return [
            dict(r)
            for r in s.tree.values()
            if r["parent"] == parent and not r.get("deleted") and (not owner or r["owner"] == owner)
        ]

    async def write(row: dict[str, Any], **_k: Any) -> None:
        s.tree[row["namespace"]] = row

    class _CH:
        async def insert(self, table: str, rows: list[dict[str, Any]]) -> None:
            assert table == "loom.namespaces"
            for row in rows:
                s.directory[row["namespace"]] = row

    monkeypatch.setattr(collections, "account_change", _held)
    monkeypatch.setattr(collections, "collections_change", _held)
    monkeypatch.setattr(collections.namespaces_repo, "fetch_row", fetch_row)
    monkeypatch.setattr(collections.namespaces_repo, "labels", labels)
    monkeypatch.setattr(collections.collections_repo, "fetch", fetch)
    monkeypatch.setattr(collections.collections_repo, "children", children)
    monkeypatch.setattr(collections.collections_repo, "write", write)
    monkeypatch.setattr(collections.collections_repo, "stored_version", _zero)
    monkeypatch.setattr(collections.grant_cache, "changed", _none)
    monkeypatch.setattr(collections, "insert_admin_grant", _none)
    monkeypatch.setattr(collections, "get_client", _CH)
    monkeypatch.setattr(collections.grants_repo, "list_active", _no_grants)
    monkeypatch.setattr(collections.registration, "refused_as_in_use", _false)
    monkeypatch.setattr(collections, "_may", _true)
    monkeypatch.setattr(collections, "check_placement", lambda *_a: None)
    monkeypatch.setattr(collections.sharing, "load_tree", _none)
    monkeypatch.setattr(collections, "resolve_subject", lambda *_a, **_k: s.caller)
    context.set_current_auth(
        AuthInfo(
            subject=s.caller,
            org="acme",
            namespaces=("*",),
            roles=(Role.WRITER,),
            is_service_account=False,
        )
    )
    yield s
    context.set_current_auth(None)


def _create(**fields: Any) -> Any:
    return asyncio.run(
        collections.handle_collection_create(CollectionCreateInput(org="acme", **fields))
    )


def test_a_title_derives_the_name_and_a_retry_answers_the_same_collection(store: _Store) -> None:
    first = _create(title="Q3 Plans", derive_name=True)
    assert (first.namespace, first.created, first.title) == ("col-q3-plans", True, "Q3 Plans")
    assert store.directory["col-q3-plans"]["title"] == "Q3 Plans"
    assert store.tree["col-q3-plans"]["owner"] == "user:ada"
    again = _create(title="  q3   PLANS ", derive_name=True)
    assert (again.namespace, again.created) == ("col-q3-plans", False)
    assert len(store.tree) == 1


def test_a_title_another_person_used_derives_a_suffixed_name(store: _Store) -> None:
    store.collection("col-specs", owner="user:bob", title="Specs")
    made = _create(title="Specs", derive_name=True)
    assert made.created and made.namespace != "col-specs"
    assert made.namespace == list(name_candidates("acme", "user:ada", "", "Specs"))[1]
    assert store.directory[made.namespace]["title"] == "Specs"


def test_under_a_parent_a_title_names_the_child_that_has_it(store: _Store) -> None:
    store.collection("col-team", owner="user:bob")
    store.collection("col-team-docs", owner="user:bob", parent="col-team", title="Docs")
    same = _create(title="docs", parent="col-team", derive_name=True)
    assert (same.namespace, same.created) == ("col-team-docs", False)


def test_an_untitled_child_with_the_plain_name_is_reused_and_given_the_title(
    store: _Store,
) -> None:
    store.collection("col-team", owner="user:bob")
    store.collection("col-reports", owner="user:bob", parent="col-team")
    same = _create(title="Reports", parent="col-team", derive_name=True)
    assert (same.namespace, same.created) == ("col-reports", False)
    assert store.directory["col-reports"]["title"] == "Reports"


def test_two_titles_may_repeat_under_different_parents(store: _Store) -> None:
    store.collection("col-a", owner="user:ada")
    store.collection("col-b", owner="user:ada")
    one = _create(title="Notes", parent="col-a", derive_name=True)
    two = _create(title="Notes", parent="col-b", derive_name=True)
    assert one.created and two.created and one.namespace != two.namespace


def test_a_create_cut_short_is_finished_by_its_retry(store: _Store) -> None:
    # The tree row and grant landed; the directory row did not.
    store.tree["col-handbook"] = {
        "namespace": "col-handbook",
        "parent": "",
        "owner": "user:ada",
        "version": 1,
    }
    made = _create(title="Handbook", derive_name=True)
    assert (made.namespace, made.created) == ("col-handbook", True)
    assert store.directory["col-handbook"]["title"] == "Handbook"


def test_a_deleted_name_is_refused_with_why_and_a_free_one(store: _Store) -> None:
    store.directory["col-x"] = {"namespace": "col-x", "kind": "collection", "active": False}
    with pytest.raises(CollectionNameTakenError) as e:
        _create(namespace="col-x")
    detail = e.value.to_detail()
    assert detail["code"] == "COLLECTION_NAME_TAKEN" and e.value.status_code == 409
    assert detail["deleted"] is True and "never used again" in detail["message"]
    assert detail["suggestion"].startswith("col-x-") and detail["suggestion"] not in store.tree


def test_a_name_in_use_is_refused_as_used_with_a_free_one(store: _Store) -> None:
    store.collection("col-x", owner="user:bob")
    with pytest.raises(CollectionNameTakenError) as e:
        _create(namespace="col-x")
    detail = e.value.to_detail()
    assert detail["deleted"] is False and "already used" in detail["message"]
    assert detail["suggestion"].startswith("col-x-")


def test_two_titles_with_nothing_to_spell_derive_two_names() -> None:
    assert next(name_candidates("acme", "u", "", "日本")) != next(
        name_candidates("acme", "u", "", "中国")
    )


def test_an_untitled_child_is_reused_but_titled_only_by_its_admin(
    store: _Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.collection("col-team", owner="user:bob")
    store.collection("col-reports", owner="user:bob", parent="col-team")

    async def _writer_only(role: Role, *_a: Any) -> bool:
        return role != Role.ADMIN

    monkeypatch.setattr(collections, "_may", _writer_only)
    same = _create(title="Reports", parent="col-team", derive_name=True)
    assert (same.namespace, same.created, same.title) == ("col-reports", False, "")
    assert store.directory["col-reports"]["title"] == ""


def test_derive_name_ignores_the_namespace_a_client_filled_in(store: _Store) -> None:
    # MCP and the SDK fill a blank namespace with the caller's own.
    store.collection("col-home", owner="user:ada")
    made = _create(namespace="col-home", title="Plans", derive_name=True)
    assert (made.namespace, made.created) == ("col-plans", True)
    assert store.directory["col-home"]["title"] == ""


def test_an_explicit_name_of_ones_own_is_given_the_title_it_lacked(store: _Store) -> None:
    store.collection("col-mine", owner="user:ada")
    same = _create(namespace="col-mine", title="Mine")
    assert (same.created, same.title) == (False, "Mine")
    assert store.directory["col-mine"]["title"] == "Mine"


def test_collection_list_carries_the_title(store: _Store, monkeypatch: pytest.MonkeyPatch) -> None:
    store.collection("col-q3", owner="user:ada", title="Q3 Plans")

    class _Tree:
        parent = {"col-q3": ""}

    async def _tree(*_a: Any) -> _Tree:
        return _Tree()

    async def _readable(*_a: Any) -> list[str]:
        return ["col-q3"]

    async def _roles(*_a: Any) -> dict[str, Role]:
        return {"col-q3": Role.ADMIN}

    monkeypatch.setattr(collections.sharing, "tree", _tree)
    monkeypatch.setattr(collections.sharing, "readable", _readable)
    monkeypatch.setattr(collections.sharing, "roles", _roles)
    out = asyncio.run(collections.handle_collection_list(CollectionListInput(org="acme")))
    assert [(c.namespace, c.title) for c in out.collections] == [("col-q3", "Q3 Plans")]


@pytest.mark.parametrize(
    ("fields", "why"),
    [
        ({}, "a name, or a title"),
        ({"title": "Notes"}, "a name, or a title"),
        ({"title": "   ", "derive_name": True}, "needs a title"),
        ({"title": "a\x00b"}, "control or invisible"),
        ({"title": "Payroll\u200b", "derive_name": True}, "control or invisible"),
        ({"title": "\u202eevil", "derive_name": True}, "control or invisible"),
        ({"title": "\u3164", "derive_name": True}, "control or invisible"),
        ({"title": "Payroll\u2800", "derive_name": True}, "control or invisible"),
        ({"title": "a\u2028b", "derive_name": True}, "control or invisible"),
        ({"title": "Pay\u2062roll", "derive_name": True}, "control or invisible"),
        ({"title": "a\u034fb", "derive_name": True}, "control or invisible"),
        ({"title": "x" * 201}, "at most 200"),
        ({"title": " " + "x" * 201, "derive_name": True}, "at most 200"),
    ],
)
def test_a_create_needs_a_name_or_a_clean_title(fields: dict[str, Any], why: str) -> None:
    with pytest.raises(pydantic.ValidationError, match=why):
        CollectionCreateInput(org="acme", **fields)


@pytest.mark.parametrize(
    ("title", "first"),
    [
        ("Café Notes", "col-cafe-notes"),
        ("Straße", "col-strasse"),
        ("Kırmızı", "col-kirmizi"),
        ("Me", "col-c-me"),
        ("日本", "col-c-"),
        ("a" * 39 + " b", "col-" + "a" * 39),
    ],
)
def test_the_names_a_title_derives(title: str, first: str) -> None:
    names = list(name_candidates("acme", "user:ada", "", title))
    assert names[0].startswith(first)
    assert len(set(names)) == len(names) == 16
    assert all(not n.startswith("col-me-") for n in names)
    # The same request derives the same names: a retry walks them again.
    assert names == list(name_candidates("acme", "user:ada", "", title))


def test_a_retry_the_sibling_read_misses_still_finds_ones_own(
    store: _Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _create(title="Q3 Plans", derive_name=True)

    async def _lagging(*_a: Any) -> None:
        return None  # a replica whose read has not seen the first create

    monkeypatch.setattr(collections, "_titled_sibling", _lagging)
    again = _create(title="Q3 Plans", derive_name=True)
    assert (again.namespace, again.created) == (first.namespace, False)
    assert len(store.tree) == 1


def test_a_token_scoped_by_ns_write_is_handed_no_name_outside_it(store: _Store) -> None:
    store.collection("col-specs", owner="user:bob", title="Specs")
    context.set_current_auth(
        AuthInfo(
            subject="user:ada",
            org="acme",
            namespaces=("*",),
            roles=(Role.WRITER,),
            is_service_account=False,
            write_namespaces=("col-elsewhere",),
        )
    )
    with pytest.raises(collections.AuthInsufficientError, match="writes only"):
        _create(title="Specs", derive_name=True)


def _scoped_to(*names: str) -> None:
    context.set_current_auth(
        AuthInfo(
            subject="user:ada",
            org="acme",
            namespaces=("*",),
            roles=(Role.WRITER,),
            is_service_account=False,
            write_namespaces=names,
        )
    )


def test_a_scoped_token_is_not_answered_with_a_sibling_it_cannot_write(store: _Store) -> None:
    store.collection("col-team", owner="user:bob")
    store.collection("col-team-docs", owner="user:bob", parent="col-team", title="Docs")
    mine = list(name_candidates("acme", "user:ada", "col-team", "Docs"))[1]
    _scoped_to(mine)
    made = _create(title="Docs", parent="col-team", derive_name=True)
    assert (made.namespace, made.created) == (mine, True)


def test_a_scoped_token_is_offered_only_a_name_it_may_write(store: _Store) -> None:
    store.collection("col-x", owner="user:bob")
    _scoped_to("col-x")
    with pytest.raises(CollectionNameTakenError) as e:
        _create(namespace="col-x")
    assert e.value.to_detail()["suggestion"] == ""
    allowed = list(name_candidates("acme", "user:ada", "", "x"))[2]
    _scoped_to("col-x", allowed)
    with pytest.raises(CollectionNameTakenError) as e:
        _create(namespace="col-x")
    assert e.value.to_detail()["suggestion"] == allowed


def test_the_audit_row_of_a_titled_create_is_the_collections() -> None:
    from server.identity.api.ops_collections import collection_create
    from server.transport.post_rows import _audit_namespace

    inp = CollectionCreateInput(org="acme", namespace="agent-home", title="Plans", derive_name=True)
    made = {"namespace": "col-plans-ab12", "parent": "", "created": True, "title": "Plans"}
    assert _audit_namespace(collection_create, inp, made, "agent-home") == "col-plans-ab12"
    assert collection_create.audit_subject is not None
    assert collection_create.audit_subject(inp, made)["namespace"] == "col-plans-ab12"


def test_a_top_level_title_skips_a_collection_its_creator_no_longer_writes(
    store: _Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.collection("col-q3-plans", owner="user:ada", title="Q3 Plans")

    async def _revoked(role: Role, org: str, namespace: str) -> bool:
        return namespace != "col-q3-plans"  # another admin revoked the creator's grant

    monkeypatch.setattr(collections, "_may", _revoked)
    made = _create(title="Q3 Plans", derive_name=True)
    assert made.created and made.namespace != "col-q3-plans"


@pytest.mark.parametrize("answer", ["", RuntimeError("no namespace")])
def test_an_audit_namespace_that_gives_none_files_the_row_under_the_gates(
    answer: str | Exception,
) -> None:
    from types import SimpleNamespace

    from server.transport.post_rows import _audit_namespace

    def _hook(_i: Any, _r: Any) -> str:
        if isinstance(answer, Exception):
            raise answer
        return answer

    op = SimpleNamespace(audit_namespace=_hook)
    assert _audit_namespace(op, None, {}, "gated") == "gated"  # type: ignore[arg-type]


@pytest.mark.parametrize("derive", [False, True])
def test_only_its_admin_titles_an_untitled_collection(
    store: _Store, monkeypatch: pytest.MonkeyPatch, derive: bool
) -> None:
    store.collection("col-team", owner="user:bob")
    store.collection("col-reports", owner="user:ada", parent="col-team")

    async def _no_admin(role: Role, *_a: Any) -> bool:
        return role != Role.ADMIN  # ada's direct grant revoked; she writes the parent

    monkeypatch.setattr(collections, "_may", _no_admin)
    fields = (
        {"title": "Reports", "derive_name": True}
        if derive
        else {"namespace": "col-reports", "title": "Anything"}
    )
    same = _create(parent="col-team", **fields)
    assert (same.namespace, same.created, same.title) == ("col-reports", False, "")
    assert store.directory["col-reports"]["title"] == ""


def test_a_title_of_200_characters_with_spaces_around_it_is_kept() -> None:
    inp = CollectionCreateInput(org="acme", title=f"  {'x' * 200} ", derive_name=True)
    assert inp.title == "x" * 200


@pytest.mark.parametrize(
    "title",
    [
        "🧑\u200d💻 Dev notes",
        "می\u200cخواهم",
        "🏴\U000e0067\U000e0062\U000e007f flags",
        "co\u00adop",
    ],
)
def test_a_title_keeps_the_joiners_emoji_and_scripts_need(title: str) -> None:
    assert CollectionCreateInput(org="acme", title=title, derive_name=True).title == title


def test_a_scoped_token_outside_the_plain_name_matches_no_untitled_collection_by_name(
    store: _Store,
) -> None:
    store.collection("col-team", owner="user:bob")
    suffixed = list(name_candidates("acme", "user:ada", "col-team", "Reports"))[1]
    # An untitled child named like the second name: not one the title spells.
    store.collection(suffixed, owner="user:ada", parent="col-team")
    third = list(name_candidates("acme", "user:ada", "col-team", "Reports"))[2]
    _scoped_to(suffixed, third)
    made = _create(title="Reports", parent="col-team", derive_name=True)
    assert (made.namespace, made.created) == (third, True)


def test_two_spellings_of_one_title_name_one_collection(store: _Store) -> None:
    nfc, nfd = "Caf\u00e9", "Cafe\u0301"
    first = _create(title=nfc, derive_name=True)
    again = _create(title=nfd, derive_name=True)
    assert (again.namespace, again.created) == (first.namespace, False)
    assert CollectionCreateInput(org="acme", title=nfd, derive_name=True).title == nfc


@pytest.mark.parametrize(
    "look_alike",
    [
        "Q3 Pl\u00adans",
        "Q3 Plans\ufe0f",
        "Q3 Pla\U000e0100ns",
    ],
)
def test_a_title_that_looks_the_same_is_the_same_title(store: _Store, look_alike: str) -> None:
    first = _create(title="Q3 Plans", derive_name=True)
    again = _create(title=look_alike, derive_name=True)
    assert (again.namespace, again.created) == (first.namespace, False)


def test_ones_own_collection_shared_with_others_is_still_ones_own(
    store: _Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.collection("col-x", owner="user:ada", title="X")

    async def _shared(*_a: Any, **_k: Any) -> list[dict[str, str]]:
        return [{"subject": "user:ada"}, {"subject": "user:bob"}]

    monkeypatch.setattr(collections.grants_repo, "list_active", _shared)
    named = _create(namespace="col-x")
    assert (named.namespace, named.created) == ("col-x", False)

    async def _lagging(*_a: Any) -> None:
        return None

    monkeypatch.setattr(collections, "_titled_sibling", _lagging)
    derived = _create(title="X", derive_name=True)
    assert (derived.namespace, derived.created) == ("col-x", False)


@pytest.mark.parametrize("stored_ahead", [False, True])
def test_a_title_is_never_stamped_behind_the_row_it_replaces(
    store: _Store, monkeypatch: pytest.MonkeyPatch, stored_ahead: bool
) -> None:
    store.collection("col-mine", owner="user:ada")  # untitled
    now = datetime(2026, 3, 1, 12, 0, 0)
    stored = datetime(2026, 3, 1, 12, 0, 5) if stored_ahead else datetime(2026, 1, 1)
    store.directory["col-mine"]["updated_at"] = stored  # another replica's clock runs ahead
    monkeypatch.setattr(collections, "naive_utc_now_seconds", lambda: now)
    _create(namespace="col-mine", title="Mine")
    assert store.directory["col-mine"]["updated_at"] == max(now, stored)


def test_a_delete_is_never_stamped_behind_the_row_it_replaces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.identity.app import namespace as ns_app

    now = datetime(2026, 3, 1, 12, 0, 0)
    ahead = datetime(
        2026, 3, 1, 12, 0, 5
    )  # a title row stamped by a replica whose clock runs ahead
    inserted: list[dict[str, Any]] = []

    @asynccontextmanager
    async def _held(*_a: Any) -> Any:
        yield

    async def _row(*_a: Any, **_k: Any) -> dict[str, Any]:
        return {"namespace": "team", "kind": "memory", "active": True, "updated_at": ahead}

    async def _none(*_a: Any, **_k: Any) -> None:
        return None

    class _CH:
        async def insert(self, _table: str, rows: list[dict[str, Any]]) -> None:
            inserted.extend(rows)

    monkeypatch.setattr(ns_app, "account_change", _held)
    monkeypatch.setattr(ns_app, "get_client", _CH)
    monkeypatch.setattr(ns_app, "naive_utc_now_seconds", lambda: now)
    monkeypatch.setattr(ns_app.namespaces_repo, "fetch_row", _row)
    monkeypatch.setattr(ns_app.grant_cache, "changed", _none)
    asyncio.run(ns_app.delete_namespace("acme", "team"))
    assert inserted[0]["active"] is False and inserted[0]["updated_at"] == ahead


def test_titles_that_draw_differently_stay_two(store: _Store) -> None:
    flag = "\U0001f3f4{} Football"
    england = flag.format("\U000e0067\U000e0062\U000e0065\U000e006e\U000e0067\U000e007f")
    scotland = flag.format("\U000e0067\U000e0062\U000e0073\U000e0063\U000e0074\U000e007f")
    first = _create(title=england, derive_name=True)
    second = _create(title=scotland, derive_name=True)
    assert first.created and second.created and first.namespace != second.namespace


@pytest.mark.parametrize("owner", ["user:ada", "user:bob"])
def test_a_collection_whose_delete_stopped_short_is_a_deleted_one(
    store: _Store, owner: str
) -> None:
    store.collection("col-docs", owner=owner, title="Docs")
    store.directory["col-docs"]["active"] = False  # the tree row is still live
    made = _create(title="Docs", derive_name=True)
    assert made.created and made.namespace != "col-docs"
    with pytest.raises(CollectionNameTakenError) as e:
        _create(namespace="col-docs")
    assert e.value.to_detail()["deleted"] is True
