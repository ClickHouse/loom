from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from server.knowledge.store import documents
from server.memory.app import use_guidance as source


@pytest.mark.asyncio
async def test_changed_source_requires_current_access_and_unambiguous_identity(monkeypatch):
    docs = AsyncMock(
        return_value=[
            SimpleNamespace(source_uri="public", access_json="", tags=[], blob_erased_at=None),
            SimpleNamespace(source_uri="mirror", access_json="{}", tags=[], blob_erased_at=None),
        ]
    )
    gate = AsyncMock(side_effect=lambda org, ns, **kw: ns == "team")
    monkeypatch.setattr(documents, "get_many_by_uris", docs)
    monkeypatch.setattr(source, "row_in_scope", gate)
    monkeypatch.setattr(source, "request_narrowed", lambda: False)
    rows = [
        {"tags": ["source-changed"], "source_resource_id": "knowledge:" + cite}
        for cite in ["team/public#intro", "hidden/doc#intro", "team/mirror#part", "team/a#b#part"]
    ]
    assert await source.guidance_source_states("org", rows) == [
        "changed",
        "unknown",
        "unknown",
        "unknown",
    ]
    docs.assert_awaited_once_with("org", "team", ["mirror", "public"])
    docs.reset_mock()
    monkeypatch.setattr(source, "request_narrowed", lambda: True)
    assert await source.guidance_source_states("org", rows) == ["unknown"] * len(rows)
    docs.assert_not_awaited()
