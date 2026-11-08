"""insights.query's handling around the boundary: how ClickHouse errors are
answered, what the probe calls repairable, and when nothing is written."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from server.core.errors import AnalysisUnavailableError, RateLimitedError, ValidationError
from server.core.ratelimit import op_class_for
from server.insights.api.models_analysis import InsightsQueryInput
from server.insights.app import analysis as app
from server.insights.domain import analysis as boundary

SECRETS = (bytes(range(32)), "pw")


@pytest.fixture
def ready(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app, "_loaded", SECRETS)
    monkeypatch.setattr(app, "_ready", True)
    monkeypatch.setattr(app, "_problem", "")
    monkeypatch.setattr(app, "_running", {})
    monkeypatch.setattr(app.store, "target_database", lambda: "`loom`")


def _failing(message: str):
    async def run(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError(message)

    return run


def _query(namespace: str = "ns") -> InsightsQueryInput:
    return InsightsQueryInput(org="acme", namespace=namespace, sql="SELECT 1")


class _NoLease:
    def __init__(self, *_: Any) -> None:
        pass

    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *_: Any) -> None:
        return None


def _returns(value: Any):
    async def fn(*_: Any, **__: Any) -> Any:
        return value

    return fn


@pytest.mark.parametrize(
    ("message", "answer"),
    [
        ("Code: 202. DB::Exception: Too many simultaneous queries", RateLimitedError),
        ("Code: 516. DB::Exception: loom_analyst: Authentication failed", AnalysisUnavailableError),
        (
            "Code: 241. DB::Exception: (total) memory limit exceeded: would use 9 GiB",
            AnalysisUnavailableError,
        ),
        ("Code: 241. DB::Exception: Memory limit (for query) exceeded", ValidationError),
        ("Code: 62. DB::Exception: Syntax error", ValidationError),
    ],
)
def test_clickhouse_errors_are_answered_by_their_cause(
    monkeypatch: pytest.MonkeyPatch, ready: None, message: str, answer: type[Exception]
) -> None:
    monkeypatch.setattr(app.store, "run_statement", _failing(message))
    with pytest.raises(answer):
        asyncio.run(app.handle_insights_query(_query()))


def test_a_refusal_never_quotes_a_key_pad(monkeypatch: pytest.MonkeyPatch, ready: None) -> None:
    leak = "Code: 62. DB::Exception: in filter hex(SHA256(concat(unhex('" + "36" * 64 + "'), x)))"
    monkeypatch.setattr(app.store, "run_statement", _failing(leak))
    with pytest.raises(ValidationError) as caught:
        asyncio.run(app.handle_insights_query(_query()))
    assert "36" * 64 not in str(caught.value)


def test_a_namespace_with_a_comma_is_refused(ready: None) -> None:
    # The policy splits the signed namespaces on commas.
    with pytest.raises(ValidationError):
        asyncio.run(app.handle_insights_query(_query("a,b")))


def test_insights_query_has_its_own_rate_limit_class() -> None:
    assert op_class_for("insights.query") == "insights.query"
    assert op_class_for("insights.catalog") == "default"


def test_a_reserved_scope_read_refused_for_want_of_grants_is_repairable(
    monkeypatch: pytest.MonkeyPatch, ready: None
) -> None:
    async def canary(_: Any) -> bool:
        return True

    monkeypatch.setattr(app, "_canary_reads", canary)
    monkeypatch.setattr(
        app.store, "run_statement", _failing("Code: 497. DB::Exception: Not enough privileges")
    )
    problem, repairable = asyncio.run(app._probe())
    assert "lacks its grants on" in problem
    assert repairable


def test_a_foreign_permissive_policy_stops_the_repair(
    monkeypatch: pytest.MonkeyPatch, ready: None
) -> None:
    written: list[list[str]] = []

    async def probe() -> tuple[str, bool]:
        return "memory admits rows in the reserved scope", True

    async def yes(*_: Any, **__: Any) -> bool:
        return True

    async def foreign(*_: Any, **__: Any) -> list[str]:
        return ["bi_scope ON memory"]

    async def none(*_: Any, **__: Any) -> None:
        return None

    async def write(statements: list[str], **_: Any) -> None:
        written.append(statements)

    monkeypatch.setattr(app, "_probe", probe)
    monkeypatch.setattr(app, "lease", _NoLease)
    monkeypatch.setattr(app.store, "secrets_table_exists", _returns(True))
    monkeypatch.setattr(app.store, "sealed_secrets", none)
    monkeypatch.setattr(app.store, "can_manage_access", yes)
    monkeypatch.setattr(app.store, "foreign_policies", foreign)
    monkeypatch.setattr(app.store, "other_deployments", none)
    monkeypatch.setattr(app.store, "write_entities", write)
    asyncio.run(app.reprobe())
    assert not written
    ready_now, problem = app.analysis_state()
    assert ready_now is False
    assert "bi_scope ON memory" in problem


def test_ch_setup_refuses_a_new_key_beside_a_live_analyst_user(
    monkeypatch: pytest.MonkeyPatch, ready: None
) -> None:
    async def none(*_: Any, **__: Any) -> None:
        return None

    async def no(*_: Any, **__: Any) -> bool:
        return False

    monkeypatch.setattr(app, "lease", _NoLease)
    monkeypatch.setattr(app.store, "secrets_table_exists", _returns(True))
    monkeypatch.setattr(app.store, "sealed_secrets", none)
    monkeypatch.setattr(app.store, "analyst_user_absent", no)
    with pytest.raises(RuntimeError, match=f"DROP USER {boundary.ANALYST_USER}"):
        asyncio.run(app.rewrite())


def test_ch_setup_refuses_over_a_newer_builds_entities(
    monkeypatch: pytest.MonkeyPatch, ready: None
) -> None:
    written: list[Any] = []
    monkeypatch.setattr(app, "lease", _NoLease)
    monkeypatch.setattr(app.store, "secrets_table_exists", _returns(True))
    monkeypatch.setattr(app.store, "sealed_secrets", _returns("sealed"))
    monkeypatch.setattr(app, "_opened", lambda _: SECRETS)
    newer = boundary.Stored(boundary.ENTITY_VERSION + 1, "theirs")
    monkeypatch.setattr(app.store, "stored_entities", _returns(newer))
    monkeypatch.setattr(app.store, "write_entities", lambda s, **_: written.append(s))
    with pytest.raises(RuntimeError, match="newer build"):
        asyncio.run(app.rewrite())
    assert not written


def _provisioning(monkeypatch: pytest.MonkeyPatch, *, stored: Any, caps: str, foreign: list[str]):
    written: list[list[str]] = []

    async def write(statements: list[str], **_: Any) -> None:
        written.append(statements)

    monkeypatch.setattr(app, "lease", _NoLease)
    monkeypatch.setattr(app, "load_secrets", _returns(None))
    monkeypatch.setattr(app, "reprobe", _returns(None))
    monkeypatch.setattr(app.store, "analyst_user_absent", _returns(False))
    monkeypatch.setattr(app.store, "can_manage_access", _returns(True))
    monkeypatch.setattr(app.store, "other_deployments", _returns([]))
    monkeypatch.setattr(app.store, "foreign_policies", _returns(foreign))
    monkeypatch.setattr(app.store, "granted", _returns(frozenset()))
    monkeypatch.setattr(app.store, "granted_roles", _returns(frozenset()))
    monkeypatch.setattr(app.store, "stored_entities", _returns(stored))
    monkeypatch.setattr(app.store, "stored_caps_digest", _returns(caps))
    monkeypatch.setattr(app.store, "write_entities", write)
    return written


def _current() -> boundary.Stored:
    statements = app.entity_statements()
    return boundary.Stored(boundary.ENTITY_VERSION, boundary.entity_digest(statements))


def test_a_boot_writes_only_the_caps_when_only_a_cap_changed(
    monkeypatch: pytest.MonkeyPatch, ready: None
) -> None:
    written = _provisioning(monkeypatch, stored=_current(), caps="old", foreign=[])
    asyncio.run(app.provision())
    assert written == [boundary.caps_sql(app.caps())]


def test_a_boot_writes_nothing_when_the_entities_and_caps_are_current(
    monkeypatch: pytest.MonkeyPatch, ready: None
) -> None:
    digest = boundary.caps_digest(app.caps())
    written = _provisioning(monkeypatch, stored=_current(), caps=digest, foreign=[])
    asyncio.run(app.provision())
    assert written == []


def test_an_older_build_leaves_a_newer_builds_caps(
    monkeypatch: pytest.MonkeyPatch, ready: None
) -> None:
    newer = boundary.Stored(boundary.ENTITY_VERSION + 1, "theirs")
    written = _provisioning(monkeypatch, stored=newer, caps="theirs", foreign=[])
    asyncio.run(app.provision())
    assert written == []


def test_a_boot_writes_nothing_over_a_foreign_permissive_policy(
    monkeypatch: pytest.MonkeyPatch, ready: None
) -> None:
    written = _provisioning(monkeypatch, stored=None, caps="", foreign=["bi_scope ON memory"])
    asyncio.run(app.provision())
    assert written == []
    assert "bi_scope ON memory" in app.analysis_state()[1]


def test_a_foreign_policy_outlasts_a_clean_probe(
    monkeypatch: pytest.MonkeyPatch, ready: None
) -> None:
    # Such a policy widens another user, never the analyst, so the probe passes.
    monkeypatch.setattr(app, "_probe", _returns(("", False)))
    monkeypatch.setattr(app.store, "sealed_secrets", _returns(None))
    monkeypatch.setattr(app.store, "can_manage_access", _returns(True))
    monkeypatch.setattr(app.store, "other_deployments", _returns([]))
    monkeypatch.setattr(app.store, "foreign_policies", _returns(["bi_scope ON memory"]))
    asyncio.run(app.reprobe())
    assert app.analysis_state()[0] is False
    assert "bi_scope ON memory" in app.analysis_state()[1]


def test_another_loom_database_on_the_server_stops_the_write(
    monkeypatch: pytest.MonkeyPatch, ready: None
) -> None:
    written = _provisioning(monkeypatch, stored=None, caps="", foreign=[])
    monkeypatch.setattr(app.store, "other_deployments", _returns(["loom_staging"]))
    asyncio.run(app.provision())
    assert written == []
    assert "loom_staging" in app.analysis_state()[1]


def test_an_org_past_its_concurrency_cap_is_rate_limited(
    monkeypatch: pytest.MonkeyPatch, ready: None
) -> None:
    monkeypatch.setattr(app, "_running", {"acme": 99})
    with pytest.raises(RateLimitedError):
        asyncio.run(app.handle_insights_query(_query()))


def test_analyst_statements_carry_no_traffic_class_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.core import ch
    from server.core.traffic_context import traffic_class_var

    monkeypatch.setattr(ch, "traffic_class_settings", lambda _: {"max_threads": 64})
    token = traffic_class_var.set(("interactive", ""))
    try:
        with ch.as_analyst(boundary.ANALYST_USER, "pw", {"max_threads": 4}):
            assert ch.statement_settings("SELECT 1")["max_threads"] == 4
        assert ch.statement_settings("SELECT 1").get("max_threads") == 64
    finally:
        traffic_class_var.reset(token)


def test_the_printed_sql_revokes_what_show_grants_lists_before_granting(
    monkeypatch: pytest.MonkeyPatch, ready: None
) -> None:
    lines = [
        "GRANT SELECT(org, namespace) ON loom.memory TO loom_analyst",
        "GRANT SELECT ON loom.signing_keys TO loom_analyst WITH GRANT OPTION",
        "GRANT bi_reader TO loom_analyst",
    ]
    monkeypatch.setattr(app.store, "own_grant_lines", _returns(lines))
    revokes = asyncio.run(app.own_grant_revokes())
    assert revokes == [
        "REVOKE SELECT(org, namespace) ON loom.memory FROM loom_analyst",
        "REVOKE SELECT ON loom.signing_keys FROM loom_analyst",
        "REVOKE bi_reader FROM loom_analyst",
    ]
    printed = app.printed_statements(revokes)
    first_grant = next(i for i, s in enumerate(printed) if s.startswith("GRANT "))
    assert printed[first_grant - len(revokes) : first_grant] == revokes


def test_a_column_grant_in_another_database_is_outside_the_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Ch:
        async def query_columns(self, sql: str) -> Any:
            return ["g"], ["String"], [("GRANT SELECT(org) ON other.memory TO loom_analyst",)]

    monkeypatch.setattr(app.store, "database", lambda: "loom")
    held = asyncio.run(app.store.own_grants(password="pw", extra={}, ch=_Ch()))
    assert ("memory", "org") not in held


def test_analyst_statements_send_no_time_cap_of_their_own() -> None:
    # The caps profile's MAX applies: a replica whose own cap differs from the
    # stored one (during a roll) must not send a value the profile refuses.
    from server.core import ch

    with ch.as_analyst(boundary.ANALYST_USER, "pw", {}):
        assert "max_execution_time" not in ch.statement_settings("SELECT 1")


def test_the_printed_checks_name_looms_database(monkeypatch: pytest.MonkeyPatch) -> None:
    # The admin's session may sit in another database: the checks never read currentDatabase().
    monkeypatch.setattr(app.store, "database", lambda: "loom_prod")
    checks = app.printed_checks()
    assert all("currentDatabase" not in c for c in checks)
    assert "database = 'loom_prod'" in checks[0]
    assert "database != 'loom_prod'" in checks[1]


def test_the_per_org_count_comes_back_down_when_a_statement_fails(
    monkeypatch: pytest.MonkeyPatch, ready: None
) -> None:
    monkeypatch.setattr(app.store, "run_statement", _failing("Code: 62. DB::Exception: Syntax"))
    with pytest.raises(ValidationError):
        asyncio.run(app.handle_insights_query(_query()))
    assert app._running == {}


def test_print_sql_refuses_a_new_key_beside_a_live_analyst_user(
    monkeypatch: pytest.MonkeyPatch, ready: None
) -> None:
    drew: list[bool] = []
    monkeypatch.setattr(app, "lease", _NoLease)
    monkeypatch.setattr(app.store, "secrets_table_exists", _returns(True))
    monkeypatch.setattr(app.store, "sealed_secrets", _returns(None))
    monkeypatch.setattr(app.store, "can_manage_access", _returns(True))
    monkeypatch.setattr(app.store, "analyst_user_absent", _returns(False))

    async def load(*, may_draw: bool) -> None:
        drew.append(may_draw)

    monkeypatch.setattr(app, "load_secrets", load)
    with pytest.raises(RuntimeError, match="DROP USER"):
        asyncio.run(app.load_for_print())
    assert not drew


def test_print_sql_draws_where_it_cannot_see_the_users(
    monkeypatch: pytest.MonkeyPatch, ready: None
) -> None:
    drew: list[bool] = []

    async def load(*, may_draw: bool) -> None:
        drew.append(may_draw)

    monkeypatch.setattr(app, "lease", _NoLease)
    monkeypatch.setattr(app.store, "secrets_table_exists", _returns(True))
    monkeypatch.setattr(app.store, "sealed_secrets", _returns(None))
    monkeypatch.setattr(app.store, "can_manage_access", _returns(False))
    monkeypatch.setattr(app, "load_secrets", load)
    asyncio.run(app.load_for_print())
    assert drew == [True]


def test_ch_setup_prints_a_refusal_as_one_line_and_nothing_to_stdout(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import argparse

    from server.cli import ch_setup

    def refuse() -> list[str]:
        raise RuntimeError("the key the replicas hold is lost")

    monkeypatch.setattr(ch_setup, "_analyst_sql", refuse)
    monkeypatch.setattr(ch_setup, "caps_problem", lambda: "")
    args = argparse.Namespace(print_analyst_sql=True, print_sql=False)
    assert ch_setup._cmd_ch_setup(args) == 2
    out = capsys.readouterr()
    assert out.out == ""
    assert out.err.strip() == "✗ the key the replicas hold is lost"


def test_print_sql_is_not_gated_on_the_reader_users_settings(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import argparse

    from server.cli import ch_setup

    monkeypatch.setattr(ch_setup, "_analyst_sql", lambda: ["SELECT 1"])
    monkeypatch.setattr(app.store, "database", lambda: "loom")
    monkeypatch.setattr(ch_setup, "caps_problem", lambda: "a caller-shaped cap is below its floor")
    args = argparse.Namespace(print_analyst_sql=True, print_sql=False)
    assert ch_setup._cmd_ch_setup(args) == 0
    assert capsys.readouterr().out.rstrip().endswith("SELECT 1;")


@pytest.mark.parametrize("path", ["rewrite", "load_for_print"])
def test_before_the_first_boot_migrates_both_paths_say_to_run_again(
    monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    monkeypatch.setattr(app.store, "secrets_table_exists", _returns(False))
    with pytest.raises(RuntimeError, match="first boot has applied its migrations"):
        asyncio.run(getattr(app, path)())


def test_ch_setup_prints_a_clickhouse_error_as_one_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import argparse

    from server.cli import ch_setup

    def fail() -> list[str]:
        raise RuntimeError("Code: 60. DB::Exception: Unknown table\nStack trace:\n0. frame")

    monkeypatch.setattr(ch_setup, "_analyst_sql", fail)
    args = argparse.Namespace(print_analyst_sql=True, print_sql=False)
    assert ch_setup._cmd_ch_setup(args) == 2
    out = capsys.readouterr()
    assert out.out == ""
    assert len(out.err.strip().splitlines()) == 1


def test_a_standing_problem_is_logged_once(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(app, "_ready", None)
    monkeypatch.setattr(app, "_problem", "")
    for problem in ["missing", "missing", "missing", "wider", "", "wider"]:
        app._set_problem(problem)
    logged = [r.message for r in caplog.records if "is unavailable" in r.message]
    # Each change logs once: a new problem, and one that cleared and came back.
    assert logged == [
        "insights.query is unavailable: missing",
        "insights.query is unavailable: wider",
        "insights.query is unavailable: wider",
    ]


@pytest.mark.parametrize(
    ("error", "advised"),
    [(app.NOT_MIGRATED, False), ("Code: 497. DB::Exception: Not enough privileges", True)],
)
def test_ch_setups_analyst_line_advises_the_printed_sql_only_after_a_migration(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], error: str, advised: bool
) -> None:
    from server.cli import _ch, ch_setup

    async def fail() -> None:
        raise RuntimeError(error)

    monkeypatch.setattr(app, "rewrite", fail)
    monkeypatch.setattr(_ch, "run_connected", lambda fn: asyncio.run(fn(None)))
    ch_setup._write_analyst()
    line = capsys.readouterr().out.strip()
    assert line.startswith("· loom_analyst (insights.query): not written (")
    assert ("--print-analyst-sql" in line) is advised
