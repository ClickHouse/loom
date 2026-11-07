"""The reference extractor's PDF layout reading: tables from ruled grids and
aligned columns, headings from type, columns, page furniture and turned pages."""

from __future__ import annotations

import time
from collections import Counter
from collections.abc import Callable
from io import BytesIO

import pytest
from pypdf import PdfReader

from loom_extractor import extract, ocr, pdffile
from loom_extractor import pdflayout as layout
from server.knowledge.domain.extract import parse_artifact
from tests.fixtures.office_files import laid_out_pdf, rule, text_at


@pytest.fixture(autouse=True)
def _no_ocr(monkeypatch: pytest.MonkeyPatch) -> None:
    """These files' pictures need OCR whether or not this machine has Tesseract."""
    monkeypatch.setattr(ocr, "engine", lambda: None)


def _blocks(a) -> list[tuple[str, int, str]]:
    return [(b.type, b.depth, b.text) for b in a.blocks]


def _grid(x: list[float], top: float, rows: list[list[str]], h: float = 20) -> bytes:
    """A ruled table: its rules and each cell's text, rows from `top` down."""
    out = b""
    bottom = top - h * len(rows)
    for k in range(len(rows) + 1):
        out += rule(x[0], top - h * k, x[-1], top - h * k)
    for xv in x:
        out += rule(xv, top, xv, bottom)
    for r, row in enumerate(rows):
        for c, cell in enumerate(row):
            out += text_at(x[c] + 4, top - h * r - 14, cell, font=2 if r == 0 else 1, size=10)
    return out


TABLE = [["Table", "Days", "Owner"], ["events", "30", "platform"], ["rollups", "365", "analytics"]]


def test_a_ruled_table_is_a_typed_table_whose_rows_are_cited_by_page() -> None:
    page = text_at(72, 720, "Retention", font=2, size=16) + text_at(
        72, 690, "Every table keeps its rows."
    )
    page += _grid([72, 200, 300, 420], 660, TABLE)
    a = extract(laid_out_pdf([page]), "r.pdf")
    (t,) = a.tables
    assert [(c.name, c.type) for c in t.columns] == [
        ("Table", "string"),
        ("Days", "number"),
        ("Owner", "string"),
    ]
    assert t.rows == (("events", 30, "platform"), ("rollups", 365, "analytics"))
    assert t.locator.to_dict() == {"page_start": 1, "page_end": 1, "row_start": 1, "row_end": 3}
    rows = [b for b in a.blocks if b.attrs.get("table_id") == t.id]
    assert [b.text for b in rows] == [
        "Table: events; Days: 30; Owner: platform\nTable: rollups; Days: 365; Owner: analytics"
    ]
    assert rows[0].locator.to_dict() == {
        "page_start": 1,
        "page_end": 1,
        "row_start": 2,
        "row_end": 3,
    }
    assert rows[0].section_path == ("Retention",)
    assert a.coverage.complete
    assert parse_artifact(a.to_json(), fmt="pdf", max_bytes=1 << 20).tables[0].rows == t.rows


def test_aligned_columns_without_rules_are_a_table_and_a_wrapped_cell_one_cell() -> None:
    page = text_at(72, 720, "The limits below hold for every tenant.")
    y = 690
    for name, default, desc in [
        ("Setting", "Default", "Description"),
        ("max_rows", "5000", "Rows one read"),
    ]:
        page += text_at(72, y, name) + text_at(200, y, default) + text_at(300, y, desc)
        y -= 16
    page += text_at(300, y, "returns at most")  # the description wraps to a second line
    y -= 16
    page += text_at(72, y, "ttl_days") + text_at(200, y, "30") + text_at(300, y, "Days a row lives")
    a = extract(laid_out_pdf([page]), "b.pdf")
    (t,) = a.tables
    assert [c.name for c in t.columns] == ["Setting", "Default", "Description"]
    assert t.rows == (
        ("max_rows", 5000, "Rows one read returns at most"),
        ("ttl_days", 30, "Days a row lives"),
    )
    assert _blocks(a)[0] == ("paragraph", 0, "The limits below hold for every tenant.")


