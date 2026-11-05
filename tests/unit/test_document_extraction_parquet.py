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

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from loom_extractor import parquetfile, tabular
from loom_extractor.source import ExtractionError, extract


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


def test_values_json_cannot_hold_as_numbers_keep_their_column_a_number() -> None:
    t = pa.table(
        {
            "u": pa.array([2**64 - 1, 1, 2], pa.uint64()),  # past signed 64 bits: its digits
            "f": pa.array([float("nan"), float("-inf"), 1.5], pa.float64()),
            "f16": pa.array([float("inf"), float("nan"), 0.5], pa.float16()),
        }
    )
    a, tab = _one(_file(t))
    # A NaN is no value, as a null; an infinity is left out and counted.
    assert tab.rows == (("18446744073709551615", None, None), (1, None, None), (2, 1.5, 0.5))
    assert [c.type for c in tab.columns] == ["number", "number", "number"]
    assert [(f.unit, f.reason) for f in a.coverage.failed] == [
        ("f.parquet values", "2 values infinite")
    ]


def test_a_row_of_nans_or_blank_text_is_no_row_but_one_left_out_is() -> None:
    nan, inf = float("nan"), float("inf")
    t = pa.table(
        {
            "f": pa.array([1.5, nan, nan, inf], pa.float64()),
            "s": pa.array(["a", None, "  ", None]),
        }
    )
    a, tab = _one(_file(t))
    # Rows 2 and 3 hold nothing; row 4's infinity was left out, so it is a row.
    assert tab.rows == ((1.5, "a"), (None, None))
    assert (tab.locator.row_start, tab.locator.row_end) == (1, 4)  # rows keep their numbers
    assert (a.coverage.to_dict()["total"], a.coverage.to_dict()["extracted"]) == (2, 2)


