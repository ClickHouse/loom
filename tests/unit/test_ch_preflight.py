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

# The real ones, for the tests of them; fixtures stand in for them elsewhere.
_real_migrations_pending = preflight._migrations_pending
_real_require_ready = preflight.require_ready


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

    def close(self) -> None:
        pass

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
    server = preflight.check(FakeCH(), need="server")
    assert server == preflight.Server("26.5.1.882", keeper=True, lightweight_updates=True)


def test_replica_discovery_cannot_be_left_to_the_user_s_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A profile with skip_unavailable_shards = 1 would silently drop a replica.
    from server.settings import settings

    monkeypatch.setattr(settings, "ch_cluster", "prod")
    sql = preflight.replica_versions_sql()
    assert "clusterAllReplicas('prod', system.one)" in sql
    assert sql.endswith("SETTINGS skip_unavailable_shards = 0")
    monkeypatch.setattr(settings, "ch_cluster", "")
    assert preflight.replica_versions_sql() == "SELECT version()"


def test_the_oldest_replica_decides_the_version() -> None:
    with pytest.raises(preflight.PreflightError, match=r"26\.3\.1\.1 is older than 26\.4"):
        preflight.check(FakeCH(replicas=("26.6.1.1", "26.3.1.1")), need="server")


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


class _NoKeeper(FakeCH):
    def command(self, sql: str, **kwargs: Any) -> Any:
        if "zookeeper_connection" in sql:
            self.ran.append(sql)
            return 0
        return super().command(sql, **kwargs)


def test_a_server_without_a_keeper_is_refused_for_every_process_that_serves() -> None:
    with pytest.raises(preflight.PreflightError, match="Loom needs a ClickHouse Keeper") as e:
        preflight.check(_NoKeeper(), need="server")
    assert e.value.no_keeper and not e.value.needs_admin
    assert preflight.check(_NoKeeper(), need="read").keeper is False


def test_ch_setup_says_how_to_add_a_keeper_and_exits_3(capsys: pytest.CaptureFixture[str]) -> None:
    assert ch_setup._set_up(_NoKeeper(), "loom", "loom_reader") == ch_setup.NO_KEEPER_EXIT == 3
    err = capsys.readouterr().err
    assert "keeper.xml" in err and "<zookeeper>" in err and "keeper_map_path_prefix" in err


def test_a_reader_falls_back_when_the_replicas_cannot_be_asked() -> None:
    ch = FakeCH(replica_error="Code: 279. DB::Exception: All connection tries failed")
    assert preflight.check(ch, need="read").version == "26.5.1.882"
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
    server = preflight.check(ch, need="server")
    assert server.version == "26.5.1.882"
    assert "clusterAllReplicas" in server.next_migration[0].reason


def test_what_the_next_migration_needs_is_reported_while_nothing_is_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(preflight, "_migrations_pending", lambda ch, journal: False)
    ch = FakeCH(denied=("SELECT * FROM system.mutations",))
    (gap,) = preflight.check(ch, need="server").next_migration
    assert "system.mutations" in gap.reason and gap.needs_admin
    assert preflight.check(ch, need="read").next_migration == ()


def test_the_doctor_warns_of_what_the_next_migration_will_refuse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.cli import doctor

    gaps = (
        preflight.PreflightError("may not read system.mutations", needs_admin=True),
        preflight.PreflightError("may not ask every replica", needs_admin=True),
    )
    server = preflight.Server(
        "26.5.1.882", keeper=True, lightweight_updates=True, unreadable=("query_log",)
    )
    monkeypatch.setattr(
        preflight, "require_ready", lambda **_: dataclasses.replace(server, next_migration=gaps)
    )
    name, grade, detail = doctor._doctor_preflight_check()
    assert (name, grade) == ("clickhouse_preflight", doctor.WARN)
    assert "will refuse: may not read system.mutations | may not ask every replica" in detail
    assert "system.query_log" in detail
    # One pointer to the SQL for the whole line.
    assert detail.count("--print-sql") == 1 and detail.count("will refuse") == 1


