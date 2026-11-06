"""Which erasures fence a move of an original to the bucket (`blobs._covers`): its
namespace's, a namespace list naming it (an unreadable one counts), and any user's
of the org; another namespace's does not."""

from __future__ import annotations

from server.knowledge.store.blobs import _covers, _Erasure, _fence_advice


def test_a_namespace_erasure_covers_its_own_namespace() -> None:
    assert _covers("namespace", "docs", "docs")
    assert not _covers("namespace", "other", "docs")


def test_a_namespace_list_covers_the_namespaces_it_names() -> None:
    assert _covers("namespace_list", '{"namespaces": ["a", "docs"]}', "docs")
    assert _covers("namespace_list", '[" docs "]', "docs")  # a bare array, names stripped
    assert not _covers("namespace_list", '["a", "b"]', "docs")


def test_an_unreadable_namespace_list_counts_as_covering() -> None:
    assert _covers("namespace_list", "not json", "docs")
    assert _covers("namespace_list", '{"namespaces": "docs"}', "docs")


def test_any_user_erasure_of_the_org_covers_it() -> None:
    # Whose an original is can change with any write, so no user's erasure is ruled out.
    assert _covers("user_id", "alice", "docs")
    assert _covers("user_id", "carol", "docs")


def test_a_scope_it_does_not_know_counts_as_covering() -> None:
    assert _covers("org", "acme", "docs")


def test_the_advice_names_the_erasure_that_holds_the_move_longest() -> None:
    done = _Erasure("e1", "user_id", "completed")
    running = _Erasure("e2", "namespace", "in_progress")
    failed = _Erasure("e3", "user_id", "failed")
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
    assert _Erasure("e", "namespace", "in_progress", "docs").deletes_namespace("docs")
    assert _Erasure("e", "namespace_list", "in_progress", '["docs"]').deletes_namespace("docs")
    assert not _Erasure("e", "namespace", "in_progress", "other").deletes_namespace("docs")
    # Fenced on, but not taken for one: a list it cannot read, a user's, a scope it does
    # not know.
    assert not _Erasure("e", "namespace_list", "in_progress", "not json").deletes_namespace("docs")
    assert not _Erasure("e", "user_id", "in_progress", "alice").deletes_namespace("docs")
    assert not _Erasure("e", "org", "in_progress", "acme").deletes_namespace("docs")
