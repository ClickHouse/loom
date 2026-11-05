"""Typed datasets from an artifact's tables: schema inference and its
clarifications, slot typing and rejected rows, the catalog row builder, and
knowledge.dataset_get / knowledge.dataset_approve over faked stores. The
ingest job's publication is in test_knowledge_ingest.py."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from server.core.errors import PreconditionFailedError, ValidationError
from server.knowledge.api.models_knowledge import (
    KnowledgeDatasetApproveInput,
    KnowledgeDatasetGetInput,
)
from server.knowledge.app import datasets as app
from server.knowledge.domain import datasets as ds
from server.knowledge.domain.extract.artifact import (
    Artifact,
    Block,
    Column,
    Coverage,
    Failure,
    Locator,
    Table,
)
from server.knowledge.store import datasets as repo
from tests.unit.schema_ddl import column_names


def _table(columns: list[tuple[str, str]], rows: list[list[Any]], **kw: Any) -> Table:
    return Table(
        id=kw.pop("id", "t0"),
        name=kw.pop("name", "sales.csv"),
        columns=tuple(Column(n, t) for n, t in columns),  # type: ignore[arg-type]
        rows=tuple(tuple(r) for r in rows),
        **kw,
    )


# ---- the tables' column sets ---------------------------------------------------


def test_the_column_sets_are_the_tables_columns() -> None:
    assert list(repo.DATASET_COLUMNS) == column_names("datasets")
    assert list(repo.ROW_COLUMNS) == column_names("dataset_rows")
    assert set(ds.SLOT_COLUMNS) <= set(column_names("dataset_rows"))


def test_a_catalog_row_keeps_every_column_across_versions() -> None:
    """loom.datasets is a ReplacingMergeTree read with FINAL: every version row
    the builder makes carries every column forward."""
    first = repo.dataset_row(None, **{c: 0 for c in repo.DATASET_COLUMNS if c != "version"})
    later = repo.dataset_row(first, schema_state="approved")
    assert set(later) == set(repo.DATASET_COLUMNS) and later["version"] > first["version"]
    assert later["row_count"] == first["row_count"] and later["schema_state"] == "approved"
    with pytest.raises(ValueError, match="not a column"):
        repo.dataset_row(first, bogus=1)
    with pytest.raises(ValueError, match="missing"):
        repo.dataset_row(None, org="o")


# ---- schema inference ------------------------------------------------------------


def test_each_type_takes_the_next_slot_of_its_kind() -> None:
    t = _table(
        [
            ("a", "string"),
            ("b", "number"),
            ("c", "date"),
            ("d", "datetime"),
            ("e", "bool"),
            ("f", "time"),
            ("g", "duration"),
            ("h", "mixed"),
        ],
        [],
    )
    schema = ds.infer(t)
    assert [(c.type, c.slot) for c in schema.columns] == [
        ("string", "s1"),
        ("number", "n1"),
        ("date", "t1"),
        ("datetime", "t2"),
        ("bool", "b1"),
        ("time", "s2"),
        ("duration", "n2"),
        ("string", "s3"),  # mixed, held as text until approved
    ]
    assert schema.columns[-1].inferred == "mixed"


def test_numbers_past_the_decimal_slots_go_to_the_float_slots_then_past_capacity() -> None:
    t = _table([(f"x{i}", "number") for i in range(34)], [])
    schema = ds.infer(t)
    slots = [c.slot for c in schema.columns]
    assert slots[:24] == [f"n{i}" for i in range(1, 25)]
    assert slots[24:32] == [f"f{i}" for i in range(1, 9)]
    assert slots[32:] == ["", ""]
    (ask,) = schema.clarifications  # one for the type, however many columns
    assert (ask.code, ask.column) == ("past_capacity", "")
    assert ask.detail == "2 number column(s) past the slots, not stored: x32, x33"


def test_a_wide_mixed_table_is_inferred_in_bounded_time_and_size() -> None:
    width = 16_384  # the contract's column bound, every column mixed
    t = _table([(f"c{i}", "mixed") for i in range(width)], [[1] * width, ["x"] * width])
    schema = ds.infer(t)
    codes = [a.code for a in schema.clarifications]
    assert codes.count("mixed_types") == ds.MAX_COLUMN_CLARIFICATIONS
    assert codes.count("past_capacity") == 1  # the text columns past s1..s24, as one
    assert len(json.dumps([a.to_dict() for a in schema.clarifications])) < 20_000


def test_what_needs_an_answer_is_asked() -> None:
    t = _table(
        [("2024", "number"), ("column 2", "string"), ("v", "mixed")],
        [[1, "a", 1], [2, "b", "x"], [3, None, {"error": "#N/A"}]],
    )
    asks = {(a.code, a.column): a.detail for a in ds.infer(t).clarifications}
    assert asks == {
        ("mixed_types", "v"): "holds number, string values",
        ("generated_name", "column 2"): "its header cell is empty",
    }
    numeric = _table([("2024", "number"), ("2025-01-01", "number")], [])
    (ask,) = ds.infer(numeric).clarifications
    assert (ask.code, ask.column) == ("header_like_data", "")
    for word in ("NaN", "Inf", "Infinity", "sNaN"):  # words Decimal reads as numbers
        assert ds.infer(_table([(word, "number")], [])).clarifications == ()


def test_a_repeated_column_name_is_renamed_and_said_and_its_values_kept() -> None:
    t = _table([("amount", "number"), ("amount", "mixed"), ("amount", "number")],
               [[1, "x", 3]])  # fmt: skip
    schema = ds.infer(t)
    assert [c.name for c in schema.columns] == ["amount", "amount (2)", "amount (3)"]
    assert [(a.code, a.column) for a in schema.clarifications] == [
        ("duplicate_name", "amount (2)"),
        ("mixed_types", "amount (2)"),
        ("duplicate_name", "amount (3)"),
    ]
    typed = ds.typed_rows(t, schema.columns, 0, 1)
    assert typed.rows == [{"n1": Decimal("1.000000000"), "s1": "x", "n2": Decimal("3.000000000"),
                           "row_id": 0}]  # fmt: skip
    # An approval names the renamed column, and carries to a revision with the same header.
    approved = ds.infer(t, {"amount (2)": "string"}).columns
    assert ds.carried(t, approved) == {"amount": "number", "amount (2)": "string",
                                       "amount (3)": "number"}  # fmt: skip
    many = _table([("a", "string")] * 5_000, [])
    assert ds.column_names(many)[-1] == "a (5000)"
    long = _table([("n" * 500, "string")] * 2, [])
    renamed = ds.column_names(long)[1]
    assert len(renamed) <= 500 and renamed.endswith(" (2)")


def test_single_column_answers_stop_at_their_bound_whatever_a_column_adds() -> None:
    # One answer, then two a column (mixed, and an empty header): the bound falls
    # inside a column, which a check before each column would pass by one.
    t = _table([("column 1", "string")] + [(f"column {i}", "mixed") for i in range(2, 81)], [])
    codes = [a.code for a in ds.infer(t).clarifications]
    assert (
        codes.count("mixed_types") + codes.count("generated_name") == ds.MAX_COLUMN_CLARIFICATIONS
    )


def test_an_override_types_the_column_and_answers_its_questions() -> None:
    t = _table([("2024", "string"), ("v", "mixed")], [])
    schema = ds.infer(t, {"2024": "string", "v": "number"})
    assert [(c.type, c.inferred, c.slot) for c in schema.columns] == [
        ("string", "string", "s1"),
        ("number", "mixed", "n1"),
    ]
    assert schema.clarifications == ()


def test_a_dataset_id_names_a_sheet_by_its_name_and_a_csv_by_its_table() -> None:
    sheet = _table([], [], id="t3", name="Q1", locator=Locator(sheet="Q1"))
    moved = _table([], [], id="t0", name="Q1", locator=Locator(sheet="Q1"))
    (a,), (b,), (c,) = (
        ds.dataset_ids(u, [t]) for u, t in (("u", sheet), ("u", moved), ("v", sheet))
    )
    assert a == b != c and a.startswith("ds_") and len(a) == 27
    assert ds.dataset_ids("u", [_table([], [])]) != [a]


def test_a_sheet_holding_two_tables_names_each_by_its_table_id() -> None:
    first = _table([], [], id="t0", name="Q1 a", locator=Locator(sheet="Q1"))
    second = _table([], [], id="t1", name="Q1 b", locator=Locator(sheet="Q1"))
    alone = _table([], [], id="t2", name="Q2", locator=Locator(sheet="Q2"))
    ids = ds.dataset_ids("u", [first, second, alone])
    assert len(set(ids)) == 3
    assert ids[2] == ds.dataset_ids("u", [alone])[0]  # a sheet alone keeps its name's id


def test_an_approval_carries_only_to_the_same_columns_in_order() -> None:
    approved = ds.infer(_table([("a", "string"), ("b", "number")], []), {"b": "string"}).columns
    same = _table([("a", "string"), ("b", "number")], [])
    assert ds.carried(same, approved) == {"a": "string", "b": "string"}
    assert ds.carried(_table([("b", "number"), ("a", "string")], []), approved) is None
    assert ds.carried(_table([("a", "string")], []), approved) is None


def test_a_table_is_incomplete_when_the_extraction_lost_any_of_its_values() -> None:
    t = _table([], [], name="Sales")

    def art(*units: str, lost: tuple[str, ...] = ()) -> Artifact:
        failed = tuple(Failure(u, "x") for u in units)
        return Artifact("xlsx", "p", "1", (), Coverage("rows", 1, 1, failed, lost), tables=(t,))

    assert ds.table_complete(art(), t)
    # Another sheet's units, those whose names start with this one's included.
    others = ("Sales 2 rows 5-", "Sales 2 values", "Sales rows", "Sales rows merges", "Other")
    assert ds.table_complete(art(*others, "sheet Macro1"), t)
    for unit in (
        "Sales",
        "Sales header",
        "Sales rows 100001-",
        "Sales merges",
        "Sales values",
        "Sales columns",
        "Sales cells",
        "shared strings",
    ):
        assert not ds.table_complete(art(unit), t), unit
    for lost in ("formula_values", "shared_strings", "number_formats"):
        assert not ds.table_complete(art(lost=(lost,)), t), lost
    # A capability of the row blocks, not of the values.
    assert ds.table_complete(art(lost=("exact_keys", "section_path")), t)


# ---- typing rows ---------------------------------------------------------------


def test_values_are_typed_into_their_slots() -> None:
    t = _table(
        [
            ("s", "string"),
            ("n", "number"),
            ("d", "date"),
            ("dt", "datetime"),
            ("b", "bool"),
            ("tm", "time"),
            ("du", "duration"),
        ],
        [
            ["x", 0.1, "2026-01-02", "2026-01-02T03:04:05.5+02:00", True, "13:30:00", "36:00:01.5"],
            [None, 12345678901234567890, None, "2026-01-02T03:04:05", False, None, "-0:01:00"],
        ],
    )
    typed = ds.typed_rows(t, ds.infer(t).columns, 0, 10)
    assert typed.rejected == 0
    assert typed.rows == [
        {
            "s1": "x",
            "n1": Decimal("0.100000000"),
            "t1": datetime(2026, 1, 2),
            "t2": datetime(2026, 1, 2, 1, 4, 5, 500000),  # in UTC
            "b1": True,
            "s2": "13:30:00",
            "n2": Decimal("129601.500000000"),
            "row_id": 0,
        },
        {
            "s1": None,
            "n1": Decimal("12345678901234567890.000000000"),
            "t1": None,
            "t2": datetime(2026, 1, 2, 3, 4, 5),
            "b1": False,
            "s2": None,
            "n2": Decimal("-60.000000000"),
            "row_id": 1,
        },
    ]


def test_a_row_a_value_does_not_fit_is_rejected_whole_and_sampled() -> None:
    t = _table(
        [("n", "number"), ("s", "string")],
        [[1, "a"], [{"error": "#DIV/0!"}, "b"], [1e30, "c"], [2, "d"]],
    )
    typed = ds.typed_rows(t, ds.infer(t).columns, 0, 10)
    assert [r["row_id"] for r in typed.rows] == [0, 3]
    assert typed.rejected == 2
    assert [r.to_dict() for r in typed.sample] == [
        {"row": 2, "column": "n", "reason": "cell error #DIV/0!"},
        {"row": 3, "column": "n", "reason": "past what a number column holds (10^29)"},
    ]


def test_a_number_that_rounds_up_past_the_decimal_range_is_rejected_not_raised() -> None:
    edge = "99999999999999999999999999999.9999999999"  # under 10^29, rounds to it
    t = _table([("n", "mixed")], [[edge], ["99999999999999999999999999999.999999999"]])
    typed = ds.typed_rows(t, ds.infer(t, {"n": "number"}).columns, 0, 10)
    assert [s.reason for s in typed.sample] == ["past what a number column holds (10^29)"]
    assert typed.rows == [{"n1": Decimal("99999999999999999999999999999.999999999"), "row_id": 1}]


def test_a_float_slot_rejects_what_a_double_cannot_hold() -> None:
    columns = (ds.DatasetColumn("n", "number", "mixed", "f1"),)
    t = _table([("n", "mixed")], [["1e400"], ["-1e400"], ["2.5"]])
    typed = ds.typed_rows(t, columns, 0, 10)
    assert typed.rejected == 2 and typed.rows == [{"f1": 2.5, "row_id": 2}]


def test_an_instant_outside_what_datetime64_holds_is_rejected() -> None:
    t = _table(
        [("d", "date"), ("dt", "datetime")],
        [
            ["9999-12-31", None],
            ["1899-12-30", None],
            [None, "2299-12-31T23:59:59"],
            [None, "1900-01-01T00:30:00+01:00"],  # 1899-12-31 23:30 in UTC
            ["1900-01-01", "2026-01-01T00:00:00"],
        ],
    )
    typed = ds.typed_rows(t, ds.infer(t).columns, 0, 10)
    assert [r["row_id"] for r in typed.rows] == [2, 4]
    assert [s.reason for s in typed.sample] == [
        "'9999-12-31' is outside 1900-2299, what a date column holds",
        "'1899-12-30' is outside 1900-2299, what a date column holds",
        "'1900-01-01T00:30:00+01:00' is outside 1900-2299, what a datetime column holds",
    ]


def test_a_float_slot_holds_a_number_past_the_decimal_range() -> None:
    columns = (ds.DatasetColumn("n", "number", "number", "f1"),)
    t = _table([("n", "number")], [[1e30]])
    assert ds.typed_rows(t, columns, 0, 1).rows == [{"f1": 1e30, "row_id": 0}]


def test_an_override_converts_text_or_rejects_it() -> None:
    t = _table(
        [("v", "mixed")],
        [["12.5"], ["true"], ["2026-01-02"], [3], [True], ["n/a"]],
    )

    def typed(kind: str) -> tuple[list[Any], list[str]]:
        out = ds.typed_rows(t, ds.infer(t, {"v": kind}).columns, 0, 10)
        return [next(v for k, v in r.items() if k != "row_id") for r in out.rows], [
            s.reason for s in out.sample
        ]

    assert typed("number") == (
        [Decimal("12.500000000"), Decimal("3.000000000")],
        [
            "'true' is not a number",
            "'2026-01-02' is not a number",
            "a boolean is not a number",
            "'n/a' is not a number",
        ],
    )
    assert typed("bool")[0] == [True, True]
    assert typed("date")[0] == [datetime(2026, 1, 2)]
    assert typed("string")[0] == ["12.5", "true", "2026-01-02", "3", "TRUE", "n/a"]


def test_rows_are_typed_a_range_at_a_time_and_the_sample_is_bounded() -> None:
    t = _table([("n", "number")], [["x"]] * (ds.MAX_REJECTED_SAMPLE + 5) + [[1]])
    columns = ds.infer(t, {"n": "number"}).columns
    head = ds.typed_rows(t, columns, 0, 10)
    assert head.rejected == 10 and [s.row for s in head.sample] == list(range(1, 11))
    whole = ds.typed_rows(t, columns, 0, 1000)
    assert whole.rejected == ds.MAX_REJECTED_SAMPLE + 5
    assert len(whole.sample) == ds.MAX_REJECTED_SAMPLE
    assert whole.rows == [{"n1": Decimal("1.000000000"), "row_id": ds.MAX_REJECTED_SAMPLE + 5}]


def test_a_column_with_no_slot_is_left_out() -> None:
    columns = (
        ds.DatasetColumn("a", "string", "string", "s1"),
        ds.DatasetColumn("b", "number", "number", ""),
    )
    t = _table([("a", "string"), ("b", "number")], [["x", "not checked"]])
    assert ds.typed_rows(t, columns, 0, 1).rows == [{"s1": "x", "row_id": 0}]


def test_a_slot_row_names_its_snapshot_and_nulls_the_other_slots() -> None:
    catalog = {c: f"<{c}>" for c in repo.DATASET_COLUMNS}
    now = datetime(2026, 1, 1)
    row = repo.slot_row(catalog, {"s1": "x", "row_id": 4}, now)
    assert set(row) == set(repo.ROW_COLUMNS)
    assert (row["s1"], row["s2"], row["n1"], row["row_id"]) == ("x", None, None, 4)
    for c in ("org", "dataset_id", "snapshot_id", "doc_version", "user_id", "source_class"):
        assert row[c] == catalog[c]


# ---- the ops ---------------------------------------------------------------------

ART = Artifact(
    "csv",
    "loom-extractor",
    "1",
    (Block("b0", "paragraph", "region: EU; amount: 10"),),
    Coverage("rows", 3, 3),
    tables=(
        _table(
            [("region", "string"), ("amount", "mixed")],
            [["EU", 10], ["US", "7"], ["FR", "n/a"]],
            locator=Locator(row_start=1, row_end=4),
        ),
    ),
)


@dataclass
class Stores:
    doc_version: int = 100
    catalog: list[dict[str, Any]] = field(default_factory=list)
    rows: list[dict[str, Any]] = field(default_factory=list)
    forgotten: list[int] = field(default_factory=list)  # snapshot ids
    claim_busy: bool = False
    in_claim: Any = None  # runs once the claim is taken
    sample_reads: int = 0

    def served(self, doc_version: int) -> list[dict[str, Any]]:
        latest: dict[str, dict[str, Any]] = {}
        for r in self.catalog:
            if r["doc_version"] == doc_version and (
                r["dataset_id"] not in latest or r["version"] > latest[r["dataset_id"]]["version"]
            ):
                latest[r["dataset_id"]] = r
        return list(latest.values())


@pytest.fixture
def stores(monkeypatch: pytest.MonkeyPatch) -> Stores:
    from server.knowledge.app import ingest_run
    from server.knowledge.store import blobs
    from server.knowledge.store import documents as documents_repo

    s = Stores()
    data = ART.to_json()

    async def get_one(org: str, ns: str, uri: str, **_: Any) -> Any:
        if s.doc_version == 0:
            return None
        # The ingest's own write: the uploader's identity, as the job's rows carry it.
        return SimpleNamespace(
            version=s.doc_version, revision=3, format="csv", agent="a", user_id="u",
            session_id="-", actor="user:u", source_class="imported",
        )  # fmt: skip

    async def at_version(
        org: str, ns: str, uri: str, v: int, *, dataset_id: str = "", **_: Any
    ) -> list[dict[str, Any]]:
        return [dict(r) for r in s.served(v) if not dataset_id or r["dataset_id"] == dataset_id]

    async def insert_catalog(rows: list[dict[str, Any]], **_: Any) -> None:
        s.catalog.extend(dict(r) for r in rows)

    async def insert_rows(rows: list[dict[str, Any]], **_: Any) -> None:
        s.rows.extend(dict(r) for r in rows)

    async def sample(catalogs: Any, slots: Any, n: int, **_: Any) -> list[dict[str, Any]]:
        s.sample_reads += 1
        out = []
        for c in catalogs:
            mine = [r for r in s.rows if r["snapshot_id"] == c["snapshot_id"]]
            out += sorted(mine, key=lambda r: r["row_id"])[:n]
        return out

    async def forget_snapshot(catalog: dict[str, Any], **_: Any) -> None:
        s.forgotten.append(catalog["snapshot_id"])
        s.rows = [r for r in s.rows if r["snapshot_id"] != catalog["snapshot_id"]]

    async def locate(org: str, ns: str, sha: str, **_: Any) -> Any:
        return blobs.Stored(len(data), "") if sha == ART.sha256 else None

    async def read(org: str, ns: str, sha: str, **_: Any) -> bytes:
        return data

    @asynccontextmanager
    async def claim(org: str, ns: str, uri: str) -> Any:
        if s.in_claim is not None and not s.claim_busy:
            s.in_claim()
        yield not s.claim_busy

    monkeypatch.setattr(documents_repo, "get_one", get_one)
    monkeypatch.setattr(repo, "at_version", at_version)
    monkeypatch.setattr(repo, "insert_catalog", insert_catalog)
    monkeypatch.setattr(repo, "insert_rows", insert_rows)
    monkeypatch.setattr(repo, "sample", sample)
    monkeypatch.setattr(repo, "forget_snapshot", forget_snapshot)

    monkeypatch.setattr(blobs, "locate", locate)
    monkeypatch.setattr(blobs, "read", read)
    monkeypatch.setattr(ingest_run, "claim_source", claim)
    monkeypatch.setattr("server.memory.app.provenance.writer_actor", lambda: "user:approver")
    job = {
        "org": "o",
        "namespace": "ns",
        "source_uri": "repo://sales.csv",
        "agent": "a",
        "user_id": "u",
        "session_id": "-",
        "actor": "user:u",
        "source_class": "imported",
    }
    asyncio.run(
        app.publish(
            job,
            ART,
            doc_version=100,
            revision=3,
            artifact_sha256=ART.sha256,
            prior_version=0,
            lease=None,
        )
    )
    return s


def _get(**kw: Any) -> Any:
    args = {"org": "o", "namespace": "ns", "source_uri": "repo://sales.csv", **kw}
    return asyncio.run(app.handle_knowledge_dataset_get(KnowledgeDatasetGetInput(**args)))


def test_dataset_get_reads_the_served_dataset_with_its_questions_and_rows(stores: Stores) -> None:
    out = _get(sample_rows=2)
    assert out.found and out.revision == 3
    (d,) = out.datasets
    assert (d.name, d.row_start, d.row_end, d.revision) == ("sales.csv", 1, 4, 3)
    assert d.schema_state == "needs_clarification" and d.row_count == 3
    assert [(c.name, c.type, c.inferred, c.stored) for c in d.columns] == [
        ("region", "string", "string", True),
        ("amount", "string", "mixed", True),
    ]
    assert [(c.code, c.column) for c in d.clarifications] == [("mixed_types", "amount")]
    assert d.rows == [{"region": "EU", "amount": "10"}, {"region": "US", "amount": "7"}]
    assert d.snapshot_id == str(stores.catalog[0]["snapshot_id"])


def test_dataset_get_finds_nothing_without_a_live_document_or_at_another_version(
    stores: Stores,
) -> None:
    assert _get(dataset_id="ds_nope").datasets == []
    stores.doc_version = 101  # a text write, or a later revision, now serves
    assert _get().found and _get().datasets == []
    stores.doc_version = 0
    assert not _get().found


def _approve_input(stores: Stores, **kw: Any) -> KnowledgeDatasetApproveInput:
    (row,) = stores.served(100)
    args = {
        "org": "o",
        "namespace": "ns",
        "source_uri": "repo://sales.csv",
        "dataset_id": row["dataset_id"],
        "snapshot_id": str(row["snapshot_id"]),
        **kw,
    }
    return KnowledgeDatasetApproveInput(**args)


def _run_approve(inp: KnowledgeDatasetApproveInput) -> Any:
    return asyncio.run(app.handle_knowledge_dataset_approve(inp))


def test_an_approval_of_the_inferred_types_keeps_the_snapshot(stores: Stores) -> None:
    (before,) = stores.served(100)
    out = _run_approve(_approve_input(stores))
    assert out.found and not out.retyped
    (after,) = stores.served(100)
    assert after["snapshot_id"] == before["snapshot_id"] and after["version"] > before["version"]
    assert (after["schema_state"], after["approved_by"]) == ("approved", "user:approver")
    assert after["approved_at"] is not None and json.loads(after["clarifications"]) == []
    assert out.dataset.schema_state == "approved" and stores.forgotten == []


def test_a_retype_writes_a_new_snapshot_from_the_stored_artifact(stores: Stores) -> None:
    (before,) = stores.served(100)
    out = _run_approve(_approve_input(stores, columns={"amount": "number"}))
    assert out.found and out.retyped
    (after,) = stores.served(100)
    assert after["snapshot_id"] != before["snapshot_id"]
    assert [c["type"] for c in json.loads(after["schema"])] == ["string", "number"]
    assert (after["row_count"], after["rejected_rows"]) == (2, 1)
    assert json.loads(after["rejected"]) == [
        {"row": 3, "column": "amount", "reason": "'n/a' is not a number"}
    ]
    # The rows of the snapshot it replaced go; the uploader still owns the new ones.
    assert stores.forgotten == [before["snapshot_id"]]
    assert {r["snapshot_id"] for r in stores.rows} == {after["snapshot_id"]}
    assert {r["user_id"] for r in stores.rows} == {"u"}
    assert sorted(r["n1"] for r in stores.rows) == [Decimal("7.000000000"), Decimal("10.000000000")]


def test_an_approval_refuses_an_unknown_column_and_a_stale_snapshot(stores: Stores) -> None:
    with pytest.raises(ValidationError, match="not a column"):
        _run_approve(_approve_input(stores, columns={"nope": "number"}))
    with pytest.raises(PreconditionFailedError):
        _run_approve(_approve_input(stores, snapshot_id="1"))
    assert not _run_approve(_approve_input(stores, dataset_id="ds_nope")).found


def test_an_approval_another_approval_overtook_writes_nothing_and_drops_its_rows(
    stores: Stores,
) -> None:
    inp = _approve_input(stores, columns={"amount": "number"})
    (row,) = stores.served(100)

    def overtaken() -> None:  # another approval switched while this one typed its rows
        stores.catalog.append({**row, "snapshot_id": 7, "version": row["version"] + 1})

    stores.in_claim = overtaken
    with pytest.raises(PreconditionFailedError):
        _run_approve(inp)
    (served,) = stores.served(100)
    assert served["snapshot_id"] == 7
    assert len(stores.forgotten) == 1 and stores.forgotten[0] not in (7, row["snapshot_id"])
    assert {r["snapshot_id"] for r in stores.rows} == {row["snapshot_id"]}


def test_an_approval_after_a_new_revision_is_refused_and_after_a_delete_finds_nothing(
    stores: Stores,
) -> None:
    inp = _approve_input(stores)

    def republished() -> None:
        stores.doc_version = 101

    stores.in_claim = republished
    with pytest.raises(PreconditionFailedError):  # live under a later snapshot: read it again
        _run_approve(inp)
    stores.doc_version = 100

    def deleted() -> None:
        stores.doc_version = 0

    stores.in_claim = deleted
    assert not _run_approve(inp).found
    assert all(r["schema_state"] != "approved" for r in stores.catalog)


def test_an_approval_refuses_while_another_write_holds_the_source(stores: Stores) -> None:
    from server.core.errors import BackendUnavailableError

    stores.claim_busy = True
    with pytest.raises(BackendUnavailableError):
        _run_approve(_approve_input(stores))


def test_a_retype_refused_at_the_claim_or_failing_midway_drops_its_rows(stores: Stores) -> None:
    from server.core.errors import BackendUnavailableError

    (row,) = stores.served(100)
    stores.claim_busy = True
    with pytest.raises(BackendUnavailableError):
        _run_approve(_approve_input(stores, columns={"amount": "number"}))
    assert len(stores.forgotten) == 1 and stores.forgotten[0] != row["snapshot_id"]
    assert {r["snapshot_id"] for r in stores.rows} == {row["snapshot_id"]}
    assert stores.served(100) == [row]

    stores.claim_busy = False

    def cut_off() -> None:
        raise TimeoutError  # the claim's deadline, as leases.holding raises it

    stores.in_claim = cut_off
    with pytest.raises(TimeoutError):
        _run_approve(_approve_input(stores, columns={"amount": "number"}))
    assert len(stores.forgotten) == 2
    assert {r["snapshot_id"] for r in stores.rows} == {row["snapshot_id"]}


def test_an_approval_row_names_its_approver_and_keeps_the_uploader(stores: Stores) -> None:
    _run_approve(_approve_input(stores, columns={"amount": "number"}))
    # A second retype takes the rows' owner from the live document row, not the
    # approver's catalog row.
    _run_approve(_approve_input(stores, columns={"amount": "string"}))
    (after,) = stores.served(100)
    assert (after["actor"], after["approved_by"]) == ("user:approver", "user:approver")
    assert after["source_class"] == "asserted"  # the approver's write
    assert (after["user_id"], after["agent"]) == ("u", "a")
    owners = {(r["user_id"], r["agent"], r["actor"], r["source_class"]) for r in stores.rows}
    assert owners == {("u", "a", "user:u", "imported")}


def test_a_retype_of_a_snapshot_with_no_rows_still_keeps_the_uploaders_rows(
    stores: Stores,
) -> None:
    """A retype no value fits stores none; the next retype has no row to read the
    owner off, and takes it from the live document row."""
    out = _run_approve(_approve_input(stores, columns={"region": "date"}))
    assert out.dataset.row_count == 0 and not stores.rows
    _run_approve(_approve_input(stores, columns={"region": "string"}))
    owners = {(r["user_id"], r["actor"], r["source_class"]) for r in stores.rows}
    assert owners == {("u", "user:u", "imported")} and len(stores.rows) == 3


def test_dataset_get_reads_every_datasets_sample_in_one_read(stores: Stores) -> None:
    (row,) = stores.served(100)
    other = {**row, "dataset_id": "ds_other", "snapshot_id": 5, "position": 1}
    stores.catalog.append(other)
    stores.rows.append({**stores.rows[0], "snapshot_id": 5, "dataset_id": "ds_other"})
    out = _get(sample_rows=1)
    assert [d.dataset_id for d in out.datasets] == [row["dataset_id"], "ds_other"]
    assert [len(d.rows) for d in out.datasets] == [1, 1] and stores.sample_reads == 1
    assert _get().datasets and stores.sample_reads == 1  # no rows asked for, no read


def test_a_number_json_would_round_goes_out_as_text() -> None:
    assert app._value(Decimal("10.500000000")) == 10.5
    assert app._value(Decimal("7.000000000")) == 7
    assert app._value(Decimal(2**53)) == str(2**53)
    assert app._value(Decimal("12345678901234567.500000000")) == "12345678901234567.5"
    assert app._value(Decimal("0.100000000")) == 0.1
    # 38 digits, past what the default decimal context holds: every one kept.
    wide = Decimal("99999999999999999999.123456789")
    assert app._value(wide) == "99999999999999999999.123456789"
    assert app._value(Decimal("-12345678901234567890.500000000")) == "-12345678901234567890.5"


def test_an_approval_stamped_before_another_that_switched_first_still_outranks_it(
    stores: Stores,
) -> None:
    (row,) = stores.served(100)
    inp = _approve_input(stores, columns={"amount": "number"})

    def earlier_approval_lands() -> None:  # approved while this call typed its rows
        stores.in_claim = None
        stores.catalog.append(
            {**row, "schema_state": "approved", "version": row["version"] + 10**15}
        )

    stores.in_claim = earlier_approval_lands
    out = _run_approve(inp)
    assert out.found and out.retyped
    (served,) = stores.served(100)
    assert served["snapshot_id"] == int(out.dataset.snapshot_id) != row["snapshot_id"]
    assert {r["snapshot_id"] for r in stores.rows} == {served["snapshot_id"]}


def test_a_catalog_insert_that_may_have_landed_keeps_its_rows(
    stores: Stores, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.core.errors import BackendTimeoutError

    (row,) = stores.served(100)

    async def lands_then_times_out(rows: list[dict[str, Any]], **_: Any) -> None:
        stores.catalog.extend(dict(r) for r in rows)
        raise BackendTimeoutError("no answer", statement_class="write")

    monkeypatch.setattr(repo, "insert_catalog", lands_then_times_out)
    with pytest.raises(BackendTimeoutError):
        _run_approve(_approve_input(stores, columns={"amount": "number"}))
    (served,) = stores.served(100)
    assert served["snapshot_id"] != row["snapshot_id"]
    assert stores.forgotten == []  # neither snapshot: the new one may serve
    assert {r["snapshot_id"] for r in stores.rows} == {row["snapshot_id"], served["snapshot_id"]}


def test_a_samples_row_ids_reach_past_the_rows_it_rejected_below_them() -> None:
    def catalog(rejected_rows: int, rows: list[int]) -> dict[str, Any]:
        return {"rejected_rows": rejected_rows, "rejected": json.dumps([{"row": r} for r in rows])}

    assert repo._first_ids(catalog(0, []), 5) == 5
    assert repo._first_ids(catalog(2, [2, 4]), 5) == 7  # ids 1 and 3 are gaps
    assert repo._first_ids(catalog(1, [90_000]), 5) == 5  # a gap past the first rows costs none
    # Past the sample, the rest lie after the last named: below the bound only
    # when the named gaps carry it past them.
    assert repo._first_ids(catalog(25, list(range(1, 21))), 5) == 30  # ids 0-19, then 5 more
    assert repo._first_ids(catalog(25, [100 + i for i in range(20)]), 5) == 5
    # Every fourth row rejected: 16 gaps (ids 3..63) below, the last named (79) above.
    assert repo._first_ids(catalog(50_000, [4 * i for i in range(1, 21)]), 50) == 66
