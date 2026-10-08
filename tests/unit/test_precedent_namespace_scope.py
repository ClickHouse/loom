"""The precedent flag is scoped by namespace globs."""

from __future__ import annotations

from server.memory.app.ask.handler import _precedents_namespace_allowed


def test_precedent_namespace_globs() -> None:
    assert _precedents_namespace_allowed("anything", "")
    assert _precedents_namespace_allowed("clickgap-public", "clickgap-*")
    assert _precedents_namespace_allowed("personal", " clickgap-* , personal ")
    assert not _precedents_namespace_allowed("demo-full2", "clickgap-*")
    assert not _precedents_namespace_allowed("", "clickgap-*")


def test_precedent_namespaces_default_to_every_namespace() -> None:
    from server.settings import Settings

    assert Settings.model_fields["memory_ask_precedents_namespaces"].default == ""
