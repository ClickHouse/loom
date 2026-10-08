"""Raw chunks and session digests travel in the OKF bundle."""

from __future__ import annotations

from server.memory.domain import okf_evidence as ev


def _chunk(**over):
    base = dict(
        session_id="conv-26:session_3",
        chunk_index=7,
        content="[user @ 2023-05-08 13:56] I went to the parade.\n[assistant] Nice!\n",
        agent="locomo-bench",
        parent_memory_key="user_parade",
        memory_keys=["user_parade", "user_weekend"],
        line_start=1,
        line_end=2,
        created_at="2026-09-12T07:33:09",
        source_url="memory.set_from_messages:conv-26:session_3",
    )
    base.update(over)
    return ev.ChunkRecord(**base)


def test_chunk_round_trips_byte_exact_with_hash_and_links() -> None:
    c = _chunk()
    files = ev.plan_files([c], [])
    assert list(files) == ["evidence/conv-26-session_3/00007.md"]
    text = files[c.path]
    assert text.startswith("---\ntype: Evidence\n")
    chunks, digests = ev.parse_files(files)
    assert digests == []
    (back,) = chunks
    assert back.content == c.content  # trailing newline preserved
    assert back.content_sha256 == ev.sha256_text(c.content)
    assert back.memory_keys == ["user_parade", "user_weekend"]
    assert (back.session_id, back.chunk_index, back.line_end) == (c.session_id, 7, 2)
    assert ev.edited_chunks(chunks) == []


def test_edited_chunk_is_reported_not_rehashed() -> None:
    c = _chunk()
    files = ev.plan_files([c], [])
    path = c.path
    files[path] = files[path].replace("Nice!", "Lovely!")
    (back,), _ = ev.parse_files(files)
    assert back.content.endswith("Lovely!\n")
    assert back.content_sha256 == ev.sha256_text(c.content)  # exported hash kept
    assert ev.edited_chunks([back]) == [back]


def test_digest_round_trips_and_other_files_are_ignored() -> None:
    d = ev.DigestRecord(
        session_id="conv-26:session_3",
        summary_text="On 2023-05-08 Caroline went to a parade; Melanie planned a camping trip.",
        agent="locomo-bench",
        user_id="-",
        key_facts=["parade", "camping"],
        derived_from=["user_parade", "user_camping"],
        valid_at="2023-05-08T00:00:00",
    )
    files = ev.plan_files([], [d])
    files["semantic/user_parade.md"] = "---\ntype: Fact\ntitle: x\n---\n# x\n"
    files["digests/index.md"] = "# digests\n"
    chunks, digests = ev.parse_files(files)
    assert chunks == []
    (back,) = digests
    assert back.summary_text == d.summary_text
    assert back.derived_from == ["user_camping", "user_parade"]
    assert back.valid_at == "2023-05-08T00:00:00" and back.key_facts == ["parade", "camping"]
    assert ev.is_evidence_path("evidence/a/00001.md") and ev.is_evidence_path("digests/a.md")
    assert not ev.is_evidence_path("semantic/user_parade.md")


def test_digest_links_to_the_concepts_it_compressed() -> None:
    """The digest is the one evidence file that is a claim
    about concepts, so it carries `relates_to` derived_from edges to the ones
    in the bundle; unknown keys and raw chunks link to nothing."""
    d = ev.DigestRecord(session_id="s1", summary_text="digest", derived_from=["k1", "k2", "gone"])
    files = ev.plan_files([_chunk()], [d], {"k1": "semantic/k1.md", "k2": "procedural/k2.md"})
    text = files[d.path]
    assert (
        "relates_to:\n- path: semantic/k1.md\n  kind: derived_from\n- path: procedural/k2.md\n  kind: derived_from\n"
        in text
    )
    assert "relates_to" not in files[_chunk().path]
    (back,) = ev.parse_files(files)[1]
    assert back.derived_from == ["gone", "k1", "k2"] and back.summary_text == "digest"
    assert "relates_to" not in ev.plan_files([], [d])[d.path]
