"""Qualification gates: the reference extractor, scored per format and slice against
the floors the Qualification table of docs/architecture/document-extraction.md
declares. PDF, DOCX and PPTX are scored on the parsing bench's corpus with its
scorer; the other formats on the labelled fixtures below. A format the reference
extractor declares with no row in the table fails, so it advertises only what
passed."""

from __future__ import annotations

import datetime as dt
import functools
import io
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest

from bench.docparse.normalize import normalize
from bench.docparse.score import heading_scores, score_file, text_scores
from loom_extractor import Options, ocr, service
from loom_extractor import extract as _extract
from tests.fixtures.floor_tables import read_floors
from tests.fixtures.xlsx_files import cell, sheet, workbook

ROOT = Path(__file__).resolve().parents[2]
DOC = ROOT / "docs" / "architecture" / "document-extraction.md"
CORPUS = ROOT / "bench" / "docparse" / "corpus"


@functools.cache
def _floors() -> dict[tuple[str, ...], float]:
    return read_floors(DOC.read_text(), "## Qualification", 4)


def extract(data: bytes, name: str, options: Options | None = None) -> Any:
    """The reference extractor with OCR off: the floors are measured without it,
    so the gates read the same wherever the OCR engine is installed or not."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(ocr, "engine", lambda: None)
        return _extract(data, name, options)


def _parse(artifact: dict[str, Any]) -> dict[str, Any]:
    """The artifact as the bench reads it (`bench.docparse.normalize`), with its
    failed units, OCR-read units and units holding unread images named as
    `page 3` / `slide 2`."""
    parse = normalize(artifact)
    unit = "slide" if artifact["format"] == "pptx" else "page"
    parse["failed_units"] = [f["unit"] for f in parse["failed"]]
    parse["unread"] = [f"{unit} {n}" for n in parse["images_unread"]]
    parse["ocr"] = [f"{unit} {n}" for n in parse["ocr_pages"]]
    return parse


# The bench's categories as the table's slices.
def _slice(category: str) -> tuple[str, str]:
    if category == "pdf_scanned":
        return "pdf", "scanned"
    if category.startswith("pdf_"):
        return "pdf", "text layer, every slice"
    return category, {"docx": "documents", "pptx": "decks"}[category]


_BENCH_METRICS = {
    "text recall": "text_recall",
    "reading order": "order_acc",
    "heading F1": "heading_f1",
    "heading level": "heading_level_acc",
    "table cell F1": "table_cell_f1",
    "locator": "locator_acc",
    "notes": "notes_acc",
}


@functools.cache
def _corpus() -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Each corpus file's gold and its parse by the reference extractor."""
    out = []
    for line in (CORPUS / "gold.jsonl").read_text().splitlines():
        gold = json.loads(line)
        artifact = extract((CORPUS / gold["file"]).read_bytes(), gold["file"]).to_dict()
        out.append((gold, _parse(artifact)))
    return out


def _names_unit(parse: dict[str, Any], unit: str) -> bool:
    """Whether the parse reads `unit` (`page 3`, `slide 2`) with OCR or says it was
    not read: a failed unit, or one `attrs.images_unread` lists."""
    return unit in parse["failed_units"] or unit in parse["unread"] or unit in parse["ocr"]


def _announced(gold: dict[str, Any], parse: dict[str, Any]) -> tuple[int, int]:
    """Of the gold's text drawn inside images, the blocks whose page or slide the
    parse names as not read, and their number."""
    unit = "slide" if gold["format"] == "pptx" else "page"
    lost = [b for b in gold["blocks"] if b["kind"] == "image_text"]
    if gold["category"] == "pdf_scanned":
        lost = [b for b in gold["blocks"] if b.get("page")]
    return sum(_names_unit(parse, f"{unit} {b['page']}") for b in lost), len(lost)


