"""A serialized search hit identifies the actual writer, including parked results."""

from server.memory.api.models_search import MemorySearchOutput, _MemorySearchHit


def test_search_wire_preserves_writer_identity_through_serialization() -> None:
    hit = _MemorySearchHit(
        memory_key="seat",
        memory_type="semantic",
        content_excerpt="aisle",
        distance=0.0,
        similarity=1.0,
    )
    hit._row_identity = ("writer-b", "alice", "shared")
    wire = MemorySearchOutput(results=[hit], embedding_model="test").model_dump(mode="json")
    assert wire["results"][0]["agent"] == "writer-b"
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
    assert "agent" not in hit.model_dump(exclude={"agent"})
