"""Native access-store definitions must prove visibility, including implicit deny."""

from __future__ import annotations

import pytest

from server.insights.domain.observation_policy import observer_policies_allow

_SCOPE = (
    "CREATE ROW POLICY loom_analyst_scope ON loom.agent_events "
    "AS PERMISSIVE FOR SELECT USING mirror_marked = 0 TO loom_analyst"
)
_OTHERS = (
    "CREATE ROW POLICY loom_analyst_others ON loom.agent_events "
    "AS PERMISSIVE FOR SELECT USING 1 TO ALL EXCEPT loom_analyst"
)


@pytest.mark.parametrize("clause", ["AS PERMISSIVE ", ""])
def test_generated_event_pair_proves_observer_visibility(clause):
    scope = _SCOPE.replace("AS PERMISSIVE ", clause)
    others = _OTHERS.replace("AS PERMISSIVE ", clause)
    assert observer_policies_allow((scope, others), "loom")
    assert observer_policies_allow((others,), "loom")
    assert observer_policies_allow((), "loom")


def test_analyst_only_policy_implicitly_denies_the_observer_without_its_companion():
    assert not observer_policies_allow((_SCOPE,), "loom")
    other_table = _OTHERS.replace("loom.agent_events", "loom.agent_events_private")
    assert not observer_policies_allow((_SCOPE, other_table), "loom")


@pytest.mark.parametrize(
    "table", ["processes", "row_policies", "clusters", "tables", "constraints", "*"]
)
def test_system_policies_cannot_hide_process_or_metadata_populations(table):
    hidden = _SCOPE.replace("loom.agent_events", "system." + table)
    assert not observer_policies_allow((hidden, _OTHERS), "loom")


@pytest.mark.parametrize(
    "replacement",
    [
        "USING mirror_marked = 0 TO ALL EXCEPT loom_analyst",
        "USING 1 TO loom_analyst",
        "USING 1 TO ALL EXCEPT another_role",
        "USING 1 TO ALL EXCEPT loom_analyst, service",
        "USING 1 TO ALL EXCEPT `loom_analyst TO service`",
    ],
)
def test_a_familiar_policy_name_cannot_replace_its_actual_visibility_definition(replacement):
    defective = _OTHERS.replace("USING 1 TO ALL EXCEPT loom_analyst", replacement)
    assert not observer_policies_allow((_SCOPE, defective), "loom")


@pytest.mark.parametrize("target", ["loom.*", "*.*", "system.*"])
def test_database_wildcard_policies_are_included_in_the_inventory(target):
    assert not observer_policies_allow(
        (_SCOPE.replace("loom.agent_events", target), _OTHERS), "loom"
    )


def test_unrelated_policy_does_not_block_an_event_proof():
    assert observer_policies_allow(
        (_SCOPE.replace("loom.agent_events", "other.agent_events"),), "loom"
    )


def test_quoted_role_text_and_policy_expressions_cannot_fake_an_unrestricted_companion():
    fake = _OTHERS.replace("USING 1", "USING '1 TO ALL EXCEPT loom_analyst' = 'no'")
    assert not observer_policies_allow((_SCOPE, fake), "loom")
    assert not observer_policies_allow((_SCOPE, _OTHERS, _OTHERS), "loom")


@pytest.mark.parametrize("index", [0, 1])
def test_a_restrictive_generated_policy_cannot_prove_visibility(index):
    policies = [_SCOPE, _OTHERS]
    policies[index] = policies[index].replace("AS PERMISSIVE", "AS RESTRICTIVE")
    assert not observer_policies_allow(tuple(policies), "loom")


def test_native_quoted_names_and_to_inside_a_literal_preserve_the_actual_recipient():
    scope = _SCOPE.replace("loom.agent_events", "`loom`.`agent_events`").replace(
        "mirror_marked = 0", "position('it\\'s TO service ([)]', 'TO') > 0"
    )
    others = _OTHERS.replace("loom.agent_events", '"loom"."agent_events"')
    assert observer_policies_allow((scope, others), "loom")


@pytest.mark.parametrize(
    "expression",
    ["1 /* hidden */", "1 -- hidden\n", "1 # hidden\n", "'unclosed", "([)]", "$tag$TO$tag$"],
)
def test_unknown_or_commented_native_policy_syntax_cannot_prove_visibility(expression):
    assert not observer_policies_allow(
        (_SCOPE.replace("mirror_marked = 0", expression), _OTHERS), "loom"
    )


def test_unsupported_quoted_target_escapes_refuse_instead_of_looking_unrelated():
    escaped = _SCOPE.replace("loom.agent_events", r"`lo\om`.agent_events")
    assert not observer_policies_allow((escaped, _OTHERS), "loom")
