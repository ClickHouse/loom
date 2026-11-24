from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from server.knowledge.domain import mcp_worker_refresh as refresh

_NOW = datetime(2026, 1, 1, tzinfo=UTC)
_SCOPE = refresh.RefreshScope(
    org="org-one",
    namespace="ns-one",
    actor="actor-one",
    source_id="source-one",
    connection_id="connection-one",
    destination_id="collection-one",
    connection_generation=5,
    source_generation=11,
    selection_generation=7,
    approval_generation=2,
    profile_digest="b" * 64,
)
_JOB = refresh.RefreshJob("job-one", _SCOPE, ("resource-one", "resource-two"))
_TASK, _OTHER_TASK = _JOB.document_tasks()
_LEASE = refresh.LiveLease(_TASK, "worker-one", 3, _NOW + timedelta(seconds=20))
_AUTHORITY = refresh.WriteAuthority(_TASK, _LEASE.worker_id)
_PRESENT = refresh.Observation(refresh.ObservationKind.PRESENT, "a" * 64)
_COMPLETE = refresh.Completion(_TASK, _LEASE.fence, _TASK.idempotency_key, _PRESENT)


def _validate(
    completion: refresh.Completion = _COMPLETE,
    *,
    worker_id: str = _LEASE.worker_id,
    lease: refresh.LiveLease | None = _LEASE,
    authority: refresh.WriteAuthority | None = _AUTHORITY,
    now: datetime = _NOW,
) -> refresh.CompletionDecision:
    return refresh.validate_completion(
        completion, worker_id=worker_id, lease=lease, authority=authority, now=now
    )


def test_refresh_plans_exact_selected_items_with_stable_retry_keys() -> None:
    rebuilt = refresh.RefreshJob(_JOB.job_id, _JOB.scope, _JOB.resources)
    assert rebuilt.document_tasks() == (_TASK, _OTHER_TASK)
    assert rebuilt.document_tasks()[0].idempotency_key == _TASK.idempotency_key
    assert _TASK.idempotency_key != _OTHER_TASK.idempotency_key
    assert _TASK.idempotency_key != replace(_TASK, job_id="next-job").idempotency_key
    assert len(_TASK.idempotency_key) == 64


@pytest.mark.parametrize(
    "resources",
    [
        (),
        ("same", "same"),
        tuple(f"resource-{index}" for index in range(refresh.MAX_DOCUMENTS_PER_JOB + 1)),
        ("é" * (refresh.MAX_IDENTIFIER_BYTES // 2 + 1),),
        ("",),
        ("resource\nresult text",),
    ],
)
def test_refresh_plan_rejects_unbounded_or_invalid_selection(resources: tuple[str, ...]) -> None:
    with pytest.raises(refresh.RefreshRejected):
        replace(_JOB, resources=resources)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("org", "org-two"),
        ("namespace", "ns-two"),
        ("actor", "actor-two"),
        ("source_id", "replacement-source"),
        ("destination_id", "other-collection"),
        ("connection_id", "replacement-connection"),
        ("connection_generation", 6),
        ("source_generation", 12),
        ("selection_generation", 8),
    ],
)
def test_scope_change_refuses_old_completion(field: str, value: Any) -> None:
    other_task = replace(_TASK, scope=replace(_SCOPE, **{field: value}))
    assert other_task.idempotency_key != _TASK.idempotency_key
    with pytest.raises(refresh.RefreshRejected, match="authority"):
        _validate(authority=replace(_AUTHORITY, task=other_task))
    with pytest.raises(refresh.RefreshRejected, match="lease"):
        _validate(lease=replace(_LEASE, task=other_task))


@pytest.mark.parametrize("task", [_OTHER_TASK, replace(_TASK, job_id="new-job")])
def test_job_and_resource_must_match_lease_and_selection(task: refresh.DocumentTask) -> None:
    completion = replace(_COMPLETE, task=task, idempotency_key=task.idempotency_key)
    with pytest.raises(refresh.RefreshRejected, match="lease"):
        _validate(completion)
    with pytest.raises(refresh.RefreshRejected, match="authority"):
        _validate(completion, lease=replace(_LEASE, task=task))