def test_a_table_that_goes_on_over_the_next_page_is_one_table_its_header_read_once() -> None:
    x = [72, 200, 300, 420]
    first = _grid(x, 700, [TABLE[0], *[[f"t{i}", str(i), "ops"] for i in range(30)]])
    second = _grid(x, 740, [TABLE[0], *[[f"t{i}", str(i), "ops"] for i in range(30, 40)]])
    second += text_at(72, 400, "After the table.")
    a = extract(laid_out_pdf([first, second]), "s.pdf")
    (t,) = a.tables
    assert len(t.rows) == 40 and t.rows[30] == ("t30", 30, "ops")
    assert t.locator.to_dict() == {"page_start": 1, "page_end": 2, "row_start": 1, "row_end": 41}
    blocks = [b for b in a.blocks if b.attrs.get("table_id")]
    assert blocks[-1].locator.page_end == 2 and blocks[0].locator.page_start == 1


def test_headings_come_from_size_and_weight_and_numbering_ranks_one_style() -> None:
    page = (
        text_at(72, 740, "Storage design", font=2, size=20)
        + text_at(72, 710, "Every replica holds a full copy of the data.")
        + text_at(72, 680, "1. Write path", font=2, size=11)
        + text_at(72, 660, "Writes go to the leader first and then fan out.")
        + text_at(72, 630, "1.1 Batching", font=2, size=11)
        + text_at(72, 610, "Batches close after one second or one megabyte.")
        + text_at(72, 580, "Note: this line is bold but ends a sentence.", font=2, size=11)
    )
    a = extract(laid_out_pdf([page]), "h.pdf")
    heads = [(b.depth, b.text) for b in a.blocks if b.type == "heading"]
    assert heads == [(1, "Storage design"), (2, "1. Write path"), (3, "1.1 Batching")]
    assert a.blocks[-1].section_path == ("Storage design", "1. Write path", "1.1 Batching")


def test_two_columns_read_left_then_right_between_full_width_lines() -> None:
    page = text_at(72, 750, "A title across the page", font=2, size=18)
    left = [f"Left column line {i} of the storage policy text." for i in range(12)]
    right = [f"Right column line {i} of the exceptions text here." for i in range(12)]
    for i, (lt, rt) in enumerate(zip(left, right, strict=True)):
        page += text_at(50, 700 - 14 * i, lt, size=10) + text_at(320, 700 - 14 * i, rt, size=10)
    a = extract(laid_out_pdf([page]), "c.pdf")
    text = "\n".join(b.text for b in a.blocks)
    assert text.index("Left column line 11") < text.index("Right column line 0")
    assert a.blocks[0].text == "A title across the page"


def test_page_furniture_is_left_out_and_named() -> None:
    pages = [
        text_at(72, 770, "Design review draft", size=8)
        + text_at(72, 700, f"Body text of page {n} that says something new.")
        + text_at(500, 30, f"Page {n}", size=8)
        for n in (1, 2, 3)
    ]
    a = extract(laid_out_pdf(pages), "f.pdf")
    assert [b.text for b in a.blocks] == [
        f"Body text of page {n} that says something new." for n in (1, 2, 3)
    ]
    assert a.attrs["furniture"] == [
        {"text": "Design review draft", "pages": 3},
        {"text": "Page 1", "pages": 3},  # one entry for each shape, digits aside
    ]
    assert any("furniture" in w for w in a.warnings)
    assert a.coverage.complete


def test_a_page_shown_turned_is_read_in_its_texts_direction() -> None:
    page = text_at(72, 720, "Escalation", font=2, size=16) + _grid([72, 200, 300, 420], 680, TABLE)
    a = extract(laid_out_pdf([page], rotate=(0,)), "t.pdf")
    assert ("heading", 1, "Escalation") in _blocks(a)
    assert a.tables[0].rows == (("events", 30, "platform"), ("rollups", 365, "analytics"))