def test_a_refused_telemetry_table_is_a_warning_not_a_stop() -> None:
    ch = FakeCH(denied=("SELECT * FROM system.query_log",))
    assert preflight.check(ch, need="server").unreadable == ("query_log",)


def test_every_system_table_loom_reads_is_checked_before_any_ddl() -> None:
    ch = FakeCH(denied=("SELECT * FROM system.merge_tree_settings",))
    with pytest.raises(preflight.PreflightError) as e:
        preflight.check(ch, need="server")
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
    preflight.check(ch, need="server")
    assert "CREATE DATABASE IF NOT EXISTS loom" in ch.ran
    with pytest.raises(preflight.PreflightError, match=r"missing, or no grant on it"):
        preflight.check(FakeCH(database=False), need="read")


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
        preflight.check(ch, need="server")
    assert e.value.needs_admin and "cannot see a database loom and may not create it" in str(
        e.value
    )


def test_missing_grants_name_the_grant_to_run() -> None:
    with pytest.raises(preflight.PreflightError) as e:
        preflight.check(FakeCH(grants=0), need="server")
    assert e.value.needs_admin and "GRANT CURRENT GRANTS ON loom.* TO `loom`" in str(e.value)


def test_the_server_grants_never_name_the_global_alter_group() -> None:
    # 26.9 put the global-only ALTER HANDLER in ALTER: a grant on loom.* never holds it.
    assert "ALTER," not in preflight.GRANTS["server"] + ","
    assert "ALTER TABLE" in preflight.GRANTS["server"]


def test_status_reads_need_only_select() -> None:
    ch = FakeCH()
    preflight.check(ch, need="read")
    assert "CHECK GRANT SELECT ON loom.*" in ch.ran


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
    assert ch_setup._cmd_ch_setup(argparse.Namespace(print_sql=True, print_analyst_sql=False)) == 2


@pytest.mark.parametrize(
    ("found", "logged_in", "problem", "refusal"),
    [
        # A same-named user that does not log in with Loom's password: an analyst's account.
        ("user", False, "refused", "exists but does not accept LOOM_CH_PASSWORD"),
        # A probe that failed for another reason proves nothing: no advice to drop it.
        ("user", None, "Read timed out", "could not check analyst: Read timed out"),
        # No password, not on its own profile, or a shared profile.
        (
            "foreign",
            True,
            "its profile does not match",
            "is taken by a user or settings profile ch-setup did not make",
        ),
        # A profile with no user: Loom's own after DROP USER, or someone else's.
        ("profile", False, "missing", "DROP SETTINGS PROFILE `analyst`"),
    ],
)
def test_an_account_ch_setup_cannot_prove_is_loom_s_is_left_alone(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    found: str,
    logged_in: bool | None,
    problem: str,
    refusal: str,
) -> None:
    monkeypatch.setattr(ch_setup, "reader_status", lambda reader: (logged_in, problem))
    monkeypatch.setattr(ch_setup, "reader_objects", lambda ch, reader: found)
    ch = FakeCH()
    assert ch_setup._set_up(ch, "loom_app", "analyst") == 1
    assert refusal in capsys.readouterr().err
    assert not any(sql.startswith(("CREATE USER", "CREATE SETTINGS", "DROP")) for sql in ch.ran)


@pytest.mark.parametrize("found", ["foreign", "profile", ""])
def test_ch_setup_does_not_log_in_as_an_account_it_does_not_own(
    monkeypatch: pytest.MonkeyPatch, found: str
) -> None:
    def must_not_log_in(reader: str) -> Any:
        raise AssertionError(f"logged in as {reader}")

    monkeypatch.setattr(ch_setup, "reader_status", must_not_log_in)
    monkeypatch.setattr(ch_setup, "reader_objects", lambda ch, reader: found)
    ch = FakeCH()
    assert ch_setup._set_up(ch, "loom_app", "loom_reader") == (0 if found == "" else 1)
    created = any(sql.startswith("CREATE USER") for sql in ch.ran)
    assert created == (found == "")


