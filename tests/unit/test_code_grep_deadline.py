"""code.grep under `deadline_ms`, and mode=message's literal fallback.

- a bounded scan carries `max_execution_time` with `timeout_overflow_mode =
  'break'`, so ClickHouse hands back the rows read so far; a scan that ran to
  its bound makes the answer `partial` with a `partial_reason`, never an error;
- the second statement (the regex retry, the message literal fallback) and the
  neighbours read are not started once the budget is spent;
- no deadline: no bound on any statement and no new field on the wire;
- mode=message: a code line (no statement template matches it) is grepped as a
  literal and says so with `fallback: literal`; a rendered message still takes
  the template path.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import patch

import pytest

from server.code.app.ttl_cache import clear_all
from server.code.domain import deadline as code_deadline
from server.code.domain.index import grep_sql
from server.settings import settings

CONVERT_FIELD = """    if (!parsed)
        throw Exception(ErrorCodes::TYPE_MISMATCH, "Cannot convert string {} to type {}",
            src.get<String>(), type.getName());
"""

FACTORY = """std::unique_ptr<IInterpreter> InterpreterFactory::get(ASTPtr & query)
{
    if (settings.allow_experimental_analyzer)
        return std::make_unique<InterpreterSelectQueryAnalyzer>(query, context, options);
    return nullptr;
}
"""


def _chunk(path: str, start: int, scope: str, content: str) -> dict[str, Any]:
    return {
        "path": path,
        "start_line": start,
        "end_line": start + content.count("\n"),
        "scope": scope,
        "content": content,
        "symbol_ids": [],
        "shingles": 1,
        "admitted_files": 1,
    }


CONVERT = _chunk(
    "src/Interpreters/convertFieldToType.cpp", 400, "DB::convertFieldToType", CONVERT_FIELD
)
FACTORY_CHUNK = _chunk("src/Interpreters/InterpreterFactory.cpp", 20, "DB::get", FACTORY)


class _Repo:
    """Each scan answers after `delay_s`, or at its bound when that comes first — a
    bounded ClickHouse scan with `timeout_overflow_mode = 'break'` returns there with
    the rows it read (`partial_rows`)."""

    def __init__(
        self,
        *,
        literal: list[dict[str, Any]] | None = None,
        regex: list[dict[str, Any]] | None = None,
        message: list[dict[str, Any]] | None = None,
        delay_s: float = 0.0,
        partial_rows: list[dict[str, Any]] | None = None,
    ) -> None:
        self.literal, self.regex, self.message = literal or [], regex or [], message or []
        self.delay_s, self.partial_rows = delay_s, partial_rows
        self.calls: list[tuple[str, bool, float | None]] = []

    async def _scan(self, rows: list[dict[str, Any]], bound: float | None) -> list[dict[str, Any]]:
        if bound is not None and bound < self.delay_s:
            await asyncio.sleep(bound)
            return list(self.partial_rows or [])
        await asyncio.sleep(self.delay_s)
        return rows

    async def grep_chunks(self, org, ns, pattern, *, regex=False, **kw):
        bound = kw.get("max_execution_s")
        self.calls.append(("chunks", regex, bound))
        return await self._scan(self.regex if regex else self.literal, bound)

    async def grep_files(self, org, ns, pattern, *, regex=False, **kw):
        bound = kw.get("max_execution_s")
        self.calls.append(("files", regex, bound))
        rows = await self._scan(self.regex if regex else self.literal, bound)
        return [{"path": r["path"], "chunks": 1, "first_line": 1} for r in rows], len(rows)

    async def grep_message_chunks(self, org, ns, needles, **kw):
        bound = kw.get("max_execution_s")
        self.calls.append(("message", False, bound))
        rows = await self._scan(self.message, bound)
        return rows, len({r["path"] for r in rows})

    async def chunks_of_path(self, org, ns, path, *, limit=400):
        return []

    async def symbols_by_ids(self, org, ns, ids):
        return []

    async def functions_for_paths(self, *a, **k):
        return []


@pytest.fixture(autouse=True)
def _intel_on(monkeypatch):
    monkeypatch.setattr(settings, "code_intel_enabled", True)
    monkeypatch.setattr(settings, "code_org", "o")
    monkeypatch.setattr(settings, "code_namespace", "n")
    clear_all()


async def _as_of(*a, **k):
    return {}


def _grep(repo: _Repo, **kw):
    from server.code.api.models_grep import CodeGrepInput
    from server.code.app import grep

    traces: list[dict[str, Any]] = []

    async def go():
        with (
            patch.object(grep, "CodeRepo", return_value=repo),
            patch.object(grep, "index_as_of", new=_as_of),
            patch.object(grep, "schedule_code_trace", new=lambda **k: traces.append(k)),
        ):
            return await grep.handle_code_grep(CodeGrepInput(org="o", namespace="n", **kw))

    out = asyncio.run(go())
    return out, traces


# --------------------------------------------------------------- no deadline


def test_no_deadline_bounds_no_statement_and_adds_no_wire_field() -> None:
    repo = _Repo(literal=[FACTORY_CHUNK])
    out, _ = _grep(repo, pattern="InterpreterSelectQueryAnalyzer")
    assert repo.calls == [("chunks", False, None)]
    data = json.loads(out.model_dump_json())
    assert not {"partial", "partial_reason", "fallback"} & set(data)
    assert [m["line"] for m in out.matches] == [23]


def test_no_deadline_in_files_and_message_mode_is_unbounded_too() -> None:
    repo = _Repo(literal=[FACTORY_CHUNK])
    _grep(repo, pattern="InterpreterSelectQueryAnalyzer", mode="files")
    repo2 = _Repo(message=[CONVERT])
    _grep(repo2, pattern="Cannot convert string 'x' to type DateTime64(3)", mode="message")
    assert all(bound is None for *_, bound in repo.calls + repo2.calls)


# --------------------------------------------------------------- the bound


def test_a_scan_stopped_at_the_deadline_returns_its_rows_marked_partial() -> None:
    repo = _Repo(literal=[FACTORY_CHUNK, CONVERT], delay_s=5.0, partial_rows=[FACTORY_CHUNK])
    out, _ = _grep(repo, pattern="return", deadline_ms=400)
    (kind, regex, bound), *_ = repo.calls
    assert kind == "chunks" and bound is not None and 0.05 <= bound < 0.4
    assert {m["path"] for m in out.matches} == {"src/Interpreters/InterpreterFactory.cpp"}
    assert out.partial is True
    assert out.partial_reason.startswith("deadline 400 ms: scan stopped at the deadline")
    data = json.loads(out.model_dump_json())
    assert data["partial"] is True and "fallback" not in data


def test_a_scan_inside_the_deadline_is_complete() -> None:
    repo = _Repo(literal=[FACTORY_CHUNK], delay_s=0.0)
    out, _ = _grep(repo, pattern="InterpreterSelectQueryAnalyzer", deadline_ms=5000)
    assert repo.calls[0][2] is not None  # bounded, but finished well inside
    assert out.partial is False and out.partial_reason == "" and out.matches


def test_files_mode_is_bounded_and_marked_partial() -> None:
    repo = _Repo(literal=[FACTORY_CHUNK, CONVERT], delay_s=5.0, partial_rows=[CONVERT])
    out, _ = _grep(repo, pattern="return", mode="files", deadline_ms=300)
    assert [f["path"] for f in out.files] == [CONVERT["path"]]
    assert out.partial and "scan stopped" in out.partial_reason


def test_the_regex_retry_is_not_started_once_the_budget_is_spent() -> None:
    # the literal read eats the budget and finds nothing: no second statement
    repo = _Repo(literal=[], regex=[FACTORY_CHUNK], delay_s=5.0, partial_rows=[])
    out, _ = _grep(repo, pattern="ALIAS|isAlias", deadline_ms=250)
    assert [c[:2] for c in repo.calls] == [("chunks", False)]
    assert out.matches == [] and out.partial
    assert "regex retry skipped" in out.partial_reason


def test_values_under_the_floor_read_as_the_floor_and_the_cap_holds() -> None:
    from pydantic import ValidationError

    from server.code.api.models_grep import CodeGrepInput

    assert code_deadline.start(50).ms == code_deadline.MIN_MS  # type: ignore[union-attr]
    assert code_deadline.start(0) is None and code_deadline.start(None) is None
    with pytest.raises(ValidationError):
        CodeGrepInput(pattern="xx", deadline_ms=60001)
    with pytest.raises(ValidationError):
        CodeGrepInput(pattern="xx", deadline_ms=-1)


def test_break_settings_ride_every_scan_builder() -> None:
    extra = code_deadline.break_settings(1.5)
    assert extra == ", max_execution_time = 1.500, timeout_overflow_mode = 'break'"
    assert code_deadline.break_settings(None) == ""
    for sql in (
        grep_sql.grep_chunks_sql("1", "", "", extra),
        grep_sql.grep_files_sql("1", "", "", extra),
        grep_sql.grep_message_sql("", "", extra),
    ):
        flat = " ".join(sql.split())
        assert flat.endswith(
            "SETTINGS ignore_data_skipping_indices = 'idx_code_chunks_content_ng', "
            "max_execution_time = 1.500, timeout_overflow_mode = 'break'"
        )
        assert "PREWHERE" not in sql
    assert "max_execution_time" not in grep_sql.grep_message_sql("", "")


def test_broke_reads_a_wall_at_the_bound() -> None:
    assert code_deadline.broke(0.95, 1.0) and not code_deadline.broke(0.5, 1.0)
    assert not code_deadline.broke(10.0, None)


# --------------------------------------------------------------- message fallback


CODE_LINES = [
    "return std::make_unique<InterpreterSelectQueryAnalyzer>(",
    "settings.allow_experimental_analyzer",
]


@pytest.mark.parametrize("pattern", CODE_LINES)
def test_a_code_line_in_message_mode_returns_literal_hits_with_fallback(pattern: str) -> None:
    repo = _Repo(literal=[FACTORY_CHUNK], message=[FACTORY_CHUNK])
    out, traces = _grep(repo, pattern=pattern, mode="message")
    assert out.fallback == "literal"
    assert out.verdict == "no_template_match"
    assert out.matches and out.matches[0]["path"].endswith("InterpreterFactory.cpp")
    assert pattern in out.matches[0]["text"]
    assert "mode 'message' is for rendered error or log text" in out.note
    assert ("chunks", False, None) in repo.calls
    data = json.loads(out.model_dump_json())
    assert data["fallback"] == "literal" and data["verdict"] == "no_template_match"
    # one trace row for the call, under the fallback's route
    assert len(traces) == 1 and traces[0]["route"].endswith("+literal_fallback")


def test_a_rendered_message_still_takes_the_template_path() -> None:
    repo = _Repo(literal=[FACTORY_CHUNK], message=[CONVERT])
    out, traces = _grep(
        repo, pattern="Cannot convert string 'abc' to type DateTime64(3)", mode="message"
    )
    assert out.verdict == "raised_at" and out.fallback == ""
    assert [c[0] for c in repo.calls] == ["message"]
    assert out.matches[0]["template"].startswith("Cannot convert string {} to type {}")
    assert "fallback" not in json.loads(out.model_dump_json())
    assert len(traces) == 1 and traces[0]["route"] == "grep_message"


def test_a_literal_fallback_that_finds_nothing_keeps_the_message_answer() -> None:
    repo = _Repo(literal=[], message=[CONVERT])
    out, traces = _grep(repo, pattern="auto cte_options = select_query_options.sub", mode="message")
    assert out.fallback == "literal" and out.verdict == "no_template_match"
    assert out.matches == [] and "matched no line either" in out.note
    assert len(traces) == 1


def test_the_literal_fallback_is_skipped_when_the_scan_spent_the_budget() -> None:
    repo = _Repo(literal=[FACTORY_CHUNK], message=[FACTORY_CHUNK], delay_s=5.0, partial_rows=[])
    out, _ = _grep(
        repo,
        pattern="Cannot build the analyzer interpreter for this query shape",
        mode="message",
        deadline_ms=250,
    )
    assert [c[0] for c in repo.calls] == ["message"]
    assert out.fallback == "" and out.verdict == "no_template_match" and out.partial
    assert "scan stopped at the deadline" in out.partial_reason
    assert "literal fallback skipped" in out.partial_reason


def test_a_long_deadline_never_raises_the_caller_shaped_ceiling(monkeypatch) -> None:
    monkeypatch.setattr(settings, "ch_caller_shaped_max_execution_s", 30.0)
    repo = _Repo(literal=[FACTORY_CHUNK])
    _grep(repo, pattern="InterpreterSelectQueryAnalyzer", deadline_ms=48_000)
    bound = repo.calls[0][2]
    assert bound is not None and bound <= 30.0
    sql = grep_sql.grep_files_sql("1", "", "", code_deadline.break_settings(bound))
    assert "max_execution_time = 30.000" in sql


def test_no_caller_shaped_ceiling_clamps_to_the_read_class_timeout(monkeypatch) -> None:
    monkeypatch.setattr(settings, "ch_caller_shaped_max_execution_s", 0.0)
    monkeypatch.setattr(settings, "ch_timeout_read_s", 30.0)
    repo = _Repo(literal=[FACTORY_CHUNK])
    _grep(repo, pattern="InterpreterSelectQueryAnalyzer", deadline_ms=60_000)
    bound = repo.calls[0][2]
    assert bound is not None and bound <= 30.0
    sql = grep_sql.grep_files_sql("1", "", "", code_deadline.break_settings(bound))
    assert "max_execution_time = 30.000" in sql
