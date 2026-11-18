"""Copied records publish only under the current feed configuration."""

import asyncio
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from server.tracker.app import github_feed as app
from server.tracker.domain.github_feed import FeedInputError

OWNER = "00000000-0000-0000-0000-000000000001"
TARGET = "00000000-0000-0000-0000-000000000002"
NOW = datetime(2025, 1, 2, tzinfo=UTC)


@pytest.fixture
def env(monkeypatch):
    state = SimpleNamespace(
        row=app.identity("team")
        | dict(
            repository="widgets/repo",
            repository_id=7,
            owner_uuid=OWNER,
            token_enc="sealed-secret",
            is_private=0,
            available=1,
            visible_until=NOW + timedelta(seconds=900),
            cursors="{}",
            max_pages=5,
            revision=uuid.uuid4(),
        ),
        writes=[],
        marks=[],
        records=[],
        keys=[],
    )

    async def put(ch, row):
        state.row = dict(row)
        state.writes.append(dict(row))

    async def records(ch, table, rows, identity, repo):
        state.records.append((table, rows, identity, repo, state.row["is_private"]))

    @asynccontextmanager
    async def fence(name, **kwargs):
        state.keys.append(name)
        yield state.marks.append

    monkeypatch.setattr(app, "fenced", fence)
    monkeypatch.setattr(app.store, "clock", AsyncMock(return_value=NOW))
    monkeypatch.setattr(app, "resealed", lambda purpose, token: None)
    monkeypatch.setattr(app, "unseal", lambda purpose, token: "read-only-secret")
    monkeypatch.setattr(
        app.store,
        "owner",
        AsyncMock(
            return_value=dict(
                org="team",
                owner_uuid=OWNER,
                schema_version=1,
            )
        ),
    )
    monkeypatch.setattr(app.store, "database_uuid", AsyncMock(return_value=TARGET))
    monkeypatch.setattr(
        app.store, "repository", AsyncMock(side_effect=lambda *args: dict(state.row))
    )
    monkeypatch.setattr(app.store, "put_repository", put)
    monkeypatch.setattr(app.store, "put_items", records)
    monkeypatch.setattr(
        app.provider,
        "metadata",
        AsyncMock(
            return_value=dict(
                id=7,
                full_name="widgets/repo",
                private=False,
            )
        ),
    )
    monkeypatch.setattr(app.provider, "pages", AsyncMock(return_value=app.provider.Batch([], True)))
    return state


async def sync():
    return await app.sync(
        object(), org="team", owner_uuid=OWNER, repo="widgets/repo", allowed_repos={"widgets/repo"}
    )


@pytest.mark.asyncio
async def test_empty_scan_checks_each_stream_without_advancing_cursor(env):
    assert await sync() == dict(items=0, comments=0, review_comments=0)
    assert env.row["cursors"] == "{}" and env.row["available"] == 1
    assert env.keys == [f"tracker-feed.writer/{TARGET}"] * 3
    assert not env.records and env.marks[-1] is False


@pytest.mark.asyncio
async def test_failed_visibility_check_hides_previously_public_content(env, monkeypatch):
    monkeypatch.setattr(
        app.provider, "metadata", AsyncMock(side_effect=app.provider.FeedProviderError("http_404"))
    )
    with pytest.raises(app.provider.FeedProviderError):
        await sync()
    assert env.row["available"] == 0 and env.row["visible_until"] == app.domain.EPOCH
    assert not env.records and env.row["cursors"] == "{}"


@pytest.mark.asyncio
async def test_different_repository_id_hides_previous_copy(env, monkeypatch):
    monkeypatch.setattr(
        app.provider,
        "metadata",
        AsyncMock(
            return_value=dict(
                id=8,
                full_name="widgets/repo",
                private=False,
            )
        ),
    )
    with pytest.raises(FeedInputError, match="identity changed"):
        await sync()
    assert env.row["available"] == 0 and not env.records


@pytest.mark.asyncio
async def test_credential_rotation_during_fetch_cannot_publish_stale_content(env, monkeypatch):
    async def fetch(*args, **kwargs):
        env.row["revision"] = uuid.uuid4()
        env.row["token_enc"] = "replacement-secret"
        return app.provider.Batch([], True)

    monkeypatch.setattr(app.provider, "pages", fetch)
    with pytest.raises(FeedInputError, match="changed while fetching"):
        await sync()
    assert env.row["token_enc"] == "replacement-secret" and not env.writes


