"""Unit tests for the write-path slice:

  (a) deterministic LLM-free candidate-entity tokenizer feeding the
      memory.set entity projection,
  (c) gated per-field typed extraction with a per-field validation gate +
      local single-field retry that freezes already-validated fields, and
      keeps the OUTER op fail-open (empty/unknown fallback, no 502).

All CH-free: the projection's SQL insert is covered separately by the
integration suite; here we test the pure tokenizer and the LLM-gated
extraction logic against an in-test fake client.
"""

from __future__ import annotations

import pytest

from server.llm.client import LLMResult
from server.memory.app.extract import (
    ExtractedRecord,
    ExtractionError,
    extract_typed_record_iterative,
)
from server.memory.app.extract.handler import _candidate_entities_from_value
from server.memory.app.extract.typed import _coerce_one_field, _field_empty

# ------------------------------------------------------------------
# (a) candidate-entity tokenizer
# ------------------------------------------------------------------


def test_candidate_entities_proper_nouns_first() -> None:
    out = _candidate_entities_from_value("User works at ClickHouse Inc with Alice on the project")
    # Multi-word proper-noun phrase is grabbed and canonicalized; "user"
    # comes from the first-person collapse. Order-preserving, deduped.
    assert "clickhouse inc" in out
    assert "alice" in out
    assert out[0] == "user"


def test_candidate_entities_first_person_collapse() -> None:
    # Multi-word first-person surface forms collapse to the read-side
    # seed form "user" via canonical_entity_text (matching the query
    # canonicalizer). Lone "I" is below the length floor and drops; "my"
    # is a function-word stop — neither is an entity on its own.
    out = _candidate_entities_from_value("The user adopted a dog named Bowie")
    assert "user" in out
    assert "bowie" in out


def test_candidate_entities_walks_dict_values_not_keys() -> None:
    out = _candidate_entities_from_value(
        {"employer": "Blue Bottle Coffee", "amount": 12.5, "note": "met Bob"}
    )
    assert "blue bottle coffee" in out
    assert "bob" in out
    # Structural keys ("employer"/"amount"/"note") are not entities.
    assert "employer" not in out
    assert "amount" not in out


def test_candidate_entities_scalar_and_stopwords_yield_nothing() -> None:
    assert _candidate_entities_from_value(42) == []
    assert _candidate_entities_from_value(True) == []
    # Pure function words below the floor / in the stoplist produce no
    # candidates.
    assert _candidate_entities_from_value("the and of to") == []


def test_candidate_entities_dedup_and_cap() -> None:
    # Repeated tokens collapse to one; cap is enforced.
    out = _candidate_entities_from_value("alpha alpha alpha beta")
    assert out.count("alpha") == 1
    big = " ".join(f"ent{i}" for i in range(100))
    assert len(_candidate_entities_from_value(big)) <= 24


# ------------------------------------------------------------------
# (c) per-field validation gate primitives
# ------------------------------------------------------------------


def test_field_empty_is_never_a_fabricated_value() -> None:
    """The give-up marker is None for EVERY spec. "" / 0 / False would be stored
    by the records projection as value_state='value' (queryable, and wrong),
    and an enum fallback `spec[0]` would be a plausible real category the model
    never extracted."""
    assert _field_empty("string") is None
    assert _field_empty("integer") is None
    assert _field_empty("number") is None
    assert _field_empty("boolean") is None
    assert _field_empty(["coffee", "lunch"]) is None


def test_coerce_one_field_enum_gate() -> None:
    assert _coerce_one_field("cat", "coffee", ["coffee", "lunch"]) == "coffee"
    with pytest.raises(ExtractionError):
        _coerce_one_field("cat", "food", ["coffee", "lunch"])


def test_coerce_one_field_type_coercion() -> None:
    assert _coerce_one_field("amount", "12.5", "number") == 12.5
    assert _coerce_one_field("paid", "yes", "boolean") is True


# ------------------------------------------------------------------
# (c) gated iterative extraction — per-field retry + fail-open
# ------------------------------------------------------------------


class _StagedClient:
    """Drives the three stages by inspecting the system-prompt anchors,
    then injects a per-field failure on the FIRST value pass that the
    single-field retry repairs."""

    provider = "staged"
    model = "staged-model"

    def __init__(self) -> None:
        self.value_passes = 0
        self.calls: list[str] = []

    async def complete(self, system: str, user: str, max_tokens: int = 1024) -> LLMResult:
        self.calls.append(system)
        if "decide whether the source messages contain" in system:
            text = '{"present": true}'
        elif "decide which schema fields are CLEARLY mentioned" in system:
            text = '{"fields": ["category", "amount"]}'
        else:
            # Value stage (initial + per-field retries share this system).
            # The single-field retry is distinguished by the user prompt
            # asking to "Re-extract ONLY".
            if "Re-extract ONLY" in user:
                # Repair pass: emit a valid full record.
                text = '{"record": {"category": "coffee", "amount": 12.5}, "entities": ["blue bottle"]}'
            else:
                self.value_passes += 1
                # Initial pass: amount is non-coercible -> fails its gate.
                text = '{"record": {"category": "coffee", "amount": "not-a-number"}, "entities": ["blue bottle"]}'
        return LLMResult(
            text=text, tokens_in=7, tokens_out=4, provider=self.provider, model=self.model
        )


