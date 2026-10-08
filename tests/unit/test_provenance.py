"""Writer principal + source class are decided by the server."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from server.auth.context import set_current_auth
from server.core.errors import ValidationError
from server.memory.app.provenance import writer_actor
from server.memory.domain import provenance as prov


def test_writer_actor_kind_comes_from_the_token() -> None:
    set_current_auth(SimpleNamespace(subject="alice", is_service_account=False))
    assert writer_actor() == "human:alice"
    set_current_auth(SimpleNamespace(subject="ci-bot", is_service_account=True))
    assert writer_actor() == "process:ci-bot"
    set_current_auth(None)
    assert writer_actor() == ""
    assert writer_actor(fallback="process:consolidation") == "process:consolidation"


def test_trace_conventions_imply_a_class() -> None:
    assert prov.class_from_trace("pipeline:set_from_messages|prompt=x") == "extracted"
    assert prov.class_from_trace("conversation:abc") == "extracted"
    assert prov.class_from_trace("observe.record") == "observed"
    assert prov.class_from_trace("consolidate.dense") == "derived"
    assert prov.class_from_trace("okf:root/path.md") == "imported"
    assert prov.class_from_trace("clickgap://finding/detected") == ""
    assert prov.class_from_trace("") == ""


def test_server_default_applies_when_trace_says_nothing() -> None:
    assert prov.source_class_for(default="asserted", source_trace="app://x") == "asserted"
    assert (
        prov.source_class_for(default="asserted", source_trace="pipeline:set_from_messages")
        == "extracted"
    )


def test_caller_may_only_downgrade() -> None:
    assert prov.source_class_for(default="asserted", requested="external") == "external"
    assert prov.source_class_for(default="asserted", requested="imported") == "imported"
    # A declared downgrade beats the trace convention too.
    assert (
        prov.source_class_for(
            default="asserted", requested="external", source_trace="pipeline:set_from_messages"
        )
        == "external"
    )
    with pytest.raises(ValidationError):
        prov.source_class_for(default="extracted", requested="asserted")
    # `derived` is a downgrade too (an OKF bundle re-importing
    # loom's own inferences keeps them marked as inferences).
    assert prov.source_class_for(default="asserted", requested="derived") == "derived"
    with pytest.raises(ValidationError):
        prov.source_class_for(default="asserted", requested="observed")
    with pytest.raises(ValidationError):
        prov.source_class_for(default="asserted", requested="verified")


def test_every_class_is_known_and_downgrades_are_a_subset() -> None:
    assert prov.CALLER_DECLARABLE < prov.SOURCE_CLASSES
    assert {
        "asserted",
        "extracted",
        "observed",
        "derived",
        "imported",
        "external",
    } == prov.SOURCE_CLASSES