def test_a_list_marker_set_apart_from_its_text_is_no_column() -> None:
    page = text_at(72, 720, "The rollout has three steps.")
    for i, step in enumerate(
        ["Drain the old pool first", "Shift a tenth of traffic", "Shift the rest"]
    ):
        page += text_at(80, 700 - 16 * i, "\x95") + text_at(100, 700 - 16 * i, step)
    a = extract(laid_out_pdf([page]), "l.pdf")
    assert a.tables == ()
    assert [b.type for b in a.blocks] == ["paragraph", "list", "list", "list"]


def test_a_page_whose_layout_holds_less_than_its_text_layer_is_read_as_that(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pdffile, "_layout_chars", lambda read: Counter())
    a = extract(laid_out_pdf([text_at(72, 720, "Only line")]), "g.pdf")
    assert [b.text for b in a.blocks] == ["Only line"]
    assert a.warnings == ("page 1: read as its text layer; its layout held less of its text",)


def test_a_page_of_more_rules_than_the_bound_is_read_without_ruled_tables() -> None:
    page = b"".join(
        rule(72, 100 + k * 0.1, 500, 100 + k * 0.1) for k in range(layout._MAX_RULES + 10)
    )
    page += _grid([72, 200, 300, 420], 700, TABLE)
    a = extract(laid_out_pdf([page]), "m.pdf")
    assert "events" in "\n".join(b.text for b in a.blocks)
    assert a.coverage.complete
    reader = PdfReader(BytesIO(laid_out_pdf([page])))
    # no rules past the bound
    assert layout.drawing(reader.pages[0], reader, lambda x, y: (x, y)).rules == []


def test_a_fonts_unicode_map_gives_its_codes_widths() -> None:
    cmap = (
        b"begincmap 2 beginbfchar <0003> <0020> <0004> <0041> endbfchar"
        b" 1 beginbfrange <0010> <0012> <0061> endbfrange"
        b" 1 beginbfrange <0020> <0021> [<0062> <00660069>] endbfrange endcmap"
    )

    class Stream:
        def get_object(self) -> Stream:
            return self

        def get_data(self) -> bytes:
            return cmap

    assert layout._to_unicode({"/ToUnicode": Stream()}) == {
        3: " ",
        4: "A",
        0x10: "a",
        0x11: "b",
        0x12: "c",
        0x20: "b",
        0x21: "fi",
    }


class _CmapStream:
    def __init__(self, data: bytes) -> None:
        self.data = data

    def get_object(self) -> _CmapStream:
        return self

    def get_data(self) -> bytes:
        return self.data


@pytest.mark.parametrize("block", [b"beginbfchar", b"beginbfrange"])
def test_an_unclosed_unicode_map_block_is_read_in_linear_time(block: bytes) -> None:
    """A map of block openings with no close is read once, not rescanned from each
    opening to the end: four times the openings cost about four times the time."""

    def cpu(n: int) -> float:
        start = time.process_time()
        assert layout._to_unicode({"/ToUnicode": _CmapStream(block * n)}) == {}
        return time.process_time() - start

    small, large = cpu(2_000), cpu(8_000)
    assert large < 8 * small + 0.05, (small, large)


def test_a_map_block_runs_to_its_first_close() -> None:
    cmap = b"beginbfchar <01> <0041> beginbfchar <02> <0042> endbfchar <03> <0043> endbfchar"
    assert layout._to_unicode({"/ToUnicode": _CmapStream(cmap)}) == {1: "A", 2: "B"}