@pytest.mark.parametrize(
    ("logged_in", "problem", "refusal"),
    [
        (None, "Read timed out", "could not check loom_reader: Read timed out"),
        (False, "it does not accept LOOM_CH_PASSWORD", "a password other than LOOM_CH_PASSWORD"),
        (True, "its profile does not match", "change its profile"),
    ],
)
def test_a_reader_in_the_server_s_config_gets_the_fix_for_what_is_wrong(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    logged_in: bool | None,
    problem: str,
    refusal: str,
) -> None:
    monkeypatch.setattr(ch_setup, "reader_status", lambda reader: (logged_in, problem))
    monkeypatch.setattr(ch_setup, "reader_objects", lambda ch, reader: "config")
    assert ch_setup._set_up(FakeCH(), "loom", "loom_reader") == 1
    err = capsys.readouterr().err
    assert refusal in err
    assert ("nlp_user_settings.xml" in err) == (logged_in is True)


class Accounts(FakeCH):
    """A server holding `user` (storage, open login) and profile rows of the reader's name."""

    def __init__(self, user=None, own_profile=0, profiles=0, shared=0, assigned=0):
        super().__init__()
        self.user, self.counts = user, (own_profile, profiles, shared, assigned)

    def query(self, sql: str, **kwargs: Any) -> SimpleNamespace:
        if "system.users" in sql:
            return SimpleNamespace(result_rows=[self.user] if self.user else [])
        return super().query(sql, **kwargs)

    def command(self, sql: str, **kwargs: Any) -> Any:
        own_profile, profiles, shared, assigned = self.counts
        if "user_name = {r:String} AND inherit_profile" in sql:
            return own_profile
        if "apply_to_all" in sql:
            return assigned
        if "system.settings_profiles" in sql:
            return profiles
        if "ifNull(user_name" in sql:
            return shared
        return super().command(sql, **kwargs)


@pytest.mark.parametrize(
    ("accounts", "found"),
    [
        (Accounts(), ""),
        (Accounts(user=("users_xml", 0)), "config"),
        (Accounts(user=("local_directory", 0), own_profile=1, profiles=1), "user"),
        (Accounts(user=("local_directory", 1), own_profile=1, profiles=1), "foreign"),
        (Accounts(user=("local_directory", 0)), "foreign"),
        (Accounts(user=("local_directory", 0), own_profile=1, profiles=1, shared=1), "foreign"),
        (Accounts(profiles=1, shared=1), "foreign"),
        (Accounts(profiles=1), "profile"),
        # Assigned to other accounts directly (TO <user>, TO <role>, TO ALL).
        (Accounts(user=("local_directory", 0), own_profile=1, profiles=1, assigned=1), "foreign"),
        (Accounts(profiles=1, assigned=1), "foreign"),
    ],
)
def test_only_a_reader_in_ch_setup_s_shape_counts_as_a_user(accounts: Accounts, found: str) -> None:
    assert ch_setup.reader_objects(accounts, "loom_reader") == found


def test_a_reader_left_halfway_is_dropped_so_a_rerun_starts_clean(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ch_setup, "reader_status", lambda reader: (False, "missing"))
    monkeypatch.setattr(ch_setup, "reader_objects", lambda ch, reader: "")
    # ClickHouse checks the DROP USER privilege even for a user that does not exist.
    ch = FakeCH(denied=("CREATE USER", "DROP USER"))
    assert ch_setup._set_up(ch, "loom_app", "loom_reader") == 1
    assert "DROP SETTINGS PROFILE IF EXISTS `loom_reader`" in ch.ran


