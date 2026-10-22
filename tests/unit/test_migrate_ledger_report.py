"""`loom migrate --dry-run` reports the ledger against the repo:
pending files with their SHA-256, ledger-only rows (applied under a filename
the repo no longer has), checksum drift — and `--strict` turns the last two
into exit 1 for the deploy preflight and CI. Nothing is applied or recorded."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from server.core.schema import migrate as _migrate
from server.core.schema.vector_codec_ddl import CODEC_SETTINGS_SQL


class _Result:
    def __init__(self, rows):
        self.result_rows = rows


class _CH:
    """Fake client: a ledger, read-only otherwise."""

    def __init__(self, ledger: dict[str, str]) -> None:
        self.ledger = ledger
        self.commands: list[str] = []
        self.inserts: list[tuple] = []

    def query(self, sql: str, *a, **k):
        if "WHERE filename" in sql:  # the _record belt: not recorded yet
            return _Result([])
        if "_migrations" in sql:
            return _Result([(name, cs) for name, cs in self.ledger.items()])
        if sql == CODEC_SETTINGS_SQL:
            return _Result([])  # no codec settings: the plain layout
        return _Result([])

    def command(self, stmt: str, settings=None):
        self.commands.append(stmt)

    def insert(self, *a, **k):
        self.inserts.append(a)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


@pytest.fixture
def mig_dir(tmp_path: Path) -> Path:
    d = tmp_path / "migrations"
    d.mkdir()
    (d / "000_init.sql").write_text("CREATE TABLE a (x Int8) ENGINE = Memory;\n")
    (d / "010_two.sql").write_text("ALTER TABLE a ADD COLUMN y Int8;\n")
    (d / "020_pending.sql").write_text(
        "ALTER TABLE a ADD COLUMN z Int8;\n\nALTER TABLE a ADD COLUMN w Int8;\n"
    )
    return d


def _files(d: Path) -> list[Path]:
    return sorted(d.glob("*.sql"))


def test_ledger_report_pending_ledger_only_and_drift(mig_dir: Path) -> None:
    applied = {
        "000_init.sql": _sha((mig_dir / "000_init.sql").read_text()),
        "010_two.sql": "deadbeef" * 8,  # edited after apply
        "005_renamed_before_merge.sql": _sha("whatever"),
    }
    rep = _migrate.ledger_report(applied, _files(mig_dir))
    assert rep.pending == ("020_pending.sql",)
    assert rep.ledger_only == ("005_renamed_before_merge.sql",)
    assert rep.drift == ("010_two.sql",)
    assert not rep.clean


def test_ledger_report_clean_when_ledger_equals_repo(mig_dir: Path) -> None:
    applied = {f.name: _sha(f.read_text()) for f in _files(mig_dir)}
    rep = _migrate.ledger_report(applied, _files(mig_dir))
    assert rep == _migrate.LedgerReport(pending=(), ledger_only=(), drift=())
    assert rep.clean


def test_empty_checksum_is_not_drift(mig_dir: Path) -> None:
    # Rows recorded before checksums were stored read as ''; they are applied, not drifted.
    rep = _migrate.ledger_report({"000_init.sql": ""}, _files(mig_dir))
    assert rep.drift == ()


class _Unleased:
    outcome, held, waited_s = "unleased", False, 0.0

    def renew_if_due(self) -> None: ...

    def release(self) -> None: ...


def _main(monkeypatch, ch: _CH, mig_dir: Path, *args: str) -> int:
    monkeypatch.setattr(_migrate, "client", lambda: ch)
    monkeypatch.setattr(_migrate, "_nlp_available", lambda ch: True)
    # The cross-container lease has its own tests; here it is unleased.
    monkeypatch.setattr(_migrate, "_acquire_lease", lambda ch: _Unleased())
    return _migrate.main(["--migrations-dir", str(mig_dir), *args])


def test_dry_run_lists_pending_with_sha_and_flags_ledger_only_without_applying(
    monkeypatch, mig_dir: Path, capsys
) -> None:
    ch = _CH(
        {
            "000_init.sql": _sha((mig_dir / "000_init.sql").read_text()),
            "010_two.sql": _sha((mig_dir / "010_two.sql").read_text()),
            "076_session_summaries_derived_from.sql": _sha("x"),
        }
    )
    rc = _main(monkeypatch, ch, mig_dir, "--dry-run")
    out = capsys.readouterr().out
    assert rc == 0  # not strict: ledger-only is reported, not fatal
    assert "pending: 1 · ledger-only: 1 · checksum drift: 0" in out
    assert "⚠ ledger-only: 076_session_summaries_derived_from.sql" in out
    sha = _sha((mig_dir / "020_pending.sql").read_text())[:12]
    assert f"→ 020_pending.sql  sha256 {sha}…  pending" in out
    assert "2 statement(s) — dry-run, not executed" in out
    assert "dry-run — 1 pending, nothing applied" in out
    assert ch.commands == [] and ch.inserts == []


def test_strict_dry_run_exits_1_on_ledger_only_and_drift(
    monkeypatch, mig_dir: Path, capsys
) -> None:
    ch = _CH(
        {
            "000_init.sql": _sha((mig_dir / "000_init.sql").read_text()),
            "010_two.sql": "0" * 64,
            "020_pending.sql": _sha((mig_dir / "020_pending.sql").read_text()),
        }
    )
    rc = _main(monkeypatch, ch, mig_dir, "--dry-run", "--strict")
    out = capsys.readouterr().out
    assert rc == 1
    assert "⚠ checksum drift: 010_two.sql" in out
    assert "no pending migrations" in out
    assert ch.inserts == []


def test_strict_dry_run_exits_0_when_ledger_is_a_subset_and_pending_is_normal(
    monkeypatch, mig_dir: Path, capsys
) -> None:
    ch = _CH({f.name: _sha(f.read_text()) for f in _files(mig_dir)[:2]})
    rc = _main(monkeypatch, ch, mig_dir, "--dry-run", "--strict")
    assert rc == 0
    assert "pending: 1 · ledger-only: 0 · checksum drift: 0" in capsys.readouterr().out


def test_status_prints_the_report_and_honours_strict(monkeypatch, mig_dir: Path, capsys) -> None:
    ch = _CH({"999_gone.sql": _sha("gone")})
    assert _main(monkeypatch, ch, mig_dir, "--status") == 0
    out = capsys.readouterr().out
    assert "⚠ ledger-only: 999_gone.sql" in out and "[ ] 020_pending.sql" in out
    assert _main(monkeypatch, ch, mig_dir, "--status", "--strict") == 1


def test_apply_path_is_unchanged_by_the_report(monkeypatch, mig_dir: Path, capsys) -> None:
    ch = _CH({f.name: _sha(f.read_text()) for f in _files(mig_dir)[:2]})
    monkeypatch.setattr(_migrate, "_codec_settings", lambda ch: _migrate.NO_CODEC)
    rc = _main(monkeypatch, ch, mig_dir)
    out = capsys.readouterr().out
    assert rc == 0 and "done — 1 applied" in out
    assert len(ch.commands) == 2 and len(ch.inserts) == 1
    assert ch.inserts[0][1][0][0] == "020_pending.sql"


# ---- renumbered files ---------------------------------------------------


def test_a_ledger_row_with_a_repo_files_checksum_is_a_rename_not_ledger_only(
    mig_dir: Path,
) -> None:
    """A branch migration renumbered past main's after it ran: the old row's
    checksum is the new file's, so the ledger is clean and the new name is
    recorded without running again."""
    pending = (mig_dir / "020_pending.sql").read_text()
    applied = {
        "000_init.sql": _sha((mig_dir / "000_init.sql").read_text()),
        "010_two.sql": _sha((mig_dir / "010_two.sql").read_text()),
        "019_pending.sql": _sha(pending),
    }
    rep = _migrate.ledger_report(applied, _files(mig_dir))
    assert rep.renamed == (("019_pending.sql", "020_pending.sql"),)
    assert rep.ledger_only == () and rep.clean
    # An edited file under the old name is not the same text: still ledger-only.
    applied["019_pending.sql"] = _sha(pending + "-- edited\n")
    rep = _migrate.ledger_report(applied, _files(mig_dir))
    assert rep.renamed == () and rep.ledger_only == ("019_pending.sql",)


def test_the_renamed_file_is_recorded_without_running(monkeypatch, mig_dir: Path, capsys) -> None:
    ch = _CH(
        {
            "000_init.sql": _sha((mig_dir / "000_init.sql").read_text()),
            "010_two.sql": _sha((mig_dir / "010_two.sql").read_text()),
            "019_pending.sql": _sha((mig_dir / "020_pending.sql").read_text()),
        }
    )
    monkeypatch.setattr(_migrate, "_codec_settings", lambda ch: _migrate.NO_CODEC)
    assert _main(monkeypatch, ch, mig_dir, "--dry-run", "--strict") == 0
    rc = _main(monkeypatch, ch, mig_dir)
    out = capsys.readouterr().out
    assert rc == 0 and "020_pending.sql: already ran under an older name" in out
    assert ch.commands == []  # nothing ran
    assert ch.inserts[0][1][0][0] == "020_pending.sql"


def test_a_repeated_text_is_matched_to_its_own_copy_by_name(monkeypatch, mig_dir: Path) -> None:
    """Two files share a text on purpose (a second backfill pass). The repeat ran
    under its branch number: that row is the repeat's, not the first copy's, so
    the repeat is recorded and nothing runs twice."""
    repeated = (mig_dir / "010_two.sql").read_text()
    (mig_dir / "030_two_again.sql").write_text(repeated)
    ledger = {
        "000_init.sql": _sha((mig_dir / "000_init.sql").read_text()),
        "010_two.sql": _sha(repeated),
        "020_pending.sql": _sha((mig_dir / "020_pending.sql").read_text()),
        "029_two_again.sql": _sha(repeated),
    }
    rep = _migrate.ledger_report(ledger, _files(mig_dir))
    assert rep.renamed == (("029_two_again.sql", "030_two_again.sql"),) and rep.clean
    ch = _CH(ledger)
    monkeypatch.setattr(_migrate, "_codec_settings", lambda ch: _migrate.NO_CODEC)
    assert _main(monkeypatch, ch, mig_dir) == 0
    assert ch.commands == [] and [i[1][0][0] for i in ch.inserts] == ["030_two_again.sql"]


# ---- baselines ----------------------------------------------------------


def _write(tmp_path, names_and_text):
    out = []
    for name, text in names_and_text:
        p = tmp_path / name
        p.write_text(text)
        out.append(p)
    return out


def test_a_fresh_database_runs_the_baseline(tmp_path) -> None:
    from server.core.schema.migrate import ledger_report

    files = _write(
        tmp_path,
        [("000_m.sql", "SELECT 1;"), ("005_base.sql", "-- loom: baseline covers 5\nSELECT 1;")],
    )
    r = ledger_report({"000_m.sql": ""}, files)
    assert r.pending == ("005_base.sql",) and r.baselined == () and r.clean


def test_a_database_that_ran_the_replaced_files_records_the_baseline(tmp_path) -> None:
    from server.core.schema.migrate import ledger_report

    files = _write(
        tmp_path,
        [("000_m.sql", "SELECT 1;"), ("005_base.sql", "-- loom: baseline covers 5\nSELECT 1;")],
    )
    applied = {"000_m.sql": "", "001_old.sql": "x", "003_drop.sql": "y", "005_drop.sql": "z"}
    r = ledger_report(applied, files)
    assert r.baselined == ("005_base.sql",)
    assert r.superseded == ("001_old.sql", "003_drop.sql", "005_drop.sql")
    assert r.ledger_only == () and r.clean


def test_a_database_that_stopped_partway_is_stranded_not_recorded(tmp_path) -> None:
    """The ledger holds files the baseline replaces but not the newest one:
    recording the baseline would claim a schema the database lacks, and
    running it would skip the replaced files it never applied."""
    from server.core.schema.migrate import ledger_report

    files = _write(
        tmp_path,
        [("000_m.sql", "SELECT 1;"), ("005_base.sql", "-- loom: baseline covers 5\nSELECT 1;")],
    )
    r = ledger_report({"000_m.sql": "", "001_old.sql": "x", "003_add.sql": "y"}, files)
    assert r.stranded == ("005_base.sql",)
    assert r.baselined == () and r.pending == () and not r.clean


def test_migrate_refuses_a_stranded_database_before_applying_anything(
    tmp_path, monkeypatch, capsys
) -> None:
    from server.core.schema import migrate as m

    _write(
        tmp_path,
        [("000_m.sql", "SELECT 1;"), ("005_base.sql", "-- loom: baseline covers 5\nSELECT 1;")],
    )
    monkeypatch.setattr(m, "client", lambda: object())
    monkeypatch.setattr(m, "applied_migrations", lambda ch: {"000_m.sql": "", "003_add.sql": "y"})

    def _never(*a, **k):
        raise AssertionError("a stranded database must not be migrated")

    monkeypatch.setattr(m, "_apply_under_lease", _never)
    monkeypatch.setattr(m, "_apply_pending", _never)
    assert m.main(["--migrations-dir", str(tmp_path)]) == 1
    assert "holds only part of what the baseline replaces" in capsys.readouterr().out


def test_a_row_past_the_baseline_is_still_ledger_only(tmp_path) -> None:
    from server.core.schema.migrate import ledger_report

    files = _write(tmp_path, [("005_base.sql", "-- loom: baseline covers 5\nSELECT 1;")])
    r = ledger_report({"003_old.sql": "x", "007_renamed.sql": "y"}, files)
    assert r.superseded == ("003_old.sql",)
    assert r.ledger_only == ("007_renamed.sql",) and not r.clean


def test_a_baselined_file_is_recorded_without_running(tmp_path, monkeypatch) -> None:
    from server.core.schema import migrate as _migrate

    base = tmp_path / "005_base.sql"
    base.write_text(
        "-- loom: baseline covers 5\nCREATE TABLE IF NOT EXISTS loom.t (a Int8) ENGINE = Memory;"
    )
    recorded: list[str] = []

    class _CH:
        def command(self, *a, **k):
            raise AssertionError("a baselined file must not run")

    monkeypatch.setattr(_migrate, "_record", lambda ch, name, cs: recorded.append(name))
    monkeypatch.setattr(_migrate, "_nlp_available", lambda ch: False)
    monkeypatch.setattr(_migrate, "_codec_settings", lambda ch: _migrate.NO_CODEC)
    rc = _migrate._apply_pending(
        _CH(), [base], dry=False, lease=None, baselined=frozenset({"005_base.sql"})
    )
    assert rc == 0 and recorded == ["005_base.sql"]


def test_schema_status_does_not_call_a_covered_baseline_pending() -> None:
    from server.core.schema.status import compare

    out = compare(
        ["000_m.sql", "005_base.sql"],
        ["000_m.sql", "001_old.sql", "004_x.sql"],
        {"005_base.sql": 5},
    )
    assert out["pending"] == [] and out["ledger_only"] == [] and out["superseded"] == 2


# ---- --target -----------------------------------------------------------


def test_target_classifies_the_ledger_against_every_file(
    monkeypatch, mig_dir: Path, capsys
) -> None:
    """Files numbered above the target that the ledger holds are applied, not
    ledger-only: a strict dry-run with a target passes on a healthy ledger."""
    ch = _CH({f.name: _sha(f.read_text()) for f in _files(mig_dir)})
    rc = _main(monkeypatch, ch, mig_dir, "--target", "10", "--dry-run", "--strict")
    out = capsys.readouterr().out
    assert rc == 0
    assert "ledger-only: 0" in out and "⚠ ledger-only" not in out


def test_target_limits_only_what_is_applied(monkeypatch, mig_dir: Path, capsys) -> None:
    ch = _CH({"000_init.sql": _sha((mig_dir / "000_init.sql").read_text())})
    monkeypatch.setattr(_migrate, "_codec_settings", lambda ch: _migrate.NO_CODEC)
    rc = _main(monkeypatch, ch, mig_dir, "--target", "10")
    assert rc == 0 and "done — 1 applied" in capsys.readouterr().out
    assert [row[1][0][0] for row in ch.inserts] == ["010_two.sql"]


def test_target_below_a_baseline_never_strands_it(tmp_path, monkeypatch, capsys) -> None:
    files = _write(
        tmp_path,
        [("000_m.sql", "SELECT 1;"), ("005_base.sql", "-- loom: baseline covers 5\nSELECT 1;")],
    )
    ch = _CH({f.name: _sha(f.read_text()) for f in files})
    rc = _main(monkeypatch, ch, tmp_path, "--target", "0", "--dry-run", "--strict")
    out = capsys.readouterr().out
    assert rc == 0 and "✗" not in out and "⚠" not in out
