"""A serialized search hit identifies the actual writer, including parked results."""

from server.memory.api.models_search import MemorySearchOutput, _MemorySearchHit


def test_search_wire_preserves_writer_identity_through_serialization() -> None:
    hit = _MemorySearchHit(
        memory_key="seat",
        memory_type="semantic",
        content_excerpt="aisle",
        distance=0.0,
        similarity=1.0,
        version=1_800_000_000_000_000_002,
    )
    hit._row_identity = ("writer-b", "alice", "shared")
    wire = MemorySearchOutput(results=[hit], embedding_model="test").model_dump(mode="json")
    assert wire["results"][0]["agent"] == "writer-b"
    assert wire["results"][0]["version_text"] == "1800000000000000002"
    from loom.memory import _parse_search_hit

    assert _parse_search_hit(wire["results"][0]).agent == "writer-b"
    restored = _MemorySearchHit.model_validate(wire["results"][0])
    assert restored.model_dump()["agent"] == "writer-b"


def test_unknown_writer_is_not_guessed_from_a_search_filter() -> None:
    hit = _MemorySearchHit(
        memory_key="seat",
        memory_type="semantic",
        content_excerpt="aisle",
        distance=0.0,
        similarity=1.0,
    )
    assert hit.model_dump()["agent"] == ""


def test_writer_serialization_honors_projection() -> None:
    hit = _MemorySearchHit(
        memory_key="seat",
        memory_type="semantic",
        content_excerpt="aisle",
        distance=0.0,
        similarity=1.0,
    )
    hit._row_identity = ("writer-b", "alice", "shared")
    assert hit.model_dump(include={"memory_key"}) == {"memory_key": "seat"}
    wire = hit.model_dump(exclude={"agent"})
    assert "agent" not in wire
    assert (wire["user_id"], wire["session_id"]) == ("alice", "shared")
    output = MemorySearchOutput(results=[hit], embedding_model="test")
    assert output.model_dump(mode="json", include={"results": {0: {"session_id"}}}) == {
        "results": [{"session_id": "shared"}],
    }
