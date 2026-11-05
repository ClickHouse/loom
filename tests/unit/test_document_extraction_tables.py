"""The reference extractor's CSV and XLSX parsers: a sheet or a file as a typed
table and as the row blocks it is searched by, and the server reading both."""

from __future__ import annotations

import gc
import io
import json
import weakref
import zipfile
from typing import Any

import pytest

from loom_extractor import (
    ExtractionError,
    Options,
    csvfile,
    extract,
    ooxml,
    tabular,
    xlsxfile,
)
from server.knowledge.domain.extract import InvalidArtifact, parse_artifact, structural
from server.settings import settings
from tests.fixtures.xlsx_files import cell, sheet, workbook
from tests.unit.test_document_extraction import _within


def _columns(a: Any, i: int = 0) -> list[tuple[str, str]]:
    return [(c.name, c.type) for c in a.tables[i].columns]


def test_csv_values_are_typed_and_its_rows_are_cited_blocks() -> None:
    data = (
        b"Region,Quarter,Revenue,Day,At\n"
        b"EMEA,Q1,1000.5,2024-01-31,2024-01-31T09:30:00\n"
        b'"North, US",Q2,900,2024-04-30,2024-04-30 10:00\n'
        b",,,,\n"  # a blank record is no row
        b"APAC,,-7,,\n"
    )
    a = extract(data, "sales.csv")
    assert _columns(a) == [
        ("Region", "string"),
        ("Quarter", "string"),
        ("Revenue", "number"),
        ("Day", "date"),
        ("At", "datetime"),
    ]
    assert a.tables[0].rows == (
        ("EMEA", "Q1", 1000.5, "2024-01-31", "2024-01-31T09:30:00"),
        ("North, US", "Q2", 900, "2024-04-30", "2024-04-30T10:00:00"),
        ("APAC", None, -7, None, None),
    )
    assert a.tables[0].locator.to_dict() == {"row_start": 1, "row_end": 5}
    (block,) = a.blocks
    assert block.text.splitlines()[2] == "Region: APAC; Revenue: -7"
    assert (block.locator.to_dict(), block.attrs) == (
        {"row_start": 2, "row_end": 5},
        {"table_id": "t0"},
    )
    assert a.coverage.to_dict()["complete"] and a.coverage.unit == "rows"


@pytest.mark.parametrize(
    ("data", "name", "row"),
    [
        (b"a;b\n1;2\n", "x.csv", (1, 2)),  # sniffed
        (b"a\tb\n1\t2\n", "x.tsv", (1, 2)),  # by its name
        (b"a|b\n1|2\n", "x.csv", (1, 2)),
    ],
)
def test_csv_delimiters_are_sniffed_or_named(data: bytes, name: str, row: tuple[int, ...]) -> None:
    assert extract(data, name).tables[0].rows == (row,)


def test_a_csv_number_that_is_no_json_number_stays_text() -> None:
    big, inf = "9" * 30, "1e999"
    a = extract(f"a,b,c\n{big},{inf},{'1' * 5000}\n".encode(), "x.csv")
    assert a.tables[0].rows == ((big, "1e999", "1" * 5000),)
    assert big in a.blocks[0].text  # its digits, searchable
    parse_artifact(a.to_json(), fmt="csv", max_bytes=1 << 20)


def test_a_csv_in_a_declared_encoding_is_read_and_a_field_too_long_fails_the_rest() -> None:
    a = extract("Name\nJosé\n".encode("latin-1"), "x.csv", Options(encoding="latin-1"))
    assert a.tables[0].rows == (("José",),)
    long = "x" * (1 << 21)
    b = extract(f"a\n1\n{long}\n2\n".encode(), "x.csv")
    assert b.tables[0].rows == ((1,),)
    assert [f.unit for f in b.coverage.failed] == ["x.csv rows 3-"]


def test_rows_past_the_cell_budget_are_one_failed_unit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tabular, "MAX_CELLS", 8)  # the header and three rows of two columns
    a = extract(b"a,b\n" + b"".join(b"%d,%d\n" % (i, i) for i in range(5)), "x.csv")
    assert len(a.tables[0].rows) == 3
    assert [(f.unit, f.reason) for f in a.coverage.failed] == [
        ("x.csv rows 5-", "2 rows past what one artifact holds")
    ]
    assert (a.coverage.total, a.coverage.extracted) == (5, 3)


def _book() -> bytes:
    shared = ("Region", "Revenue", "EMEA", "APAC", "Q1", "Q2")
    sales = sheet(
        {
            1: [cell("A1", 0, t="s"), cell("B1", 1, t="s"), cell("C1")],
            2: [cell("A2", 2, t="s"), cell("B2", 1000.5), cell("C2", "#DIV/0!", t="e")],
            3: [cell("A3", 3, t="s"), cell("B3", 45322, s=1), cell("C3", 1, t="b")],
            4: [cell("A4", "Total", t="str", f="A2"), cell("B4", None, f="SUM(B2:B3)")],
            5: [cell("A5", "inline", t="inlineStr"), cell("B5", 45322.5, s=2)],
        },
        merges=("B1:C1",),
    )
    hidden = sheet({1: [cell("A1", 4, t="s")], 2: [cell("A2", 7)]})
    return workbook([("Sales", sales), ("Notes", hidden)], shared=shared, hidden=("Notes",))


def test_xlsx_cells_read_as_the_workbook_holds_them() -> None:
    a = extract(_book(), "book.xlsx")
    sales, notes = a.tables
    # The header merged over B and C names both; the repeat is numbered.
    assert _columns(a) == [("Region", "string"), ("Revenue", "mixed"), ("Revenue (2)", "bool")]
    assert sales.rows == (
        ("EMEA", 1000.5, {"error": "#DIV/0!"}),
        ("APAC", "2024-01-31", True),
        ("Total", None, None),  # a formula with no cached value reads empty
        ("inline", "2024-01-31T12:00:00", None),
    )
    assert sales.locator.to_dict() == {"sheet": "Sales", "row_start": 1, "row_end": 5}
    assert (notes.name, notes.attrs, notes.rows) == ("Notes", {"hidden": True}, ((7,),))
    assert a.coverage.lost == ("formula_values",) and not a.coverage.complete
    assert [(b.type, b.text, b.locator.to_dict()) for b in a.blocks if b.type == "heading"] == [
        ("heading", "Sales", {"sheet": "Sales"}),
        ("heading", "Notes", {"sheet": "Notes"}),
    ]
    rows = [b for b in a.blocks if b.attrs.get("table_id") == "t0"]
    assert rows[0].section_path == ("Sales",) and rows[0].locator.row_start == 2


def test_an_xlsx_of_the_1904_date_system_reads_its_dates_from_1904() -> None:
    book = workbook(
        [("S", sheet({1: [cell("A1", "d", t="str")], 2: [cell("A2", 0, s=1)]}))], date1904=True
    )
    assert extract(book, "b.xlsx").tables[0].rows == (("1904-01-01",),)


def test_a_damaged_sheet_fails_that_sheet_and_an_empty_one_is_no_table() -> None:
    book = workbook(
        [
            ("Good", sheet({1: [cell("A1", "a", t="str")], 2: [cell("A2", 1)]})),
            ("Bad", "x"),
            ("Empty", sheet({})),
        ]
    )
    a = extract(book, "b.xlsx")
    assert [t.name for t in a.tables] == ["Good"]
    assert [f.unit for f in a.coverage.failed] == ["sheet Bad"]
    with zipfile.ZipFile(io.BytesIO(book)) as z:
        assert "xl/worksheets/sheet3.xml" in z.namelist()


