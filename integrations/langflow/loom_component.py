"""Langflow custom component: save and search Loom memory, and search knowledge documents."""

from __future__ import annotations

from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import (
    DropdownInput,
    IntInput,
    MessageTextInput,
    Output,
    SecretStrInput,
    StrInput,
)
from lfx.schema.data import Data
from loom import SyncAgent

SAVE = "save"
SEARCH_MEMORY = "search memory"
SEARCH_DOCUMENTS = "search documents"
_ACTIONS = (SAVE, SEARCH_MEMORY, SEARCH_DOCUMENTS)


def _open_agent(**kwargs: Any) -> SyncAgent:
    return SyncAgent(**kwargs)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _secret(value: Any) -> str | None:
    if value is None:
        return None
    raw = value.get_secret_value() if hasattr(value, "get_secret_value") else value
    text = _text(raw)
    return text or None


def _query_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    inner = getattr(value, "text", None)
    if isinstance(inner, str):
        return inner
    return str(value)


class LoomComponent(Component):
    display_name = "Loom"
    description = "Save and search Loom memory, and search knowledge documents."
    documentation: str = (
        "https://github.com/ClickHouse/loom/blob/main/integrations/langflow/README.md"
    )
    icon = "database"
    name = "Loom"

    inputs = [
        DropdownInput(
            name="action",
            display_name="Action",
            options=list(_ACTIONS),
            value=SEARCH_MEMORY,
            info="Save a memory, search memories, or search knowledge documents.",
        ),
        StrInput(name="org", display_name="Org", value="", required=False),
        StrInput(
            name="namespace",
            display_name="Namespace",
            value="",
            required=True,
            info="Loom namespace the component reads and writes.",
        ),
        StrInput(
            name="user_id",
            display_name="User",
            value="",
            required=False,
            info="End user for memory.set and memory.search. Empty reads the namespace.",
        ),
        MessageTextInput(
            name="query",
            display_name="Query",
            value="",
            required=True,
            info="The memory value to save, or the search query.",
        ),
        StrInput(
            name="memory_key",
            display_name="Memory key",
            value="",
            required=False,
            info="Required when the action is save.",
        ),
        IntInput(name="top_k", display_name="Top K", value=5, required=False),
        StrInput(
            name="base_url",
            display_name="Loom URL",
            value="http://localhost:7777",
            required=False,
        ),
        SecretStrInput(name="api_key", display_name="API key", value="", required=False),
        StrInput(
            name="agent_name",
            display_name="Agent",
            value="langflow",
            required=False,
            info="Agent name stamped on writes.",
        ),
    ]
    outputs = [
        Output(display_name="Result", name="result", method="produce_result"),
    ]

    def produce_result(self) -> Data:
        action = _text(getattr(self, "action", ""))
        if action not in _ACTIONS:
            raise ValueError(f"unknown action: {action or '(empty)'}")
        namespace = _text(getattr(self, "namespace", ""))
        if not namespace:
            raise ValueError("namespace is required")
        memory_key = _text(getattr(self, "memory_key", ""))
        if action == SAVE and not memory_key:
            raise ValueError("memory_key is required when saving a memory")
        return Data(data=self._call_loom(action, namespace, memory_key))

    def _call_loom(self, action: str, namespace: str, memory_key: str) -> dict[str, Any]:
        with _open_agent(**self._agent_kwargs(namespace)) as agent:
            user_id = _text(getattr(self, "user_id", "")) or None
            query = _query_text(getattr(self, "query", ""))
            top_k = int(getattr(self, "top_k", 5) or 5)
            if action == SAVE:
                written = agent.memory.set(memory_key, query, user_id=user_id)
                return _save_payload(written)
            if action == SEARCH_MEMORY:
                found = agent.memory.search(query, user_id=user_id, top_k=top_k)
                return _memory_hits_payload(found)
            found = agent.knowledge.search(query, top_k=top_k)
            return _document_hits_payload(found)

    def _agent_kwargs(self, namespace: str) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "org": _text(getattr(self, "org", "")),
            "namespace": namespace,
            "agent": _text(getattr(self, "agent_name", "")) or "langflow",
            "retry": {"max_attempts": 1},
        }
        base_url = _text(getattr(self, "base_url", ""))
        if base_url:
            kwargs["base_url"] = base_url
        api_key = _secret(getattr(self, "api_key", None))
        if api_key:
            kwargs["api_key"] = api_key
        return kwargs


def _save_payload(written: dict[str, Any]) -> dict[str, Any]:
    return {
        "action": SAVE,
        "memory_id": written.get("memory_id"),
        "version": written.get("version"),
        "content_sha256": written.get("content_sha256"),
        "conflict_verdict": written.get("conflict_verdict"),
        "noop": written.get("noop"),
    }


def _memory_hits_payload(found: Any) -> dict[str, Any]:
    hits = [
        {
            "memory_key": hit.memory_key,
            "value": hit.content_excerpt,
            "similarity": hit.similarity,
            "memory_type": hit.memory_type,
            "namespace": hit.namespace,
        }
        for hit in found
    ]
    return {
        "action": SEARCH_MEMORY,
        "abstain": bool(getattr(found, "abstain", False)),
        "abstain_code": getattr(found, "abstain_code", "") or "",
        "hits": hits,
    }


def _document_hits_payload(found: list[Any]) -> dict[str, Any]:
    hits = [
        {
            "source_uri": hit.source_uri,
            "content": hit.content,
            "similarity": hit.similarity,
            "chunk_idx": hit.chunk_idx,
            "namespace": hit.namespace,
        }
        for hit in found
    ]
    return {"action": SEARCH_DOCUMENTS, "hits": hits}
