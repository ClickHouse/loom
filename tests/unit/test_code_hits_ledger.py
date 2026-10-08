"""Code-plane grounding is visible on the wire and in the ledger."""

from __future__ import annotations

from server.memory.api.models_ask import MemoryAskOutput
from server.memory.app.ask import MemoryAskResult
from server.observability.event_args import IDENTIFIER_KEYS


def test_code_hits_rides_the_result_the_output_and_the_ledger() -> None:
    assert MemoryAskResult(answer="", source="synthesized").code_hits == 0
    assert "code_hits" in MemoryAskOutput.model_fields
    assert "code_hits" in IDENTIFIER_KEYS
