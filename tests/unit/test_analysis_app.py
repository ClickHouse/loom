"""insights.query's handling around the boundary: how ClickHouse errors are
answered, what the probe calls repairable, and when nothing is written."""

from __future__ import annotations

import asyncio
import re
from typing import Any

import pytest

from server.core.errors import (
    AnalysisUnavailableError,
    LoomServerError,
    OverloadedError,
    RateLimitedError,
    ValidationError,
    error_class,
)
from server.core.ratelimit import op_class_for
from server.insights.api.models_analysis import InsightsQueryInput
from server.insights.app import analysis as app
from server.insights.domain import analysis as boundary
from server.observability import problems

SECRETS = (bytes(range(32)), "pw")
NAMES = boundary.SettingNames("SQL_")
_CLOUD_PASSWORD_REFUSAL = (
    "Received ClickHouse exception, code: 36, server response: Code: 36. DB::Exception: "
    "Invalid password. The password should: contain at least 1 uppercase character, "
    "contain at least 1 special character. (BAD_ARGUMENTS) (version 26.10.2.93 (official build))"
)


@pytest.fixture(autouse=True)
def _sql_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    # ClickHouse's default config takes custom settings under SQL_.
    monkeypatch.setattr(app.store, "setting_prefix", _returns("SQL_"))
    monkeypatch.setattr(app, "_setting_names", None)


@pytest.fixture
def ready(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app, "_loaded", SECRETS)
    monkeypatch.setattr(app, "_setting_names", NAMES)
    monkeypatch.setattr(app, "_ready", True)
    monkeypatch.setattr(app, "_problem", "")
    monkeypatch.setattr(app, "_fix", "")
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


def _named_in_the_log_only(caplog: pytest.LogCaptureFixture, problem: str, name: str) -> None:
    # /v1/health shows the problem without a token: the names go to the log.
    assert problem and name not in problem
    assert any(name in r.message for r in caplog.records)