def test_float32_bounds_and_small_or_scaled_decimals_read_as_plain_numbers() -> None:
    big = 3.4028234663852886e38  # float32's maximum, a common "no value" mark
    t = pa.table(
        {
            "f32": pa.array([big, -big, 2.5], pa.float32()),
            "dec": pa.array([Decimal("0.0000001"), Decimal("1"), None], pa.decimal128(10, 7)),
        }
    )
    a, tab = _one(_file(t))
    assert tab.rows[0] == (3.4028235e38, "0.0000001")
    assert tab.rows[1][:2] == (-3.4028235e38, "1.0000000") and tab.rows[2][0] == 2.5
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

    def counting(column: Any, target: Any, kind: str, unheld: list[int], j: int) -> list[Any]:
        decoded.append(len(column))
        return real(column, target, kind, unheld, j)

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
            "large": pa.array([[ns[0]], [ns[1]]], pa.large_list(pa.timestamp("ns"))),
            "tod": pa.array([1_000_000_000, 3_600_000_123_456], pa.time64("ns")),
        }
    )
    a, tab = _one(_file(t))
    assert [c.type for c in tab.columns] == ["datetime", "duration", "string", "string", "time"]
    whole, sub = '["2023-11-14T22:13:20"]', '["2023-11-14T22:13:20.123456"]'
    assert tab.rows == (
        ("2023-11-14T22:13:20", "0:00:00.000001", whole, whole, "00:00:01"),
        ("2023-11-14T22:13:20.123456", "24:00:00.000123", sub, sub, "01:00:00.000123"),
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

    def recording(column: Any, target: Any, kind: str, unheld: list[int], j: int) -> list[Any]:
        sizes.append(len(column))
        return real(column, target, kind, unheld, j)

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
    # Names that read as numbers are the schema's, no first row of data: the
    # server asks no header question of them.
    years = pa.table({"2023": [1], "2024": [2]})
    served = parse_artifact(
        extract(_file(years), "y.parquet").to_json(), fmt="parquet", max_bytes=1 << 24
    )
    assert served.tables[0].header_rows == 0 and ds.infer(served.tables[0]).clarifications == ()


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


def test_every_cause_of_a_value_left_out_is_named_in_full(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(parquetfile, "MAX_VALUE_CHARS", 3)
    monkeypatch.setattr(parquetfile, "_MAX_UNHELD", 1)
    n = 1_000_000  # the decode bound's order: counts of seven digits must fit too
    t = pa.table(
        {
            "bin": [b"\x00", None, None],
            "f": [float("inf"), None, None],
            "s": ["long text", None, None],
            "d": pa.array([3_000_000, 3_000_000, None], pa.date32()),
            "k": ["a", "b", "c"],
        }
    )
    a, _ = _one(_file(t))
    reason = (
        "1 value of bytes, no text; 1 value infinite; 1 value over 3 characters; "
        "1 value past what Python holds; 1 value unread past 1 in a column"
    )
    assert [f.reason for f in a.coverage.failed] == [reason]
    monkeypatch.undo()  # the real bounds' phrases
    worst = "; ".join(f"{n} values {why}" for why in parquetfile._causes().values())
    assert len(worst) <= 200  # what a failed unit's reason holds


def test_values_left_out_are_counted_for_the_rows_read(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tabular, "MAX_ROWS", 2)
    t = pa.table({"k": list("abcde"), "d": pa.array([3_000_000] * 5, pa.date32())})
    a, _ = _one(_file(t))
    units = {f.unit: f.reason for f in a.coverage.failed}
    # The two held and the one the budget cut; the two after it are not read.
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


def test_one_value_python_cannot_hold_is_left_out_of_its_batch_alone() -> None:
    t = pa.table({"d": pa.array([0, 3_000_000, 1], pa.date32())})  # one batch
    a, tab = _one(_file(t))
    assert tab.rows == (("1970-01-01",), (None,), ("1970-01-02",))
    assert [f.reason for f in a.coverage.failed] == ["1 value past what Python holds"]


def test_a_sub_microsecond_value_in_a_list_view_is_left_out_and_counted() -> None:
    ns = pa.array([1_700_000_000_123_456_789], pa.timestamp("ns"))
    lv = pa.ListViewArray.from_arrays(pa.array([0], pa.int32()), pa.array([1], pa.int32()), ns)
    a, tab = _one(_file(pa.table({"lv": lv})))
    assert tab.rows == ((None,),)  # still a row
    assert [f.reason for f in a.coverage.failed] == ["1 value past what Python holds"]


def test_a_list_of_float16_is_the_double_each_widens_to() -> None:
    t = pa.table({"l": pa.array([[0.5, 0.1]], pa.list_(pa.float16()))})
    _, tab = _one(_file(t))
    assert tab.rows == (("[0.5, 0.0999755859375]",),)


def test_an_arrow_memory_error_in_a_conversion_is_raised_not_left_out() -> None:
    class Column:
        type = pa.int64()
        null_count = 0

        def __len__(self) -> int:
            return 1

        def to_pylist(self) -> list[Any]:
            raise pa.ArrowMemoryError("out of memory")  # a MemoryError and an ArrowException

    with pytest.raises(MemoryError):  # the loop fails the rows from there by name
        parquetfile._split(Column(), None, [0], 0)


def test_a_file_of_only_blank_rows_is_its_header_block_at_row_0() -> None:
    from server.knowledge.domain.extract.validate import parse_artifact

    t = pa.table({"v": pa.array([None] * 3, pa.int64()), "s": pa.array([None] * 3, pa.string())})
    a = extract(_file(t), "f.parquet")
    (tab,) = a.tables
    assert (tab.rows, tab.header_rows, tab.locator.row_start, tab.locator.row_end) == ((), 0, 0, 0)
    assert [(b.text, b.locator.row_start, b.locator.row_end) for b in a.blocks] == [("v; s", 0, 0)]
    served = parse_artifact(a.to_json(), fmt="parquet", max_bytes=1 << 24)
    assert [b.text for b in served.blocks] == ["v; s"] and served.coverage.complete


def test_a_file_whose_values_were_all_left_out_is_still_its_header_to_find() -> None:
    from server.knowledge.domain.extract.structural import chunk_artifact
    from server.knowledge.domain.extract.validate import parse_artifact

    a = extract(_file(pa.table({"bin": [b"\x00", b"\x01"]})), "b.parquet")
    (tab,) = a.tables
    assert (tab.rows, tab.locator.row_start, tab.locator.row_end) == (((None,), (None,)), 1, 2)
    # No row has text, so the names are its block, as for a table with no rows.
    assert [(b.text, b.locator.row_start) for b in a.blocks] == [("bin", 0)]
    served = parse_artifact(a.to_json(), fmt="parquet", max_bytes=1 << 24)
    assert [c.text for c in chunk_artifact(served, size=760, stride=600)] == ["bin"]
    # A block of such rows is no block; one with text after it is the table's text.
    rows = tabular.ROWS_PER_BLOCK
    mixed = pa.table({"bin": [b"\x00"] * rows + [None], "s": [None] * rows + ["x"]})
    a = extract(_file(mixed), "m.parquet")
    assert [(b.text, b.locator.row_start) for b in a.blocks] == [("s: x", rows + 1)]


def test_a_block_partly_of_rows_left_out_holds_and_cites_only_rows_with_text() -> None:
    t = pa.table({"bin": [b"\x00", None, b"\x01", None], "s": [None, "a", None, "b"]})
    a = extract(_file(t), "p.parquet")
    assert [(b.text, b.locator.row_start, b.locator.row_end) for b in a.blocks] == [
        ("s: a\ns: b", 2, 4)
    ]
    assert len(a.tables[0].rows) == 4  # every row is still the table's


def test_a_file_of_blank_rows_is_read_only_to_the_decode_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(parquetfile, "_MAX_DECODED", 10)
    monkeypatch.setattr(parquetfile, "_BATCH_ROWS", 4)
    t = pa.table({"v": pa.array([None] * 30, pa.int64())})
    a, tab = _one(_file(t))
    assert tab.rows == ()
    (unit,) = a.coverage.failed
    # Two batches decoded (8 values), the third past the bound: counted, not read.
    assert (unit.unit, unit.reason) == ("f.parquet rows 9-", "22 rows past what one file decodes")
    # In groups of 10: the first read whole (10 values), the second's first batch
    # past the bound, so its 10 rows and the third's are counted, not read.
    a, tab = _one(_file(t, row_group_size=10))
    assert tab.rows == ()
    assert [(f.unit, f.reason) for f in a.coverage.failed] == [
        ("f.parquet rows 11-", "20 rows past what one file decodes")
    ]


def test_values_python_cannot_hold_are_searched_for_by_halves_to_a_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []
    real = parquetfile._split

    def counting(column: Any, target: Any, unheld: list[int], j: int) -> list[Any]:
        calls.append(len(column))
        return real(column, target, unheld, j)

    monkeypatch.setattr(parquetfile, "_split", counting)
    days = [0] * 1023 + [3_000_000]  # one bad value at the end of a batch of 1,024
    _, tab = _one(_file(pa.table({"d": pa.array(days, pa.date32())})))
    assert tab.rows[-1] == (None,) and tab.rows[0] == ("1970-01-01",)
    assert len(calls) <= 2 * 11  # halves to the one value: about two calls a level
    monkeypatch.setattr(parquetfile, "_MAX_UNHELD", 2)
    days = pa.array([3_000_000, 3_000_000, 3_000_000, 1], pa.date32())
    a, tab = _one(_file(pa.table({"d": days})))
    assert [f.reason for f in a.coverage.failed] == [
        "2 values past what Python holds; 2 values unread past 2 in a column"
    ]


def test_values_too_long_to_hold_are_read_only_to_the_decode_byte_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(parquetfile, "MAX_VALUE_CHARS", 10)
    monkeypatch.setattr(parquetfile, "_MAX_DECODED_BYTES", 4_000)
    monkeypatch.setattr(parquetfile, "_BATCH_ROWS", 4)
    t = pa.table({"s": ["x" * 1_000] * 40})  # each too long to hold, each a row held empty
    a, tab = _one(_file(t))
    units = {f.unit: f.reason for f in a.coverage.failed}
    assert len(tab.rows) < 8  # cut at the byte bound, not at MAX_ROWS
    assert units[f"f.parquet rows {len(tab.rows) + 1}-"].endswith("rows past what one file decodes")


def test_a_dictionary_entry_is_charged_for_every_row_that_names_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(parquetfile, "_MAX_DECODED_BYTES", 10_000)
    monkeypatch.setattr(parquetfile, "_BATCH_ROWS", 4)
    # One entry of 1,000 characters: a few KB stored, 1,000 bytes a row read into Python.
    col = pa.DictionaryArray.from_arrays(pa.array([0] * 40, pa.int32()), pa.array(["x" * 1_000]))
    a, tab = _one(_file(pa.table({"s": col})))
    assert len(tab.rows) < 12  # past 10,000 bytes of copies, not past the 40 rows
    assert any(f.reason.endswith("rows past what one file decodes") for f in a.coverage.failed)


def test_a_lists_elements_count_against_the_decode_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(parquetfile, "_MAX_DECODED", 1_000)
    monkeypatch.setattr(parquetfile, "_BATCH_ROWS", 2)
    # Each row a list of 300 nulls: no buffer behind its elements, 300 values to read.
    t = pa.table({"l": pa.array([[None] * 300] * 20, pa.list_(pa.null()))})
    a, tab = _one(_file(t))
    assert len(tab.rows) == 2  # one batch of two (602 values); the next would pass 1,000
    assert [(f.unit, f.reason) for f in a.coverage.failed] == [
        ("f.parquet rows 3-", "18 rows past what one file decodes")
    ]


def test_a_dictionary_inside_a_list_is_charged_for_every_element_that_names_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entries = pa.DictionaryArray.from_arrays(pa.array([0] * 4, pa.int32()), pa.array(["x" * 1_000]))
    value = pa.ListArray.from_arrays(pa.array([0, 4], pa.int32()), entries)
    assert parquetfile._cost(value) == (1 + 4, 4_000)


def test_a_schema_of_no_columns_is_counted_not_iterated(monkeypatch: pytest.MonkeyPatch) -> None:
    class Meta:
        num_row_groups = 1
        num_columns = 0
        num_rows = 10**12

        def row_group(self, g: int) -> Any:
            return type("Group", (), {"num_rows": 10**12})()

    class File:  # as a hand-written footer may declare: no column, a trillion rows
        metadata = Meta()
        schema_arrow: list[Any] = []

        def iter_batches(self, **_: Any) -> Any:
            raise AssertionError("no batch is read for a schema of no columns")

    monkeypatch.setattr(parquetfile, "_open", lambda data: File())
    a = parquetfile.extract_parquet(b"", "z.parquet")
    assert [(t.columns, t.rows, t.header_rows) for t in a.tables] == [((), (), 0)]
    assert a.blocks == ()  # no name to find: the server answers no_text
    assert (a.coverage.to_dict()["total"], a.coverage.to_dict()["extracted"]) == (0, 0)


def test_a_row_count_a_footer_declares_below_zero_is_no_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Meta:
        num_row_groups = 2
        num_columns = 1
        num_rows = -5

        def row_group(self, g: int) -> Any:
            return type("Group", (), {"num_rows": -5})()

    class File:
        metadata = Meta()
        schema_arrow = [pa.field("v", pa.int64())]

        def iter_batches(self, **_: Any) -> Any:
            raise AssertionError("a group of no rows is not read")

    monkeypatch.setattr(parquetfile, "_open", lambda data: File())
    a = parquetfile.extract_parquet(b"", "z.parquet")
    assert (a.coverage.to_dict()["total"], a.coverage.failed) == (0, ())


def test_a_footer_declaring_rows_past_signed_64_bits_is_corrupt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Meta:
        num_row_groups = 2
        num_columns = 1

        def row_group(self, g: int) -> Any:
            return type("Group", (), {"num_rows": 1 << 62})()

    class File:
        metadata = Meta()
        schema_arrow = [pa.field("v", pa.int64())]

    monkeypatch.setattr(parquetfile, "_open", lambda data: File())
    with pytest.raises(ExtractionError) as e:
        extract(b"PAR1 footer PAR1", "z.parquet")
    assert e.value.code == "corrupt"


@pytest.mark.parametrize(
    ("declared", "kept"),
    [(3, 2), (4, 4), (30, 10)],  # 4: the footer's count on a batch boundary, past it more
)
def test_a_row_group_whose_pages_disagree_with_its_footer_fails_from_there(
    monkeypatch: pytest.MonkeyPatch, declared: int, kept: int
) -> None:
    monkeypatch.setattr(parquetfile, "_BATCH_ROWS", 2)
    real = parquetfile._open(_file(pa.table({"v": list(range(10))})))

    class Meta:
        num_row_groups = 1
        num_columns = 1

        def row_group(self, g: int) -> Any:
            return type("Group", (), {"num_rows": declared})()

    class File:  # the real pages, under a footer that says another count
        metadata = Meta()
        schema_arrow = real.schema_arrow
        iter_batches = real.iter_batches

    monkeypatch.setattr(parquetfile, "_open", lambda data: File())
    a = parquetfile.extract_parquet(b"", "f.parquet")
    (tab,) = a.tables
    c = a.coverage.to_dict()
    # Rows are counted by the footer, so the artifact never holds more than it declares.
    assert (c["total"], c["extracted"], len(tab.rows)) == (declared, kept, kept)
    assert [(f.unit, f.reason) for f in a.coverage.failed] == [
        (
            f"f.parquet rows {kept + 1}-",
            "not decoded: the row group's pages disagree with its footer",
        )
    ]


def test_a_batch_of_rows_with_no_values_still_costs_its_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(parquetfile, "_MAX_DECODED", 6)
    monkeypatch.setattr(parquetfile, "_BATCH_ROWS", 4)
    # As a column with nothing in it.
    monkeypatch.setattr(parquetfile, "_cost", lambda array: (0, 0))
    a, _ = _one(_file(pa.table({"v": pa.array([None] * 20, pa.int64())})))
    assert [(f.unit, f.reason) for f in a.coverage.failed] == [
        ("f.parquet rows 5-", "16 rows past what one file decodes")
    ]


def test_views_sharing_one_buffer_are_charged_for_every_value() -> None:
    long = pa.array(["x" * 1_000] * 4, pa.string_view())
    assert parquetfile._cost(long) == (4, 0)  # each value its own data: nbytes counts it
    # Four views of one 1,000-byte value: one buffer, four copies read into Python.
    one = pa.array(["x" * 1_000], pa.string_view())
    buffers = one.buffers()
    view = bytes(buffers[1])
    shared = pa.Array.from_buffers(
        pa.string_view(), 4, [None, pa.py_buffer(view * 4), *buffers[2:]]
    )
    assert shared.to_pylist() == ["x" * 1_000] * 4
    assert parquetfile._cost(shared) == (4, 3_000)


def test_row_groups_past_the_bound_are_counted_not_read(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(parquetfile, "_MAX_GROUPS", 3)
    t = pa.table({"v": pa.array([None] * 10, pa.int64())})  # blank rows: no other bound moves
    a, _ = _one(_file(t, row_group_size=1))
    assert [(f.unit, f.reason) for f in a.coverage.failed] == [
        ("f.parquet rows 4-", "7 rows past what one file decodes")
    ]


def test_a_column_past_its_unheld_bound_is_not_searched_in_later_batches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(parquetfile, "_MAX_UNHELD", 2)
    monkeypatch.setattr(parquetfile, "_BATCH_ROWS", 2)
    searched: list[int] = []
    real = parquetfile._split

    def counting(column: Any, target: Any, unheld: list[int], j: int) -> list[Any]:
        searched.append(len(column))
        return real(column, target, unheld, j)

    monkeypatch.setattr(parquetfile, "_split", counting)
    a, _ = _one(_file(pa.table({"d": pa.array([3_000_000] * 10, pa.date32())})))
    # The first batch finds the two; each later batch is one call, nothing converted.
    assert searched == [2, 1, 1, 2, 2, 2, 2]
    assert a.coverage.failed[-1].reason == (
        "2 values past what Python holds; 8 values unread past 2 in a column"
    )


def test_the_unheld_bound_is_counted_for_each_column_apart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(parquetfile, "_MAX_UNHELD", 2)
    bad = 3_000_000  # a date past year 9999
    t = pa.table(
        {
            "a": pa.array([bad, bad, bad, 1], pa.date32()),
            "b": pa.array([bad, 1, 2, 3], pa.date32()),
        }
    )
    a, tab = _one(_file(t))
    # Column a's bound does not stop b: b's one bad value is left out, its others read.
    assert [r[1] for r in tab.rows] == [None, "1970-01-02", "1970-01-03", "1970-01-04"]
    assert [r[0] for r in tab.rows] == [None, None, None, None]
    assert [f.reason for f in a.coverage.failed] == [
        "3 values past what Python holds; 2 values unread past 2 in a column"
    ]


def test_the_extractor_imports_without_pyarrow() -> None:
    import subprocess
    import sys
    from pathlib import Path

    extractor = Path(__file__).resolve().parents[2] / "extractor"
    code = (
        "import sys; sys.modules['pyarrow'] = None\n"
        "from loom_extractor import source\n"
        "print('parquet' in source.PARSED_FORMATS)\n"
    )
    run = subprocess.run(
        [sys.executable, "-c", code], cwd=extractor, capture_output=True, text=True, timeout=60
    )
    assert (run.returncode, run.stdout.strip()) == (0, "False"), run.stderr


def test_a_bytes_column_is_charged_no_copies_since_none_is_made(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(parquetfile, "_MAX_DECODED_BYTES", 150_000)
    monkeypatch.setattr(parquetfile, "_BATCH_ROWS", 4)
    # One entry of 10,000 bytes named by every row: each batch decodes it once
    # (about 100,000 bytes in all), and 400,000 bytes were it copied.
    blob = pa.DictionaryArray.from_arrays(pa.array([0] * 40, pa.int32()), pa.array([b"x" * 10_000]))
    a, tab = _one(_file(pa.table({"k": [str(i) for i in range(40)], "b": blob})))
    assert len(tab.rows) == 40
    assert [f.unit for f in a.coverage.failed] == ["f.parquet values"]  # the bytes, left out


def test_a_column_past_the_column_bound_is_charged_no_copies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(parquetfile, "MAX_COLUMNS", 1)
    monkeypatch.setattr(parquetfile, "_MAX_DECODED_BYTES", 150_000)
    monkeypatch.setattr(parquetfile, "_BATCH_ROWS", 4)
    # Its one 10,000-character entry would be 400,000 bytes copied, were it read.
    text = pa.DictionaryArray.from_arrays(pa.array([0] * 40, pa.int32()), pa.array(["x" * 10_000]))
    a, tab = _one(_file(pa.table({"k": [str(i) for i in range(40)], "t": text})))
    assert len(tab.rows) == 40
    assert [f.unit for f in a.coverage.failed] == ["f.parquet columns"]


def test_a_dictionary_of_views_is_charged_by_its_views_lengths() -> None:
    # Arrow has no length kernel for views; the lengths come from the views, sliced or not.
    big = "x" * 100
    entries = pa.array(["q", "ab", big, None], pa.string_view()).slice(1)
    d = pa.DictionaryArray.from_arrays(pa.array([0, 1, 1, None, 2], pa.int32()), entries)
    assert parquetfile._cost(d) == (5, 2 + 100 + 100)
    none = pa.DictionaryArray.from_arrays(pa.array([], pa.int32()), pa.array([], pa.binary_view()))
    assert parquetfile._cost(none) == (0, 0)


def test_a_view_array_of_no_values_costs_nothing() -> None:
    empty = pa.array([], pa.string_view())
    assert parquetfile._cost(empty) == (0, 0)
    lists = pa.array([[], []], pa.list_(pa.string_view()))
    assert parquetfile._cost(lists) == (2, 0)
