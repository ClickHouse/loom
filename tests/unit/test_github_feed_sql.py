"""Feed publication timestamps keep late comments in incremental tracker reads."""

import json
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from server.tracker.app import github_feed as app
from server.tracker.domain import github_feed as domain
from server.tracker.store import github_feed as store
from server.tracker.store.source.base import SourceBase
from server.tracker.store.source.embed import EmbedReads
from tests.unit.ch_fake import RecordingCH
from tests.unit.clickhouse_local import CLICKHOUSE, run

pytestmark = pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")


def _record():
    return [
        "USE loom",
        *store.statements(),
        "INSERT INTO feed_owner (org, owner_uuid) "
        "VALUES ('team', '00000000-0000-0000-0000-000000000001')",
        "INSERT INTO feed_repositories "
        "(org, repository, owner_uuid, available, visible_until) "
        "VALUES ('team', 'widgets/repo', '00000000-0000-0000-0000-000000000001', "
        "1, '2100-01-01 00:00:00')",
    ]


def _read(setup, sql, params=None):
    rows = run(setup, sql, params)
    for row in rows:
        for column in ("created_at", "updated_at", "closed_at"):
            if isinstance(row.get(column), str):
                row[column] = datetime.fromisoformat(row[column])
    return rows


@pytest.mark.parametrize("view,raw", [("github_issues", "feed_issues"), ("github_prs", "feed_prs")])
@pytest.mark.parametrize("edit", [False, True])
@pytest.mark.asyncio
async def test_late_comment_publication_advances_the_saved_tracker_cursor(view, raw, edit):
    setup = [
        *_record(),
        f"INSERT INTO {raw} "
        "(org, repository, number, source_id, body, updated_at, version) "
        "VALUES ('team', 'widgets/repo', 1, 'widgets/repo#1', 'Item body', "
        "'2000-01-01 00:00:00', '2025-01-02 00:00:00.001')",
        f"CREATE TABLE saved_cursor ENGINE = Memory AS SELECT updated_at, source_id FROM {view}",
        "INSERT INTO feed_comments "
        "(org, repository, comment_id, item_number, body, updated_at, version) "
        "VALUES ('team', 'widgets/repo', 1, 1, 'Late reply', "
        "'1999-01-01 00:00:00', '2025-01-02 00:00:00.002')",
    ]
    if edit:
        setup += [
            "TRUNCATE TABLE saved_cursor",
            f"INSERT INTO saved_cursor SELECT updated_at, source_id FROM {view}",
            "INSERT INTO feed_comments "
            "(org, repository, comment_id, item_number, body, updated_at, version) "
            "VALUES ('team', 'widgets/repo', 1, 1, 'Edited reply', "
            "'1999-01-01 00:00:01', '2025-01-02 00:00:00.003')",
        ]
    rows = run(
        setup,
        f"SELECT body, toString(updated_at) AS published FROM {view} "
        "WHERE (updated_at, source_id) > (SELECT updated_at, source_id FROM saved_cursor)",
    )
    assert rows == [
        {
            "body": "Item body\n" + ("Edited reply" if edit else "Late reply"),
            "published": "2025-01-02 00:00:00.00" + ("3" if edit else "2"),
        }
    ]

    class Source(EmbedReads):
        def __init__(self):
            self.db = "loom"

        def repos(self):
            return ["widgets/repo"]

        def _privacy(self):
            return "is_private = 0 AND "

        async def _rows(self, name, sql, params, **kwargs):
            return _read(setup, sql, params)

    source = Source()
    cursor = datetime(2025, 1, 2, microsecond=2000 if edit else 1000, tzinfo=UTC)
    published = cursor.replace(microsecond=3000 if edit else 2000)
    kind = "issue" if view == "github_issues" else "pr"
    for since_id in (None, "widgets/repo#1"):
        options = dict(kind=kind, closed_days=365, body_chars=600, since_id=since_id)
        changed = await source.embed_items("team", "feed", since=cursor, **options)
        assert len(changed) == 1 and changed[0]["body"] == rows[0]["body"]
        assert changed[0]["updated_at"] == published.replace(tzinfo=None)
        assert await source.embed_items("team", "feed", since=published, **options) == []


