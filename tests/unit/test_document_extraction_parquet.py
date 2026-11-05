"""The reference extractor's Parquet reader: each column type as a table value,
the rows as passages cited by row, the bounds on what is read, and the
artifact the server reads back and types as a dataset. Fixtures are written
with pyarrow here, so each test says what the file holds."""

from __future__ import annotations

import datetime as dt
import io
import json
from decimal import Decimal
from typing import Any

import pytest

pa = pytest.importorskip("pyarrow")
pq = pytest.importorskip("pyarrow.parquet")

from loom_extractor import parquetfile, tabular  # noqa: E402
from loom_extractor.source import ExtractionError, extract  # noqa: E402


def _file(table: Any, **kw: Any) -> bytes:
    buf = io.BytesIO()
    pq.write_table(table, buf, **kw)
    return buf.getvalue()


def _one(data: bytes, name: str = "f.parquet") -> Any:
    a = extract(data, name)
    (t,) = a.tables
    return a, t


def test_each_column_type_reads_as_its_table_value() -> None:
    utc = dt.UTC
    t = pa.table(
        {
            "s": ["a", "b"],
            "i": pa.array([1, 2**62], pa.int64()),
            "f": pa.array([1.25, -0.5], pa.float64()),
            "f32": pa.array([0.1, 1.5], pa.float32()),
            "dec": pa.array([Decimal("12.345000000"), Decimal("-1")], pa.decimal128(38, 9)),
            "ts": pa.array([dt.datetime(2026, 1, 2, 3, 4, 5, tzinfo=utc), None], pa.timestamp("us", tz="UTC")),
            "naive": pa.array([dt.datetime(2026, 1, 2, 3, 4, 5), None], pa.timestamp("ms")),
            "d": pa.array([dt.date(2026, 1, 2), None], pa.date32()),
            "tm": pa.array([dt.time(13, 30), None], pa.time64("us")),
            "du": pa.array([dt.timedelta(hours=36, seconds=1.5), dt.timedelta(minutes=-1)], pa.duration("us")),
            "b": [True, False],
            "cat": pa.array(["p", "q"]).dictionary_encode(),
        }
    )  # fmt: skip
    a, tab = _one(_file(t))
    assert (a.format, a.parser, a.coverage.complete) == ("parquet", "loom-parquet", True)
    assert a.parser_version == f"1+pyarrow{pa.__version__}"  # pyarrow decodes the pages
    assert [(c.name, c.type) for c in tab.columns] == [
        ("s", "string"),
        ("i", "number"),
        ("f", "number"),
        ("f32", "number"),
        ("dec", "number"),
        ("ts", "datetime"),
        ("naive", "datetime"),
        ("d", "date"),
        ("tm", "time"),
        ("du", "duration"),
        ("b", "bool"),
        ("cat", "string"),
    ]
    assert tab.rows[0] == (
        "a",
        1,
        1.25,
        0.1,  # its shortest float32 text, not 0.10000000149011612
        "12.345000000",  # a decimal's exact text
        "2026-01-02T03:04:05+00:00",
        "2026-01-02T03:04:05",
        "2026-01-02",
        "13:30:00",
        "36:00:01.5",
        True,
        "p",
    )
    assert tab.rows[1][1] == 2**62 and tab.rows[1][9] == "-0:01:00"
    assert tab.header_rows == 0  # the names are the schema's, no row of the file
    assert (tab.locator.row_start, tab.locator.row_end) == (1, 2)


def test_values_json_cannot_hold_as_numbers_are_text() -> None:
    t = pa.table(
        {
            "u": pa.array([2**64 - 1, 1], pa.uint64()),  # past signed 64 bits: its digits
            "f": pa.array([float("nan"), float("inf")], pa.float64()),
        }
    )
    _, tab = _one(_file(t))
    assert tab.rows == (("18446744073709551615", "nan"), (1, "inf"))
    assert [c.type for c in tab.columns] == ["mixed", "string"]


def test_nested_values_are_their_json_and_bytes_are_left_out_and_counted() -> None:
    t = pa.table(
        {
            "k": ["a", "b"],
            "l": [[1, 2], []],
            "st": [{"x": 1, "y": "z"}, None],
            "m": pa.array([[("a", 1)], None], pa.map_(pa.string(), pa.int64())),
            "bin": [b"\x00\x01", None],
        }
    )
    a, tab = _one(_file(t))
    assert tab.rows[0][:4] == ("a", "[1, 2]", '{"x": 1, "y": "z"}', '[["a", 1]]')
    assert tab.rows[0][4] is None and tab.rows[1] == ("b", "[]", None, None, None)
    assert [(f.unit, f.reason) for f in a.coverage.failed] == [
        ("f.parquet values", "1 value not text or over 1048576 characters")
    ]
    assert not a.coverage.complete


