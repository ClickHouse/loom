from server.memory.domain.evidence_pack import EvidenceItem, pack_evidence


def test_pack_evidence_dedupes_citations_and_groups_sources() -> None:
    packed = pack_evidence(
        [
            EvidenceItem("memory:ns/a", "first", source="fact", group="g1", score=0.9),
            EvidenceItem("memory:ns/a", "duplicate", source="fact", group="g1", score=0.8),
            EvidenceItem("chunk:s1:0", "raw", source="chunk", group="g2", score=0.7),
        ],
        char_budget=1000,
    )

    assert packed.citations == ["memory:ns/a", "chunk:s1:0"]
    assert packed.groups == ["g1", "g2"]
    assert "duplicate" not in packed.rendered


def test_pack_evidence_enforces_budget() -> None:
    packed = pack_evidence(
        [
            EvidenceItem("a", "x" * 80, score=1.0),
            EvidenceItem("b", "y" * 80, score=0.9),
        ],
        char_budget=90,
    )

    assert packed.source_count == 0 or packed.source_count == 1
    assert packed.truncated is True
    assert len(packed.rendered) <= 90


def test_pack_evidence_limits_each_group_before_overflow() -> None:
    packed = pack_evidence(
        [
            EvidenceItem("a1", "a1", group="a", score=1.0),
            EvidenceItem("a2", "a2", group="a", score=0.9),
            EvidenceItem("b1", "b1", group="b", score=0.8),
        ],
        char_budget=1000,
        max_per_group=1,
    )

    first_b = packed.rendered.find("[b1")
    second_a = packed.rendered.find("[a2")
    assert first_b != -1 and second_a != -1
    assert first_b < second_a