@pytest.mark.asyncio
async def test_private_visibility_publishes_before_content(env, monkeypatch):
    monkeypatch.setattr(
        app.provider,
        "metadata",
        AsyncMock(
            return_value=dict(
                id=7,
                full_name="widgets/repo",
                private=True,
            )
        ),
    )
    data = dict(number=1, title="private", created_at=NOW.isoformat(), updated_at=NOW.isoformat())
    monkeypatch.setattr(
        app.provider,
        "pages",
        AsyncMock(
            side_effect=[
                app.provider.Batch([data], True),
                app.provider.Batch([], True),
                app.provider.Batch([], True),
            ]
        ),
    )
    await sync()
    assert env.records[0][-1] == 1
    assert env.records[0][2]["actor"] == "process:tracker-github-feed"
    assert env.records[0][2]["source_class"] == "imported"
    assert '"items"' in env.row["cursors"]


@pytest.mark.asyncio
async def test_uncertain_content_write_keeps_send_claim_and_checkpoint(env, monkeypatch):
    async def fail(*args):
        raise TimeoutError("insert acknowledgement lost")

    monkeypatch.setattr(app.store, "put_items", fail)
    data = dict(number=1, created_at=NOW.isoformat(), updated_at=NOW.isoformat())
    monkeypatch.setattr(
        app.provider, "pages", AsyncMock(return_value=app.provider.Batch([data], True))
    )
    with pytest.raises(TimeoutError):
        await sync()
    assert env.marks[-1] is True and env.row["cursors"] == "{}"


@pytest.mark.asyncio
async def test_another_deployment_refuses_before_provider_read(env, monkeypatch):
    monkeypatch.setattr(
        app.store,
        "owner",
        AsyncMock(
            return_value=dict(
                org="team",
                owner_uuid=TARGET,
                schema_version=1,
            )
        ),
    )
    with pytest.raises(FeedInputError, match="another deployment"):
        await sync()
    app.provider.metadata.assert_not_awaited()


@pytest.mark.asyncio
async def test_old_configure_observation_cannot_overwrite_newer_private_state(env, monkeypatch):
    async def metadata(*args):
        env.row["revision"] = uuid.uuid4()
        env.row["is_private"] = 1
        return dict(id=7, full_name="widgets/repo", private=False)

    monkeypatch.setattr(app.provider, "metadata", metadata)
    with pytest.raises(FeedInputError, match="changed while configuring"):
        await app.configure(
            object(),
            org="team",
            owner_uuid=OWNER,
            repo="widgets/repo",
            token="read-only-secret",
            allowed_repos={"widgets/repo"},
        )
    assert env.row["is_private"] == 1 and not env.writes


@pytest.mark.asyncio
async def test_private_observation_with_capacity_failure_hides_prior_public_record(
    env, monkeypatch
):
    env.row["cursors"] = '{"items": "2025-01-02T00:00:00+00:00"}'
    monkeypatch.setattr(
        app.provider,
        "metadata",
        AsyncMock(
            return_value=dict(
                id=7,
                full_name="widgets/repo",
                private=True,
            )
        ),
    )
    data = [
        dict(number=n + 1, created_at=NOW.isoformat(), updated_at=NOW.isoformat())
        for n in range(100)
    ]
    monkeypatch.setattr(
        app.provider, "pages", AsyncMock(return_value=app.provider.Batch(data, False))
    )
    with pytest.raises(app.domain.FeedCapacityError):
        await sync()
    assert env.row["available"] == 0 and not env.records


@pytest.mark.asyncio
async def test_cancelled_scan_invalidates_visibility_without_advancing_cursor(env, monkeypatch):
    monkeypatch.setattr(app.provider, "pages", AsyncMock(side_effect=asyncio.CancelledError))
    with pytest.raises(asyncio.CancelledError):
        await sync()
    assert env.row["available"] == 0 and env.row["cursors"] == "{}"


@pytest.mark.asyncio
async def test_initializer_resumes_an_interrupted_owner_insert(env, monkeypatch):
    ch = SimpleNamespace(execute=AsyncMock(), insert=AsyncMock())
    monkeypatch.setattr(app.store, "objects", AsyncMock(return_value=[{"name": "feed_owner"}]))
    monkeypatch.setattr(
        app.store, "owner_marker", AsyncMock(return_value=app.store.owner_tag("team", OWNER))
    )
    monkeypatch.setattr(
        app.store,
        "owner",
        AsyncMock(side_effect=[None, dict(org="team", owner_uuid=OWNER, schema_version=1)]),
    )
    await app.initialize(ch, "team", OWNER)
    assert ch.insert.await_count == 1
    assert ch.insert.call_args.args[0] == "feed_owner"
    assert ch.execute.await_count == len(app.store.statements())
    assert env.marks[-1] is False


