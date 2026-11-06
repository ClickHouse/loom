"""The reference extractor's format detection, strict decoding and SQL, TOML,
Markdown and HTML parsers against labelled fixtures, and the server's
structural chunks of what it extracts, read back through the server's
contract reader as any extractor's response is.

The fixtures are in `tests/unit/fixtures/knowledge/`; every locator asserted
here was read off the fixture by hand, so a parser change that moves one is a
failure, not a new golden value.
"""

from __future__ import annotations

import io
import json
import sys
import time
import tomllib
import zipfile
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from loom_extractor import ExtractionError, Options, decode, detect_format, extract
from loom_extractor.artifact import Artifact, Block, Coverage, Locator
from server.knowledge.domain.extract import InvalidArtifact, parse_artifact, structural

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "knowledge"


def _read(a: Artifact) -> Any:
    """`a` as the server reads it from an extractor's response."""
    return parse_artifact(a.to_json(), fmt=a.format, max_bytes=1 << 30)


def chunk_artifact(a: Artifact, *, size: int, stride: int) -> list[structural.StructuralChunk]:
    return structural.chunk_artifact(_read(a), size=size, stride=stride)


def _fixture(name: str, options: Options | None = None) -> Artifact:
    return extract((FIXTURES / name).read_bytes(), name, options)


def _lines(name: str, loc: Locator) -> str:
    text = (FIXTURES / name).read_text(encoding="utf-8").split("\n")
    return "\n".join(text[loc.line_start - 1 : loc.line_end])


