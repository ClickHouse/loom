"""Calibration readiness reads the namespace override without creating one."""

from __future__ import annotations

from tests.integration._resources import unique_org


async def test_unavailable_namespace_calibration_is_a_readiness_warning(ch_client) -> None:
    from server.code.app.index_status import _calibration_warnings
    from server.core.ch import get_client

    await get_client().connect()
    warnings = await _calibration_warnings(unique_org("calibration-status"), "repo")
    assert warnings == [
        "No namespace calibration is available; code retrieval uses cluster or configured "
        "thresholds."
    ]
