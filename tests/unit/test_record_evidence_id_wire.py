"""A record field's evidence_id is a UUID or nothing at the
wire, not a serialization 500 at the ClickHouse insert."""

import pytest
from pydantic import ValidationError

from server.memory.api.models_records import RecordFieldInput


def test_uuid_and_empty_are_accepted() -> None:
    assert RecordFieldInput(value="x", evidence_id=None).evidence_id is None
    assert RecordFieldInput(value="x", evidence_id="").evidence_id is None
    assert RecordFieldInput(value="x", evidence_id="  ").evidence_id is None
    u = "64368ddd-7f23-46a7-befb-36fea4438627"
    assert RecordFieldInput(value="x", evidence_id=u).evidence_id == u
    assert RecordFieldInput(value="x", evidence_id=u.upper()).evidence_id == u


def test_free_text_is_refused_with_a_reason() -> None:
    with pytest.raises(ValidationError, match="memory.evidence"):
        RecordFieldInput(value="x", evidence_id="ev-123")
