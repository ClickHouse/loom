"""Derived record identity: canonicalisation per field kind,
a stable key, parity with the stored record, and no repair of missing identity."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

import pytest

from server.memory.domain.record_identity import canonical_record_key, canonical_value, is_name_like


def test_name_like_fields_agree_across_articles_possessives_case_and_separators() -> None:
    assert (
        is_name_like("name") and is_name_like("vendor_name") and not is_name_like("invoice_number")
    )
    variants = ["The Vendor's Name", "vendor name", "Vendor-Name", "  vendor_name ", "Véndor Name"]
    assert {canonical_value("name", v) for v in variants} == {"VENDOR_NAME"}


def test_other_fields_squash_to_alphanumerics_and_keep_digits() -> None:
    assert {
        canonical_value("invoice_number", v)
        for v in ["INV-2024/001", "inv 2024 001", "inv_2024_001"]
    } == {"inv2024001"}
    assert canonical_value("run", "run-1") != canonical_value("run", "run-2"), "digits survive"
    assert canonical_value("amount", 12.0) == "12" and canonical_value("amount", 12.5) == "125"
    assert canonical_value("tags", ["b", "A"]) == "a|b", "lists sort"
    assert canonical_value("x", None) == "" and canonical_value("x", "   ") == ""


def test_key_is_stable_prefixed_and_order_free() -> None:
    k1 = canonical_record_key(
        "invoice", {"invoice_number": "INV-2024/001", "vendor_name": "The Vendor's Name"}
    )
    k2 = canonical_record_key(
        "invoice", {"vendor_name": "vendor name", "invoice_number": "inv 2024 001"}
    )
    assert k1 == k2 and k1.startswith("invoice:") and len(k1) == len("invoice:") + 16
    assert (
        canonical_record_key(
            "receipt", {"invoice_number": "INV-2024/001", "vendor_name": "vendor name"}
        )
        != k1
    )


def test_empty_identity_is_rejected_not_repaired() -> None:
    with pytest.raises(ValueError):
        canonical_record_key("invoice", {"invoice_number": ""})
    with pytest.raises(ValueError):
        canonical_record_key("invoice", {"invoice_number": None})


def test_a_key_with_latin_letters_or_digits_is_unchanged() -> None:
    # Pinned: a stored record is found again only under the key it was written with.
    assert (
        canonical_record_key(
            "invoice", {"invoice_number": "INV-2024/001", "vendor_name": "The Vendor's Name"}
        )
        == "invoice:bef11f75f1b36a62"
    )
    assert canonical_value("scenario", "Выбор БД v2") == "v2", "the Latin form wins when it exists"


def test_text_without_latin_letters_keys_on_its_own_script() -> None:
    scenarios = ["Срок хранения журналов", "原始记录保留多久", "مدة الاحتفاظ بالسجلات", "किताब"]
    keys = [canonical_record_key("decision", {"scenario": s}) for s in scenarios]
    assert len(set(keys)) == len(scenarios), "each script gives its own key"
    assert keys == [canonical_record_key("decision", {"scenario": s}) for s in scenarios]
    assert canonical_value("scenario", "СРОК  хранения!") == canonical_value(
        "scenario", "срок хранения"
    ), "case, spacing and punctuation do not count"
    assert canonical_value("scenario", "किताब") == "किताब", "an Indic vowel sign stays"
    assert canonical_value("scenario", "보존") == "보존", "Hangul stays composed"
    assert canonical_value("scenario", "原始记录") != canonical_value("scenario", "原始纪录")


def test_punctuation_alone_has_no_key() -> None:
    assert canonical_value("scenario", "?! … —") == ""
    with pytest.raises(ValueError):
        canonical_record_key("decision", {"scenario": "?! … —"})


# --- handler: a caller-less write lands on the derived key and supersedes ----


class _CH:
    def __init__(self) -> None:
        self.executes: list[dict[str, Any]] = []
        self.inserts: list[tuple[str, list[dict[str, Any]]]] = []

    async def execute(self, sql, params=None):
        self.executes.append(params or {})

    async def insert(self, table, rows, settings=None):
        self.inserts.append((table, rows))

    async def query_one(self, sql, params=None):
        return None  # no active record for the key: nothing to fold onto


def _write(fields: dict[str, dict[str, Any]], memory_key: str = "") -> tuple[Any, _CH]:
    from server.memory.api.models_records import MemoryRecordWriteInput
    from server.memory.app import records as h

    ch = _CH()

    async def schema_by_name(org, ns, name):
        return {"schema_id": "11111111-1111-1111-1111-111111111111", "status": "active"}

    async def schema_fields(org, ns, sid):
        return [
            {
                "field_name": "invoice_number",
                "field_type": "string",
                "required": True,
                "identity": True,
                "enum_values": [],
            },
            {
                "field_name": "vendor_name",
                "field_type": "string",
                "required": False,
                "identity": True,
                "enum_values": [],
            },
            {
                "field_name": "total",
                "field_type": "number",
                "required": False,
                "identity": False,
                "enum_values": [],
            },
        ]

    inp = MemoryRecordWriteInput(
        org="o",
        namespace="n",
        schema_name="invoice",
        memory_key=memory_key,
        fields={k: v for k, v in fields.items()},
    )
    with (
        patch.object(h, "_schema_by_name", new=schema_by_name),
        patch.object(h, "_schema_fields", new=schema_fields),
        patch.object(h, "get_client", return_value=ch),
    ):
        out = asyncio.run(h.handle_memory_record_write(inp))
    return out, ch


def test_write_without_key_derives_it_and_supersedes_on_it() -> None:
    out, ch = _write(
        {
            "invoice_number": {"value": "INV-2024/001"},
            "vendor_name": {"value": "The Vendor's Name"},
            "total": {"value": 12.5},
        }
    )
    assert out.key_source == "identity" and out.memory_key.startswith("invoice:")
    assert ch.executes and ch.executes[0]["key"] == out.memory_key, (
        "supersede query runs on the derived key"
    )
    assert (
        ch.inserts[0][0] == "loom.memory_records"
        and ch.inserts[0][1][0]["memory_key"] == out.memory_key
    )
    again, _ = _write(
        {"invoice_number": {"value": "inv 2024 001"}, "vendor_name": {"value": "vendor name"}}
    )
    assert again.memory_key == out.memory_key, (
        "a second extraction of the same invoice lands on the same key"
    )


def test_caller_key_wins_and_missing_identity_is_rejected() -> None:
    out, _ = _write(
        {"invoice_number": {"value": "X"}, "vendor_name": {"value": "Y"}}, memory_key="mine"
    )
    assert out.key_source == "caller" and out.memory_key == "mine"
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as ei:
        _write({"invoice_number": {"value": "X"}, "vendor_name": {"state": "unknown"}})
    assert ei.value.detail["code"] == "MISSING_IDENTITY_FIELD" and ei.value.detail["fields"] == [
        "vendor_name"
    ]


def test_an_identity_value_of_punctuation_alone_is_a_missing_identity() -> None:
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as ei:
        _write({"invoice_number": {"value": "X"}, "vendor_name": {"value": "  "}})
    assert ei.value.status_code == 400 and ei.value.detail["fields"] == ["vendor_name"]
    with pytest.raises(HTTPException) as ei:
        _write({"invoice_number": {"value": "#/-"}, "vendor_name": {"value": "Y"}})
    assert ei.value.detail["code"] == "MISSING_IDENTITY_FIELD"
    assert ei.value.detail["fields"] == ["invoice_number"]
    out, _ = _write({"invoice_number": {"value": "№ ٣٤٥"}, "vendor_name": {"value": "Вектор"}})
    assert out.key_source == "identity" and out.memory_key.startswith("invoice:")
