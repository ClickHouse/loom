"""One vocabulary for the per-namespace grant roles and their order."""

from __future__ import annotations

import ast
import re
from pathlib import Path

from server.auth.authz import GRANT_ROLES, Role, grant_rank, grant_role

ROOT = Path(__file__).resolve().parents[2]


def test_grant_roles_rank_in_role_order() -> None:
    assert GRANT_ROLES == ("viewer", "writer", "admin")
    ranks = [grant_rank(r) for r in GRANT_ROLES]
    assert ranks == sorted(ranks) and ranks[0] > 0
    assert [grant_role(r) for r in GRANT_ROLES] == [Role.VIEWER, Role.WRITER, Role.ADMIN]


def test_anything_else_ranks_below_viewer_and_confers_nothing() -> None:
    for other in ("", None, "org_admin", "platform_admin", "owner", "jwt"):
        assert grant_rank(other) == 0
        assert grant_role(other) is None
    assert grant_role(" Admin ") is Role.ADMIN


def test_no_module_keeps_its_own_copy_of_the_order() -> None:
    copy = re.compile(r"""["']viewer["']\s*:\s*1\s*,\s*["']writer["']\s*:\s*2""")
    hits = [
        str(p.relative_to(ROOT))
        for p in (ROOT / "server").rglob("*.py")
        if copy.search(p.read_text(encoding="utf-8"))
    ]
    assert hits == []


def _grant_statements(path: Path) -> list[str]:
    """Every string literal in `path` that reads `loom.namespace_grants`."""
    out: list[str] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if "namespace_grants" in node.value:
                out.append(node.value)
    return out


def test_every_grant_read_takes_the_one_live_predicate_and_rank() -> None:
    """A grant read under server/auth, server/identity or server/transport
    takes `grant_sql.LIVE_GRANT` and `grant_sql.GRANT_RANK_SQL`, never its own
    spelling, so no copy drops the status-less rows the gate admits or ranks
    a role no grant confers first."""
    from server.core.grant_sql import GRANT_RANK_SQL, LIVE_GRANT

    live = re.compile(r"\bstatus\s*=\s*'(active)?'")
    rank = re.compile(r"multiIf\(\s*(\w+\.)?role\s*=")
    hits = [
        str(p.relative_to(ROOT))
        for area in ("auth", "identity", "transport")
        for p in (ROOT / "server" / area).rglob("*.py")
        if any(live.search(s) or rank.search(s) for s in _grant_statements(p))
    ]
    assert hits == []
    assert LIVE_GRANT == "revoked_at IS NULL AND status = 'active'"
    assert "org_admin" not in GRANT_RANK_SQL