def _bench_measures() -> dict[tuple[str, str, str], list[tuple[str, float]]]:
    """Each (format, slice, metric) the corpus measures: its value per bench category,
    so a slice of every category is checked category by category."""
    per: dict[tuple[str, str, str], dict[str, list[float]]] = {}
    announced: dict[tuple[str, str, str], list[int]] = {}  # (format, slice, category): hit, n
    for gold, parse in _corpus():
        fmt, slice_ = _slice(gold["category"])
        scores = score_file(gold, parse)
        for metric, name in _BENCH_METRICS.items():
            if scores.get(name) is not None:
                per.setdefault((fmt, slice_, metric), {}).setdefault(gold["category"], []).append(
                    scores[name]
                )
        if slice_ == "scanned":
            pages = range(1, gold["pages"] + 1)
            # read with OCR or failed, never complete and empty
            read_or_failed = set(parse["failed_units"]) | set(parse["ocr"])
            failed = sum(f"page {n}" in read_or_failed for n in pages) / len(pages)
            per.setdefault((fmt, slice_, "failed units"), {}).setdefault(
                gold["category"], []
            ).append(failed)
        hit, n = _announced(gold, parse)
        loss_slice = "every slice" if fmt == "pdf" else slice_
        tally = announced.setdefault((fmt, loss_slice, gold["category"]), [0, 0])
        tally[0] += hit
        tally[1] += n
    out = {
        key: [(cat, sum(v) / len(v)) for cat, v in sorted(cats.items())]
        for key, cats in per.items()
    }
    for (fmt, slice_, category), (hit, n) in sorted(announced.items()):
        if n:  # a category with text in images, each on its own
            out.setdefault((fmt, slice_, "announced loss"), []).append((category, hit / n))
    return out


# ---------------------------------------------------------------------------
# The text formats: (file name, source, gold blocks as (kind, text, first line,
# last line, heading level)).

_TEXT_CASES: dict[str, list[tuple[str, str, list[tuple[str, str, int, int, int]]]]] = {
    "markdown": [
        (
            "design.md",
            "Storage design\n"
            "==============\n"
            "\n"
            "The store keeps one row per key.\n"
            "\n"
            "## Limits\n"
            "\n"
            "- At most 64 KiB a value.\n"
            "- At most 1,000 keys a batch.\n"
            "\n"
            "```toml\n"
            "[limits]\n"
            "max = 10\n"
            "```\n"
            "\n"
            "### Merges\n"
            "\n"
            "Merges run hourly,\n"
            "and never during a backup.\n",
            [
                ("heading", "Storage design", 1, 2, 1),
                ("paragraph", "The store keeps one row per key.", 4, 4, 0),
                ("heading", "Limits", 6, 6, 2),
                ("list", "- At most 64 KiB a value.\n- At most 1,000 keys a batch.", 8, 9, 0),
                ("code", "[limits]\nmax = 10", 12, 13, 0),
                ("heading", "Merges", 16, 16, 3),
                ("paragraph", "Merges run hourly,\nand never during a backup.", 18, 19, 0),
            ],
        ),
        (
            "runbook.md",
            "# Runbook: E4031\n"
            "\n"
            "1. Drain the replica.\n"
            "2. Restart it.\n"
            "\n"
            "> The replica rejoins on its own.\n"
            "\n"
            "## Escalation\n"
            "\n"
            "Page the on-call owner.\n",
            [
                ("heading", "Runbook: E4031", 1, 1, 1),
                ("list", "1. Drain the replica.\n2. Restart it.", 3, 4, 0),
                ("paragraph", "The replica rejoins on its own.", 6, 6, 0),
                ("heading", "Escalation", 8, 8, 2),
                ("paragraph", "Page the on-call owner.", 10, 10, 0),
            ],
        ),
    ],
    "html": [
        (
            "page.html",
            "<!doctype html>\n"
            "<html><head><title>Limits</title><style>p{color:red}</style></head>\n"
            "<body>\n"
            "<h1>Limits</h1>\n"
            "<p>Each value holds at most 64 KiB.</p>\n"
            "<h2>Batches</h2>\n"
            "<ul><li>1,000 keys</li><li>16 MiB</li></ul>\n"
            "<script>var hidden = 1;</script>\n"
            "<pre>SELECT count() FROM keys;</pre>\n"
            "</body></html>\n",
            [
                ("heading", "Limits", 4, 4, 1),
                ("paragraph", "Each value holds at most 64 KiB.", 5, 5, 0),
                ("heading", "Batches", 6, 6, 2),
                ("list", "1,000 keys", 7, 7, 0),
                ("list", "16 MiB", 7, 7, 0),
                ("code", "SELECT count() FROM keys;", 9, 9, 0),
            ],
        ),
    ],
    "text": [
        (
            "notes.txt",
            "Rollout notes\n"
            "\n"
            "The first region switched at noon.\n"
            "The second follows tomorrow.\n"
            "\n"
            "\n"
            "Rollback: revert the flag.\n",
            [
                ("paragraph", "Rollout notes", 1, 1, 0),
                (
                    "paragraph",
                    "The first region switched at noon.\nThe second follows tomorrow.",
                    3,
                    4,
                    0,
                ),
                ("paragraph", "Rollback: revert the flag.", 7, 7, 0),
            ],
        ),
    ],
}