def test_ch_setup_checks_for_the_next_migration_even_with_nothing_pending(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(preflight, "_migrations_pending", lambda ch, journal: False)
    ch = FakeCH(replica_error="Code: 497. DB::Exception: Not enough privileges. (ACCESS_DENIED)")
    assert ch_setup._set_up(ch, "loom_app", "loom_reader") == 1
    assert "GRANT READ ON REMOTE" in capsys.readouterr().err


def test_a_reader_that_logs_in_with_loom_s_password_is_rebuilt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ch_setup, "reader_status", lambda r: (True, "its profile does not match"))
    monkeypatch.setattr(ch_setup, "reader_objects", lambda ch, r: "user")
    ch = FakeCH()
    assert ch_setup._set_up(ch, "loom", "loom_reader") == 0
    assert any(sql.startswith("CREATE USER OR REPLACE `loom_reader`") for sql in ch.ran)


def test_a_bundled_reader_whose_caps_differ_is_a_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        ch_setup, "reader_status", lambda r: (True, "its profile does not match the current caps")
    )
    monkeypatch.setattr(ch_setup, "reader_objects", lambda ch, r: "config")
    assert ch_setup._set_up(FakeCH(), "loom", "loom_reader") == 1
    assert (
        "change its profile in deploy/clickhouse-config/nlp_user_settings.xml"
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

    monkeypatch.setattr(ch_setup, "reader_status", lambda reader: (False, "missing"))
    ch = MayNotListUsers()
    assert ch_setup.reader_objects(ch, "someone_elses") == "unknown"
    assert ch_setup._set_up(ch, "delegate", "someone_elses") == 1
    assert not any(sql.startswith("CREATE USER") for sql in ch.ran)
    captured = capsys.readouterr()
    assert "someone_elses: missing; 'delegate' cannot see who owns it" in captured.err
    assert "CREATE USER OR REPLACE `someone_elses`" in captured.err  # for an admin to judge


def test_a_user_who_may_not_create_the_reader_gets_the_admin_sql(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "ch_password", "s3cret-value")
    monkeypatch.setattr(ch_setup, "reader_status", lambda reader: (False, "missing"))
    ch = FakeCH(denied=("CREATE SETTINGS PROFILE",))
    assert ch_setup._set_up(ch, "loom_app", "loom_reader") == 1
    captured = capsys.readouterr()
    assert "may not create the reader or grant it SELECT on loom.*" in captured.err
    assert "CREATE USER OR REPLACE `loom_reader`" in captured.err
    assert "s3cret-value" not in captured.out + captured.err


def test_the_doctor_fails_where_boot_could_not_create_the_database() -> None:
    class NoCreate(FakeCH):
        def command(self, sql: str, **kwargs: Any) -> Any:
            if sql.startswith("CHECK GRANT CREATE DATABASE"):
                self.ran.append(sql)
                return 0
            return super().command(sql, **kwargs)

    with pytest.raises(preflight.PreflightError) as e:
        preflight.check(NoCreate(database=False), need="server", create_database=False)
    assert e.value.needs_admin and "may not create it" in str(e.value)


def test_a_database_another_worker_created_meanwhile_is_not_a_failure() -> None:
    class Raced(FakeCH):
        def command(self, sql: str, **kwargs: Any) -> Any:
            if sql.startswith("CREATE DATABASE"):
                self.database = True  # the other worker won
                raise RuntimeError(
                    "Code: 341. DB::Exception: Database loom was created by another replica"
                )
            return super().command(sql, **kwargs)

    preflight.check(Raced(database=False), need="server")


def test_a_check_that_may_not_create_the_database_only_reports_it_missing() -> None:
    ch = FakeCH(database=False)
    server = preflight.check(ch, need="server", create_database=False)
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


_TELEMETRY_GAP = preflight.Server(
    "26.5.1.882", keeper=True, lightweight_updates=True, unreadable=("query_log",)
)


async def test_boot_logs_what_a_passing_preflight_warns_of_once(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from server import boot

    class Narrowed(FakeCH):
        """Nothing pending, but the replicas and system.query_log are refused."""

    ch = Narrowed(
        replica_error="Code: 497. DB::Exception: Not enough privileges. (ACCESS_DENIED)",
        denied=("SELECT * FROM system.query_log",),
    )
    monkeypatch.setattr(preflight, "require_ready", _real_require_ready)
    monkeypatch.setattr(preflight, "connect", lambda: ch)
    monkeypatch.setattr(preflight, "_migrations_pending", lambda ch, journal: False)
    with caplog.at_level("DEBUG"):
        await boot.require_clickhouse_ready()
    lines = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert len(lines) == 1 and lines[0].startswith("ClickHouse preflight: ")
    assert "clusterAllReplicas" in lines[0] and "system.query_log" in lines[0]


def test_migrate_prints_what_a_passing_preflight_warns_of(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from server.core.schema import migrate

    monkeypatch.setattr(preflight, "require_ready", lambda **_: _TELEMETRY_GAP)
    migrate._check_server(argparse.Namespace(status=False, dry_run=False, migrations_dir="."))
    assert "! may not read system.query_log" in capsys.readouterr().err


def test_ch_setup_names_the_grant_for_an_unreadable_telemetry_table(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(ch_setup, "reader_objects", lambda ch, reader: "config")
    monkeypatch.setattr(ch_setup, "reader_status", lambda reader: (True, ""))
    ch = FakeCH(denied=("SELECT * FROM system.query_log",))
    assert ch_setup._set_up(ch, "loom_app", "loom_reader") == 0
    out = capsys.readouterr().out
    assert "GRANT SELECT ON system.query_log TO `loom_app`" in out and "--print-sql" not in out


def test_an_empty_reader_user_opts_out_of_the_reader(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from server.settings import settings

    def must_not_touch(*_: Any) -> Any:
        raise AssertionError("the reader was looked at")

    monkeypatch.setenv("LOOM_CH_READER_USER", "")
    monkeypatch.setattr(settings, "ch_user", "loom_app")
    monkeypatch.setattr(settings, "ch_reader_user", "")
    monkeypatch.setattr(ch_setup, "reader_objects", must_not_touch)
    monkeypatch.setattr(ch_setup, "reader_status", must_not_touch)
    ch = FakeCH()
    monkeypatch.setattr(preflight, "connect", lambda: ch)
    assert ch_setup._cmd_ch_setup(argparse.Namespace(print_sql=False, print_analyst_sql=False)) == 0
    assert "caller-shaped reads run as 'loom_app'" in capsys.readouterr().out
    assert not any("loom_reader" in sql for sql in ch.ran)
    ch_setup._cmd_ch_setup(argparse.Namespace(print_sql=True, print_analyst_sql=False))
    assert "CREATE USER" not in capsys.readouterr().out


def test_an_unset_reader_user_means_the_default_reader(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from server.settings import settings

    monkeypatch.delenv("LOOM_CH_READER_USER", raising=False)
    monkeypatch.setattr(settings, "ch_user", "loom_app")
    monkeypatch.setattr(settings, "ch_reader_user", "")
    ch_setup._cmd_ch_setup(argparse.Namespace(print_sql=True, print_analyst_sql=False))
    assert "CREATE USER OR REPLACE `loom_reader`" in capsys.readouterr().out


_WIDER = "its grants are not SELECT on loom.* alone"


@pytest.mark.parametrize(
    ("reader", "grants", "problem", "lands_as"),
    [
        ("loom_reader", ["GRANT SELECT ON loom.* TO loom_reader"], "", "loom_reader"),
        # ClickHouse quotes a name like this one in SHOW GRANTS.
        ("loom-reader", ["GRANT SELECT ON loom.* TO `loom-reader`"], "", "loom-reader"),
        # An auth mapping that logs the reader in as someone else.
        (
            "loom_reader",
            ["GRANT SELECT ON loom.* TO loom_reader"],
            "its login lands as 'mapped_user', not 'loom_reader'",
            "mapped_user",
        ),
        (
            "loom_reader",
            ["GRANT SELECT ON loom.* TO loom_reader", "GRANT SELECT ON *.* TO loom_reader"],
            _WIDER,
            "loom_reader",
        ),
        # Narrowed by an admin: not the reader ch-setup makes either.
        (
            "loom_reader",
            [
                "GRANT SELECT ON loom.* TO loom_reader",
                "REVOKE SELECT ON loom.memory FROM loom_reader",
            ],
            _WIDER,
            "loom_reader",
        ),
    ],
)
def test_the_reader_holds_select_on_loom_alone(
    monkeypatch: pytest.MonkeyPatch,
    reader: str,
    grants: list[str],
    problem: str,
    lands_as: str,
) -> None:
    import clickhouse_connect

    def row(name: str, s: ch_setup.Setting) -> tuple[Any, ...]:
        value = s.value.strip("'")
        low = None if s.low is None else str(s.low)
        high = None if s.high is None else str(s.high)
        return (name, value, low, high, int(s.const))

    class Reader:
        def command(self, sql: str) -> Any:
            return lands_as

        def query(self, sql: str, **_: Any) -> SimpleNamespace:
            if sql == "SHOW GRANTS":
                return SimpleNamespace(result_rows=[(g,) for g in grants])
            return SimpleNamespace(result_rows=[row(n, s) for n, s in ch_setup._profile().items()])

        def close(self) -> None:
            pass

    monkeypatch.setattr(clickhouse_connect, "get_client", lambda **_: Reader())
    assert ch_setup.reader_status(reader) == (True, problem)


def test_a_missing_ddl_grant_stops_even_with_nothing_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class DmlOnly(FakeCH):
        def command(self, sql: str, **kwargs: Any) -> Any:
            held = ("SELECT", "INSERT", "ALTER UPDATE", "ALTER DELETE")
            if sql.startswith("CHECK GRANT ") and "," not in sql:
                return int(sql.removeprefix("CHECK GRANT ").split(" ON ")[0] in held)
            if sql == f"CHECK GRANT {preflight.GRANTS['server']} ON loom.*":
                return 0
            return super().command(sql, **kwargs)

    # The running server's workers use the DDL grants too.
    monkeypatch.setattr(preflight, "_migrations_pending", lambda ch, journal: False)
    with pytest.raises(preflight.PreflightError) as e:
        preflight.check(DmlOnly(), need="server")
    assert "is missing CREATE TABLE" in str(e.value)
    assert "SELECT" not in str(e.value).split(" on loom.*")[0]


def test_every_process_needs_what_the_workers_run() -> None:
    class NoWorkerGrants(FakeCH):
        def command(self, sql: str, **kwargs: Any) -> Any:
            if sql.startswith("CHECK GRANT ") and any(
                g in sql for g in ("OPTIMIZE", "TRUNCATE", "SYSTEM REFRESH VIEW")
            ):
                return 0
            return super().command(sql, **kwargs)

    # Boot migrations off changes nothing: the workers and provider rebuilds run.
    with pytest.raises(preflight.PreflightError) as e:
        preflight.check(NoWorkerGrants(), need="server", migrates=False)
    assert "is missing OPTIMIZE, TRUNCATE on loom.*" in str(e.value)


class _NoTelemetryGrants(FakeCH):
    """Every grant on loom.*, none on loom_telemetry.*."""

    def command(self, sql: str, **kwargs: Any) -> Any:
        if sql.startswith("CHECK GRANT ") and sql.endswith(" ON loom_telemetry.*"):
            self.ran.append(sql)
            return 0
        return super().command(sql, **kwargs)


def test_a_migrating_boot_without_the_telemetry_grants_stops_naming_them() -> None:
    with pytest.raises(preflight.PreflightError) as e:
        preflight.check(_NoTelemetryGrants(), need="server")
    assert e.value.needs_admin
    assert "is missing CREATE DATABASE, CREATE TABLE" in str(e.value)
    assert "GRANT CURRENT GRANTS ON loom_telemetry.* TO `loom`" in str(e.value)


def test_the_telemetry_grants_warn_with_nothing_pending_and_are_not_a_reader_s(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Only a migration writes loom_telemetry; the serving process never does.
    monkeypatch.setattr(preflight, "_migrations_pending", lambda ch, journal: False)
    server = preflight.check(_NoTelemetryGrants(), need="server")
    assert "on loom_telemetry.*" in preflight.warnings(server)
    ch = _NoTelemetryGrants()
    preflight.check(ch, need="read")
    assert not any(sql.endswith(" ON loom_telemetry.*") for sql in ch.ran)


def test_a_server_that_does_not_migrate_warns_when_it_cannot_read_the_telemetry() -> None:
    server = preflight.check(_NoTelemetryGrants(), need="server", migrates=False)
    assert server.no_telemetry_read
    assert "may not SELECT on loom_telemetry.*" in preflight.warnings(server)
    assert not preflight.check(FakeCH(), need="server", migrates=False).no_telemetry_read


def test_the_setup_sql_grants_the_telemetry_database_but_not_to_the_reader() -> None:
    sql = ch_setup.admin_sql("loom_app", "loom_reader")
    assert "GRANT CURRENT GRANTS ON loom_telemetry.* TO `loom_app`" in sql
    assert not any("loom_telemetry" in line and "loom_reader" in line for line in sql)


def test_a_process_that_does_not_migrate_never_creates_the_database() -> None:
    ch = FakeCH(database=False)
    with pytest.raises(preflight.PreflightError, match="cannot see a database loom"):
        preflight.check(ch, need="server", migrates=False)
    assert not any(sql.startswith("CREATE DATABASE") for sql in ch.ran)


@pytest.mark.parametrize("boot_migrate", [True, False])
async def test_boot_and_the_doctor_say_whether_this_process_migrates(
    monkeypatch: pytest.MonkeyPatch, boot_migrate: bool
) -> None:
    from server import boot
    from server.cli import doctor
    from server.settings import settings

    seen: list[bool] = []

    def ready(**kwargs: Any) -> preflight.Server:
        seen.append(kwargs["migrates"])
        return preflight.Server("26.5.1.882", keeper=True, lightweight_updates=True)

    monkeypatch.setattr(settings, "boot_migrate", boot_migrate)
    monkeypatch.setattr(preflight, "require_ready", ready)
    await boot.require_clickhouse_ready()
    doctor._doctor_preflight_check()
    assert seen == [boot_migrate, boot_migrate]


def test_a_server_without_refresh_view_boots_with_a_warning() -> None:
    class NoRefresh(FakeCH):
        def command(self, sql: str, **kwargs: Any) -> Any:
            if sql == f"CHECK GRANT {preflight.REFRESH_GRANT} ON loom.*":
                return 0
            return super().command(sql, **kwargs)

    server = preflight.check(NoRefresh(), need="server")
    assert server.no_refresh
    assert "may not SYSTEM REFRESH VIEW on loom.*" in preflight.warnings(server)
    assert not preflight.check(NoRefresh(), need="read").no_refresh


def test_ch_setup_says_a_missing_refresh_grant_and_how_to_give_it(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class NoRefresh(FakeCH):
        def command(self, sql: str, **kwargs: Any) -> Any:
            if sql == f"CHECK GRANT {preflight.REFRESH_GRANT} ON loom.*":
                return 0
            return super().command(sql, **kwargs)

    monkeypatch.setattr(ch_setup, "reader_objects", lambda ch, reader: "config")
    monkeypatch.setattr(ch_setup, "reader_status", lambda reader: (True, ""))
    assert ch_setup._set_up(NoRefresh(), "loom_app", "loom_reader") == 0
    assert "GRANT SYSTEM REFRESH VIEW ON loom.* TO `loom_app`" in capsys.readouterr().out


def test_a_reader_rebuilt_for_new_caps_says_to_recreate_loom(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(ch_setup, "reader_objects", lambda ch, r: "user")
    monkeypatch.setattr(ch_setup, "reader_status", lambda r: (True, ch_setup.PROFILE_DIFFERS))
    assert ch_setup._set_up(FakeCH(), "loom", "loom_reader") == 0
    assert "--force-recreate loom" in capsys.readouterr().out
    # Rebuilt for its grants, the caps are unchanged: nothing to recreate.
    wider = "its grants are not SELECT on loom.* alone"
    monkeypatch.setattr(ch_setup, "reader_status", lambda r: (True, wider))
    assert ch_setup._set_up(FakeCH(), "loom", "loom_reader") == 0
    assert "--force-recreate" not in capsys.readouterr().out


def test_a_reader_the_server_rejects_is_one_line_not_a_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class Rejects(FakeCH):
        def command(self, sql: str, **kwargs: Any) -> Any:
            if sql.startswith("CREATE SETTINGS PROFILE"):
                raise RuntimeError("Code: 70. DB::Exception: Cannot convert. (CANNOT_CONVERT_TYPE)")
            return super().command(sql, **kwargs)

    monkeypatch.setattr(ch_setup, "reader_objects", lambda ch, r: "")
    ch = Rejects()
    assert ch_setup._set_up(ch, "loom", "loom_reader") == 1
    assert "could not create loom_reader: Code: 70." in capsys.readouterr().err
    assert "DROP SETTINGS PROFILE IF EXISTS `loom_reader`" in ch.ran  # half-made, dropped


@pytest.mark.parametrize(
    ("setting", "value", "refused"),
    [
        ("ch_caller_shaped_max_execution_s", 0.01, True),
        ("ch_caller_shaped_max_execution_s", -5.0, True),
        ("ch_caller_shaped_max_rows_to_read", -1, True),
        ("ch_caller_shaped_max_execution_s", 0.0, False),
        ("ch_caller_shaped_max_execution_s", 0.05, False),
    ],
)
def test_a_cap_the_profile_cannot_hold_is_refused(
    monkeypatch: pytest.MonkeyPatch, setting: str, value: float, refused: bool
) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, setting, value)
    assert bool(ch_setup.caps_problem()) is refused


def test_a_driver_error_line_keeps_the_server_s_words() -> None:
    from server.core.ch_errors import error_line

    raw = (
        "Received ClickHouse exception, code: 36, server response: Code: 36. DB::Exception: "
        "Invalid password. (BAD_ARGUMENTS) (for url http://h:8123)\ntraceback"
    )
    assert (
        error_line(RuntimeError(raw))
        == "Code: 36. DB::Exception: Invalid password. (BAD_ARGUMENTS)"
    )
    assert error_line(TimeoutError()) == "TimeoutError"


def test_the_recreate_hint_follows_the_admin_sql_only_for_an_existing_reader(
    capsys: pytest.CaptureFixture[str],
) -> None:
    ch_setup._print_admin_sql("loom_app", "loom_reader")
    assert "recreate Loom" not in capsys.readouterr().err  # a first install: nothing runs yet
    ch_setup._print_admin_sql("loom_app", "loom_reader", existing=True)
    assert "recreate Loom" in capsys.readouterr().err


def test_the_admin_sql_grants_nothing_that_lists_other_accounts() -> None:
    sql = " ".join(ch_setup.admin_sql("loom_app", "loom_reader"))
    assert "system.users" not in sql and "settings_profile" not in sql


@pytest.mark.parametrize(("logged_in", "hint"), [(False, False), (True, True)])
def test_an_unknown_owner_gets_the_recreate_hint_only_for_a_reader_that_exists(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    logged_in: bool,
    hint: bool,
) -> None:
    problem = ch_setup.PROFILE_DIFFERS if logged_in else "it does not exist"
    monkeypatch.setattr(ch_setup, "reader_objects", lambda ch, r: "unknown")
    monkeypatch.setattr(ch_setup, "reader_status", lambda r: (logged_in, problem))
    assert ch_setup._set_up(FakeCH(), "loom_app", "loom_reader") == 1
    assert ("recreate Loom" in capsys.readouterr().err) is hint
