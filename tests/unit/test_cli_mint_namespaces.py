"""`loom mint-token --namespaces`: a comma-joined list must not mint one bogus
namespace named "a,b" (a token that 403s everywhere and reads as broken
membership to the caller)."""

from __future__ import annotations

from server.cli.access import _invalid_namespace_names, _normalize_namespace_args


def test_comma_and_space_separated_lists_are_the_same():
    assert _normalize_namespace_args(["a,b", "c"]) == ["a", "b", "c"]
    assert _normalize_namespace_args(["a", "b", "c"]) == ["a", "b", "c"]
    assert _normalize_namespace_args(["a, b ,,c,"]) == ["a", "b", "c"]
    assert _normalize_namespace_args(["*"]) == ["*"]
    assert _normalize_namespace_args(None) == []


def test_order_kept_and_deduped():
    assert _normalize_namespace_args(["b,a", "b", "a"]) == ["b", "a"]


def test_names_with_whitespace_are_rejected():
    assert _invalid_namespace_names(["ok-name", "bad name", "tab\tname"]) == [
        "bad name",
        "tab\tname",
    ]
    assert _invalid_namespace_names(["idfull-off", "support-bot"]) == []
