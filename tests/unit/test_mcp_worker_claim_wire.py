from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone

import pytest

from server.knowledge.domain.mcp_worker_claim_wire import ClaimWorkerBinding, project_refresh_claim
from server.knowledge.domain.mcp_worker_refresh import (
    DocumentTask,
    LiveLease,
    RefreshRejected,
    RefreshScope,
    WriteAuthority,
)
from server.knowledge.domain.mcp_worker_upload import CurrentRawCopy, RawVersion

_WORKER_ID = "10000000-0000-4000-8000-000000000001"
_OWNER = "fixture-owner"
_SCOPE = RefreshScope(
    org="fixture-org",
    namespace="fixture-queue",
    actor="fixture-actor",
    source_id="20000000-0000-4000-8000-000000000002",
    connection_id="30000000-0000-4000-8000-000000000003",
    destination_id="fixture-destination",
    connection_generation=9007199254740993,
    source_generation=13,
    selection_generation=7,
    approval_generation=11,
    profile_digest="a" * 64,
)
_TASK = DocumentTask("job-café-01", _SCOPE, "mcp://fixture.invalid/selected/01")
_EXPIRY = datetime(2030, 1, 1, 0, 0, 30, 123456, tzinfo=UTC)
_NOW = _EXPIRY - timedelta(microseconds=1)
_LEASE = LiveLease(_TASK, _WORKER_ID, 2, _EXPIRY)
_AUTHORITY = WriteAuthority(_TASK, _WORKER_ID)
_WORKER = ClaimWorkerBinding(
    _WORKER_ID,
    _SCOPE.org,
    _OWNER,
    _SCOPE.connection_id,
    _SCOPE.connection_generation,
    _SCOPE.profile_digest,
)
_RAW = RawVersion(
    "7b49b9e063bd91a4f9252b413261f5557b9c570aa61516989499f64a62dbcdd6", "fixture-revision-01"
)


def test_claim_projects_only_metadata_with_the_current_task_key() -> None:
    lease = replace(_LEASE, expires_at=_EXPIRY.astimezone(timezone(timedelta(hours=-5))))
    result = project_refresh_claim(
        lease,
        authority=_AUTHORITY,
        owner=_OWNER,
        worker=_WORKER,
        now=_NOW,
        current=CurrentRawCopy(_SCOPE, _TASK.resource_id, _RAW),
    )
    assert result == {
        "version": 1,
        "task": {
            "job_id": "job-café-01",
            "scope": {
                "org": "fixture-org",
                "namespace": "fixture-queue",
                "source_id": "20000000-0000-4000-8000-000000000002",
                "connection_id": "30000000-0000-4000-8000-000000000003",
                "destination_id": "fixture-destination",
                "connection_generation": "9007199254740993",
                "selection_generation": "7",
                "approval_generation": "11",
                "profile_digest": "a" * 64,
            },
            "resource_id": "mcp://fixture.invalid/selected/01",
        },
        "worker_id": _WORKER_ID,
        "requester": "fixture-owner",
        "source_generation": "13",
        "fence": 2,
        "expires_at": "2030-01-01T00:00:30.123456Z",
        "idempotency_key": "b79435966b9a3f7cc0b60c1f96c90418a86cfbd476d3a0c0b36dcec8fda96b23",
        "previous": {"body_sha256": _RAW.sha256, "revision": _RAW.revision},
    }


def test_claim_preserves_maximum_generation_and_fence() -> None:
    maximum = (1 << 63) - 1
    scope = replace(
        _SCOPE,
        source_generation=maximum,
        connection_generation=maximum,
        selection_generation=maximum,
        approval_generation=maximum,
    )
    task = replace(_TASK, scope=scope)
    result = project_refresh_claim(
        replace(_LEASE, task=task, fence=(1 << 53) - 1),
        authority=replace(_AUTHORITY, task=task),
        owner=_OWNER,
        worker=replace(_WORKER, connection_generation=maximum),
        now=_NOW,
    )
    assert result["fence"] == 9007199254740991
    assert result["source_generation"] == "9223372036854775807"
    scope_wire = result["task"]["scope"]
    assert scope_wire["connection_generation"] == "9223372036854775807"
    assert scope_wire["selection_generation"] == "9223372036854775807"
    assert scope_wire["approval_generation"] == "9223372036854775807"
    assert result["previous"] is None