@pytest.mark.asyncio
async def test_initializer_refuses_another_atomic_owner_claim(env, monkeypatch):
    ch = SimpleNamespace(execute=AsyncMock(), insert=AsyncMock())
    monkeypatch.setattr(app.store, "objects", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        app.store, "owner_marker", AsyncMock(return_value=app.store.owner_tag("other", TARGET))
    )
    with pytest.raises(FeedInputError, match="another deployment or organization"):
        await app.initialize(ch, "team", OWNER)
    ch.insert.assert_not_awaited()
    assert ch.execute.await_count == 1
    assert env.marks[-1] is True


@pytest.mark.asyncio
async def test_successful_publish_reseals_previous_root_credentials(env, monkeypatch):
    monkeypatch.setattr(
        app, "resealed", lambda purpose, token: "current-root" if token == "sealed-secret" else None
    )
    await sync()
    assert env.writes and all(row["token_enc"] == "current-root" for row in env.writes)


@pytest.mark.asyncio
async def test_visibility_expiry_uses_receiving_clock_before_provider_wait(env, monkeypatch):
    monkeypatch.setattr(app.store, "clock", AsyncMock(return_value=NOW), raising=False)
    await sync()
    assert env.row["visible_until"] == NOW + timedelta(seconds=app.VISIBILITY_TTL_S)


@pytest.mark.asyncio
async def test_configure_reused_repository_name_hides_previous_identity(env, monkeypatch):
    monkeypatch.setattr(
        app.provider,
        "metadata",
        AsyncMock(return_value=dict(id=8, full_name="widgets/repo", private=False)),
    )
    with pytest.raises(FeedInputError, match="identity changed"):
        await app.configure(
            object(),
            org="team",
            owner_uuid=OWNER,
            repo="widgets/repo",
            token="read-only-secret",
            allowed_repos={"widgets/repo"},
        )
    assert env.row["available"] == 0
    assert env.row["visible_until"] == app.domain.EPOCH


@pytest.mark.asyncio
async def test_name_reuse_hides_current_row_even_if_snapshot_revision_changed(env, monkeypatch):
    async def metadata(*args):
        env.row["revision"] = uuid.uuid4()
        env.row["token_enc"] = "rotated-secret"
        return dict(id=8, full_name="widgets/repo", private=False)

    monkeypatch.setattr(app.provider, "metadata", metadata)
    with pytest.raises(FeedInputError, match="identity changed"):
        await app.configure(
            object(),
            org="team",
            owner_uuid=OWNER,
            repo="widgets/repo",
            token="read-only-secret",
            allowed_repos={"widgets/repo"},
        )
    assert env.row["available"] == 0 and env.row["token_enc"] == "rotated-secret"


@pytest.mark.asyncio
async def test_sync_name_reuse_hides_current_row_after_concurrent_publication(env, monkeypatch):
    async def metadata(*args):
        env.row["revision"] = uuid.uuid4()
        env.row["token_enc"] = "rotated-secret"
        return dict(id=8, full_name="widgets/repo", private=False)

    monkeypatch.setattr(app.provider, "metadata", metadata)
    with pytest.raises(FeedInputError, match="identity changed"):
        await sync()
    assert env.row["available"] == 0 and env.row["token_enc"] == "rotated-secret"


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["fetch_failure", "publication", "configure"])
async def test_private_observation_tightens_access_after_revision_change(env, monkeypatch, path):
    async def metadata(*args):
        env.row["revision"] = uuid.uuid4()
        env.row["token_enc"] = "rotated-secret"
        return dict(id=7, full_name="widgets/repo", private=True)

    monkeypatch.setattr(app.provider, "metadata", metadata)
    if path == "fetch_failure":
        monkeypatch.setattr(
            app.provider, "pages", AsyncMock(side_effect=app.provider.FeedProviderError("http_503"))
        )
    with pytest.raises((FeedInputError, app.provider.FeedProviderError)):
        if path == "configure":
            await app.configure(
                object(),
                org="team",
                owner_uuid=OWNER,
                repo="widgets/repo",
                token="read-only-secret",
                allowed_repos={"widgets/repo"},
            )
        else:
            await sync()
    assert env.row["available"] == 0 and env.row["token_enc"] == "rotated-secret"


@pytest.mark.asyncio
async def test_failed_old_credential_cannot_hide_rotated_configuration(env, monkeypatch):
    async def metadata(*args):
        env.row["revision"] = uuid.uuid4()
        env.row["token_enc"] = "rotated-secret"
        raise app.provider.FeedProviderError("http_401")

    monkeypatch.setattr(app.provider, "metadata", metadata)
    with pytest.raises(app.provider.FeedProviderError):
        await sync()
    assert env.row["available"] == 1 and env.row["token_enc"] == "rotated-secret"


