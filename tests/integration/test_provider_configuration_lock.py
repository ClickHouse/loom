"""Operator saves serialize across independent ClickHouse connections."""

import pytest

from server.insights.store.provider_lock import (
    ConfigurationLockUnavailable,
    configuration_lock,
)


@pytest.mark.asyncio
async def test_configuration_lock_excludes_another_connection(ch_target_required):
    async with configuration_lock() as first:
        assert first.held
        with pytest.raises(ConfigurationLockUnavailable):
            async with configuration_lock():
                pytest.fail("two independent clients acquired the same configuration lock")
        assert first.held
    async with configuration_lock() as next_holder:
        assert next_holder.held
        assert next_holder.holder != first.holder
