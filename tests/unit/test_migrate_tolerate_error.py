"""Migration files can declare an error code the server may answer with
(`-- loom: tolerate-error <code>`); that statement is skipped, every other
failure still aborts the file."""

from __future__ import annotations

from pathlib import Path

import pytest

from server.core.schema import migrate as _migrate


class _CH:
    def __init__(self, fail_with: str | None = None) -> None:
        self.fail_with, self.calls = fail_with, []

    def command(self, stmt, settings=None):
        self.calls.append(stmt)
        if self.fail_with:
            raise RuntimeError(self.fail_with)


def _file(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "999_probe.sql"
    p.write_text(body)
    return p


def test_directive_is_parsed_per_file() -> None:
    assert _migrate._tolerated_errors(
        "-- loom: tolerate-error 472\nALTER TABLE t MODIFY SETTING x = 1;"
    ) == {472}
    assert _migrate._tolerated_errors(
        "--loom:tolerate-error 472 -- why\n-- loom: tolerate-error 60\nSELECT 1;"
    ) == {472, 60}
    assert (
        _migrate._tolerated_errors("-- tolerate-error 472 without the loom prefix\nSELECT 1;")
        == set()
    )


def test_consecutive_directives_each_count() -> None:
    sql = "-- loom: tolerate-error 999\n-- loom: tolerate-error 139\n-- loom: tolerate-error 36\nSELECT 1;"
    assert _migrate._tolerated_errors(sql) == {999, 139, 36}


def test_tolerated_code_is_skipped_and_the_file_continues(tmp_path: Path, capsys) -> None:
    ch = _CH(
        fail_with="Code: 472. DB::Exception: Setting 'index_granularity' is readonly for storage 'ReplacingMergeTree'. (READONLY_SETTING)"
    )
    path = _file(
        tmp_path,
        "-- loom: tolerate-error 472\nALTER TABLE loom.t MODIFY SETTING index_granularity = 256;",
    )
    _migrate._apply_file(ch, path, dry=False)
    assert len(ch.calls) == 1
    out = capsys.readouterr().out
    assert "skipped" in out and "472" in out


def test_other_codes_still_abort(tmp_path: Path) -> None:
    ch = _CH(fail_with="Code: 60. DB::Exception: Table loom.t does not exist. (UNKNOWN_TABLE)")
    path = _file(
        tmp_path,
        "-- loom: tolerate-error 472\nALTER TABLE loom.t MODIFY SETTING index_granularity = 256;",
    )
    with pytest.raises(RuntimeError):
        _migrate._apply_file(ch, path, dry=False)


def test_without_a_directive_the_same_error_aborts(tmp_path: Path) -> None:
    ch = _CH(fail_with="Code: 472. DB::Exception: readonly (READONLY_SETTING)")
    path = _file(tmp_path, "ALTER TABLE loom.t MODIFY SETTING index_granularity = 256;")
    with pytest.raises(RuntimeError):
        _migrate._apply_file(ch, path, dry=False)


@pytest.mark.parametrize(
    "error",
    [
        "Code: 999. Coordination::Exception: No node. (KEEPER_EXCEPTION)",
        "Code: 139. DB::Exception: KeeperMap is disabled. (NO_ELEMENTS_IN_CONFIG)",
        "Code: 36. DB::Exception: KeeperMap is disabled. (BAD_ARGUMENTS)",
    ],
)
def test_real_keeper_lease_table_runs_clean_without_a_keeper(error: str) -> None:
    ch = _CH(fail_with=error)
    real = Path(__file__).resolve().parents[2] / "schemas" / "migrations" / "002_task_leases.sql"
    _migrate._apply_file(ch, real, dry=False)
    assert len(ch.calls) == 1 and "ENGINE = KeeperMap" in ch.calls[0]
