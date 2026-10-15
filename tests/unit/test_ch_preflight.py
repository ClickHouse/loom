"""The ClickHouse preflight (server/core/schema/preflight.py) and `loom
ch-setup`, against a scripted client: no ClickHouse needed."""

from __future__ import annotations

import argparse
import dataclasses
from types import SimpleNamespace
from typing import Any

import pytest

from server.cli import ch_setup
from server.core.schema import preflight

# The real one, for the tests of it; the fixture below stands in for it elsewhere.
_real_migrations_pending = preflight._migrations_pending


@pytest.fixture(autouse=True)
def _migrations_pending(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every check here is about to migrate unless a test says otherwise."""
    monkeypatch.setattr(preflight, "_migrations_pending", lambda ch, journal: True)


class FakeCH:
    """Answers the preflight's statements from a dict; records the rest."""

    def __init__(
        self, *, replicas=("26.5.1.882",), database=True, grants=1, denied=(), replica_error=""
    ):
        self.replicas = replicas
        self.replica_error = replica_error
        self.database = database
        self.grants = grants
        self.denied = set(denied)
        self.ran: list[str] = []

    def query(self, sql: str, **_: Any) -> SimpleNamespace:
        if "clusterAllReplicas" in sql:
            if self.replica_error:
                raise RuntimeError(self.replica_error)
            if self.replicas is None:
                raise RuntimeError("Code: 701. Requested cluster 'default' not found")
            return SimpleNamespace(result_rows=[(v,) for v in self.replicas])
        return SimpleNamespace(result_rows=[])

    def command(self, sql: str, **_: Any) -> Any:
        self.ran.append(sql)
        if any(sql.startswith(prefix) for prefix in self.denied):
            raise RuntimeError("Code: 497. DB::Exception: Not enough privileges. (ACCESS_DENIED)")
        if sql == "SELECT version()":
            return "26.5.1.882"
        if "system.databases" in sql:
            return int(self.database)
        if sql.startswith("CHECK GRANT"):
            return self.grants
        if "system.tables" in sql:
            return 1
        if "enable_lightweight_update" in sql:
            return "1"
        return None


def test_a_ready_server_passes_and_reports_what_it_has() -> None:
    server = preflight.check(FakeCH(), need="serve")
    assert server == preflight.Server("26.5.1.882", keeper=True, lightweight_updates=True)


def test_the_oldest_replica_decides_the_version() -> None:
    with pytest.raises(preflight.PreflightError, match=r"26\.3\.1\.1 is older than 26\.4"):
        preflight.check(FakeCH(replicas=("26.6.1.1", "26.3.1.1")), need="migrate")


@pytest.mark.parametrize(
    ("error", "needs_admin"),
    [
        ("Code: 497. DB::Exception: Not enough privileges. (ACCESS_DENIED)", True),
        ("Code: 210. DB::Exception: Connection refused (NETWORK_ERROR)", False),
    ],
)
def test_replicas_that_cannot_be_asked_are_not_taken_for_one_server(
    error: str, needs_admin: bool
) -> None:
    with pytest.raises(preflight.PreflightError) as e:
        preflight.oldest_version(FakeCH(replica_error=error))
    assert e.value.needs_admin is needs_admin


def test_a_serving_process_falls_back_when_the_replicas_cannot_be_asked() -> None:
    ch = FakeCH(replica_error="Code: 279. DB::Exception: All connection tries failed")
    assert preflight.check(ch, need="serve").version == "26.5.1.882"
    runner = {f"SELECT * FROM system.{t} LIMIT 0" for t in preflight.RUNNER_SYSTEM_TABLES}
    assert not runner & set(ch.ran)


def test_pending_comes_from_the_journal_the_command_runs(tmp_path) -> None:
    class Ledger(FakeCH):
        def query(self, sql: str, **kwargs: Any) -> SimpleNamespace:
            if "_migrations" in sql:
                return SimpleNamespace(result_rows=[("000_migrations.sql", "x")])
            return super().query(sql, **kwargs)

    (tmp_path / "000_migrations.sql").write_text("SELECT 1")
    assert not _real_migrations_pending(Ledger(), tmp_path)
    (tmp_path / "001_custom.sql").write_text("SELECT 1")
    assert _real_migrations_pending(Ledger(), tmp_path)


def test_an_unreadable_ledger_counts_as_pending(tmp_path) -> None:
    class Refused(FakeCH):
        def query(self, sql: str, **kwargs: Any) -> SimpleNamespace:
            if "_migrations" in sql:
                raise RuntimeError("Code: 497. DB::Exception: Not enough privileges")
            return super().query(sql, **kwargs)

    (tmp_path / "000_migrations.sql").write_text("SELECT 1")
    assert _real_migrations_pending(Refused(), tmp_path)


def test_no_migration_files_to_compare_counts_as_pending(tmp_path) -> None:
    assert _real_migrations_pending(FakeCH(), tmp_path / "not-shipped")


def test_a_restart_with_nothing_pending_is_not_strict(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(preflight, "_migrations_pending", lambda ch, journal: False)
    ch = FakeCH(replica_error="Code: 497. DB::Exception: Not enough privileges. (ACCESS_DENIED)")
    server = preflight.check(ch, need="migrate")
    assert server.version == "26.5.1.882"
    assert "clusterAllReplicas" in server.next_migration[0]


def test_what_the_next_migration_needs_is_reported_while_nothing_is_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(preflight, "_migrations_pending", lambda ch, journal: False)
    ch = FakeCH(denied=("SELECT * FROM system.mutations",))
    (gap,) = preflight.check(ch, need="migrate").next_migration
    assert "system.mutations" in gap
    assert preflight.check(ch, need="serve").next_migration == ()


def test_the_doctor_warns_of_what_the_next_migration_will_refuse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.cli import doctor

    gap = "'loom' may not read system.mutations, which the migrations need"
    server = preflight.Server("26.5.1.882", keeper=True, lightweight_updates=True)
    monkeypatch.setattr(preflight, "needed_at_boot", lambda: "migrate")
    monkeypatch.setattr(
        preflight, "require_ready", lambda **_: dataclasses.replace(server, next_migration=(gap,))
    )
    name, grade, detail = doctor._doctor_preflight_check()
    assert (name, grade) == ("clickhouse_preflight", doctor.WARN)
    assert "next boot with a migration" in detail and gap in detail


def test_a_refused_telemetry_table_is_a_warning_not_a_stop() -> None:
    ch = FakeCH(denied=("SELECT * FROM system.query_log",))
    assert preflight.check(ch, need="migrate").unreadable == ("query_log",)


def test_every_system_table_loom_reads_is_checked_before_any_ddl() -> None:
    ch = FakeCH(denied=("SELECT * FROM system.merge_tree_settings",))
    with pytest.raises(preflight.PreflightError) as e:
        preflight.check(ch, need="migrate")
    assert "system.merge_tree_settings" in str(e.value) and e.value.needs_admin
    assert not any(sql.startswith("CREATE") for sql in ch.ran)


def test_the_reader_profile_is_compared_with_its_constraints() -> None:
    profile = ch_setup._profile()
    assert profile["max_execution_time"] == ("30", 0.05, 30.0, False)
    assert profile["read_overflow_mode"] == ("'throw'", None, None, True)
    # What system.settings shows a reader whose profile lost its MIN/MAX.
    assert ch_setup._setting("30", None, None, 0) != profile["max_execution_time"]
    assert ch_setup._setting("30", "0.05", "30", 0) == profile["max_execution_time"]
    assert ch_setup._setting("throw", None, None, 0) != profile["read_overflow_mode"]


def test_a_server_without_the_default_cluster_answers_for_itself() -> None:
    assert preflight.oldest_version(FakeCH(replicas=None)) == "26.5.1.882"


def test_a_missing_database_is_created_only_when_asked() -> None:
    ch = FakeCH(database=False)
    preflight.check(ch, need="migrate")
    assert "CREATE DATABASE IF NOT EXISTS loom" in ch.ran
    with pytest.raises(preflight.PreflightError, match=r"missing, or no grant on it"):
        preflight.check(FakeCH(database=False), need="serve")


def test_the_admin_pointer_is_in_the_message_but_not_the_reason() -> None:
    e = preflight.PreflightError("an admin has to run Loom's setup SQL", needs_admin=True)
    assert "`loom ch-setup --print-sql` prints it" in str(e)
    assert "--print-sql" not in e.reason


def test_a_disabled_cap_is_written_uncapped(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "ch_caller_shaped_max_rows_to_read", 0)
    profile = ch_setup.reader_sql("loom_reader", "'pw'")[0]
    assert "max_rows_to_read = 0," in profile and "max_rows_to_read = 0 MIN" not in profile


def test_a_user_who_may_not_create_the_database_needs_an_admin() -> None:
    ch = FakeCH(database=False, denied=("CREATE DATABASE",))
    with pytest.raises(preflight.PreflightError) as e:
        preflight.check(ch, need="migrate")
    assert e.value.needs_admin and "cannot see a database loom and may not create it" in str(
        e.value
    )


def test_missing_grants_name_the_grant_to_run() -> None:
    with pytest.raises(preflight.PreflightError) as e:
        preflight.check(FakeCH(grants=0), need="migrate")
    assert e.value.needs_admin and "GRANT CURRENT GRANTS ON loom.* TO `loom`" in str(e.value)


def test_a_process_that_does_not_migrate_needs_only_the_serving_grants() -> None:
    ch = FakeCH()
    preflight.check(ch, need="serve")
    assert f"CHECK GRANT {preflight.GRANTS['serve']} ON loom.*" in ch.ran
    assert not any(preflight.GRANTS["migrate"] in sql for sql in ch.ran)


def test_the_migration_grants_never_name_the_global_alter_group() -> None:
    # 26.9 put the global-only ALTER HANDLER in ALTER: a grant on loom.* never holds it.
    assert "ALTER," not in preflight.GRANTS["migrate"] + ","
    assert "ALTER TABLE" in preflight.GRANTS["migrate"]


def test_status_reads_need_only_select() -> None:
    ch = FakeCH()
    preflight.check(ch, need="read")
    assert "CHECK GRANT SELECT ON loom.*" in ch.ran


@pytest.mark.parametrize(
    ("boot_migrate", "read_replica", "need"),
    [
        (True, False, "migrate"),
        (False, False, "serve"),
        (True, True, "serve"),
    ],
)
def test_boot_and_doctor_need_the_same(
    monkeypatch: pytest.MonkeyPatch, boot_migrate: bool, read_replica: bool, need: str
) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "boot_migrate", boot_migrate)
    monkeypatch.setattr(settings, "read_replica", read_replica)
    assert preflight.needed_at_boot() == need


def test_a_url_in_ch_host_is_refused_before_connecting(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "ch_host", "https://abc.clickhouse.cloud")
    with pytest.raises(preflight.PreflightError, match="is not a host name"):
        preflight.connect()


def test_wrong_credentials_say_which_settings_to_check(monkeypatch: pytest.MonkeyPatch) -> None:
    import clickhouse_connect

    from server.settings import settings

    monkeypatch.setattr(settings, "ch_host", "ch.example")

    def refuse(**_: Any) -> None:
        raise RuntimeError("Code: 516. DB::Exception: default: Authentication failed")

    monkeypatch.setattr(clickhouse_connect, "get_client", refuse)
    with pytest.raises(preflight.PreflightError, match="check LOOM_CH_USER and LOOM_CH_PASSWORD"):
        preflight.connect()


def test_an_unreachable_server_names_the_socket_error(monkeypatch: pytest.MonkeyPatch) -> None:
    import clickhouse_connect

    from server.settings import settings

    monkeypatch.setattr(settings, "ch_host", "ch.example")

    def refuse(**_: Any) -> None:
        raise RuntimeError(
            "Error HTTPConnectionPool(host='ch.example', port=8123): Max retries exceeded "
            '(Caused by NewConnectionError("...: [Errno 111] Connection refused"))'
        )

    monkeypatch.setattr(clickhouse_connect, "get_client", refuse)
    with pytest.raises(
        preflight.PreflightError, match=r"8123 \(plain HTTP\): Connection refused; check"
    ):
        preflight.connect()


def test_boot_stops_on_a_failed_preflight(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    from server import boot

    def fail(**_: Any) -> None:
        raise preflight.PreflightError("ClickHouse 25.8.1.1 is older than 26.4: upgrade the server")

    monkeypatch.setattr(preflight, "require_ready", fail)
    with pytest.raises(RuntimeError, match="ClickHouse preflight: ClickHouse 25.8.1.1 is older"):
        asyncio.run(boot.require_clickhouse_ready())


def test_the_reader_profile_pins_the_caps_with_floors() -> None:
    profile, user, grant = ch_setup.reader_sql("loom_reader", "'pw'")
    assert profile.startswith(
        "CREATE SETTINGS PROFILE OR REPLACE `loom_reader` SETTINGS readonly = 2"
    )
    assert "max_execution_time = 30 MIN 0.05 MAX 30" in profile
    assert "max_rows_to_read = 500000000 MIN 1 MAX 500000000" in profile
    assert "read_overflow_mode = 'throw' CONST" in profile
    assert user.endswith("IDENTIFIED BY 'pw' SETTINGS PROFILE 'loom_reader'")
    assert grant == "GRANT SELECT ON loom.* TO `loom_reader`"


def test_printed_sql_never_carries_the_password(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "ch_user", "loom_app")
    monkeypatch.setattr(settings, "ch_password", "s3cret-value")
    import server.cli as cli

    assert cli.main(["ch-setup", "--print-sql"]) == 0
    out = capsys.readouterr().out
    assert "s3cret-value" not in out
    assert "IDENTIFIED BY '<LOOM_CH_PASSWORD>'" in out
    assert "GRANT CURRENT GRANTS ON loom.* TO `loom_app`;" in out


def test_a_password_is_quoted_as_a_literal() -> None:
    assert ch_setup._quote("it's\\x") == "'it\\'s\\\\x'"


def test_the_reader_must_not_be_the_loom_user(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "ch_user", "loom")
    monkeypatch.setattr(settings, "ch_reader_user", "loom")
    assert ch_setup._cmd_ch_setup(argparse.Namespace(print_sql=True)) == 2


@pytest.mark.parametrize(
    ("found", "problem", "refusal"),
    [
        # A same-named user that does not log in with Loom's password: an analyst's account.
        ("user", ch_setup.CANNOT_LOG_IN, "does not accept LOOM_CH_PASSWORD"),
        # A same-named profile with no reader user, or one other accounts inherit.
        ("profile", ch_setup.CANNOT_LOG_IN, "a settings profile named analyst exists"),
    ],
)
def test_an_account_ch_setup_cannot_prove_is_loom_s_is_left_alone(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    found: str,
    problem: str,
    refusal: str,
) -> None:
    monkeypatch.setattr(ch_setup, "reader_problem", lambda reader: problem)
    monkeypatch.setattr(ch_setup, "reader_objects", lambda ch, reader: found)
    ch = FakeCH()
    assert ch_setup._set_up(ch, "loom_app", "analyst") == 1
    assert refusal in capsys.readouterr().err
    assert not any(sql.startswith(("CREATE USER", "CREATE SETTINGS")) for sql in ch.ran)


def test_a_reader_that_logs_in_with_loom_s_password_is_rebuilt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ch_setup, "reader_problem", lambda r: "its profile does not match")
    monkeypatch.setattr(ch_setup, "reader_objects", lambda ch, r: "user")
    ch = FakeCH()
    assert ch_setup._set_up(ch, "loom", "loom_reader") == 0
    assert any(sql.startswith("CREATE USER OR REPLACE `loom_reader`") for sql in ch.ran)


def test_a_bundled_reader_whose_caps_differ_is_a_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        ch_setup, "reader_problem", lambda r: "its profile does not match the current caps"
    )
    monkeypatch.setattr(ch_setup, "reader_objects", lambda ch, r: "config")
    assert ch_setup._set_up(FakeCH(), "loom", "loom_reader") == 1
    assert (
        "raise its profile in deploy/clickhouse-config/nlp_user_settings.xml"
        in capsys.readouterr().err
    )


