"""The `loom.shared.promoted_from` entry that names a promotion's source row. Pure.

Format: `org/namespace/agent/user_id/session_id/memory_key`. A key may hold `/` (a
`definition/` key does), so the parser keeps everything after the fifth `/` as the key,
and an agent, user or session holding `/` is read into it. The read-time definition check
compares whole entries, so it is not affected.
"""

from __future__ import annotations

PromotionKey = tuple[str, str, str, str, str, str]

_FIELDS = ("org", "namespace", "agent", "user_id", "session_id", "memory_key")


def promotion_source(
    org: str, namespace: str, agent: str, user_id: str, session_id: str, memory_key: str
) -> str:
    """The entry naming a source row."""
    return f"{org}/{namespace}/{agent}/{user_id}/{session_id}/{memory_key}"


def parse_promotion_source(entry: str) -> dict[str, str] | None:
    """An entry's six fields, or None when it is not one (so one malformed row does not
    fail a read)."""
    parts = entry.split("/", len(_FIELDS) - 1)
    if len(parts) != len(_FIELDS) or not parts[-1]:
        return None
    return dict(zip(_FIELDS, parts, strict=True))


# not a setting: fail-closed work ceiling for ambiguous legacy source identities.
_CONSENT_WORK_LIMIT = 250_000


def promotion_users(entries: list[str], org: str, namespace: str) -> set[str] | None:
    """Every possible source user, or no proof when complete denial checking is too large.

    The known organization and namespace fix the prefix. Slashes in any remaining
    field leave several possible users; consent checks must cover all of them.
    """
    prefix = f"{org}/{namespace}/"
    users: set[str] = set()
    work = 0
    for entry in entries:
        if not isinstance(entry, str) or not entry.startswith(prefix):
            return None
        work += len(entry)
        if work > _CONSENT_WORK_LIMIT:
            return None
        parts = entry[len(prefix) :].split("/")
        if len(parts) < 4:
            return None
        found = False
        for start in range(1, len(parts) - 2):
            for stop in range(start + 1, len(parts) - 1):
                user = "/".join(parts[start:stop])
                key = "/".join(parts[stop + 1 :])
                work += len(user) + len(key) + stop - start
                if work > _CONSENT_WORK_LIMIT:
                    return None
                if key:
                    users.add(user)
                    found = True
        if not found:
            return None
    return users or None


def promotion_keys(entries: list[str], org: str, namespace: str) -> list[PromotionKey] | None:
    """All exact identities a legacy entry may name, with complete bounded verification."""
    from itertools import combinations

    prefix = f"{org}/{namespace}/"
    keys: set[PromotionKey] = set()
    work = 0
    for entry in entries:
        if not isinstance(entry, str) or not entry.startswith(prefix):
            return None
        work += len(entry)
        if work > _CONSENT_WORK_LIMIT:
            return None
        parts = entry[len(prefix) :].split("/")
        if len(parts) < 4:
            return None
        found = False
        for first, second, third in combinations(range(1, len(parts)), 3):
            fields = tuple(
                "/".join(parts[a:b])
                for a, b in ((0, first), (first, second), (second, third), (third, len(parts)))
            )
            work += sum(map(len, fields)) + len(parts)
            if work > _CONSENT_WORK_LIMIT:
                return None
            agent, user, session, key = fields
            if key:
                keys.add((org, namespace, agent, user, session, key))
                found = True
        if not found:
            return None
    return sorted(keys) or None