def _cid_font(widths: list[object], cmap: bytes) -> object:
    from pypdf.generic import (
        ArrayObject,
        DecodedStreamObject,
        DictionaryObject,
        FloatObject,
        NameObject,
        NumberObject,
    )

    def num(v: object) -> object:
        return ArrayObject(num(x) for x in v) if isinstance(v, list) else NumberObject(v)

    stream = DecodedStreamObject()
    stream.set_data(cmap)
    desc = DictionaryObject(
        {
            NameObject("/DW"): FloatObject(1000),
            NameObject("/W"): ArrayObject(num(w) for w in widths),
        }
    )
    return DictionaryObject(
        {
            NameObject("/Subtype"): NameObject("/Type0"),
            NameObject("/BaseFont"): NameObject("/AAAAAA+Sans-Bold"),
            NameObject("/DescendantFonts"): ArrayObject([desc]),
            NameObject("/ToUnicode"): stream,
        }
    )


def test_a_cid_fonts_widths_come_through_its_unicode_map() -> None:
    cmap = b"beginbfchar <0003> <0043> <0004> <0065> endbfchar beginbfrange <000A> <000B> <006C> endbfrange"
    w = layout._Widths(_cid_font([3, [500, 600], 10, 11, 250], cmap))
    assert w.bold and w.of("Cell") == 500 + 600 + 250 + 250
    assert w.of("x") == 1000  # a character the map does not give: the font's default


def test_a_fonts_widths_past_their_budget_are_read_in_bounded_work() -> None:
    class Counted(int):
        reads = 0

        def __float__(self) -> float:
            Counted.reads += 1
            return float(int(self))

    # each range charges the values it writes, so repeats of a whole range stop at once
    layout._Widths(_cid_font([Counted(0), Counted(65_534), Counted(500)] * 50_000, b""))
    assert Counted.reads <= 3


def test_a_table_over_three_pages_is_one_table_and_rows_at_the_margins_stay_rows() -> None:
    x = [72, 200, 300, 420]
    pages = []
    n = 0
    for _ in range(3):
        rows = [
            ["Shard", "Rows", "Bytes"],
            *[[str(n + i), str(10 * (n + i)), str(99 + i)] for i in range(38)],
        ]
        n += 38
        pages.append(_grid(x, 785, rows))  # from the page's top tenth to its bottom tenth
    a = extract(laid_out_pdf(pages), "t.pdf")
    (t,) = a.tables
    assert len(t.rows) == 114 and t.rows[0] == (0, 0, 99) and t.rows[-1] == (113, 1130, 136)
    assert t.locator.to_dict()["page_end"] == 3
    assert "furniture" not in a.attrs


def test_text_drawn_turned_without_rotate_reads_in_its_direction() -> None:
    page = b"q 0 1 -1 0 612 0 cm\n" + text_at(72, 400, "Turned heading", font=2, size=16)
    page += _grid([72, 200, 300, 420], 360, TABLE) + b"Q\n"
    a = extract(laid_out_pdf([page]), "t.pdf")
    assert ("heading", 1, "Turned heading") in _blocks(a)
    assert a.tables[0].rows[0] == ("events", 30, "platform")


def test_an_outlines_headings_sit_under_a_larger_title_on_every_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    title = text_at(72, 740, "Storage design", font=2, size=22)
    first = (
        title
        + text_at(72, 700, "Scope", font=2, size=14)
        + text_at(72, 680, "What the design covers.")
    )
    second = text_at(72, 740, "Limits", font=2, size=14) + text_at(
        72, 720, "What the design bounds."
    )
    data = laid_out_pdf([first, second])
    # the same pages with an outline naming the sections, not the title
    from pypdf import PdfWriter

    w = PdfWriter()
    for p in PdfReader(BytesIO(data)).pages:
        w.add_page(p)
    w.add_outline_item("Scope", 0)
    w.add_outline_item("Limits", 1)
    out = BytesIO()
    w.write(out)
    real = pdffile._lay_out_page

    def second_page_fails(r, *args):  # page 2 is read as its text layer
        if r.number == 2:
            raise ValueError("unreadable")
        return real(r, *args)

    monkeypatch.setattr(pdffile, "_lay_out_page", second_page_fails)
    a = extract(out.getvalue(), "o.pdf")
    heads = [(b.depth, b.text, b.section_path) for b in a.blocks if b.type == "heading"]
    assert heads == [
        (1, "Storage design", ("Storage design",)),
        (2, "Scope", ("Storage design", "Scope")),
        (2, "Limits", ("Storage design", "Limits")),
    ]