def test_a_wide_hostile_sheet_is_laid_out_only_as_far_as_the_byte_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cell at A and at XFD on each row: each row writes 16,384 values, nulls
    included, so the bytes bound how many are laid out, not the cells held."""
    monkeypatch.setattr(tabular, "MAX_BYTES", 1_200_000)  # its 16,384 names and a few rows
    rows = {n: [cell(f"A{n}", n), cell(f"XFD{n}", n)] for n in range(1, 11)}
    a = extract(workbook([("Wide", sheet(rows))]), "b.xlsx")
    assert 0 < len(a.tables[0].rows) < 9 and len(a.tables[0].columns) == 16_384
    assert [f.unit.split(" rows ")[0] for f in a.coverage.failed] == ["Wide"]
    assert _written(a) <= 1_200_000


def test_the_server_reads_tables_and_row_locators_and_a_chunk_spans_its_rows() -> None:
    a = extract(b"a,b\n" + b"".join(b"%d,x\n" % i for i in range(45)), "t.csv")
    parsed = parse_artifact(a.to_json(), fmt="csv", max_bytes=1 << 22)
    assert [t.to_dict() for t in parsed.tables] == [t.to_dict() for t in a.tables]
    chunks = structural.chunk_artifact(parsed, size=100_000, stride=100_000)
    assert [c.locator.to_dict() for c in chunks] == [{"row_start": 2, "row_end": 46}]


@pytest.mark.parametrize(
    ("change", "why"),
    [
        (lambda t: t["rows"].append([1]), "is not a list of 2 values"),
        (lambda t: t["columns"][0].update(type="money"), "is not a column type"),
        (lambda t: t["rows"][0].__setitem__(0, [1, 2]), "is not a scalar or a cell error"),
        (lambda t: t["rows"][0].__setitem__(0, {"error": 1}), "is not a scalar or a cell error"),
        (lambda t: t.update(id=""), "empty or repeated id"),
        (lambda t: t["locator"].update(row_end=0), "row_start..row_end"),
        (lambda t: t["locator"].update(sheet="s" * 501), "sheet: is over the 500 characters"),
        (lambda t: t.update(columns=t["columns"] * 8_193), "is not a list of at most 16384"),
        (lambda t: t["columns"][0].update(name="n" * 501), "is over 500 characters"),
        (lambda t: t.update(header_rows=-1), "is negative"),
        (lambda t: t["columns"].__setitem__(0, "a"), r"columns\[0\]: is not an object"),
    ],
)
def test_the_server_refuses_a_table_that_breaks_the_contract(change: Any, why: str) -> None:
    d = json.loads(extract(b"a,b\n1,2\n", "t.csv").to_json())
    change(d["tables"][0])
    with pytest.raises(InvalidArtifact, match=why):
        parse_artifact(json.dumps(d).encode(), fmt="csv", max_bytes=1 << 20)


def test_the_server_refuses_tables_that_are_no_list() -> None:
    d = json.loads(extract(b"a,b\n1,2\n", "t.csv").to_json())
    d["tables"] = {"t0": d["tables"][0]}
    with pytest.raises(InvalidArtifact, match="artifact.tables: is not a list"):
        parse_artifact(json.dumps(d).encode(), fmt="csv", max_bytes=1 << 20)


def test_a_float_is_searched_as_a_sheet_shows_it() -> None:
    rows = {1: [cell("A1", "Total", t="str")], 2: [cell("A2", "0.30000000000000004", f="0.1+0.2")]}
    a = extract(workbook([("S", sheet(rows))]), "b.xlsx")
    assert a.tables[0].rows == ((0.30000000000000004,),)
    assert a.blocks[-1].text == "Total: 0.3"


def test_xlsx_and_csv_are_parsed_and_parquet_is_not() -> None:
    assert extract(b"a\n1\n", "t.csv").format == "csv"
    assert extract(_book(), "t.xlsx").format == "xlsx"
    with pytest.raises(ExtractionError) as e:
        extract(b"PAR1 not read PAR1", "t.parquet")
    assert e.value.code == "format_unavailable"


def test_a_csv_whose_rows_are_wider_than_its_header_is_bounded_by_its_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A title line of one cell over 50-column rows: the budget counts the rows'
    cells, so only two rows are kept and laid out, not as many as one cell allows."""
    monkeypatch.setattr(tabular, "MAX_CELLS", 101)  # the title's cell and two rows
    row = b",".join(b"v" for _ in range(50))
    a = extract(b"Title\n" + b"\n".join(row for _ in range(10)) + b"\n", "x.csv")
    assert len(a.tables[0].rows) == 2
    assert (a.coverage.total, a.coverage.extracted) == (10, 2)


def _written(a: Any) -> int:
    """The bytes an artifact's tables and row blocks write in its JSON."""
    rows = [b for b in a.to_dict()["blocks"] if "table_id" in b.get("attrs", {})]
    compact = {"separators": (",", ":")}  # as the artifact is written
    return len(json.dumps(a.to_dict().get("tables", []), **compact)) + len(
        json.dumps(rows, **compact)
    )


@pytest.mark.parametrize(
    ("header", "value"),
    [
        (",".join(f"a fairly long column name {i:02d}" for i in range(20)), "y"),  # names dominate
        (",".join(f"列{i}" for i in range(20)), "東京大阪"),  # escapes: 6 bytes a character
    ],
)
def test_the_tables_an_artifact_writes_stay_within_the_byte_budget(
    monkeypatch: pytest.MonkeyPatch, header: str, value: str
) -> None:
    monkeypatch.setattr(tabular, "MAX_BYTES", 200_000)
    row = ",".join(value for _ in range(20))
    a = extract(("\n".join([header] + [row] * 5000) + "\n").encode(), "x.csv")
    assert 0 < len(a.tables[0].rows) < 5000
    assert [f.unit.split(" rows ")[0] for f in a.coverage.failed] == ["x.csv"]
    # The budget charges what is written, names, escapes and each block's own JSON
    # included, so the written bytes stay within it.
    assert _written(a) <= 200_000
    parse_artifact(a.to_json(), fmt="csv", max_bytes=1 << 22)


def test_a_header_with_no_rows_under_it_is_still_a_passage() -> None:
    a = extract(b"Approved by finance,on Monday\n", "x.csv")
    assert [(b.text, b.locator.to_dict()) for b in a.blocks] == [
        ("Approved by finance; on Monday", {"row_start": 1, "row_end": 1})
    ]
    book = workbook([("Notes", sheet({1: [cell("A1", "Approved by finance", t="str")]}))])
    assert [b.text for b in extract(book, "b.xlsx").blocks] == ["Notes", "Approved by finance"]


def test_xlsx_rows_and_cells_that_name_no_position_take_the_next_one() -> None:
    xml = (
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
        '<row><c t="inlineStr"><is><t>a</t></is></c><c t="inlineStr"><is><t>b</t></is></c></row>'
        "<row><c><v>1</v></c><c><v>2</v></c></row>"
        '<row r="5"><c r="B5"><v>3</v></c></row>'
        '<row><c r="A0"><v>4</v></c></row>'  # no row 0: the next row's first column
        "</sheetData></worksheet>"
    )
    t = extract(workbook([("S", xml)]), "b.xlsx").tables[0]
    assert t.rows == ((1, 2), (None, 3), (4, None))
    assert t.locator.to_dict() == {"sheet": "S", "row_start": 1, "row_end": 6}


def test_xlsx_serials_read_as_excel_dates_times_and_numbers() -> None:
    cells = {
        1: [cell("A1", "v", t="str")],
        2: [cell("A2", 1, s=1)],
        3: [cell("A3", 59, s=1)],
        4: [cell("A4", 60, s=1)],  # Excel's 1900-02-29
        5: [cell("A5", 61, s=1)],
        6: [cell("A6", 0.5, s=3)],  # built-in h:mm
        7: [cell("A7", 0.75, s=4)],  # custom [h]:mm
        8: [cell("A8", 10**9, s=1)],  # past year 9999: a number
    }
    t = extract(workbook([("S", sheet(cells))]), "b.xlsx").tables[0]
    assert [r[0] for r in t.rows] == [
        "1900-01-01",
        "1900-02-28",
        "1900-02-29",
        "1900-03-01",
        "12:00:00",
        "18:00:00",
        10**9,
    ]
    times = workbook([("S", sheet({1: [cell("A1", "t", t="str")], 2: [cell("A2", 0.25, s=3)]}))])
    assert [(c.name, c.type) for c in extract(times, "b.xlsx").tables[0].columns] == [("t", "time")]


def test_hostile_xlsx_values_cost_their_cell_not_the_workbook() -> None:
    big = "9" * 5000
    cells = {
        1: [cell("A1", "v", t="str")],
        2: [cell("A2", "NaN")],
        3: [cell("A3", "1e999")],
        4: [cell("A4", "12345678901234567")],  # exact, past a float's 53 bits
        5: [cell("A5", big, t="s")],  # no shared string has that index
        6: [f'<c r="A6" s="{big}"><v>7</v></c>'],  # nor any style
        7: [cell("A7", "not a date", t="d")],
        8: [cell("A8", 2_958_000, s=1)],  # past year 9999 in the 1904 system
    }
    a = extract(workbook([("S", sheet(cells))], date1904=True), "b.xlsx")
    assert [r[0] for r in a.tables[0].rows] == [
        "NaN",
        "1e999",
        12345678901234567,
        7,  # row 5, whose only cell names no shared string, is empty and no row
        "not a date",
        2_958_000.0,
    ]
    parse_artifact(a.to_json(), fmt="xlsx", max_bytes=1 << 20)


def test_xlsx_strings_leave_out_phonetic_readings_and_a_chart_sheet_is_no_table() -> None:
    rich = "<r><t>Tok</t></r><r><t>yo</t></r><rPh><t>とうきょう</t></rPh>"
    inline = '<c r="A2" t="inlineStr"><is><t>Kyo</t><rPh><t>きょう</t></rPh></is></c>'
    book = workbook(
        [("S", sheet({1: [cell("A1", 0, t="s")], 2: [inline]}))], shared_items=(rich,), chart=True
    )
    a = extract(book, "b.xlsx")
    assert [t.name for t in a.tables] == ["S"] and a.tables[0].columns[0].name == "Tokyo"
    assert a.tables[0].rows == (("Kyo",),)


def test_merged_ranges_spread_within_a_budget_and_repeats_spread_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tabular, "MAX_CELLS", 50)
    rows = {1: [cell("A1", "Region", t="str"), cell("D1", "x", t="str")], 2: [cell("A2", 1)]}
    merges = ("A1:C1",) * 1000 + ("D1:Z9",)  # repeats of one range, and one past the budget
    a = extract(workbook([("S", sheet(rows, merges=merges))]), "b.xlsx")
    assert [c.name for c in a.tables[0].columns][:4] == ["Region", "Region (2)", "Region (3)", "x"]
    assert [(f.unit, f.reason) for f in a.coverage.failed] == [
        ("S merges", "1 merged range not spread")
    ]


