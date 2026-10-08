"""code.search `bodies` omitted on the request resolves to the deployment
default; an explicit value always wins; the memo key sees
the resolved value, so an omitted and an explicit default share a memo."""

from __future__ import annotations

import pytest

from server.code.api.models_search import CodeSearchInput
from server.code.app import search_memo as memo


def test_omitted_bodies_is_none_on_the_wire_model() -> None:
    assert CodeSearchInput(query="x").bodies is None
    assert CodeSearchInput(query="x", bodies="none").bodies == "none"


def test_setting_default_is_auto_and_typed() -> None:
    from server.settings import Settings

    assert Settings().code_search_bodies_default == "auto"


@pytest.mark.parametrize("default", ["auto", "none"])
def test_resolution_prefers_the_request_then_the_setting(
    monkeypatch: pytest.MonkeyPatch, default: str
) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "code_search_bodies_default", default)
    omitted = CodeSearchInput(query="x")
    explicit = CodeSearchInput(query="x", bodies="none" if default == "auto" else "auto")
    assert (omitted.bodies or settings.code_search_bodies_default) == default
    assert (explicit.bodies or settings.code_search_bodies_default) == explicit.bodies


def test_memo_key_uses_the_resolved_bodies(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "code_search_bodies_default", "auto")
    omitted = CodeSearchInput(query="how are aliases resolved")
    explicit = CodeSearchInput(query="how are aliases resolved", bodies="auto")
    kw = dict(embedding_model="m", rerank="", token_budget=3000, recent_dirs=[])
    resolved = omitted.model_copy(
        update={"bodies": omitted.bodies or settings.code_search_bodies_default}
    )
    assert memo.key_for(resolved, **kw) == memo.key_for(explicit, **kw)
    assert memo.key_for(resolved, **kw) != memo.key_for(
        CodeSearchInput(query="how are aliases resolved", bodies="none"), **kw
    )
