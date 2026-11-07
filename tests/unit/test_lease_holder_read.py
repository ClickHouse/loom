"""`held_by` refuses with a fixed reason when the lease store cannot answer; the
driver's text, which can carry the server's address, is logged. CH-free."""

from __future__ import annotations

from typing import Any

import pytest

from server.core import leases


class _DownCH:
    async def query_one(self, *a: Any, **k: Any) -> Any:
        raise RuntimeError("Code: 210. Connection refused (ch-internal-7:9440)")


async def test_an_unanswered_holder_read_names_no_driver_text() -> None:
    with pytest.raises(leases.LeaseUnavailable) as ei:
        await leases.held_by("code_ingest", "h", ch=_DownCH())
    assert ei.value.message == "the lease store did not answer"
    assert ei.value.extra["operation"] == "code_ingest"
