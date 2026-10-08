"""A qa.* input that names no org reads the deployment's QA
org, the way code.* inputs default to the code plane; the transport
authorizes against the same default; the namespace stays required."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from server.code.app.common import plane_org_for
from server.settings import settings
from server.tracker.api.models import (
    TrackerBindingInput,
    TrackerDuplicateCheckInput,
    TrackerSimilarInput,
)


def test_tracker_inputs_default_org_but_still_require_namespace() -> None:
    assert TrackerSimilarInput(namespace="tracker-clickhouse", text="x").org == ""
    assert TrackerDuplicateCheckInput(namespace="clickgap-public", title="t").org == ""
    with pytest.raises(ValidationError) as e:
        TrackerDuplicateCheckInput(title="t")  # type: ignore[call-arg]
    assert any(err["loc"] == ("namespace",) for err in e.value.errors())


def test_plane_org_for_names_only_the_two_planes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "code_org", "acme-code")
    monkeypatch.setattr(settings, "tracker_org", "acme-tracker")
    assert plane_org_for("code.impact") == "acme-code"
    assert plane_org_for("tracker.duplicate_check") == "acme-tracker"
    assert plane_org_for("memory.search") == ""
    monkeypatch.setattr(settings, "tracker_org", "")
    assert (
        plane_org_for("tracker.similar") == "acme-code"
    )  # the tracker org falls back to the code org


@pytest.mark.asyncio
async def test_bound_input_fills_the_org_before_binding(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.tracker.app import common as tcommon

    monkeypatch.setattr(settings, "tracker_org", "acme-tracker")
    # a tracker namespace answers itself: no rebinding, only the org is filled
    monkeypatch.setattr(
        "server.tracker.domain.namespaces.tracker_namespace", lambda org, ns: object()
    )
    out = await tcommon._bound_input(TrackerSimilarInput(namespace="tracker-clickhouse", text="x"))
    assert out.org == "acme-tracker" and out.namespace == "tracker-clickhouse"
    # an explicit org is never overwritten
    out = await tcommon._bound_input(
        TrackerSimilarInput(org="other", namespace="tracker-clickhouse", text="x")
    )
    assert out.org == "other"


def test_binding_input_gets_the_org_and_keeps_its_namespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.tracker.app import common as tcommon

    monkeypatch.setattr(settings, "tracker_org", "acme-tracker")
    out = tcommon._with_default_org(TrackerBindingInput(namespace="code-clickhouse"))
    assert out.org == "acme-tracker" and out.namespace == "code-clickhouse"