def test_a_sheet_named_like_a_failure_is_counted_once_and_a_long_name_is_cut() -> None:
    long = "N" * 600
    book = workbook(
        [
            ("sheet 2024", sheet({1: [cell("A1", "a", t="str")], 2: [cell("A2", 1)]})),
            (long, sheet({1: [cell("A1", "b", t="str")]})),
        ]
    )
    a = extract(book, "b.xlsx")
    assert (a.coverage.total, a.coverage.extracted) == (1, 1)
    assert a.tables[1].name == "N" * 500 and a.tables[1].locator.sheet == "N" * 500
    assert [(f.unit, f.reason) for f in a.coverage.failed] == [
        ("sheet names", "1 sheet name cut to 500 characters")
    ]
    parse_artifact(a.to_json(), fmt="xlsx", max_bytes=1 << 20)


def test_a_header_past_the_byte_budget_fails_its_table_and_merges_draw_on_the_artifact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A long string merged over a whole row, on each of three sheets: each header
    writes its names, cut to 500 characters, from one budget; the merges spread
    from one budget of cells; once the budget is spent, the third sheet is not read."""
    monkeypatch.setattr(tabular, "MAX_CELLS", 2 * 16_384 + 10)
    monkeypatch.setattr(tabular, "MAX_BYTES", 2 * 16_384 * 540)
    row = sheet({1: [cell("A1", 0, t="s")]}, merges=("A1:XFD1",))
    book = workbook([("One", row), ("Two", row), ("Three", row)], shared=("x" * 1_000_000,))
    a = extract(book, "b.xlsx")
    units = [f.unit for f in a.coverage.failed]
    assert "sheets 3-" in units and [t.name for t in a.tables][:1] == ["One"]
    assert _written(a) <= 2 * 16_384 * 540
    parse_artifact(a.to_json(), fmt="xlsx", max_bytes=1 << 27)


def test_xlsx_durations_round_and_dates_written_as_text() -> None:
    cells = {
        1: [cell("A1", "v", t="str")],
        2: [cell("A2", 1.5, s=4)],  # [h]:mm, 36 hours
        3: [cell("A3", "45000.3333333333", s=2)],  # 08:00 written to 15 digits
        4: [cell("A4", "2024-01-31", t="d")],
        5: [cell("A5", "2024-01-31T09:30:00", t="d")],
    }
    t = extract(workbook([("S", sheet(cells))]), "b.xlsx").tables[0]
    assert [r[0] for r in t.rows] == [
        "36:00:00",
        "2023-03-15T08:00:00",
        "2024-01-31",
        "2024-01-31T09:30:00",
    ]


@pytest.mark.parametrize("chunk", [1, 7, 64])
def test_xlsx_parts_read_the_same_in_chunks_of_any_size(
    monkeypatch: pytest.MonkeyPatch, chunk: int
) -> None:
    whole = extract(_book(), "b.xlsx").to_json()
    monkeypatch.setattr(ooxml, "_CHUNK", chunk)
    assert extract(_book(), "b.xlsx").to_json() == whole


def test_shared_strings_past_their_bound_read_empty_and_the_artifact_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(xlsxfile, "MAX_SHARED_BYTES", 200)
    rows = {1: [cell("A1", 0, t="s")], 2: [cell("A2", 1, t="s")], 3: [cell("A3", 2, t="s")]}
    a = extract(workbook([("S", sheet(rows))], shared=("head", "abcdefghij", "z")), "b.xlsx")
    assert a.tables[0].columns[0].name == "head" and a.tables[0].rows == ()
    assert "shared_strings" in a.coverage.to_dict()["lost"]


def test_long_and_non_ascii_sheet_names_stay_within_the_byte_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tabular, "MAX_BYTES", 200_000)
    rows = {n: [cell(f"A{n}", n)] for n in range(1, 2_000)}
    names = ["é" * 400 + str(i) for i in range(3)]
    a = extract(workbook([(n, sheet(rows)) for n in names]), "b.xlsx")
    assert a.coverage.failed and _written(a) <= 200_000
    parse_artifact(a.to_json(), fmt="xlsx", max_bytes=1 << 20)


def test_xlsx_edge_values_read_as_what_they_are() -> None:
    cells = {
        1: [cell("A1", "v", t="str")],
        2: [cell("A2", "1e305", s=4)],  # a duration no timedelta holds: a number
        3: [cell("A3", 1.5, s=5)],  # [H]:MM:SS
        4: [cell("A4", "2024-01-31T09:30:00.600", t="d")],
        5: [cell("A5", "2024-01-31T09:30:00+02:00", t="d")],
        6: ['<c r="A6"><v></v></c>', cell("B6", 1)],  # an empty <v> is an empty cell
        7: [cell("A7", "9999-12-31T23:59:59.6", t="d")],  # rounds past year 9999: text
    }
    t = extract(workbook([("S", sheet(cells))]), "b.xlsx").tables[0]
    assert [r[0] for r in t.rows] == [
        1e305,
        "36:00:00",
        "2024-01-31T09:30:01",
        "2024-01-31T09:30:00+02:00",
        None,
        "9999-12-31T23:59:59.6",
    ]


def test_a_merge_reaches_no_row_past_the_last_one_held(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(xlsxfile, "MAX_ROWS", 5)
    rows = {1: [cell("A1", "h", t="str")], 2: [cell("A2", 1)]}
    a = extract(workbook([("S", sheet(rows, merges=("A2:A900000",)))]), "b.xlsx")
    assert a.tables[0].rows == ((1,),) and not a.coverage.failed


def test_a_csv_record_past_its_bound_fails_the_rest(monkeypatch: pytest.MonkeyPatch) -> None:
    # A line past the bound is matched only that far, so it is never copied whole.
    line = csvfile._LINE.match("x" * (csvfile.MAX_RECORD_CHARS + 10))
    assert line is not None and line.end() == csvfile.MAX_RECORD_CHARS + 1
    monkeypatch.setattr(csvfile, "MAX_RECORD_CHARS", 100)
    a = extract(b'a,b\r\n1,"x\ny"\r' + b"," * 200 + b"\n3,4\n", "w.csv")
    assert a.tables[0].rows == ((1, "x\ny"),)
    assert [f.unit for f in a.coverage.failed] == ["w.csv rows 3-"]


def test_parts_past_what_a_workbook_inflates_to_in_all_are_cut_there(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = sheet({1: [cell("A1", "h", t="str")], 2: [cell("A2", "x" * 3_000, t="str")]})
    book = workbook([("One", rows), ("Two", rows)])
    monkeypatch.setattr(ooxml, "MAX_PACKAGE_BYTES", 5_000)
    a = extract(book, "b.xlsx")
    assert [t.name for t in a.tables] == ["One", "Two"] and a.tables[1].rows == ()
    assert [f.unit for f in a.coverage.failed] == ["Two rows 2-"]


def test_a_wide_csv_of_empty_fields_holds_only_its_values(monkeypatch: pytest.MonkeyPatch) -> None:
    """Rows of one value and 16,384 empty fields: each holds one cell, not a column
    per field (the header holds one too), and the table is only as wide as its values."""
    monkeypatch.setattr(tabular, "MAX_CELLS", 3)
    a = extract(b"h\n" + b"\n".join(b"x" + b"," * 16_384 for _ in range(5)) + b"\n", "w.csv")
    assert a.tables[0].rows == (("x",),) * 2 and len(a.tables[0].columns) == 1


def test_an_xlsx_value_past_its_bound_is_left_out_and_counted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(xlsxfile, "MAX_VALUE_CHARS", 10)
    rows = {
        1: [cell("A1", "h", t="str"), cell("B1", "i", t="str")],
        2: [cell("A2", "x" * 11, t="str"), cell("B2", "y" * 11, t="inlineStr")],
        3: [cell("A3", "short", t="str")],
    }
    a = extract(workbook([("S", sheet(rows))]), "b.xlsx")
    assert a.tables[0].rows == (("short", None),)
    assert [(f.unit, f.reason) for f in a.coverage.failed] == [
        ("S values", "2 values over 10 characters")
    ]


def test_sheets_past_the_budget_are_one_failed_unit_and_reasons_are_cut(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tabular, "MAX_BYTES", 2_000)
    data = sheet({1: [cell("A1", "h", t="str")], 2: [cell("A2", "v" * 300, t="str")]})
    a = extract(workbook([(f"S{i}", data) for i in range(1_000)]), "b.xlsx")
    assert len(a.coverage.failed) < 20 and a.coverage.failed[-1].unit.startswith("sheets ")
    assert a.coverage.total >= 1_000
    # A sheet whose part is missing, named by a long target.
    rels = "xl/_rels/workbook.xml.rels"
    book = _rewritten(workbook([("S", data)]), rels, b"worksheets/sheet1.xml", b"p" * 5_000)
    (failure,) = extract(book, "b.xlsx").coverage.failed
    assert len(failure.reason) == 200


def test_a_workbook_is_read_for_a_bounded_number_of_elements(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The sheet the bound runs out in keeps the rows read before it, and the
    sheets after it are one failed unit, never read."""
    monkeypatch.setattr(ooxml, "MAX_ELEMENTS", 40)
    rows = sheet({n: [cell(f"A{n}", n)] for n in range(1, 30)})
    a = extract(workbook([("One", rows), ("Two", rows)]), "b.xlsx")
    assert [t.name for t in a.tables] == ["One"] and 0 < len(a.tables[0].rows) < 28
    assert [f.unit.split(" rows ")[0] for f in a.coverage.failed] == ["One", "sheets 2-"]
    # The sheets past it name the bound that stopped them.
    assert "elements a package is read for" in a.coverage.failed[-1].reason


