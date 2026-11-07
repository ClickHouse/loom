"""The namespace an op addressed by an org-wide memory id acts in."""

from __future__ import annotations

from server.operations.base import Role


async def namespace_for_id(
    org: str, memory_id: str, role: Role = Role.VIEWER, *, strong: bool
) -> str | None:
    """The one namespace holding a live head of `memory_id` (restored namespaces keep the
    ids) that the caller reaches with `role`; None when it reaches none or several, or a
    holder's grant could not be read. `strong`: the holders are read current."""
    import server.memory.store as memory_repo
    from server.auth.authz import row_reach
    from server.auth.fail_closed import note_answered_unverified

    reachable: list[str] = []
    for ns in await memory_repo.namespaces_of_id(org, memory_id, strong=strong):
        verdict = await row_reach(org, ns, role)
        if verdict is None:
            note_answered_unverified()
            return None
        if verdict:
            reachable.append(ns)
    return reachable[0] if len(reachable) == 1 else None
