"""Which erasures fence a move of an original to the bucket (`blobs._covers`): its
namespace's, a namespace list naming it (an unreadable one counts), and any user's
of the org; another namespace's does not."""

from __future__ import annotations

from server.knowledge.store.blobs import _covers


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
