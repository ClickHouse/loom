"""Feed content publications advance beyond every persisted record version."""

import json
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from server.tracker.app import github_feed as app
from server.tracker.store import github_feed as store
from tests.unit.ch_fake import RecordingCH

NOW = datetime(2025, 1, 2, microsecond=123000, tzinfo=UTC)
NOW_MS = int(NOW.timestamp()) * 1000 + NOW.microsecond // 1000
IDENTITY = {name: "team" if name == "org" else "-" for name in store.IDENTITY}


@pytest.mark.asyncio
@pytest.mark.parametrize("item_table", ["feed_issues", "feed_prs"])
async def test_item_then_comment_publication_advances_with_an_unchanged_record_clock(item_table):
    ch = RecordingCH()

    def publication_clock(sql, params):
        versions = [
            int(row["version"].timestamp()) * 1000 + row["version"].microsecond // 1000
            for call in ch.calls
            if call.kind == "insert"
            for row in call.rows
        ]
        return [{"now_ms": NOW_MS, "prior_ms": max(versions, default=0)}]

    ch.on("AS now_ms", publication_clock)
    await store.put_items(ch, item_table, [{"number": 1}], IDENTITY, "widgets/repo")
    await store.put_items(ch, "feed_comments", [{"comment_id": 1}], IDENTITY, "widgets/repo")
    item = ch.inserted(item_table)[0]["version"]
    comment = ch.inserted("feed_comments")[0]["version"]
    assert item == NOW
    assert comment == NOW + timedelta(milliseconds=1)
    assert item.microsecond % 1000 == comment.microsecond % 1000 == 0
    assert all(call.params == {"org": "team"} for call in ch.calls if call.kind == "query_one")


@pytest.mark.asyncio
async def test_publication_advances_beyond_persisted_versions_ahead_of_the_record_clock():
    ch = RecordingCH().on("AS now_ms", [{"now_ms": NOW_MS, "prior_ms": NOW_MS + 7}])
    await store.put_items(ch, "feed_comments", [{"comment_id": 1}], IDENTITY, "widgets/repo")
    assert ch.inserted("feed_comments")[0]["version"] == NOW + timedelta(milliseconds=8)


@pytest.mark.asyncio
async def test_other_repository_publication_exceeds_the_records_logical_watermark():
    ch = RecordingCH()
    await ch.insert(
        "feed_issues",
        [IDENTITY | {"repository": "widgets/first", "version": NOW + timedelta(milliseconds=7)}],
    )

    def publication_clock(sql, params):
        versions = [
            int(row["version"].timestamp()) * 1000 + row["version"].microsecond // 1000
            for call in ch.calls
            if call.kind == "insert"
            for row in call.rows
            if row["org"] == params["org"]
            and ("repo" not in params or row["repository"] == params["repo"])
        ]
        return [{"now_ms": NOW_MS, "prior_ms": max(versions, default=0)}]

    ch.on("AS now_ms", publication_clock)
    await store.put_items(ch, "feed_prs", [{"number": 1}], IDENTITY, "widgets/second")
    assert ch.inserted("feed_prs")[0]["version"] == NOW + timedelta(milliseconds=8)


