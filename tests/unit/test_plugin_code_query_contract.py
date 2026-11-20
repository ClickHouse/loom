"""The hook contract uses requests and replies served by the existing wire models."""

import json
from pathlib import Path

from server.code.api.models_search import CodeSearchOutput
from server.code.api.models_symbol import (
    CodeCallersInput,
    CodeCallersOutput,
    CodeImpactInput,
    CodeImpactOutput,
    CodeSymbolInput,
    CodeSymbolOutput,
)


def test_prompt_pack_typed_fixtures_match_the_server_wire_contract():
    fixture = (
        Path(__file__).resolve().parents[2]
        / "integrations/claude-code-plugin/tests/fixtures/code-query-wire.json"
    )
    wire = json.loads(fixture.read_text())
    models = {
        "code.symbol": (CodeSymbolInput, CodeSymbolOutput),
        "code.callers": (CodeCallersInput, CodeCallersOutput),
        "code.impact": (CodeImpactInput, CodeImpactOutput),
    }
    for op, (request, response) in models.items():
        assert wire["requests"][op].keys() <= request.model_fields.keys()
        parsed = request.model_validate(wire["requests"][op])
        assert parsed.name == "Parser.parse_record"
        assert wire["responses"][op].keys() <= response.model_fields.keys()
        response.model_validate(wire["responses"][op])
        assert wire["empty_responses"][op].keys() <= response.model_fields.keys()
        response.model_validate(wire["empty_responses"][op])
    CodeSearchOutput.model_validate(wire["responses"]["code.search"])

    body_request = CodeSymbolInput.model_validate(wire["symbol_body"]["request"])
    assert body_request.include_body
    assert body_request.token_budget == 600
    CodeSymbolOutput.model_validate(wire["symbol_body"]["response"])
