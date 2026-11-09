"""The SQL surface's views and its scope rows: every view reads through its
definer and admits only the scope rows of the user that sent the statement,
and `loom sql-access` writes whole rows that give a user exactly one scope."""

from __future__ import annotations

import asyncio
import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from server.core.paths import MIGRATIONS_DIR
from server.insights.domain import sql_surface as surface
from tests.unit.ch_fake import RecordingCH
from tests.unit.schema_ddl import ddl

MIGRATION = MIGRATIONS_DIR / "090_sql_surface.sql"
NOW = datetime(2026, 1, 2, tzinfo=UTC)
_VIEW = re.compile(r"CREATE VIEW IF NOT EXISTS loom\.(\w+)(.*?);", re.S)


def _views() -> dict[str, str]:
    return dict(_VIEW.findall(MIGRATION.read_text(encoding="utf-8")))


def test_the_granted_views_are_the_migrations_views() -> None:
    assert sorted(_views()) == sorted(surface.VIEWS)


@pytest.mark.parametrize("view", surface.VIEWS)
def test_each_view_reads_through_its_definer(view: str) -> None:
    """What each view admits is the integration test's (test_sql_surface_live_ch.py)."""
    body = " ".join(_views()[view].split())
    assert body.startswith("DEFINER = CURRENT_USER SQL SECURITY DEFINER AS"), view


_SCOPE_CTE = re.compile(r"WITH scope AS \(.*?\)\s*SELECT", re.S)
_ADMIT = re.compile(
    r"\(\(org, namespace\) IN \(SELECT org, namespace FROM scope\).*?restricted\)", re.S
)


def test_every_view_scopes_and_excludes_alike() -> None:
    """One changed clause in one view would widen it silently."""
    shapes = set()
    for view, body in _views().items():
        flat = " ".join(body.split())
        cte, admit = _SCOPE_CTE.search(flat), _ADMIT.search(flat)
        assert cte and admit, view
        shapes.add((cte.group(0), admit.group(0)))
    assert len(shapes) == 1, shapes


def test_the_profile_lets_a_statement_change_only_harmless_settings() -> None:
    caps = surface.Caps(10, 1000, 2000, 4, 8)
    create, alter = surface.profile_sql(caps)
    assert create.startswith(f"CREATE SETTINGS PROFILE IF NOT EXISTS `{surface.PROFILE}`")
    for statement in (create, alter):
        listed = statement.split("` SETTINGS ", 1)[1].split(", ")
        changeable = [s.split()[0] for s in listed if s.endswith(" CHANGEABLE_IN_READONLY")]
        assert changeable == ["final", "join_use_nulls", "group_by_use_nulls", "transform_null_in"]
        assert [s for s in listed if not s.endswith("CHANGEABLE_IN_READONLY")] == [
            *surface.PINNED,
            "max_execution_time = 10",
            "max_rows_to_read = 1000",
            "max_memory_usage = 2000",
            "max_threads = 4",
            "max_concurrent_queries_for_user = 8",
        ]
    assert surface.PINNED[0] == "readonly = 1 CONST"
    for pin in (
        "apply_deleted_mask = 1 CONST",
        "use_query_cache = 0 CONST",
        "use_query_condition_cache = 0 CONST",
        "optimize_move_to_prewhere_if_final = 0 CONST",
        "apply_patch_parts = 1 CONST",
        "use_skip_indexes_if_final_exact_mode = 1 CONST",
        "do_not_merge_across_partitions_select_final = 0 CONST",
        "query_plan_filter_push_down = 0 CONST",
        "enable_optimize_predicate_expression = 0 CONST",
        "allow_introspection_functions = 0 CONST",
        "send_logs_level = 'none' CONST",
        *(f"{mode} = 'throw' CONST" for mode in surface.OVERFLOW_MODES),
    ):
        assert pin in surface.PINNED, pin
    assert len(surface.OVERFLOW_MODES) == 11


def test_the_access_rows_are_whole() -> None:
    columns = re.findall(r"^\s+`(\w+)`", ddl("sql_access"), re.M)
    assert tuple(columns) == surface.ACCESS_COLUMNS
    rows = surface.access_rows("u", surface.Scope("o", ("a",)), set(), NOW)
    assert rows == [
        {"user_name": "u", "org": "o", "namespace": "a", "granted": True, "updated_at": NOW}
    ]


