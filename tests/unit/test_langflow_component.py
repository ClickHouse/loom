"""Langflow component: each action's request and output, without installing Langflow."""

from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path

import httpx
import pytest
from loom import SyncAgent

ROOT = Path(__file__).resolve().parents[2]
COMPONENT = ROOT / "integrations" / "langflow" / "loom_component.py"


class _Field:
    def __init__(self, **kwargs: object) -> None:
        self.__dict__.update(kwargs)


class _Data:
    def __init__(self, data: object = None, **kwargs: object) -> None:
        self.data = data if data is not None else dict(kwargs)


class _Component:
    pass


def _install_lfx_stubs() -> None:
    lfx = types.ModuleType("lfx")
    custom = types.ModuleType("lfx.custom")
    custom_component = types.ModuleType("lfx.custom.custom_component")
    component = types.ModuleType("lfx.custom.custom_component.component")
    component.Component = _Component
    io = types.ModuleType("lfx.io")
    io.DropdownInput = _Field
    io.IntInput = _Field
    io.MessageTextInput = _Field
    io.SecretStrInput = _Field
    io.StrInput = _Field
    io.Output = _Field
    schema = types.ModuleType("lfx.schema")
    data = types.ModuleType("lfx.schema.data")
    data.Data = _Data
    modules = {
        "lfx": lfx,
        "lfx.custom": custom,
        "lfx.custom.custom_component": custom_component,
        "lfx.custom.custom_component.component": component,
        "lfx.io": io,
        "lfx.schema": schema,
        "lfx.schema.data": data,
    }
    sys.modules.update(modules)
    lfx.custom = custom
    custom.custom_component = custom_component
    custom_component.component = component
    lfx.io = io
    lfx.schema = schema
    schema.data = data


def _load_component():
    _install_lfx_stubs()
    spec = importlib.util.spec_from_file_location("loom_langflow_component", COMPONENT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def component_mod():
    return _load_component()


def _handler(responses: dict[str, dict]) -> tuple[list[tuple[str, dict]], object]:
    recorded: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        path = request.url.path
        suffix = path.split("/v1/", 1)[-1] if "/v1/" in path else path
        recorded.append((suffix, body))
        payload = responses.get(suffix)
        if payload is None:
            return httpx.Response(404, json={"error": f"unexpected {path}"})
        return httpx.Response(200, json=payload)

    return recorded, handler


def _patch_agent(mod, handler) -> None:
    def factory(**kwargs):
        sa = SyncAgent(**kwargs)
        base = kwargs.get("base_url") or "http://x"
        sa.transport._client = httpx.AsyncClient(
            base_url=base, transport=httpx.MockTransport(handler)
        )
        return sa

    mod._open_agent = factory


def _node(mod, **inputs):
    node = mod.LoomComponent()
    values = {
        "action": "search memory",
        "org": "acme",
        "namespace": "support",
        "user_id": "alice",
        "query": "preferred seat",
        "memory_key": "",
        "top_k": 5,
        "base_url": "http://x",
        "api_key": "k",
        "agent_name": "langflow",
    }
    values.update(inputs)
    for key, value in values.items():
        setattr(node, key, value)
    return node


def test_save_sends_memory_set_and_returns_write_fields(component_mod) -> None:
    recorded, handler = _handler(
        {
            "memory.set": {
                "memory_id": "m-1",
                "version": 1,
                "content_sha256": "abc",
                "conflict_verdict": "clear",
                "noop": False,
            }
        }
    )
    _patch_agent(component_mod, handler)
    result = _node(
        component_mod, action="save", memory_key="pref.seat", query="aisle seat"
    ).produce_result()
    assert recorded[0][0] == "memory.set"
    body = recorded[0][1]
    assert body["org"] == "acme"
    assert body["namespace"] == "support"
    assert body["user_id"] == "alice"
    assert body["memory_key"] == "pref.seat"
    assert body["value"] == "aisle seat"
    assert result.data["action"] == "save"
    assert result.data["memory_id"] == "m-1"
    assert result.data["version"] == 1
    assert result.data["content_sha256"] == "abc"


def test_search_memory_sends_query_and_returns_hits(component_mod) -> None:
    recorded, handler = _handler(
        {
            "memory.search": {
                "results": [
                    {
                        "memory_key": "pref.seat",
                        "memory_type": "semantic",
                        "content_excerpt": "aisle seat",
                        "distance": 0.1,
                        "similarity": 0.9,
                        "namespace": "support",
                    }
                ],
                "abstain": False,
                "abstain_code": "",
            }
        }
    )
    _patch_agent(component_mod, handler)
    result = _node(
        component_mod, action="search memory", query="which seat", top_k=3
    ).produce_result()
    assert recorded[0][0] == "memory.search"
    body = recorded[0][1]
    assert body["org"] == "acme"
    assert body["namespace"] == "support"
    assert body["user_id"] == "alice"
    assert body["query"] == "which seat"
    assert body["top_k"] == 3
    assert result.data["action"] == "search memory"
    assert result.data["abstain"] is False
    assert result.data["hits"][0]["memory_key"] == "pref.seat"
    assert result.data["hits"][0]["value"] == "aisle seat"
    assert result.data["hits"][0]["similarity"] == 0.9


def test_search_documents_sends_query_and_returns_hits(component_mod) -> None:
    recorded, handler = _handler(
        {
            "knowledge.search": {
                "results": [
                    {
                        "source_uri": "doc://guide",
                        "chunk_idx": 0,
                        "content": "Seat map is in the cabin guide.",
                        "tags": [],
                        "distance": 0.2,
                        "similarity": 0.8,
                        "namespace": "support",
                    }
                ]
            }
        }
    )
    _patch_agent(component_mod, handler)
    result = _node(
        component_mod, action="search documents", query="cabin guide", top_k=2
    ).produce_result()
    assert recorded[0][0] == "knowledge.search"
    body = recorded[0][1]
    assert body["org"] == "acme"
    assert body["namespace"] == "support"
    assert body["query"] == "cabin guide"
    assert body["top_k"] == 2
    assert "user_id" not in body
    assert result.data["action"] == "search documents"
    assert result.data["hits"][0]["source_uri"] == "doc://guide"
    assert result.data["hits"][0]["content"] == "Seat map is in the cabin guide."
    assert result.data["hits"][0]["similarity"] == 0.8


def test_save_requires_memory_key(component_mod) -> None:
    with pytest.raises(ValueError, match="memory_key is required"):
        _node(component_mod, action="save", memory_key="").produce_result()
