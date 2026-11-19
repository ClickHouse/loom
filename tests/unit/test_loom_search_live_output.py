"""The optional live section stays bounded and separate from indexed hits."""

from copy import deepcopy

import pytest
from pydantic import ValidationError

from server.memory.api.models_loom_search import LiveSearchResults, SearchInput, SearchOutput


def _hit() -> dict[str, str]:
    return {
        "id": "private-result-id",
        "title": "Private result title",
        "url": "https://example.test/private-result",
        "provider": "gdrive",
        "content": "private excerpt canary",
    }


def _output() -> SearchOutput:
    return SearchOutput.model_validate(
        {
            "memory": {"results": []},
            "knowledge": {"results": []},
            "profile": {"results": []},
            "conversations": {"results": []},
            "live": {"results": [_hit()]},
        }
    )


def test_live_search_requires_an_explicit_boolean_and_is_absent_by_default() -> None:
    base = {"org": "org", "namespace": "scope", "query": "question"}
    assert SearchInput.model_validate(base).live is False
    assert SearchInput.model_validate({**base, "live": True}).live is True
    assert SearchInput.model_validate({**base, "live": False}).live is False
    for value in ("true", "false", 1, 0, None, [], {}):
        with pytest.raises(ValidationError):
            SearchInput.model_validate({**base, "live": value})
    assert "live" not in SearchOutput().model_dump(mode="json")


def test_live_results_bound_every_private_field_and_do_not_become_served_keys() -> None:
    from server.memory.app.served_reads import search_query_keys

    hit = {**_hit(), "id": "memory_key/private-live-only", "content": "x" * 200}
    output = SearchOutput.model_validate(
        {
            "memory": {"results": [{"memory_key": "indexed-key", "namespace": "scope"}]},
            "live": {"results": [hit]},
        }
    )
    expected = {**hit, "live": True}
    assert output.model_dump(mode="json")["live"] == {"live": True, "results": [expected]}
    request = SearchInput(org="org", namespace="scope", query="question")
    assert search_query_keys(request, output) == [("scope", "indexed-key")]
    no_content = {key: value for key, value in _hit().items() if key != "content"}
    assert LiveSearchResults.model_validate({"results": [no_content]}).results[0].content == ""
    assert len(LiveSearchResults.model_validate({"results": [_hit()] * 10}).results) == 10

    for field, limit in (("id", 1024), ("title", 1024), ("url", 4096), ("content", 200)):
        LiveSearchResults.model_validate({"results": [{**_hit(), field: "x" * limit}]})
        with pytest.raises(ValidationError):
            LiveSearchResults.model_validate({"results": [{**_hit(), field: "x" * (limit + 1)}]})
    for invalid in (
        {"results": [_hit()] * 11},
        {"results": [{**_hit(), "provider": "unknown"}]},
        {"results": [{**_hit(), "live": False}]},
        {"results": [{**_hit(), "credential": "never serialize"}]},
        {"results": [_hit()], "proof": "never serialize"},
        {"results": [_hit()], "live": False},
    ):
        with pytest.raises(ValidationError):
            LiveSearchResults.model_validate(invalid)


def test_mcp_declares_and_renders_the_separate_live_section() -> None:
    from server.transport.mcp_tools import _synthetic_tool_descriptors_core, search_answer

    descriptor = next(d for d in _synthetic_tool_descriptors_core() if d["name"] == "loom_search")
    live_input = descriptor["inputSchema"]["properties"]["live"]
    assert live_input["type"] == "boolean"
    assert live_input["default"] is False
    assert "live" not in descriptor["inputSchema"]["required"]
    assert descriptor["outputSchema"]["properties"]["live"]["type"] == "object"

    payload = _output().model_dump(mode="json")
    answer = search_answer(payload, {"query": "question"}, "scope", "")
    text = answer["content"][0]["text"]
    assert "Live source hits:" in text
    for field in ("title", "url", "content"):
        assert _hit()[field] in text
    assert answer["structuredContent"]["live"] == payload["live"]
    for section in ("memory", "knowledge", "profile", "conversations"):
        assert answer["structuredContent"][section] == payload[section]


def test_mcp_body_none_omits_live_excerpts_without_mutating_the_payload() -> None:
    from server.transport.mcp_tools import search_answer

    payload = _output().model_dump(mode="json")
    before = deepcopy(payload)
    for explain in (False, True):
        answer = search_answer(
            payload, {"query": "question", "bodies": "none", "explain": explain}, "scope", ""
        )
        text = answer["content"][0]["text"]
        assert _hit()["content"] not in text
        assert _hit()["title"] in text
        assert _hit()["url"] in text
        live = answer["structuredContent"]["live"]
        assert live["live"] is True
        assert live["results"][0]["live"] is True
        assert "content" not in live["results"][0]
    assert payload == before
