"""The owned record's views enforce current visibility and bounded comments."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.integration._resources import unique_org

pytestmark = pytest.mark.usefixtures("ch_target_required")


@pytest.fixture
async def feed_record(monkeypatch):
    from server.cli.tracker_feed import _validate_writer
    from server.core.ch import ExternalCHClient, get_client, set_query_tag
    from server.settings import settings
    from server.tracker.store import github_feed as store

    set_query_tag("test.github_feed")
    org = unique_org("github-feed")
    suffix = uuid.uuid4().hex
    database, writer, reader = "feed_" + suffix, "fw_" + suffix, "fr_" + suffix
    password = uuid.uuid4().hex
    password_env = "LOOM_FEED_TEST_PASSWORD_" + suffix
    monkeypatch.setenv(password_env, password)
    digest = hashlib.sha256(password.encode()).hexdigest()
    deployment = get_client()
    await deployment.connect()
    params = dict(db=database, writer=writer, reader=reader)
    record = None
    readonly = None
    try:
        await deployment.execute("CREATE DATABASE {db:Identifier}", params)
        for user in (writer, reader):
            # Password syntax needs a literal; this locally generated digest is hex only.
            await deployment.execute(
                f"CREATE USER `{user}` IDENTIFIED WITH sha256_hash BY '{digest}'",
                {"user": user},
            )
        await deployment.execute(
            f"GRANT CREATE TABLE, CREATE VIEW, SELECT, INSERT ON `{database}`.* TO `{writer}`",
            params,
        )
        for table in ("columns", "databases", "tables"):
            await deployment.execute(f"GRANT SELECT ON system.{table} TO `{writer}`")
        await deployment.execute(f"ALTER USER `{reader}` SETTINGS readonly = 2", params)
        await deployment.execute(f"GRANT SELECT ON system.columns TO `{reader}`", params)
        options = dict(
            host=settings.ch_host,
            port=settings.ch_port,
            secure=bool(settings.ch_secure),
            database=database,
            password=password,
        )
        record = ExternalCHClient(**options, username=writer)
        readonly = ExternalCHClient(**options, username=reader)
        await record.connect()
        await _validate_writer(record, database)
        yield SimpleNamespace(
            org=org,
            database=database,
            record=record,
            readonly=readonly,
            owner=await store.database_uuid(deployment),
            deployment=deployment,
            params=params,
            password_env=password_env,
        )
    finally:
        if readonly is not None:
            await readonly.disconnect()
        if record is not None:
            await record.disconnect()
        await deployment.execute("DROP DATABASE IF EXISTS {db:Identifier} SYNC", params)
        await deployment.execute(f"DROP USER IF EXISTS `{writer}`, `{reader}`", params)
        await deployment.disconnect()


async def test_feed_record_roundtrip_visibility_and_recent_comments(feed_record, monkeypatch):
    from server.tracker.app import github_feed as feed
    from server.tracker.domain import github_feed as domain
    from server.tracker.store import github_feed as store

    org, record, owner = feed_record.org, feed_record.record, feed_record.owner
    await feed.initialize(record, org, owner)
    await feed.initialize(record, org, owner)
    assert await store.owner_marker(record) == store.owner_tag(org, owner)
    monkeypatch.setattr(
        feed.provider,
        "metadata",
        AsyncMock(return_value=dict(id=7, full_name="widgets/repo", private=False)),
    )
    await feed.configure(
        record,
        org=org,
        owner_uuid=owner,
        repo="widgets/repo",
        token="read-only-fixture",
        allowed_repos={"widgets/repo"},
    )
    now = datetime.now(UTC).replace(microsecond=0)
    item = domain.item_row(
        "widgets/repo",
        dict(
            number=1,
            title="Saved",
            body="Issue body",
            created_at=now.isoformat(),
            updated_at=now.isoformat(),
        ),
        is_pr=False,
    )
    await store.put_items(record, "feed_issues", [item], feed.identity(org), "widgets/repo")
    raw = await record.query_one("SELECT * EXCEPT version FROM feed_issues FINAL LIMIT 1")
    raw = {
        key: value.replace(tzinfo=UTC)
        if isinstance(value, datetime) and value.tzinfo is None
        else value
        for key, value in raw.items()
    }
    expected = feed.identity(org) | {"repository": "widgets/repo"} | item
    assert raw == expected
    comments = [
        domain.comment_row(
            "widgets/repo",
            dict(
                id=n,
                issue_url="https://api.github.com/repos/widgets/repo/issues/1",
                body=f"comment-{n:03d}",
                created_at=now.isoformat(),
                updated_at=(now + timedelta(seconds=n)).isoformat(),
            ),
            review=False,
        )
        for n in range(1, 66)
    ]
    await store.put_items(record, "feed_comments", comments, feed.identity(org), "widgets/repo")
    public = await record.query_one("SELECT id, body, is_private FROM github_issues LIMIT 1")
    assert public["is_private"] == 0
    assert "comment-065" in public["body"] and "comment-001" not in public["body"]
    assert public["body"].count("comment-") == 64
    row = await store.repository(record, org, "WIDGETS/REPO")
    assert row and feed.unseal(domain.SECRET_PURPOSE, row["token_enc"]) == "read-only-fixture"
    await feed._put(record, row | {"is_private": 1})
    private = await record.query_one("SELECT id, is_private FROM github_issues LIMIT 1")
    assert private["is_private"] == 1 and private["id"] != public["id"]
    await feed._put(record, row | {"visible_until": now - timedelta(seconds=1)})
    assert await record.query_one("SELECT count() AS n FROM github_issues") == {"n": 0}
    with pytest.raises(domain.FeedInputError, match="another deployment or organization"):
        await feed.initialize(record, org, str(uuid.uuid4()))


async def test_feed_sync_replays_with_scoped_writer_and_readonly_views(feed_record, monkeypatch):
    from clickhouse_connect.driver.exceptions import DatabaseError

    from server.settings import settings
    from server.tracker.app import github_feed as feed
    from server.tracker.app.onboarding import probe_credential
    from server.tracker.domain.contract import diff
    from server.tracker.store import github_feed as store

    f = feed_record
    await feed.initialize(f.record, f.org, f.owner)
    for view in store.VIEWS:
        await f.deployment.execute(
            f"GRANT SELECT ON `{f.database}`.`{view}` TO `{f.params['reader']}`"
        )
    await f.readonly.connect()
    monkeypatch.setattr(settings, "tracker_self_service_allow_private_hosts", True)
    probe = await probe_credential(
        host=settings.ch_host,
        port=settings.ch_port,
        secure=bool(settings.ch_secure),
        database=f.database,
        user=f.params["reader"],
        password=os.environ[f.password_env],
    )
    assert probe.grants.read_only and not probe.unreadable
    report = diff("feed", f.database, probe.described)
    assert report.ok, report
    stamp = datetime.now(UTC).replace(microsecond=0).isoformat()
    issue = dict(number=1, title="Issue", body="source issue", created_at=stamp, updated_at=stamp)
    pr = dict(number=2, title="PR", body="source pr", created_at=stamp, updated_at=stamp)
    comment = dict(
        id=1,
        issue_url="https://api.github.com/repos/widgets/repo/issues/1",
        body="issue reply",
        created_at=stamp,
        updated_at=stamp,
    )
    review = dict(
        id=2,
        pull_request_url="https://api.github.com/repos/widgets/repo/pulls/2",
        body="inline review",
        created_at=stamp,
        updated_at=stamp,
    )
    batches = {
        "items": [issue, pr | {"pull_request": {}}],
        "comments": [comment],
        "review_comments": [review],
    }

    async def pages(repo, token, stream, since, **kwargs):
        return feed.provider.Batch(batches[stream], True)

    monkeypatch.setattr(
        feed.provider,
        "metadata",
        AsyncMock(return_value=dict(id=7, full_name="widgets/repo", private=False)),
    )
    monkeypatch.setattr(feed.provider, "pages", pages)
    monkeypatch.setattr(feed.provider, "pull_request", AsyncMock(return_value=pr))
    options = dict(
        org=f.org, owner_uuid=f.owner, repo="widgets/repo", allowed_repos={"widgets/repo"}
    )
    await feed.configure(f.record, **options, token="read-only-fixture")
    assert await feed.sync(f.record, **options) == dict(items=2, comments=1, review_comments=1)
    first = await store.repository(f.record, f.org, "widgets/repo")
    assert first and set(json.loads(first["cursors"])) == set(batches)
    assert await feed.sync(f.record, **options) == dict(items=2, comments=1, review_comments=1)
    replay = await store.repository(f.record, f.org, "widgets/repo")
    assert replay and replay["cursors"] == first["cursors"]
    assert (await f.readonly.query_one("SELECT body FROM github_issues LIMIT 1"))[
        "body"
    ] == "source issue\nissue reply"
    assert (
        "inline review"
        in (await f.readonly.query_one("SELECT body FROM github_prs LIMIT 1"))["body"]
    )
    assert await f.record.query_one("SELECT count() AS n FROM feed_comments FINAL") == {"n": 2}
    with pytest.raises(DatabaseError, match="ACCESS_DENIED|Not enough privileges"):
        await f.readonly.query_one("SELECT token_enc FROM feed_repositories LIMIT 1")
    with pytest.raises(DatabaseError, match="ACCESS_DENIED|Not enough privileges"):
        await f.record.query_one("SELECT count() FROM loom.memory")
    monkeypatch.setattr(
        feed.provider,
        "metadata",
        AsyncMock(side_effect=feed.provider.FeedProviderError("http_404")),
    )
    with pytest.raises(feed.provider.FeedProviderError):
        await feed.sync(f.record, **options)
    assert await f.readonly.query_one("SELECT count() AS n FROM github_issues") == {"n": 0}


async def test_cli_initializes_the_owned_record_using_explicit_writer(feed_record):
    from server.settings import settings
    from server.tracker.store import github_feed as store

    f = feed_record
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "server.cli",
        "tracker-feed",
        "init",
        "--org",
        f.org,
        "--host",
        settings.ch_host,
        "--port",
        str(settings.ch_port),
        "--database",
        f.database,
        "--insecure",
        "--writer-user",
        f.params["writer"],
        "--writer-password-env",
        f.password_env,
        env=dict(
            os.environ, LOOM_TRACKER_ORG=f.org, LOOM_TRACKER_SELF_SERVICE_ALLOW_PRIVATE_HOSTS="true"
        ),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), 30)
        assert process.returncode == 0, stderr.decode()
        assert json.loads(stdout) == {"initialized": True}
        owner = await store.owner(f.record)
        assert owner and owner["org"] == f.org and str(owner["owner_uuid"]) == f.owner
    finally:
        if process.returncode is None:
            process.terminate()
            await process.wait()