# An HTML table's rows, each its cells in order.
_HTML_TABLE = (
    "table.html",
    "<html><body>\n"
    "<h1>Regions</h1>\n"
    "<table>\n"
    "<tr><th>Region</th><th>Owner</th><th>Limit</th></tr>\n"
    "<tr><td>EMEA</td><td>Ops</td><td>64</td></tr>\n"
    "<tr><td>APAC</td><td>Data</td><td>128</td></tr>\n"
    "</table>\n"
    "</body></html>\n",
    [["Region", "Owner", "Limit"], ["EMEA", "Ops", "64"], ["APAC", "Data", "128"]],
)


def _fold(text: str) -> str:
    return " ".join(text.split())


def _text_gold(blocks: list[tuple[str, str, int, int, int]]) -> dict[str, Any]:
    return {
        "blocks": [{"kind": k, "text": t, "level": lv} for k, t, _, _, lv in blocks],
        "tables": [],
    }


def _text_measures() -> dict[tuple[str, str, str], list[tuple[str, float]]]:
    out: dict[tuple[str, str, str], list[tuple[str, float]]] = {}
    for fmt, cases in _TEXT_CASES.items():
        recall, precision, headings, levels, located, n = [], [], [], [], 0, 0
        for name, source, gold_blocks in cases:
            parse = _parse(extract(source.encode(), name).to_dict())
            gold = _text_gold(gold_blocks)
            words = text_scores(gold, parse)
            recall.append(words["text_recall"])
            precision.append(words["text_precision"])
            if any(k == "heading" for k, *_ in gold_blocks):
                found = heading_scores(gold, parse)
                headings.append(found["heading_f1"])
                levels.append(found["heading_level_acc"] or 0.0)
            for _, text, first, last, _lv in gold_blocks:
                n += 1
                # Cited right: a block holding the gold's text whose lines are the gold's.
                located += any(
                    _fold(text) in _fold(b["text"])
                    and (b["line_start"], b["line_end"]) == (first, last)
                    for b in parse["blocks"]
                )
        out[(fmt, "fixtures", "text recall")] = [("fixtures", sum(recall) / len(recall))]
        out[(fmt, "fixtures", "text precision")] = [("fixtures", sum(precision) / len(precision))]
        if headings:
            out[(fmt, "fixtures", "heading F1")] = [("fixtures", sum(headings) / len(headings))]
            out[(fmt, "fixtures", "heading level")] = [("fixtures", sum(levels) / len(levels))]
        out[(fmt, "fixtures", "locator")] = [("fixtures", located / n)]
    name, source, rows = _HTML_TABLE
    parse = _parse(extract(source.encode(), name).to_dict())
    held = [[_fold(c) for c in r] for t in parse["tables"] for r in t["rows"]]
    out[("html", "fixtures", "table rows")] = [
        ("fixtures", sum([_fold(c) for c in r] in held for r in rows) / len(rows))
    ]
    return out


# ---------------------------------------------------------------------------
# SQL: (file name, source, dialect, gold statements as (text, first line, last
# line, the objects it defines or reads)).

