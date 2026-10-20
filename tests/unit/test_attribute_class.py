"""Protected-attribute labels: a tag at write time, never a
gate; the profile fold leaves labelled rows out; extraction never leaves
provenance silent."""

from __future__ import annotations

from server.memory.domain import sensitivity as sens
from server.memory.domain.extracted_facts import _source_role_tags


def test_attribute_classes_are_detected_in_a_fixed_order():
    text = (
        "User is a 34-year-old married woman who uses Catholic Church prayer apps, "
        "manages anxiety, and voted for the liberal candidate."
    )
    assert sens.attribute_classes(text) == [
        "gender",
        "religion",
        "health",
        "age",
        "family",
        "politics",
    ]
    assert sens.attribute_classes("Prefers dark mode and terse answers.") == []
    assert sens.attribute_classes("") == []


def test_proxy_vocabulary_is_covered_and_bounded():
    assert "gender" in sens.attribute_classes("A former actress and now a waitress.")
    assert "ethnicity" in sens.attribute_classes("Visa status pending; immigrated in 2019.")
    assert "income" in sens.attribute_classes("Salary expectations around 90k.")
    # Only the first 4,000 chars are scanned.
    assert sens.attribute_classes("x" * 5000 + " pregnant") == []


def test_attribute_tags_respect_existing_and_the_setting(monkeypatch):
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_attribute_tagging", True)
    tags = sens.attribute_tags(
        "User is Muslim and 60 years old", ["attribute:religion", "preference"]
    )
    assert tags == ["attribute:age"]
    monkeypatch.setattr(settings, "memory_attribute_tagging", False)
    assert sens.attribute_tags("User is Muslim") == []


def test_extraction_batch_rows_are_labelled(monkeypatch):
    from server.memory.app.bulk_rows import _with_attribute_tags
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_attribute_tagging", True)
    out = _with_attribute_tags(["preference"], "User is drawn to strong female protagonists.", "ns")
    assert out == ["preference", "attribute:gender"]
    assert _with_attribute_tags(None, "likes tea", "ns") == []


def test_profile_fold_excludes_attribute_rows():
    from server.memory.store import profile_sources
    from server.memory.workers import profile_consolidation as pc

    assert pc.user_facts is profile_sources.user_facts  # the fold reads the shared predicate
    src = profile_sources.CURRENT_FACTS
    assert "NOT arrayExists(t -> startsWith(t, 'attribute:'), tags)" in src


def test_unattributed_facts_get_a_provenance_tag():
    msgs = [{"role": "user", "content": "I like tea"}, {"role": "assistant", "content": "Noted"}]
    assert _source_role_tags(msgs, {"source_message_indices": [0]}) == ["source_role:user"]
    assert _source_role_tags(msgs, {"source_message_indices": []}) == ["source_role:unattributed"]
    assert _source_role_tags(msgs, {}) == ["source_role:unattributed"]
    assert _source_role_tags(msgs, {"source_message_indices": [7]}) == ["source_role:unattributed"]
