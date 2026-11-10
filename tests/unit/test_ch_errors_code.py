"""One parse of the `Code: <n>.` a ClickHouse server error carries."""

from __future__ import annotations

from server.core.ch_errors import error_code, error_name


def test_error_code_reads_the_server_code_or_none() -> None:
    assert error_code(RuntimeError("Code: 60. DB::Exception: Table loom.x does not exist")) == 60
    assert error_code("HTTPDriver received Code: 159. Timeout exceeded") == 159
    assert error_code(RuntimeError("code: 159, no server code")) is None
    assert error_code(ValueError("")) is None


def test_error_name_reads_the_name_before_the_version_or_none() -> None:
    cloud = "Code: 36. DB::Exception: Invalid password. (BAD_ARGUMENTS) (version 26.10.2.93)"
    assert error_name(RuntimeError(cloud)) == "BAD_ARGUMENTS"
    assert (
        error_name("Code: 60. DB::Exception: Table loom.x does not exist (UNKNOWN_TABLE)") is None
    )
    assert error_name(ValueError("")) is None