def test_column_names_are_unique_and_within_the_contract() -> None:
    long = "x" * 600
    a = extract(f"a,a,a (2),{long},{long}\n1,2,3,4,5\n".encode(), "n.csv")
    names = [c.name for c in a.tables[0].columns]
    assert names[:3] == ["a", "a (2)", "a (2) (2)"] and len(set(names)) == 5
    assert names[3] == "x" * 500 and names[4] == "x" * 492 + " (2)"
    parse_artifact(a.to_json(), fmt="csv", max_bytes=1 << 20)


def test_a_csv_header_is_its_text_as_written() -> None:
    a = extract(b"1e3,2024-01-01 10:00,-0\n1,2,3\n", "h.csv")
    assert [c.name for c in a.tables[0].columns] == ["1e3", "2024-01-01 10:00", "-0"]


def _rewritten(book: bytes, part: str, old: bytes, new: bytes) -> bytes:
    parts = zipfile.ZipFile(io.BytesIO(book))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name in parts.namelist():
            xml = parts.read(name)
            z.writestr(name, xml.replace(old, new) if name == part else xml)
    return buf.getvalue()


def test_xlsx_cells_and_sheets_not_read_are_failed_units(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = {1: [cell("A1", "h", t="str"), cell("XFE1", 9)], 2: [cell("A2", 1)]}
    book = workbook([("S", sheet(rows)), ("Lost", sheet(rows))])
    # The second sheet's relationship id names no relationship.
    book = _rewritten(book, "xl/workbook.xml", b'r:id="rId2"', b'r:id="rId9"')
    a = extract(book, "b.xlsx")
    assert a.tables[0].rows == ((1,),)
    assert [(f.unit, f.reason) for f in a.coverage.failed] == [
        ("S columns", "1 value past column 16384"),
        ("sheet Lost", "its relationship names no part"),
    ]
    monkeypatch.setattr(xlsxfile, "MAX_SHEETS", 1)
    a = extract(workbook([("S", sheet(rows)), ("T", sheet(rows))]), "b.xlsx")
    assert [t.name for t in a.tables] == ["S"] and a.coverage.failed[0].unit == "sheets 2-"


def test_the_element_bound_names_the_row_it_ran_out_in(monkeypatch: pytest.MonkeyPatch) -> None:
    # workbook, rels (a tree), styles: 16 elements; the sheet: worksheet, sheetData, then
    # each row of two cells with a <v> is five, so the bound runs out in row 3's second cell.
    monkeypatch.setattr(ooxml, "MAX_ELEMENTS", 16 + 2 + 5 * 2 + 4)
    rows = sheet({n: [cell(f"A{n}", n), cell(f"B{n}", n)] for n in range(1, 6)})
    a = extract(workbook([("S", rows)]), "b.xlsx")
    assert a.tables[0].rows == ((2, 2),)
    assert a.coverage.failed[0].unit == "S rows 3-"


def test_a_shared_string_past_the_value_bound_reads_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(xlsxfile, "MAX_VALUE_CHARS", 5)
    rows = {1: [cell("A1", 0, t="s")], 2: [cell("A2", 1, t="s")], 3: [cell("A3", 2, t="s")]}
    a = extract(workbook([("S", sheet(rows))], shared=("h", "toolong", "ok")), "b.xlsx")
    assert a.tables[0].rows == (("ok",),)
    assert [f.unit for f in a.coverage.failed] == ["shared strings"]


def test_merges_add_no_rows_past_a_tables_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(xlsxfile, "MAX_ROWS", 3)
    rows = {1: [cell("A1", "h", t="str")], 2: [cell("A2", 1)], 50: [cell("A50", 2)]}
    a = extract(workbook([("S", sheet(rows, merges=("A2:A40",)))]), "b.xlsx")
    assert len(a.tables[0].rows) == 3
    assert [f.unit for f in a.coverage.failed] == ["S merges"]
    assert not a.coverage.to_dict()["complete"]


def test_styles_past_their_bound_are_numbers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(xlsxfile, "MAX_STYLES", 1)
    rows = {1: [cell("A1", "d", t="str")], 2: [cell("A2", 45000, s=1)]}
    a = extract(workbook([("S", sheet(rows))]), "b.xlsx")
    assert a.tables[0].rows == ((45000,),)
    assert a.coverage.to_dict()["lost"] == ["number_formats"]


def test_held_text_is_bounded_by_its_widest_size(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tabular, "MAX_HELD_BYTES", 4 * 2_500)
    rows = {1: [cell("A1", "h", t="str")]}
    rows |= {n: [cell(f"A{n}", "v" * 1_000, t="inlineStr")] for n in range(2, 10)}
    a = extract(workbook([("S", sheet(rows))]), "b.xlsx")
    assert len(a.tables[0].rows) == 2 and a.coverage.failed[0].unit == "S rows 4-"


def test_shared_strings_and_styles_that_run_out_leave_a_partial_artifact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The workbook's three elements, then the shared strings run out partway.
    monkeypatch.setattr(ooxml, "MAX_ELEMENTS", 3 + 21)
    rows = {1: [cell("A1", 0, t="s")], 2: [cell("A2", 1, t="s")]}
    a = extract(workbook([("S", sheet(rows))], shared=tuple(f"s{i}" for i in range(100))), "b.xlsx")
    assert set(a.coverage.to_dict()["lost"]) == {"shared_strings", "number_formats"}
    assert [f.unit for f in a.coverage.failed] == ["S rows 1-"]


def test_a_merge_too_large_to_spread_leaves_the_budget_to_the_next_sheet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tabular, "MAX_CELLS", 1_000)
    rows = {1: [cell("A1", "h", t="str")], 2: [cell("A2", 1)], 3_000: [cell("Z3000", 2)]}
    first = sheet(rows, merges=("A2:Z2999",))  # 77,948 cells, past the 1,000
    second = sheet({1: [cell("A1", "h", t="str")], 2: [cell("A2", 3)]})
    a = extract(workbook([("One", first), ("Two", second)]), "b.xlsx")
    assert [t.name for t in a.tables] == ["One", "Two"] and a.tables[1].rows == ((3,),)
    assert [f.unit for f in a.coverage.failed] == ["One merges"]


def test_an_east_asian_built_in_date_format_reads_as_a_date() -> None:
    rows = {1: [cell("A1", "d", t="str")], 2: [cell("A2", 45000, s=6)]}
    assert extract(workbook([("S", sheet(rows))]), "b.xlsx").tables[0].rows == (("2023-03-15",),)


def test_a_row_too_wide_to_hold_is_counted_not_read(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tabular, "MAX_HELD_BYTES", 4 * 2_500)
    wide = [cell(f"{c}2", "v" * 1_000, t="inlineStr") for c in "ABCDEFGH"]
    rows = {1: [cell("A1", "h", t="str")], 2: wide, 3: [cell("A3", 1)]}
    a = extract(workbook([("S", sheet(rows))]), "b.xlsx")
    assert a.tables[0].rows == () and a.coverage.failed[0].unit == "S rows 2-"
    assert (a.coverage.total, a.coverage.extracted) == (2, 0)