def test_a_very_wide_page_and_a_long_outline_are_read_in_bounded_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pypdf import PdfWriter

    lines = b"".join(
        text_at(72, 700 - 12 * i, f"Line {i} of a page wider than any screen") for i in range(40)
    )
    a = extract(laid_out_pdf([lines], width=1e9), "w.pdf")  # a gutter search of a billion points
    assert "Line 39" in "\n".join(b.text for b in a.blocks)
    w = PdfWriter()
    w.add_page(PdfReader(BytesIO(laid_out_pdf([lines]))).pages[0])
    for i in range(3_000):
        w.add_outline_item(f"Entry {i}", 0)
    out = BytesIO()
    w.write(out)
    keys = [0]
    real = pdffile._key

    def counted(text: str) -> str:
        keys[0] += 1
        return real(text)

    monkeypatch.setattr(pdffile, "_key", counted)
    a = extract(out.getvalue(), "o.pdf")
    assert sum(1 for b in a.blocks if b.type == "heading") == 3_000
    # one key per entry and per slot (a line, a paragraph whole): linear, not entries x lines
    assert keys[0] <= 2 * (3_000 + 2 * 40)


def test_a_cell_is_a_number_only_when_it_reads_back_as_the_page_shows_it() -> None:
    rows = [["Package", "Version", "Size"], ["core", "1.10", "1.50"], ["cli", "2", "3"]]
    a = extract(laid_out_pdf([_grid([72, 200, 300, 420], 700, rows)]), "v.pdf")
    (t,) = a.tables
    assert t.rows == (("core", "1.10", "1.50"), ("cli", 2, 3))
    assert "Version: 1.10; Size: 1.50" in next(b.text for b in a.blocks if b.attrs.get("table_id"))


def test_a_page_past_the_line_bound_is_read_as_its_text_layer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(layout, "MAX_LINES", 2)
    page = b"".join(text_at(72, 700 - 14 * i, f"Line {i}") for i in range(3))
    a = extract(laid_out_pdf([page]), "n.pdf")
    assert [b.text for b in a.blocks] == ["Line 0\nLine 1\nLine 2"]
    assert a.warnings == ("page 1: read as its text layer; it holds more than 2 lines",)


def test_a_run_drawn_twice_in_place_is_read_once_and_keeps_its_page_laid_out() -> None:
    page = text_at(72, 720, "Bold title", font=2, size=16) * 2 + text_at(
        72, 690, "The body under it."
    )
    a = extract(laid_out_pdf([page]), "d.pdf")
    assert _blocks(a) == [("heading", 1, "Bold title"), ("paragraph", 0, "The body under it.")]
    assert a.warnings == ()


def test_past_its_bound_the_furniture_list_is_cut_and_the_warning_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pdffile, "_MAX_FURNITURE", 1)
    pages = [
        text_at(72, 770, "Running head", size=8)
        + text_at(500, 30, "Confidential", size=8)
        + text_at(72, 700, f"Body {n} of the report.")
        for n in range(3)
    ]
    a = extract(laid_out_pdf(pages), "k.pdf")
    assert a.attrs["furniture"] == [{"text": "Running head", "pages": 3}]
    assert a.warnings == (
        "page furniture left out of the text: 6 header, footer or page-number lines"
        " (attrs.furniture; the first 1 of 2 kinds named)",
    )


