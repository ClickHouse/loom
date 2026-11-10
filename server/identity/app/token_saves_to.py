"""Where a list token saves the calls that name no namespace: chosen at mint
and signed into the token (`saves_to`), so a later change of its owner's
dashboard pick never moves it (`caller_home._pick_default`).

A `*` token keeps its own record, the namespace named before `*`, and takes no
`saves_to`. Both mint routes (`/v1/me/service_accounts.create`,
`/v1/admin/token.mint`) ask `saves_to_target` before the token is saved.
"""

from __future__ import annotations

from dataclasses import replace

from fastapi import HTTPException

from server.auth.delegation import Delegated
from server.auth.sharing import is_collection
from server.identity.app.caller_home import is_memory_home, star_home


def _invalid(message: str) -> HTTPException:
    return HTTPException(status_code=422, detail={"code": "VALIDATION_FAILED", "message": message})


def asked_saves_to(raw: object, *, everything: bool) -> str:
    """The `saves_to` a mint body asks for ("" for none); 422 for one that is not
    a string, or one beside `*`, whose record is the name before `*`."""
    if raw is None or raw == "":
        return ""
    if not isinstance(raw, str) or not raw.strip():
        raise _invalid("saves_to must be a namespace name")
    if everything:
        raise _invalid('saves_to needs a list without "*": name the namespace before "*"')
    return raw.strip()


async def saves_to_target(
    org: str, delegated: Delegated, asked: str, *, before_star: str = ""
) -> Delegated:
    """`delegated` with the namespace its unaddressed calls save to. A list token
    saves to `asked`, else to `before_star` (a `*` request's name before `*` that
    a narrower parent turned into this list) when it may write there, else to the
    parent's when the child may write there (`delegate` carried it), else to the
    first memory namespace in its order it may write, else nowhere (it only
    reads). `asked` must be a memory namespace the token may write: else a 422
    naming it; `before_star` is a preference, as it is on a `*` token. The kinds
    are read fail-closed: a 503 before anything is saved, never a target picked
    on a guess. A `*` token records nothing here; the name before its `*` may not
    be a collection (a 422), for the same reason."""
    if "*" in delegated.namespaces:  # `delegate` never carries a target into one
        if is_collection(star_home(list(delegated.namespaces))):
            raise _invalid(
                "a collection is shared reading, never a token's home: name a memory"
                ' namespace before "*", or none'
            )
        return delegated
    from server.identity.app.namespace_kinds import kind_map

    kinds = await kind_map(org, fail_closed=True)
    # A collection is shared reading, never where one token's calls land unasked.
    writable = [
        ns
        for ns in delegated.namespaces
        if is_memory_home(kinds, ns) and not is_collection(ns) and delegated.writes(ns)
    ]
    if asked:
        if asked not in writable:
            raise _invalid(
                f"saves_to {asked!r} must be a memory namespace the token lists and may write"
            )
        return replace(delegated, saves_to=asked)
    if before_star in writable:
        return replace(delegated, saves_to=before_star)
    if delegated.saves_to in writable:
        return delegated
    return replace(delegated, saves_to=writable[0] if writable else "")


__all__ = ["asked_saves_to", "saves_to_target"]