def test_a_rescope_revokes_what_the_user_no_longer_reads() -> None:
    held = {("o", "a"), ("o", "b"), ("p", "*")}
    rows = surface.access_rows("u", surface.Scope("o", ("b", "c")), held, NOW)
    assert [(r["org"], r["namespace"], r["granted"]) for r in rows] == [
        ("o", "a", False),
        ("o", "b", True),
        ("o", "c", True),
        ("p", "*", False),
    ]
    revoked = surface.revoke_rows("u", held, NOW)
    assert {(r["org"], r["namespace"]) for r in revoked} == held
    assert not any(r["granted"] for r in revoked)


def test_the_whole_deployment_is_one_row() -> None:
    assert surface.Scope(surface.ALL, ()).rows() == {("*", "*")}


@pytest.mark.parametrize(
    ("scope", "refused"),
    [
        (surface.Scope("o", ("a", "b")), False),
        (surface.Scope("o", ("*",)), False),
        (surface.Scope("*", ()), False),
        (surface.Scope("*", ("a",)), True),
        (surface.Scope("", ("a",)), True),
        (surface.Scope("o", ()), True),
        (surface.Scope("o", ("*", "a")), True),
        (surface.Scope("o", ("",)), True),
    ],
)
def test_a_scope_names_something_to_read(scope: surface.Scope, refused: bool) -> None:
    assert bool(surface.scope_problem(scope)) is refused


@pytest.mark.parametrize(
    ("name", "refused"),
    [
        ("acme_bi", False),
        ("a" * 64, False),
        ("a" * 65, True),
        ("1acme", True),
        ("acme`; DROP USER loom; --", True),
        ("acme-bi", True),
        ("loom", True),
    ],
)
def test_a_user_name_is_plain_and_not_looms_own(name: str, refused: bool) -> None:
    assert bool(surface.user_name_problem(name, {"loom"})) is refused


def test_a_drawn_password_meets_the_cloud_rules() -> None:
    password = surface.draw_password()
    assert re.search(r"[a-z]", password) and re.search(r"[A-Z]", password)
    assert re.search(r"[0-9]", password) and re.search(r"[^A-Za-z0-9]", password)
    assert len(password) >= 40 and password != surface.draw_password()


def test_a_new_user_is_granted_the_views_alone() -> None:
    assert surface.create_user_sql("acme_bi", "pw'x") == (
        "CREATE USER `acme_bi` IDENTIFIED WITH sha256_password BY 'pw\\'x' "
        f"SETTINGS PROFILE '{surface.PROFILE}'"
    )
    grants = surface.grant_sql("`loom`", "acme_bi")
    assert grants == [f"GRANT SELECT ON `loom`.`{v}` TO `acme_bi`" for v in surface.VIEWS]


@pytest.mark.parametrize(
    "argv",
    [
        ["--user", "u", "--org", "o", "--all"],
        ["--user", "u", "--namespaces", "a"],
    ],
)
def test_the_cli_takes_an_org_or_the_whole_deployment(argv: list[str]) -> None:
    import argparse

    from server.cli.sql_access import register_sql_access_cli

    parser = argparse.ArgumentParser()
    register_sql_access_cli(parser.add_subparsers())
    with pytest.raises(SystemExit):
        parser.parse_args(["sql-access", "grant", *argv])


def test_star_is_not_an_org_name() -> None:
    import argparse

    from server.cli.sql_access import _scope, register_sql_access_cli

    parser = argparse.ArgumentParser()
    register_sql_access_cli(parser.add_subparsers())
    args = parser.parse_args(["sql-access", "grant", "--user", "u", "--org", "*"])
    assert surface.scope_problem(_scope(args)) == "name the org"


def test_the_whole_deployment_takes_no_namespaces_from_the_cli() -> None:
    import argparse

    from server.cli.sql_access import _scope, register_sql_access_cli

    parser = argparse.ArgumentParser()
    register_sql_access_cli(parser.add_subparsers())
    args = parser.parse_args(["sql-access", "grant", "--user", "u", "--all", "--namespaces", "a"])
    assert surface.scope_problem(_scope(args)) == "the whole deployment takes no namespaces"


