"""Definition-body reads scope and bound the retained source span."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from server.code.app.symbol.resolve import symbol_body
from server.code.app.symbol.verify import _body_complete, _subject_bodies
from server.code.store.index import CodeRepo
from server.settings import settings


class _CH:
    def __init__(
        self,
        *,
        rows: list[dict[str, Any]],
        span: tuple[int, int] | None = None,
        model: str | None = None,
    ):
        self.rows, self.span = rows, span
        self.model = model
        self.statements: list[str] = []

    async def query_all(self, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        self.statements.append(" ".join(sql.split()))
        assert params == {
            "org": "o",
            "ns": "n",
            "path": "src/a.cpp",
            "start": 10,
            "end": 20,
            "model": self.model if self.model is not None else settings.embedding_model,
        }
        assert "FROM loom.code_chunks FINAL" in sql and "LIMIT 401" in sql
        assert "tombstoned_at IS NULL" in sql and "kind != 'class_skeleton'" in sql
        assert "embedding_model = {model:String}" in sql
        return self.rows

    async def query_one(self, sql: str, params: dict[str, Any]) -> dict[str, Any] | None:
        self.statements.append(" ".join(sql.split()))
        lo, hi = self.span if self.span else (None, None)
        return {"lo": lo, "hi": hi}


SYM = {"path": "src/a.cpp", "symbol_id": "s1", "start_line": 10, "end_line": 20}


def _body(ch: _CH) -> str:
    return asyncio.run(symbol_body(CodeRepo(ch), "o", "n", SYM))  # type: ignore[arg-type]


def test_the_body_read_is_scoped_bounded_and_covers_the_declared_span() -> None:
    text = "source line\n" * 11
    ch = _CH(
        rows=[
            {
                "content": text,
                "start_line": 10,
                "end_line": 20,
                "kind": "function",
                "indexed_commit": "a" * 40,
            }
        ]
    )
    assert _body(ch) == text and len(ch.statements) == 1


def test_a_missing_or_partial_retained_span_returns_no_body() -> None:
    assert _body(_CH(rows=[])) == ""
    assert (
        _body(
            _CH(
                rows=[
                    {
                        "content": "header\n",
                        "start_line": 10,
                        "end_line": 10,
                        "kind": "function",
                        "indexed_commit": "a" * 40,
                    }
                ]
            )
        )
        == ""
    )


@pytest.mark.parametrize("key_missing", [False, True], ids=["factory-error", "missing-key"])
def test_retained_source_does_not_construct_an_unavailable_provider(
    monkeypatch: pytest.MonkeyPatch,
    key_missing: bool,
) -> None:
    from server.embedding import embedder

    def unavailable(*args: Any, **kwargs: Any) -> Any:
        raise ImportError("embedding provider cannot be constructed")

    monkeypatch.setattr(settings, "embedding_model", "text-embedding-3-small")
    monkeypatch.setattr(settings, "embedding_provider", "openai")
    monkeypatch.setattr(embedder, "_cached", None)
    monkeypatch.setattr(embedder, "_key_missing", lambda provider: key_missing)
    monkeypatch.setattr(embedder, "_build", unavailable)
    text = "retained source\n" * 11
    ch = _CH(
        rows=[
            {
                "content": text,
                "start_line": 10,
                "end_line": 20,
                "kind": "function",
            }
        ]
    )
    assert _body(ch) == text
    assert embedder._cached is None


def test_an_unset_model_label_does_not_read_unscoped_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "embedding_model", "")
    ch = _CH(rows=[])
    assert _body(ch) == ""
    assert not ch.statements


@pytest.mark.parametrize(
    ("provider", "label"),
    [("local", "local"), ("fake", "fake-ngram-768")],
)
def test_explicit_provider_overrides_use_the_stored_label_without_construction(
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    label: str,
) -> None:
    from server.embedding import embedder

    def unavailable(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("hydration must not construct a provider")

    monkeypatch.setattr(settings, "embedding_provider", provider)
    monkeypatch.setattr(settings, "embedding_model", "text-embedding-3-small")
    monkeypatch.setattr(settings, "embedding_dim", 768)
    monkeypatch.setattr(embedder, "_cached", None)
    monkeypatch.setattr(embedder, "_build", unavailable)
    text = "retained source\n" * 11
    ch = _CH(
        rows=[
            {
                "content": text,
                "start_line": 10,
                "end_line": 20,
                "kind": "function",
            }
        ],
        model=label,
    )
    assert _body(ch) == text
    assert embedder._cached is None


def test_the_definition_span_is_complete_only_when_the_chunks_reach_both_ends() -> None:
    def complete(span: tuple[int, int] | None) -> bool:
        repo = CodeRepo(_CH(rows=[], span=span))  # type: ignore[arg-type]
        return asyncio.run(_body_complete(repo, "o", "n", [SYM]))

    assert complete((5, 25)) is True
    assert complete((12, 25)) is False
    assert complete(None) is False


def test_a_metadata_span_with_missing_source_cannot_support_a_negative_proof() -> None:
    ch = _CH(
        rows=[
            {"content": "first\n", "start_line": 10, "end_line": 10, "kind": "other"},
            {"content": "last\n", "start_line": 20, "end_line": 20, "kind": "other"},
        ],
        span=(10, 20),
    )
    repo = CodeRepo(ch)  # type: ignore[arg-type]
    bodies, complete = asyncio.run(_subject_bodies(repo, "o", "n", [SYM]))
    assert bodies == [""] and not complete