@pytest.mark.asyncio
@pytest.mark.parametrize("registered", ["widgets/repo", "Widgets/Repo"])
async def test_configure_preserves_registered_repository_spelling(env, monkeypatch, registered):
    env.row["repository"] = registered
    monkeypatch.setattr(app, "seal", lambda *args: "sealed-secret")
    monkeypatch.setattr(
        app.provider,
        "metadata",
        AsyncMock(return_value=dict(id=7, full_name="WIDGETS/REPO", private=False)),
    )
    await app.configure(
        object(),
        org="team",
        owner_uuid=OWNER,
        repo="widgets/REPO",
        token="read-only-secret",
        allowed_repos={registered},
    )
    assert env.row["repository"] == registered
    data = dict(number=1, created_at=NOW.isoformat(), updated_at=NOW.isoformat())
    monkeypatch.setattr(
        app.provider,
        "pages",
        AsyncMock(
            side_effect=[
                app.provider.Batch([data], True),
                app.provider.Batch([], True),
                app.provider.Batch([], True),
            ]
        ),
    )
    await sync()
    assert env.records[0][3] == registered
    assert env.records[0][1][0]["source_id"] == registered + "#1"


@pytest.mark.asyncio
async def test_configure_refuses_one_page_before_any_provider_request(env, monkeypatch):
    monkeypatch.setattr(app, "seal", lambda *args: "sealed-secret")
    with pytest.raises(FeedInputError, match="2..100 max-pages"):
        await app.configure(
            object(),
            org="team",
            owner_uuid=OWNER,
            repo="widgets/repo",
            token="read-only-secret",
            allowed_repos={"widgets/repo"},
            max_pages=1,
        )
    app.provider.metadata.assert_not_awaited()
    assert not env.writes


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "before_private,after_private", [(True, False), (False, False), (True, True)]
)
async def test_visibility_transition_replays_streams_only_when_repository_becomes_public(
    env, monkeypatch, before_private, after_private
):
    cursors = {stream: NOW.isoformat() for stream in app.provider.STREAMS}
    env.row.update(is_private=int(before_private), cursors=app.json.dumps(cursors))
    monkeypatch.setattr(
        app.provider,
        "metadata",
        AsyncMock(return_value=dict(id=7, full_name="widgets/repo", private=after_private)),
    )
    await sync()
    replay = before_private and not after_private
    assert [call.args[3] for call in app.provider.pages.await_args_list] == [
        None if replay else NOW
    ] * len(app.provider.STREAMS)
    assert app.json.loads(env.row["cursors"]) == ({} if replay else cursors)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "before_private,after_private", [(True, False), (False, False), (True, True)]
)
async def test_configure_replays_content_only_when_repository_becomes_public(
    env, monkeypatch, before_private, after_private
):
    cursors = {stream: NOW.isoformat() for stream in app.provider.STREAMS}
    env.row.update(is_private=int(before_private), cursors=app.json.dumps(cursors))
    monkeypatch.setattr(app, "seal", lambda *args: "sealed-secret")
    monkeypatch.setattr(
        app.provider,
        "metadata",
        AsyncMock(return_value=dict(id=7, full_name="widgets/repo", private=after_private)),
    )
    await app.configure(
        object(),
        org="team",
        owner_uuid=OWNER,
        repo="widgets/repo",
        token="read-only-secret",
        allowed_repos={"widgets/repo"},
    )
    replay = before_private and not after_private
    assert app.json.loads(env.row["cursors"]) == ({} if replay else cursors)


@pytest.mark.asyncio
async def test_interrupted_public_replay_retains_reset_cursors(env, monkeypatch):
    env.row.update(
        is_private=1,
        cursors=app.json.dumps({stream: NOW.isoformat() for stream in app.provider.STREAMS}),
    )
    data = dict(number=1, created_at=NOW.isoformat(), updated_at=NOW.isoformat())
    monkeypatch.setattr(
        app.provider, "pages", AsyncMock(return_value=app.provider.Batch([data], True))
    )
    monkeypatch.setattr(app.store, "put_items", AsyncMock(side_effect=TimeoutError("uncertain")))
    with pytest.raises(TimeoutError):
        await sync()
    assert env.row["is_private"] == 0
    assert app.json.loads(env.row["cursors"]) == {}
    assert app.provider.pages.await_args.args[3] is None