def test_a_grant_never_reaches_a_restricted_namespace_the_views_cannot_leave_out() -> None:
    config = {("o", "code-private"), ("p", "code-private")}
    directory = {("o", "hr")}

    def reach(org: str, *namespaces: str) -> list[tuple[str, str]]:
        return surface.restricted_reach(surface.Scope(org, namespaces), config, directory)

    assert reach("o", "support") == []
    assert reach("o", "hr") == [("o", "hr")]
    assert reach("o", "code-private") == [("o", "code-private")]
    # A wildcard leaves the directory's restricted namespaces to the views.
    assert reach("o", "*") == [("o", "code-private")]
    assert reach("*") == [("o", "code-private"), ("p", "code-private")]


def test_a_later_row_wins_over_a_grant_from_a_clock_ahead() -> None:
    ahead = datetime(2026, 1, 2, 0, 5, tzinfo=UTC)
    assert surface.next_version(NOW, ahead) == ahead + timedelta(milliseconds=1)
    assert surface.next_version(NOW, None) == NOW
    # ClickHouse hands DateTime64 back naive; it is UTC.
    assert surface.next_version(NOW, ahead.replace(tzinfo=None)) > ahead


def _fake(
    *, exists: bool, held: list[dict[str, Any]] | None = None, cls: type[RecordingCH] = RecordingCH
) -> RecordingCH:
    ch = cls()
    ch.on("FROM loom.namespaces FINAL", [])
    ch.on("FROM system.users", [{"n": int(exists)}])
    ch.on("FROM system.settings_profile_elements", [{"n": int(exists)}])
    ch.on("FROM loom.sql_access FINAL", held or [])
    return ch


def _grant(ch: RecordingCH, check: Any, *, rotate: bool = False) -> Any:
    from server.insights.app import sql_access

    scope = surface.Scope("o", ("a",))
    return asyncio.run(sql_access.grant("acme_bi", scope, login_check=check, rotate=rotate, ch=ch))


def _executed(ch: RecordingCH) -> list[str]:
    """Each statement sent, by its first two words."""
    return [
        " ".join(s.sql.split()[:2]) if s.kind == "execute" else "INSERT"
        for s in ch.calls
        if s.kind in ("execute", "insert")
    ]


