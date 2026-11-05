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
    # Past signed 64 bits, a number's exact digits, as a decimal's: the column stays a number.
    assert [c.type for c in tab.columns] == ["number", "string"]


def test_float32_bounds_and_small_or_scaled_decimals_read_as_plain_numbers() -> None:
    big = 3.4028234663852886e38  # float32's maximum, a common "no value" mark
    t = pa.table(
        {
            "f32": pa.array([big, -big, float("inf")], pa.float32()),
            "dec": pa.array([Decimal("0.0000001"), Decimal("1"), None], pa.decimal128(10, 7)),
        }
    )
    a, tab = _one(_file(t))
    assert tab.rows[0] == (3.4028235e38, "0.0000001")
    assert tab.rows[1][:2] == (-3.4028235e38, "1.0000000") and tab.rows[2][0] == "inf"
    assert a.coverage.complete


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
        ("f.parquet values", "1 value of bytes, no text")
    ]
    assert not a.coverage.complete
    view = pa.table({"k": ["a"], "v": pa.array([b"\x00"], pa.binary_view())})
    va, vtab = _one(_file(view))
    assert vtab.rows == (("a", None),) and [f.unit for f in va.coverage.failed] == [
        "f.parquet values"
    ]


def test_a_row_whose_values_were_all_left_out_is_still_a_row() -> None:
    t = pa.table({"bin": [b"\x00", b"\x01", None]})  # two rows of bytes, one of nulls
    a, tab = _one(_file(t))
    assert tab.rows == ((None,), (None,))
    assert (a.coverage.to_dict()["total"], a.coverage.to_dict()["extracted"]) == (2, 2)
    assert [f.reason for f in a.coverage.failed] == ["2 values of bytes, no text"]


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

    def counting(column: Any, target: Any, kind: str) -> list[Any]:
        decoded.append(len(column))
        return real(column, target, kind)

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


def test_columns_past_the_bound_are_left_out_and_counted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(parquetfile, "MAX_COLUMNS", 2)
    a, tab = _one(_file(pa.table({"a": [1], "b": [2], "c": [3], "d": [4]})))
    assert [c.name for c in tab.columns] == ["a", "b"] and tab.rows == ((1, 2),)
    assert [f.unit for f in a.coverage.failed] == ["f.parquet columns"]
    assert a.coverage.failed[0].reason == "2 columns past column 2"


def test_a_nanosecond_column_is_read_to_the_microsecond_in_every_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(parquetfile, "_BATCH_ROWS", 1)  # each value its own batch
    ns = [1_700_000_000_000_000_000, 1_700_000_000_123_456_789]  # whole, then sub-microsecond
    t = pa.table(
        {
            "ts": pa.array(ns, pa.timestamp("ns")),
            "du": pa.array([1_500, 86_400_000_123_456], pa.duration("ns")),
            "nested": pa.array([[ns[0]], [ns[1]]], pa.list_(pa.timestamp("ns"))),
        }
    )
    a, tab = _one(_file(t))
    assert [c.type for c in tab.columns] == ["datetime", "duration", "string"]
    assert tab.rows == (
        ("2023-11-14T22:13:20", "0:00:00.000001", '["2023-11-14T22:13:20"]'),
        ("2023-11-14T22:13:20.123456", "24:00:00.000123", '["2023-11-14T22:13:20.123456"]'),
    )
    assert a.coverage.complete


def test_a_value_python_cannot_hold_is_left_out_and_counted() -> None:
    # Days since 1970: one past year 9999, a date Python's date cannot hold.
    t = pa.table({"k": ["a", "b"], "d": pa.array([0, 3_000_000], pa.date32())})
    a, tab = _one(_file(t, row_group_size=1))
    assert tab.rows == (("a", "1970-01-01"), ("b", None))
    assert [f.unit for f in a.coverage.failed] == ["f.parquet values"]
    # A row with nothing else in it is still a row: its value existed.
    alone = pa.table({"d": pa.array([3_000_000] * 5, pa.date32())})
    a, tab = _one(_file(alone))
    assert tab.rows == ((None,),) * 5
    assert (a.coverage.to_dict()["total"], a.coverage.to_dict()["extracted"]) == (5, 5)