_SQL_CASES = [
    (
        "schema.sql",
        "-- Events, one row per write.\n"
        "CREATE TABLE events\n"
        "(\n"
        "    ts DateTime64(3),\n"
        "    tenant LowCardinality(String)\n"
        ")\n"
        "ENGINE = MergeTree\n"
        "ORDER BY (tenant, ts);\n"
        "\n"
        "CREATE MATERIALIZED VIEW events_daily ENGINE = SummingMergeTree ORDER BY day\n"
        "AS SELECT toDate(ts) AS day, count() AS n FROM events GROUP BY day;\n"
        "\n"
        "INSERT INTO events VALUES (now64(3), 'acme');\n",
        "clickhouse",
        [
            (
                "CREATE TABLE events ( ts DateTime64(3), tenant LowCardinality(String) ) "
                "ENGINE = MergeTree ORDER BY (tenant, ts);",
                1,
                8,
                {"events"},
            ),
            (
                "CREATE MATERIALIZED VIEW events_daily ENGINE = SummingMergeTree ORDER BY day "
                "AS SELECT toDate(ts) AS day, count() AS n FROM events GROUP BY day;",
                10,
                11,
                {"events_daily", "events"},
            ),
            ("INSERT INTO events VALUES (now64(3), 'acme');", 13, 13, {"events"}),
        ],
    ),
    (
        "report.sql",
        "SELECT o.id, c.name\n"
        "FROM orders AS o\n"
        "JOIN customers AS c ON c.id = o.customer_id\n"
        "WHERE o.total > 100;\n"
        "UPDATE orders SET total = 0 WHERE id = 7;\n",
        "postgres",
        [
            (
                "SELECT o.id, c.name FROM orders AS o JOIN customers AS c ON c.id = o.customer_id "
                "WHERE o.total > 100;",
                1,
                4,
                {"orders", "customers"},
            ),
            ("UPDATE orders SET total = 0 WHERE id = 7;", 5, 5, {"orders"}),
        ],
    ),
]


def _sql_measures() -> dict[tuple[str, str, str], list[tuple[str, float]]]:
    found = located = names = n_names = n = 0
    for name, source, dialect, statements in _SQL_CASES:
        parse = _parse(extract(source.encode(), name, Options(sql_dialect=dialect)).to_dict())
        held = [b for b in parse["blocks"] if b["type"] == "sql_statement"]
        for text, first, last, objects in statements:
            n += 1
            n_names += len(objects)
            hit = next((b for b in held if _fold(text) in _fold(b["text"])), None)
            if hit is None:
                continue
            found += 1
            located += (hit["line_start"], hit["line_end"]) == (first, last)
            names += len(objects & set(hit["keys"]))
    return {
        ("sql", "fixtures", "statements"): [("fixtures", found / n)],
        ("sql", "fixtures", "locator"): [("fixtures", located / n)],
        ("sql", "fixtures", "names"): [("fixtures", names / n_names)],
    }


# TOML: (file name, source, gold keys as (key path, line)).
_TOML_CASES = [
    (
        "settings.toml",
        'title = "loom"\n'
        "\n"
        "[server]\n"
        "port = 7777\n"
        'hosts = ["a", "b"]\n'
        "\n"
        "[server.limits]\n"
        "max_rows = 10_000\n"
        "ratio = 0.5\n"
        "\n"
        "[[workers]]\n"
        'name = "embed"\n'
        "\n"
        "[[workers]]\n"
        'name = "gc"\n'
        "\n"
        "[owner]\n"
        "since = 2026-01-02\n"
        'contact = { team = "ops", page = true }\n',
        [
            ("title", 1),
            ("server.port", 4),
            ("server.hosts", 5),
            ("server.limits.max_rows", 8),
            ("server.limits.ratio", 9),
            ("workers[0].name", 12),
            ("workers[1].name", 15),
            ("owner.since", 18),
            ("owner.contact", 19),
        ],
    ),
]


def _toml_measures() -> dict[tuple[str, str, str], list[tuple[str, float]]]:
    found = located = n = 0
    for name, source, keys in _TOML_CASES:
        parse = _parse(extract(source.encode(), name).to_dict())
        for path, line in keys:
            n += 1
            hits = [b for b in parse["blocks"] if path in b["keys"]]
            found += bool(hits)
            located += any(b["line_start"] <= line <= b["line_end"] for b in hits)
    return {
        ("toml", "fixtures", "key paths"): [("fixtures", found / n)],
        ("toml", "fixtures", "locator"): [("fixtures", located / n)],
    }


# ---------------------------------------------------------------------------
# Tables: (file name, bytes, options, gold tables as (columns as (name, type),
# rows)). A number is compared by its decimal value.


def _parquet() -> bytes:
    import pyarrow as pa
    import pyarrow.parquet as pq

    utc = dt.UTC
    table = pa.table(
        {
            "id": pa.array([1, 2], pa.int64()),
            "amount": pa.array([Decimal("12.345"), Decimal("-1.5")], pa.decimal128(18, 3)),
            "at": pa.array(
                [dt.datetime(2026, 1, 2, 3, 4, 5, tzinfo=utc), None], pa.timestamp("us", tz="UTC")
            ),
            "day": pa.array([dt.date(2026, 1, 2), dt.date(2025, 12, 31)]),
            "opens": pa.array([dt.time(9, 30), dt.time(17, 0)], pa.time64("us")),
            "tags": pa.array([["a", "b"], []], pa.list_(pa.string())),
            "owner": pa.array(
                [{"team": "ops", "size": 3}, None],
                pa.struct([("team", pa.string()), ("size", pa.int64())]),
            ),
            "active": pa.array([True, False]),
        }
    )
    buf = io.BytesIO()
    pq.write_table(table, buf)
    return buf.getvalue()