@pytest.fixture(autouse=True)
def _no_code_repos(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("server.insights.app.sql_access.config_entries", list)


def test_the_restricted_config_is_read_before_the_registry_is(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A CLI process never reads the code registry: the check must not need it."""
    from server.code.domain.index import repos
    from server.insights.app import sql_access

    # The real accessor, not the autouse stand-in; the registry never read.
    monkeypatch.setattr(sql_access, "config_entries", repos.config_entries)
    monkeypatch.setattr(repos.settings, "code_self_service_enabled", True)
    monkeypatch.setattr(repos, "_registry_loaded", False)
    entries = [
        {
            "org": "acme",
            "namespace": "code-private",
            "repo": "acme/private",
            "classification": "restricted",
        },
        {"org": "acme", "namespace": "code-open", "repo": "acme/open"},
    ]
    monkeypatch.setattr(repos.settings, "code_repos_json", json.dumps(entries))
    with pytest.raises(repos.RegistryUnread):
        repos.configured_repos()
    assert sql_access.config_restricted() == {("acme", "code-private")}


def test_a_new_user_exists_before_its_scope_and_is_granted_last() -> None:
    ch = _fake(exists=False)
    done = _grant(ch, lambda user, password: "")
    assert done.created and done.password
    steps = [s for s in _executed(ch) if not s.endswith("SETTINGS")]
    assert steps == ["CREATE USER", "INSERT", *["GRANT SELECT"] * len(surface.VIEWS)]
    assert [r["granted"] for r in ch.inserted("sql_access")] == [True]


def test_a_login_that_reads_the_system_tables_drops_the_user() -> None:
    from server.insights.app.sql_access import AccessRefused

    ch = _fake(exists=False)
    with pytest.raises(AccessRefused, match="was dropped: it can read system.processes"):
        _grant(ch, lambda user, password: "it can read system.processes")
    assert not ch.inserted("sql_access")
    assert not ch.statements("GRANT ")
    assert ch.statements("DROP USER IF EXISTS `acme_bi`")


def test_an_unchecked_rotation_leaves_the_users_scope_and_grants() -> None:
    from server.insights.app.sql_access import AccessRefused, LoginUnchecked

    def unchecked(user: str, password: str) -> str:
        raise LoginUnchecked("could not log in as it: timed out")

    ch = _fake(exists=True)
    with pytest.raises(AccessRefused, match="could not be checked; .* --rotate"):
        _grant(ch, unchecked, rotate=True)
    assert ch.statements("ALTER USER `acme_bi`")
    assert not ch.inserted("sql_access") and not ch.statements("DROP USER")


def test_an_unchecked_new_user_is_dropped() -> None:
    from server.insights.app.sql_access import AccessRefused, LoginUnchecked

    def unchecked(user: str, password: str) -> str:
        raise LoginUnchecked("could not log in as it: timed out")

    ch = _fake(exists=False)
    with pytest.raises(AccessRefused, match="was dropped"):
        _grant(ch, unchecked)
    assert ch.statements("DROP USER IF EXISTS `acme_bi`") and not ch.inserted("sql_access")


def test_a_revocation_outranks_rows_a_clock_ahead_wrote() -> None:
    from server.insights.app import sql_access

    ahead = datetime(2099, 1, 1, tzinfo=UTC)
    ch = _fake(
        exists=True, held=[{"org": "o", "namespace": "a", "granted": True, "updated_at": ahead}]
    )
    done = asyncio.run(sql_access.revoke("acme_bi", ch=ch))
    assert done.held == {("o", "a")} and done.dropped
    (row,) = ch.inserted("sql_access")
    assert row["granted"] is False and row["updated_at"] > ahead
    assert ch.statements("DROP USER IF EXISTS `acme_bi`")


def test_a_revocation_takes_the_scope_from_a_name_another_user_holds() -> None:
    from server.insights.app import sql_access

    ch = RecordingCH()
    ch.on(
        "FROM loom.sql_access FINAL",
        [{"org": "o", "namespace": "a", "granted": True, "updated_at": NOW}],
    )
    ch.on("FROM system.users", [{"n": 1}])
    ch.on("FROM system.settings_profile_elements", [{"n": 0}])
    with pytest.raises(sql_access.AccessRefused, match="scope rows are revoked"):
        asyncio.run(sql_access.revoke("acme_bi", ch=ch))
    assert [r["granted"] for r in ch.inserted("sql_access")] == [False]
    assert not ch.statements("DROP USER")


class _FailingInsertCH(RecordingCH):
    async def insert(self, table: str, rows: list[dict[str, Any]], **kw: Any) -> None:
        raise RuntimeError("insert refused")


def test_a_failure_after_a_new_password_says_to_rotate() -> None:
    from server.insights.app.sql_access import AccessRefused

    ch = _fake(exists=False, cls=_FailingInsertCH)
    with pytest.raises(
        AccessRefused,
        match="new password but its scope or grants were not written; run grant again with --rotate",
    ):
        _grant(ch, lambda user, password: "")
    assert ch.statements("CREATE USER `acme_bi`") and not ch.statements("GRANT ")


def test_list_names_any_grant_the_code_repo_configuration_now_restricts(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import argparse

    from server.cli import sql_access as cli

    rows = [
        {"user_name": "acme_bi", "org": "acme", "namespace": "support", "updated_at": NOW},
        {"user_name": "acme_bi", "org": "acme", "namespace": "repo", "updated_at": NOW},
    ]
    monkeypatch.setattr(cli, "_connected", lambda work: rows)
    monkeypatch.setattr(
        "server.insights.app.sql_access.config_restricted", lambda: {("acme", "repo")}
    )
    assert cli._cmd_list(argparse.Namespace()) == cli.REFUSED
    err = capsys.readouterr().err
    assert "reaches acme/repo" in err and "support" not in err

    monkeypatch.setattr("server.insights.app.sql_access.config_restricted", set)
    assert cli._cmd_list(argparse.Namespace()) == 0


class _Login:
    """clickhouse_connect.get_client as the login check calls it: `fails` auth
    failures first, then a client whose read of system.processes raises `read`
    (or answers when it is None)."""

    def __init__(self, fails: int, read: Exception | None) -> None:
        self.fails, self.read, self.calls = fails, read, 0

    def __call__(self, **kw: Any) -> Any:
        self.calls += 1
        if self.calls <= self.fails:
            raise RuntimeError("Code: 516. DB::Exception: Authentication failed")
        login = self

        class _Client:
            def query(self, sql: str) -> None:
                assert sql == "SELECT count() FROM system.processes"
                if login.read is not None:
                    raise login.read

            def close(self) -> None:
                pass

        return _Client()


def _check(monkeypatch: pytest.MonkeyPatch, login: _Login) -> str:
    import clickhouse_connect

    from server.cli.sql_access import _login_check

    monkeypatch.setattr(clickhouse_connect, "get_client", login)
    monkeypatch.setattr("time.sleep", lambda s: None)
    return _login_check("acme_bi", "pw")


def test_the_login_check_waits_for_a_replica_to_see_the_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    denied = RuntimeError("Code: 497. DB::Exception: Not enough privileges")
    login = _Login(fails=2, read=denied)
    assert _check(monkeypatch, login) == "" and login.calls == 3


def test_a_login_that_never_lands_is_unchecked(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.cli.sql_access import LOGIN_ATTEMPTS
    from server.insights.app.sql_access import LoginUnchecked

    login = _Login(fails=LOGIN_ATTEMPTS, read=None)
    with pytest.raises(LoginUnchecked, match="could not log in as it"):
        _check(monkeypatch, login)
    assert login.calls == LOGIN_ATTEMPTS


def test_a_login_that_reads_system_processes_is_unsafe(monkeypatch: pytest.MonkeyPatch) -> None:
    assert "it can read system.processes" in _check(monkeypatch, _Login(fails=0, read=None))


def test_a_read_that_fails_otherwise_is_unchecked(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.insights.app.sql_access import LoginUnchecked

    timeout = RuntimeError("Code: 159. DB::Exception: Timeout exceeded")
    with pytest.raises(LoginUnchecked, match="could not read as it"):
        _check(monkeypatch, _Login(fails=0, read=timeout))


_WINDOW = "AND ts >= toStartOfDay(now(), 'UTC') - INTERVAL 31 DAY"
_LEDGER = {"sql_v1_agent_events", "sql_v1_usage_daily"}


@pytest.mark.parametrize("view", surface.VIEWS)
def test_the_ledger_views_read_their_window_and_no_other_view_has_one(view: str) -> None:
    """A caller's filter runs on what a sealed view returns, so the window that
    bounds a ledger read must sit in the view's own WHERE, right after the admit
    clause."""
    flat = " ".join(_views()[view].split())
    admit = _ADMIT.search(flat)
    assert admit, view
    after = flat[admit.end() :].lstrip()
    if view in _LEDGER:
        assert after.startswith(_WINDOW), (view, after[:80])
        rest = after[len(_WINDOW) :].strip()
        assert rest == "" or rest.startswith("GROUP BY"), (view, rest[:80])
    else:
        assert "INTERVAL" not in flat, view


def test_the_daily_usage_columns_are_the_documented_ones() -> None:
    """A v1 view only gains columns: nothing may slip into the contract."""
    columns = re.findall(r"^\s+`(\w+)`", ddl("sql_v1_usage_daily"), re.M)
    assert columns == [
        "day",
        "org",
        "namespace",
        "agent",
        "operation",
        "kind",
        "ops",
        "errors",
        "input_tokens",
        "output_tokens",
        "embedding_tokens",
        "llm_calls",
        "upstream_cost_usd",
        "bytes_written",
        "latency_sum_ms",
        "latency_p50_ms",
        "latency_p95_ms",
    ]


def test_the_cli_prints_why_a_login_check_could_not_run(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The app's refusal names no caught error; the operator who ran the command reads
    the cause on the next line."""
    import argparse

    from server.cli import sql_access as cli
    from server.insights.app.sql_access import AccessRefused, LoginUnchecked

    def refused(work: Any) -> Any:
        cause = LoginUnchecked("could not log in as it: Code: 516. Authentication failed")
        raise AccessRefused("acme_bi was dropped: its login could not be checked") from cause

    monkeypatch.setattr(cli, "_connected", refused)
    args = argparse.Namespace(user="acme_bi", org="o", namespaces="a", all=False, rotate=False)
    assert cli._cmd_grant(args) == cli.REFUSED
    err = capsys.readouterr().err
    assert "its login could not be checked" in err
    assert "could not log in as it: Code: 516" in err
