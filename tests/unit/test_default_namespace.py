"""LOOM_DEFAULT_NAMESPACE — the auth-disabled default namespace.

The setting feeds three consumers: the dashboard's boot snippet
(window.__LOOM_DEFAULT_NAMESPACE, which the SPA's auth-off identity
synthesis reads as the one namespace that mode serves), the auth-off
`loom.namespaces` bootstrap (provisioned like a real account: row + admin
grant for the single-user subject), and the MCP/SDK caller-default
resolver (`_caller_defaults`), which must land agent writes in the same
namespace the dashboard shows. These tests pin the setting's default,
the env-driven override, the auth-gated validation, and the bootstrap's
cache/reactivation discipline.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "sdk" / "python"))
sys.path.insert(0, str(ROOT))


def test_default_is_local() -> None:
    from server.settings import Settings

    assert Settings(_env_file=None).default_namespace == "local"


def test_env_override(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from server.settings import Settings

    monkeypatch.setenv("LOOM_DEFAULT_NAMESPACE", "team-alpha")
    assert Settings(_env_file=None).default_namespace == "team-alpha"


def test_blank_normalises_to_local(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """An explicitly-empty env var behaves like an unset one, not like a
    namespace named ''."""
    from server.settings import Settings

    monkeypatch.setenv("LOOM_DEFAULT_NAMESPACE", "   ")
    assert Settings(_env_file=None).default_namespace == "local"


@pytest.mark.parametrize(
    "value",
    [
        "personal-avery",  # collides with per-subject auto-provisioning
        "acl-0123456789abcdef01234567",  # a mirror's name, refused on every request
        "Team Alpha/prod",  # spaces + separator, inserted verbatim otherwise
        "TeamAlpha",  # uppercase: no other creation path emits it
        "-leading",
        "a" * 129,  # over the shared NAMESPACE_MAX_LEN ceiling
    ],
)
def test_invalid_names_rejected_when_auth_off(  # type: ignore[no-untyped-def]
    monkeypatch, value: str
) -> None:
    """Fail fast at construction — but only in the mode that reads the
    setting (auth off), before data is written under an unusable name."""
    from pydantic import ValidationError

    from server.settings import Settings

    monkeypatch.setenv("LOOM_AUTH_REQUIRED", "false")
    monkeypatch.setenv("LOOM_DEFAULT_NAMESPACE", value)
    with pytest.raises(ValidationError, match="LOOM_DEFAULT_NAMESPACE"):
        Settings(_env_file=None)


def test_invalid_name_warns_but_boots_when_auth_on(  # type: ignore[no-untyped-def]
    monkeypatch, caplog
) -> None:
    """Auth-ON deployments never read the setting: a stale value left in a
    shared .env must not crash-loop the server/CLI/migrations."""
    import logging

    from server.settings import Settings

    monkeypatch.setenv("LOOM_AUTH_REQUIRED", "true")
    monkeypatch.setenv("LOOM_DEFAULT_NAMESPACE", "Team_Alpha")
    with caplog.at_level(logging.WARNING, logger="server.settings"):
        s = Settings(_env_file=None)
    assert s.default_namespace == "Team_Alpha"  # kept verbatim, mode ignores it
    assert any("LOOM_DEFAULT_NAMESPACE" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize(
    "value",
    [
        "local",
        "team-alpha",
        "acme",
        "ns2",
        "a",
        # Shapes the product's own creation paths accept — the setting must
        # not be stricter than namespace.register / the create dialog
        # (shared rules: server/core/naming.py):
        "trailing-",
        "a" * 128,
    ],
)
def test_valid_names_accepted(monkeypatch, value: str) -> None:  # type: ignore[no-untyped-def]
    from server.settings import Settings

    monkeypatch.setenv("LOOM_AUTH_REQUIRED", "false")
    monkeypatch.setenv("LOOM_DEFAULT_NAMESPACE", value)
    assert Settings(_env_file=None).default_namespace == value


# --- boot-snippet escaping -------------------------------------------------
# Every value in the dashboard's boot snippet is operator-supplied config. A broken
# literal isn't cosmetic: the <script> block carries __LOOM_AUTH_DISABLED, so a
# SyntaxError in any one assignment drops an auth-off deployment onto a login
# screen it has no credentials for.


@pytest.mark.parametrize(
    "value",
    [
        "local",
        "team-alpha",
        r"ends-with-backslash\\",
        "has'quote",
        'has"doublequote',
        "line\nbreak",
        "https://sso.example.com/login?next=/settings&x=1",
    ],
)
def test_js_string_round_trips(value: str) -> None:
    """The emitted literal is valid JSON, so it parses back to the input —
    which is the same guarantee a JS parser gives for these forms."""
    import json

    from server.identity.app.dashboard_routes import _js_string

    literal = _js_string(value)
    assert literal.startswith('"') and literal.endswith('"')
    assert json.loads(literal.replace("<\\/", "</")) == value


def test_js_string_neutralises_script_close() -> None:
    """`</script>` inside a value ends the element at the HTML level, which no
    amount of JS-level escaping prevents — so the sequence must not survive."""
    from server.identity.app.dashboard_routes import _js_string

    literal = _js_string("</script><img src=x onerror=alert(1)>")
    assert "</script>" not in literal
    assert "</" not in literal


def test_js_string_neutralises_comment_open() -> None:
    """`<!--` flips the HTML tokenizer into script-data-escaped state, where
    the snippet's real `</script>` stops closing the element and the parser
    swallows the rest of index.html scanning for `-->`."""
    from server.identity.app.dashboard_routes import _js_string

    literal = _js_string("<!--<script>steal()</script>-->")
    assert "<!--" not in literal
    assert "</" not in literal


def test_js_string_survives_trailing_backslash() -> None:
    """A trailing backslash must not eat the closing quote and SyntaxError
    the whole snippet, as a quote-only escape would."""
    from server.identity.app.dashboard_routes import _js_string

    literal = _js_string("team\\")
    # The value's backslash is escaped, so the closing quote still closes.
    assert literal == '"team\\\\"'


# --- namespace-row bootstrap ----------------------------------------------


class _FakeCH:
    """Minimal stand-in for the ClickHouse client.

    Serves both the bootstrap module's probe/warning queries and
    `provision_account`'s two inserts (namespaces + grants) — the bootstrap
    reuses the real provisioning helper so the row shape can't drift.
    """

    def __init__(
        self,
        *,
        exists: bool = False,
        active: bool = True,
        others: tuple[str, ...] = (),
        fail: bool = False,
    ) -> None:
        self.exists = exists
        self.active = active
        self.others = others
        self.fail = fail
        self.probe_calls = 0
        self.warn_calls = 0
        self.inserted: list[tuple[str, list[dict]]] = []
        self.watermarks: list[dict] = []  # loom.acl_versions, the grant writes' announcements

    async def query_one(self, sql, params=None):  # type: ignore[no-untyped-def]
        if self.fail:
            raise RuntimeError("clickhouse unreachable")
        if "count()" in sql:
            self.warn_calls += 1
            return {"n": len(self.others), "sample": list(self.others)}
        self.probe_calls += 1
        return {"active": self.active} if self.exists else None

    async def insert(self, table, rows, settings=None):  # type: ignore[no-untyped-def]
        if table == "loom.acl_versions":
            self.watermarks += rows
            return
        self.inserted.append((table, rows))


@pytest.fixture()
def bootstrap(monkeypatch):  # type: ignore[no-untyped-def]
    """The module caches process-wide by design — reset it per test."""
    from server.auth import default_namespace as mod

    monkeypatch.setattr(mod, "_bootstrapped", False)
    return mod


def _wire(monkeypatch, ch) -> None:  # type: ignore[no-untyped-def]
    """Point both the bootstrap module and provision_account at the fake."""
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)
    monkeypatch.setattr("server.auth.self_signup.get_client", lambda: ch)


async def test_bootstrap_provisions_once_then_caches(  # type: ignore[no-untyped-def]
    bootstrap, monkeypatch
) -> None:
    """The row can only need creating once per process; the dashboard must not pay for a
    blocking FINAL lookup on every page load. Provisioning goes through
    provision_account: namespaces row + an admin grant for the single-user
    subject, so list_mine works and the namespace stays administrable after
    auth is enabled."""
    from server.settings import settings

    ch = _FakeCH()
    _wire(monkeypatch, ch)
    monkeypatch.setattr(settings, "default_namespace", "team-alpha")

    await bootstrap.ensure_default_namespace_cached()
    await bootstrap.ensure_default_namespace_cached()
    await bootstrap.ensure_default_namespace_cached()

    assert ch.probe_calls == 1
    tables = [t for t, _ in ch.inserted]
    assert tables == ["loom.namespaces", "loom.namespace_grants"]
    ns_row = ch.inserted[0][1][0]
    assert ns_row["org"] == settings.self_signup_org
    assert ns_row["namespace"] == "team-alpha"
    assert ns_row["owner_email"] == "you"
    grant = ch.inserted[1][1][0]
    assert (grant["subject"], grant["role"]) == ("you", "admin")


async def test_bootstrap_noop_when_active_row_exists(  # type: ignore[no-untyped-def]
    bootstrap, monkeypatch
) -> None:
    ch = _FakeCH(exists=True, active=True)
    _wire(monkeypatch, ch)

    await bootstrap.ensure_default_namespace_cached()
    await bootstrap.ensure_default_namespace_cached()

    assert ch.inserted == []
    assert ch.probe_calls == 1


async def test_bootstrap_reactivates_soft_deleted_row(  # type: ignore[no-untyped-def]
    bootstrap, monkeypatch
) -> None:
    """`namespace.delete` writes an active=false RMT row. A soft-deleted
    default must be revived, not silently served in a state every list path
    and worker filters out."""
    ch = _FakeCH(exists=True, active=False)
    _wire(monkeypatch, ch)

    await bootstrap.ensure_default_namespace_cached()

    assert [t for t, _ in ch.inserted] == [
        "loom.namespaces",
        "loom.namespace_grants",
    ]
    assert ch.inserted[0][1][0]["active"] is True


async def test_bootstrap_retries_after_failure(  # type: ignore[no-untyped-def]
    bootstrap, monkeypatch
) -> None:
    """A ClickHouse blip must not permanently skip the bootstrap — the cache is
    only set on success (same contract as ensure_personal_namespace_cached)."""
    broken = _FakeCH(fail=True)
    _wire(monkeypatch, broken)
    await bootstrap.ensure_default_namespace_cached()
    assert broken.inserted == []

    healthy = _FakeCH()
    _wire(monkeypatch, healthy)
    await bootstrap.ensure_default_namespace_cached()
    assert len(healthy.inserted) == 2


async def test_bootstrap_single_flight(  # type: ignore[no-untyped-def]
    bootstrap, monkeypatch
) -> None:
    """N concurrent first requests must not each probe + insert."""
    import asyncio

    ch = _FakeCH()
    _wire(monkeypatch, ch)

    await asyncio.gather(*(bootstrap.ensure_default_namespace_cached() for _ in range(8)))

    assert ch.probe_calls == 1
    assert len(ch.inserted) == 2


async def test_warning_scan_failure_does_not_unset_cache(  # type: ignore[no-untyped-def]
    bootstrap, monkeypatch, caplog
) -> None:
    """The stranded-namespace scan is a diagnostic, not part of the bootstrap
    contract: if it throws after a successful insert, subsequent requests must
    not re-probe (per-request retry storm against a degraded ClickHouse)."""
    import logging

    class _WarnBroken(_FakeCH):
        async def query_one(self, sql, params=None):  # type: ignore[no-untyped-def]
            if "count()" in sql:
                raise RuntimeError("scan timeout")
            return await super().query_one(sql, params)

    ch = _WarnBroken()
    _wire(monkeypatch, ch)

    with caplog.at_level(logging.WARNING, logger="server.auth.default_namespace"):
        await bootstrap.ensure_default_namespace_cached()
        await bootstrap.ensure_default_namespace_cached()

    assert ch.probe_calls == 1  # cached despite the scan failure
    assert not any("bootstrap failed" in r.getMessage() for r in caplog.records)


async def test_bootstrap_warns_about_stranded_namespaces_on_insert(  # type: ignore[no-untyped-def]
    bootstrap, monkeypatch, caplog
) -> None:
    """Renaming the default strands the old namespace's data: reachable by
    SDK/MCP, invisible in the dashboard. Say so on the boot that (re)provisions the row —
    exactly when the operator who changed the setting is watching."""
    import logging

    from server.settings import settings

    ch = _FakeCH(exists=False, others=("local", "old-team"))
    _wire(monkeypatch, ch)
    monkeypatch.setattr(settings, "default_namespace", "team-alpha")

    with caplog.at_level(logging.WARNING, logger="server.auth.default_namespace"):
        await bootstrap.ensure_default_namespace_cached()

    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("local" in m and "old-team" in m for m in warnings), warnings


async def test_bootstrap_quiet_on_steady_state_boot(  # type: ignore[no-untyped-def]
    bootstrap, monkeypatch, caplog
) -> None:
    """A healthy deployment that deliberately holds extra namespaces must not
    re-hear the stranded warning on every boot × replica — it only fires on
    the boot that actually provisioned the row."""
    import logging

    ch = _FakeCH(exists=True, active=True, others=("intentional-extra",))
    _wire(monkeypatch, ch)

    with caplog.at_level(logging.WARNING, logger="server.auth.default_namespace"):
        await bootstrap.ensure_default_namespace_cached()

    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []
    assert ch.warn_calls == 0


def test_description_is_not_hardcoded_to_local() -> None:
    """The inserted row's description shows in the namespace UI — it can't keep
    calling a custom namespace "local"."""
    from server.auth.default_namespace import _description

    assert _description("local").startswith("Local single-user data.")
    custom = _description("team-alpha")
    assert "Local single-user data" not in custom
    assert "team-alpha" in custom
    # The operative half — what this namespace is for — survives either way.
    for text in (_description("local"), custom):
        assert "Persists across sessions and restarts." in text


# --- MCP/SDK caller defaults ------------------------------------------------


async def test_mcp_defaults_use_default_namespace_in_single_user(  # type: ignore[no-untyped-def]
    monkeypatch,
) -> None:
    """The blocking gap: an auth-off MCP/SDK caller must land in the SAME
    namespace the dashboard serves — not `personal-you`, and not a stale
    `loom.user_state` row left by an old click in the namespace picker (no DB lookup at
    all: get_client raising proves the resolution is static)."""
    from server.settings import settings
    from server.transport.mcp_identity import _caller_defaults

    def _boom() -> None:
        raise AssertionError("single-user resolution must not touch ClickHouse")

    monkeypatch.setattr("server.core.ch.get_client", _boom)
    with (
        patch.object(settings, "auth_required", False),
        patch.object(settings, "identity_provider", ""),
        patch.object(settings, "default_namespace", "team-alpha"),
    ):
        out = await _caller_defaults(None)

    assert out is not None
    assert out["namespace"] == "team-alpha"
    assert out["org"] == settings.self_signup_org


async def test_mcp_defaults_claimed_namespace_still_wins(  # type: ignore[no-untyped-def]
    monkeypatch,
) -> None:
    """A Setup-tab service-account token minted for a specific namespace keeps
    targeting it even in single-user mode."""
    from server.auth.jwt import AuthInfo
    from server.operations.base import Role
    from server.settings import settings
    from server.transport.mcp_identity import _caller_defaults

    async def _none_restricted(org: str) -> frozenset[str]:
        return frozenset()

    monkeypatch.setattr(
        "server.identity.app.namespace_classification.restricted_set", _none_restricted
    )
    auth = AuthInfo(
        subject="sa:svc",
        org="dev",
        namespaces=("minted-ns",),
        roles=(Role.WRITER,),
        is_service_account=True,
    )
    with (
        patch.object(settings, "auth_required", False),
        patch.object(settings, "identity_provider", ""),
        patch.object(settings, "default_namespace", "team-alpha"),
    ):
        out = await _caller_defaults(auth)

    assert out is not None
    assert out["namespace"] == "minted-ns"


async def test_boot_snippet_survives_hostile_config() -> None:
    """End-to-end on the assembled snippet, not just the helper: every global
    must still be assigned when the operator's values are pathological."""
    import json
    import re

    from starlette.requests import Request

    from server.identity.app import dashboard_routes as me_mod
    from server.settings import settings

    if not (me_mod._FRONTEND_DIST / "index.html").is_file():
        pytest.skip("frontend not built (npm --prefix frontend run build)")

    request = Request({"type": "http", "method": "GET", "path": "/settings", "headers": []})
    with (
        # auth on: no ClickHouse round-trip on this path.
        patch.object(settings, "auth_required", True),
        patch.object(settings, "identity_provider", "gateway"),
        patch.object(settings, "default_namespace", "team\\"),
    ):
        response = await me_mod.dashboard_page(request)

    html = response.body.decode()
    globals_found = dict(re.findall(r"window\.__LOOM_([A-Z_]+) = (.+?);", html))
    # A broken literal would swallow the assignments that follow it.
    assert globals_found["AUTH_DISABLED"] == "false"
    assert globals_found["SELF_SIGNUP"] in ("true", "false")
    assert json.loads(globals_found["DEFAULT_NAMESPACE"]) == "team\\"
