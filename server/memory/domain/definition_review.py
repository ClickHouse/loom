"""Which shared definition a draft revises, and whether its source may be changed."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from server.core.datetimes import to_naive_utc
from server.core.errors import (
    DefinitionNameTakenError,
    DefinitionPromotionRefusedError,
    ValidationError,
)
from server.core.hashing import served_content_sha256
from server.memory.domain.definition_check import liveness_lapse
from server.memory.domain.definitions import (
    DEFINITION_KEY_PREFIX,
    definition_names,
    fold_name,
    names_of,
)
from server.memory.domain.lock import effective_mode
from server.memory.domain.memory_types import is_definition


def target_key(value: Any, proposed_key: str, shared: list[dict[str, Any]]) -> str:
    """Revise the one shared key holding the term, or use the draft's own key."""
    if definition_names(value) is None:
        raise ValidationError("a definition draft needs a term and a list of string aliases")
    term = fold_name(value["term"])
    matches = sorted(
        {str(r["memory_key"]) for r in shared if term in names_of(r["term"], r["aliases"])}
    )
    if len(matches) > 1:
        raise DefinitionNameTakenError(
            "more than one shared definition holds the draft's term; demote all but one",
            conflicting_keys=matches,
            names=[term],
        )
    if matches:
        return matches[0]
    if not proposed_key.startswith(DEFINITION_KEY_PREFIX) or proposed_key == DEFINITION_KEY_PREFIX:
        raise ValidationError("a definition draft needs its own definition/ key")
    return proposed_key


def write_needed(head: Any, shared_value: Any, value: Any, *, now: datetime) -> bool:
    """An exact live draft needs no write; other writes preserve a writer's edits."""
    if head is None or head.tombstoned_at is not None:
        return True
    lapse = liveness_lapse(
        status=head.status,
        invalid_at=to_naive_utc(head.invalid_at),
        expires_at=to_naive_utc(head.expires_at),
        valid_to=to_naive_utc(head.valid_to),
        now=now,
    )
    same = served_content_sha256(head.value) == served_content_sha256(value)
    if (
        is_definition(head)
        and lapse is None
        and same
        and head.expires_at is None
        and head.valid_to is None
    ):
        return False
    if head.pinned or effective_mode(head, now=now):
        raise DefinitionPromotionRefusedError(
            "unpin or unlock the source before changing its definition"
        )
    if not is_definition(head) or lapse not in (None, "expired", "ended"):
        raise DefinitionPromotionRefusedError("the definition's source is not an active definition")
    if (
        same
        or lapse in ("expired", "ended")
        or (
            shared_value is not None
            and served_content_sha256(head.value) == served_content_sha256(shared_value)
        )
    ):
        return True
    raise DefinitionPromotionRefusedError(
        "the source holds a writer's edit; write the approved draft explicitly or reject it"
    )