@pytest.mark.parametrize(
    ("message", "answer"),
    [
        ("Code: 202. DB::Exception: Too many simultaneous queries", RateLimitedError),
        ("Code: 516. DB::Exception: loom_analyst: Authentication failed", AnalysisUnavailableError),
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


def test_clickhouse_short_of_memory_is_answered_as_overloaded(
    monkeypatch: pytest.MonkeyPatch, ready: None
) -> None:
    # The client answers the server's or the user's memory running short as a 503.
    async def run(*args: Any, **kwargs: Any) -> Any:
        raise OverloadedError("ClickHouse is short of memory; retry shortly", retry_after_s=5)

    monkeypatch.setattr(app.store, "run_statement", run)
    with pytest.raises(OverloadedError, match="short of memory"):
        asyncio.run(app.handle_insights_query(_query()))


def test_a_refusal_never_quotes_a_key_pad(monkeypatch: pytest.MonkeyPatch, ready: None) -> None:
    leak = "Code: 62. DB::Exception: in filter hex(SHA256(concat(unhex('" + "36" * 64 + "'), x)))"
    monkeypatch.setattr(app.store, "run_statement", _failing(leak))
    with pytest.raises(ValidationError) as caught:
        asyncio.run(app.handle_insights_query(_query()))
    assert "36" * 64 not in str(caught.value)


@pytest.mark.parametrize("prefix", ["SQL_", "custom_"])
def test_a_refusal_naming_a_scope_setting_quotes_no_clickhouse_text(
    monkeypatch: pytest.MonkeyPatch, ready: None, prefix: str
) -> None:
    named = f"Code: 62. DB::Exception: Syntax error near {prefix}loom_sig quoting the filter"
    monkeypatch.setattr(app.store, "run_statement", _failing(named))
    with pytest.raises(ValidationError) as caught:
        asyncio.run(app.handle_insights_query(_query()))
    assert f"{prefix}loom_sig" not in str(caught.value) and "quoting" not in str(caught.value)


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
    problem, repairable, _ = asyncio.run(app._probe())
    assert "lacks its grants on" in problem
    assert repairable


def test_a_foreign_permissive_policy_stops_the_repair(
    monkeypatch: pytest.MonkeyPatch, ready: None, caplog: pytest.LogCaptureFixture
) -> None:
    written: list[list[str]] = []

    async def probe() -> tuple[str, bool, str]:
        return "memory admits rows in the reserved scope", True, ""

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
    ready_now, problem, _ = app.analysis_state()
    assert ready_now is False
    _named_in_the_log_only(caplog, problem, "bi_scope ON memory")


def test_ch_setup_refuses_a_new_key_beside_a_live_analyst_user(
    monkeypatch: pytest.MonkeyPatch, ready: None
) -> None:
    async def none(*_: Any, **__: Any) -> None:
        return None

    async def no(*_: Any, **__: Any) -> bool:
        return False

    monkeypatch.setattr(app, "lease", _NoLease)
    monkeypatch.setattr(app.store, "secrets_table_exists", _returns(True))
    monkeypatch.setattr(app.store, "can_manage_access", _returns(True))
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
    monkeypatch.setattr(app.store, "can_manage_access", _returns(True))
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
    return boundary.Stored(boundary.ENTITY_VERSION, boundary.entity_digest(NAMES, statements))


def test_a_boot_writes_only_the_caps_when_only_a_cap_changed(
    monkeypatch: pytest.MonkeyPatch, ready: None
) -> None:
    written = _provisioning(monkeypatch, stored=_current(), caps="old", foreign=[])
    asyncio.run(app.provision())
    assert written == [boundary.caps_sql(NAMES, app.caps())]


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
    monkeypatch: pytest.MonkeyPatch, ready: None, caplog: pytest.LogCaptureFixture
) -> None:
    written = _provisioning(monkeypatch, stored=None, caps="", foreign=["bi_scope ON memory"])
    asyncio.run(app.provision())
    assert written == []
    _named_in_the_log_only(caplog, app.analysis_state()[1], "bi_scope ON memory")


def test_a_foreign_policy_outlasts_a_clean_probe(
    monkeypatch: pytest.MonkeyPatch, ready: None, caplog: pytest.LogCaptureFixture
) -> None:
    # Such a policy widens another user, never the analyst, so the probe passes.
    monkeypatch.setattr(app, "_probe", _returns(("", False, "")))
    monkeypatch.setattr(app.store, "sealed_secrets", _returns(None))
    monkeypatch.setattr(app.store, "can_manage_access", _returns(True))
    monkeypatch.setattr(app.store, "other_deployments", _returns([]))
    monkeypatch.setattr(app.store, "foreign_policies", _returns(["bi_scope ON memory"]))
    asyncio.run(app.reprobe())
    assert app.analysis_state()[0] is False
    _named_in_the_log_only(caplog, app.analysis_state()[1], "bi_scope ON memory")


def test_another_loom_database_on_the_server_stops_the_write(
    monkeypatch: pytest.MonkeyPatch, ready: None, caplog: pytest.LogCaptureFixture
) -> None:
    written = _provisioning(monkeypatch, stored=None, caps="", foreign=[])
    monkeypatch.setattr(app.store, "other_deployments", _returns(["loom_staging"]))
    asyncio.run(app.provision())
    assert written == []
    _named_in_the_log_only(caplog, app.analysis_state()[1], "loom_staging")


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
    # The file is run in one session, whose first statement cuts the rest from the log.
    assert printed[0] == "SET log_queries_cut_to_length = 1, log_formatted_queries = 0"


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
    ("error", "advised", "own_fix"),
    [
        # A refusal Loom wrote names its own next step.
        (app.SetupRefused(app.NOT_MIGRATED), False, False),
        (RuntimeError("Code: 497. DB::Exception: Not enough privileges"), True, False),
        (RuntimeError(_CLOUD_PASSWORD_REFUSAL), False, True),
    ],
)
def test_ch_setups_analyst_line_advises_the_printed_sql_only_where_no_fix_fits(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    error: Exception,
    advised: bool,
    own_fix: bool,
) -> None:
    from server.cli import _ch, ch_setup

    async def fail() -> None:
        raise error

    monkeypatch.setattr(app, "rewrite", fail)
    monkeypatch.setattr(_ch, "run_connected", lambda fn, **_declared: asyncio.run(fn(None)))
    ch_setup._write_analyst()
    line = capsys.readouterr().out.strip()
    assert line.startswith("· loom_analyst (insights.query): not written (")
    assert ("--print-analyst-sql" in line) is advised
    assert ("DROP USER IF EXISTS loom_analyst" in line) is own_fix


