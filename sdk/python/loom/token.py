"""What the bearer token says about its holder.

Loom tokens are JWTs whose payload carries ``org``, ``namespaces``,
``roles`` and, for scoped tokens, ``ns_roles`` (namespace → role). The SDK
decodes the payload WITHOUT verifying the signature — the server does that
on every call — and uses it only to avoid requests the token cannot make,
never to grant anything.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from typing import Any

#: Ascending privilege, mirroring the server's ``Role`` enum.
ROLE_RANK: dict[str, int] = {
    "viewer": 10,
    "writer": 20,
    "admin": 30,
    "org_admin": 40,
    "platform_admin": 50,
}


@dataclass(frozen=True)
class TokenClaims:
    subject: str | None = None
    org: str | None = None
    namespaces: tuple[str, ...] = ()
    #: Flat roles (legacy / wildcard tokens), lowercased.
    roles: tuple[str, ...] = ()
    #: Per-namespace role for scoped tokens, authoritative where present.
    ns_roles: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    exp: int | None = None


def decode_token_claims(token: str | None) -> TokenClaims | None:
    """The token's payload, or None when it is not a decodable JWT."""
    if not token:
        return None
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        raw = parts[1] + "=" * (-len(parts[1]) % 4)
        payload: Any = json.loads(base64.urlsafe_b64decode(raw))
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None

    def strs(v: Any) -> tuple[str, ...]:
        return tuple(x for x in v if isinstance(x, str)) if isinstance(v, list) else ()

    ns_roles: list[tuple[str, str]] = []
    nr = payload.get("ns_roles")
    if isinstance(nr, list):
        for e in nr:
            if (
                isinstance(e, (list, tuple))
                and len(e) >= 2
                and isinstance(e[0], str)
                and isinstance(e[1], str)
            ):
                ns_roles.append((e[0], e[1].lower()))
    elif isinstance(nr, dict):
        ns_roles.extend((str(k), v.lower()) for k, v in nr.items() if isinstance(v, str))
    exp = payload.get("exp")
    return TokenClaims(
        subject=payload.get("sub") if isinstance(payload.get("sub"), str) else None,
        org=payload.get("org") if isinstance(payload.get("org"), str) else None,
        namespaces=strs(payload.get("namespaces")),
        roles=tuple(r.lower() for r in strs(payload.get("roles"))),
        ns_roles=tuple(ns_roles),
        exp=exp if isinstance(exp, int) else None,
    )


def _best(roles: Any) -> str | None:
    top: str | None = None
    for r in roles:
        rank = ROLE_RANK.get(r)
        if rank is not None and (top is None or rank > ROLE_RANK[top]):
            top = r
    return top


def role_on(claims: TokenClaims | None, namespace: str | None = None) -> str | None:
    """The role a token holds on ``namespace``: the scoped entry when the token
    has a map, else its highest flat role; None when the token says nothing."""
    if claims is None:
        return None
    if claims.ns_roles:
        if namespace:
            for ns, r in claims.ns_roles:
                if ns == namespace:
                    return r
        # A deployment-scoped op is gated on the best role the map grants anywhere.
        return _best(r for _ns, r in claims.ns_roles)
    return _best(claims.roles)


def role_satisfies(role: str | None, required: str | None) -> bool | None:
    """Whether ``role`` satisfies ``required``; None when either name is unknown."""
    if not role or not required:
        return None
    have = ROLE_RANK.get(role.lower())
    need = ROLE_RANK.get(required.lower())
    if have is None or need is None:
        return None
    return have >= need