def test_a_reader_whose_owner_cannot_be_seen_is_not_replaced(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class MayNotListUsers(FakeCH):
        def query(self, sql: str, **kwargs: Any) -> SimpleNamespace:
            if "system.users" in sql:
                raise RuntimeError("Code: 497. DB::Exception: Not enough privileges")
            return super().query(sql, **kwargs)

    monkeypatch.setattr(ch_setup, "reader_problem", lambda reader: "missing")
    ch = MayNotListUsers()
    assert ch_setup.reader_objects(ch, "someone_elses") == "unknown"
    assert ch_setup._set_up(ch, "delegate", "someone_elses") == 1
    assert not any(sql.startswith("CREATE USER") for sql in ch.ran)
    captured = capsys.readouterr()
    assert "cannot tell who owns someone_elses" in captured.err
    assert "CREATE USER OR REPLACE `someone_elses`" in captured.err  # for an admin to judge


def test_a_user_who_may_not_create_the_reader_gets_the_admin_sql(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "ch_password", "s3cret-value")
    monkeypatch.setattr(ch_setup, "reader_problem", lambda reader: "missing")
    ch = FakeCH(denied=("CREATE SETTINGS PROFILE",))
    assert ch_setup._set_up(ch, "loom_app", "loom_reader") == 1
    captured = capsys.readouterr()
    assert "may not create users or settings profiles" in captured.err
    assert "CREATE USER OR REPLACE `loom_reader`" in captured.err
    assert "s3cret-value" not in captured.out + captured.err


def test_the_doctor_fails_where_boot_could_not_create_the_database() -> None:
    class NoCreate(FakeCH):
        def command(self, sql: str) -> Any:
            if sql.startswith("CHECK GRANT CREATE DATABASE"):
                self.ran.append(sql)
                return 0
            return super().command(sql)

    with pytest.raises(preflight.PreflightError) as e:
        preflight.check(NoCreate(database=False), need="migrate", create_database=False)
    assert e.value.needs_admin and "may not create it" in str(e.value)


def test_a_database_another_worker_created_meanwhile_is_not_a_failure() -> None:
    class Raced(FakeCH):
        def command(self, sql: str) -> Any:
            if sql.startswith("CREATE DATABASE"):
                self.database = True  # the other worker won
                raise RuntimeError(
                    "Code: 341. DB::Exception: Database loom was created by another replica"
                )
            return super().command(sql)

    preflight.check(Raced(database=False), need="migrate")


def test_a_check_that_may_not_create_the_database_only_reports_it_missing() -> None:
    ch = FakeCH(database=False)
    server = preflight.check(ch, need="migrate", create_database=False)
    assert not server.database
    assert not any(sql.startswith("CREATE DATABASE") for sql in ch.ran)


def test_the_doctor_warns_that_boot_will_create_the_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.cli import doctor

    missing = preflight.Server("26.9.19.9", keeper=True, lightweight_updates=True, database=False)
    monkeypatch.setattr(preflight, "require_ready", lambda **_: missing)
    name, grade, detail = doctor._doctor_preflight_check()
    assert (name, grade) == ("clickhouse_preflight", doctor.WARN)
    assert "boot will create it" in detail