def test_a_fixed_size_list_of_nanoseconds_is_read_to_the_microsecond() -> None:
    t = pa.table({"l": pa.array([[1_700_000_000_123_456_789]], pa.list_(pa.timestamp("ns"), 1))})
    a, tab = _one(_file(t))
    assert tab.rows == (('["2023-11-14T22:13:20.123456"]',),) and a.coverage.complete


def test_a_decode_the_childs_memory_cannot_hold_fails_its_rows_by_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def no_memory(*a: Any, **k: Any) -> Any:
        raise MemoryError  # as a list value past the child's limit raises it: no message

    monkeypatch.setattr(pq.ParquetFile, "iter_batches", no_memory)
    a = extract(_file(pa.table({"v": [1, 2]})), "f.parquet")
    assert [(f.unit, f.reason) for f in a.coverage.failed] == [
        ("f.parquet rows 1-", "not decoded: MemoryError")
    ]


def test_a_batch_holds_no_more_cells_than_the_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tabular, "MAX_CELLS", 12)
    sizes: list[int] = []
    real = parquetfile._values

    def recording(column: Any, target: Any, kind: str) -> list[Any]:
        sizes.append(len(column))
        return real(column, target, kind)

    monkeypatch.setattr(parquetfile, "_values", recording)
    t = pa.table({f"c{i}": list(range(5)) for i in range(4)})  # 4 wide: 3 rows a batch
    _one(_file(t))
    assert max(sizes) == 3
    sizes.clear()
    # One column, but a struct of six leaves: the batch is sized by its leaves.
    leaves = pa.struct([(f"f{i}", pa.int64()) for i in range(6)])
    nested = pa.table({"s": pa.array([{f"f{i}": r for i in range(6)} for r in range(5)], leaves)})
    _one(_file(nested))
    assert max(sizes) == 2


