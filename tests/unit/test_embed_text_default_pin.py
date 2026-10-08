"""The default embed text, byte for byte.

The stored vectors of `memory_embeddings` are built from the text
`_build_embed_text` returns on the default path (and that `loom reembed`
rebuilds from the stored excerpt); a change to it silently splits the corpus
into two embeddings of the same row. This pins the digest of that text for a
few shapes — plain value, key suffix, answer topic, a long value, a JSON
object — on the class defaults (computed with and without the row's tags
and entities, which the default never embeds). Update the digests only
when the embed text is meant to change, together with a re-embed plan.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from server.settings import Settings

_CASES: list[tuple[str, str, str, str]] = [
    # (name, serialized value, memory_key, question_topic)
    ("plain", json.dumps("Lives in Lisbon since 2021."), "user_city", ""),
    ("newlines", json.dumps("parts failed to move\nbetween disks"), "finding_h_pr112639", ""),
    ("no_key", json.dumps("The deploy window is Tuesdays 14:00 UTC."), "", ""),
    ("topic", json.dumps("Use 3 replicas and a 7-day TTL."), "ch_cluster_advice", "cluster sizing"),
    ("long", json.dumps("word " * 900), "long_note", ""),
    ("object", json.dumps({"city": "Lisbon", "since": 2021}), "user_home", ""),
]

_EXPECTED: dict[str, str] = {
    "plain": "66cc4400568a70019039307621b9febbc2078f6bca4bde64ed256f552ab85b35",
    "newlines": "df11934c5be05ed3eadb65221fc15f45a1e10cf95a74b469a9cf1ca1f7f96f5d",
    "no_key": "e08a09698b9081da2abf68a521e158e670609d363fb231e616e4441c4ff778d6",
    "topic": "f9219265da4cd257d39ec632042c82323540ed79f1c4559dd25141720f6d1007",
    "long": "c8e4073c3426f8ce2626d1ff2a3032ba4e7635c841baeb47f44447f03ea99b9e",
    "object": "e8d95a444ee2d2e25ff62b17b478a26c98285183b360bb5fee288149c15475fe",
}


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


@pytest.fixture
def defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    import server.memory.app.handlers as hm
    import server.settings as sm

    fresh = Settings(_env_file=None)  # type: ignore[call-arg]
    monkeypatch.setattr(hm, "settings", fresh)
    monkeypatch.setattr("server.memory.app.write.settings", hm.settings)
    monkeypatch.setattr(sm, "settings", fresh)


@pytest.mark.parametrize(("name", "serialized", "key", "topic"), _CASES)
def test_default_embed_text_is_pinned(
    defaults: None, name: str, serialized: str, key: str, topic: str
) -> None:
    from server.memory.domain.embed_text import _build_embed_text

    text = _build_embed_text(serialized, memory_key=key, question_topic=topic)
    assert _sha(text) == _EXPECTED[name], (name, text[:200])


@pytest.mark.parametrize(("name", "serialized", "key", "topic"), _CASES)
def test_reembed_rebuilds_the_same_default_text(
    defaults: None, name: str, serialized: str, key: str, topic: str
) -> None:
    """`loom reembed` feeds the embedder the stored excerpt as-is; that is the
    write path's text again."""
    from server.memory.domain.embed_text import _build_embed_text
    from server.memory.domain.excerpt import build_excerpt

    if topic:
        pytest.skip("a topic prefix lives only in the write-time embed text")
    excerpt = build_excerpt(serialized, memory_key=key)
    assert excerpt == _build_embed_text(serialized, memory_key=key)