def test_refresh_needs_independent_worker_identity_and_current_authority() -> None:
    with pytest.raises(refresh.RefreshRejected, match="lease"):
        _validate(worker_id="worker-two")
    with pytest.raises(refresh.RefreshRejected, match="authority"):
        _validate(authority=replace(_AUTHORITY, worker_id="worker-two"))
    with pytest.raises(refresh.RefreshRejected, match="authority"):
        _validate(authority=None)
    with pytest.raises(refresh.RefreshRejected, match="lease"):
        _validate(lease=None)


@pytest.mark.parametrize("offset", [timedelta(), timedelta(microseconds=1)])
def test_completion_refused_at_or_after_deadline_even_before_reclaim(offset: timedelta) -> None:
    assert _validate(now=_LEASE.expires_at - timedelta(microseconds=1)).receipt_to_commit
    with pytest.raises(refresh.RefreshRejected, match="deadline"):
        _validate(now=_LEASE.expires_at + offset)


def test_completion_refuses_naive_clock_and_lease_times() -> None:
    with pytest.raises(refresh.RefreshRejected, match="deadline"):
        _validate(now=_NOW.replace(tzinfo=None))
    with pytest.raises(refresh.RefreshRejected, match="deadline"):
        _validate(lease=replace(_LEASE, expires_at=_LEASE.expires_at.replace(tzinfo=None)))


def test_reclaimed_lease_refuses_old_fence_even_for_same_worker() -> None:
    renewed = replace(_LEASE, fence=_LEASE.fence + 1)
    with pytest.raises(refresh.RefreshRejected, match="lease"):
        _validate(lease=renewed)
    current = replace(_COMPLETE, fence=renewed.fence)
    assert _validate(current, lease=renewed).receipt_to_commit == refresh.CompletionReceipt(
        _COMPLETE.idempotency_key, _PRESENT
    )


@pytest.mark.parametrize("fence", [0, -1, True, 1 << 32])
def test_invalid_fences_do_not_pass_even_if_lease_matches(fence: int) -> None:
    with pytest.raises(refresh.RefreshRejected, match="lease"):
        _validate(replace(_COMPLETE, fence=fence), lease=replace(_LEASE, fence=fence))


def test_proposed_receipt_requires_current_authority_and_deadline() -> None:
    receipt = refresh.CompletionReceipt(_TASK.idempotency_key, _PRESENT)
    assert _validate() == refresh.CompletionDecision(refresh.CompletionAction.UPSERT, receipt)
    with pytest.raises(refresh.RefreshRejected, match="deadline"):
        _validate(now=_LEASE.expires_at)
    with pytest.raises(refresh.RefreshRejected, match="authority"):
        _validate(authority=None)
    with pytest.raises(refresh.RefreshRejected, match="idempotency key"):
        _validate(replace(_COMPLETE, idempotency_key=_OTHER_TASK.idempotency_key))


@pytest.mark.parametrize(
    "kind",
    [
        refresh.ObservationKind.TIMEOUT,
        refresh.ObservationKind.OMITTED,
        refresh.ObservationKind.INCOMPLETE,
        refresh.ObservationKind.AMBIGUOUS_ABSENCE,
    ],
)
def test_non_observations_do_not_checkpoint(kind: refresh.ObservationKind) -> None:
    incomplete = replace(_COMPLETE, observation=refresh.Observation(kind))
    assert _validate(incomplete) == refresh.CompletionDecision(refresh.CompletionAction.RETRY)


def test_observation_does_not_accept_a_body_or_a_digest_for_an_incomplete_read() -> None:
    with pytest.raises(refresh.RefreshRejected, match="SHA-256"):
        refresh.Observation(refresh.ObservationKind.PRESENT, "result body")
    with pytest.raises(refresh.RefreshRejected, match="carries a digest"):
        refresh.Observation(refresh.ObservationKind.INCOMPLETE, "a" * 64)