def test_millisecond_reader_resumes_after_two_thousand_rows_from_one_publication():
    setup = [
        *_record(),
        "INSERT INTO feed_issues "
        "(org, repository, number, source_id, updated_at, version) "
        "SELECT 'team', 'widgets/repo', number + 1, "
        "concat('widgets/repo#', toString(number + 1)), "
        "toDateTime64('2000-01-01 00:00:00', 3), "
        "toDateTime64('2025-01-02 00:00:00.123456', 6) FROM numbers(2001)",
    ]
    select = "SELECT source_id, updated_at FROM github_issues "
    order = " ORDER BY updated_at, source_id LIMIT 2000"
    first = _read(setup, select + order)
    assert len(first) == 2000
    cursor = first[-1]
    second = _read(
        setup,
        select + "WHERE (updated_at, source_id) > ({since:DateTime64(3)}, {id:String})" + order,
        {"since": cursor["updated_at"], "id": cursor["source_id"]},
    )
    assert len(second) == 1
    assert second[0]["source_id"] not in {row["source_id"] for row in first}
    assert cursor["updated_at"] == datetime(2025, 1, 2, microsecond=123000)


def _insert_recorded(table, row):
    def value(item):
        if isinstance(item, datetime):
            return item.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")
        return str(item)

    columns = ", ".join(row)
    data = json.dumps(row, default=value)
    return f"INSERT INTO {table} ({columns}) SELECT {columns} FROM format(JSONEachRow, $${data}$$)"


