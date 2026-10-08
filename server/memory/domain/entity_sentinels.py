"""Entity sentinels the entity_extractor worker writes into `entities`.

`_none_` means "the model returned a well-formed empty array: nothing to
extract", `_parse_failed_` means "the model never answered in the requested
shape". Both must stay ON THE ROW so the worker's `empty(entities)` rescan
leaves it alone, and so `memory.set` can preserve them on update. Neither is
an entity: readers (memory.get / list / bulk_get, exports, bundles) must not
hand them to callers as if the row were about a thing called "_none_" (an
export and re-import would otherwise carry the sentinel into the copies).
"""

from __future__ import annotations

from collections.abc import Iterable

NONE_SENTINEL = "_none_"
PARSE_FAILED_SENTINEL = "_parse_failed_"
SENTINEL_ENTITIES: frozenset[str] = frozenset({NONE_SENTINEL, PARSE_FAILED_SENTINEL})


def public_entities(entities: Iterable[str] | None) -> list[str]:
    """`entities` as a caller should see them: sentinels dropped, order kept."""
    return [str(e) for e in (entities or []) if str(e) not in SENTINEL_ENTITIES]
