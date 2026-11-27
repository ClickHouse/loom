"""Check native policy definitions before replacing the analyst policies."""

from __future__ import annotations

import pytest

from server.insights.domain.observation_policy import analyst_policy_catalog_allows_writes

_SCOPE = (
    "CREATE ROW POLICY loom_analyst_scope ON loom.memory "
    "AS PERMISSIVE FOR SELECT USING org = 'example' TO loom_analyst"
)
_OTHERS = (
    "CREATE ROW POLICY loom_analyst_others ON loom.memory "
    "AS PERMISSIVE FOR SELECT USING 1 TO ALL EXCEPT loom_analyst"
)


@pytest.mark.parametrize("clause", ["AS PERMISSIVE ", ""])
def test_generated_policy_pair_allows_a_rewrite(clause):
    scope = _SCOPE.replace("AS PERMISSIVE ", clause)
    others = _OTHERS.replace("AS PERMISSIVE ", clause)
    assert analyst_policy_catalog_allows_writes((scope, others), "loom")
    assert analyst_policy_catalog_allows_writes((others,), "loom")
    assert analyst_policy_catalog_allows_writes((), "loom")


def test_a_missing_companion_can_be_created():
    assert analyst_policy_catalog_allows_writes((_SCOPE,), "loom")


def test_foreign_restrictive_policy_can_stay():
    restrictive = _SCOPE.replace("loom_analyst_scope", "foreign").replace(
        "AS PERMISSIVE", "AS RESTRICTIVE"
    )
    assert analyst_policy_catalog_allows_writes((restrictive,), "loom")


@pytest.mark.parametrize(
    "replacement",
    [
        "USING org = 'example' TO ALL EXCEPT loom_analyst",
        "USING 1 TO loom_analyst",
        "USING 1 TO ALL EXCEPT another_role",
        "USING 1 TO ALL EXCEPT loom_analyst, service",
        "USING 1 TO ALL EXCEPT `loom_analyst TO service`",
    ],
)
def test_a_familiar_policy_name_requires_the_expected_definition(replacement):
    defective = _OTHERS.replace("USING 1 TO ALL EXCEPT loom_analyst", replacement)
    assert not analyst_policy_catalog_allows_writes((_SCOPE, defective), "loom")


@pytest.mark.parametrize("target", ["loom.*", "*.*"])
def test_database_wildcard_policies_are_included_in_the_inventory(target):
    assert not analyst_policy_catalog_allows_writes(
        (_SCOPE.replace("loom.memory", target), _OTHERS), "loom"
    )


def test_unrelated_policy_allows_a_rewrite():
    assert analyst_policy_catalog_allows_writes(
        (_SCOPE.replace("loom.memory", "other.agent_events"),), "loom"
    )


def test_quoted_role_text_and_policy_expressions_cannot_fake_an_unrestricted_companion():
    fake = _OTHERS.replace("USING 1", "USING '1 TO ALL EXCEPT loom_analyst' = 'no'")
    assert not analyst_policy_catalog_allows_writes((_SCOPE, fake), "loom")
    assert not analyst_policy_catalog_allows_writes((_SCOPE, _OTHERS, _OTHERS), "loom")


@pytest.mark.parametrize("index", [0, 1])
def test_a_restrictive_policy_cannot_reuse_a_loom_name(index):
    policies = [_SCOPE, _OTHERS]
    policies[index] = policies[index].replace("AS PERMISSIVE", "AS RESTRICTIVE")
    assert not analyst_policy_catalog_allows_writes(tuple(policies), "loom")


def test_native_quoted_names_and_to_inside_a_literal_preserve_the_actual_recipient():
    scope = _SCOPE.replace("loom.memory", "`loom`.`agent_events`").replace(
        "org = 'example'", "position('it\\'s TO service ([)]', 'TO') > 0"
    )
    others = _OTHERS.replace("loom.memory", '"loom"."agent_events"')
    assert analyst_policy_catalog_allows_writes((scope, others), "loom")


@pytest.mark.parametrize(
    "expression",
    ["1 /* hidden */", "1 -- hidden\n", "1 # hidden\n", "'unclosed", "([)]", "$tag$TO$tag$"],
)
def test_unknown_policy_syntax_refuses_a_rewrite(expression):
    assert not analyst_policy_catalog_allows_writes(
        (_SCOPE.replace("org = 'example'", expression), _OTHERS), "loom"
    )


def test_unsupported_quoted_target_escapes_refuse_instead_of_looking_unrelated():
    escaped = _SCOPE.replace("loom.memory", r"`lo\om`.memory")
    assert not analyst_policy_catalog_allows_writes((escaped, _OTHERS), "loom")