@pytest.mark.asyncio
async def test_iterative_local_retry_repairs_one_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _StagedClient()
    monkeypatch.setattr("server.memory.app.extract.typed.get_llm_client", lambda: client)
    schema = {"category": "string", "amount": "number"}
    record, llm = await extract_typed_record_iterative(
        [{"role": "user", "content": "Spent 12.50 on coffee at Blue Bottle."}],
        schema,
    )
    assert isinstance(record, ExtractedRecord)
    # category validated on the first pass and FROZEN; amount repaired by
    # the local single-field retry.
    assert record.record == {"category": "coffee", "amount": 12.5}
    # One value pass + exactly one single-field retry for `amount`.
    assert client.value_passes == 1
    # token cost accumulates across object + field + value + retry.
    assert llm.tokens_in == 7 * 4


class _UnrepairableClient(_StagedClient):
    async def complete(self, system: str, user: str, max_tokens: int = 1024) -> LLMResult:
        self.calls.append(system)
        if "decide whether the source messages contain" in system:
            text = '{"present": true}'
        elif "decide which schema fields are CLEARLY mentioned" in system:
            text = '{"fields": []}'
        else:
            # Every value pass leaves `amount` non-coercible — the retry
            # cannot repair it.
            text = '{"record": {"category": "coffee", "amount": "nope"}, "entities": []}'
        return LLMResult(
            text=text, tokens_in=5, tokens_out=3, provider=self.provider, model=self.model
        )


@pytest.mark.asyncio
async def test_iterative_outer_fail_open_falls_back_to_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A field that cannot be validated after its local retries falls back
    to the schema's empty/unknown value — the OUTER op never raises (no
    502) as long as the object is present."""
    client = _UnrepairableClient()
    monkeypatch.setattr("server.memory.app.extract.typed.get_llm_client", lambda: client)
    schema = {"category": "string", "amount": "number"}
    record, _llm = await extract_typed_record_iterative(
        [{"role": "user", "content": "Spent some money on coffee."}],
        schema,
    )
    # category validated; amount is written as the explicit unknown marker
    # (None) — never the fabricated 0.0 — and reported as stubbed.
    assert record.record["category"] == "coffee"
    assert "amount" in record.record
    assert record.record["amount"] is None
    assert record.stubbed_fields == ["amount"]


class _AbsentClient(_StagedClient):
    async def complete(self, system: str, user: str, max_tokens: int = 1024) -> LLMResult:
        self.calls.append(system)
        if "decide whether the source messages contain" in system:
            return LLMResult(
                text='{"present": false}',
                tokens_in=1,
                tokens_out=1,
                provider=self.provider,
                model=self.model,
            )
        return LLMResult(
            text="{}", tokens_in=1, tokens_out=1, provider=self.provider, model=self.model
        )


@pytest.mark.asyncio
async def test_iterative_object_absent_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """object-detect present=false is a TOTAL failure -> ExtractionError
    (502 at the handler), and no later stage runs."""
    from server.memory.app.extract import ExtractionError

    client = _AbsentClient()
    monkeypatch.setattr("server.memory.app.extract.typed.get_llm_client", lambda: client)
    with pytest.raises(ExtractionError):
        await extract_typed_record_iterative(
            [{"role": "user", "content": "totally unrelated chatter here."}],
            {"category": "string"},
        )
    # Only the object-detect stage ran.
    assert len(client.calls) == 1


class _EnumUnrepairableClient(_StagedClient):
    """The model keeps coining a category outside the enum."""

    async def complete(self, system: str, user: str, max_tokens: int = 1024) -> LLMResult:
        self.calls.append(system)
        if "decide whether the source messages contain" in system:
            text = '{"present": true}'
        elif "decide which schema fields are CLEARLY mentioned" in system:
            text = '{"fields": ["amount"]}'
        else:
            text = '{"record": {"category": "snacks", "amount": 4.5}, "entities": []}'
        return LLMResult(
            text=text, tokens_in=5, tokens_out=3, provider=self.provider, model=self.model
        )


@pytest.mark.asyncio
async def test_enum_field_is_stubbed_to_none_not_first_value(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """An unvalidatable ENUM field must not silently become spec[0]
    ("coffee") — a real category the model never said. It is None, listed
    in stubbed_fields, and logged at warning with the field name."""
    import logging

    client = _EnumUnrepairableClient()
    monkeypatch.setattr("server.memory.app.extract.typed.get_llm_client", lambda: client)
    schema = {"category": ["coffee", "lunch"], "amount": "number"}
    with caplog.at_level(logging.WARNING, logger="server.memory.app.extract"):
        record, _llm = await extract_typed_record_iterative(
            [{"role": "user", "content": "Spent 4.50 on snacks."}],
            schema,
        )
    assert record.record["amount"] == 4.5
    assert record.record["category"] is None
    assert record.record["category"] != "coffee"
    assert record.stubbed_fields == ["category"]
    assert any(
        "unvalidatable" in rec.getMessage() and "category" in rec.getMessage()
        for rec in caplog.records
    )
