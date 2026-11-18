"""A review revises only an approved source or the exact draft it already wrote."""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from server.core.errors import DefinitionNameTakenError, DefinitionPromotionRefusedError

VALUE = {"term": "ARR", "aliases": [], "body": "Yearly revenue."}
OLD = {**VALUE, "body": "Previous definition."}
NOW = datetime(2026, 1, 1)


def _head(**changes: Any) -> Any:
    return SimpleNamespace(
        **dict(
            {
                "value": OLD,
                "memory_type": "definition",
                "pinned": False,
                "lock_mode": "",
                "expires_at": None,
                "valid_to": None,
                "invalid_at": None,
                "tombstoned_at": None,
                "status": "active",
            },
            **changes,
        )
    )


@pytest.mark.parametrize(
    "changes",
    [
        {},
        {"value": VALUE},
        {"expires_at": NOW - timedelta(seconds=1)},
        {"valid_to": NOW - timedelta(seconds=1)},
        {"value": VALUE, "expires_at": NOW + timedelta(days=1)},
    ],
)
def test_a_review_revises_the_approved_head_or_clears_its_expiry(changes: dict[str, Any]) -> None:
    from server.memory.domain.definition_review import write_needed

    head = _head(**changes)
    expected = head.value != VALUE or head.expires_at is not None or head.valid_to is not None
    assert write_needed(head, OLD, VALUE, now=NOW) is expected


@pytest.mark.parametrize(
    "changes",
    [
        {"value": {**VALUE, "body": "Writer edit"}},
        {"pinned": True},
        {"lock_mode": "no_delete"},
        {"status": "conflicted"},
        {"status": "duplicate"},
        {"invalid_at": NOW},
        {"memory_type": "semantic"},
    ],
)
def test_a_review_refuses_writer_edits_and_protected_heads(changes: dict[str, Any]) -> None:
    from server.memory.domain.definition_review import write_needed

    with pytest.raises(DefinitionPromotionRefusedError):
        write_needed(_head(**changes), OLD, VALUE, now=NOW)


def test_an_exact_draft_promotes_without_changing_a_pin_or_lock() -> None:
    from server.memory.domain.definition_review import write_needed

    assert not write_needed(
        _head(value=VALUE, pinned=True, lock_mode="read_only"), OLD, VALUE, now=NOW
    )
    assert write_needed(None, None, VALUE, now=NOW)
    with pytest.raises(DefinitionPromotionRefusedError):
        write_needed(_head(), None, VALUE, now=NOW)


def test_a_review_uses_the_shared_key_that_holds_its_term() -> None:
    from server.memory.domain.definition_review import target_key

    rows = [{"memory_key": "definition/old", "term": "Revenue", "aliases": ["arr"]}]
    assert target_key(VALUE, "definition/new", rows) == "definition/old"
    assert target_key({**VALUE, "term": "MRR"}, "definition/new", rows) == "definition/new"
    with pytest.raises(DefinitionNameTakenError):
        target_key(VALUE, "definition/new", rows + [{**rows[0], "memory_key": "definition/other"}])
