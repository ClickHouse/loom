"""A migration waits for its ALTERs and mutations: the wait settings each
statement kind carries, and the `system.mutations` check that keeps a file
whose mutation failed or never finished out of the ledger."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from server.core.schema import migrate as mig

ALL = frozenset(mig._SYNC_SETTINGS)


@pytest.mark.parametrize(
    ("stmt", "want"),
    [
        ("ALTER TABLE loom.t ADD COLUMN c UInt8 DEFAULT 0", {"alter_sync": 2}),
        ("ALTER TABLE loom.t MODIFY SETTING index_granularity = 256", {"alter_sync": 2}),
        (
            "ALTER TABLE loom.t MATERIALIZE COLUMN c",
            {"alter_sync": 2, "mutations_sync": 2},
        ),
        (
            "ALTER TABLE loom.t MATERIALIZE INDEX idx",
            {"alter_sync": 2, "mutations_sync": 2},
        ),
        ("ALTER TABLE loom.t MODIFY COLUMN c UInt16", {"alter_sync": 2, "mutations_sync": 2}),
        (
            "alter table loom.t\n  update c = 1 where c = 0",
            {"alter_sync": 2, "mutations_sync": 2},
        ),
        ("ALTER TABLE loom.t DELETE WHERE c = 0", {"alter_sync": 2, "mutations_sync": 2}),
        ("DELETE FROM loom.t WHERE c = 0", {"lightweight_deletes_sync": 2}),
        ("OPTIMIZE TABLE loom.t FINAL", {"alter_sync": 2, "optimize_throw_if_noop": 0}),
        ("CREATE TABLE IF NOT EXISTS loom.t (c UInt8) ENGINE = MergeTree ORDER BY c", {}),
        ("INSERT INTO loom.t SELECT 1", {}),
    ],
)
def test_wait_settings_by_statement_kind(stmt: str, want: dict[str, int]) -> None:
    assert mig._sync_settings(stmt, ALL) == want


def test_only_accepted_settings_are_sent() -> None:
    stmt = "ALTER TABLE loom.t MATERIALIZE COLUMN c"
    assert mig._sync_settings(stmt, frozenset({"mutations_sync"})) == {"mutations_sync": 2}
    assert mig._sync_settings(stmt, frozenset()) == {}


class _SettingsCH:
    def __init__(self, rows=None, error: str | None = None) -> None:
        self.rows, self.error, self.sql = rows or [], error, ""

    def query(self, sql: str, *a, **k):
        self.sql = sql
        if self.error:
            raise RuntimeError(self.error)
        return SimpleNamespace(result_rows=self.rows)


def test_probe_keeps_changeable_settings_and_names_the_rest(capsys) -> None:
    ch = _SettingsCH(rows=[("alter_sync",), ("mutations_sync",), ("optimize_throw_if_noop",)])
    got = mig._sync_settings_accepted(ch)
    assert got == {"alter_sync", "mutations_sync", "optimize_throw_if_noop"}
    assert "system.settings" in ch.sql and "readonly = 0" in ch.sql
    assert (
        "not sent (unknown or readonly here): lightweight_deletes_sync" in capsys.readouterr().out
    )
    assert mig._sync_settings_accepted(_SettingsCH(error="Code: 497.")) == frozenset()


def test_apply_file_attaches_wait_settings_next_to_the_others(tmp_path: Path) -> None:
    f = tmp_path / "1999_x.sql"
    f.write_text(
        "ALTER TABLE loom.memory_embeddings MATERIALIZE COLUMN c;\n\n"
        "OPTIMIZE TABLE loom.t FINAL;\n\n"
        "CREATE TABLE IF NOT EXISTS loom.t (c UInt8) ENGINE = MergeTree ORDER BY c;\n"
    )
    calls: list[dict | None] = []

    class CH:
        def command(self, sql, settings=None):
            calls.append(settings)

    mig._apply_file(CH(), f, dry=False, nlp_ok=True, sync=mig.SyncPolicy(ALL))
    assert calls == [
        {"allow_experimental_nlp_functions": 1, "alter_sync": 2, "mutations_sync": 2},
        {"alter_sync": 2, "optimize_throw_if_noop": 0},
        None,
    ]


def test_mutated_tables_are_the_loom_tables_altered_or_deleted_from() -> None:
    sql = (
        "-- a comment naming ALTER TABLE loom.nope\n"
        "CREATE TABLE IF NOT EXISTS loom.a (c UInt8) ENGINE = MergeTree ORDER BY c;\n"
        "ALTER TABLE loom.b MATERIALIZE COLUMN c;\n"
        "DELETE FROM loom.c WHERE 1;\n"
        "ALTER TABLE loom.b ADD COLUMN d UInt8;\n"
    )
    assert mig._mutated_tables(sql) == ["b", "c"]


# -- the system.mutations gate ------------------------------------------------------


class _MutationsCH:
    """`system.mutations` as a queue of answers, one per read; the last repeats."""

    def __init__(self, answers: list[list[tuple]]) -> None:
        self.answers = answers
        self.reads: list[dict] = []
        self.commands: list[tuple[str, dict | None]] = []
        self.inserts: list = []

    def query(self, sql: str, parameters=None, settings=None):
        if "system.mutations" in sql:
            assert "database = currentDatabase()" in sql and "NOT is_done" in sql and "LIMIT" in sql
            self.reads.append(parameters)
            rows = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
            return SimpleNamespace(result_rows=rows)
        return SimpleNamespace(result_rows=[])  # probes, the ledger belt

    def command(self, sql: str, settings=None):
        self.commands.append((sql, settings))

    def insert(self, *a, **k):
        self.inserts.append(a)


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def monotonic(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s


def test_wait_returns_once_the_new_mutation_is_done(capsys) -> None:
    old = ("t", "0000000001", "UPDATE x = 1", "")
    new = ("t", "0000000002", "MATERIALIZE COLUMN c", "")
    ch = _MutationsCH([[old, new], [old, new], [old]])
    clock = _Clock()
    mig._await_mutations(
        ch, ["t"], {("t", "0000000001")}, wait_s=60, sleep=clock.sleep, clock=clock.monotonic
    )
    assert len(ch.reads) == 3 and ch.reads[0] == {"tables": ["t"]}
    assert "waiting for 1 mutation(s) on t" in capsys.readouterr().out


def test_a_pending_mutation_from_before_the_file_is_not_waited_on() -> None:
    old = ("t", "0000000001", "UPDATE x = 1", "Code: 241. MEMORY_LIMIT_EXCEEDED")
    ch = _MutationsCH([[old]])
    mig._await_mutations(ch, ["t"], {("t", "0000000001")}, wait_s=60, sleep=lambda s: None)
    assert len(ch.reads) == 1


def test_a_failed_mutation_raises_with_its_reason() -> None:
    bad = ("t", "0000000002", "MATERIALIZE COLUMN c", "Code: 6. Cannot parse string 'x'")
    ch = _MutationsCH([[bad]])
    with pytest.raises(mig.MutationError, match="0000000002 on loom.t .*Cannot parse string"):
        mig._await_mutations(ch, ["t"], set(), wait_s=60, sleep=lambda s: None)


def test_a_mutation_still_running_past_the_wait_raises() -> None:
    run = ("t", "0000000002", "MATERIALIZE COLUMN c", "")
    ch = _MutationsCH([[run]])
    clock = _Clock()
    with pytest.raises(mig.MutationError, match="still running after 30 s .*loom.t 0000000002"):
        mig._await_mutations(ch, ["t"], set(), wait_s=30, sleep=clock.sleep, clock=clock.monotonic)
    assert clock.t >= 30


def _pending_run(monkeypatch, tmp_path: Path, answers: list[list[tuple]], wait_s: int = 30):
    from server.settings import settings

    monkeypatch.setattr(settings, "migrate_mutation_wait_s", wait_s)
    monkeypatch.setattr(mig, "_nlp_available", lambda ch: True)
    monkeypatch.setattr(mig, "_codec_settings", lambda ch: mig.NO_CODEC)
    monkeypatch.setattr(mig, "_sync_settings_accepted", lambda ch: ALL)
    monkeypatch.setattr(mig, "_sync_level", lambda ch: 2)
    clock = _Clock()
    real = mig._await_mutations
    monkeypatch.setattr(
        mig,
        "_await_mutations",
        lambda *a, **kw: real(*a, **kw, sleep=clock.sleep, clock=clock.monotonic),
    )
    f = tmp_path / "1999_mat.sql"
    f.write_text(
        "ALTER TABLE loom.t ADD COLUMN c UInt8;\n\nALTER TABLE loom.t MATERIALIZE COLUMN c;\n"
    )
    ch = _MutationsCH(answers)
    rc = mig._apply_pending(ch, [f], dry=False, lease=None)
    return rc, ch


def test_a_failed_mutation_stops_the_file_unrecorded(monkeypatch, tmp_path, capsys) -> None:
    bad = ("t", "0000000009", "MATERIALIZE COLUMN c", "Code: 6. Cannot parse")
    rc, ch = _pending_run(monkeypatch, tmp_path, [[], [bad]])
    assert rc == 1 and ch.inserts == []
    assert [s for _, s in ch.commands] == [
        {"alter_sync": 2},
        {"alter_sync": 2, "mutations_sync": 2},
    ]
    err = capsys.readouterr().err
    assert "✗ 1999_mat.sql: MutationError" in err and "Cannot parse" in err


def test_a_still_running_mutation_times_out_unrecorded(monkeypatch, tmp_path, capsys) -> None:
    run = ("t", "0000000009", "MATERIALIZE COLUMN c", "")
    rc, ch = _pending_run(monkeypatch, tmp_path, [[], [run]], wait_s=20)
    assert rc == 1 and ch.inserts == []
    assert "still running after 20 s" in capsys.readouterr().err


def test_a_finished_mutation_records_the_file(monkeypatch, tmp_path) -> None:
    rc, ch = _pending_run(monkeypatch, tmp_path, [[]])
    assert rc == 0 and len(ch.inserts) == 1
    assert len(ch.reads) == 2  # the snapshot before, the check after


def test_tolerate_error_still_skips_and_the_file_records(monkeypatch, tmp_path) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "migrate_mutation_wait_s", 30)
    monkeypatch.setattr(mig, "_nlp_available", lambda ch: True)
    monkeypatch.setattr(mig, "_codec_settings", lambda ch: mig.NO_CODEC)
    monkeypatch.setattr(mig, "_sync_settings_accepted", lambda ch: ALL)
    monkeypatch.setattr(mig, "_sync_level", lambda ch: 2)

    class Refuses(_MutationsCH):
        def command(self, sql, settings=None):
            super().command(sql, settings)
            raise RuntimeError("Code: 472. DB::Exception: Setting is readonly. (READONLY_SETTING)")

    f = tmp_path / "1999_tol.sql"
    f.write_text(
        "-- loom: tolerate-error 472\nALTER TABLE loom.t MODIFY SETTING index_granularity = 256;\n"
    )
    ch = Refuses([[]])
    assert mig._apply_pending(ch, [f], dry=False, lease=None) == 0
    assert len(ch.inserts) == 1


def test_the_level_is_3_where_the_server_takes_it_and_2_otherwise() -> None:
    class _Level:
        def __init__(self, ok: bool) -> None:
            self.ok = ok

        def query(self, sql, settings=None):
            if settings and settings.get("alter_sync") == 3 and not self.ok:
                raise RuntimeError("Code: 36. BAD_ARGUMENTS")
            return None

    assert mig._sync_level(_Level(True)) == 3
    assert mig._sync_level(_Level(False)) == 2
    policy = mig.SyncPolicy(ALL, 3)
    assert policy.settings_for("ALTER TABLE loom.t MATERIALIZE COLUMN c") == {
        "alter_sync": 3,
        "mutations_sync": 3,
    }
    assert mig.NO_SYNC.settings_for("ALTER TABLE loom.t ADD COLUMN c Int8") == {}
