"""What the deployment serves and what this token may call.

    caps = await agent.capabilities()
    if caps.can_call("conflicts.list") is not False:   # None = cannot tell, let the call decide
        ...

``can_call`` compares the op's ``required_role`` from ``ops.capabilities``
with the role the bearer token carries (decoded, not verified — the server
verifies on every call). It answers ``False`` only when both sides are
known and the token falls short; ``None`` when the op is not served, the
token is not a JWT, or a role name is unknown.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from loom.token import TokenClaims, role_on, role_satisfies


@dataclass(frozen=True)
class Capabilities:
    ops: dict[str, dict[str, Any]]
    build_commit: str
    schema_version: str
    claims: TokenClaims | None
    fetched_at: float = field(default_factory=time.monotonic)

    def has(self, op: str) -> bool:
        """The deployment serves this op."""
        return op in self.ops

    def required_role(self, op: str) -> str | None:
        """The op's minimum role, lowercased, if served."""
        r = self.ops.get(op, {}).get("required_role")
        return str(r).lower() if r else None

    def role(self, namespace: str | None = None) -> str | None:
        """The token's role on ``namespace``; None when unknown."""
        return role_on(self.claims, namespace)

    def can_call(self, op: str, namespace: str | None = None) -> bool | None:
        """True / False when both sides are known; None means "cannot tell — make the call"."""
        if not self.has(op):
            return None
        return role_satisfies(self.role(namespace), self.required_role(op))

    def callable(self, namespace: str | None = None) -> list[str]:
        """Served ops the token is known to be allowed to call."""
        return sorted(op for op in self.ops if self.can_call(op, namespace) is True)

    @classmethod
    def from_response(cls, caps: dict[str, Any], claims: TokenClaims | None) -> Capabilities:
        ops = {
            str(o["name"]): dict(o)
            for o in caps.get("ops") or []
            if isinstance(o, dict) and o.get("name")
        }
        return cls(
            ops=ops,
            build_commit=str(caps.get("build_commit") or ""),
            schema_version=str(caps.get("schema_version") or ""),
            claims=claims,
        )