@pytest.mark.parametrize(
    "scope,resource_id",
    [
        (replace(_SCOPE, source_generation=12), _TASK.resource_id),
        (replace(_SCOPE, destination_id="another-destination"), _TASK.resource_id),
        (replace(_SCOPE, actor="another-actor"), _TASK.resource_id),
        (_SCOPE, "mcp://fixture.invalid/selected/02"),
    ],
)
def test_claim_omits_previous_outside_the_exact_scope_and_resource(
    scope: RefreshScope, resource_id: str
) -> None:
    result = project_refresh_claim(
        _LEASE,
        authority=_AUTHORITY,
        owner=_OWNER,
        worker=_WORKER,
        now=_NOW,
        current=CurrentRawCopy(scope, resource_id, _RAW),
    )
    assert result["previous"] is None


@pytest.mark.parametrize(
    "task",
    [
        replace(_TASK, scope=replace(_SCOPE, connection_generation=1)),
        replace(_TASK, scope=replace(_SCOPE, source_generation=12)),
        replace(_TASK, scope=replace(_SCOPE, selection_generation=6)),
        replace(_TASK, scope=replace(_SCOPE, approval_generation=10)),
        replace(_TASK, scope=replace(_SCOPE, destination_id="another-destination")),
        replace(_TASK, resource_id="mcp://fixture.invalid/selected/02"),
        None,
    ],
)
def test_claim_refuses_missing_or_stale_task_authority(task: DocumentTask | None) -> None:
    authority = WriteAuthority(task, _WORKER_ID) if task is not None else None
    with pytest.raises(RefreshRejected, match="^refresh claim refused$"):
        project_refresh_claim(_LEASE, authority=authority, owner=_OWNER, worker=_WORKER, now=_NOW)


@pytest.mark.parametrize(
    "worker,owner",
    [
        (replace(_WORKER, worker_id="40000000-0000-4000-8000-000000000004"), _OWNER),
        (_WORKER, "other-owner"),
        (replace(_WORKER, owner="x" * 256), "x" * 256),
        (replace(_WORKER, owner="-"), "-"),
        (replace(_WORKER, owner=" owner"), " owner"),
        (replace(_WORKER, profile_digest="b" * 64), _OWNER),
    ],
)
def test_claim_refuses_mismatched_or_unrepresentable_binding(
    worker: ClaimWorkerBinding, owner: str
) -> None:
    with pytest.raises(RefreshRejected, match="^refresh claim refused$"):
        project_refresh_claim(_LEASE, authority=_AUTHORITY, owner=owner, worker=worker, now=_NOW)


@pytest.mark.parametrize(
    "worker_id,scope",
    [
        ("00000000-0000-0000-0000-000000000000", _SCOPE),
        (_WORKER_ID.replace("-", ""), _SCOPE),
        (_WORKER_ID, replace(_SCOPE, source_id="A0000000-0000-4000-8000-000000000002")),
        (_WORKER_ID, replace(_SCOPE, connection_id="{30000000-0000-4000-8000-000000000003}")),
        (_WORKER_ID, replace(_SCOPE, namespace="x" * 256)),
        (_WORKER_ID, replace(_SCOPE, source_id="00000000-0000-0000-0000-000000000000")),
        (_WORKER_ID, replace(_SCOPE, connection_id="00000000-0000-0000-0000-000000000000")),
    ],
)
def test_claim_refuses_lossy_identity_even_with_matching_authority(
    worker_id: str, scope: RefreshScope
) -> None:
    task = replace(_TASK, scope=scope)
    with pytest.raises(RefreshRejected, match="^refresh claim refused$"):
        project_refresh_claim(
            replace(_LEASE, task=task, worker_id=worker_id),
            authority=WriteAuthority(task, worker_id),
            owner=_OWNER,
            worker=replace(_WORKER, worker_id=worker_id, connection_id=scope.connection_id),
            now=_NOW,
        )


@pytest.mark.parametrize(
    "lease,now",
    [
        (None, _NOW),
        (_LEASE, _EXPIRY),
        (_LEASE, _NOW.replace(tzinfo=None)),
        (replace(_LEASE, expires_at=_EXPIRY.replace(tzinfo=None)), _NOW),
        (replace(_LEASE, fence=0), _NOW),
        (replace(_LEASE, fence=True), _NOW),
        (replace(_LEASE, fence=1 << 53), _NOW),
    ],
)
def test_claim_refuses_invalid_or_expired_lease(lease: LiveLease | None, now: datetime) -> None:
    with pytest.raises(RefreshRejected, match="^refresh claim refused$"):
        project_refresh_claim(lease, authority=_AUTHORITY, owner=_OWNER, worker=_WORKER, now=now)