def test_columns_past_the_bound_are_kept_by_position_even_when_a_name_repeats(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(parquetfile, "MAX_COLUMNS", 2)
    t = pa.Table.from_arrays([pa.array([1]), pa.array([2]), pa.array([3])], names=["a", "a", "b"])
    a, tab = _one(_file(t))
    assert [c.name for c in tab.columns] == ["a", "a (2)"] and tab.rows == ((1, 2),)
    assert [f.unit for f in a.coverage.failed] == ["f.parquet columns"]


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


def test_nanoseconds_inside_a_struct_a_map_or_a_dictionary_and_a_float16_column_read_alike() -> (
    None
):
    ns = 1_700_000_000_123_456_789
    t = pa.table(
        {
            "st": pa.array([{"at": ns}], pa.struct([("at", pa.timestamp("ns"))])),
            "m": pa.array([[("k", ns)]], pa.map_(pa.string(), pa.timestamp("ns"))),
            "dict": pa.array([ns], pa.timestamp("ns")).dictionary_encode(),
            "f16": pa.array([0.1], pa.float16()),  # stored as 0.0999755859375
        }
    )
    a, tab = _one(_file(t))
    assert tab.rows == (
        (
            '{"at": "2023-11-14T22:13:20.123456"}',
            '[["k", "2023-11-14T22:13:20.123456"]]',
            "2023-11-14T22:13:20.123456",
            0.1,
        ),
    )
    assert a.coverage.complete


def test_a_uuid_reads_as_its_text_and_a_named_zone_keeps_its_offset() -> None:
    import uuid

    u = uuid.UUID("12345678-1234-5678-1234-567812345678")
    at = dt.datetime(2026, 1, 2, 12, tzinfo=dt.UTC)
    t = pa.table(
        {
            "u": pa.array([u.bytes], pa.uuid()),
            "ts": pa.array([at], pa.timestamp("us", tz="America/New_York")),
        }
    )
    a, tab = _one(_file(t))
    assert tab.rows == ((str(u), "2026-01-02T07:00:00-05:00"),) and a.coverage.complete


def test_a_build_without_pyarrow_neither_parses_nor_declares_parquet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from loom_extractor import source

    without = source.parsed_formats(has_pyarrow=False)
    assert "parquet" not in without and set(source.KNOWN_FORMATS) - set(without) == {"parquet"}
    assert source.parsed_formats(has_pyarrow=True) == source.KNOWN_FORMATS == source.PARSED_FORMATS
    monkeypatch.setattr(source, "PARSED_FORMATS", without)  # as that build's import sets it
    with pytest.raises(ExtractionError) as e:
        extract(b"PAR1 not read PAR1", "a.parquet")
    assert e.value.code == "format_unavailable"


def test_a_uuid_inside_a_list_or_struct_is_its_text_in_the_json() -> None:
    import uuid

    u = uuid.UUID("12345678-1234-5678-1234-567812345678")
    ids = pa.ExtensionArray.from_storage(pa.uuid(), pa.array([u.bytes], pa.binary(16)))
    t = pa.table(
        {
            "l": pa.ListArray.from_arrays(pa.array([0, 1], pa.int32()), ids),
            "st": pa.StructArray.from_arrays([ids], names=["id"]),
        }
    )
    a, tab = _one(_file(t))
    assert tab.rows == ((f'["{u}"]', f'{{"id": "{u}"}}'),) and a.coverage.complete


def test_an_extension_type_reads_as_what_it_stores() -> None:
    opaque = pa.opaque(pa.binary(), "image", "vendor")
    t = pa.table(
        {
            "k": ["a", "b"],
            "o": pa.ExtensionArray.from_storage(opaque, pa.array([b"\x00\x01", None])),
            "b": pa.ExtensionArray.from_storage(pa.bool8(), pa.array([1, 0], pa.int8())),
        }
    )
    a, tab = _one(_file(t))
    assert [(c.name, c.type) for c in tab.columns] == [
        ("k", "string"),
        ("o", "string"),
        ("b", "bool"),
    ]
    assert tab.rows == (("a", None, True), ("b", None, False))  # the bytes are no text
    assert [(f.unit, f.reason) for f in a.coverage.failed] == [
        ("f.parquet values", "1 value of bytes, no text")
    ]


def test_each_cause_of_a_value_left_out_is_named(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(parquetfile, "MAX_VALUE_CHARS", 3)
    t = pa.table(
        {
            "bin": [b"\x00", None],
            "s": ["long text", "ok"],
            "d": pa.array([3_000_000, None], pa.date32()),
        }
    )
    a, _ = _one(_file(t))
    assert [(f.unit, f.reason) for f in a.coverage.failed] == [
        (
            "f.parquet values",
            "1 value of bytes, no text; 1 value over 3 characters; 1 value past what Python holds",
        )
    ]


def test_values_left_out_are_counted_for_the_rows_decoded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tabular, "MAX_ROWS", 2)
    t = pa.table({"k": list("abcde"), "d": pa.array([3_000_000] * 5, pa.date32())})
    a, _ = _one(_file(t))
    units = {f.unit: f.reason for f in a.coverage.failed}
    # The two held and the one the budget cut; the two after it are never decoded.
    assert units["f.parquet values"] == "3 values past what Python holds"
    assert units["f.parquet rows 3-"] == "3 rows past what one artifact holds"


def test_a_list_of_bool8_reads_as_bools_and_a_list_view_as_it_stores() -> None:
    b8 = pa.ExtensionArray.from_storage(pa.bool8(), pa.array([1, 0], pa.int8()))
    us = pa.array([1_700_000_000_123_456], pa.timestamp("us"))
    t = pa.table(
        {
            "lb": pa.ListArray.from_arrays(pa.array([0, 2], pa.int32()), b8),
            "lv": pa.ListViewArray.from_arrays(
                pa.array([0], pa.int32()), pa.array([1], pa.int32()), us
            ),
        }
    )
    a, tab = _one(_file(t))
    assert tab.rows == (("[true, false]", '["2023-11-14T22:13:20.123456"]'),)
    assert a.coverage.complete
