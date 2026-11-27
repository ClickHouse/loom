"""Status explains inherited calibration without changing retrieval policy."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, call, patch

from server.code.app import index_status
from server.memory.app import embedder_calibration


class CalibrationStatusWarningTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_namespace_calibration_needs_no_warning(self) -> None:
        lookup = AsyncMock(return_value=object())
        with patch.object(embedder_calibration, "get_namespace_calibration", lookup):
            warnings = await index_status._calibration_warnings("org-one", "repo-one")
        self.assertEqual(warnings, [])
        lookup.assert_awaited_once_with("org-one", "repo-one")

    async def test_unavailable_namespace_calibration_explains_inherited_thresholds(self) -> None:
        lookup = AsyncMock(return_value=None)
        with patch.object(embedder_calibration, "get_namespace_calibration", lookup):
            warnings = await index_status._calibration_warnings("org-one", "repo-one")
        self.assertEqual(
            warnings,
            [
                "No namespace calibration is available; code retrieval uses cluster or "
                "configured thresholds."
            ],
        )
        lookup.assert_awaited_once_with("org-one", "repo-one")

    async def test_lookup_failure_does_not_break_status_or_disclose_error_text(self) -> None:
        lookup = AsyncMock(side_effect=ValueError("detail must stay out of status"))
        with patch.object(embedder_calibration, "get_namespace_calibration", lookup):
            warnings = await index_status._calibration_warnings("org-one", "repo-one")
        self.assertEqual(warnings, ["Code retrieval calibration could not be checked."])
        self.assertNotIn("detail must stay out of status", " ".join(warnings))

    async def test_each_identity_uses_its_own_calibration_lookup(self) -> None:
        lookup = AsyncMock(side_effect=[object(), None])
        with patch.object(embedder_calibration, "get_namespace_calibration", lookup):
            first = await index_status._calibration_warnings("org-one", "repo-one")
            second = await index_status._calibration_warnings("org-two", "repo-two")
        self.assertEqual(first, [])
        self.assertEqual(len(second), 1)
        self.assertEqual(
            lookup.await_args_list,
            [call("org-one", "repo-one"), call("org-two", "repo-two")],
        )