def test_a_replica_whose_first_setup_could_not_run_answers_overloaded(
    monkeypatch: pytest.MonkeyPatch, ready: None
) -> None:
    from server.core import leases

    class _Busy:
        def __init__(self, *_: Any) -> None:
            pass

        async def __aenter__(self) -> None:
            raise leases.LeaseBusy("analysis.entities")

        async def __aexit__(self, *_: Any) -> None:
            return None

    monkeypatch.setattr(app, "_ready", None)
    monkeypatch.setattr(app, "_unreached_kind", "")
    monkeypatch.setattr(app, "lease", _Busy)
    asyncio.run(app.provision())
    assert app.analysis_state()[0] is None
    with pytest.raises(OverloadedError) as caught:
        asyncio.run(app.handle_insights_query(_query()))
    assert error_class(caught.value.code, caught.value.status_code) == ("capacity", True)
    assert caught.value.headers() == {"Retry-After": "5"}


def test_an_unavailable_answer_names_no_cause(monkeypatch: pytest.MonkeyPatch, ready: None) -> None:
    # Every org's caller gets this answer: the cause is for `loom doctor`.
    monkeypatch.setattr(app, "_ready", False)
    monkeypatch.setattr(app, "_problem", "another Loom database on this ClickHouse writes it")
    with pytest.raises(AnalysisUnavailableError) as caught:
        asyncio.run(app.handle_insights_query(_query()))
    assert "another Loom database" not in str(caught.value)


@pytest.mark.parametrize(
    ("answer", "expected", "repairable"),
    [
        (None, f"{boundary.ANALYST_USER} may read memory.value: its grants are too wide", True),
        (
            # An answer that is neither a refusal nor a passing fault.
            "Code: 60. DB::Exception: Unknown table",
            f"{boundary.ANALYST_USER} was not refused memory.value: Code 60",
            False,
        ),
    ],
)
def test_an_unrefused_probe_is_named_by_the_column_not_the_sql(
    monkeypatch: pytest.MonkeyPatch,
    ready: None,
    answer: str | None,
    expected: str,
    repairable: bool,
) -> None:
    # /v1/health shows this text: no SQL, and no database name.
    async def run(sql: str, **_: Any) -> Any:
        if sql.startswith("SELECT count()") and "system." not in sql:
            return [], [], [(1,)] if boundary.CANARY_TABLE in sql else [(0,)]
        if "system." in sql:
            raise RuntimeError("Code: 497. DB::Exception: Not enough privileges")
        if answer:
            raise RuntimeError(answer)
        return [], [], [("leaked",)]

    monkeypatch.setattr(app.store, "run_statement", run)
    assert asyncio.run(app._probe()) == (expected, repairable, answer or "")


class _FailingRead:
    def __init__(self, error: str) -> None:
        self.error = error

    async def query_columns(self, sql: str, params: Any = None) -> Any:
        raise RuntimeError(self.error)


def test_an_unknown_stamp_reads_as_no_version() -> None:
    read = app.store.own_version(NAMES, password="pw", extra={}, ch=_FailingRead("Code: 115."))
    assert asyncio.run(read) == 0


def test_a_failed_stamp_read_raises() -> None:
    # Read as 0, a timeout under a newer build's entities would run checks that
    # are that build's to make, and report a wrong cause instead of the failure.
    read = app.store.own_version(NAMES, password="pw", extra={}, ch=_FailingRead("Code: 159."))
    with pytest.raises(RuntimeError, match="159"):
        asyncio.run(read)


def test_each_cap_setting_reaches_the_caps_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.settings import settings

    for name, value in {
        "ch_analysis_max_execution_s": 7.5,
        "ch_analysis_max_rows_to_read": 1234,
        "ch_analysis_max_memory_bytes": 5678,
        "ch_analysis_max_result_rows": 9,
        "ch_analysis_max_result_bytes": 4321,
        "ch_analysis_max_concurrent": 3,
        "ch_analysis_max_threads": 2,
    }.items():
        monkeypatch.setattr(settings, name, value)
    profile = " ".join(boundary.caps_sql(NAMES, app.caps()))
    for pinned in (
        "max_execution_time = 7.5 MIN 0.1 MAX 7.5",
        "max_rows_to_read = 1234 MIN 1 MAX 1234",
        "max_memory_usage = 5678 MIN 1 MAX 5678",
        "max_result_rows = 10 MIN 1 MAX 10",
        "max_result_bytes = 4321 MIN 1 MAX 4321",
        "max_concurrent_queries_for_user = 3 CONST",
        "max_threads = 2 MIN 1 MAX 2",
    ):
        assert pinned in profile, pinned