def test_a_row_of_astral_values_is_measured_as_it_is_written(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Twelve bytes a character past U+FFFF, escaped: a row whose characters fit
    the byte budget but whose JSON does not is not laid out."""
    monkeypatch.setattr(tabular, "MAX_BYTES", 30_000)
    value = "\U0001d7d8" * 2_000  # 2,000 characters, 24,000 bytes escaped, twice
    a = extract(f"h\n{value}\nx\n".encode(), "a.csv")
    assert a.tables[0].rows == () and a.coverage.failed[0].unit == "a.csv rows 2-"
    assert _written(a) <= 30_000


def test_a_sheet_the_element_bound_cuts_counts_its_rest_and_a_damaged_one_its_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ooxml, "MAX_ELEMENTS", 40)
    rows = sheet({n: [cell(f"A{n}", n)] for n in range(1, 30)})
    a = extract(workbook([("One", rows)]), "b.xlsx")
    assert a.coverage.total == len(a.tables[0].rows) + 1
    monkeypatch.setattr(ooxml, "MAX_ELEMENTS", 50_000_000)
    monkeypatch.setattr(tabular, "MAX_CELLS", 30)
    broken = rows.replace("</sheetData>", "<row>")  # read to its end, then not XML
    a = extract(workbook([("Bad", broken), ("Good", rows)]), "b.xlsx")
    assert [t.name for t in a.tables] == ["Good"] and len(a.tables[0].rows) == 28


def test_a_sheet_whose_first_row_cannot_be_held_is_counted_as_failed_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tabular, "MAX_CELLS", 12)
    first = sheet({1: [cell("A1", "h", t="str")]} | {n: [cell(f"A{n}", n)] for n in range(2, 11)})
    second = sheet({1: [cell(f"{c}1", c, t="str") for c in "ABCDEFGHIJ"], 2: [cell("A2", 1)]})
    a = extract(workbook([("One", first), ("Two", second)]), "b.xlsx")
    assert [t.name for t in a.tables] == ["One"]
    assert [f.unit for f in a.coverage.failed] == ["Two rows 1-"]
    assert not a.coverage.to_dict()["complete"] and a.coverage.total == 9 + 1


def test_column_names_sharing_a_cut_stem_are_named_in_bounded_time() -> None:
    """32,768 names sharing a cut stem: naming that turned quadratic on them
    makes 134 M names of 500 characters, minutes, not milliseconds."""
    names = [tabular.Cell("p" * 495 + f"{k:05d}", "string") for k in range(16_384)] * 2
    out: list[str] = []
    _within(20.0, lambda: out.extend(tabular._names(names, len(names))[0]))
    assert len(set(out)) == len(out) and max(map(len, out)) <= 500


def test_a_strict_open_xml_workbook_is_refused_as_unsupported() -> None:
    book = workbook([("S", sheet({1: [cell("A1", "h", t="str")]}))])
    strict = b"http://purl.oclc.org/ooxml/spreadsheetml/main"
    book = _rewritten(
        book,
        "xl/workbook.xml",
        b"http://schemas.openxmlformats.org/spreadsheetml/2006/main",
        strict,
    )
    with pytest.raises(ExtractionError) as e:
        extract(book, "b.xlsx")
    assert e.value.code == "unsupported_format"


@pytest.mark.parametrize("data", [b'a,b\n1,"x\n2,y\n3,z\n', b'a,b\n1,"x"y\n2,z\n'])
def test_a_csv_quote_that_never_closes_fails_the_rest(data: bytes) -> None:
    a = extract(data, "q.csv")
    assert a.tables[0].rows == () and [f.unit for f in a.coverage.failed] == ["q.csv rows 2-"]
    assert not a.coverage.to_dict()["complete"]


def test_a_sheet_whose_heading_does_not_fit_counts_its_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tabular, "MAX_BYTES", 300)
    rows = sheet({1: [cell("A1", "h", t="str")], 2: [cell("A2", 1)], 3: [cell("A3", 2)]})
    a = extract(workbook([("N" * 400, rows)]), "b.xlsx")
    assert a.tables == () and (a.coverage.total, a.coverage.extracted) == (2, 0)


def test_a_tsv_reads_quotes_as_text() -> None:
    a = extract(b'mode\tn\n"Fast" mode\t1\n"open\t2\n', "m.tsv")
    assert a.tables[0].rows == (('"Fast" mode', 1), ('"open', 2))


def test_a_number_format_id_that_is_no_index_is_not_held() -> None:
    styles = xlsxfile._StylesStream()
    styles.start(xlsxfile._STYLES_ROOT, {})
    styles.start(xlsxfile._NUMFMT, {"numFmtId": "9" * 5_000, "formatCode": "d"})
    styles.start(xlsxfile._NUMFMT, {"numFmtId": "170", "formatCode": "d"})
    assert styles.custom == {170: "date"}


def test_row_blocks_are_cut_by_characters_and_a_cut_table_stays_under_the_chunk_cap() -> None:
    """Rows of about 250 characters: a block holds what fits in BLOCK_CHARS, and
    the rows a default artifact keeps index to fewer chunks than a source may
    publish by default."""
    header = ",".join(f"column{i:02d}" for i in range(10))
    row = ",".join("v" * 12 for _ in range(10))
    a = extract(("\n".join([header] + [row] * 120_000) + "\n").encode(), "w.csv")
    assert a.coverage.failed and len(a.tables[0].rows) < 100_000
    assert all(len(b.text) <= tabular.BLOCK_CHARS for b in a.blocks)
    chunks = structural.chunk_artifact(
        parse_artifact(a.to_json(), fmt="csv", max_bytes=64 << 20),
        size=settings.knowledge_chunk_size,
        stride=settings.knowledge_chunk_stride,
    )
    assert len(chunks) <= settings.knowledge_ingest_max_chunks


def test_a_format_code_past_excels_length_is_no_format() -> None:
    styles = xlsxfile._StylesStream()
    styles.start(xlsxfile._STYLES_ROOT, {})
    _within(
        5.0,
        lambda: styles.start(xlsxfile._NUMFMT, {"numFmtId": "170", "formatCode": "[" * 1_000_000}),
    )
    styles.start(xlsxfile._NUMFMT, {"numFmtId": "171", "formatCode": "yyyy"})
    assert styles.custom == {171: "date"}


def test_many_sheets_index_under_the_chunk_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each sheet's table is at least a chunk: charged a chunk's floor, so the
    sheets past what the text budget indexes fail, and the chunks stay under it."""
    monkeypatch.setattr(tabular, "MAX_TEXT_CHARS", 50 * tabular.CHUNK_FLOOR)
    data = sheet({1: [cell("A1", "x", t="str")]})
    a = extract(workbook([(f"S{i}", data) for i in range(200)]), "b.xlsx")
    chunks = structural.chunk_artifact(
        parse_artifact(a.to_json(), fmt="xlsx", max_bytes=64 << 20),
        size=settings.knowledge_chunk_size,
        stride=settings.knowledge_chunk_stride,
    )
    assert 0 < len(a.tables) <= 50 and len(chunks) <= 50
    # The sheets after the text runs out are one unit, never read.
    assert len(a.coverage.failed) <= 2 and a.coverage.failed[-1].unit.startswith("sheets ")


def test_a_sheet_past_what_the_package_inflates_to_keeps_the_rows_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ooxml, "MAX_PACKAGE_BYTES", 3_000)
    monkeypatch.setattr(ooxml, "_CHUNK", 64)
    rows = sheet({n: [cell(f"A{n}", n)] for n in range(1, 500)})
    a = extract(workbook([("S", rows)]), "b.xlsx")
    assert 0 < len(a.tables[0].rows) < 498
    assert a.coverage.failed[0].unit.startswith("S rows ")


def test_a_merged_header_names_its_columns_on_a_sheet_cut_at_the_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tabular, "MAX_CELLS", 40)
    rows = {1: [cell("A1", "Region", t="str"), cell("B1", "Revenue", t="str")]}
    rows |= {n: [cell(f"A{n}", n), cell(f"B{n}", n), cell(f"C{n}", n)] for n in range(2, 40)}
    a = extract(workbook([("S", sheet(rows, merges=("B1:C1",)))]), "b.xlsx")
    assert a.coverage.failed and [c.name for c in a.tables[0].columns] == [
        "Region",
        "Revenue",
        "Revenue (2)",
    ]


def test_a_package_past_its_total_parses_nothing_more() -> None:
    pkg = ooxml.Package(workbook([("S", sheet({1: [cell("A1", 1)]}))]))
    pkg._read = ooxml.MAX_PACKAGE_BYTES + 10
    seen: list[str] = []
    handler = xlsxfile._WorkbookStream()
    handler.start = lambda tag, attrs: seen.append(tag)  # type: ignore[method-assign]
    with pytest.raises(ooxml.Exhausted):
        pkg.stream("xl/workbook.xml", handler)
    assert seen == []


def test_a_tag_over_the_token_bound_cuts_its_sheet_there(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ooxml, "MAX_TOKEN_BYTES", 1_000)
    monkeypatch.setattr(ooxml, "_CHUNK", 256)
    rows = {n: [cell(f"A{n}", n)] for n in range(1, 10)}
    data = sheet(rows).replace('<row r="9">', '<row r="9" x="' + "y" * 5_000 + '">')
    a = extract(workbook([("S", data), ("T", sheet(rows))]), "b.xlsx")
    assert a.tables[0].rows == tuple((n,) for n in range(2, 9))
    assert [f.unit for f in a.coverage.failed] == ["S rows 9-"]
    assert [t.name for t in a.tables] == ["S", "T"]  # a token bound is the part's alone


def test_a_workbook_part_cut_keeps_the_sheets_it_named(monkeypatch: pytest.MonkeyPatch) -> None:
    """A tag past the token bound in the workbook part, after its first two
    sheets: those two are read, and the rest are one unit."""
    monkeypatch.setattr(ooxml, "MAX_TOKEN_BYTES", 1_000)
    monkeypatch.setattr(ooxml, "_CHUNK", 64)
    data = sheet({1: [cell("A1", "h", t="str")], 2: [cell("A2", 1)]})
    book = workbook([("One", data), ("Two", data), ("Three", data)])
    huge = b'<sheet name="Three" x="' + b"y" * 5_000 + b'"'
    book = _rewritten(book, "xl/workbook.xml", b'<sheet name="Three"', huge)
    a = extract(book, "b.xlsx")
    assert [t.name for t in a.tables] == ["One", "Two"]
    assert [f.unit for f in a.coverage.failed] == ["sheets 3-"]


def test_a_19_digit_integer_reads_exact() -> None:
    rows = {1: [cell("A1", "n", t="str")], 2: [cell("A2", "9000000000000000001")]}
    assert extract(workbook([("S", sheet(rows))]), "b.xlsx").tables[0].rows == (
        (9000000000000000001,),
    )


@pytest.mark.parametrize(
    "data", [b"h\n" + b"," * 20_000 + b"x\n", b"a,b\n1," + b"," * 20_000 + b"x\n"]
)
def test_a_csv_value_past_the_columns_a_table_holds_is_counted(data: bytes) -> None:
    a = extract(data, "w.csv")
    assert [f.unit for f in a.coverage.failed] == ["w.csv columns"]
    assert not a.coverage.to_dict()["complete"]


def test_a_damaged_sheet_past_the_cell_budget_leaves_the_next_sheet_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tabular, "MAX_CELLS", 30)
    many = sheet({n: [cell(f"A{n}", n)] for n in range(1, 60)}).replace("</sheetData>", "<row>")
    good = sheet({1: [cell("A1", "h", t="str")], 2: [cell("A2", 1)]})
    a = extract(workbook([("Bad", many), ("Good", good)]), "b.xlsx")
    assert [t.name for t in a.tables] == ["Good"] and a.tables[0].rows == ((1,),)


