"""Finite process observations and cumulative obligations for one cell attempt."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from server.core import cell
from server.core.provider_process import certified, has_certificate
from server.insights.domain.cell_participants import bounded_participant, refused

PARTICIPANTS = 1024
OBSERVATIONS = 1024
REPLICAS = 64
METADATA_BYTES = 8 * 1024**2
FINGERPRINT_BYTES = 256 * 1024
FIELD_BYTES = 2048


def text(value: Any, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value):
        raise refused("unreadable process metadata")
    try:
        if "\0" in value or len(value.encode()) > FIELD_BYTES:
            raise ValueError
    except UnicodeError:
        raise refused("unreadable process metadata") from None
    except ValueError:
        raise refused("process metadata exceeds its field bound") from None
    return value


def identity(value: Any, *, instance: bool = False) -> str:
    canonical = text(value)
    try:
        parsed = UUID(canonical)
    except ValueError:
        raise refused("unreadable process identity") from None
    expected = parsed.hex if instance else str(parsed)
    if canonical != expected:
        raise refused("noncanonical process identity")
    return canonical


def _size(value: Any) -> int:
    return len(
        json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()
    )


@dataclass(frozen=True)
class Context:
    """Already bounded own/directory values, supplied by the normal owning caller."""

    cell_name: str
    is_first: bool
    own_pair: tuple[dict[str, Any], ...]
    directory_pair: tuple[dict[str, Any], ...]
    recorded_keys_fp: str | None

    def validate(self) -> None:
        if not isinstance(self.cell_name, str) or not cell.NAME.fullmatch(self.cell_name):
            raise refused("unreadable census cell")
        if type(self.is_first) is not bool:
            raise refused("unreadable census first-cell state")
        for pair in (self.own_pair, self.directory_pair):
            if not 1 <= len(pair) <= 2 or any(not cell.readable_fingerprint(v) for v in pair):
                raise refused("unreadable accepted fingerprint pair")
            if _size(pair) > 64 * 1024:
                raise refused("accepted fingerprint pair exceeds its bound")
        if self.recorded_keys_fp is not None and not cell.SHA256.fullmatch(self.recorded_keys_fp):
            raise refused("unreadable recorded root-key fingerprint")


def _claim(observed: dict[str, Any]) -> dict[str, Any]:
    value = observed["value"]
    claim = value.get("attach_observation")
    keys = {"protocol", "role", "participant_id", "host_proof", "keys_fp", "accepting", "period_ms"}
    if not isinstance(claim, dict) or set(claim) != keys:
        raise refused("process reports no supported attach observation")
    if type(claim["protocol"]) is not int or claim["protocol"] != 1:
        raise refused("process reports an unsupported observation protocol")
    if claim["role"] not in ("server", "worker") or type(claim["accepting"]) is not bool:
        raise refused("process reports an unreadable observation role or acceptance")
    if identity(claim["participant_id"]) != observed["participant_id"]:
        raise refused("inner and outer participant identities disagree")
    proof = text(claim["host_proof"])
    if not has_certificate(proof):
        raise refused("process reports no installed lifecycle certificate")
    keys_fp = text(claim["keys_fp"])
    if not cell.SHA256.fullmatch(keys_fp):
        raise refused("process reports an unreadable root-key fingerprint")
    if type(claim["period_ms"]) is not int or not 1000 <= claim["period_ms"] <= 60000:
        raise refused("process reports an unsupported heartbeat period")
    if type(value.get("serves")) is not bool or type(value.get("marked")) is not bool:
        raise refused("process reports unreadable serving or marker state")
    if claim["role"] == "worker" and value["serves"]:
        raise refused("worker observation claims to serve traffic")
    return claim


@dataclass
class Attempt:
    """One invocation retains first obligations and latest evidence without eviction."""

    participants: dict[str, dict[str, Any]] = field(default_factory=dict)
    observations: dict[str, dict[str, Any]] = field(default_factory=dict)
    _sizes: dict[tuple[str, str], int] = field(default_factory=dict)
    retained_bytes: int = 0
    deadline: float | None = None
    cell_name: str | None = None

    def _retain(self, kind: str, key: str, value: dict[str, Any]) -> None:
        table = self.participants if kind == "participant" else self.observations
        limit = PARTICIPANTS if kind == "participant" else OBSERVATIONS
        if key not in table and len(table) >= limit:
            raise refused(f"cumulative {kind} identity limit exceeded")
        size = _size(value)
        total = self.retained_bytes - self._sizes.get((kind, key), 0) + size
        if total > METADATA_BYTES:
            raise refused("cumulative process metadata byte limit exceeded")
        table[key] = value
        self._sizes[(kind, key)] = size
        self.retained_bytes = total

    def capture(self, rows: dict[str, dict[str, Any]]) -> None:
        for key, raw in rows.items():
            current = bounded_participant(raw)
            if key != current["participant_id"]:
                raise refused("participant census key disagrees with its row")
            birth = certified(current["host_proof"])
            previous = self.participants.get(key)
            if previous:
                if previous["birth"] != birth:
                    raise refused("participant UUID is reused across process births")
                latest = previous["latest"]
                if current["version"] < latest["version"]:
                    raise refused("participant version regressed")
                if current["version"] == latest["version"] and current != latest:
                    raise refused("participant has conflicting equal versions across reads")
                if previous["closed"] and current["state"] != "retired":
                    raise refused("a certified retired participant re-enrolled")
            closed = current["state"] == "retired" and has_certificate(current["host_proof"])
            first = (
                previous["first"] if previous else {"birth": birth, "version": current["version"]}
            )
            self._retain(
                "participant",
                key,
                {"first": first, "latest": current, "birth": birth, "closed": closed},
            )

    def retain_observation(self, observed: dict[str, Any]) -> None:
        key = observed["instance_id"]
        previous = self.observations.get(key)
        participant_id = observed["participant_id"]
        participant = self.participants.get(participant_id)
        if participant is None:
            raise refused("observation has no participant in the complete census")
        # A retired historical row need not acquire an observation capability retroactively.
        if not participant["closed"]:
            claim = _claim(observed)
            if claim["host_proof"] != participant["birth"]:
                raise refused("observation and participant process births disagree")
        digest = hashlib.sha256(
            json.dumps(
                {k: v for k, v in observed.items() if k not in {"age_ms", "generation"}},
                sort_keys=True,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        if previous:
            if previous["participant_id"] != participant_id:
                raise refused("observation identity is reused by another participant")
            if observed["generation"] < previous["generation"]:
                raise refused("observation generation regressed")
            if observed["generation"] == previous["generation"] and digest != previous["digest"]:
                raise refused("observation has conflicting equal generations")
        for other_key, other in self.observations.items():
            if other_key != key and other["participant_id"] == participant_id:
                raise refused("participant reused its observation identity")
        # Retain identity/generation evidence, not a history of fingerprint payloads.
        self._retain(
            "observation",
            key,
            {
                "participant_id": participant_id,
                "generation": observed["generation"],
                "digest": digest,
            },
        )

    def judge(
        self,
        context: Context,
        current: dict[str, dict[str, Any]],
        observed: dict[str, dict[str, Any]],
        instances: dict[str, dict[str, Any]],
    ) -> tuple[tuple[str, ...], str | None]:
        """Return unresolved obligations, never a diagnostic or attach-success flag."""
        context.validate()
        pending: set[str] = set()
        by_participant: dict[str, dict[str, Any]] = {}
        keys: set[str] = set()
        for value in observed.values():
            participant = value["participant_id"]
            if participant in by_participant:
                raise refused("participant has more than one process observation")
            by_participant[participant] = value
            if self.participants[participant]["closed"]:
                continue
            claim = _admitted(context, value)
            if not claim["accepting"] or not 0 <= value["age_ms"] <= 3 * claim["period_ms"]:
                pending.add(participant)
            if not _instance_join(value, claim, instances):
                pending.add(participant)
            keys.add(claim["keys_fp"])
        for key, participant in self.participants.items():
            if not participant["closed"] and (
                key not in current
                or key not in by_participant
                or not has_certificate(current[key]["host_proof"])
            ):
                pending.add(key)
        for key, instance in instances.items():
            if not instance["stopped"]:
                joined = observed.get(key)
                if joined is None:
                    pending.add("instance:" + key)
                elif (
                    self.participants[joined["participant_id"]]["closed"]
                    and instance["age_ms"] <= 180000
                ):
                    pending.add("instance:" + key)
        if len(keys) > 1:
            raise refused("processes report different root-key fingerprints")
        actual_keys = next(iter(keys)) if keys else None
        if (
            actual_keys
            and context.recorded_keys_fp is not None
            and actual_keys != context.recorded_keys_fp
        ):
            raise refused("process root-key fingerprint differs from its recorded value")
        if actual_keys is None:
            pending.add("processes")
        return tuple(sorted(pending)), actual_keys


def _admitted(context: Context, value: dict[str, Any]) -> dict[str, Any]:
    claim = _claim(value)
    if value["cell"] != context.cell_name or value["mode"] != "enforced":
        raise refused("process observation belongs to another or unenforced cell")
    fields = {**cell.DEFAULTS, **value["value"]["fields"]}
    for pair in (context.own_pair, context.directory_pair):
        if not any(cell.matches(fields, accepted) for accepted in pair):
            raise refused("process fingerprint disagrees with an accepted pair")
    if value["value"]["marked"] is not True:
        raise refused("process observation has no tenancy marker")
    if (
        context.is_first
        and claim["role"] == "server"
        and value["value"]["serves"]
        and value["value"].get("router") is not True
    ):
        raise refused("first-cell traffic process has no accepted router key")
    return claim


def _instance_join(
    value: dict[str, Any],
    claim: dict[str, Any],
    instances: dict[str, dict[str, Any]],
) -> bool:
    instance = instances.get(value["instance_id"])
    if claim["role"] == "worker":
        if instance is not None:
            raise refused("worker observation also owns a web instance row")
        return True
    if (
        instance is None
        or instance["stopped"]
        or not 0 <= instance["age_ms"] <= 3 * claim["period_ms"]
    ):
        return False
    if instance["keys_fp"] != claim["keys_fp"]:
        raise refused("instance and observation root-key fingerprints disagree")
    if instance["serving"] and not value["value"]["serves"]:
        raise refused("nonserving observation owns a traffic instance")
    return True
