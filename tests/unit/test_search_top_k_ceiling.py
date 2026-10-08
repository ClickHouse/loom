"""A token's role on the namespace caps top_k."""

from __future__ import annotations

import pytest

from server.auth.context import current_auth_var
from server.auth.jwt import AuthInfo
from server.memory.api.models_search import MemorySearchInput
from server.memory.app.search.request import _apply_top_k_ceiling
from server.operations.base import Role


def _input(top_k: int = 30, ns: str = "clickgap-public") -> MemorySearchInput:
    return MemorySearchInput(org="clickhouse", namespace=ns, agent="*", query="q", top_k=top_k)


def _scoped(role: str) -> AuthInfo:
    return AuthInfo(
        subject="sa",
        org="clickhouse",
        namespaces=("clickgap-public",),
        roles=(),
        is_service_account=True,
        ns_roles=(("clickgap-public", role),),
    )


def _flat(*roles: Role) -> AuthInfo:
    return AuthInfo(
        subject="u",
        org="clickhouse",
        namespaces=("clickgap-public",),
        roles=tuple(roles),
        is_service_account=False,
    )


@pytest.fixture
def ceilings(monkeypatch: pytest.MonkeyPatch):
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_search_top_k_ceiling_viewer", 10)
    monkeypatch.setattr(settings, "memory_search_top_k_ceiling_writer", 30)
    yield settings
    current_auth_var.set(None)


def test_viewer_scoped_token_is_capped_and_the_ceiling_is_reported(ceilings) -> None:
    current_auth_var.set(_scoped("viewer"))
    out, ceiling = _apply_top_k_ceiling(_input(30))
    assert out.top_k == 10 and ceiling == 10
    out, ceiling = _apply_top_k_ceiling(_input(5))
    assert out.top_k == 5 and ceiling == 0  # under the ceiling: untouched, not reported


def test_writer_admin_and_no_token(ceilings) -> None:
    current_auth_var.set(_scoped("writer"))
    out, ceiling = _apply_top_k_ceiling(_input(60))
    assert out.top_k == 30 and ceiling == 30
    current_auth_var.set(_scoped("admin"))
    out, ceiling = _apply_top_k_ceiling(_input(60))
    assert out.top_k == 60 and ceiling == 0
    current_auth_var.set(None)
    out, ceiling = _apply_top_k_ceiling(_input(60))
    assert out.top_k == 60 and ceiling == 0


def test_legacy_token_uses_its_highest_role(ceilings) -> None:
    current_auth_var.set(_flat(Role.VIEWER))
    assert _apply_top_k_ceiling(_input(30))[1] == 10
    current_auth_var.set(_flat(Role.VIEWER, Role.WRITER))
    assert _apply_top_k_ceiling(_input(60))[1] == 30
    current_auth_var.set(_flat(Role.ORG_ADMIN))
    assert _apply_top_k_ceiling(_input(60))[1] == 0


def test_scoped_token_on_another_namespace_falls_back_to_flat_roles(ceilings) -> None:
    current_auth_var.set(_scoped("viewer"))
    out, ceiling = _apply_top_k_ceiling(_input(30, ns="other"))
    assert ceiling == 0 and out.top_k == 30  # no per-namespace role and roles=() → uncapped


def test_default_off(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.settings import Settings

    assert Settings.model_fields["memory_search_top_k_ceiling_viewer"].default == 0
    assert Settings.model_fields["memory_search_top_k_ceiling_writer"].default == 0
    current_auth_var.set(_scoped("viewer"))
    try:
        assert _apply_top_k_ceiling(_input(60))[1] == 0
    finally:
        current_auth_var.set(None)


def test_output_field_and_ledger_key_exist() -> None:
    from server.memory.api.models_search import MemorySearchOutput
    from server.observability.event_args import IDENTIFIER_KEYS, project_result

    assert MemorySearchOutput.model_fields["top_k_ceiling"].default == 0
    assert "top_k_ceiling" in IDENTIFIER_KEYS
    assert project_result({"top_k_ceiling": 10, "results": []}).get("out_top_k_ceiling") == "10"


async def test_a_read_your_writes_reread_reports_the_ceiling_the_first_pass_applied(
    ceilings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reread runs on the already-capped input, where the cap lowers nothing; the
    adopted output still carries the ceiling the caller was held to."""
    import server.memory.app.search as pkg
    from server.memory import store as memory_repo
    from server.memory.api.models_search import MemorySearchOutput
    from server.memory.app.search import consistency

    search = pkg.handle_memory_search
    reread_top_k: list[int] = []

    async def reread(inp: MemorySearchInput) -> MemorySearchOutput:
        # what the re-entered handler reports: its own ceiling check on the capped input
        reread_top_k.append(inp.top_k)
        return MemorySearchOutput(
            results=[], embedding_model="fake", top_k_ceiling=_apply_top_k_ceiling(inp)[1]
        )

    monkeypatch.setattr(pkg, "handle_memory_search", reread)
    monkeypatch.setattr(consistency, "written_recently", lambda *a: True)
    monkeypatch.setattr(memory_repo, "read_your_writes_enabled", lambda: True)
    current_auth_var.set(_scoped("viewer"))
    out = await search(_input(30))
    assert reread_top_k == [10]
    assert out.top_k_ceiling == 10
