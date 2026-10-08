"""Lost DDL responses cannot toggle a published table back to old vectors."""

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from server.insights.store.provider_publish import publish_prepared_table
from server.insights.store.provider_staging import StagingNames


@pytest.fixture
def publication():
    names = StagingNames(uuid4(), "memory_embeddings")
    old, new = uuid4(), uuid4()
    before = [{"name": names.table, "uuid": old}, {"name": names.prepared, "uuid": new}]
    between = [{"name": names.backup, "uuid": old}, {"name": names.prepared, "uuid": new}]
    after = [{"name": names.backup, "uuid": old}, {"name": names.table, "uuid": new}]
    client = AsyncMock()
    return names, old, new, client, before, between, after


@pytest.mark.asyncio
async def test_publication_uses_two_forward_only_renames(publication):
    names, old, new, client, before, between, after = publication
    client.query_all.side_effect = [before, between, after]
    await publish_prepared_table(names, old, new, ch=client)
    statements = [call.args[0] for call in client.execute.await_args_list]
    assert statements == [
        f"RENAME TABLE `loom`.`{names.table}` TO `loom`.`{names.backup}`",
        f"RENAME TABLE `loom`.`{names.prepared}` TO `loom`.`{names.table}`",
    ]


@pytest.mark.asyncio
async def test_lost_first_response_resumes_second_rename(publication):
    names, old, new, client, before, between, after = publication
    client.query_all.side_effect = [before, between, after]
    client.execute.side_effect = [RuntimeError("response lost"), None]
    with pytest.raises(RuntimeError):
        await publish_prepared_table(names, old, new, ch=client)
    await publish_prepared_table(names, old, new, ch=client)
    assert client.execute.await_count == 2
    assert names.prepared in client.execute.await_args.args[0]


@pytest.mark.asyncio
async def test_lost_second_response_recognizes_publication_without_repeating_ddl(publication):
    names, old, new, client, before, between, after = publication
    client.query_all.side_effect = [before, between, after]
    client.execute.side_effect = [None, RuntimeError("response lost")]
    with pytest.raises(RuntimeError):
        await publish_prepared_table(names, old, new, ch=client)
    await publish_prepared_table(names, old, new, ch=client)
    assert client.execute.await_count == 2


@pytest.mark.asyncio
async def test_preexisting_unowned_backup_prevents_any_rename(publication):
    names, old, new, client, before, _, _ = publication
    client.query_all.return_value = [*before, {"name": names.backup, "uuid": uuid4()}]
    with pytest.raises(ValueError, match="durable plan"):
        await publish_prepared_table(names, old, new, ch=client)
    client.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_identity_evidence_is_not_permission_to_publish(publication):
    names, old, new, client, _, _, _ = publication
    client.query_all.return_value = []
    with pytest.raises(ValueError, match="durable plan"):
        await publish_prepared_table(names, old, new, ch=client)
    client.execute.assert_not_awaited()