def _xlsx_sales() -> bytes:
    # A formula reads its saved value; a date its number format makes one.
    rows = {
        1: [
            cell("A1", "Region", t="inlineStr"),
            cell("B1", "Q1", t="inlineStr"),
            cell("C1", "Total", t="inlineStr"),
        ],
        2: [cell("A2", "EMEA", t="inlineStr"), cell("B2", 10), cell("C2", 20, f="B2*2")],
        3: [cell("A3", "APAC", t="inlineStr"), cell("B3", 5.5), cell("C3", 11, f="B3*2")],
    }
    dates = {
        1: [cell("A1", "Day", t="inlineStr"), cell("B1", "Closed", t="inlineStr")],
        2: [cell("A2", 46023, s=1), cell("B2", 1, t="b")],
        3: [cell("A3", 46024, s=1), cell("B3", 0, t="b")],
    }
    return workbook([("Sales", sheet(rows)), ("Days", sheet(dates))])


def _xlsx_merged_header() -> bytes:
    # A header over two rows: "2025" spans its two halves; Region and Note span both rows.
    rows = {
        1: [
            cell("A1", "Region", t="inlineStr"),
            cell("B1", "2025", t="inlineStr"),
            cell("D1", "Note", t="inlineStr"),
        ],
        2: [cell("B2", "H1", t="inlineStr"), cell("C2", "H2", t="inlineStr")],
        3: [
            cell("A3", "EMEA", t="inlineStr"),
            cell("B3", 1),
            cell("C3", 2),
            cell("D3", "ok", t="inlineStr"),
        ],
        4: [
            cell("A4", "APAC", t="inlineStr"),
            cell("B4", 3),
            cell("C4", 4),
            cell("D4", "late", t="inlineStr"),
        ],
    }
    return workbook([("Plan", sheet(rows, merges=("A1:A2", "B1:C1", "D1:D2")))])


_TABLE_CASES: dict[
    str, list[tuple[str, bytes, Options, list[tuple[list[tuple[str, str]], list[list[Any]]]]]]
] = {
    "csv": [
        (
            "quoted.csv",
            b'id,name,note\n1,"Smith, J","said ""hi"""\n2,Lee,"two\nlines"\n',
            Options(),
            [
                (
                    [("id", "number"), ("name", "string"), ("note", "string")],
                    [[1, "Smith, J", 'said "hi"'], [2, "Lee", "two\nlines"]],
                )
            ],
        ),
        (
            "latin.csv",
            "id;city;amount\n1;Zürich;10,5\n2;Malmö;7\n".encode("latin-1"),
            Options(encoding="latin-1"),
            # "10,5" may be a decimal comma or text: the column is mixed, not guessed.
            [
                (
                    [("id", "number"), ("city", "string"), ("amount", "mixed")],
                    [[1, "Zürich", "10,5"], [2, "Malmö", 7]],
                )
            ],
        ),
        (
            "wide.csv",
            "id,city\n1,Zürich\n".encode("utf-16"),
            Options(),
            [([("id", "number"), ("city", "string")], [[1, "Zürich"]])],
        ),
    ],
    "xlsx": [
        (
            "sales.xlsx",
            _xlsx_sales(),
            Options(),
            [
                (
                    [("Region", "string"), ("Q1", "number"), ("Total", "number")],
                    [["EMEA", 10, 20], ["APAC", 5.5, 11]],
                ),
                (
                    [("Day", "date"), ("Closed", "bool")],
                    [["2026-01-01", True], ["2026-01-02", False]],
                ),
            ],
        ),
        (
            "plan.xlsx",
            _xlsx_merged_header(),
            Options(),
            [
                (
                    [
                        ("Region", "string"),
                        ("2025 / H1", "number"),
                        ("2025 / H2", "number"),
                        ("Note", "string"),
                    ],
                    [["EMEA", 1, 2, "ok"], ["APAC", 3, 4, "late"]],
                )
            ],
        ),
    ],
    "parquet": [
        (
            "orders.parquet",
            _parquet(),
            Options(),
            [
                (
                    [
                        ("id", "number"),
                        ("amount", "number"),
                        ("at", "datetime"),
                        ("day", "date"),
                        ("opens", "time"),
                        ("tags", "string"),
                        ("owner", "string"),
                        ("active", "bool"),
                    ],
                    [
                        [
                            1,
                            "12.345",
                            "2026-01-02T03:04:05+00:00",
                            "2026-01-02",
                            "09:30:00",
                            '["a", "b"]',
                            '{"team": "ops", "size": 3}',
                            True,
                        ],
                        [2, "-1.5", None, "2025-12-31", "17:00:00", "[]", None, False],
                    ],
                )
            ],
        )
    ],
}


