"""A bounded attempt retains every process obligation across complete reads."""

from __future__ import annotations

import copy
import json
from uuid import UUID

import pytest

from server.core import cell
from server.core.cell_ch import Refused
from server.insights.domain import cell_observations as domain
from server.insights.store import cell_observations as store

KEYS = "a" * 64
PROOF = "host|boot|container|namespace|123|456|||cell-retirement-v1"


def participant(n=1, *, state="running", version=1, proof=PROOF):
    return {
        "participant_id": str(UUID(int=n)),
        "state": state,
        "version": version,
        "host_proof": proof,
    }


def context(*, first=False):
    fp = cell.as_value({"deployment_id": "dep"})
    return domain.Context("later", first, (fp,), (fp,), KEYS)


def observation(n=1, *, role="server", age=0, generation=1):
    value = {
        **cell.as_value({"deployment_id": "dep"}),
        "participant_id": str(UUID(int=n)),
        "marked": True,
        "serves": role == "server",
        "router": False,
        "attach_observation": {
            "protocol": 1,
            "role": role,
            "participant_id": str(UUID(int=n)),
            "host_proof": PROOF,
            "keys_fp": KEYS,
            "accepting": True,
            "period_ms": 1000,
        },
    }
    raw = json.dumps(value)
    return store.observation(
        {
            "instance_id": UUID(int=n).hex,
            "cell": "later",
            "tenancy_mode": "enforced",
            "fingerprint": raw,
            "fingerprint_bytes": len(raw.encode()),
            "age_ms": age,
            "generation": generation,
        }
    )


def instance(n=1, **changes):
    return {UUID(int=n).hex: {"keys_fp": KEYS, "serving": 1, "stopped": 0, "age_ms": 0, **changes}}


def test_server_and_worker_join_the_same_complete_attempt():
    attempt = domain.Attempt()
    rows = {p["participant_id"]: p for p in (participant(), participant(2))}
    attempt.capture(rows)
    values = {v["instance_id"]: v for v in (observation(), observation(2, role="worker"))}
    for value in values.values():
        attempt.retain_observation(value)
    assert attempt.judge(context(), rows, values, instance()) == ((), KEYS)
    assert len(attempt.participants) == len(attempt.observations) == 2


@pytest.mark.parametrize(
    "fault",
    ["stale", "future", "not-accepting", "missing", "disappeared", "stopped", "no-instance"],
)
def test_unknown_or_nonaccepting_processes_remain_obligations(fault):
    attempt = domain.Attempt()
    row, value = participant(), observation()
    rows = {row["participant_id"]: row}
    attempt.capture(rows)
    if fault == "stale":
        value["age_ms"] = 3001
    elif fault == "future":
        value["age_ms"] = -1
    elif fault == "not-accepting":
        value["value"]["attach_observation"]["accepting"] = False
    values = {} if fault == "missing" else {value["instance_id"]: value}
    for observed in values.values():
        attempt.retain_observation(observed)
    current = {} if fault == "disappeared" else rows
    instances = {} if fault == "no-instance" else instance(stopped=int(fault == "stopped"))
    pending, _ = attempt.judge(context(), current, values, instances)
    assert row["participant_id"] in pending
    assert row["participant_id"] in attempt.participants