def test_an_outline_entry_after_a_title_paragraph_takes_its_own_line() -> None:
    from pypdf import PdfWriter

    page = (
        text_at(72, 740, "Introduction and", font=2, size=14)
        + text_at(72, 724, "Scope", font=2, size=14)
        + text_at(72, 700, "Why the design exists.")
        + text_at(72, 670, "Scope", font=2, size=14)
        + text_at(72, 650, "What it covers.")
    )
    w = PdfWriter()
    w.add_page(PdfReader(BytesIO(laid_out_pdf([page]))).pages[0])
    parent = w.add_outline_item("Introduction and Scope", 0)
    w.add_outline_item("Scope", 0, parent=parent)
    out = BytesIO()
    w.write(out)
    a = extract(out.getvalue(), "s.pdf")
    heads = [(b.depth, b.text) for b in a.blocks if b.type == "heading"]
    assert heads == [(1, "Introduction and Scope"), (2, "Scope")]
    assert a.blocks[-1].section_path == ("Introduction and Scope", "Scope")


def test_a_borderless_table_over_pages_keeps_its_rows_at_the_margins_and_drops_a_running_header() -> (
    None
):
    pages = []
    n = 0
    for _ in range(3):
        page = text_at(72, 778, "Annual report", size=8) + text_at(400, 778, "Section 3", size=8)
        y = 740  # the table's header, in the page's top tenth
        page += text_at(72, y, "Node") + text_at(220, y, "State") + text_at(360, y, "Uptime")
        while y > 40:  # down into the bottom tenth
            y -= 14
            page += (
                text_at(72, y, f"node-{n}")
                + text_at(220, y, "running")
                + text_at(360, y, f"{n % 9}d")
            )
            n += 1
        pages.append(page)
    a = extract(laid_out_pdf(pages), "n.pdf")
    (t,) = a.tables
    assert [c.name for c in t.columns] == ["Node", "State", "Uptime"]
    assert len(t.rows) == n and t.rows[-1][0] == f"node-{n - 1}"
    assert a.attrs["furniture"] == [{"text": "Annual report Section 3", "pages": 3}]


def test_the_same_text_again_in_another_font_is_text_not_a_copy() -> None:
    page = b"BT /F1 11 Tf 72 700 Td (ha) Tj /F2 11 Tf (ha) Tj ET\n"
    a = extract(laid_out_pdf([page]), "h.pdf")
    assert [b.text for b in a.blocks] == ["haha"]


def test_a_borderless_run_past_its_line_bound_is_read_whole_in_two_tables() -> None:
    page = b"".join(
        text_at(72, 780 - 2.5 * i, f"key{i}", size=2)
        + text_at(200, 780 - 2.5 * i, f"value{i}", size=2)
        for i in range(300)
    )
    a = extract(laid_out_pdf([page]), "r.pdf")
    cells = {
        v for t in a.tables for v in [c.name for c in t.columns] + [x for r in t.rows for x in r]
    }
    assert {f"key{i}" for i in range(300)} <= cells and len(a.tables) == 2


def test_furniture_close_to_a_borderless_table_stays_out_of_its_rows_and_its_header_in() -> None:
    pages = []
    n = 0
    for p in range(3):
        # a running header 22 points over the table's header, which sits 20 points
        # over rows 12 apart (half again their spacing would be 18), and a page
        # number 18 points under the last row, under the last column
        page = text_at(72, 762, "Annual report", size=8) + text_at(400, 762, "Section 3", size=8)
        y = 740
        page += (
            text_at(72, y, "Node", size=10)
            + text_at(220, y, "State", size=10)
            + text_at(360, y, "Uptime", size=10)
        )
        y -= 20
        while y > 60:
            page += text_at(72, y, f"node-{n}", size=10) + text_at(220, y, "running", size=10)
            page += text_at(360, y, f"{n % 9}d", size=10)
            n += 1
            y -= 12
        page += text_at(362, y + 12 - 18, f"{p + 1}", size=10)
        pages.append(page)
    a = extract(laid_out_pdf(pages), "f.pdf")
    (t,) = a.tables
    assert [c.name for c in t.columns] == ["Node", "State", "Uptime"]
    assert len(t.rows) == n and all(r[2] == f"{k % 9}d" for k, r in enumerate(t.rows))
    assert {f["text"] for f in a.attrs["furniture"]} == {"Annual report Section 3", "1"}


