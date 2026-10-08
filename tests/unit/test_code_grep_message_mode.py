"""Code.grep mode=message (a printed message finds the
statement that printed it), the regex-shaped-literal fallback, code.files.

The three messages are real greps that found nothing by literal; the source
windows are the statements ClickHouse raises them from, as indexed.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import patch

import pytest

from server.code.app import message_grep as mg
from server.code.app.ttl_cache import clear_all
from server.core.errors import ValidationError
from server.settings import settings

# --------------------------------------------------------------- fixtures

BLOCK_CPP = """    if (actual.name != expected.name)
        return onError<ReturnType>(code, "Block structure mismatch in {} stream: different names of columns:\\n{}\\n{}",
                                   context_description, actual.dumpStructure(), expected.dumpStructure());

    if ((actual.type && !expected.type) || (!actual.type && expected.type)
        || (actual.type && expected.type && !actual.type->equals(*expected.type)))
        return onError<ReturnType>(code, "Block structure mismatch in {} stream: different types:\\n{}\\n{}",
                                   context_description, actual.dumpStructure(), expected.dumpStructure());

    if (!actual.column || !expected.column)
        return onError<ReturnType>(code, "Block structure mismatch in {} stream: different columns:\\n{}\\n{}",
                                   context_description, actual.dumpStructure(), expected.dumpStructure());
"""

SCHEMA_CONVERTER_TUPLE = """    if (nullable_group && primitive_start == primitive_columns.size())
        throw Exception(ErrorCodes::TYPE_MISMATCH,
            "Requested type of column {} doesn't match parquet schema: physically nullable Tuple has no "
            "physical elements to read (all requested elements are missing), so its null map cannot be "
            "reconstructed; requested type is {}", node.getNameForLogging(), node.type_hint->getName());
    if (nullable_group)
        out.emplace_back();
"""

SCHEMA_CONVERTER_MAPPER = """        if (element.field_id < 1 || !last_assigned_field_id.has_value())
            throw Exception(
                ErrorCodes::ICEBERG_SPECIFICATION_VIOLATION,
                "Parquet file has column {} with field_id {} that is not in datalake metadata, and the table cannot have "
                "assigned that field id: the highest field id it ever assigned is {}",
                element.name,
                element.field_id,
                last_assigned_field_id.has_value() ? std::to_string(*last_assigned_field_id) : "none");
"""

CONVERT_FIELD = """    if (!parsed)
        throw Exception(ErrorCodes::TYPE_MISMATCH, "Cannot convert string {} to type {}",
            src.get<String>(), type.getName());
"""

ERROR_CODES_CPP = """#define APPLY_FOR_BUILTIN_ERROR_CODES(M) \\
    M(0, OK) \\
    M(1, UNSUPPORTED_METHOD) \\
    M(53, TYPE_MISMATCH) \\
    M(170, BAD_GET) \\
    M(172, CANNOT_CREATE_DIRECTORY) \\