def test_a_workbook_part_past_the_package_total_is_cut_not_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = sheet({1: [cell("A1", "h", t="str")]})
    book = workbook([("One", data), ("Two", data)])
    book = _rewritten(book, "xl/workbook.xml", b"</workbook>", b" " * 20_000 + b"</workbook>")
    monkeypatch.setattr(ooxml, "MAX_PACKAGE_BYTES", 2_000)
    monkeypatch.setattr(ooxml, "_CHUNK", 64)
    a = extract(book, "b.xlsx")
    assert [f.unit for f in a.coverage.failed][0] == "sheets 3-"
    assert not a.coverage.to_dict()["complete"]


def test_a_cut_in_the_merged_ranges_names_the_merges(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ooxml, "MAX_TOKEN_BYTES", 1_000)
    monkeypatch.setattr(ooxml, "_CHUNK", 64)
    rows = {1: [cell("A1", "h", t="str")], 2: [cell("A2", 1)]}
    data = sheet(rows, merges=("B1:C1",)).replace(
        'ref="B1:C1"', 'ref="B1:C1" x="' + "y" * 5_000 + '"'
    )
    a = extract(workbook([("S", data)]), "b.xlsx")
    assert a.tables[0].rows == ((1,),)
    assert [f.unit for f in a.coverage.failed] == ["S merges"]
    assert (a.coverage.total, a.coverage.extracted) == (1, 1)


def test_a_damaged_sheet_loses_no_formula_values_for_the_artifact() -> None:
    bad = sheet({1: [cell("A1", "h", t="str")], 2: [cell("A2", None, f="1+1")]})
    bad = bad.replace("</sheetData>", "<row>")
    good = sheet({1: [cell("A1", "h", t="str")], 2: [cell("A2", 1)]})
    a = extract(workbook([("Bad", bad), ("Good", good)]), "b.xlsx")
    assert a.coverage.to_dict()["lost"] == []


def test_an_xlsx_chunk_cites_its_sheet_and_rows() -> None:
    rows = {1: [cell("A1", "h", t="str")]} | {n: [cell(f"A{n}", n)] for n in range(2, 6)}
    a = extract(workbook([("Sales", sheet(rows)), ("Notes", sheet(rows))]), "b.xlsx")
    chunks = structural.chunk_artifact(
        parse_artifact(a.to_json(), fmt="xlsx", max_bytes=1 << 20),
        size=settings.knowledge_chunk_size,
        stride=settings.knowledge_chunk_stride,
    )
    assert [c.locator.to_dict() for c in chunks] == [
        {"sheet": "Sales", "row_start": 2, "row_end": 5},
        {"sheet": "Notes", "row_start": 2, "row_end": 5},
    ]


def test_a_sheet_nested_too_deep_is_cut_there(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ooxml, "MAX_DEPTH", 8)
    rows = sheet({1: [cell("A1", "h", t="str")], 2: [cell("A2", 1)]})
    deep = rows.replace(
        "</sheetData>", '<row r="3">' + "<x>" * 20 + "</x>" * 20 + "</row></sheetData>"
    )
    a = extract(workbook([("S", deep), ("T", rows)]), "b.xlsx")
    assert a.tables[0].rows == ((1,),) and [t.name for t in a.tables] == ["S", "T"]
    assert [f.unit for f in a.coverage.failed] == ["S rows 3-"]


def test_sheets_past_the_budget_are_named_by_their_place_in_the_workbook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tabular, "MAX_CELLS", 2)
    data = sheet({1: [cell("A1", "h", t="str")], 2: [cell("A2", 1)]})
    a = extract(workbook([("S1", data), ("S2", data)], chart=True), "b.xlsx")
    assert [f.unit for f in a.coverage.failed] == ["sheets 3-"]  # the chart sheet is first
    assert a.attrs["sheets"] == 3


def test_a_sheet_of_too_many_distinct_names_is_cut_there(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ooxml, "MAX_NAMES", 64)
    rows = sheet({1: [cell("A1", "h", t="str")], 2: [cell("A2", 1)]})
    names = "".join(f"<e{i}/>" for i in range(200))
    many = rows.replace("</sheetData>", f'<row r="3">{names}</row></sheetData>')
    a = extract(workbook([("S", many), ("T", rows)]), "b.xlsx")
    assert a.tables[0].rows == ((1,),) and [t.name for t in a.tables] == ["S", "T"]
    assert [f.unit for f in a.coverage.failed] == ["S rows 3-"]


def test_a_sheet_as_a_spreadsheet_application_writes_it_reads_its_cells() -> None:
    """The parts an application adds around the cells: compatibility namespaces,
    a dimension, views, column widths, row attributes of other namespaces, page
    setup and an extension list."""
    mc = "http://schemas.openxmlformats.org/markup-compatibility/2006"
    x14ac = "http://schemas.microsoft.com/office/spreadsheetml/2009/9/ac"
    xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
        ' xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
        f' xmlns:mc="{mc}" mc:Ignorable="x14ac" xmlns:x14ac="{x14ac}">'
        '<sheetPr><tabColor rgb="FF00B050"/></sheetPr><dimension ref="A1:B3"/>'
        '<sheetViews><sheetView tabSelected="1" workbookViewId="0">'
        '<selection activeCell="B3" sqref="B3"/></sheetView></sheetViews>'
        '<sheetFormatPr defaultRowHeight="15" x14ac:dyDescent="0.25"/>'
        '<cols><col min="1" max="1" width="12.7109375" customWidth="1"/></cols><sheetData>'
        '<row r="1" spans="1:2" x14ac:dyDescent="0.25"><c r="A1" t="s"><v>0</v></c>'
        '<c r="B1" t="s"><v>1</v></c></row>'
        '<row r="2" spans="1:2" x14ac:dyDescent="0.25"><c r="A2" t="s"><v>2</v></c>'
        '<c r="B2"><v>1000.5</v></c></row>'
        '<row r="3" spans="1:2" ht="20" customHeight="1" x14ac:dyDescent="0.25">'
        '<c r="A3" s="1"><v>45000</v></c><c r="B3"><f>B2*2</f><v>2001</v></c></row>'
        '</sheetData><pageMargins left="0.7" right="0.7" top="0.75" bottom="0.75"'
        ' header="0.3" footer="0.3"/><pageSetup orientation="portrait" r:id="rId1"/>'
        '<extLst><ext uri="{x}" xmlns:x14="http://schemas.microsoft.com/office/spreadsheetml/2009/9/main">'
        "<x14:conditionalFormattings/></ext></extLst></worksheet>"
    )
    a = extract(workbook([("Sheet1", xml)], shared=("Region", "Revenue", "EMEA")), "b.xlsx")
    assert _columns(a) == [("Region", "mixed"), ("Revenue", "number")]
    assert a.tables[0].rows == (("EMEA", 1000.5), ("2023-03-15", 2001))
    assert a.coverage.to_dict()["complete"]


def test_format_codes_that_never_close_read_in_bounded_time() -> None:
    """A scan from every unclosed bracket again is quadratic; 65,536 codes of 255
    take minutes that way, not a second."""
    styles = xlsxfile._StylesStream()
    styles.start(xlsxfile._STYLES_ROOT, {})
    attrs = {"numFmtId": "170", "formatCode": "[" * 255}
    _within(10.0, lambda: [styles.start(xlsxfile._NUMFMT, attrs) for _ in range(70_000)])
    assert styles.custom == {}


def test_a_time_of_day_format_reads_the_time_of_any_day() -> None:
    rows = {1: [cell("A1", "t", t="str")], 2: [cell("A2", "45600.6", s=3)]}  # h:mm, =NOW()
    t = extract(workbook([("S", sheet(rows))]), "b.xlsx").tables[0]
    assert t.rows == (("14:24:00",),) and t.columns[0].type == "time"


def test_a_sheet_in_an_encoding_the_xml_parser_does_not_read_fails_alone() -> None:
    good = sheet({1: [cell("A1", "h", t="str")], 2: [cell("A2", 1)]})
    odd = '<?xml version="1.0" encoding="shift_jis"?>' + good
    a = extract(workbook([("Odd", odd), ("Good", good)]), "b.xlsx")
    assert [t.name for t in a.tables] == ["Good"]
    (failure,) = a.coverage.failed
    assert failure.unit == "sheet Odd" and "not XML" in failure.reason


def test_shared_strings_are_the_part_the_workbook_names_and_a_missing_one_is_lost() -> None:
    rows = {1: [cell("A1", 0, t="s")], 2: [cell("A2", 1, t="s")]}
    book = workbook([("S", sheet(rows))], shared=("h", "v"))
    moved = _rewritten(book, "xl/_rels/workbook.xml.rels", b"sharedStrings.xml", b"strings.xml")
    moved = _rename(moved, "xl/sharedStrings.xml", "xl/strings.xml")
    assert extract(moved, "b.xlsx").tables[0].rows == (("v",),)
    gone = _rename(book, "xl/sharedStrings.xml", "xl/elsewhere.xml")
    a = extract(gone, "b.xlsx")
    assert a.coverage.to_dict()["lost"] == ["shared_strings"]


def _rename(book: bytes, old: str, new: str) -> bytes:
    parts = zipfile.ZipFile(io.BytesIO(book))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name in parts.namelist():
            z.writestr(new if name == old else name, parts.read(name))
    return buf.getvalue()


