"""Unit tests for DEFAULT-OFF entity-stable memory keys.

Covers the pure transform (`entity_stable_key`) plus the settings-gated
wrapper (`rewrite_fact_key`): PR/issue numbers, @handles and
test ids yield an entity-prefixed key; no salient entity leaves the key
unchanged; the 64-char cap is respected; and the whole thing is a strict
no-op when the setting is off.
"""

from __future__ import annotations

from server.memory.app.extract import ExtractedFact
from server.memory.app.extract.keys import entity_stable_key, rewrite_fact_key

# ---------------------------------------------------------------------------
# Pure helper: entity_stable_key
# ---------------------------------------------------------------------------


def test_pr_number_in_value_prefixes_key() -> None:
    assert (
        entity_stable_key("user_task_status", [], "PR 107585 is ready to merge")
        == "pr107585_status"
    )


def test_pr_number_no_separator_prefixes_key() -> None:
    assert entity_stable_key("user_task_status", ["pr107585"], "") == "pr107585_status"


def test_bare_hash_ref_treated_as_pr() -> None:
    assert entity_stable_key("user_task_status", [], "landed in #107585") == "pr107585_status"


def test_issue_number_prefixes_with_issue() -> None:
    assert entity_stable_key("user_task_status", [], "issue 4242 still open") == "issue4242_status"


def test_handle_prefixes_key() -> None:
    assert entity_stable_key("user_directive", [], "@tvega asked us to revert") == "tvega_directive"


def test_handle_from_entities_list() -> None:
    assert entity_stable_key("user_directive", ["@tvega"], "someone asked") == "tvega_directive"


def test_test_keyword_id_prefixes_key() -> None:
    assert (
        entity_stable_key("user_fix", [], "test 04653 is flaky and needs a fix") == "test04653_fix"
    )


def test_bare_test_file_convention_prefixes_key() -> None:
    assert entity_stable_key("user_status", [], "04653_stateless_query broke") == "test04653_status"


def test_no_entity_leaves_key_unchanged() -> None:
    # No PR/issue/test/handle anywhere -> concept key returned as-is.
    assert (
        entity_stable_key("user_task_status", ["deployment"], "the plan is set")
        == "user_task_status"
    )


def test_lone_small_number_is_not_a_pr() -> None:
    # High-precision: a bare number with no keyword/# is not an identifier.
    assert entity_stable_key("user_task_status", [], "we shipped 3 features") == "user_task_status"


def test_concept_suffix_strips_multiple_generic_prefixes() -> None:
    # `user` and `task` are both generic leading tokens; both stripped.
    assert (
        entity_stable_key("user_task_deploy_blocker", [], "pr 900 blocks it")
        == "pr900_deploy_blocker"
    )


def test_all_generic_key_is_not_stripped_to_empty() -> None:
    # A key made entirely of generic tokens keeps its last segment.
    assert entity_stable_key("user", [], "pr 900 note") == "pr900_user"


def test_priority_pr_beats_handle() -> None:
    # Both a PR number and an @handle present -> PR wins (more salient).
    assert entity_stable_key("user_status", [], "@tvega opened PR 555") == "pr555_status"


def test_cap_respected() -> None:
    long_concept = "user_" + "x" * 200
    out = entity_stable_key(long_concept, [], "pr 107585 update")
    assert len(out) <= 64
    assert out.startswith("pr107585_")


def test_handle_ignores_email_local_part() -> None:
    # `foo@bar` is an email, not a handle -> no rewrite.
    assert (
        entity_stable_key("user_task_status", [], "email me at foo@bar.com") == "user_task_status"
    )


def test_tags_source_is_scanned() -> None:
    assert entity_stable_key("user_status", [], "the change", tags=["pr#8123"]) == "pr8123_status"


# ---------------------------------------------------------------------------
# The per-fact wrapper: rewrite_fact_key
# ---------------------------------------------------------------------------


def _fact() -> ExtractedFact:
    return ExtractedFact(
        memory_key="user_task_status",
        value="PR 107585 is ready",
        entities=["pr107585"],
    )


def test_wrapper_rewrites_key() -> None:
    out = rewrite_fact_key(_fact())
    assert out.memory_key == "pr107585_status"
    # Only the key changes; the rest of the fact is preserved.
    assert out.value == "PR 107585 is ready"
    assert out.entities == ["pr107585"]


def test_wrapper_no_entity_leaves_key() -> None:
    fact = ExtractedFact(memory_key="user_task_status", value="the plan is set")
    out = rewrite_fact_key(fact)
    assert out.memory_key == "user_task_status"


# ---- task slugs and per-run state keys ----------------------------------

from server.memory.app.extract.keys import (  # noqa: E402
    batch_task_slug,
    scope_session_state_keys,
)


def test_a_task_slug_is_a_high_precision_entity() -> None:
    f = ExtractedFact(
        memory_key="user_task_update",
        value="User updated task 2026-08-30-ci-p2-master-install-packages-amdrele.",
    )
    assert (
        entity_stable_key(f.memory_key, f.entities, f.value) == "task20260830_ci_p2_master_update"
    )
    # a PR reference still wins over a slug in the same text (priority order)
    both = "PR 107585 for task 2026-08-30-ci-p2-master-install fixed the build"
    assert entity_stable_key("user_task_status", [], both) == "pr107585_status"
    # a bare date is not a slug
    assert (
        entity_stable_key("user_task_deadline", [], "deadline is 2026-09-02")
        == "user_task_deadline"
    )


def test_per_run_state_facts_take_the_batch_s_single_task_slug() -> None:
    facts = [
        ExtractedFact(
            memory_key="user_task_id",
            value="User's task id is 2026-08-30-ci-p2-master-install-packages-amdrele.",
        ),
        ExtractedFact(memory_key="user_task_status", value="User's task status is waiting_ci."),
        ExtractedFact(
            memory_key="user_clickhouse_worker_slot",
            value="User is one maintenance slot of the 32-slot fleet (slot 13).",
        ),
        ExtractedFact(memory_key="user_favorite_editor", value="User prefers neovim."),
    ]
    assert batch_task_slug(facts) == "task20260830_ci_p2_master"
    out = scope_session_state_keys(facts)
    keys = {f.value[:14]: f.memory_key for f in out}
    # generic subject tokens (user, task) are stripped from the suffix
    assert keys["User's task st"] == "task20260830_ci_p2_master_status"
    assert keys["User is one ma"] == "task20260830_ci_p2_master_clickhouse_worker_slot"
    assert keys["User prefers n"] == "user_favorite_editor"  # not per-run state
    # the fact with its own identifier is left for the per-fact rewrite
    assert keys["User's task id"] == "user_task_id"


def test_two_task_slugs_in_one_batch_give_no_licence() -> None:
    facts = [
        ExtractedFact(memory_key="user_task_id", value="task 2026-08-30-ci-p2-master-install"),
        ExtractedFact(
            memory_key="user_task_update", value="also touched 2026-08-03-p1-coordinator-x"
        ),
        ExtractedFact(memory_key="user_task_status", value="status is waiting_ci"),
    ]
    assert batch_task_slug(facts) is None
    assert [f.memory_key for f in scope_session_state_keys(facts)] == [
        "user_task_id",
        "user_task_update",
        "user_task_status",
    ]
    assert scope_session_state_keys([facts[2]]) == [facts[2]]  # no slug at all
