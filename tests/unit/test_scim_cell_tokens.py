"""Cell token syntax and configuration never infer an unread first identity."""

from __future__ import annotations

import importlib

import pytest

SECRET = "a" * 64


def policy():
    return importlib.import_module("server.core.scim_tokens")


@pytest.mark.parametrize("name", ["a", "a-b", "x" * 32])
@pytest.mark.parametrize("size", [32, 33, 64])
def test_named_token_uses_the_complete_exact_grammar(name, size):
    assert policy().token_cell(f"lsc_{name}_{'a' * size}") == name


@pytest.mark.parametrize(
    "token",
    [
        f"lsc__{SECRET}",
        f"lsc_{'x' * 33}_{SECRET}",
        f"lsc_Name_{SECRET}",
        f"lsc_name_{'A' * 32}",
        f"lsc_name_{'a' * 31}",
        f"lsc_name_{SECRET}_tail",
        f"lsc_name_{SECRET}\n",
        f"lsc_name_{SECRET}\x00",
        f"lsc_na_me_{SECRET}",
        f"lsc_namé_{SECRET}",
    ],
)
def test_reserved_prefix_must_parse_in_full(token):
    with pytest.raises(ValueError):
        policy().token_cell(token)


@pytest.mark.parametrize("token", ["old-secret", "LSC_name_secret", "eyJ.payload.signature"])
def test_only_the_reserved_lowercase_prefix_names_a_cell(token):
    assert policy().token_cell(token) is None


def test_splitter_preserves_duplicates_and_only_ignores_empty_entries():
    assert policy().tokens(" , first, second ,first,, ") == ["first", "second", "first"]


@pytest.mark.parametrize("first", [None, (True, "first"), (False, "first")])
def test_no_cell_keeps_its_existing_configuration_policy(first):
    assert policy().cell_policy_problem("old,lsc_malformed,third,fourth", "", first) == ""


def test_scim_off_needs_no_first_identity():
    assert policy().cell_policy_problem(" , ", "first", None) == ""


@pytest.mark.parametrize("raw", ["old-secret", f"lsc_first_{SECRET}"])
def test_valid_configuration_with_unknown_first_identity_stays_undecided(raw):
    assert policy().cell_policy_problem(raw, "first", None) is None


@pytest.mark.parametrize("first", [(True, "first"), (False, "first")])
def test_own_named_token_is_valid_on_either_known_cell_kind(first):
    assert policy().cell_policy_problem(f"lsc_first_{SECRET}", "first", first) == ""


def test_unnamed_tokens_require_the_exact_observed_first_tuple():
    assert policy().cell_policy_problem("old-secret", "first", (True, "first")) == ""
    assert policy().cell_policy_problem("old-secret", "first", (False, "first"))
    assert policy().cell_policy_problem("old-secret", "first", (True, "another"))


@pytest.mark.parametrize(
    "raw",
    [
        f"lsc_another_{SECRET}",
        f"lsc_first_{'A' * 32}",
        ",".join([f"lsc_first_{SECRET}"] * 3),
    ],
)
@pytest.mark.parametrize("first", [None, (True, "first"), (False, "first")])
def test_invalid_configuration_refuses_even_before_a_first_read(raw, first):
    problem = policy().cell_policy_problem(raw, "first", first)
    assert isinstance(problem, str) and problem
    assert SECRET not in problem and "A" * 32 not in problem


def test_rotation_accepts_two_entries_and_counts_duplicate_entries():
    raw = f"lsc_first_{SECRET},lsc_first_{'b' * 64}"
    assert policy().cell_policy_problem(raw, "first", (False, "first")) == ""
    assert policy().cell_policy_problem(raw + f",lsc_first_{SECRET}", "first", None)


def test_named_first_configuration_cannot_use_a_differently_named_cached_row():
    assert policy().cell_policy_problem(f"lsc_first_{SECRET}", "first", (True, "another"))


def test_mixed_named_and_unnamed_rotation_is_only_for_the_first_cell():
    raw = f"old-secret,lsc_first_{SECRET}"
    assert policy().cell_policy_problem(raw, "first", (True, "first")) == ""
    assert policy().cell_policy_problem(raw, "first", (False, "first"))
