"""Validate citations against structured results delivered during this answer."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from server.assistant.api.models import AssistantCitation
from server.assistant.app.tools import Receipt
from server.assistant.domain import support as rule
from server.assistant.domain.citations import Citation, parse, section_path
from server.observability.event_args import project_result
from server.operations.toolsets import ASSISTANT_CITABLE_OPS, ASSISTANT_NO_NAMESPACE_DATA_OPS


def support(text: str, receipts: list[Receipt], citations: list[AssistantCitation]) -> str:
    rows = []
    for receipt in receipts:
        projected = project_result(receipt.result, op_name=receipt.op)
        rows.append(
            rule.CallRow(
                receipt.op, True, projected.get("out_found", ""), projected.get("out_hits", "")
            )
        )
    return rule.support_of(
        [rule.VERIFIED if c.verified else rule.UNREAD for c in citations],
        text=text,
        calls=rule.derive(rows),
        citable=ASSISTANT_CITABLE_OPS,
        no_data=ASSISTANT_NO_NAMESPACE_DATA_OPS,
    )


# Only structured result containers are walked. Text, snippets, arbitrary records
# and memory values cannot fabricate a second source by containing JSON or a citation.
_CONTAINERS = frozenset(
    {
        "results",
        "rows",
        "hits",
        "definitions",
        "memories",
        "sections",
        "listed",
        "memory",
        "document",
        "unit",
        "data",
    }
)


def _nodes(value: Any) -> Iterator[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for key in _CONTAINERS & value.keys():
            yield from _nodes(value[key])
    elif isinstance(value, list):
        for item in value:
            yield from _nodes(item)


def _matches(citation: Citation, receipt: Receipt) -> bool:
    result, op = receipt.result, receipt.op
    if citation.kind == "sql":
        return (
            op == "insights.query"
            and citation.sql.strip() == str(receipt.arguments.get("sql", "")).strip()
        )
    for node in _nodes(result):
        if citation.kind == "memory" and op in (
            "memory.search",
            "memory.get",
            "memory.list",
            "memory.inspect",
        ):
            key = node.get("memory_key", node.get("key"))
            if op == "memory.get" and node is result and result.get("found"):
                key = key or result.get("canonical_key") or receipt.arguments.get("memory_key")
            version = node.get("version_text") or node.get("version")
            if key == citation.key and str(version) == citation.text.rsplit("@v", 1)[-1][:-1]:
                return True
        if citation.kind == "help" and op == "help.get":
            if result.get("found") and node.get("path", result.get("path")) == citation.path:
                section = node.get("section", "")
                if not citation.section or section_path(str(section)) == citation.section:
                    return True
        if citation.kind == "doc" and op in ("knowledge.search", "knowledge.get"):
            if op == "knowledge.get" and result.get("found") is not True:
                continue
            if node.get("source_uri", result.get("source_uri")) == citation.source_uri:
                section = node.get("section", node.get("heading_path", ""))
                if isinstance(section, list):
                    section = " > ".join(map(str, section))
                if not citation.section or section_path(str(section)) == citation.section:
                    return True
        if citation.kind == "code" and op in ("code.symbol", "code.search"):
            if node.get("path") == citation.path:
                line = int(citation.text.rsplit(":", 1)[-1][:-1])
                start = node.get("start_line", node.get("line"))
                end = node.get("end_line", start)
                if type(start) is int and type(end) is int and start <= line <= end:
                    return True
    return False


def verify(text: str, receipts: list[Receipt]) -> list[AssistantCitation]:
    out = []
    for citation in parse(text):
        verified = any(_matches(citation, receipt) for receipt in receipts)
        out.append(
            AssistantCitation(
                kind=citation.kind,
                text=citation.text,
                ref=citation.ref,
                verified=verified,
                reason="" if verified else "this answer did not read this source",
            )
        )
    return out
