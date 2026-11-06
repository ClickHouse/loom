"""Which erasures fence a move of an original to the bucket (`blobs._covers`): its
namespace's, a namespace list naming it (an unreadable one counts), and an erasure of
one of its uploaders; another namespace's or user's does not."""

from __future__ import annotations

from server.knowledge.store.blobs import _covers


def test_a_namespace_erasure_covers_its_own_namespace() -> None:
    assert _covers("namespace", "docs", "docs", set())
    assert not _covers("namespace", "other", "docs", set())


def test_a_namespace_list_covers_the_namespaces_it_names() -> None:
    assert _covers("namespace_list", '{"namespaces": ["a", "docs"]}', "docs", set())
    assert _covers("namespace_list", '[" docs "]', "docs", set())  # a bare array, names stripped
    assert not _covers("namespace_list", '["a", "b"]', "docs", set())


def test_an_unreadable_namespace_list_counts_as_covering() -> None:
    assert _covers("namespace_list", "not json", "docs", set())
    assert _covers("namespace_list", '{"namespaces": "docs"}', "docs", set())


def test_a_user_erasure_covers_the_originals_of_that_uploader() -> None:
    assert _covers("user_id", "alice", "docs", {"alice", "bob"})
    assert not _covers("user_id", "carol", "docs", {"alice", "bob"})


def test_a_scope_it_does_not_know_counts_as_covering() -> None:
    assert _covers("org", "acme", "docs", set())