def _same(got: Any, want: Any, kind: str) -> bool:
    if kind == "number" and want is not None and got is not None and not isinstance(got, bool):
        try:
            return Decimal(str(got)) == Decimal(str(want))
        except ArithmeticError:
            return False
    return bool(got == want and type(got) is type(want))


def _table_measures() -> dict[tuple[str, str, str], list[tuple[str, float]]]:
    """Cells read with their value and type, and columns with their name, over
    the gold's or the extraction's count, whichever is larger: an extra row,
    column or table counts as a miss."""
    out: dict[tuple[str, str, str], list[tuple[str, float]]] = {}
    for fmt, cases in _TABLE_CASES.items():
        cells = right = names = n_names = 0
        for name, data, options, tables in cases:
            got = extract(data, name, options).to_dict().get("tables", [])
            for extra in got[len(tables) :]:
                n_names += len(extra["columns"])
                cells += len(extra["rows"]) * len(extra["columns"])
            for i, (columns, rows) in enumerate(tables):
                g_rows = got[i]["rows"] if i < len(got) else []
                held = [(c["name"], c["type"]) for c in got[i]["columns"]] if i < len(got) else []
                n_names += max(len(columns), len(held))
                cells += max(len(rows), len(g_rows)) * max(len(columns), len(held))
                names += sum(a == b for (a, _), (b, _) in zip(columns, held, strict=False))
                for r, row in enumerate(rows):
                    for c, want in enumerate(row):
                        if r < len(g_rows) and c < len(held) and held[c] == columns[c]:
                            right += _same(g_rows[r][c], want, columns[c][1])
        out[(fmt, "fixtures", "typed cells")] = [("fixtures", right / cells)]
        out[(fmt, "fixtures", "headers")] = [("fixtures", names / n_names)]
    return out


@functools.cache
def _measures() -> dict[tuple[str, str, str], list[tuple[str, float]]]:
    return {
        **_bench_measures(),
        **_text_measures(),
        **_sql_measures(),
        **_toml_measures(),
        **_table_measures(),
    }


def test_every_slice_meets_its_floor() -> None:
    """One test for every row, so the corpus is extracted once per run rather than
    once per worker; the message names each row under its floor."""
    measures = _measures()
    missing = [" / ".join(g) for g in _floors() if not measures.get(g)]
    under = [
        f"{' / '.join(g)} under {floor}: {[(label, round(v, 3)) for label, v in measures[g] if v < floor]}"
        for g, floor in _floors().items()
        if measures.get(g) and any(v < floor for _, v in measures[g])
    ]
    assert not missing, f"rows with no measurement: {missing}"
    assert not under, "\n".join(under)


async def test_the_reference_extractor_declares_only_formats_with_floors() -> None:
    app = service.create_app(service.Config(token="t" * 24))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://x") as c:
        served = (
            await c.get("/v1/capabilities", headers={"Authorization": "Bearer " + "t" * 24})
        ).json()
    # Formats and their options are all it claims: no figure, chart or OCR capability.
    assert set(served) == {"contract_version", "extractor", "max_bytes", "formats"}
    assert all(set(f) == {"format", "options"} for f in served["formats"])
    declared = {f["format"] for f in served["formats"]}
    qualified = {fmt for fmt, _, _ in _floors()}
    assert declared <= qualified, f"declared with no floors: {sorted(declared - qualified)}"
    # Every format the table qualifies is one the reference extractor reads.
    assert qualified <= declared, (
        f"floors for a format it does not declare: {sorted(qualified - declared)}"
    )
