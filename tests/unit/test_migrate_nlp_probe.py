"""The migration runner's NLP capability probe reads `system.settings`
before it lemmatizes. On ClickHouse Cloud `allow_experimental_nlp_functions`
is readonly at 0, and a lemmatize probe there would leave a refused statement
in the query log. Where the flag is writable the lemmatize probe decides (OSS
may lack the dictionary). Every outcome is one printed line."""

from __future__ import annotations

from server.core.schema import migrate as _migrate


class _Result:
    def __init__(self, rows):
        self.result_rows = rows


class _CH:
    def __init__(self, settings_rows, *, lemmatize_ok: bool = True, settings_raise: bool = False):
        self._rows = settings_rows
        self._ok = lemmatize_ok
        self._raise = settings_raise
        self.queries: list[str] = []
        self.commands: list[tuple[str, dict | None]] = []

    def query(self, sql: str):
        self.queries.append(sql)
        if self._raise:
            raise RuntimeError("Unknown table system.settings")
        return _Result(self._rows)

    def command(self, sql: str, settings=None):
        self.commands.append((sql, settings))
        if not self._ok:
            raise RuntimeError("Setting allow_experimental_nlp_functions is readonly")
        return ""


def test_readonly_zero_answers_without_lemmatizing(capsys) -> None:
    ch = _CH([("0", 1)])
    assert _migrate._nlp_available(ch) is False
    assert ch.commands == []  # no lemmatize statement → no exception row in query_log
    assert len(ch.queries) == 1 and "system.settings" in ch.queries[0]
    out = capsys.readouterr().out
    assert out.count("\n") == 1 and "is readonly at 0" in out


def test_writable_flag_runs_the_lemmatize_probe() -> None:
    ch = _CH([("0", 0)])
    assert _migrate._nlp_available(ch) is True
    assert len(ch.commands) == 1
    sql, settings = ch.commands[0]
    assert "lemmatize" in sql and settings == {"allow_experimental_nlp_functions": 1}


def test_lemmatize_refusal_is_one_line_not_a_traceback(capsys) -> None:
    ch = _CH([("0", 0)], lemmatize_ok=False)
    assert _migrate._nlp_available(ch) is False
    out = capsys.readouterr().out
    assert out.count("\n") == 1 and "refused" in out and "Traceback" not in out


def test_already_enabled_readonly_flag_still_probes() -> None:
    """readonly=1 with value 1 (an operator pinned it on) is not "unavailable"."""
    ch = _CH([("1", 1)])
    assert _migrate._nlp_available(ch) is True
    assert len(ch.commands) == 1


def test_a_server_without_the_setting_row_is_treated_as_unavailable(capsys) -> None:
    ch = _CH([], settings_raise=True)
    assert _migrate._nlp_available(ch) is False
    assert ch.commands == []
    assert "system.settings" in capsys.readouterr().out


def test_no_row_falls_through_to_the_probe() -> None:
    ch = _CH([])
    assert _migrate._nlp_available(ch) is True
    assert len(ch.commands) == 1
