"""The verbatim chunk plane keeps the whole call.

The per-call chunk cap is a setting (512, ~400K chars), large enough for a
heavy agent transcript with tool results carried whole; a cap of 64 (~50K
chars) would cut such calls short."""

from __future__ import annotations

from server.memory.app import chunk_projection as chunk_projection


def _text(n_paragraphs: int) -> str:
    return "\n\n".join(f"paragraph {i}: " + ("x" * 700) for i in range(n_paragraphs))


def test_default_cap_keeps_a_100k_char_call_whole() -> None:
    text = _text(140)  # ~100K chars
    chunks, truncated = chunk_projection.chunk_text_with_truncation(text)
    assert truncated == 0
    assert len(chunks) > 64


def test_explicit_cap_records_the_unchunked_tail() -> None:
    text = _text(140)
    chunks, truncated = chunk_projection.chunk_text_with_truncation(text, max_chunks=64)
    assert len(chunks) == 64
    assert truncated > 0


def test_setting_drives_the_cap(monkeypatch) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_chunks_max_per_call", 10, raising=False)
    chunks, truncated = chunk_projection.chunk_text_with_truncation(_text(40))
    assert len(chunks) == 10
    assert truncated > 0


def test_zero_or_bad_setting_falls_back_to_the_old_constant(monkeypatch) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_chunks_max_per_call", 0, raising=False)
    assert chunk_projection._max_chunks_per_call() == 64
