"""Shared local grounding for model-selected source reads."""

from __future__ import annotations

from examples.software_factory.factory.model import Budget, Ledger, Retrieval, Task
from examples.software_factory.factory.workspace import Checkout


class Context:
    def __init__(
        self,
        task: Task,
        checkout: Checkout,
        backend: Retrieval,
        ledger: Ledger,
        budget: Budget,
        org: str,
        *,
        memory_namespaces: tuple[str, ...] = (),
        memory_code_namespaces: tuple[str, ...] = (),
    ):
        self.task, self.checkout, self.backend = task, checkout, backend
        self.ledger, self.budget, self.org = ledger, budget, org
        self.memory_namespaces = tuple(memory_namespaces)
        self.memory_code_namespaces = tuple(memory_code_namespaces)
        self.cache: dict[str, dict] = {}
        self.requests = 0
        self.chars = 0

    def ground(self, data: dict) -> dict:
        if data.get("abstain"):
            return {"abstain": True, "reason": "index_abstained"}
        accepted, rejected = [], []
        for item in data.get("definitions", []):
            try:
                source = self.checkout.full_read(item["path"])
                lines = source.splitlines()
                start, end = int(item["start_line"]), int(item["end_line"])
                body = item.get("body", "")
                if item.get("elided"):
                    raise ValueError("Retrieved body elided by context budget")
                if not body:
                    raise ValueError("Retrieved body unavailable")
                if (
                    start < 1
                    or end < start
                    or end > len(lines)
                    or "\n".join(lines[start - 1 : end]) != body.removesuffix("\n")
                ):
                    raise ValueError("Retrieved body differs from working source")
                accepted.append(
                    {
                        "path": item["path"],
                        "start_line": start,
                        "end_line": end,
                        "body": body,
                        "grounding": "exact_local_span",
                    }
                )
            except (PermissionError, KeyError, ValueError) as error:
                rejected.append(str(error))
        return {
            "abstain": not accepted,
            "definitions": accepted,
            "rejected": rejected,
            "freshness": "verified_local" if accepted else "no_current_body",
        }