def test_a_cut_after_the_merged_ranges_loses_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ooxml, "MAX_TOKEN_BYTES", 1_000)
    monkeypatch.setattr(ooxml, "_CHUNK", 64)
    rows = {1: [cell("A1", "h", t="str"), cell("B1", "i", t="str")], 2: [cell("A2", 1)]}
    data = sheet(rows, merges=("A2:B2",)).replace(
        "</worksheet>", '<extLst><ext uri="' + "u" * 5_000 + '"/></extLst></worksheet>'
    )
    a = extract(workbook([("S", data)]), "b.xlsx")
    assert a.tables[0].rows == ((1, 1),) and a.coverage.to_dict()["complete"]


def test_the_smallest_64_bit_integer_reads_exact() -> None:
    low = "-9223372036854775808"
    assert extract(f"n\n{low}\n".encode(), "i.csv").tables[0].rows == ((-(2**63),),)
    rows = {1: [cell("A1", "n", t="str")], 2: [cell("A2", low)]}
    assert extract(workbook([("S", sheet(rows))]), "b.xlsx").tables[0].rows == ((-(2**63),),)


def test_an_extension_list_before_the_rows_leaves_a_cut_in_them_counted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ooxml, "MAX_TOKEN_BYTES", 1_000)
    monkeypatch.setattr(ooxml, "_CHUNK", 64)
    rows = {n: [cell(f"A{n}", n)] for n in range(1, 10)}
    data = (
        sheet(rows)
        .replace(
            "<sheetData>", "<sheetViews><sheetView><extLst/></sheetView></sheetViews><sheetData>", 1
        )
        .replace('<row r="9">', '<row r="9" x="' + "y" * 5_000 + '">')
    )
    a = extract(workbook([("S", data)]), "b.xlsx")
    assert [f.unit for f in a.coverage.failed] == ["S rows 9-"]
    assert not a.coverage.to_dict()["complete"]


def test_the_text_budget_indexes_under_the_servers_chunk_defaults() -> None:
    """A block costs at most one chunk per CHUNK_FLOOR characters charged, as
    long as the server windows a long block no closer than CHUNK_FLOOR apart."""
    assert tabular.CHUNK_FLOOR <= settings.knowledge_chunk_stride <= settings.knowledge_chunk_size
    assert tabular.BLOCK_CHARS <= settings.knowledge_chunk_size
    assert tabular.MAX_TEXT_CHARS // tabular.CHUNK_FLOOR <= settings.knowledge_ingest_max_chunks


def test_merges_after_a_later_part_of_the_sheet_still_count_a_cut(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ooxml, "MAX_TOKEN_BYTES", 1_000)
    monkeypatch.setattr(ooxml, "_CHUNK", 64)
    rows = {1: [cell("A1", "h", t="str")], 2: [cell("A2", 1)]}
    big = 'ref="A1:B1" x="' + "y" * 5_000 + '"'
    data = sheet(rows, merges=("A1:B1",)).replace('ref="A1:B1"', big)
    data = data.replace("</sheetData>", "</sheetData><pageMargins/>")
    a = extract(workbook([("S", data)]), "b.xlsx")
    assert [f.unit for f in a.coverage.failed] == ["S merges"]


@pytest.mark.parametrize("start", [',"a', '"a,', ',"', "'a;"])
def test_the_delimiter_is_sniffed_in_bounded_time(start: str) -> None:
    data = (start * (70_000 // len(start)) + "\n1\n").encode()
    _within(5.0, lambda: extract(data, "s.csv"))


def test_shared_strings_past_their_bound_stop_being_read_and_the_sheets_are_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The items past the bound are not parsed, so they spend none of the
    package's elements: the sheet after them is read."""
    monkeypatch.setattr(xlsxfile, "MAX_SHARED_BYTES", 400)
    monkeypatch.setattr(ooxml, "MAX_ELEMENTS", 200)
    rows = {1: [cell("A1", 0, t="s")], 2: [cell("A2", 1)]}
    a = extract(workbook([("S", sheet(rows))], shared=tuple(f"s{i}" for i in range(500))), "b.xlsx")
    assert a.tables[0].rows == ((1,),)
    assert a.coverage.to_dict()["lost"] == ["shared_strings"]


def test_a_csv_with_single_quoted_prose_is_read_with_double_quotes() -> None:
    a = extract(b"id,comment\n1,He said 'yes' to it\n2,'90s style\n3,\"Smith, John\"\n", "c.csv")
    assert a.tables[0].rows == ((1, "He said 'yes' to it"), (2, "'90s style"), (3, "Smith, John"))
    assert a.coverage.to_dict()["complete"]


def test_a_csv_date_time_with_a_zone_keeps_it() -> None:
    a = extract(b"at\n2024-05-01T10:00:00Z\n2024-05-01T10:00:00+02:00\n", "z.csv")
    assert a.tables[0].rows == (("2024-05-01T10:00:00+00:00",), ("2024-05-01T10:00:00+02:00",))
    assert a.tables[0].columns[0].type == "datetime"


def test_a_cut_before_the_merges_after_a_custom_view_counts_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ooxml, "MAX_TOKEN_BYTES", 1_000)
    monkeypatch.setattr(ooxml, "_CHUNK", 64)
    rows = {1: [cell("A1", "h", t="str")], 2: [cell("A2", 1)]}
    views = (
        "<customSheetViews><customSheetView><pageMargins/></customSheetView>"
        '<customSheetView x="' + "y" * 5_000 + '"/></customSheetViews>'
    )
    data = sheet(rows, merges=("A1:B1",)).replace("</sheetData>", "</sheetData>" + views)
    a = extract(workbook([("S", data)]), "b.xlsx")
    assert [f.unit for f in a.coverage.failed] == ["S merges"]


def test_a_csv_keeps_the_rows_a_table_holds_and_counts_the_rest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tabular, "MAX_ROWS", 3)
    a = extract(b"h\n" + b"".join(b"%d\n" % n for n in range(6)), "r.csv")
    assert len(a.tables[0].rows) == 3 and [f.unit for f in a.coverage.failed] == ["r.csv rows 5-"]
    assert not a.coverage.to_dict()["complete"]


def test_an_xlsx_sheet_keeps_the_rows_a_table_holds_and_counts_the_rest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(xlsxfile, "MAX_ROWS", 3)
    rows = {1: [cell("A1", "h", t="str")]} | {n: [cell(f"A{n}", n)] for n in range(2, 8)}
    a = extract(workbook([("S", sheet(rows))]), "b.xlsx")
    assert len(a.tables[0].rows) == 3 and [f.unit for f in a.coverage.failed] == ["S rows 5-"]
    assert not a.coverage.to_dict()["complete"]


def test_a_cut_in_rows_after_merges_written_first_is_counted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ooxml, "MAX_TOKEN_BYTES", 1_000)
    monkeypatch.setattr(ooxml, "_CHUNK", 64)
    rows = {n: [cell(f"A{n}", n)] for n in range(1, 10)}
    data = sheet(rows, merges=("B1:C1",))
    merges = data[data.index("<mergeCells") : data.index("</mergeCells>") + len("</mergeCells>")]
    data = data.replace(merges, "").replace("<sheetData>", merges + "<sheetData>")
    data = data.replace('<row r="9">', '<row r="9" x="' + "y" * 5_000 + '">')
    a = extract(workbook([("S", data)]), "b.xlsx")
    assert [f.unit for f in a.coverage.failed] == ["S rows 9-"]
    assert not a.coverage.to_dict()["complete"]


def test_a_date_time_on_the_last_day_reads_as_one() -> None:
    rows = {1: [cell("A1", "t", t="str")], 2: [cell("A2", "2958465.5", s=2)]}
    assert extract(workbook([("S", sheet(rows))]), "b.xlsx").tables[0].rows == (
        ("9999-12-31T12:00:00",),
    )


