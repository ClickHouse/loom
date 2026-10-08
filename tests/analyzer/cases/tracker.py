"""Loom's own tables of the tracker plane.

- The issue–PR link candidates table: the pass's insert is a VALUES insert
  (unchecked); the readers are point reads on the key, the PR direction and
  the stamp reads.
- The per-test daily roll-up's readers: the coverage ledger,
  one test's window, the co-failure hours and the window's median.
- The set-wide backport report's table: one branch's rows,
  every branch's, the pass's stamps and the trunk ledger's labelled fixes.
- The tracker record's own vectors: the pass's digest scan
  and keyed rows, the leg's stored-vector point read, the cosine scan with its
  wall and exclusion, and the namespace stats.

The remote statements of `TrackerSource` run at the QA service, not here; the
corpus holds loom's side."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from tests.analyzer.cases._common import NS, ORG
from tests.analyzer.corpus import Case, RecordingCH

LINKS_MODULE = "server.tracker.store.links"
DAILY_MODULE = "server.tracker.store.daily"
GAPS_MODULE = "server.code.store.backport_gaps"
EMBED_MODULE = "server.tracker.store.embed"


async def _links(ch: RecordingCH, c: dict[str, Any]) -> None:
    from server.tracker.store.links import TrackerLinksRepo

    repo = TrackerLinksRepo(ch)  # type: ignore[arg-type]
    await repo.candidates_for_issue(ORG, NS, 118120, limit=c["limit"])
    await repo.candidates_for_pr(ORG, NS, 118497, limit=c["limit"])
    await repo.latest_computed(ORG, NS, [118120, 118121])
    await repo.namespace_stats(ORG, NS)


async def _readers(ch: RecordingCH, c: dict[str, Any]) -> None:
    from server.tracker.store.daily import TrackerDailyRepo

    repo = TrackerDailyRepo(ch)  # type: ignore[arg-type]
    d0, d1 = date(2026, 8, 17), date(2026, 9, 16)
    await repo.rolled_days(ORG, NS, d0, d1)
    await repo.window(ORG, NS, "00001_select_1", d0, d1)
    hours = [datetime(2026, 9, 15, 16), datetime(2026, 9, 15, 17)]
    await repo.hour_stats(ORG, NS, hours if c["hours"] else hours[:1])
    await repo.hour_median(ORG, NS, datetime(2026, 8, 17, 4), datetime(2026, 9, 16, 9))
    # The candidate reads for a name no test carries
    for prefix in (True, False):
        await repo.test_names_like(ORG, NS, "02346_text_index", d0, d1, prefix=prefix)


async def _gaps(ch: RecordingCH, c: dict[str, Any]) -> None:
    from server.code.store.backport_gaps import CodeBackportGapsRepo
    from server.code.store.index import CodeRepo

    repo = CodeBackportGapsRepo(ch)  # type: ignore[arg-type]
    since = datetime(2026, 6, 18)
    await repo.rows_for_branch(
        ORG, NS, "code-clickhouse-26-6", since=since, wanted_only=c["wanted"], limit=500
    )
    await repo.rows_for_namespace(ORG, NS, since=since, wanted_only=c["wanted"], limit=500)
    await repo.rows_for_namespace(
        ORG, NS, since=since, wanted_only=c["wanted"], limit=500, branches=["code-clickhouse-26-6"]
    )
    await repo.fix_stamps(ORG, NS)
    await repo.namespace_stats(ORG, NS)
    code = CodeRepo(ch)  # type: ignore[arg-type]
    await code.labelled_fixes_in_window(ORG, NS, since=since)


async def _embed(ch: RecordingCH, c: dict[str, Any]) -> None:
    from server.tracker.store.embed import TrackerEmbedRepo

    repo = TrackerEmbedRepo(ch)  # type: ignore[arg-type]
    await repo.stored(ORG, NS)
    await repo.rows_by_key(ORG, NS, "issue", [119601, 116490])
    await repo.stored_digests(ORG, NS, "issue", [119601, 116490])
    await repo.stored_vector(ORG, NS, "issue", 119601)
    await repo.nearest(
        ORG,
        NS,
        vector=[0.1] * 8,
        dim=8,
        limit=50,
        kinds=["issue", "pr"] if c["walled"] else None,
        before=datetime(2026, 9, 16) if c["walled"] else None,
        exclude=("issue", 119601) if c["walled"] else None,
        # The bisect window's PRs only
        numbers=[119601, 116490] if c["walled"] else None,
    )
    await repo.namespace_stats(ORG, NS)


def cases() -> list[Case]:
    return [
        Case("tracker_links", LINKS_MODULE, _links, {"limit": [10]}),
        Case("tracker_embed", EMBED_MODULE, _embed, {"walled": (False, True)}),
        Case("code_backport_gaps", GAPS_MODULE, _gaps, {"wanted": (False, True)}),
        Case("tracker_daily_repo.readers", DAILY_MODULE, _readers, {"hours": (False,)}),
    ]
