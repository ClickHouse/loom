"""Canonical retirement fixtures that do not express permanent content refusal."""

from server.core.ch import get_client
from server.identity.domain.read_scope import WHOLE_NAMESPACE
from server.memory.api.models_crud import MemoryDeleteInput, MemoryDeleteOutput
from server.memory.store.rows import Identity, version_over


async def retire_reversibly(input: MemoryDeleteInput) -> MemoryDeleteOutput:
    """Seed an automatic or legacy tombstone through the real canonical write path.

    Restore tests use this state to keep covering resurrection without cancelling
    the permanent refusal expressed by an explicit deletion.
    """
    import server.memory.store as memory_repo
    from server.memory.app.handlers import _retire_derived, _write_tombstone_row

    ch = get_client()
    connected_here = not ch.connected
    if connected_here:
        await ch.connect()
    try:
        source = await memory_repo.get_by_key(
            Identity(
                org=input.org,
                namespace=input.namespace,
                agent=input.agent,
                user_id=input.user_id,
                session_id=input.session_id,
            ),
            input.memory_key,
            fresh=True,
            strong=True,
            scope=WHOLE_NAMESPACE,
            ch=ch,
        )
        assert source is not None, "the reversible retirement fixture needs a live source"
        version = version_over(source)
        await _write_tombstone_row(source, version=version, refuse_value=False)
        tombstoned, downgraded = await _retire_derived(source, version=version, op="memory.expire")
        return MemoryDeleteOutput(
            tombstoned=True,
            version=version,
            dependents_tombstoned=tombstoned,
            dependents_downgraded=downgraded,
        )
    finally:
        if connected_here:
            await ch.disconnect()
