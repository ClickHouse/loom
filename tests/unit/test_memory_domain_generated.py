"""Generated checks for rollback time, scoped identities and provenance rules."""

from __future__ import annotations

import calendar
import unittest
from datetime import UTC, datetime, timedelta, timezone

from hypothesis import given, settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule

from server.core.errors import ValidationError
from server.memory.domain.provenance import source_class_for
from server.memory.domain.rollback import plan_rollback, version_at

_GENERATED = settings(max_examples=60, derandomize=True, database=None, print_blob=True)
_CLASSES = ("asserted", "extracted", "observed", "derived", "imported", "external")
_Identity = tuple[str, str, str, str]
_Version = tuple[int, bool]
_IDENTITIES = tuple(
    (agent, user, session, "same-key")
    for agent in ("a", "b")
    for user in ("u", "v")
    for session in ("s", "t")
)
_IDENTITY = st.sampled_from(_IDENTITIES)


class RollbackTimeProperties(unittest.TestCase):
    @_GENERATED
    @given(st.datetimes(timezones=st.just(UTC)))
    def test_cutoff_preserves_each_microsecond(self, instant: datetime) -> None:
        expected = (
            calendar.timegm(instant.utctimetuple()) * 1_000_000_000 + instant.microsecond * 1_000
        )
        self.assertEqual(version_at(instant), expected)

    @_GENERATED
    @given(
        st.datetimes(min_value=datetime(2, 1, 1), max_value=datetime(9998, 12, 31)),
        st.integers(min_value=-1439, max_value=1439),
    )
    def test_naive_utc_and_offset_representations_agree(
        self, instant: datetime, minutes: int
    ) -> None:
        aware = instant.replace(tzinfo=UTC)
        offset = aware.astimezone(timezone(timedelta(minutes=minutes)))
        self.assertEqual(version_at(instant), version_at(aware))
        self.assertEqual(version_at(offset), version_at(aware))

    def test_minimized_fraction_and_datetime_boundaries(self) -> None:
        self.assertEqual(
            version_at(datetime(2000, 1, 1, microsecond=1, tzinfo=UTC)), 946_684_800_000_001_000
        )
        for instant in (datetime.min, datetime.max, datetime(1969, 12, 31, 23, 59, 59, 999999)):
            expected = (
                calendar.timegm(instant.timetuple()) * 1_000_000_000 + instant.microsecond * 1_000
            )
            self.assertEqual(version_at(instant), expected)
        self.assertEqual(version_at(datetime(1970, 1, 1)), 0)
        for instant in (datetime.min, datetime.max):
            for minutes in (-1439, 1439):
                offset = timezone(timedelta(minutes=minutes))
                self.assertEqual(
                    version_at(instant.replace(tzinfo=offset)),
                    version_at(instant) - minutes * 60 * 1_000_000_000,
                )


class ProvenanceProperties(unittest.TestCase):
    @_GENERATED
    @given(
        st.sampled_from(_CLASSES),
        st.sampled_from(("derived", "external", "imported")),
        st.text(max_size=40),
    )
    def test_explicit_downgrade_wins_over_trace_and_default(
        self, default: str, requested: str, trace: str
    ) -> None:
        self.assertEqual(
            source_class_for(default=default, requested=" " + requested + " ", source_trace=trace),
            requested,
        )

    @_GENERATED
    @given(
        st.sampled_from(_CLASSES),
        st.sampled_from(("asserted", "extracted", "observed")),
        st.text(max_size=40),
    )
    def test_caller_cannot_claim_server_class(
        self, default: str, requested: str, trace: str
    ) -> None:
        with self.assertRaises(ValidationError):
            source_class_for(default=default, requested=requested, source_trace=trace)

    @_GENERATED
    @given(
        st.text(min_size=1, max_size=30).filter(
            lambda value: bool(value.strip()) and value.strip() not in _CLASSES
        )
    )
    def test_unknown_caller_class_is_refused(self, requested: str) -> None:
        with self.assertRaises(ValidationError):
            source_class_for(default="asserted", requested=requested)


class RollbackLifecycle(RuleBasedStateMachine):
    """Compare the planner with versioned snapshots within one supplied scope.

    The planner receives already scoped join rows. This exercises agent/user/session
    identity separation and erased-head safety, not transport or database tenant auth.
    """

    def __init__(self) -> None:
        super().__init__()
        self.clock = 0
        self.history: dict[_Identity, list[_Version]] = {}
        self.heads: dict[_Identity, _Version] = {}

    def _commit(self, identity: _Identity, tombstone: bool) -> None:
        self.clock += 1
        state = (self.clock, tombstone)
        self.history.setdefault(identity, []).append(state)
        self.heads[identity] = state

    @rule(identity=_IDENTITY, tombstone=st.booleans())
    def write_or_tombstone(self, identity: _Identity, tombstone: bool) -> None:
        self._commit(identity, tombstone)

    @rule(identity=_IDENTITY)
    def hard_erase_head(self, identity: _Identity) -> None:
        # Retain stale history to exercise the no-resurrection race defensively.
        self.heads.pop(identity, None)

    def _check(
        self, cutoff: int
    ) -> tuple[dict[_Identity, _Version], set[_Identity], set[_Identity]]:
        past = {
            identity: max(versions)
            for identity, history in self.history.items()
            if (versions := [state for state in history if state[0] <= cutoff])
        }
        current_ids, past_ids = set(self.heads), set(past)
        reinstated = {
            identity
            for identity in current_ids & past_ids
            if self.heads[identity] != past[identity]
        }
        removed = {identity for identity in current_ids - past_ids if not self.heads[identity][1]}
        rows = [
            dict(
                zip(("agent", "user_id", "session_id", "memory_key"), identity, strict=True),
                version_now=self.heads.get(identity, (0, False))[0],
                tombstoned_now=int(self.heads.get(identity, (0, False))[1]),
                version_at=past.get(identity, (0, False))[0],
            )
            for identity in sorted(set(self.history) | current_ids)
        ]
        plan = plan_rollback(rows)
        actual = [
            (
                action.agent,
                action.user_id,
                action.session_id,
                action.memory_key,
                action.action,
                action.version_from,
                action.version_to,
            )
            for action in plan.reinstate + plan.remove
        ]
        expected = [
            (*identity, "reinstate", self.heads[identity][0], past[identity][0])
            for identity in reinstated
        ]
        expected += [(*identity, "remove", self.heads[identity][0], 0) for identity in removed]
        assert sorted(actual) == sorted(expected)
        assert plan.changes == len(reinstated) + len(removed)
        assert plan.skipped_no_head == len(past_ids - current_ids)
        assert plan.skipped_already_gone == sum(
            self.heads[identity][1] for identity in current_ids - past_ids
        )
        return past, reinstated, removed

    @rule(point=st.integers(min_value=0, max_value=200))
    def rollback(self, point: int) -> None:
        past, reinstated, removed = self._check(point % (self.clock + 1))
        # Apply the independent snapshot model, not the returned planner actions.
        for identity in sorted(reinstated):
            self._commit(identity, past[identity][1])
        for identity in sorted(removed):
            self._commit(identity, True)

    @invariant()
    def current_and_empty_snapshots_agree(self) -> None:
        self._check(self.clock)
        self._check(0)


TestRollbackLifecycle = RollbackLifecycle.TestCase
TestRollbackLifecycle.settings = settings(
    max_examples=35, stateful_step_count=25, derandomize=True, database=None, print_blob=True
)


if __name__ == "__main__":
    unittest.main()
