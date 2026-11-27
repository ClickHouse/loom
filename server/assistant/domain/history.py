"""Offline conversion of owner-bound exported chat text; no storage or model I/O."""

from __future__ import annotations

from typing import Any

from server.assistant.domain.limits import MAX_HISTORY, MAX_MESSAGE_CHARS

FORMAT = "loom-assistant-history/1"


def convert(
    export: dict[str, Any],
    *,
    subject: str,
    org: str,
    namespace: str,
    owner_bindings: dict[str, str],
) -> dict[str, Any]:
    owner = export.get("user")
    if not subject or not org or not namespace or owner_bindings.get(str(owner)) != subject:
        raise ValueError("the export owner is not bound to the importing subject")
    conversation = export.get("conversationId")
    if not isinstance(conversation, str) or not conversation or len(conversation) > 256:
        raise ValueError("the export requires a stable conversationId")
    rows = export.get("messages")
    if not isinstance(rows, list) or not rows or len(rows) > MAX_HISTORY:
        raise ValueError("the export exceeds the bounded history contract")
    messages: list[dict[str, str]] = []
    known: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("a message must be an object")
        if row.get("conversationId", conversation) != conversation:
            raise ValueError("a message belongs to another conversation")
        identifier, parent = row.get("messageId"), row.get("parentMessageId")
        if (
            not isinstance(identifier, str)
            or not identifier
            or len(identifier) > 256
            or identifier in known
        ):
            raise ValueError("a message needs a unique stable id")
        if parent not in (None, "", "00000000-0000-0000-0000-000000000000") and parent not in known:
            raise ValueError("a message's parent must precede it in this export")
        role = row.get("role")
        if role is None and type(row.get("isCreatedByUser")) is bool:
            role = "user" if row["isCreatedByUser"] else "assistant"
        text = row.get("text")
        if role not in ("user", "assistant") or not isinstance(text, str) or not text.strip():
            raise ValueError("only user and assistant text can be imported")
        if len(text) > MAX_MESSAGE_CHARS or any(
            row.get(k) for k in ("files", "attachments", "tool_calls", "content")
        ):
            raise ValueError("files, tool payloads and oversized text require separate archival")
        messages.append(
            {"id": identifier, "parent_id": parent or "", "role": role, "content": text}
        )
        known.add(identifier)
    return {
        "format": FORMAT,
        "org": org,
        "namespace": namespace,
        "subject": subject,
        "conversation_id": conversation,
        "messages": messages,
        "provenance": {
            "source_class": "imported",
            "source_format": "chat-export",
            "export_owner": str(owner),
        },
    }


def history(
    archive: dict[str, Any], *, subject: str, org: str, namespace: str, leaf: str
) -> list[dict[str, str]]:
    if archive.get("format") != FORMAT or any(
        archive.get(k) != v
        for k, v in (("subject", subject), ("org", org), ("namespace", namespace))
    ):
        raise ValueError("the archive does not belong to this caller and scope")
    rows = archive.get("messages", [])
    if len(rows) > MAX_HISTORY:
        raise ValueError("the archive exceeds the history bound")
    by_id = {row["id"]: row for row in rows}
    selected: list[dict[str, str]] = []
    seen: set[str] = set()
    while leaf and leaf != "00000000-0000-0000-0000-000000000000":
        if leaf not in by_id or leaf in seen:
            raise ValueError("the selected history has a missing parent or cycle")
        seen.add(leaf)
        row = by_id[leaf]
        if row["role"] not in ("user", "assistant") or len(row["content"]) > MAX_MESSAGE_CHARS:
            raise ValueError("the archive contains invalid history")
        selected.append({"role": row["role"], "content": row["content"]})
        leaf = row["parent_id"]
    return list(reversed(selected))