@pytest.mark.parametrize(
    "fault",
    [
        "protocol",
        "bool-period",
        "long-period",
        "birth",
        "uuid",
        "worker-instance",
        "cell",
        "marker",
        "fingerprint",
        "keys",
        "router",
    ],
)
def test_inconsistent_observations_refuse(fault):
    attempt = domain.Attempt()
    row, value = (
        participant(),
        observation(role="worker" if fault == "worker-instance" else "server"),
    )
    rows = {row["participant_id"]: row}
    attempt.capture(rows)
    claim = value["value"]["attach_observation"]
    if fault == "protocol":
        claim["protocol"] = 2
    elif fault == "bool-period":
        claim["period_ms"] = True
    elif fault == "long-period":
        claim["period_ms"] = 60001
    elif fault == "birth":
        claim["host_proof"] = PROOF.replace("|123|", "|124|")
    elif fault == "uuid":
        claim["participant_id"] = str(UUID(int=2))
    elif fault == "cell":
        value["cell"] = "other"
    elif fault == "marker":
        value["value"]["marked"] = False
    elif fault == "fingerprint":
        value["value"]["fields"]["deployment_id"] = "other"
    elif fault == "keys":
        claim["keys_fp"] = "b" * 64
    with pytest.raises(Refused):
        attempt.retain_observation(value)
        attempt.judge(
            context(first=fault == "router"), rows, {value["instance_id"]: value}, instance()
        )


def test_retirement_closes_only_the_same_captured_birth_and_keeps_its_budget():
    attempt = domain.Attempt()
    row = participant()
    attempt.capture({row["participant_id"]: row})
    closed = participant(state="retired", version=2)
    attempt.capture({closed["participant_id"]: closed})
    before = copy.deepcopy(attempt.participants)
    assert attempt.judge(context(), {}, {}, {}) == (("processes",), None)
    assert attempt.participants == before and len(before) == 1
    with pytest.raises(Refused, match="birth"):
        bad = participant(state="retired", version=3, proof=PROOF.replace("|123|", "|124|"))
        attempt.capture({bad["participant_id"]: bad})
    assert attempt.participants == before


def test_participant_limit_is_cumulative_including_closed_history():
    attempt = domain.Attempt()
    for n in range(1, 1025):
        row = participant(n, state="retired")
        attempt.capture({row["participant_id"]: row})
    size = attempt.retained_bytes
    attempt.capture({})
    for row in list(attempt.participants.values()):
        current = row["latest"]
        attempt.capture({current["participant_id"]: current})
    assert len(attempt.participants) == 1024 and attempt.retained_bytes == size
    row = participant(1025)
    with pytest.raises(Refused, match="identity limit"):
        attempt.capture({row["participant_id"]: row})
    assert len(attempt.participants) == 1024 and attempt.retained_bytes == size


def test_observation_limit_is_cumulative_without_evicting_closed_rows():
    attempt = domain.Attempt()
    for n in range(1, 1025):
        row = participant(n, state="retired")
        attempt.capture({row["participant_id"]: row})
        attempt.retain_observation(observation(n))
    before = copy.deepcopy(attempt.observations)
    # A different observation identity for an already closed participant is still new history.
    value = observation(1)
    value["instance_id"] = UUID(int=1025).hex
    with pytest.raises(Refused):
        attempt.retain_observation(value)
    assert len(attempt.observations) == 1024 and attempt.observations == before


def test_retained_growth_is_checked_before_replacement(monkeypatch):
    attempt = domain.Attempt()
    row = participant()
    attempt.capture({row["participant_id"]: row})
    before = copy.deepcopy(attempt.participants)
    monkeypatch.setattr(domain, "METADATA_BYTES", attempt.retained_bytes)
    row = participant(version=100000)
    with pytest.raises(Refused, match="byte limit"):
        attempt.capture({row["participant_id"]: row})
    assert attempt.participants == before


def test_repeated_observations_retain_only_latest_evidence_and_check_generation():
    attempt = domain.Attempt()
    row, value = participant(), observation()
    attempt.capture({row["participant_id"]: row})
    attempt.retain_observation(value)
    size = attempt.retained_bytes
    attempt.retain_observation(copy.deepcopy(value))
    assert attempt.retained_bytes == size and len(attempt.observations) == 1
    changed = copy.deepcopy(value)
    changed["value"]["marked"] = False
    with pytest.raises(Refused, match="equal generations"):
        attempt.retain_observation(changed)
    changed = copy.deepcopy(value)
    changed["generation"] = 0
    with pytest.raises(Refused, match="regressed"):
        attempt.retain_observation(changed)
    assert attempt.retained_bytes == size


