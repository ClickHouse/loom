"""Bounded initial context and the relevant subset of the read-only tool roster."""

from __future__ import annotations

import re

_NAMED = re.compile(r"`([A-Za-z_][\w.:/]*(?:\([^`]*\))?)`")
_CODE_VERBS = re.compile(r"\b(calls?|callers?|callees?|defined|implemented)\b", re.I)
_CODE = re.compile(r"\b(code|function|class|caller|implementation|repository|stack trace)\b", re.I)
_HELP = re.compile(r"\b(loom|LOOM_[A-Z_]+|how (?:do|can|to)|setting|dashboard)\b", re.I)
_SQL = re.compile(r"\b(count|counts|trend|ranking|how many|how much)\b", re.I)


def initial(question: str) -> list[tuple[str, dict[str, object]]]:
    query = question[:500]
    if question.strip().rstrip("!?.").casefold() in {"hi", "hello", "hey", "thanks", "thank you"}:
        return []
    named = _NAMED.search(question)
    if _SQL.search(question):
        return [("insights.catalog", {})]
    if _CODE.search(question) or (named and _CODE_VERBS.search(question)):
        if named:
            return [
                (
                    "code.symbol",
                    {
                        "name": named[1].split("(", 1)[0],
                        "include_history": False,
                        "include_examples": False,
                    },
                )
            ]
        return [("code.search", {"query": query, "top_k": 3})]
    if _HELP.search(question):
        return [("help.search", {"query": query, "limit": 3})]
    return [
        ("memory.search", {"query": query, "top_k": 3}),
        ("knowledge.search", {"query": query, "top_k": 3}),
    ]


def relevant(ops: set[str]) -> set[str]:
    """Expose only the planes the initial plan needs, plus bounded cross-plane search."""
    planes = {op.split(".")[0] for op in ops}
    names = {
        "memory.search",
        "memory.get",
        "memory.history",
        "namespace.inventory",
        "knowledge.search",
        "knowledge.get",
        "help.search",
        "help.get",
        "code.search",
        "code.symbol",
    }
    if "insights" in planes:
        names.update({"insights.catalog", "insights.query"})
    if "help" in planes:
        names.update(
            {
                "namespace.get",
                "namespace.audit",
                "namespace.guide_get",
                "mentor.brief",
                "mentor.guide",
                "namespace.tune",
                "grants.list",
            }
        )
    return names
