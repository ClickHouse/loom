"""A test name no test carries answers with the tests it names.

`tracker.test_signal {test_name: "02346_text_index"}` must not answer
`no_signal` / "0 runs in the window" when no test has that exact name but
many start with it (02346_text_index_postprocessor_tokenizers …). The
answer lists them (`unknown_test`),
answers for the one test a name or file name points at, and never sums a
family into one test's features."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest

from server.tracker.api.models import TrackerTestSignalInput, TrackerTestSignalOutput
from server.tracker.app import handlers as h
from server.tracker.app import source as tsrc
from server.tracker.app import test_signal as tsig
from server.tracker.domain import test_signal as ts
from tests.unit.test_tracker_test_daily import (  # noqa: F401 — `wired` is a fixture
    _CH,
    NS,
    ORG,
    _Daily,
    _daily,
    _Source,
    _window_days,
    wired,
)

FAMILY = "02346_text_index"


def _cand(name: str, runs: int, fails: int = 0, matches: int = 128) -> dict[str, Any]:
    return {"test_name": name, "runs_in_window": runs, "fails_in_window": fails, "matches": matches}


# --- the pure reads ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("asked", "needle"),
    [
        ("02346_text_index", "02346_text_index"),
        (" 02346_text_index.sql ", "02346_text_index"),
        ("tests/queries/0_stateless/03001_foo.sh", "03001_foo"),
        ("03001_foo.reference", "03001_foo"),
        ("03001_foo.sql.j2", "03001_foo"),
        ("test_storage_s3/test.py::test_url", "test_storage_s3/test.py::test_url"),
    ],
)
def test_the_needle_is_the_stem_a_user_means(asked: str, needle: str) -> None:
    assert ts.name_needle(asked) == needle


def test_a_family_is_listed_never_summed() -> None:
    rows = [_cand(f"{FAMILY}_{i}", 150_000 - i, fails=i) for i in range(5)]
    got = ts.name_candidates(FAMILY, FAMILY, rows, prefix=True)
    assert got is not None and got["resolved"] == "" and got["matches"] == 128
    assert [c["test_name"] for c in got["candidates"]] == [f"{FAMILY}_{i}" for i in range(5)]
    assert "128 tests start with '02346_text_index'" in got["why"]
    assert "`candidates` holds the 5 with the most runs; pass one as test_name" in got["why"]
    assert all(set(c) == {"test_name", "runs", "fails"} for c in got["candidates"])


def test_the_only_match_or_a_case_exact_one_is_answered_for() -> None:
    one = ts.name_candidates(
        "03001_fo", "03001_fo", [_cand("03001_foo", 40, matches=1)], prefix=True
    )
    assert one is not None and one["resolved"] == "03001_foo" and one["candidates"] == []
    many = [_cand("03001_Foo", 40, matches=3), _cand("03001_foo_bar", 30, matches=3)]
    exact = ts.name_candidates("03001_foo.sql", "03001_foo", many, prefix=False)
    assert exact is not None and exact["resolved"] == "03001_Foo"
    assert ts.name_candidates("x", "xyz", [], prefix=True) is None


def test_unknown_test_is_a_verdict_and_a_known_answer_keeps_its_shape() -> None:
    assert ts.UNKNOWN_TEST in ts.VERDICTS
    plain = TrackerTestSignalOutput(test_name="00001_select_1").model_dump()
    assert not {"candidates", "candidate_matches", "resolved_from"} & set(plain)
    named = TrackerTestSignalOutput(
        test_name=FAMILY,
        verdict=ts.UNKNOWN_TEST,
        candidates=[{"test_name": "a"}],
        candidate_matches=2,
    ).model_dump()
    assert named["candidates"] == [{"test_name": "a"}] and named["candidate_matches"] == 2


# --- the repo read -------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_prefix_read_is_a_key_range_and_the_substring_read_is_case_blind() -> None:
    from server.tracker.store.daily import TrackerDailyRepo

    ch = _CH([_cand(f"{FAMILY}_a", 10)])
    repo = TrackerDailyRepo(ch)  # type: ignore[arg-type]
    await repo.test_names_like(ORG, NS, FAMILY, date(2026, 8, 26), date(2026, 9, 25), prefix=True)
    _kind, sql, params = ch.calls[-1]
    assert (
        "FROM loom.qa_test_daily FINAL WHERE org = {org:String} AND qa_namespace = {ns:String}"
        in sql
    )
    assert "startsWith(test_name, {n:String})" in sql and "count() OVER () AS matches" in sql
    assert "ORDER BY runs_in_window DESC, test_name LIMIT {lim:UInt32}" in sql
    assert params["n"] == FAMILY and params["lim"] == 5
    await repo.test_names_like(
        ORG, NS, FAMILY, date(2026, 8, 26), date(2026, 9, 25), prefix=False, limit=3
    )
    _kind, sql, params = ch.calls[-1]
    assert "positionCaseInsensitive(test_name, {n:String}) > 0" in sql and params["lim"] == 3


# --- the handler ---------------------------------------------------------------------------


class _NamedDaily(_Daily):
    """The roll-up with no row for the asked name and `names` for the candidate reads."""

    def __init__(
        self,
        now: datetime,
        names: dict[bool, list[dict[str, Any]]],
        rows: list[dict[str, Any]] | None = None,
        fail: bool = False,
    ) -> None:
        super().__init__(_window_days(now), rows or [], [])
        self.names, self.fail = names, fail

    async def test_names_like(self, org, ns, needle, since_day, until_day, *, prefix, limit=5):
        self.calls.append(("names", needle, prefix, limit))
        if self.fail:
            raise RuntimeError("Code: 159. Timeout exceeded")
        return list(self.names.get(prefix, []))


def _wire(monkeypatch, daily: _Daily, live: dict[str, Any] | None = None) -> _Source:
    s = _Source(live)
    monkeypatch.setattr(tsrc, "tracker_source", lambda org, ns: s)
    monkeypatch.setattr(tsig, "_daily_repo", lambda: daily)
    return s


@pytest.mark.asyncio
@pytest.mark.usefixtures("wired")
async def test_a_family_name_answers_unknown_test_with_its_members(monkeypatch) -> None:
    now = datetime.now(UTC).replace(tzinfo=None)
    daily = _NamedDaily(now, {True: [_cand(f"{FAMILY}_{i}", 150_000 - i) for i in range(5)]})
    _wire(monkeypatch, daily)
    out = await tsig.handle_tracker_test_signal(
        TrackerTestSignalInput(org=ORG, namespace=NS, test_name=FAMILY)
    )
    assert out.verdict == ts.UNKNOWN_TEST and out.candidate_matches == 128
    assert [c["test_name"] for c in out.candidates][:2] == [f"{FAMILY}_0", f"{FAMILY}_1"]
    assert out.why[0].startswith(
        "no test named '02346_text_index' in the window; 128 tests start with"
    )
    # nothing is read as one test: no features, no churn or smell legs, one key-range read
    assert out.features == {} and out.churn == {} and out.smells == {}
    assert [c for c in daily.calls if c[0] == "names"] == [
        ("names", FAMILY, True, ts.MAX_CANDIDATES)
    ]
    assert set(out.timings_ms) == {"history", "candidates"}


@pytest.mark.asyncio
@pytest.mark.usefixtures("wired")
async def test_a_file_name_answers_for_its_one_test(monkeypatch) -> None:
    now = datetime.now(UTC).replace(tzinfo=None)
    yday = (now - timedelta(days=1)).date()
    real = "03001_foo"
    daily = _NamedDaily(
        now,
        {True: [], False: [_cand(real, 100, matches=1)]},
        rows=[{**_daily(yday), "test_name": real}],
    )
    orig_window = daily.window

    async def window(org, ns, name, since_day, until_day):
        return await orig_window(org, ns, name, since_day, until_day) if name == real else []

    daily.window = window  # type: ignore[method-assign]
    _wire(monkeypatch, daily)
    out = await tsig.handle_tracker_test_signal(
        TrackerTestSignalInput(
            org=ORG,
            namespace=NS,
            test_name="tests/queries/0_stateless/03001_FOO.sql",
            window_days=14,
        )
    )
    assert out.test_name == real and out.resolved_from == "tests/queries/0_stateless/03001_FOO.sql"
    assert out.why[0] == (
        "no test named 'tests/queries/0_stateless/03001_FOO.sql'; answered for "
        "'03001_foo', the only test that matches"
    )
    assert out.features["runs"] == 100 and out.verdict != ts.UNKNOWN_TEST
    # prefix (case-sensitive key range) first, then the case-blind substring read
    assert [c[2] for c in daily.calls if c[0] == "names"] == [True, False]


@pytest.mark.asyncio
@pytest.mark.usefixtures("wired")
async def test_no_match_or_a_failed_read_keeps_the_plain_answer(monkeypatch) -> None:
    now = datetime.now(UTC).replace(tzinfo=None)
    for daily in (_NamedDaily(now, {}), _NamedDaily(now, {}, fail=True)):
        _wire(monkeypatch, daily)
        out = await tsig.handle_tracker_test_signal(
            TrackerTestSignalInput(org=ORG, namespace=NS, test_name="99999_nothing_like_it")
        )
        assert out.verdict == ts.NO_SIGNAL and out.candidates == [] and out.resolved_from == ""
        assert out.why == ["0 runs in the window, fewer than 5: nothing to read"]


@pytest.mark.asyncio
@pytest.mark.usefixtures("wired")
async def test_flaky_with_signal_carries_the_candidates(monkeypatch) -> None:
    from server.tracker.api.models import TrackerFlakyInput

    now = datetime.now(UTC).replace(tzinfo=None)
    daily = _NamedDaily(now, {True: [_cand(f"{FAMILY}_{i}", 10) for i in range(3)]})
    s = _wire(monkeypatch, daily)

    async def no_issues(org, ns, name, *, limit=10):
        return []

    s.flaky_issues_matching = no_issues  # type: ignore[attr-defined]
    out = await h.handle_tracker_flaky(
        TrackerFlakyInput(org=ORG, namespace=NS, test_name=FAMILY, include_signal=True)
    )
    assert out.signal is not None and out.signal["verdict"] == ts.UNKNOWN_TEST
    assert len(out.signal["candidates"]) == 3