def test_a_future_replica_cannot_be_hidden_by_a_fresh_replica():
    value = observation()
    raw = json.dumps(value["value"])
    rows = [
        {
            "h": host,
            "instance_id": value["instance_id"],
            "cell": "later",
            "cell_bytes": 5,
            "tenancy_mode": "enforced",
            "tenancy_mode_bytes": 8,
            "fingerprint": raw,
            "fingerprint_bytes": len(raw.encode()),
            "generation": 1,
            "age_ms": age,
        }
        for host, age in (("a", 0), ("b", -1))
    ]
    attempt = domain.Attempt()
    row = participant()
    attempt.capture({row["participant_id"]: row})
    merged = store._collapse_observations(rows, attempt)
    assert merged[value["instance_id"]]["age_ms"] == -1


@pytest.mark.parametrize("fault", ["replica", "logical", "unknown-host"])
def test_replica_and_logical_cap_plus_one_refuse_before_latest_selection(fault):
    from server.core.cell_ch import Topology
    from server.insights.store.provider_participants import bounded_participants

    count = 1 if fault == "unknown-host" else 1025
    rows = [
        {
            **participant(n),
            "proof_bytes": len(PROOF.encode()),
            "h": "outside"
            if fault == "unknown-host"
            else "a"
            if fault == "replica"
            else ("a" if n % 2 else "b"),
        }
        for n in range(1, count + 1)
    ]
    with pytest.raises(Refused):
        bounded_participants(
            rows, Topology("main", 1, (("a", 9000), ("b", 9000))), {"a": "a", "b": "b"}
        )


def test_replica_duplication_does_not_consume_the_logical_identity_allowance():
    from server.core.cell_ch import Topology
    from server.insights.domain.cell_participants import latest
    from server.insights.store.provider_participants import bounded_participants

    rows = [
        {**participant(n), "proof_bytes": len(PROOF.encode()), "h": host}
        for host in ("a", "b")
        for n in range(1, 1025)
    ]
    selected = bounded_participants(
        rows, Topology("main", 1, (("a", 9000), ("b", 9000))), {"a": "a", "b": "b"}
    )
    assert len(selected) == 2048 and len(latest(selected)) == 1024


@pytest.mark.parametrize(
    ("field", "nondefault"),
    [("deployment_id", "dep"), ("public_host", "cell.example.test"), ("configured_orgs", ["acme"])],
)
@pytest.mark.parametrize("pair", ["own", "directory"])
def test_omitted_observation_field_cannot_match_a_nondefault_pair(field, nondefault, pair):
    row, value = participant(), observation(role="worker")
    value["value"]["fields"] = {}
    default = cell.as_value(cell.DEFAULTS)
    accepted = cell.as_value({field: nondefault})
    context = domain.Context(
        "later",
        False,
        (accepted if pair == "own" else default,),
        (accepted if pair == "directory" else default,),
        KEYS,
    )
    attempt = domain.Attempt()
    rows = {row["participant_id"]: row}
    attempt.capture(rows)
    attempt.retain_observation(value)
    with pytest.raises(Refused, match="fingerprint disagrees"):
        attempt.judge(context, rows, {value["instance_id"]: value}, {})


def test_omitted_default_fields_preserve_unknown_observation_fields():
    row, value = participant(), observation(role="worker")
    fields = {"future_field": {"enabled": True}}
    value["value"]["fields"] = copy.deepcopy(fields)
    accepted = cell.as_value({**cell.DEFAULTS, **fields})
    context = domain.Context("later", False, (accepted,), (accepted,), KEYS)
    attempt = domain.Attempt()
    rows = {row["participant_id"]: row}
    attempt.capture(rows)
    attempt.retain_observation(value)
    assert attempt.judge(context, rows, {value["instance_id"]: value}, {}) == ((), KEYS)
    assert value["value"]["fields"] == fields