def test_workbook_relationships_past_their_bound_refuse_the_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no relationships no sheet can be found: the file is refused, not
    read as an empty workbook."""
    monkeypatch.setattr(xlsxfile, "_RELS_BYTES", 100)
    with pytest.raises(ExtractionError) as e:
        extract(workbook([("S", sheet({1: [cell("A1", 1)]}))]), "b.xlsx")
    assert e.value.code == "corrupt"


@pytest.mark.parametrize("end", ["\n", "\r"])
def test_a_wide_csv_is_sniffed_from_whole_lines(end: str) -> None:
    line = ";".join(["v" * 20] * 430) + end  # 9 KB a line: the 64 KiB sample cuts the eighth
    data = (";".join(f"c{i}" for i in range(430)) + end + line * 8).encode()
    a = extract(data, "s.csv")
    assert len(a.tables[0].columns) == 430 and a.coverage.to_dict()["complete"]


def test_styles_past_their_bound_stop_being_read_and_the_sheets_are_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(xlsxfile, "MAX_STYLES", 2)
    monkeypatch.setattr(ooxml, "MAX_ELEMENTS", 80)
    book = workbook([("S", sheet({1: [cell("A1", "h", t="str")], 2: [cell("A2", 1)]}))])
    xfs = b"<xf/>" * 200
    book = _rewritten(book, "xl/styles.xml", b"</cellXfs>", xfs + b"</cellXfs>")
    a = extract(book, "b.xlsx")
    assert a.tables[0].rows == ((1,),)
    assert a.coverage.to_dict()["lost"] == ["number_formats"]


def test_a_macro_sheet_is_a_failed_unit_not_a_sheet_left_out() -> None:
    rows = {1: [cell("A1", "h", t="str")], 2: [cell("A2", 1)]}
    book = workbook([("S", sheet(rows)), ("M", sheet(rows))])
    rels = "xl/_rels/workbook.xml.rels"
    macro = b"http://schemas.microsoft.com/office/2006/relationships/xlMacrosheet"
    worksheet_two = b'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet2.xml"'
    book = _rewritten(
        book, rels, worksheet_two, b'Type="' + macro + b'" Target="worksheets/sheet2.xml"'
    )
    a = extract(book, "b.xlsx")
    assert [t.name for t in a.tables] == ["S"]
    assert [(f.unit, f.reason) for f in a.coverage.failed] == [
        ("sheet M", "xlMacrosheet sheets are not read")
    ]
    assert not a.coverage.to_dict()["complete"]


@pytest.mark.parametrize(
    ("head", "cut"),
    [
        ("n" * 2_000 + ",b", 1),
        ("a" + " " * 1_001 + "b,b", 1),
        # Text past what is read and a suffix cutting the stem: each column counted once.
        (",".join(["x" * 495 + " " * 600 + "y"] * 2), 2),
    ],
)
def test_a_header_cut_to_the_contract_is_a_failed_unit(head: str, cut: int) -> None:
    a = extract((head + "\n1,2\n").encode(), "h.csv")
    names = "column name" if cut == 1 else "column names"
    assert [(f.unit, f.reason) for f in a.coverage.failed] == [
        ("h.csv header", f"{cut} {names} cut to 500 characters")
    ]
    assert not a.coverage.to_dict()["complete"]


@pytest.mark.parametrize(
    "second",
    [
        '<row r="2"><c r="A2"><v>1</v></c></row><row r="2"><c r="A2"><v>2</v></c></row>',
        '<row r="2"><c r="A2"><v>1</v></c><c r="A2"><v>2</v></c></row>',  # in one row
    ],
)
def test_a_cell_written_twice_is_a_failed_unit(second: str) -> None:
    rows = '<row r="1"><c r="A1" t="str"><v>h</v></c></row>' + second
    data = sheet({}).replace("<sheetData></sheetData>", f"<sheetData>{rows}</sheetData>")
    a = extract(workbook([("S", data)]), "b.xlsx")
    assert [(f.unit, f.reason) for f in a.coverage.failed] == [
        ("S cells", "1 cell at a position already read")
    ]


def test_a_sheet_of_a_name_too_long_is_cut_there() -> None:
    rows = sheet({1: [cell("A1", "h", t="str")], 2: [cell("A2", 1)]})
    long = rows.replace("</sheetData>", f'<row r="3"><{"e" * 1_100}/></row></sheetData>')
    a = extract(workbook([("S", long)]), "b.xlsx")
    assert a.tables[0].rows == ((1,),) and [f.unit for f in a.coverage.failed] == ["S rows 3-"]


def test_parts_whose_root_is_in_another_namespace_fail_and_are_not_read_as_empty() -> None:
    rows = sheet({1: [cell("A1", 0, t="s")], 2: [cell("A2", 1, s=1)]})
    strict = "http://purl.oclc.org/ooxml/spreadsheetml/main"
    main = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    book = workbook([("Odd", rows.replace(main, strict)), ("Good", rows)], shared=("h",))
    a = extract(book, "b.xlsx")
    assert [t.name for t in a.tables] == ["Good"]
    assert [f.unit for f in a.coverage.failed] == ["sheet Odd"]
    for part, lost in (
        ("xl/styles.xml", "number_formats"),
        ("xl/sharedStrings.xml", "shared_strings"),
    ):
        odd = _rewritten(book, part, main.encode(), strict.encode())
        assert lost in extract(odd, "b.xlsx").coverage.to_dict()["lost"]


def test_a_cut_in_the_merges_and_merges_not_spread_are_one_unit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ooxml, "MAX_TOKEN_BYTES", 1_000)
    monkeypatch.setattr(ooxml, "_CHUNK", 64)
    monkeypatch.setattr(tabular, "MAX_CELLS", 4)
    rows = {1: [cell("A1", "h", t="str")], 2: [cell("A2", 1)]}
    big = 'ref="C1:D1" x="' + "y" * 5_000 + '"'
    data = sheet(rows, merges=("A1:Z1", "C1:D1")).replace('ref="C1:D1"', big)
    a = extract(workbook([("S", data)]), "b.xlsx")
    assert [f.unit for f in a.coverage.failed] == ["S merges"]


def test_the_csv_header_is_held_in_the_cell_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tabular, "MAX_CELLS", 3)  # the header's three cells, and no more
    a = extract(b"a,b,c\n1,2,3\n", "h.csv")
    assert a.tables[0].rows == () and [f.unit for f in a.coverage.failed] == ["h.csv rows 2-"]


def test_a_megabyte_header_merged_over_every_column_is_named_in_bounded_time() -> None:
    """One 1 MiB cell spread over 16,384 columns: what follows the read part of each
    is looked for, not copied (16 GB copied otherwise)."""
    for text in ("\U0001d7d8" * 1_000_000, "a" + " " * 1_000_000):  # a tail of spaces scans it all
        wide = tabular.Cell(text, "string")
        _within(20.0, lambda wide=wide: tabular._names([wide] * 16_384, 16_384))


def test_a_merge_cut_at_the_row_cap_gives_back_the_cells_it_did_not_fill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(xlsxfile, "MAX_ROWS", 3)
    # The first sheet's three cells and its merge's 898: none left for the second
    # sheet unless the 896 rows the cap leaves unfilled are given back.
    monkeypatch.setattr(tabular, "MAX_CELLS", 3 + 898 + 2)
    rows = {1: [cell("A1", "h", t="str")], 2: [cell("A2", 1)], 900: [cell("A900", 9)]}
    first = sheet(rows, merges=("A2:A899",))
    second = sheet({1: [cell("A1", "h", t="str")]} | {n: [cell(f"A{n}", n)] for n in range(2, 5)})
    a = extract(workbook([("One", first), ("Two", second)]), "b.xlsx")
    assert [t.name for t in a.tables] == ["One", "Two"]
    assert a.tables[1].rows == ((2,), (3,), (4,))
    assert [(f.unit, f.reason) for f in a.coverage.failed] == [
        ("One merges", "1 merged range cut at the row cap")
    ]


def test_merges_read_across_a_workbook_are_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """Past the ranges a workbook reads, a merge is counted as not spread, unread."""
    monkeypatch.setattr(xlsxfile, "MAX_MERGES", 2)
    rows = {
        1: [cell(f"{c}1", c, t="str") for c in "ABCE"],
    }
    data = sheet(rows, merges=("C1:D1", "E1:F1"))
    a = extract(workbook([("One", data), ("Two", data)]), "b.xlsx")
    assert [(f.unit, f.reason) for f in a.coverage.failed] == [
        ("Two merges", "2 merged ranges not spread")
    ]


@pytest.mark.parametrize("cut", [False, True])
def test_a_streamed_part_leaves_its_handler_to_be_freed_at_once(
    monkeypatch: pytest.MonkeyPatch, cut: bool
) -> None:
    """With no garbage collection, the handler is freed when the stream ends: the
    parser does not hold it in a cycle (a sheet's merges kept alive otherwise)."""
    if cut:
        monkeypatch.setattr(ooxml, "MAX_DEPTH", 2)
    rows = sheet({1: [cell("A1", "h", t="str")], 2: [cell("A2", 1)]})
    pkg = ooxml.Package(workbook([("S", rows)]))
    handler = xlsxfile._SheetStream(xlsxfile._Reader([], {}, False), tabular.Budget())
    alive = weakref.ref(handler)
    raised = False
    gc.disable()
    try:
        try:
            pkg.stream("xl/worksheets/sheet1.xml", handler)
        except ooxml.Cut:
            raised = True
        assert raised == cut
        del handler
        assert alive() is None
    finally:
        gc.enable()


def test_a_damaged_sheets_merges_stay_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """The bound is on the time merges take: a damaged sheet's were read all the same."""
    monkeypatch.setattr(xlsxfile, "MAX_MERGES", 2)
    rows = {1: [cell("A1", "Region", t="str")], 2: [cell("A2", 1)]}
    bad = sheet(rows, merges=("A1:B1", "A2:B2")).replace("</worksheet>", "<row>")
    good = sheet(rows, merges=("A1:B1",))
    a = extract(workbook([("Bad", bad), ("Good", good)]), "b.xlsx")
    assert [(f.unit, f.reason) for f in a.coverage.failed][1:] == [
        ("Good merges", "1 merged range not spread")
    ]


def test_an_empty_header_cell_is_named_by_its_column_in_the_sheet() -> None:
    rows = {1: [cell("C1", "c", t="str"), cell("E1", "e", t="str")], 2: [cell("D2", 1)]}
    a = extract(workbook([("S", sheet(rows))]), "b.xlsx")
    assert [c.name for c in a.tables[0].columns] == ["c", "column 4", "e"]


def test_an_xlsx_cell_of_only_whitespace_is_empty_as_a_csv_field_is() -> None:
    rows = {
        1: [cell("A1", 0, t="s")],  # a space typed to clear the cell
        2: [cell("A2", "Name", t="str"), cell("B2", "Age", t="inlineStr")],
        3: [cell("A3", "Ann", t="str"), cell("B3", 30)],
        4: [cell("A4", "Bo", t="str"), cell("B4", "  ", t="str")],
    }
    a = extract(workbook([("S", sheet(rows))], shared=("   ",)), "b.xlsx")
    assert _columns(a) == [("Name", "string"), ("Age", "number")]
    assert a.tables[0].rows == (("Ann", 30), ("Bo", None))