def _node_table_pages(last: Callable[[int, float], bytes]) -> tuple[bytes, int]:
    """Three pages of a borderless table, its header repeated at the top of each
    and its rows 12 points apart down to y=90, with `last(page, y of the last
    row)` under them; and the number of rows."""
    pages = []
    n = 0
    for p in range(3):
        page = (
            text_at(72, 752, "Node", size=10)
            + text_at(220, 752, "State", size=10)
            + text_at(360, 752, "Uptime", size=10)
        )
        y = 738.0
        while y > 86:
            page += text_at(72, y, f"node-{n}", size=10) + text_at(220, y, "running", size=10)
            page += text_at(360, y, f"{n % 9}d", size=10)
            n += 1
            y -= 12
        pages.append(page + last(p, y + 12))
    return laid_out_pdf(pages), n


@pytest.mark.parametrize(
    ("cells", "furniture"),
    [
        ([(72, "{p}")], "1"),  # under the first column
        ([(72, "Page"), (220, "{p}"), (360, "of 3")], "Page 1 of 3"),  # across every column
    ],
)
def test_a_page_number_under_a_borderless_table_is_no_row(
    cells: list[tuple[float, str]], furniture: str
) -> None:
    # 18 points under the last row: within half again the row spacing
    data, n = _node_table_pages(
        lambda p, y: b"".join(text_at(x, y - 18, t.format(p=p + 1), size=10) for x, t in cells)
    )
    a = extract(data, "p.pdf")
    (t,) = a.tables
    assert len(t.rows) == n and t.rows[-1][0] == f"node-{n - 1}"
    assert {f["text"] for f in a.attrs["furniture"]} == {furniture}


@pytest.mark.parametrize(("gap", "kept"), [(12, True), (20, False)])
def test_a_recurring_line_under_a_borderless_table_is_its_row_only_within_its_row_spacing(
    gap: float, kept: bool
) -> None:
    # a subtotal with an empty last cell, in the bottom tenth of each page
    data, n = _node_table_pages(
        lambda p, y: (
            text_at(72, y - gap, "Subtotal", size=10) + text_at(220, y - gap, "30", size=10)
        )
    )
    a = extract(data, "s.pdf")
    (t,) = a.tables
    subtotals = [r for r in t.rows if r[0] == "Subtotal"]
    assert len(subtotals) == (3 if kept else 0) and len(t.rows) == n + len(subtotals)
    assert ("furniture" not in a.attrs) is kept


@pytest.mark.parametrize(("gap", "kept"), [(14, True), (22, False)])
def test_a_repeated_header_over_a_column_of_row_names_is_the_tables_header_at_its_row_spacing(
    gap: float, kept: bool
) -> None:
    pages = []
    n = 0
    for _ in range(3):
        # the header's first cell is empty, in the page's top tenth, over rows 14 apart
        page = text_at(150, 760, "Q1") + text_at(250, 760, "Q2") + text_at(350, 760, "Q3")
        y = 760 - gap + 14
        while y > 100:
            y -= 14
            page += text_at(72, y, f"Region {n}") + text_at(150, y, f"{n}")
            page += text_at(250, y, f"{2 * n}") + text_at(350, y, f"{3 * n}")
            n += 1
        pages.append(page)
    a = extract(laid_out_pdf(pages), "q.pdf")
    (t,) = a.tables
    assert ([c.name for c in t.columns][1:] == ["Q1", "Q2", "Q3"]) is kept
    if kept:
        assert len(t.rows) == n and t.rows[0][0] == "Region 0"
        assert "furniture" not in a.attrs
    else:
        # the first row stands for the header, and the pages still join into one table
        assert t.columns[0].name == "Region 0" and len(t.rows) == n - 1
        assert t.rows[-1][0] == f"Region {n - 1}"
        assert [f["text"] for f in a.attrs["furniture"]] == ["Q1 Q2 Q3"]