def test_rows_are_blocks_cited_by_row_and_a_row_of_nulls_is_no_row() -> None:
    t = pa.table({"k": ["a", None, "c"], "v": [1, None, 3]})
    a, tab = _one(_file(t, row_group_size=2))
    assert tab.rows == (("a", 1), ("c", 3))
    assert a.coverage.to_dict()["total"] == 2 and a.coverage.complete
    (block,) = a.blocks
    assert block.text == "k: a; v: 1\nk: c; v: 3"
    assert (block.locator.row_start, block.locator.row_end) == (1, 3)  # rows 1 and 3
    assert block.attrs == {"table_id": "t0"}


def test_rows_past_the_budget_are_counted_from_the_metadata_not_decoded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tabular, "MAX_ROWS", 3)
    decoded: list[int] = []
    real = parquetfile._values

    def counting(column: Any, kinds: list[str], j: int) -> list[Any]:
        decoded.append(len(column))
        return real(column, kinds, j)

    monkeypatch.setattr(parquetfile, "_values", counting)
    monkeypatch.setattr(parquetfile, "_BATCH_ROWS", 2)
    t = pa.table({"v": list(range(100))})
    a, tab = _one(_file(t, row_group_size=10))
    assert [r[0] for r in tab.rows] == [0, 1, 2]
    assert [(f.unit, f.reason) for f in a.coverage.failed] == [
        ("f.parquet rows 4-", "97 rows past what one artifact holds")
    ]
    assert a.coverage.to_dict()["total"] == 100
    assert sum(decoded) == 4  # two batches of two, then the rest from the metadata


def test_columns_past_the_bound_are_not_read_and_are_counted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(parquetfile, "MAX_COLUMNS", 2)
    a, tab = _one(_file(pa.table({"a": [1], "b": [2], "c": [3], "d": [4]})))
    assert [c.name for c in tab.columns] == ["a", "b"] and tab.rows == ((1, 2),)
    assert [f.unit for f in a.coverage.failed] == ["f.parquet columns"]
    assert a.coverage.failed[0].reason == "2 columns past column 2"


def test_a_value_python_cannot_hold_reads_as_arrows_text() -> None:
    # A nanosecond timestamp: Python's datetime holds microseconds.
    t = pa.table({"ts": pa.array([1_700_000_000_123_456_789], pa.timestamp("ns"))})
    _, tab = _one(_file(t))
    assert tab.rows == (("2023-11-14 22:13:20.123456789",),)
    assert tab.columns[0].type == "string"


def test_a_file_pyarrow_cannot_read_is_corrupt_and_one_that_breaks_off_fails_its_rows() -> None:
    with pytest.raises(ExtractionError) as e:
        extract(b"PAR1 not read PAR1", "t.parquet")
    assert e.value.code == "corrupt" and "not a readable parquet file" in str(e.value)
    # A good file whose second row group's pages are damaged.
    data = bytearray(_file(pa.table({"v": list(range(20))}), row_group_size=10, compression="none"))
    meta = pq.ParquetFile(io.BytesIO(bytes(data))).metadata.row_group(1).column(0)
    start = meta.data_page_offset
    data[start + 8 : start + 40] = b"\xff" * 32
    a, tab = _one(bytes(data), "t.parquet")
    assert [r[0] for r in tab.rows] == list(range(10))
    assert [f.unit for f in a.coverage.failed] == ["t.parquet rows 11-"]
    assert a.coverage.failed[0].reason.startswith("not decoded:")


def test_the_artifact_reads_back_and_types_its_decimals_exactly() -> None:
    from server.knowledge.domain import datasets as ds
    from server.knowledge.domain.extract.validate import parse_artifact

    amounts = [Decimal("99999999999999999999.123456789"), Decimal("0.100000000")]
    t = pa.table({"k": ["a", "b"], "amount": pa.array(amounts, pa.decimal128(38, 9))})
    a = parse_artifact(extract(_file(t), "m.parquet").to_json(), fmt="parquet", max_bytes=1 << 24)
    (tab,) = a.tables
    schema = ds.infer(tab)
    assert [(c.type, c.slot) for c in schema.columns] == [("string", "s1"), ("number", "n1")]
    assert [r["n1"] for r in ds.typed_rows(tab, schema.columns, 0, 9).rows] == amounts
    assert json.loads(a.to_json())["tables"][0]["rows"][0][1] == "99999999999999999999.123456789"