@pytest.mark.asyncio
@pytest.mark.parametrize("prior_offset_ms", [10_000, 0, -10_000])
@pytest.mark.parametrize("change", ["hide", "configure", "cursor"])
async def test_repository_controls_replace_persisted_rows_despite_clock_rollback(
    monkeypatch, prior_offset_ms, change
):
    owner = "00000000-0000-0000-0000-000000000001"
    target = "00000000-0000-0000-0000-000000000002"
    prior = NOW + timedelta(milliseconds=prior_offset_ms)
    previous_cursor = (NOW - timedelta(days=1)).isoformat()
    original = app.identity("team") | dict(
        repository="widgets/repo",
        repository_id=7,
        owner_uuid=owner,
        token_enc="sealed-secret",
        is_private=0,
        available=1,
        visible_until=NOW + timedelta(seconds=900),
        cursors=json.dumps({"items": previous_cursor}),
        max_pages=2,
        revision=uuid.uuid4(),
        version=prior,
    )
    ch = RecordingCH()
    await ch.insert(
        "feed_repositories",
        [
            original,
            original | {"repository": "widgets/other", "version": NOW + timedelta(hours=1)},
            original | {"org": "other-team", "version": NOW + timedelta(hours=2)},
        ],
    )

    def persisted(params):
        return [
            row
            for row in ch.inserted("feed_repositories")
            if row["org"] == params["org"] and row["repository"] == params["repo"]
        ]

    def publication_clock(sql, params):
        assert "WHERE org = {org:String} AND repository = {repo:String}" in sql
        assert "LIMIT 1 SETTINGS select_sequential_consistency = 1" in sql
        latest = max(row.get("version", NOW) for row in persisted(params))
        prior_ms = int(latest.timestamp()) * 1000 + latest.microsecond // 1000
        return [{"now_ms": NOW_MS, "prior_ms": prior_ms}]

    def final_repository(sql, params):
        # Model ReplacingMergeTree's maximum-version winner, including the
        # server-clock default when the writer omits an explicit version.
        return [max(persisted(params), key=lambda row: row.get("version", NOW))]

    ch.on("AS now_ms", publication_clock)
    ch.on("FROM feed_repositories FINAL", final_repository)
    ch.on("AS ms", [{"ms": NOW_MS}])
    ch.on("FROM feed_owner", [{"org": "team", "owner_uuid": owner, "schema_version": 1}])
    ch.on("FROM system.databases", [{"id": target}])
    keys = []

    @asynccontextmanager
    async def fence(key, **kwargs):
        keys.append(key)
        yield lambda uncertain: None

    monkeypatch.setattr(app, "fenced", fence)
    monkeypatch.setattr(app, "resealed", lambda *args: None)
    monkeypatch.setattr(app, "unseal", lambda *args: "read-only-secret")
    monkeypatch.setattr(app, "seal", lambda *args: "rotated-sealed")
    metadata = AsyncMock(return_value={"id": 7, "full_name": "widgets/repo", "private": False})
    monkeypatch.setattr(app.provider, "metadata", metadata)
    if change == "hide":
        metadata.side_effect = app.provider.FeedProviderError("http_404")
        with pytest.raises(app.provider.FeedProviderError):
            await app.sync(
                ch,
                org="team",
                owner_uuid=owner,
                repo="widgets/repo",
                allowed_repos={"widgets/repo"},
            )
    elif change == "configure":
        await app.configure(
            ch,
            org="team",
            owner_uuid=owner,
            repo="widgets/repo",
            token="rotated-read-only-secret",
            allowed_repos={"widgets/repo"},
            max_pages=7,
        )
    else:
        assert await app._publish(
            ch, original, owner, target, "items", NOW, {}, app._Observation(7, True, NOW)
        )
    written = ch.inserted("feed_repositories")[3:]
    expected_version = max(NOW, prior + timedelta(milliseconds=1))
    assert written[0]["version"] == expected_version
    assert all(row["version"] > prior and row["version"].microsecond % 1000 == 0 for row in written)
    assert all(
        before["version"] < after["version"]
        for before, after in zip(written, written[1:], strict=False)
    )
    current = await store.repository(ch, "team", "widgets/repo")
    assert current == written[-1]
    assert current["revision"] != original["revision"]
    assert keys == [f"tracker-feed.writer/{target}"]
    if change == "hide":
        assert current["available"] == 0 and current["visible_until"] == app.domain.EPOCH
        assert current["token_enc"] == original["token_enc"]
        assert current["cursors"] == original["cursors"]
    elif change == "configure":
        assert current["token_enc"] == "rotated-sealed" and current["max_pages"] == 7
        assert current["cursors"] == original["cursors"]
    else:
        assert current["is_private"] == 1 and current["available"] == 1
        assert json.loads(current["cursors"])["items"] == NOW.isoformat()
        assert current["token_enc"] == original["token_enc"]
