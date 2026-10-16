"""Operator saves serialize across holders on one ClickHouse."""

import pytest

from server.core.ch import get_client
from server.insights.store.provider_lock import (
    ConfigurationLockUnavailable,
    configuration_lock,
)


@pytest.mark.asyncio
async def test_configuration_lock_excludes_another_holder(ch_target_required):
    await get_client().connect()
    async with configuration_lock() as first:
        assert first.held
        with pytest.raises(ConfigurationLockUnavailable):
            async with configuration_lock():
                pytest.fail("two holders acquired the same configuration lock")
        assert first.held
    async with configuration_lock() as next_holder:
        assert next_holder.held
        assert next_holder.holder != first.holder