def _zip(*names: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for n in names:
            z.writestr(n, "<x/>")
    return buf.getvalue()


# ---- detection and decoding -------------------------------------------------


@pytest.mark.parametrize(
    ("data", "name", "fmt"),
    [
        (b"%PDF-1.7\n...", "report.bin", "pdf"),
        # Named: a zip's bytes carry the time it was written, and an id must be the
        # same in every xdist worker.
        pytest.param(_zip("[Content_Types].xml", "word/document.xml"), "a.docx", "docx", id="docx"),
        pytest.param(_zip("ppt/presentation.xml"), "deck", "pptx", id="pptx"),
        pytest.param(_zip("xl/workbook.xml"), "book.xlsx", "xlsx", id="xlsx"),
        (b"PAR1....PAR1", "t.parquet", "parquet"),
        (b"SELECT 1;", "q.sql", "sql"),
        (b"a = 1", "c.toml", "toml"),
        (b"# T", "README.md", "markdown"),
        (b"<p>x</p>", "x.htm", "html"),
        (b"a,b\n1,2", "t.tsv", "csv"),
        (b"plain", "notes", "text"),
        (b"plain", "main.rs", "text"),
    ],
)
def test_detect_format_reads_content_before_the_name(data: bytes, name: str, fmt: str) -> None:
    assert detect_format(data, name) == fmt


@pytest.mark.parametrize(
    ("data", "name", "declared", "code"),
    [
        (b"just text", "report.pdf", "", "format_mismatch"),
        (b"%PDF-1.4", "report.docx", "", "format_mismatch"),
        (b"%PDF-1.4", "x", "sql", "format_mismatch"),
        pytest.param(_zip("data.txt"), "a.zip", "", "unsupported_format", id="plain-zip"),
        (b"PK\x03\x04broken", "a.docx", "", "corrupt"),
        (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1rest", "old.bin", "", "unsupported_format"),
        (b"anything", "old.xls", "", "unsupported_format"),
        (b"x", "x.txt", "spreadsheet", "invalid_option"),
    ],
)
def test_detect_format_refuses_what_the_bytes_do_not_support(
    data: bytes, name: str, declared: str, code: str
) -> None:
    with pytest.raises(ExtractionError) as e:
        detect_format(data, name, declared)
    assert e.value.code == code


def test_decode_is_strict_and_honours_byte_order_marks_and_declared_encodings() -> None:
    assert decode("é".encode()) == "é"
    assert decode(b"\xef\xbb\xbfhi") == "hi"
    assert decode("hi".encode("utf-16")) == "hi"
    assert decode("café".encode("latin-1"), "latin-1") == "café"
    with pytest.raises(ExtractionError) as bad:
        decode("café".encode("latin-1"))
    assert bad.value.code == "encoding" and "byte 3" in str(bad.value)
    with pytest.raises(ExtractionError) as binary:
        decode(b"ab\x00cd")
    assert binary.value.code == "unsupported_format"
    with pytest.raises(ExtractionError) as unknown:
        decode(b"x", "no-such-codec")
    assert unknown.value.code == "invalid_option"


def test_a_declared_encoding_is_recorded_on_the_artifact() -> None:
    a = extract("# Café\n\nbody".encode("latin-1"), "a.md", Options(encoding="latin-1"))
    assert a.blocks[0].text == "Café"
    assert a.options == {"encoding": "iso8859-1"}  # the codec's own name


# ---- SQL ---------------------------------------------------------------------


def test_sql_statements_keep_their_line_spans_kinds_names_and_comments() -> None:
    a = _fixture("retention.clickhouse.sql")
    assert a.format == "sql" and a.parser == "loom-sql"
    assert a.attrs["dialect"] == "clickhouse" and a.attrs["dialect_source"] == "detected"
    assert a.coverage.complete and (a.coverage.total, a.coverage.extracted) == (4, 4)
    got = [
        (
            b.locator.line_start,
            b.locator.line_end,
            b.attrs["kind"],
            b.attrs.get("defines") or b.attrs.get("writes"),
        )
        for b in a.blocks
    ]
    assert got == [
        (1, 10, "CREATE TABLE", "analytics.events"),
        (12, 16, "CREATE MATERIALIZED VIEW", "analytics.events_daily"),
        (18, 18, "ALTER", ["analytics.events"]),
        (20, 20, "SELECT", None),
    ]
    create, view, _, select = a.blocks
    assert create.attrs["comments"] == ["Raw events are kept for 30 days."]
    assert create.attrs["columns"] == ["id", "ts", "payload"]
    assert {"analytics.events", "events", "ts"} <= set(create.keys)
    assert view.attrs["reads"] == ["analytics.events"] and "events" in view.keys
    assert view.attrs["comments"] == ["Daily rollup; kept for a year."]
    # The `;` inside the string literal does not end the statement.
    assert select.text.endswith("today() - 7;") and "'a; b'" in select.text
    for b in a.blocks:
        assert _lines("retention.clickhouse.sql", b.locator) == b.text
        raw = (FIXTURES / "retention.clickhouse.sql").read_text(encoding="utf-8")
        assert raw[b.locator.char_start : b.locator.char_end] == b.text


def test_sql_dialect_detection_reports_a_tie_and_a_declared_dialect_wins() -> None:
    detected = _fixture("orders.postgres.sql")
    assert detected.attrs["dialect"] == "postgres"
    assert "mysql" in detected.attrs["dialect_candidates"]
    assert detected.warnings and detected.warnings[0].startswith("sql_dialect_ambiguous")
    index = detected.blocks[1]
    assert index.attrs == {
        "kind": "CREATE INDEX",
        "defines": "orders_region_idx",
        "writes": ["orders"],  # an index changes the table it is built on
    }
    assert detected.blocks[2].attrs == {"kind": "INSERT", "writes": ["orders"]}
    assert detected.blocks[3].attrs == {"kind": "DELETE", "writes": ["orders"]}

    declared = _fixture("orders.postgres.sql", Options(sql_dialect="postgres"))
    assert declared.attrs == {"dialect": "postgres", "dialect_source": "declared"}
    assert declared.options == {"sql_dialect": "postgres"} and declared.warnings == ()


def test_an_unknown_declared_dialect_is_an_invalid_option() -> None:
    with pytest.raises(ExtractionError) as e:
        _fixture("orders.postgres.sql", Options(sql_dialect="cobol"))
    assert e.value.code == "invalid_option"


def test_an_unparsable_statement_is_kept_as_text_and_its_lost_structure_recorded() -> None:
    a = _fixture("broken.sql")
    kinds = [b.attrs["kind"] for b in a.blocks]
    assert kinds == ["CREATE TABLE", "UNKNOWN", "SELECT", "COMMENT"]
    assert a.blocks[1].text == "THIS IS NOT SQL AT ALL;"
    assert a.blocks[3].attrs["comments"] == ["trailing note"]
    assert "comments" not in a.blocks[2].attrs  # the note after its `;` is not the SELECT's
    assert not a.coverage.complete
    assert a.coverage.lost == ("sql_structure",)
    assert (a.coverage.total, a.coverage.extracted) == (3, 2), (
        "the trailing comment is no statement"
    )
    assert [f.unit for f in a.coverage.failed] == [a.blocks[1].id], "a failure names its block"


def test_a_script_the_tokenizer_cannot_split_is_one_block_with_no_structure() -> None:
    a = extract(b"SELECT 1;\nSELECT 'unterminated", "q.sql", Options(sql_dialect="clickhouse"))
    assert len(a.blocks) == 1 and a.blocks[0].attrs == {"kind": "UNKNOWN"}
    assert (a.blocks[0].locator.line_start, a.blocks[0].locator.line_end) == (1, 2)
    assert a.coverage.lost == ("sql_structure",) and a.coverage.extracted == 0


# ---- TOML --------------------------------------------------------------------


def test_toml_keys_carry_full_paths_types_values_lines_and_comments() -> None:
    a = _fixture("service.toml")
    keys = {b.locator.key_path: b for b in a.blocks if b.type == "toml_key"}
    assert a.coverage.complete and a.coverage.total == len(keys) == 14
    expect = {
        "title": (1, 2, "string", "loom"),  # the comment above it is its first line
        "started": (3, 3, "datetime", "2026-01-02T03:04:05+00:00"),
        "server.port": (6, 6, "integer", 7777),
        "server.hosts": (7, 10, "array", ["a.internal", "b.internal"]),
        "server.limits.max_rows": (14, 14, "integer", 1000),
        'server.limits."query.timeout"': (15, 15, "float", 30.5),
        "server.limits.enabled": (16, 16, "boolean", True),
        "retention.rules[0].days": (20, 20, "integer", 30),
        "retention.rules[1].days": (24, 24, "integer", 7),
        "retention.rules[1].notes": (25, 27, "string", "Short-lived:\ndebug only."),
        "retention.rules[1].exceptions[0].tenant": (30, 30, "string", "acme"),
        "retention.rules[1].exceptions[0].inline": (
            31,
            31,
            "table",
            {"days": 90, "reason": "contract"},
        ),
    }
    for path, (first, last, vtype, value) in expect.items():
        b = keys[path]
        assert (b.locator.line_start, b.locator.line_end) == (first, last), path
        assert (b.attrs["value_type"], b.attrs["value"]) == (vtype, value), path
    assert keys["title"].attrs["comments"] == ["Service configuration.", "the product"]
    assert keys["retention.rules[1].days"].keys == (
        "retention.rules[1].days",
        "retention.rules.days",
    )
    assert keys["server.limits.max_rows"].section_path == ("server", "limits")


def test_toml_tables_number_arrays_of_tables_and_keep_their_header_text() -> None:
    a = _fixture("service.toml")
    tables = [
        (b.locator.key_path, b.text, b.locator.line_start)
        for b in a.blocks
        if b.type == "toml_table"
    ]
    assert tables == [
        ("server", "[server]", 5),
        ("server.limits", "# Hard limits.\n[server.limits]", 12),
        ("retention.rules[0]", "[[retention.rules]]", 18),
        ("retention.rules[1]", "[[retention.rules]]", 22),
        ("retention.rules[1].exceptions[0]", "[[retention.rules.exceptions]]", 29),
    ]
    by_id = {b.id: b for b in a.blocks}
    days = next(b for b in a.blocks if b.locator.key_path == "retention.rules[1].days")
    assert by_id[days.parent].locator.key_path == "retention.rules[1]"


def test_invalid_toml_stays_searchable_text_and_records_the_lost_types() -> None:
    a = _fixture("invalid.toml")
    assert a.format == "toml" and [b.type for b in a.blocks] == ["paragraph"]
    commented = extract(b'# owner: ops\ntitle = "x"\n[broken\n', "c.toml")
    assert [b.type for b in commented.blocks] == ["paragraph"], "a TOML comment is not a heading"
    assert a.coverage.lost == ("toml_types",)
    assert "line 2" in a.coverage.failed[0].reason


# ---- Markdown and HTML -------------------------------------------------------


def test_markdown_blocks_keep_headings_lists_code_and_line_spans() -> None:
    a = _fixture("runbook.md")
    got = [(b.type, b.locator.line_start, b.locator.line_end, b.section_path) for b in a.blocks]
    assert got == [
        ("heading", 1, 1, ("Runbook",)),
        ("paragraph", 3, 4, ("Runbook",)),
        ("heading", 6, 6, ("Runbook", "Rollback")),
        ("list", 8, 9, ("Runbook", "Rollback")),
        ("code", 12, 13, ("Runbook", "Rollback")),  # the body, without its fences
        ("heading", 16, 17, ("Runbook", "Setext section")),
        ("list", 19, 20, ("Runbook", "Setext section")),
    ]
    code = a.blocks[4]
    assert code.text == "# not a heading\nmake rollback" and code.attrs == {"language": "bash"}
    assert a.blocks[3].parent == a.blocks[2].id
    assert a.coverage.complete


def test_html_blocks_drop_invisible_elements_and_keep_structure() -> None:
    a = _fixture("page.html")
    got = [(b.type, b.text, b.section_path) for b in a.blocks]
    assert got == [
        ("heading", "Policy", ("Policy",)),
        ("paragraph", "Data is kept for 30 days.", ("Policy",)),
        ("heading", "Exceptions", ("Policy", "Exceptions")),
        ("list", "Legal hold", ("Policy", "Exceptions")),
        ("list", "Contract", ("Policy", "Exceptions")),
        ("code", "SELECT 1;", ("Policy", "Exceptions")),
    ]
    assert (a.blocks[1].locator.line_start, a.blocks[1].locator.line_end) == (4, 5)
    assert all("hidden" not in b.text and "color" not in b.text for b in a.blocks)
    assert a.coverage.unit == "chars"


# ---- the artifact ------------------------------------------------------------


def test_an_artifact_round_trips_through_json_and_is_addressed_by_its_bytes() -> None:
    a = _fixture("service.toml")
    again = _read(a)
    assert again.to_json() == a.to_json() and again.sha256 == a.sha256
    assert _fixture("service.toml").sha256 == a.sha256  # deterministic
    assert _fixture("runbook.md").sha256 != a.sha256


def test_an_artifact_of_another_schema_version_is_refused() -> None:
    blob = _fixture("runbook.md").to_json().replace(b'"schema_version":1', b'"schema_version":99')
    with pytest.raises(InvalidArtifact, match="schema_version"):
        parse_artifact(blob, fmt="markdown", max_bytes=1 << 30)


def test_coverage_is_complete_only_with_every_unit_and_no_loss() -> None:
    assert Coverage("pages", 3, 3).complete
    assert not Coverage("pages", 3, 2).complete
    assert not Coverage("statements", 2, 2, lost=("sql_structure",)).complete


# ---- structural chunks -------------------------------------------------------


def test_each_sql_statement_is_its_own_chunk_with_its_names_as_keys() -> None:
    a = _fixture("retention.clickhouse.sql")
    chunks = chunk_artifact(a, size=2000, stride=1500)
    assert [c.block_ids for c in chunks] == [("b0",), ("b1",), ("b2",), ("b3",)]
    assert chunks[1].locator.line_start == 12 and "analytics.events_daily" in chunks[1].keys


def test_a_toml_table_packs_with_its_keys_and_the_chunk_spans_their_lines() -> None:
    chunks = chunk_artifact(_fixture("service.toml"), size=2000, stride=1500)
    limits = next(c for c in chunks if c.section_path == ("server", "limits"))
    assert limits.block_ids == ("b5", "b6", "b7", "b8")
    assert (limits.locator.line_start, limits.locator.line_end) == (12, 16)
    assert "server.limits.max_rows" in limits.keys
    assert limits.locator.key_path == ""  # a table and its three keys: no single path
    assert limits.breadcrumb == "server > limits"


def test_headings_are_not_chunk_text_and_sections_do_not_mix() -> None:
    chunks = chunk_artifact(_fixture("runbook.md"), size=2000, stride=1500)
    assert [c.section_path for c in chunks] == [
        ("Runbook",),
        ("Runbook", "Rollback"),
        ("Runbook", "Setext section"),
    ]
    assert all("Rollback" not in c.text for c in chunks)


def test_an_oversized_block_is_windowed_and_each_window_cites_its_own_lines() -> None:
    body = "\n".join(f"line {i:03d} of the long section" for i in range(1, 41))
    text = f"# Long\n\n{body}\n"
    a = extract(text.encode(), "long.md")
    chunks = chunk_artifact(a, size=300, stride=250)
    assert len(chunks) > 1 and all(c.section_path == ("Long",) for c in chunks)
    source = text.split("\n")
    for c in chunks:
        assert text[c.locator.char_start : c.locator.char_end] == c.text
        cited = "\n".join(source[c.locator.line_start - 1 : c.locator.line_end])
        assert c.text in cited


def test_blocks_of_one_section_pack_up_to_the_size() -> None:
    blocks = tuple(
        Block(
            id=f"b{i}",
            type="paragraph",
            text="x" * 40,
            locator=Locator(line_start=i + 1, line_end=i + 1),
        )
        for i in range(5)
    )
    a = Artifact("text", "t", "1", blocks, Coverage("lines", 5, 5))
    chunks = chunk_artifact(a, size=100, stride=80)
    assert [c.block_ids for c in chunks] == [("b0", "b1"), ("b2", "b3"), ("b4",)]
    assert (chunks[0].locator.line_start, chunks[0].locator.line_end) == (1, 2)


# ---- implied end tags, fences, quoted keys, comment attribution --------------


@pytest.mark.parametrize(
    ("html", "want"),
    [
        (
            "<h1>T</h1><p>one<p>two<h2>S</h2><p>three</p>",
            [
                ("heading", "T"),
                ("paragraph", "one"),
                ("paragraph", "two"),
                ("heading", "S"),
                ("paragraph", "three"),
            ],
        ),
        ("<table><tr><td>a<td>b</tr></table>", [("paragraph", "a"), ("paragraph", "b")]),
        ("<ul><li>x<li><p>y</p></ul>", [("list", "x"), ("list", "y")]),
        (
            "<ul><li><p>a</p><p>b</p></li><li>c<p>d</p></ul>",
            [("list", "a b"), ("list", "c"), ("paragraph", "d")],
        ),
        ("<html><head><meta charset=utf-8><body><p>seen", [("paragraph", "seen")]),
        ("<h7>not a heading</h7><p>p</p>", [("paragraph", "not a heading"), ("paragraph", "p")]),
        ("<div>one<br>two</div>", [("paragraph", "one two")]),
        ("<body>Intro<div>More</div>", [("paragraph", "Intro"), ("paragraph", "More")]),
        ("<td>a<div>b</div></td>", [("paragraph", "a b")]),
    ],
)
def test_html_blocks_close_where_html_lets_an_end_tag_be_left_out(
    html: str, want: list[tuple[str, str]]
) -> None:
    a = extract(html.encode(), "x.html")
    assert [(b.type, b.text) for b in a.blocks] == want


def test_html_coverage_counts_visible_text_only() -> None:
    a = extract(b"<p>kept</p><svg><text>drawn</text></svg><script>x()</script>", "x.html")
    assert a.coverage.to_dict()["total"] == len("kept")


def test_a_long_fence_records_its_language_and_plain_text_has_no_headings() -> None:
    code = extract(b"````python\nx = 1\n````\n", "a.md")
    assert code.blocks[0].attrs == {"language": "python"} and code.blocks[0].text == "x = 1"
    plain = extract(b"# not a heading in a log\nnext line\n\nsecond", "app.log")
    assert [(b.type, b.text) for b in plain.blocks] == [
        ("paragraph", "# not a heading in a log\nnext line"),
        ("paragraph", "second"),
    ]
    assert plain.coverage.complete


def test_windows_of_an_oversized_code_block_cite_their_own_lines_and_chars() -> None:
    body = "\n".join(f"step_{i:03d}()  # one line of a long script" for i in range(40))
    text = f"# Script\n\n```bash\n{body}\n```\n"
    chunks = chunk_artifact(extract(text.encode(), "s.md"), size=300, stride=250)
    assert len(chunks) > 1
    source = text.split("\n")
    for c in chunks:
        assert text[c.locator.char_start : c.locator.char_end] == c.text
        cited = "\n".join(source[c.locator.line_start - 1 : c.locator.line_end])
        assert c.text.rstrip("\n") in cited
        assert "```" not in c.text


def test_toml_quoted_keys_decode_escapes_and_triple_quoted_strings_keep_escaped_quotes() -> None:
    src = 'x = """a \\""" b"""\n"caf\\u00e9" = 1\n[a."\\u00e9"]\nk = 2\n'
    a = extract(src.encode(), "c.toml")
    keys = {b.locator.key_path: b for b in a.blocks if b.type == "toml_key"}
    assert set(keys) == {"x", '"café"', 'a."é".k'}
    assert keys["x"].attrs["value"] == 'a """ b' and keys["x"].locator.line_end == 1
    assert a.coverage.complete


def test_sql_ctes_are_not_tables_and_an_insert_reading_its_target_reads_it() -> None:
    a = extract(
        b"WITH recent AS (SELECT * FROM events) SELECT * FROM recent;\nINSERT INTO t SELECT * FROM t;",
        "q.sql",
        Options(sql_dialect="clickhouse"),
    )
    assert a.blocks[0].attrs == {"kind": "SELECT", "reads": ["events"]}
    assert "recent" not in a.blocks[0].keys
    assert a.blocks[1].attrs == {"kind": "INSERT", "writes": ["t"], "reads": ["t"]}


def test_a_comment_after_a_semicolon_on_its_line_belongs_to_that_statement() -> None:
    text = "ALTER TABLE t MODIFY TTL ts + INTERVAL 1 DAY; -- widen the TTL\nSELECT 1\n-- before\n;\nSELECT 2;\n"
    a = extract(text.encode(), "q.sql", Options(sql_dialect="clickhouse"))
    alter, one, two = a.blocks
    assert alter.text.endswith("-- widen the TTL") and alter.attrs["comments"] == ["widen the TTL"]
    assert alter.attrs["kind"] == "ALTER", "the comment is not parsed as SQL"
    assert one.attrs == {"comments": ["before"], "kind": "SELECT"} and one.locator.line_end == 4
    assert "comments" not in two.attrs
    for b in a.blocks:
        assert text[b.locator.char_start : b.locator.char_end] == b.text


def test_a_script_no_dialect_parses_reports_no_ambiguity() -> None:
    a = extract(b"THIS IS NOT SQL AT ALL;\n(((;", "q.sql")
    assert a.warnings == () and not a.coverage.complete
    assert a.attrs == {"dialect": "clickhouse", "dialect_source": "default"}, "nothing was detected"


def test_an_untokenizable_script_cites_where_its_text_is() -> None:
    text = "\n\n  SELECT 'unterminated\n"
    a = extract(text.encode(), "q.sql", Options(sql_dialect="clickhouse"))
    (b,) = a.blocks
    assert text[b.locator.char_start : b.locator.char_end] == b.text == "SELECT 'unterminated"
    assert (b.locator.line_start, b.locator.line_end) == (3, 3)
    assert [f.unit for f in a.coverage.failed] == ["b0"], "a failure names its block"


def test_the_sql_parser_version_names_loom_and_sqlglot() -> None:
    import sqlglot

    assert _fixture("broken.sql").parser_version == f"1+sqlglot{sqlglot.__version__}"


def test_a_block_comment_between_statements_on_one_line_keeps_both() -> None:
    text = "SELECT 1; /* note */ SELECT 2;\nSELECT 'x';\n-- x\n"
    a = extract(text.encode(), "q.sql", Options(sql_dialect="clickhouse"))
    assert [(b.text, b.attrs.get("comments")) for b in a.blocks] == [
        ("SELECT 1;", None),
        ("/* note */ SELECT 2;", ["note"]),
        ("SELECT 'x';", None),
        ("-- x", ["x"]),
    ]
    assert a.coverage.complete


def test_detection_survives_a_cut_inside_a_quoted_body() -> None:
    from loom_extractor import sql

    body = "CREATE FUNCTION f() RETURNS int AS $$ " + "SELECT 1; " * 60 + "$$ LANGUAGE sql;\n"
    long = body + "SELECT 2;\n" + "x" * sql.DETECT_CHARS
    prefixes = sql._detection_prefixes(long)
    assert prefixes[0].count(";") == sql.DETECT_STATEMENTS and len(prefixes) > 1
    assert sql._detection_prefixes(body)[0] == body, "a short script is read whole first"
    # Every cut lands inside the `$$` body: the dialects that read it are scored on
    # the whole script, and the ones that split it into fragments lose.
    chosen, tied = sql.detect_dialect(body + "SELECT 2;\n")
    assert "postgres" in tied and chosen in tied and not {"mysql", "sqlite", "tsql"} & set(tied)
    huge = "INSERT INTO t VALUES " + ", ".join(["(1)"] * 100_000) + ";"
    assert all(len(p) <= sql.DETECT_CHARS for p in sql._detection_prefixes(huge))


def test_an_unclosed_head_ends_at_the_first_body_element_and_spans_end_on_their_text() -> None:
    a = extract(b"<html><head><title>T</title><p>Hello", "x.html")
    assert [(b.type, b.text) for b in a.blocks] == [("paragraph", "Hello")]
    b = extract(b"<p>one\n\n<p>two", "x.html")
    assert [(x.locator.line_start, x.locator.line_end) for x in b.blocks] == [(1, 1), (3, 3)]


def test_a_toml_block_cites_the_comment_lines_its_text_holds() -> None:
    text = "# owner\n# team ops\nport = 1  # the listener\n"
    a = extract(text.encode(), "c.toml")
    (b,) = a.blocks
    assert b.text == "# owner\n# team ops\nport = 1  # the listener"
    assert (b.locator.line_start, b.locator.line_end) == (1, 3)
    assert b.attrs["comments"] == ["owner", "team ops", "the listener"]


def test_a_heading_with_nothing_under_it_is_a_chunk_of_its_own() -> None:
    chunks = chunk_artifact(extract(b"# A\n## B\n\nbody\n# C\n# D\n", "h.md"), size=500, stride=400)
    assert [(c.block_type, c.text) for c in chunks] == [
        ("paragraph", "body"),
        ("heading", "C"),
        ("heading", "D"),
    ]


def test_a_materialized_view_writes_its_to_table_and_a_drop_writes_what_it_drops() -> None:
    text = "CREATE MATERIALIZED VIEW db.mv TO db.dst AS SELECT a FROM db.src;\nDROP TABLE IF EXISTS db.t, u;\n"
    mv, drop = extract(text.encode(), "q.sql", Options(sql_dialect="clickhouse")).blocks
    assert mv.attrs == {
        "kind": "CREATE MATERIALIZED VIEW",
        "defines": "db.mv",
        "writes": ["db.dst"],
        "reads": ["db.src"],
    }
    assert {"mv", "dst", "src"} <= set(mv.keys)
    assert drop.attrs == {"kind": "DROP TABLE", "writes": ["db.t", "u"]}


def test_toml_comments_no_key_follows_stay_searchable_text() -> None:
    text = '# Licence header\n# line two\n\n[server]\nhosts = [\n  "a", # primary\n]\n# trailing note\n'
    a = extract(text.encode(), "c.toml")
    loose = [
        (b.type, b.text, b.locator.line_start, b.locator.line_end)
        for b in a.blocks
        if b.type == "paragraph"
    ]
    assert loose == [
        ("paragraph", "# Licence header\n# line two", 1, 2),
        ("paragraph", "# trailing note", 8, 8),
    ]
    hosts = next(b for b in a.blocks if b.type == "toml_key")
    assert "# primary" in hosts.text and hosts.attrs["value"] == ["a"]
    assert next(b for b in a.blocks if b.text == "# trailing note").parent == a.blocks[1].id


def test_yaml_front_matter_is_one_block_not_a_heading() -> None:
    a = extract(b"---\ntitle: Guide\ntags: [a]\n---\n# Guide\n\nbody\n", "page.mdx")
    assert [(b.type, b.text) for b in a.blocks] == [
        ("paragraph", "title: Guide\ntags: [a]"),
        ("heading", "Guide"),
        ("paragraph", "body"),
    ]
    assert a.blocks[0].attrs == {"front_matter": True}


def test_a_declared_utf8_encoding_drops_the_byte_order_mark() -> None:
    a = extract(b"\xef\xbb\xbf# Title\n", "a.md", Options(encoding="utf-8"))
    assert a.blocks[0].type == "heading" and a.blocks[0].text == "Title"


def test_merged_and_windowed_locators_keep_an_agreed_key_path() -> None:
    one = Locator(line_start=1, line_end=1, char_start=0, char_end=40, key_path="a.b")
    blocks = tuple(
        Block(id=f"b{i}", type="paragraph", text="x" * 40, locator=one) for i in range(2)
    )
    (merged,) = chunk_artifact(
        Artifact("text", "t", "1", blocks, Coverage("lines", 2, 2)), size=200, stride=150
    )
    assert merged.locator.key_path == "a.b"
    long = Block(id="b0", type="paragraph", text="y" * 500, locator=replace(one, char_end=500))
    windows = chunk_artifact(
        Artifact("text", "t", "1", (long,), Coverage("lines", 1, 1)), size=200, stride=150
    )
    assert len(windows) > 1 and all(w.locator.key_path == "a.b" for w in windows)


@pytest.mark.parametrize(
    ("sql", "dialect", "attrs"),
    [
        (
            "MERGE INTO t USING s ON t.id = s.id WHEN MATCHED THEN UPDATE SET v = s.v;",
            "postgres",
            {"kind": "MERGE", "writes": ["t"], "reads": ["s"]},
        ),
        ("TRUNCATE TABLE db.t;", "clickhouse", {"kind": "TRUNCATE", "writes": ["db.t"]}),
        (
            "DROP MATERIALIZED VIEW mv;",
            "clickhouse",
            {"kind": "DROP MATERIALIZED VIEW", "writes": ["mv"]},
        ),
    ],
)
def test_merge_truncate_and_drop_write_their_targets(sql: str, dialect: str, attrs: dict) -> None:
    b = extract(sql.encode(), "q.sql", Options(sql_dialect=dialect)).blocks[0]
    assert b.attrs == attrs


def test_deep_nesting_and_empty_statements_do_not_break_extraction() -> None:
    deep = "SELECT " + "(" * 3000 + "1" + ")" * 3000 + ";\nSELECT 2;;\n"
    a = extract(deep.encode(), "q.sql", Options(sql_dialect="clickhouse"))
    assert [b.attrs["kind"] for b in a.blocks] == ["UNKNOWN", "SELECT"]
    assert a.coverage.failed[0].reason == "no clickhouse syntax tree"
    assert extract(deep.encode(), "q.sql").attrs["dialect"] in ("clickhouse", "postgres")


def test_a_backtick_info_string_with_a_backtick_opens_no_fence_and_a_language_is_one_word() -> None:
    a = extract(b"```js``` is inline\n# Heading\n\ntext\n", "a.md")
    assert [b.type for b in a.blocks] == ["paragraph", "heading", "paragraph"]
    code = extract(b'```js title="a.js"\nx\n```\n', "a.md").blocks[0]
    assert code.attrs == {"language": "js", "info": 'title="a.js"'}


def test_html_spans_start_on_their_first_text_line() -> None:
    a = extract(b"<div>\n\nloose</div>\n<pre>\nSELECT 1;\n</pre>\n", "x.html")
    assert [(b.text, b.locator.line_start, b.locator.line_end) for b in a.blocks] == [
        ("loose", 3, 3),
        ("SELECT 1;", 5, 5),
    ]


def test_utf32_text_with_its_byte_order_mark_decodes() -> None:
    assert extract("hi".encode("utf-32"), "a.txt").blocks[0].text == "hi"


def test_an_artifact_a_newer_build_wrote_reads_back() -> None:
    future = _fixture("runbook.md").to_dict()
    future["blocks"][1]["locator"]["column"] = 7  # a field a newer build adds
    future["coverage"]["failed"] = [{"unit": "x", "reason": "y", "page_count": 2}]
    future["coverage"]["complete"] = False
    again = parse_artifact(json.dumps(future).encode(), fmt="markdown", max_bytes=1 << 30)
    assert again.blocks[1].locator.line_start == 3 and again.coverage.failed[0].unit == "x"


def test_a_declared_format_overrides_a_legacy_extension() -> None:
    a = extract(b"plain notes", "notes.doc", Options(format="text"))
    assert a.blocks[0].text == "plain notes"
    with pytest.raises(ExtractionError):
        extract(b"plain notes", "notes.doc")


def test_a_failure_after_a_comment_block_names_its_own_block() -> None:
    a = extract(
        b"-- lead\n;\nSELECT 1;\nTHIS IS NOT SQL AT ALL;",
        "q.sql",
        Options(sql_dialect="clickhouse"),
    )
    assert [b.attrs["kind"] for b in a.blocks] == ["COMMENT", "SELECT", "UNKNOWN"]
    assert a.blocks[0].attrs["comments"] == ["lead"]
    assert [f.unit for f in a.coverage.failed] == ["b2"]
    assert (a.coverage.total, a.coverage.extracted) == (2, 1)


def test_a_stride_past_the_size_leaves_no_text_between_windows() -> None:
    a = extract(("z" * 1000).encode(), "a.txt")
    chunks = chunk_artifact(a, size=300, stride=500)
    assert "".join(c.text for c in chunks).count("z") >= 1000


@pytest.mark.parametrize(
    ("line", "title"),
    [
        ("## `max_rows`", "max_rows"),
        ("## Option<String> fields", "Option<String> fields"),
        ("## Using C#", "Using C#"),
        ("## Closed ##", "Closed"),
        ("## **Bold** and *it*", "Bold and it"),
        ("# <code>x</code> y", "x y"),
        ('# <img src="logo.svg" width="32"> Project', "Project"),
        ("## `__init__` and `**kwargs`", "__init__ and **kwargs"),
        ("## __init__ and *args and **kwargs", "__init__ and *args and **kwargs"),
        ("## Set <your token>", "Set <your token>"),
        ("## Promise<T extends Base>", "Promise<T extends Base>"),
        ("## 2 * 3", "2 * 3"),
        ("## a<br/>b", "a b"),
        ("## The `<br>` element", "The <br> element"),
        ("## Into<S> for String", "Into<S> for String"),
        ("## Option<A> and Vec<U>", "Option<A> and Vec<U>"),
        ("## Map<a, b> and Result<i, E>", "Map<a, b> and Result<i, E>"),
        ("## <https://loom.dev> docs", "https://loom.dev docs"),
        ("## ``a`b`` span", "a`b span"),
        ("## ***Note***", "Note"),
        ("## **a *b* c**", "a b c"),
        ("## **The `foo` option**", "The foo option"),
        ("## *`x`* and `**y**`", "x and **y**"),
        ("## <b>Bold</b> <i>tag</i>", "Bold tag"),
    ],
)
def test_heading_text_keeps_identifiers_generics_and_a_trailing_hash(line: str, title: str) -> None:
    (b,) = extract(line.encode(), "a.md").blocks
    assert b.type == "heading" and b.text == title


def test_deeply_nested_toml_is_kept_as_text_not_an_error() -> None:
    a = extract(b"a = " + b"[" * 5000, "c.toml")
    assert a.coverage.lost == ("toml_types",) and len(a.blocks) == 1


def test_two_comments_after_a_semicolon_both_belong_to_that_statement() -> None:
    a = extract(b"SELECT 1; /* a */ /* b */\nSELECT 2;", "q.sql", Options(sql_dialect="clickhouse"))
    assert [b.attrs.get("comments") for b in a.blocks] == [["a", "b"], None]


def test_a_leading_thematic_break_is_not_front_matter() -> None:
    a = extract(b"---\n# Intro\n\ntext\n\n---\n## Next\n", "a.md")
    assert [b.text for b in a.blocks if b.type == "heading"] == ["Intro", "Next"]


def test_an_unclosed_fence_ends_on_its_text() -> None:
    (code,) = extract(b"```\ncode\n", "a.md").blocks
    assert (code.text, code.locator.line_end) == ("code", 2)


def test_page_landmarks_separate_their_text() -> None:
    a = extract(b"<header>Site</header><nav>Menu</nav><footer>Copyright</footer>", "x.html")
    assert [b.text for b in a.blocks] == ["Site", "Menu", "Copyright"]


@pytest.mark.parametrize(
    ("html", "want"),
    [
        ("<li><div>Title</div><div>Details</div></li>", [("list", "Title Details")]),
        ("<td><div>b</div> c</td>", [("paragraph", "b c")]),
        ("<html><head><title>T</title>Welcome<p>x", [("paragraph", "Welcome"), ("paragraph", "x")]),
    ],
)
def test_a_container_inside_a_block_keeps_the_block_and_text_ends_an_unclosed_head(
    html: str, want: list[tuple[str, str]]
) -> None:
    assert [(b.type, b.text) for b in extract(html.encode(), "x.html").blocks] == want


def test_a_paragraph_over_a_setext_underline_is_one_heading() -> None:
    a = extract(b"Line one\nLine two\n---\n\nbody\n", "a.md")
    assert [(b.type, b.text, b.locator.line_start, b.locator.line_end) for b in a.blocks] == [
        ("heading", "Line one Line two", 1, 3),
        ("paragraph", "body", 5, 5),
    ]


@pytest.mark.parametrize(
    "name",
    ["runbook.md", "front.md", "fences.md"],
)
def test_every_non_blank_markdown_line_is_in_a_block_or_is_its_delimiter(name: str) -> None:
    sources = {
        "front.md": "---\ntitle: x\n---\n# H\n\ntext\nmore\n---\n",
        "fences.md": "````md\n```bash\necho\n```\n````\n\n```\nunclosed\n",
    }
    text = sources.get(name) or (FIXTURES / name).read_text(encoding="utf-8")
    a = extract(text.encode(), name)
    lines = text.split("\n")
    covered: set[int] = set()
    for b in a.blocks:
        covered.update(range(b.locator.line_start, b.locator.line_end + 1))
        if b.type == "code" or b.attrs.get("front_matter"):
            covered.update((b.locator.line_start - 1, b.locator.line_end + 1))  # its delimiters
    missing = [n for n, line in enumerate(lines, 1) if line.strip() and n not in covered]
    assert missing == []


def test_a_crlf_setext_heading_is_a_heading_and_an_empty_one_stays_text() -> None:
    crlf = extract(b"Title\r\n=====\r\n\r\nbody\r\n", "a.md")
    assert [(b.type, b.text.rstrip("\r")) for b in crlf.blocks] == [
        ("heading", "Title"),
        ("paragraph", "body"),
    ]
    image = extract(b'<img src="logo.svg">\n===\n', "a.md")
    assert [(b.type, b.text) for b in image.blocks] == [("paragraph", '<img src="logo.svg">\n===')]


def test_a_comment_after_a_semicolon_is_matched_once() -> None:
    a = extract(b"SELECT 1; -- abc\n-- b", "q.sql", Options(sql_dialect="clickhouse"))
    assert [b.attrs.get("comments") for b in a.blocks] == [["abc"], ["b"]]


def test_a_crlf_heading_drops_its_closing_hashes() -> None:
    (h,) = extract(b"## Closed ##\r\n", "a.md").blocks
    assert h.text == "Closed"


def test_a_quote_over_a_rule_is_no_heading() -> None:
    a = extract(b"> quote\n---\n", "a.md")
    assert [(b.type, b.text) for b in a.blocks] == [("paragraph", "> quote")]
    assert a.coverage.complete, "the rule is markup"


def test_a_rule_ends_the_paragraph_or_list_above_it() -> None:
    a = extract(b"text\n***\n- item\n---\nafter\n", "a.md")
    assert [(b.type, b.text) for b in a.blocks] == [
        ("paragraph", "text"),
        ("list", "- item"),
        ("paragraph", "after"),
    ]


def test_a_chunk_of_blocks_of_different_types_is_mixed() -> None:
    a = extract(b"# A\n\ntext\n\n- item\n\n# B\n\nonly\n", "a.md")
    assert [c.block_type for c in chunk_artifact(a, size=500, stride=400)] == ["mixed", "paragraph"]


def test_html_summary_and_rule_separate_text() -> None:
    details = extract(b"<details><summary>Question</summary>Answer</details>", "x.html")
    assert [b.text for b in details.blocks] == ["Question", "Answer"]
    assert [b.text for b in extract(b"<div>one<hr>two</div>", "x.html").blocks] == ["one two"]


def test_an_empty_fenced_block_keeps_its_info_string() -> None:
    (code,) = extract(b'```sql title="x"\n```\n', "a.md").blocks
    assert (code.type, code.text, code.attrs) == (
        "code",
        "",
        {"language": "sql", "info": 'title="x"'},
    )


def test_an_encoding_error_names_the_byte_offset_counting_a_byte_order_mark() -> None:
    with pytest.raises(ExtractionError, match="at byte 6;"):
        extract(b"\xef\xbb\xbfcaf\xe9", "a.txt")
    with pytest.raises(ExtractionError, match="at byte 3;"):
        extract(b"caf\xe9", "a.txt")
    for declared in ("utf-8", "utf-8-sig"):  # a declared encoding counts the mark too
        with pytest.raises(ExtractionError, match="at byte 6;"):
            extract(b"\xef\xbb\xbfcaf\xe9", "a.txt", Options(encoding=declared))
    assert extract(b"\xef\xbb\xbfok", "a.txt", Options(encoding="utf-8-sig")).blocks[0].text == "ok"


def test_a_page_with_only_an_unclosed_head_reads_whole() -> None:
    a = extract(b"<head><title>Doc</title>", "x.html")
    assert a.blocks == () and a.attrs == {"title": "Doc"}
    assert a.coverage.failed == ()


def test_a_quoted_dotted_toml_key_keeps_its_quotes_in_every_key() -> None:
    a = extract(
        b'[server.limits]\n"query.timeout" = 30\n\n[server.limits.query]\ntimeout = 5\n', "c.toml"
    )
    by_key = {k: b.text for b in a.blocks for k in b.keys}
    assert by_key['server.limits."query.timeout"'].endswith('"query.timeout" = 30')
    assert by_key["server.limits.query.timeout"].endswith("timeout = 5")


def test_a_toml_key_path_escapes_as_toml_reads_it() -> None:
    a = extract(b'"a\\\\b" = 1\n"q\\"t" = 2\n', "c.toml")
    paths = [b.locator.key_path for b in a.blocks if b.type == "toml_key"]
    assert [list(tomllib.loads(f"{p} = 0")) for p in paths] == [["a\\b"], ['q"t']]


def test_a_parser_bug_on_hostile_sql_is_an_unparsed_statement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sqlglot

    def crash(*a: object, **kw: object) -> None:
        raise IndexError("list index out of range")

    monkeypatch.setattr(sqlglot, "parse_one", crash)
    a = extract(b"SELECT 1;", "q.sql", Options(sql_dialect="clickhouse"))
    assert [b.attrs["kind"] for b in a.blocks] == ["UNKNOWN"] and a.coverage.lost == (
        "sql_structure",
    )


def test_a_spaced_thematic_break_is_markup_not_a_list_item() -> None:
    a = extract(b"intro\n\n- - -\n\n* * *\n\n- item\n", "a.md")
    assert [(b.type, b.text) for b in a.blocks] == [("paragraph", "intro"), ("list", "- item")]
    chunks = chunk_artifact(a, size=500, stride=400)
    assert all("- - -" not in c.text and "* * *" not in c.text for c in chunks)


def test_a_dialect_whose_tokenizer_refuses_the_script_does_not_win_on_a_shorter_cut() -> None:
    from loom_extractor import sql

    # A backslash escapes a quote for ClickHouse and MySQL, not for Postgres.
    text = "SELECT 1;\nSELECT 'C:\\' AS p;\n"
    chosen, tied = sql.detect_dialect(text)
    assert "clickhouse" not in tied and "postgres" in tied
    a = extract(text.encode(), "q.sql")
    assert a.coverage.complete and [b.attrs["kind"] for b in a.blocks] == ["SELECT", "SELECT"]


def test_a_first_statement_longer_than_the_detection_window_still_splits_and_parses() -> None:
    from loom_extractor import sql

    text = "INSERT INTO t VALUES " + ", ".join(["(1)"] * 20_000) + ";\nSELECT 2;\n"
    assert len(text) > sql.DETECT_CHARS
    a = extract(text.encode(), "q.sql")
    assert len(a.blocks) == 2 and a.coverage.complete


def test_a_script_no_dialect_parses_is_split_by_one_that_reads_it() -> None:
    from loom_extractor import sql

    # ClickHouse's tokenizer refuses the backslash before the quote; Postgres reads the
    # script and parses nothing in it.
    text = "VACUUM 'C:\\';\nVACUUM x;\n"
    chosen, tied = sql.detect_dialect(text)
    assert tied == [] and chosen != "clickhouse"
    a = extract(text.encode(), "q.sql")
    assert a.attrs["dialect_source"] == "default" and len(a.blocks) == 2


def test_slides_that_open_with_a_rule_keep_their_first_heading() -> None:
    a = extract(b"---\n# Slide 1\n---\n# Slide 2\n", "deck.md")
    assert [b.text for b in a.blocks if b.type == "heading"] == ["Slide 1", "Slide 2"]
    # A YAML comment stays one when unspaced or indented; `# ` at a line's start is a heading.
    fm = extract(b"---\n#a comment\ntitle: x\n  # another\n---\n# Body\n", "a.md")
    assert fm.blocks[0].text == "#a comment\ntitle: x\n  # another" and fm.blocks[1].text == "Body"


def test_sql_offsets_are_characters_in_non_ascii_text() -> None:
    text = "-- Événements bruts\nSELECT 'café' AS ü FROM 事件;\nSELECT 2;\n"
    a = extract(text.encode(), "q.sql", Options(sql_dialect="clickhouse"))
    for b in a.blocks:
        assert text[b.locator.char_start : b.locator.char_end] == b.text


def test_a_long_heading_line_keeps_its_text_and_reads_markup_in_a_bounded_head() -> None:
    from loom_extractor import markdown

    for line in ("*a " * 30000, "<b>x " * 20000, "`a" * 30000):
        head, rest = line[: markdown.TITLE_MAX], line[markdown.TITLE_MAX :]
        # The patterns see only the head (so their cost is bounded); the rest is kept.
        title = markdown.clean_title(line)
        assert title.startswith(markdown.clean_title(head).rsplit(" ", 1)[0])
        assert title.endswith(" ".join(rest.split())[-200:])
    long = "word " * 200 + "tail"
    (heading, _) = extract(f"# {long}\n\nbody\n".encode(), "a.md").blocks
    assert heading.text.endswith("tail"), "no text of a long heading is dropped"
    h, _ = extract(f"{long}\n---\n\nbody\n".encode(), "a.md").blocks
    assert h.type == "heading" and h.text.endswith("tail")


def test_a_heading_line_with_a_long_run_of_blanks_is_read_whole() -> None:
    line = "# a" + " " * 40_000 + "b ##"
    heading, _ = extract((line + "\n\nx\n").encode(), "a.md").blocks
    assert (heading.type, heading.text) == ("heading", "a b")


def _within(seconds: float, fn: Any) -> None:
    """`fn` uses under `seconds` of this process's CPU time, so a test running beside it
    does not count; a loop that turned quadratic on these inputs would take minutes."""
    start = time.process_time()
    fn()
    assert time.process_time() - start < seconds


def test_hostile_markdown_lines_parse_in_bounded_time() -> None:
    for line in (
        "`" * 1_000_000 + "a`",  # a 1 MB backtick run with a trailing backtick
        "# a" + " " * 200_000 + "b",  # a heading's blanks before its closing run
        "# " + "*a " * 200_000,  # unclosed emphasis
        "<div>\n" + "===\n" * 50_000,  # an HTML line over many underlines
        "<br />" * 40 + "x\n---\n",  # self-closing tags a tag-only pattern could try 2^n ways
        "<img src=a /> " * 40 + "text\n---\n",
        "<pre " + "<pre\n" * 100_000,  # a raw-text block never closed
    ):
        _within(5.0, lambda line=line: extract(line.encode(), "a.md"))


def test_many_arrays_of_tables_parse_in_bounded_time() -> None:
    toml = "".join(f"[[x{i}]]\na = 1\n" for i in range(50_000)).encode()
    _within(20.0, lambda: extract(toml, "c.toml"))


def test_empty_front_matter_makes_no_block() -> None:
    a = extract(b"---\n\n---\n# Title\n", "a.md")
    assert [(b.type, b.text) for b in a.blocks] == [("heading", "Title")]


def test_a_long_heading_is_windowed_and_its_path_carries_a_bounded_title() -> None:
    long = "word " * 400 + "tail"
    a = extract(f"{long}\n---\n\nbody\n".encode(), "a.md")
    chunks = chunk_artifact(a, size=300, stride=250)
    heading_chunks = [c for c in chunks if c.block_type == "heading"]
    assert len(heading_chunks) > 1 and all(len(c.text) <= 300 for c in heading_chunks)
    assert heading_chunks[-1].text.endswith("tail"), "no word of the heading is dropped"
    (body,) = [c for c in chunks if c.text == "body"]
    assert len(body.section_path[0]) == 500


def test_a_window_of_a_long_heading_cites_the_headings_whole_span() -> None:
    for text in (
        "## " + "word " * 400 + "\n\nbody\n",
        "line one two three four\n" * 30 + "---\n\nbody\n",
    ):
        a = extract(text.encode(), "a.md")
        (h,) = [b for b in a.blocks if b.type == "heading"]
        windows = [c for c in chunk_artifact(a, size=300, stride=250) if c.block_type == "heading"]
        assert len(windows) > 1 and all(c.locator.to_dict() == h.locator.to_dict() for c in windows)
        assert text[h.locator.char_start : h.locator.char_end].count("\n") == (
            h.locator.line_end - h.locator.line_start
        )


def test_deeply_nested_hidden_html_parses_in_bounded_time() -> None:
    for html, complete in (
        ("<svg>" * 100_000 + "</svg>" * 100_000 + "<p>seen", True),
        ("<head>" + "<script>" * 100_000 + "<p>seen", False),  # unclosed: `seen` is hidden
        ("<p>" * 100_000, True),
        ("<a " * 100_000, True),  # attributes with no closing `>`
        ("<!--" + "x" * 1_000_000, True),  # an unclosed comment
    ):
        out: list[Artifact] = []
        _within(10.0, lambda html=html, out=out: out.append(extract(html.encode(), "x.html")))
        assert out[0].coverage.complete is complete, html[:20]


def test_a_tree_sqlglot_cannot_describe_is_an_unparsed_statement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from loom_extractor import sql

    def crash(*a: object, **kw: object) -> None:
        raise AttributeError("'NoneType' object has no attribute 'name'")

    monkeypatch.setattr(sql, "_describe", crash)
    a = extract(b"SELECT 1;", "q.sql", Options(sql_dialect="clickhouse"))
    assert [b.attrs["kind"] for b in a.blocks] == ["UNKNOWN"] and not a.coverage.complete


def test_many_sql_statements_on_one_line_split_in_bounded_time() -> None:
    from loom_extractor import sql

    text = "SELECT 1;" * 100_000
    _within(10.0, lambda: sql._spans(text, "clickhouse"))


def test_a_numbered_line_in_a_wrapped_paragraph_stays_prose() -> None:
    a = extract(b"Loom shipped in\n2019. The next release\ncame later.\n\n1. first\n", "a.md")
    assert [(b.type, b.text) for b in a.blocks] == [
        ("paragraph", "Loom shipped in\n2019. The next release\ncame later."),
        ("list", "1. first"),
    ]
    b = extract(b"Intro text\n1. starts a list\n", "a.md")
    assert [x.type for x in b.blocks] == ["paragraph", "list"]


def test_a_nul_byte_behind_a_utf8_byte_order_mark_is_binary() -> None:
    with pytest.raises(ExtractionError) as e:
        extract(b"\xef\xbb\xbfa\x00b", "a.txt")
    assert e.value.code == "unsupported_format"
    assert extract("hi".encode("utf-16"), "a.txt").blocks[0].text == "hi", "UTF-16 holds NULs"


def test_a_quoted_name_with_a_dot_keeps_its_whole_name_as_its_last_part() -> None:
    a = extract(
        b'CREATE TABLE "my.table" (id Int32) ENGINE = Memory;\nSELECT * FROM db."my.table";\n',
        "q.sql",
        Options(sql_dialect="clickhouse"),
    )
    create, select = a.blocks
    assert 'table"' not in create.keys and '"my.table"' in create.keys
    assert "my.table" in create.keys and "my.table" in select.keys


def test_a_bare_list_marker_over_a_rule_is_no_heading() -> None:
    for text in ("-\n---\n\nbody\n", "*\n===\n", "+\n---\n", "1.\n---\n", "-\r\n---\r\n"):
        assert "heading" not in [b.type for b in extract(text.encode(), "a.md").blocks], repr(text)


def test_a_multiline_toml_string_closing_on_extra_quotes_keeps_its_comment() -> None:
    for line in (b'a = """x""""  # c\n', b"a = '''x''''  # c\n", b'a = """x"""""  # c\n'):
        (key,) = extract(line, "c.toml").blocks
        assert key.attrs["comments"] == ["c"], line


def test_a_quoted_name_is_found_by_its_bare_last_part_however_it_was_quoted() -> None:
    for sql in (
        b"CREATE TABLE `my.table` (a Int8) ENGINE = Memory;",
        b"SELECT * FROM db.`my.table`;",
        b'SELECT * FROM db."my.table";',
    ):
        (b,) = extract(sql, "q.sql", Options(sql_dialect="clickhouse")).blocks
        assert "my.table" in b.keys and 'table"' not in b.keys, sql


def test_html_and_toml_name_the_python_whose_parser_read_them() -> None:
    import platform

    for data, name in ((b"<p>x</p>", "x.html"), (b"a = 1\n", "c.toml")):
        assert extract(data, name).parser_version.endswith(f"+python{platform.python_version()}")
    assert extract(b"# x\n", "a.md").parser_version == "1"


def test_text_inside_an_unclosed_hidden_element_is_recorded_as_not_read() -> None:
    a = extract(b"<p>shown</p><svg><p>after an svg left open", "x.html")
    assert [b.text for b in a.blocks] == ["shown"] and not a.coverage.complete
    (failure,) = a.coverage.failed
    assert failure.unit == "document" and "<svg>" in failure.reason


def test_a_quote_line_ends_the_paragraph_above_it() -> None:
    a = extract(b"foo\n> bar\n---\n", "a.md")
    assert "heading" not in [b.type for b in a.blocks]
    assert [b.text for b in a.blocks] == ["foo", "> bar"]


def test_a_client_command_line_is_no_sql_and_coverage_says_so() -> None:
    # `GO`, `/`, `DELIMITER` and backslash lines are the client's, not SQL: the
    # script splits on its `;` tokens all the same, and what a command line joins
    # is an unparsed statement that coverage records.
    a = extract(b"CREATE TABLE t (a int);\nGO\nSELECT 1;\n", "q.sql", Options(sql_dialect="tsql"))
    assert [b.attrs["kind"] for b in a.blocks] == ["CREATE TABLE", "UNKNOWN"]
    assert [f.unit for f in a.coverage.failed] == ["b1"] and a.coverage.lost == ("sql_structure",)


def test_a_zip_entry_name_that_is_not_its_flagged_encoding_is_corrupt() -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", "<w/>")
    data = bytearray(buf.getvalue())
    cd = data.rfind(b"PK\x01\x02")
    data[cd + 8 : cd + 10] = (0x0800).to_bytes(2, "little")  # the UTF-8 name flag
    name_at = cd + 46
    data[name_at : name_at + 1] = b"\xff"  # not UTF-8
    with pytest.raises(ExtractionError) as e:
        extract(bytes(data), "a.docx")
    assert e.value.code == "corrupt"


def test_one_line_sql_with_a_comment_after_every_semicolon_splits_in_bounded_time() -> None:
    from loom_extractor import sql

    text = "SELECT 1; /* c */ " * 100_000
    out: list[list[Any]] = []
    _within(10.0, lambda: out.append(sql._spans(text, "clickhouse")))
    assert len(out[0]) == 100_000
    # No span cuts a comment: each holds every `*/` its `/*` opens.
    assert all(
        text[sp.start : sp.end].count("/*") == text[sp.start : sp.end].count("*/") for sp in out[0]
    )
    assert text[out[0][-1].start : out[0][-1].end].endswith("*/"), "the last keeps its own comment"


def test_an_atx_line_whose_title_cleans_to_nothing_stays_text() -> None:
    for line in ('# <img src="logo.svg" alt="Loom">', '## <a name="setup"></a>'):
        for text in (f"{line}\n\ntext\n", f"{line}\ntext\n"):  # one line, a block of its own
            a = extract(text.encode(), "a.md")
            assert [(b.type, b.text) for b in a.blocks] == [
                ("paragraph", line),
                ("paragraph", "text"),
            ]
        b = extract(f"{line}\n---\n".encode(), "a.md")
        assert "heading" not in [x.type for x in b.blocks], "an ATX line is never a setext title"


@pytest.mark.parametrize(
    ("sql", "dialect", "attrs"),
    [
        (
            "CREATE FUNCTION f() RETURNS int AS $$ SELECT 1 $$ LANGUAGE sql;",
            "postgres",
            {"kind": "CREATE FUNCTION", "defines": "f"},
        ),
        (
            "CREATE PROCEDURE p() BEGIN SELECT 1; END",
            "mysql",
            {"kind": "CREATE PROCEDURE", "defines": "p"},
        ),
        ("ALTER TABLE t RENAME TO u;", "postgres", {"kind": "ALTER", "writes": ["t", "u"]}),
        (
            "COMMENT ON TABLE orders IS 'x';",
            "postgres",
            {"kind": "COMMENT ON", "writes": ["orders"]},
        ),
    ],
)
def test_a_statement_is_described_by_what_it_defines_and_writes_never_as_reading_itself(
    sql: str, dialect: str, attrs: dict[str, Any]
) -> None:
    # The first block: a body's inner `;` splits the statement (a client's body is
    # not kept whole), and the first block holds its head.
    b = extract(sql.encode(), "q.sql", Options(sql_dialect=dialect)).blocks[0]
    assert b.attrs == attrs


def test_a_line_starting_an_html_comment_over_a_rule_is_no_heading() -> None:
    text = "<!-- note --> Install\n---\n\nbody\n"
    assert "heading" not in [b.type for b in extract(text.encode(), "a.md").blocks]


def test_a_block_comment_starting_on_the_semicolon_line_belongs_to_that_statement() -> None:
    text = "SELECT 1; /* a\n b */\nSELECT 2;"
    first, second = extract(text.encode(), "q.sql", Options(sql_dialect="clickhouse")).blocks
    assert (first.text, first.attrs["comments"]) == ("SELECT 1; /* a\n b */", ["a\n b"])
    assert second.text == "SELECT 2;"


def test_a_bare_hash_is_an_empty_heading() -> None:
    for text in ("#\n---\n\nbody\n", "##\n===\n"):
        assert "heading" not in [b.type for b in extract(text.encode(), "a.md").blocks], text


def test_a_comment_on_the_line_a_crossing_comment_ends_on_belongs_with_it() -> None:
    text = "SELECT 1; /* a\n b */ /* c */\nSELECT 2;"
    first, second = extract(text.encode(), "q.sql", Options(sql_dialect="clickhouse")).blocks
    assert (first.text, first.attrs["comments"]) == (
        "SELECT 1; /* a\n b */ /* c */",
        ["a\n b", "c"],
    )
    assert second.attrs.get("comments") is None, "each comment is in the block whose text holds it"


def test_an_html_pages_title_is_an_attr_and_no_block() -> None:
    a = extract(b"<html><head><title>Loom  Guide</title></head><body><p>x</p>", "x.html")
    assert a.attrs == {"title": "Loom Guide"} and [b.text for b in a.blocks] == ["x"]
    assert a.coverage.complete
    assert extract(b"<p>no title</p>", "x.html").attrs == {}


def test_a_deck_opening_on_a_rule_then_a_heading_is_no_front_matter() -> None:
    a = extract(b"---\n# Agenda\nTime: 10am\n---\n# Next\n", "deck.md")
    assert "Agenda" in [b.text for b in a.blocks if b.type == "heading"]
    assert not any(b.attrs.get("front_matter") for b in a.blocks)


def test_a_toml_key_path_escapes_del_as_toml_requires() -> None:
    (key,) = extract(b'"a\\u007fb" = 1\n', "c.toml").blocks
    assert list(tomllib.loads(f"{key.locator.key_path} = 0")) == ["a\x7fb"]


def test_spellings_of_one_codec_address_one_artifact() -> None:
    a, b = (extract(b"x = 1\n", "c.toml", Options(encoding=e)) for e in ("utf8", "UTF-8"))
    assert a.options["encoding"] == b.options["encoding"] == "utf-8"
    assert a.sha256 == b.sha256


def test_a_declared_byte_order_codec_drops_the_byte_order_mark() -> None:
    for codec in ("utf-16-le", "utf-32-le"):
        data = b"\xff\xfe" + (b"\x00\x00" if codec == "utf-32-le" else b"")
        a = extract(data + "a = 1\n".encode(codec), "c.toml", Options(encoding=codec))
        assert a.coverage.lost == () and a.blocks[0].text == "a = 1", codec


def test_a_padded_dialect_name_addresses_the_same_artifact() -> None:
    a, b = (
        extract(b"SELECT 1;", "q.sql", Options(sql_dialect=d))
        for d in ("clickhouse", " clickhouse ")
    )
    assert a.options == b.options == {"sql_dialect": "clickhouse"} and a.sha256 == b.sha256


def test_a_dialect_with_settings_is_an_invalid_option() -> None:
    for declared in ("mysql, normalization_strategy", "mysql, normalization_strategy=lowercase"):
        with pytest.raises(ExtractionError) as e:
            extract(b"SELECT 1;", "q.sql", Options(sql_dialect=declared))
        assert e.value.code == "invalid_option", declared


def test_a_byte_order_mark_character_in_the_text_is_kept() -> None:
    """Only the mark a decoder leaves is dropped: a U+FEFF after it is text."""
    for declared in ("", "utf-8", "utf-8-sig"):
        assert decode(b"\xef\xbb\xbf\xef\xbb\xbfx", declared) == "\ufeffx", declared
    assert decode("\ufeffx".encode("utf-16"), "utf-16") == "\ufeffx"  # it adds its own mark
    assert decode(b"\xff\xfe" + "\ufeffx".encode("utf-16-le"), "utf-16-le") == "\ufeffx"


def test_a_quote_of_several_lines_is_one_block_whose_chunk_is_the_source() -> None:
    text = "> a\n> b\n\nafter\n"
    a = extract(text.encode(), "a.md")
    quote = a.blocks[0]
    assert (quote.text, quote.locator.line_start, quote.locator.line_end) == ("> a\n> b", 1, 2)
    assert chunk_artifact(a, size=1500, stride=1200)[0].text.startswith("> a\n> b")


def test_use_names_a_database_and_reads_no_table() -> None:
    b = extract(b"USE db;", "q.sql", Options(sql_dialect="clickhouse")).blocks[0]
    assert b.attrs.get("kind") == "USE" and "reads" not in b.attrs


def test_a_statement_naming_more_keys_than_the_contract_allows_keeps_the_first() -> None:
    from loom_extractor.artifact import MAX_KEYS

    names = " ".join(f"c{i}" for i in range(MAX_KEYS + 50))
    a = extract(f"FROB {names};".encode(), "q.sql", Options(sql_dialect="clickhouse"))
    (b,) = a.blocks
    assert len(b.keys) == MAX_KEYS and b.attrs["keys_truncated"] is True
    assert "exact_keys" in a.coverage.lost
    assert _read(a).blocks[0].keys == b.keys, "the server accepts it"


def test_a_toml_header_deeper_than_the_contract_is_cut_to_it_and_accepted() -> None:
    from loom_extractor.artifact import MAX_SECTION_DEPTH

    header = ".".join(f"t{i}" for i in range(MAX_SECTION_DEPTH + 1))
    a = extract(f"[{header}]\nk = 1\n".encode(), "c.toml")
    table, key = a.blocks
    assert len(table.section_path) == MAX_SECTION_DEPTH and table.attrs["section_truncated"]
    assert "section_path" in a.coverage.lost
    assert key.locator.key_path == f"{header}.k", "the locator keeps the whole path"
    assert len(_read(a).blocks) == 2, "the server accepts it"


def test_a_toml_header_of_many_parts_extracts_in_bounded_time() -> None:
    header = ".".join(["a"] * 50_000)
    _within(10.0, lambda: extract(f"[{header}]\nk = 1\n".encode(), "c.toml"))
    _within(10.0, lambda: extract(f"[[{header}]]\nk = 1\n[[{header}]]\n".encode(), "c.toml"))


def test_a_key_longer_than_the_contract_is_left_out_of_keys() -> None:
    long = "q" * 2000
    a = extract(f'"{long}" = 1\nshort = 2\n'.encode(), "c.toml")
    first, second = a.blocks
    assert first.keys == () and first.attrs["keys_truncated"] and long in first.text
    assert second.keys == ("short",) and "keys_truncated" not in second.attrs
    assert a.coverage.lost == ("exact_keys",) and not a.coverage.complete
    assert len(_read(a).blocks) == 2


@pytest.mark.parametrize(
    ("source", "name"),
    [
        ("# " + "x" * 600 + "\n\nbody\n", "a.md"),
        ("<h1>" + "x" * 600 + "</h1><p>body</p>", "a.html"),
    ],
)
def test_a_heading_longer_than_a_section_entry_is_cut_and_marked(source: str, name: str) -> None:
    a = extract(source.encode(), name)
    heading, body = a.blocks
    assert heading.text == "x" * 600, "the heading block keeps the whole title"
    assert body.section_path == ("x" * 500,) and body.attrs["section_truncated"]
    assert "section_path" in a.coverage.lost and not a.coverage.complete


def test_an_escape_codec_that_decodes_to_a_lone_surrogate_is_an_encoding_error() -> None:
    with pytest.raises(ExtractionError) as e:
        decode(b"a\\ud800b", "unicode_escape")
    assert e.value.code == "encoding"
    assert decode(b"caf\\u00e9", "unicode_escape") == "café"


def test_a_codec_that_raises_a_plain_unicode_error_is_an_encoding_error() -> None:
    for data, codec in ((b"!", "punycode"), (b"xn--a-.com", "idna")):
        with pytest.raises(ExtractionError) as e:
            decode(data, codec)
        assert e.value.code == "encoding", codec


def test_a_toml_integer_outside_signed_64_bits_makes_the_file_invalid_toml() -> None:
    a = extract(b"a = 9223372036854775807\nb = -9223372036854775808\n", "c.toml")
    assert [b.attrs["value"] for b in a.blocks] == [2**63 - 1, -(2**63)]
    for wide in (
        b"9223372036854775808",
        b"[[1, 99999999999999999999]]",
        b"1" * 4301,
        b"0x" + b"f" * 4000,
    ):
        a = extract(b"a = " + wide + b"\n", "c.toml")
        assert a.coverage.lost == ("toml_types",) and not a.coverage.complete, wide[:20]
        assert a.blocks[0].type == "paragraph"
    reasons = {
        b"[t]\nx = [[1, 99999999999999999999]]\n": "t.x[0][1] is an integer outside signed 64 bits",
        b"a = 99999999999999999999\nb = 99999999999999999999\n": "a is an integer outside signed 64 bits",
        b'["q k"]\n"x.y" = [-9223372036854775809]\n': '"q k"."x.y"[0] is an integer outside signed 64 bits',
        b"a = " + b"1" * 4301 + b"\n": "an integer of over 4,300 digits",
    }
    for source, reason in reasons.items():
        assert extract(source, "c.toml").coverage.failed[0].reason == f"invalid TOML: {reason}"


def test_the_wide_integer_check_is_linear_in_a_deep_table_of_long_keys() -> None:
    """A path that copies its prefix at each level is quadratic in the depth:
    20,000 levels of 1,000-character keys would copy about 200 GB. It runs under
    a trace function, as under coverage, where CPython does not extend a string
    in place. Four times the depth must cost under eight times the time (linear
    is 4x, quadratic 16x), a ratio a slower machine does not change."""
    from loom_extractor.tomlfile import _wide_integer

    def deep(levels: int) -> dict[str, object]:
        doc: dict[str, object] = {"v": 99999999999999999999}
        for _ in range(levels):
            doc = {"k" * 1000: doc}
        return doc

    def seconds(doc: dict[str, object]) -> float:
        best = float("inf")
        for _ in range(3):
            start = time.process_time()
            _wide_integer(doc)
            best = min(best, time.process_time() - start)
        return best

    small, large = deep(5_000), deep(20_000)
    previous = sys.gettrace()
    sys.settrace(lambda *_: None)
    try:
        ratio = seconds(large) / seconds(small)
    finally:
        sys.settrace(previous)
    assert ratio < 8, f"4x the depth took {ratio:.1f}x the time"
    path = _wide_integer(large)
    assert path.endswith(".v") and path.count(".") == 20_000


def test_a_wide_integer_in_a_reopened_table_is_named_in_key_order() -> None:
    source = b"[a]\nx = 1\n[c]\ny = 99999999999999999999\n[a.b]\nz = 99999999999999999999\n"
    reason = extract(source, "c.toml").coverage.failed[0].reason
    assert reason == "invalid TOML: a.b.z is an integer outside signed 64 bits"
