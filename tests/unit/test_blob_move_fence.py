"""Which erasures fence a move of an original to the bucket (`scopes.may_cover`): its
namespace's, a namespace list naming it (an unreadable one counts), and any user's
of the org; another namespace's does not."""

from __future__ import annotations

from server.erasure.domain.scopes import may_cover
from server.knowledge.app.originals import _Erasure, _fence_advice


def test_a_namespace_erasure_covers_its_own_namespace() -> None:
    assert may_cover("namespace", "docs", "docs")
    assert not may_cover("namespace", "other", "docs")


def test_a_namespace_list_covers_the_namespaces_it_names() -> None:
    assert may_cover("namespace_list", '{"namespaces": ["a", "docs"]}', "docs")
    assert may_cover("namespace_list", '[" docs "]', "docs")  # a bare array, names stripped
    assert not may_cover("namespace_list", '["a", "b"]', "docs")


def test_an_unreadable_namespace_list_counts_as_covering() -> None:
    assert may_cover("namespace_list", "not json", "docs")
    assert may_cover("namespace_list", '{"namespaces": "docs"}', "docs")


def test_any_user_erasure_of_the_org_covers_it() -> None:
    # Whose an original is can change with any write, so no user's erasure is ruled out.
    assert may_cover("user_id", "alice", "docs")
    assert may_cover("user_id", "carol", "docs")


def test_a_scope_it_does_not_know_counts_as_covering() -> None:
    assert may_cover("org", "acme", "docs")


def test_the_advice_names_the_erasure_that_holds_the_move_longest() -> None:
    done = _Erasure("e1", "user_id", "alice", "completed")
    running = _Erasure("e2", "namespace", "docs", "in_progress")
    failed = _Erasure("e3", "user_id", "bob", "failed")
    advice = _fence_advice([done, running], "covers it")
    assert advice == "erasure e2 (namespace, in_progress) covers it; run again once it has finished"
    # A failed one: the recovery worker retries it, and past its attempts a re-verify.
    assert _fence_advice([done, running, failed], "covers it") == (
        "erasure e3 (user_id, failed) covers it; run again once the recovery worker has "
        "completed it, or complete it with loom erasure-re-verify --resume"
    )
    # Only one that finished just now: a minute's wait, said where it reads right.
    assert _fence_advice([done], "overlapped its copy, and it stays in ClickHouse") == (
        "erasure e1 (user_id, completed), which finished just now, overlapped its copy, and "
        "it stays in ClickHouse; run again in a minute"
    )


def test_only_an_erasure_that_certainly_deletes_the_namespace_takes_a_move_back() -> None:
    assert _Erasure("e", "namespace", "docs", "in_progress").deletes_namespace("docs")
    assert _Erasure("e", "namespace_list", '["docs"]', "in_progress").deletes_namespace("docs")
    assert not _Erasure("e", "namespace", "other", "in_progress").deletes_namespace("docs")
    # Fenced on, but not taken for one: a list it cannot read, a user's, a scope it does
    # not know.
    assert not _Erasure("e", "namespace_list", "not json", "in_progress").deletes_namespace("docs")
    assert not _Erasure("e", "user_id", "alice", "in_progress").deletes_namespace("docs")
    assert not _Erasure("e", "org", "acme", "in_progress").deletes_namespace("docs")