def test_the_per_org_cap_setting_bounds_one_orgs_statements(
    monkeypatch: pytest.MonkeyPatch, ready: None
) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "ch_analysis_max_concurrent_per_org", 1)
    monkeypatch.setattr(app, "_running", {"acme": 1})
    with pytest.raises(RateLimitedError):
        asyncio.run(app.handle_insights_query(_query()))


def test_the_probe_loop_waits_ch_analysis_probe_interval_s(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.settings import settings

    waits: list[float] = []

    async def sleep(seconds: float) -> None:
        waits.append(seconds)
        raise asyncio.CancelledError

    monkeypatch.setattr(settings, "ch_analysis_probe_interval_s", 17.0)
    monkeypatch.setattr(app, "provision", _returns(None))
    monkeypatch.setattr(app.asyncio, "sleep", sleep)

    async def run() -> None:
        with pytest.raises(asyncio.CancelledError):
            await app.start_probe_loop()

    asyncio.run(run())
    assert waits == [17.0]


def test_a_scope_outlives_every_attempt_of_one_statement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A retry past the scope's expiry would read no rows rather than fail.
    from server.settings import settings

    monkeypatch.setattr(settings, "ch_analysis_max_execution_s", 120.0)
    monkeypatch.setattr(settings, "ch_replica_bound_retries", 2)
    # The first attempt and two replica-bound retries: the last starts after
    # two waits of the cap and five seconds.
    assert app.scope_ttl_s() > (120 + 5) * 2


def test_statements_under_the_analysis_lease_are_bounded_inside_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import contextlib

    from server.core import ch, leases

    @contextlib.asynccontextmanager
    async def held(*_: Any, **__: Any) -> Any:
        yield None

    monkeypatch.setattr(app.leases, "cluster_lease", held)

    async def run() -> float:
        async with app.lease():
            return ch.statement_timeout_var.get()

    assert asyncio.run(run()) == leases.statement_bound(60)


def test_a_statement_and_its_retries_carry_the_requests_query_id(ready: None) -> None:
    # The retry ladder rebuilds the settings for each attempt from the same
    # extra: every attempt sends the request's id and replaces a first attempt
    # ClickHouse still runs (code 216 otherwise).
    import contextvars

    from server.core import ch, request_work

    def in_a_request() -> tuple[list[dict[str, Any]], str]:
        request_work.begin()
        ch.query_tag_var.set("insights.query rid-1")
        extra = app._scope_extra("acme", ["ns"])
        with ch.as_analyst(boundary.ANALYST_USER, "pw", extra):
            attempts = [ch.statement_settings("SELECT 1") for _ in range(2)]
        return attempts, app._scope_extra("acme", ["ns"])["query_id"]

    # A copied context: the request and its tag end with the call.
    attempts, next_id = contextvars.copy_context().run(in_a_request)
    ids = {a["query_id"] for a in attempts}
    assert len(ids) == 1 and next(iter(ids)).startswith("rid-1:")
    assert all(a["replace_running_query"] == 1 for a in attempts)
    assert next_id not in ids  # the next statement gets its own


@pytest.mark.parametrize(
    ("state", "category", "retryable"),
    [(None, "capacity", True), (False, "configuration", False)],
)
def test_a_replica_still_starting_is_retried_and_a_failed_probe_is_not(
    monkeypatch: pytest.MonkeyPatch,
    ready: None,
    state: bool | None,
    category: str,
    retryable: bool,
) -> None:
    # A first probe still to run passes; only an operator ends a failed one.
    monkeypatch.setattr(app, "_ready", state)
    with pytest.raises(LoomServerError) as caught:
        asyncio.run(app.handle_insights_query(_query()))
    assert error_class(caught.value.code, caught.value.status_code) == (category, retryable)


def test_a_finding_whose_repair_fails_is_kept(monkeypatch: pytest.MonkeyPatch, ready: None) -> None:
    # A grant outside the allowlist that the write cannot revoke: the replica
    # must stop answering, not pass for want of a write.
    async def write() -> tuple[str, str]:
        raise RuntimeError("Code: 497. DB::Exception: Not enough privileges to revoke")

    monkeypatch.setattr(app.store, "sealed_secrets", _returns(None))
    monkeypatch.setattr(
        app, "_probe", _returns(("loom_analyst holds grants outside the allowlist", True, "g"))
    )
    monkeypatch.setattr(app.store, "can_manage_access", _returns(True))
    monkeypatch.setattr(app, "_write_under_lease", write)
    asyncio.run(app.reprobe())
    ready_now, problem, _ = app.analysis_state()
    assert ready_now is False and problem == "loom_analyst holds grants outside the allowlist"


def _clean_ran(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app.store, "sealed_secrets", _returns(None))
    monkeypatch.setattr(app.store, "can_manage_access", _returns(False))
    monkeypatch.setattr(app, "_unreached_kind", "")
    monkeypatch.setattr(app, "_unreached_since", 0.0)


def test_a_probe_with_no_answer_stops_serving_but_is_resent(
    monkeypatch: pytest.MonkeyPatch, ready: None, caplog: pytest.LogCaptureFixture
) -> None:
    # No answer is no pass: the replica answers OVERLOADED, which a client resends.
    _clean_ran(monkeypatch)
    monkeypatch.setattr(app, "_probe", _failing_probe(TimeoutError("read timed out")))
    asyncio.run(app.reprobe())
    asyncio.run(app.reprobe())
    assert app.analysis_state() == (None, "", "")
    assert sum("got no answer" in r.message for r in caplog.records) == 1  # once per kind
    with pytest.raises(OverloadedError):
        asyncio.run(app.handle_insights_query(_query()))


def test_no_answer_for_three_intervals_is_named_but_still_resent(
    monkeypatch: pytest.MonkeyPatch, ready: None
) -> None:
    # An outage or a busy analyst user is no operator's fix: the replica keeps
    # answering OVERLOADED, and health names it once it lasts.
    from server.settings import settings

    _clean_ran(monkeypatch)
    monkeypatch.setattr(settings, "ch_analysis_probe_interval_s", 5.0)
    monkeypatch.setattr(app, "_probe", _failing_probe(TimeoutError("read timed out")))
    now = [100.0]

    def monotonic() -> float:
        now[0] += 10.0  # each reading a probe interval and more later
        return now[0]

    monkeypatch.setattr(app.time, "monotonic", monotonic)
    for _ in range(3):
        asyncio.run(app.reprobe())
    ready_now, problem, _ = app.analysis_state()
    assert ready_now is None and "no answer for three intervals" in problem
    with pytest.raises(OverloadedError):
        asyncio.run(app.handle_insights_query(_query()))


def test_a_proxys_own_status_is_no_answer(monkeypatch: pytest.MonkeyPatch, ready: None) -> None:
    # A balancer's 503 carries no ClickHouse code: it says nothing about the analyst.
    from clickhouse_connect.driver.exceptions import OperationalError

    _clean_ran(monkeypatch)

    async def run(*_: Any, **__: Any) -> Any:
        raise OperationalError("HTTP driver received HTTP status 503, server response: busy")

    monkeypatch.setattr(app.store, "run_statement", run)
    asyncio.run(app.reprobe())
    assert app.analysis_state() == (None, "", "")


def test_a_refusal_loom_wrote_during_a_repair_is_the_problem(
    monkeypatch: pytest.MonkeyPatch, ready: None
) -> None:
    # A replica with no secrets loaded repairs by loading them; a row that does
    # not open answers with Loom's own text, which names the fix.
    _clean_ran(monkeypatch)
    monkeypatch.setattr(app, "_loaded", None)
    monkeypatch.setattr(app, "lease", _NoLease)
    monkeypatch.setattr(
        app, "_probe", _returns(("this replica holds no analyst secrets", True, ""))
    )
    monkeypatch.setattr(app.store, "can_manage_access", _returns(True))
    monkeypatch.setattr(app.store, "analyst_user_absent", _returns(False))
    monkeypatch.setattr(app.store, "sealed_secrets", _returns("not-a-sealed-value"))
    monkeypatch.setattr(app, "_opened", lambda _: None)
    asyncio.run(app.reprobe())
    ready_now, problem, _ = app.analysis_state()
    assert ready_now is False and "does not open with LOOM_JWT_SECRET" in problem


def test_a_first_boot_with_no_answer_about_the_user_is_no_finding(
    monkeypatch: pytest.MonkeyPatch, ready: None
) -> None:
    # Declining to draw on a guess is right; calling it a finding is not.
    monkeypatch.setattr(app, "_ready", None)
    monkeypatch.setattr(app, "_unreached_kind", "")
    monkeypatch.setattr(app, "_unreached_since", 0.0)
    monkeypatch.setattr(app, "lease", _NoLease)

    class _Ch:
        async def query_one(self, *_: Any, **__: Any) -> Any:
            raise TimeoutError("read timed out")

    monkeypatch.setattr(app.store, "_default_client", lambda: _Ch())
    asyncio.run(app.provision())
    assert app.analysis_state() == (None, "", "")


@pytest.mark.parametrize(
    ("error", "cause"),
    [
        (
            "Code: 159. DB::Exception: Timeout exceeded. (TIMEOUT_EXCEEDED) (version 26.10.2.93)",
            "with Code 159 (TIMEOUT_EXCEEDED):",
        ),
        ("Code: 158. DB::Exception: Limit for rows to read exceeded", "with Code 158:"),
        ("Code: 241. DB::Exception: Memory limit (for query) exceeded", "with Code 241:"),
    ],
)
def test_a_capped_reserved_scope_count_is_a_finding(
    monkeypatch: pytest.MonkeyPatch, ready: None, error: str, cause: str
) -> None:
    # A count that hits a cap was not narrowed to nothing by a scope policy.
    async def run(sql: str, **_: Any) -> Any:
        if boundary.CANARY_TABLE in sql:
            return [], [], [(1,)]
        raise RuntimeError(error)

    monkeypatch.setattr(app.store, "run_statement", run)
    problem, repairable, _ = asyncio.run(app._probe())
    assert f"count in the reserved scope failed {cause}" in problem and repairable


def test_a_canary_read_with_no_answer_raises(monkeypatch: pytest.MonkeyPatch, ready: None) -> None:
    monkeypatch.setattr(app.store, "run_statement", _failing("Code: 210. Connection refused"))
    with pytest.raises(RuntimeError):
        asyncio.run(app._probe())


def test_a_manage_check_with_no_answer_does_not_pass_a_replica(
    monkeypatch: pytest.MonkeyPatch, ready: None
) -> None:
    # A foreign-policy finding must not clear because the access check timed out.
    monkeypatch.setattr(app, "_ready", False)
    monkeypatch.setattr(app, "_problem", "row policies Loom did not write are on the tables")
    _clean_ran(monkeypatch)
    monkeypatch.setattr(app, "_probe", _returns(("", False, "")))

    async def manage() -> bool:
        raise TimeoutError("read timed out")

    monkeypatch.setattr(app.store, "can_manage_access", manage)
    asyncio.run(app.reprobe())
    assert app.analysis_state()[0] is not True


@pytest.mark.parametrize(
    ("error", "ready_after"),
    [(TimeoutError("read timed out"), None), (RuntimeError("Code: 47. Unknown identifier"), False)],
)
def test_a_failed_foreign_policy_check_after_a_clean_probe(
    monkeypatch: pytest.MonkeyPatch, ready: None, error: Exception, ready_after: bool | None
) -> None:
    _clean_ran(monkeypatch)
    monkeypatch.setattr(app, "_probe", _returns(("", False, "")))
    monkeypatch.setattr(app.store, "can_manage_access", _returns(True))

    async def foreign() -> tuple[str, str]:
        raise error

    monkeypatch.setattr(app, "_foreign_problem", foreign)
    asyncio.run(app.reprobe())
    assert app.analysis_state()[0] is ready_after


def test_a_recheck_that_does_not_finish_after_a_repair_keeps_the_finding(
    monkeypatch: pytest.MonkeyPatch, ready: None, caplog: pytest.LogCaptureFixture
) -> None:
    _clean_ran(monkeypatch)
    calls = iter(
        [("memory admits rows in the reserved scope", True, ""), TimeoutError("timed out")]
    )

    async def probe() -> Any:
        answer = next(calls)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(app, "_probe", probe)
    monkeypatch.setattr(app.store, "can_manage_access", _returns(True))
    monkeypatch.setattr(app, "_write_under_lease", _returns(("", "")))
    asyncio.run(app.reprobe())
    ready_now, problem, _ = app.analysis_state()
    assert ready_now is False and problem == "memory admits rows in the reserved scope"
    assert any("re-check did not finish" in r.message for r in caplog.records)


def _failing_probe(error: Exception):
    async def probe() -> Any:
        raise error

    return probe


def test_ch_setup_without_access_management_says_so(
    monkeypatch: pytest.MonkeyPatch, ready: None
) -> None:
    # It cannot tell whether the analyst user exists, so it names that, not a lost row.
    monkeypatch.setattr(app, "lease", _NoLease)
    monkeypatch.setattr(app.store, "secrets_table_exists", _returns(True))
    monkeypatch.setattr(app.store, "can_manage_access", _returns(False))
    with pytest.raises(app.SetupRefused, match="cannot manage access entities"):
        asyncio.run(app.rewrite())


def test_the_setup_runs_again_until_its_write_step_finishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Only the setup writes a new version's entities and changed caps.
    from server.settings import settings

    answers = iter([False, False, True])
    calls: list[str] = []

    async def provision() -> bool:
        calls.append("provision")
        return next(answers)

    async def reprobe() -> None:
        calls.append("reprobe")

    async def sleep(_: float) -> None:
        if len(calls) >= 4:
            raise asyncio.CancelledError

    monkeypatch.setattr(settings, "ch_analysis_probe_interval_s", 5.0)
    monkeypatch.setattr(app, "provision", provision)
    monkeypatch.setattr(app, "reprobe", reprobe)
    monkeypatch.setattr(app.asyncio, "sleep", sleep)

    async def run() -> None:
        with pytest.raises(asyncio.CancelledError):
            await app.start_probe_loop()

    asyncio.run(run())
    assert calls == ["provision", "provision", "provision", "reprobe"]


def test_a_serving_replica_whose_probe_gets_no_answer_says_so(
    monkeypatch: pytest.MonkeyPatch, ready: None
) -> None:
    _clean_ran(monkeypatch)
    monkeypatch.setattr(app, "_probe", _failing_probe(TimeoutError("read timed out")))
    asyncio.run(app.reprobe())
    with pytest.raises(OverloadedError, match="no answer from ClickHouse"):
        asyncio.run(app.handle_insights_query(_query()))


def test_provision_is_done_only_once_its_write_step_finished(
    monkeypatch: pytest.MonkeyPatch, ready: None
) -> None:
    # A foreign policy stops the write: the setup runs again and, with the
    # policy dropped, writes this build's entities.
    foreign: list[str] = ["bi_scope ON memory"]
    written = _provisioning(monkeypatch, stored=None, caps="", foreign=foreign)
    assert asyncio.run(app.provision()) is False
    assert written == []
    foreign.clear()
    assert asyncio.run(app.provision()) is True
    assert written == [app.entity_statements()]


def test_provision_with_no_answer_is_not_done(monkeypatch: pytest.MonkeyPatch, ready: None) -> None:
    _provisioning(monkeypatch, stored=None, caps="", foreign=[])

    async def no_answer(*_: Any, **__: Any) -> Any:
        raise TimeoutError("read timed out")

    monkeypatch.setattr(app.store, "stored_entities", no_answer)
    monkeypatch.setattr(app, "_unreached_kind", "")
    monkeypatch.setattr(app, "_unreached_since", 0.0)
    assert asyncio.run(app.provision()) is False


def test_a_write_clickhouse_refuses_is_a_kept_finding_and_not_done(
    monkeypatch: pytest.MonkeyPatch, ready: None
) -> None:
    _provisioning(monkeypatch, stored=None, caps="", foreign=[])

    async def refuse(*_: Any, **__: Any) -> None:
        raise RuntimeError("Code: 497. DB::Exception: Not enough privileges")

    monkeypatch.setattr(app.store, "write_entities", refuse)
    for _ in range(2):  # every run records the same finding
        assert asyncio.run(app.provision()) is False
        ready_now, problem, _ = app.analysis_state()
        assert ready_now is False and problem.startswith("writing or probing loom_analyst failed")


@pytest.mark.parametrize(
    ("raised", "shown", "fix_names"),
    [
        (
            _CLOUD_PASSWORD_REFUSAL,
            "Code 36 (BAD_ARGUMENTS): the password does not meet the server's password policy",
            "`DROP USER IF EXISTS loom_analyst`",
        ),
        (
            "Code: 497. DB::Exception: loom: Not enough privileges. (ACCESS_DENIED) (version 1)",
            "Code 497 (ACCESS_DENIED): a privilege the statement needs is missing",
            "access management",
        ),
        (
            "Code: 180. DB::Exception: There is no settings profile `loom_analyst_caps` in "
            "`user directories`. (THERE_IS_NO_PROFILE) (version 1)",
            "Code 180 (THERE_IS_NO_PROFILE)",
            "server log",
        ),
        (
            "Code: 36. DB::Exception: Unknown format. (BAD_ARGUMENTS) (version 1)",
            "Code 36 (BAD_ARGUMENTS)",
            "server log",
        ),
        ("unexpected answer", "RuntimeError", "server log"),
    ],
)
def test_a_refused_write_names_clickhouse_code_and_its_fix(
    monkeypatch: pytest.MonkeyPatch, ready: None, raised: str, shown: str, fix_names: str
) -> None:
    _provisioning(monkeypatch, stored=None, caps="", foreign=[])

    async def refuse(*_: Any, **__: Any) -> None:
        raise RuntimeError(raised)

    monkeypatch.setattr(app.store, "write_entities", refuse)
    assert asyncio.run(app.provision()) is False
    ready_now, problem, fix = app.analysis_state()
    assert (ready_now, problem) == (False, f"writing or probing loom_analyst failed: {shown}")
    state = problems.HealthState(analysis_ok=ready_now, analysis_error=problem, analysis_fix=fix)
    (shown_problem,) = problems._analysis_problems(None, state)
    assert fix_names in shown_problem.fix


def test_a_later_finding_drops_the_fix_of_the_one_it_replaces(
    monkeypatch: pytest.MonkeyPatch, ready: None
) -> None:
    _provisioning(monkeypatch, stored=None, caps="", foreign=[])

    async def refuse(*_: Any, **__: Any) -> None:
        raise RuntimeError(_CLOUD_PASSWORD_REFUSAL)

    monkeypatch.setattr(app.store, "write_entities", refuse)
    asyncio.run(app.provision())
    assert app.analysis_state()[2]
    monkeypatch.setattr(app.store, "foreign_policies", _returns(["bi_scope ON memory"]))
    asyncio.run(app.provision())
    _, problem, fix = app.analysis_state()
    assert problem.startswith("row policies Loom did not write") and fix == ""


def test_a_drawn_password_has_every_character_class() -> None:
    # ClickHouse Cloud refuses a user whose password lacks one.
    password = app._drawn_password()
    for character_class in ("[a-z]", "[A-Z]", "[0-9]", "[^A-Za-z0-9]"):
        assert re.search(character_class, password), character_class


def test_the_prefix_is_probed_once_per_process(monkeypatch: pytest.MonkeyPatch) -> None:
    # A refusal after the probe, here an unopenable row, must not probe again.
    probes = []

    async def probe() -> str:
        probes.append(1)
        return "custom_"

    monkeypatch.setattr(app.store, "setting_prefix", probe)
    monkeypatch.setattr(app.store, "sealed_secrets", _returns("not-a-sealed-value"))
    for _ in range(2):
        with pytest.raises(app.SetupRefused):
            asyncio.run(app.load_secrets(may_draw=False))
    assert probes == [1] and app._names() == boundary.SettingNames("custom_")


def test_a_server_that_takes_neither_prefix_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app.store, "setting_prefix", _returns(None))
    with pytest.raises(app.SetupRefused, match="custom_settings_prefixes"):
        asyncio.run(app.load_secrets(may_draw=True))
