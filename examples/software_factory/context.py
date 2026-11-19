"""Host lookup policy, deduplication and local grounding."""

from __future__ import annotations

import ast
import json
import time

from .model import Budget, Ledger, Retrieval, Task, digest
from .workspace import Checkout


class Context:
    def __init__(
        self,
        task: Task,
        checkout: Checkout,
        backend: Retrieval,
        ledger: Ledger,
        budget: Budget,
        org: str,
    ):
        self.task, self.checkout, self.backend = task, checkout, backend
        self.ledger, self.budget, self.org = ledger, budget, org
        self.cache: dict[str, dict] = {}
        self.requests = 0
        self.chars = 0

    def lookup(
        self, stage: str, *, symbol: str = "", failure: str = "", caller: str = "host"
    ) -> dict:
        symbol = symbol or self.task.symbol
        op = "code.impact" if stage == "diff" else "code.symbol"
        key = digest([op, symbol, self.checkout.state()])
        if key in self.cache:
            self.ledger.emit("context_reuse", stage=stage, caller=caller, key=key)
            self.ledger.emit(
                "context",
                stage=stage,
                caller=caller,
                evidence=self.cache[key],
                state=self.checkout.state(),
                failure_sha256=digest(failure),
                reused=True,
            )
            return self.cache[key]
        if self.requests >= self.budget.retrievals or self.chars >= self.budget.context_chars:
            self.ledger.emit("context_budget", stage=stage, caller=caller)
            return {"abstain": True, "reason": "context_budget"}
        body = {"org": self.org, "namespace": self.task.namespace, "name": symbol}
        if op == "code.symbol":
            body.update(
                include_body=True,
                token_budget=800,
                include_history=False,
                include_examples=False,
                include_lessons=False,
            )
        else:
            body.pop("name")
            body.update(diff=self.checkout.diff(), depth=1, per_hop_limit=10, include_lessons=False)
        self.requests += 1
        started = time.monotonic()
        try:
            receipt = self.backend.request(op, body)
        except Exception as error:  # Retrieval failures must remain in the attempt ledger.
            self.ledger.emit(
                "retrieval",
                caller=caller,
                stage=stage,
                op=op,
                request=body,
                logical_requests=1,
                physical_requests=getattr(error, "physical_requests", None),
                error=str(error),
                latency_s=time.monotonic() - started,
                cost_usd=None,
                cost_status="unknown",
            )
            raise
        data = receipt["data"]
        self.ledger.emit(
            "retrieval",
            caller=caller,
            stage=stage,
            op=op,
            request=body,
            logical_requests=1,
            latency_s=time.monotonic() - started,
            result_sha256=digest(data),
            **{k: v for k, v in receipt.items() if k != "data"},
        )
        scope = receipt.get("scope", {})
        expected = {
            "org": self.org,
            "namespace": self.task.namespace,
            "repository": self.task.repository,
            "commit": self.task.base_commit,
        }
        if scope != expected or receipt.get("permission") != "allowed":
            raise PermissionError("Index permission or exact immutable scope is unverified")
        if receipt.get("ignored_args"):
            raise PermissionError("Index ignored scoped request arguments")
        evidence = self.ground(data)
        remaining = self.budget.context_chars - self.chars
        encoded = json.dumps(evidence)
        if len(encoded) > remaining:
            evidence = {"abstain": True, "reason": "context_budget"}
        else:
            self.chars += len(encoded)
        self.cache[key] = evidence
        self.ledger.emit(
            "context",
            stage=stage,
            caller=caller,
            evidence=evidence,
            state=self.checkout.state(),
            failure_sha256=digest(failure),
        )
        return evidence

    def ground(self, data: dict) -> dict:
        if data.get("abstain"):
            return {"abstain": True, "reason": "index_abstained"}
        accepted, rejected = [], []
        for item in data.get("definitions", [])[:3]:
            try:
                source = self.checkout.read(item["path"])
                lines = source.splitlines()
                start, end = int(item["start_line"]), int(item["end_line"])
                body = item.get("body", "")
                if (
                    not body
                    or start < 1
                    or end < start
                    or end > len(lines)
                    or item.get("elided")
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
        hints = self.impact_hints(data)
        return {
            "abstain": not accepted and not hints,
            "definitions": accepted,
            "rejected": rejected,
            "impact_hints": hints,
            "impact_counts_are_lower_bounds": True,
            "freshness": "verified_local" if accepted else "no_current_body",
        }

    def impact_hints(self, data: dict) -> list[dict]:
        hints = []
        for item in data.get("impacted_symbols", [])[:10]:
            try:
                source = self.checkout.read(item["path"])
                bare = item["qualified_name"].split("::")[-1].split(".")[-1]
                if not any(
                    isinstance(n, (ast.FunctionDef, ast.ClassDef)) and n.name == bare
                    for n in ast.walk(ast.parse(source))
                ):
                    continue
                hints.append(
                    {
                        "path": item["path"],
                        "symbol": bare,
                        "local_source_sha256": digest(source),
                        "relationship": "base_index_lower_bound",
                    }
                )
            except (PermissionError, KeyError, SyntaxError, TypeError):
                continue
        return hints