"""


def _chunk(path: str, start: int, scope: str, content: str) -> dict[str, Any]:
    return {
        "path": path,
        "start_line": start,
        "end_line": start + content.count("\n"),
        "scope": scope,
        "content": content,
        "symbol_ids": [],
    }


SOURCE = [
    _chunk("src/Core/Block.cpp", 233, "DB::checkColumnStructure", BLOCK_CPP),
    _chunk(
        "src/Processors/Formats/Impl/Parquet/SchemaConverter.cpp",
        910,
        "DB::Parquet::SchemaConverter::processSubtreeTuple",
        SCHEMA_CONVERTER_TUPLE,
    ),
    _chunk(
        "src/Processors/Formats/Impl/Parquet/SchemaConverter.cpp",
        150,
        "DB::Parquet::SchemaConverter::useColumnMapperIfNeeded",
        SCHEMA_CONVERTER_MAPPER,
    ),
    _chunk("src/Interpreters/convertFieldToType.cpp", 400, "DB::convertFieldToType", CONVERT_FIELD),
]

MESSAGES = {
    "Requested type of column p doesn't match parquet schema": (
        "SchemaConverter.cpp",
        911,
        "Requested type of column p doesn't match parquet schema",
    ),
    "Parquet file has column legacy_col with field_id  that is not in": (
        "SchemaConverter.cpp",
        151,
        "Parquet file has column legacy_col with field_id {} that is not in",
    ),
    "Block structure mismatch in function connect between": (
        "Block.cpp",
        234,
        "Block structure mismatch in function connect between",
    ),
    "Cannot convert string  to type DateTime64(, )": (
        "convertFieldToType.cpp",
        401,
        "Cannot convert string {} to type {}",
    ),
}


# --------------------------------------------------------------- splitter


@pytest.mark.parametrize("message", sorted(MESSAGES))
def test_real_messages_split_to_templates_that_find_their_statement(message: str) -> None:
    file_, line, template = MESSAGES[message]
    parsed = mg.split_message(message)
    assert parsed.template == template
    assert parsed.needles, "every message yields at least one retrieval key"
    ranked = mg.rank_statements(parsed, SOURCE)
    assert ranked, message
    best = ranked[0]
    assert best.path.endswith(file_) and best.line == line, (best.path, best.line, best.coverage)
    assert mg.verdict_for(best) == "raised_at"
    # the runner-up is a different statement, not a duplicate window
    assert len({(s.path, s.line) for s in ranked}) == len(ranked)


def test_split_message_boundaries_numbers_quotes_identifiers_and_code_prefix() -> None:
    p = mg.split_message(
        "Code: 53. DB::Exception: Received from localhost:9000. DB::Exception: "
        "Cannot convert string 'abc' to type DateTime64(3) (version 26.8.1.1) (TYPE_MISMATCH)"
    )
    assert p.error_code == 53
    assert p.error_name == "TYPE_MISMATCH"
    assert p.template == "Cannot convert string {} to type {}"
    assert p.spans == ("Cannot convert string",)
    # the 'While executing …' context chain is not part of the template
    p2 = mg.split_message(
        "Got read request from replica 3 for unknown stream default.attributes: "
        "While executing Remote: While executing JoiningTransform"
    )
    assert p2.template == "Got read request from replica {} for unknown stream {}"
    # a doubled space (an argument the caller stripped) is a boundary
    assert mg.split_message("Primary key (k) is not used and setting  is set").template == (
        "Primary key {} is not used and setting {} is set"
    )
    # ellipsis and UUID= are arguments; the trailing (NAME) is the code
    p3 = mg.split_message("Code : Unknown table function IN (UNKNOWN_FUNCTION)")
    assert p3.error_name == "UNKNOWN_FUNCTION" and p3.error_code is None
    assert p3.template == "Unknown table function IN"
    assert mg.split_message("Mapping for table with UUID=... doesn't exist").template == (
        "Mapping for table with {} doesn't exist"
    )


def test_short_spans_are_not_retrieval_keys_but_stay_in_the_template() -> None:
    p = mg.split_message("Data types Variant/Dynamic are not allowed in GROUP BY keys")
    assert p.template == "Data types {} are not allowed in GROUP BY keys"
    assert p.spans == ("are not allowed in GROUP BY keys",)
    assert mg.split_message("Received from xxx").needles == ["received from xxx"]


def test_stopword_only_shingles_are_dropped_when_a_specific_one_exists() -> None:
    p = mg.split_message("Parquet file has column legacy_col with field_id  that is not in")
    assert "that is not" not in p.needles and "parquet file has" in p.needles
    assert mg.split_message("that is not in the").needles  # nothing better: kept


# --------------------------------------------------------------- statements


def test_statement_windows_join_adjacent_literals_and_do_not_double_open() -> None:
    lines = SCHEMA_CONVERTER_MAPPER.splitlines()
    windows = mg.statement_windows(lines, 150)
    # `throw Exception(` on line 151 and `ErrorCodes::…` on 152 are ONE statement
    assert [w[0] for w in windows] == [151]
    tpl = mg.statement_template(windows[0][2])
    assert tpl.startswith(
        "Parquet file has column {} with field_id {} that is not in datalake metadata"
    )
    assert "assigned that field id" in tpl  # the second adjacent literal was joined
    assert "{}" in tpl and "{0}" not in tpl


def test_verify_is_truncation_tolerant_and_argument_blind() -> None:
    parsed = mg.split_message("Block structure mismatch in function connect between")
    v = mg.verify(parsed, "Block structure mismatch in {} stream: different types: {} {}")
    assert v.matched_words == 4 and 0.5 <= v.coverage < 0.6 and v.verified
    v2 = mg.verify(parsed, "Cannot convert string {} to type {}")
    assert v2.matched_words == 0 and v2.coverage == 0.0 and not v2.verified
    long = mg.split_message(
        "Code: 171. DB::Exception: Block structure mismatch in function connect between "
        "PartialSortingTransform and LazyOutputFormat stream: different columns:"
    )
    ranked = mg.rank_statements(long, SOURCE)
    assert ranked[0].path.endswith("Block.cpp")
    # long identifier arguments dilute character coverage; six aligned words carry it
    assert ranked[0].matched_words >= 6 and mg.verdict_for(ranked[0]) == "raised_at"


def test_error_code_name_parses_and_prefers_the_statement_naming_it() -> None:
    table = mg.parse_error_codes([_chunk("src/Common/ErrorCodes.cpp", 17, "", ERROR_CODES_CPP)])
    assert table[53] == "TYPE_MISMATCH" and table[0] == "OK" and 171 not in table
    parsed = mg.split_message("Code: 53. Requested type of column p doesn't match parquet schema")
    ranked = mg.rank_statements(parsed, SOURCE, error_name="TYPE_MISMATCH")
    assert ranked[0].names_code is True
    assert ranked[0].score > ranked[0].coverage


@pytest.mark.parametrize(
    "message, template",
    [
        # the message's head word is not an argument of this template
        ("Quorum is not supported", "NamedMutex::lock() is not supported on Android"),
        # a `{}` head with literal words the message lacks is another statement
        ("Trimming an empty string", "{} <path> -- Creates new node with an empty string as value"),
        # message words with no `{}` to sit against
        (
            "returned a column with unexpected number of rows",
            "function returned unexpected number of rows: {} instead of {}",
        ),
    ],
)
def test_verify_refuses_a_similar_but_different_statement(message: str, template: str) -> None:
    v = mg.verify(mg.split_message(message), template)
    assert v.matched_words >= 3 and not v.verified


@pytest.mark.parametrize(
    "message, template",
    [
        # a suffix paste: the agent copied the tail of a longer message
        (
            "Code:  ... Nullable Tuple type is not allowed",
            "Cannot create column with type ' {} ' because Nullable Tuple type is not allowed",
        ),
        # an argument at the head, a bare `{}` there
        (
            "no_read: Not enough privileges. To execute this query, it's necessary",
            "{} : Not enough privileges. To execute this query, it's necessary to have the grant {}",
        ),
        # a prefix paste of a long template
        (
            "Dictionary size is",
            "Index for LowCardinality is out of range. Dictionary size is {} , but found",
        ),
    ],
)
def test_verify_accepts_truncation_and_arguments(message: str, template: str) -> None:
    v = mg.verify(mg.split_message(message), template)
    assert v.verified and v.matched_words >= 3


def test_variant_skipping_fewer_template_literals_ranks_first() -> None:
    parsed = mg.split_message(
        "Block structure mismatch in function connect between PartialSortingTransform "
        "and LazyOutputFormat stream: different columns:"
    )
    ranked = mg.rank_statements(parsed, SOURCE)
    # three variants share the prefix; the one whose literal tail the
    # message carries ("different columns") skips nothing and wins
    assert [s.line for s in ranked[:3]] == [243, 234, 239]
    assert ranked[0].missing_chars == 0 and ranked[1].missing_chars == 7
    assert all(s.verified for s in ranked[:3])
    assert mg.verdict_for(ranked[0]) == "raised_at"


def test_verdict_no_template_match_below_half_coverage() -> None:
    parsed = mg.split_message("Cannot convert string  to type DateTime64(, )")
    only_block = [SOURCE[0]]
    ranked = mg.rank_statements(parsed, only_block)
    assert mg.verdict_for(ranked[0] if ranked else None) == "no_template_match"


# --------------------------------------------------------------- regex shape


@pytest.mark.parametrize(
    "pattern",
    [
        "ALIAS|isAlias|Alias",
        "deduplication_v2|/blocks",
        r"withVirtuals\(VirtualsKind::All, VirtualsMaterializationPlace::All\)",
        r"header.columns\(\) == current_step_columns.size\(\)",
        "^CoalescingSorted",
        "foo.*bar",
        "[Aa]lias",
        r"applySettingsFromQuery\|setSettings",
    ],
)
def test_regex_shaped_patterns(pattern: str) -> None:
    assert mg.regex_shaped(pattern)


@pytest.mark.parametrize(
    "pattern",
    [
        "res.info.num_rows",
        "ReadFromMerge::createSources(",
        "LOGICAL_ERROR)`",
        "isSerializable",
        "emplace(config_prefix",
        "Storage Buffer requires from 9 to 12 parameters",
        "data/system/",
    ],
)
def test_literal_shaped_patterns(pattern: str) -> None:
    assert not mg.regex_shaped(pattern)


def test_compile_fallback_rejects_an_invalid_regex() -> None:
    assert mg.compile_fallback(r"convertFieldToType(f, \*actual_type") is None
    assert mg.compile_fallback("ALIAS|isAlias") is not None


# --------------------------------------------------------------- handler


class _Repo:
    def __init__(
        self,
        literal: list[dict[str, Any]],
        regex: list[dict[str, Any]],
        message: list[dict[str, Any]] | None = None,
        admitted: int = 0,
    ) -> None:
        self.literal, self.regex, self.message, self.admitted = (
            literal,
            regex,
            message or [],
            admitted,
        )
        self.calls: list[tuple[str, bool]] = []
        self.files_calls: list[dict[str, Any]] = []

    async def grep_chunks(self, org, ns, pattern, *, regex=False, **kw):
        self.calls.append(("chunks", regex))
        return self.regex if regex else self.literal

    async def grep_files(self, org, ns, pattern, *, regex=False, **kw):
        self.calls.append(("files", regex))
        rows = self.regex if regex else self.literal
        return [{"path": r["path"], "chunks": 1, "first_line": 1} for r in rows], len(rows)

    async def grep_message_chunks(self, org, ns, needles, **kw):
        self.calls.append(("message", False))
        self.needles = list(needles)
        return self.message, self.admitted

    async def chunks_of_path(self, org, ns, path, *, limit=400):
        return [_chunk(path, 17, "", ERROR_CODES_CPP)]

    async def symbols_by_ids(self, org, ns, ids):
        return []

    async def list_files(self, org, ns, **kw):
        self.files_calls.append(dict(kw))
        return [
            {
                "path": "tests/queries/0_stateless/05138_with_fill_step_below_precision.sql",
                "lang": "sql",
                "size_bytes": 1127,
                "loc": 35,
                "indexed_commit": "abc",
            }
        ], 1


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

    async def go():
        with (
            patch.object(grep, "CodeRepo", return_value=repo),
            patch.object(grep, "index_as_of", new=_as_of),
            patch.object(grep, "schedule_code_trace", new=lambda **k: None),
        ):
            return await grep.handle_code_grep(CodeGrepInput(org="o", namespace="n", **kw))

    return asyncio.run(go())


LINE_CHUNK = _chunk("src/a.cpp", 10, "f", "int x;\nbool isAlias = ALIAS;\nreturn x;\n")


def test_literal_hit_is_byte_identical_and_never_falls_back() -> None:
    repo = _Repo(literal=[LINE_CHUNK], regex=[])
    out = _grep(repo, pattern="ALIAS|isAlias|Alias")
    # the literal read admitted a chunk: no line matches the literal text,
    # and no regex read happens
    assert repo.calls == [("chunks", False)]
    data = json.loads(out.model_dump_json())
    assert data["matches"] == [] and data["note"] == ""
    assert set(data) == {
        "matches",
        "next_cursor",
        "has_more",
        "files",
        "files_total",
        "chunks_matched",
        "truncated",
        "coverage_floor",
        "as_of",
        "not_covered",
        "note",
        "trace_run_id",
    }, "a matches-mode payload carries no message-mode field"


def test_regex_shaped_literal_with_no_chunks_is_run_once_as_a_regex() -> None:
    repo = _Repo(literal=[], regex=[LINE_CHUNK])
    out = _grep(repo, pattern="ALIAS|isAlias|Alias")
    assert repo.calls == [("chunks", False), ("chunks", True)]
    assert [m["line"] for m in out.matches] == [11]
    assert out.note == mg.REGEX_FALLBACK_NOTE


def test_plain_literal_with_no_chunks_does_not_fall_back() -> None:
    repo = _Repo(literal=[], regex=[LINE_CHUNK])
    out = _grep(repo, pattern="isSerializable")
    assert repo.calls == [("chunks", False)] and out.matches == [] and out.note == ""


def test_invalid_regex_shaped_literal_only_notes() -> None:
    repo = _Repo(literal=[], regex=[LINE_CHUNK])
    out = _grep(repo, pattern=r"convertFieldToType(f, \*actual_type")
    assert repo.calls == [("chunks", False)] and out.matches == []
    assert out.note == mg.REGEX_INVALID_NOTE


def test_files_mode_falls_back_the_same_way() -> None:
    repo = _Repo(literal=[], regex=[LINE_CHUNK])
    out = _grep(repo, pattern="deduplication_v2|/blocks", mode="files")
    assert repo.calls == [("files", False), ("files", True)]
    assert [f["path"] for f in out.files] == ["src/a.cpp"] and out.note == mg.REGEX_FALLBACK_NOTE


def test_message_mode_returns_verdict_template_code_and_ranked_statements() -> None:
    repo = _Repo(literal=[], regex=[], message=SOURCE, admitted=2)
    out = _grep(
        repo,
        pattern="Code: 53. Requested type of column p doesn't match parquet schema",
        mode="message",
        limit=3,
    )
    assert repo.calls == [("message", False)]
    assert out.verdict == "raised_at"
    assert out.template == "Requested type of column p doesn't match parquet schema"
    assert out.error_code == "TYPE_MISMATCH"
    assert out.matches[0]["path"].endswith("SchemaConverter.cpp") and out.matches[0]["line"] == 911
    assert out.matches[0]["names_error_code"] is True
    assert 0 < out.matches[0]["coverage"] <= 1 and out.coverage == out.matches[0]["coverage"]
    data = json.loads(out.model_dump_json())
    assert {"verdict", "template", "error_code", "coverage"} <= set(data)


def test_message_mode_unmapped_code_and_no_match_are_disclosed() -> None:
    repo = _Repo(literal=[], regex=[], message=[SOURCE[0]], admitted=1)
    out = _grep(
        repo, pattern="Code: 171. Cannot convert string  to type DateTime64(, )", mode="message"
    )
    assert out.verdict == "no_template_match" and out.error_code == ""
    assert out.note.startswith("Code: 171 is not in the indexed ErrorCodes table")


def test_message_mode_applies_the_pre_count_cap(monkeypatch) -> None:
    monkeypatch.setattr(settings, "code_search_pattern_max_files", 10)
    repo = _Repo(literal=[], regex=[], message=SOURCE, admitted=5000)
    out = _grep(
        repo, pattern="Requested type of column p doesn't match parquet schema", mode="message"
    )
    assert out.verdict == "no_template_match" and out.matches == []
    assert "pattern_too_broad" in out.note and "5000" in out.note


# --------------------------------------------------------------- code.files


def _files(repo: _Repo, **kw):
    from server.code.api.models_grep import CodeFilesInput
    from server.code.app import files

    async def go():
        with (
            patch.object(files, "CodeRepo", return_value=repo),
            patch.object(files, "index_as_of", new=_as_of),
            patch.object(files, "schedule_code_trace", new=lambda **k: None),
        ):
            return await files.handle_code_files(CodeFilesInput(org="o", namespace="n", **kw))

    return asyncio.run(go())


def test_code_files_by_stem_and_glob() -> None:
    repo = _Repo(literal=[], regex=[])
    out = _files(repo, stem="05138_with_fill")
    assert out.total == 1 and not out.truncated
    assert out.files[0]["path"].endswith("05138_with_fill_step_below_precision.sql")
    assert (
        repo.files_calls[-1]["stem"] == "05138_with_fill" and repo.files_calls[-1]["glob_re"] == ""
    )
    _files(repo, glob="tests/queries/0_stateless/05138_*", limit=7)
    assert repo.files_calls[-1]["glob_re"] == "^tests/queries/0_stateless/05138_[^/]*$"
    assert repo.files_calls[-1]["limit"] == 7


def test_code_files_needs_a_key() -> None:
    with pytest.raises(ValidationError):
        _files(_Repo(literal=[], regex=[]))


def test_code_files_is_registered_as_a_viewer_op() -> None:
    from server.operations.base import Role
    from server.operations.registry import ALL_OPERATIONS

    op = next(o for o in ALL_OPERATIONS if o.name == "code.files")
    assert op.required_role == Role.VIEWER and op.idempotent