@pytest.mark.asyncio
@pytest.mark.parametrize("view,raw", [("github_issues", "feed_issues"), ("github_prs", "feed_prs")])
@pytest.mark.parametrize("publication", ["item", "comment"])
@pytest.mark.parametrize("metadata_private,published_private", [(0, 1), (0, 0), (1, 0), (1, 1)])
async def test_contract_reader_keeps_publication_privacy_when_repository_metadata_lags(
    monkeypatch, view, raw, publication, metadata_private, published_private
):
    owner = "00000000-0000-0000-0000-000000000001"
    now = datetime(2025, 1, 2, tzinfo=UTC)
    current = app.identity("team") | dict(
        repository="widgets/repo",
        repository_id=7,
        owner_uuid=uuid.UUID(owner),
        token_enc="",
        is_private=metadata_private,
        available=1,
        visible_until=datetime(2100, 1, 1, tzinfo=UTC),
        cursors="{}",
        max_pages=2,
        revision=uuid.UUID(int=3),
    )
    ch = (
        RecordingCH()
        .on("FROM feed_owner", [{"org": "team", "owner_uuid": owner, "schema_version": 1}])
        .on("AS now_ms", [{"now_ms": int(now.timestamp()) * 1000, "prior_ms": 0}])
        .on("FROM feed_repositories", [current])
    )

    @asynccontextmanager
    async def fence(*args, **kwargs):
        yield lambda uncertain: None

    monkeypatch.setattr(app, "fenced", fence)
    monkeypatch.setattr(app, "resealed", lambda *args: None)
    item = domain.item_row(
        "widgets/repo",
        dict(number=1, body="Item body", created_at=now.isoformat(), updated_at=now.isoformat()),
        is_pr=view == "github_prs",
    )
    comment = domain.comment_row(
        "widgets/repo",
        dict(
            id=1,
            issue_url="https://api.github.com/repos/widgets/repo/issues/1",
            pull_request_url="https://api.github.com/repos/widgets/repo/pulls/1",
            body="New reply",
            created_at=now.isoformat(),
            updated_at=now.isoformat(),
        ),
        review=view == "github_prs",
    )
    table = raw if publication == "item" else "feed_comments"
    stream = (
        "items"
        if publication == "item"
        else ("review_comments" if view == "github_prs" else "comments")
    )
    assert await app._publish(
        ch,
        current,
        owner,
        "00000000-0000-0000-0000-000000000002",
        stream,
        now,
        {table: [item if publication == "item" else comment]},
        app._Observation(7, bool(published_private), now),
    )
    # One replica has learned the raw publication but still holds the previous
    # unexpired repository part. Keep actual writer rows and omit its metadata writes.
    setup = [*_record()[:-1], _insert_recorded("feed_repositories", current)]
    if publication == "comment":
        setup.append(
            _insert_recorded(raw, item | app.identity("team") | {"repository": "widgets/repo"})
        )
    setup.extend(_insert_recorded(table, row) for row in ch.inserted(table))
    ordinary = object.__new__(SourceBase)
    ordinary.cfg = SimpleNamespace(restricted=False)
    rows = run(
        setup,
        f"SELECT 'ordinary' AS reach, body, is_private, toString(id) AS id FROM {view} FINAL "
        f"WHERE {ordinary._privacy()}1 UNION ALL "
        f"SELECT 'restricted' AS reach, body, is_private, toString(id) AS id FROM {view} FINAL",
    )
    body = "Item body\n" + ("New reply" if publication == "comment" else "")
    effective_private = int(bool(metadata_private or published_private))
    kind = "github_issue" if view == "github_issues" else "github_pr"
    identity = f"{kind}{'_private' if effective_private else ''}:widgets/repo#1"
    expected = {
        "body": body,
        "is_private": effective_private,
        "id": str(uuid.uuid5(uuid.NAMESPACE_DNS, identity)),
    }
    assert [row for row in rows if row["reach"] == "ordinary"] == (
        [] if effective_private else [{"reach": "ordinary", **expected}]
    )
    assert [row for row in rows if row["reach"] == "restricted"] == [
        {"reach": "restricted", **expected}
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["hide", "configure", "cursor"])
async def test_repository_controls_replace_future_versions_in_the_record(change):
    clock_row = run([], "SELECT toUnixTimestamp64Milli(now64(3)) AS ms")[0]
    prior = domain.EPOCH + timedelta(days=1, milliseconds=int(clock_row["ms"]))
    current = app.identity("team") | dict(
        repository="widgets/repo",
        repository_id=7,
        owner_uuid=uuid.UUID("00000000-0000-0000-0000-000000000001"),
        token_enc="old-sealed",
        is_private=0,
        available=1,
        visible_until=prior,
        cursors="{}",
        max_pages=2,
        revision=uuid.UUID(int=3),
        version=prior,
    )
    setup = [
        *_record(),
        _insert_recorded("feed_repositories", current),
        "INSERT INTO feed_issues (org, repository, number, source_id, body) "
        "VALUES ('team', 'widgets/repo', 1, 'widgets/repo#1', 'Public item')",
    ]
    changes = {
        "hide": {"available": 0, "visible_until": domain.EPOCH},
        "configure": {"token_enc": "rotated-sealed", "max_pages": 7},
        "cursor": {"cursors": json.dumps({"items": "2025-01-02T00:00:00+00:00"})},
    }[change]
    ch = RecordingCH().on("AS now_ms", lambda sql, params: run(setup, sql, params))
    await store.put_repository(ch, current | changes | {"revision": uuid.UUID(int=4)})
    written = ch.inserted("feed_repositories")[0]
    assert written["version"] == prior + timedelta(milliseconds=1)
    rows = run(
        [*setup, _insert_recorded("feed_repositories", written)],
        "SELECT available, token_enc, cursors, max_pages, "
        "toUnixTimestamp64Milli(visible_until) AS visible_ms, "
        "toUnixTimestamp64Milli(version) AS version_ms, "
        "(SELECT count() FROM github_issues) AS visible_items "
        "FROM feed_repositories FINAL WHERE org = 'team' AND repository = 'widgets/repo'",
    )
    assert len(rows) == 1
    for column in ("available", "token_enc", "cursors"):
        assert rows[0][column] == written[column]
    assert int(rows[0]["max_pages"]) == written["max_pages"]
    expected_visible_ms = (
        int(written["visible_until"].timestamp()) * 1000
        + written["visible_until"].microsecond // 1000
    )
    prior_ms = int(prior.timestamp()) * 1000 + prior.microsecond // 1000
    assert int(rows[0]["visible_ms"]) == expected_visible_ms
    assert int(rows[0]["version_ms"]) == prior_ms + 1
    assert int(rows[0]["visible_items"]) == (0 if change == "hide" else 1)
