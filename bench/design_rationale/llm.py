"""Model calls under a spend cap.

Every call is priced before it is sent, at its whole prompt plus its full output cap, and
refused when that would take the ledger past `--max-usd`; after the call the ledger
records what the provider billed. The extraction leg runs in the server's code, so its
calls enter the ledger as an estimate charged before the run (`estimated: true`).
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from server.observability.cost_accumulator import PRICE_PER_1K_TOKENS

CHARS_PER_TOKEN = 4


class CapReached(RuntimeError):
    pass


def price(model: str) -> tuple[float, float] | None:
    """USD per 1K (input, output) tokens; None for a model the price table does not list."""
    return PRICE_PER_1K_TOKENS.get(model)


def cost_usd(model: str, tokens_in: int, tokens_out: int) -> float:
    p = price(model)
    if p is None:
        raise CapReached(f"model {model!r} has no price: the spend cap cannot be enforced")
    return tokens_in / 1000 * p[0] + tokens_out / 1000 * p[1]


class Ledger:
    """One JSON line per call in `<out>/ledger.jsonl`; spend survives a resumed run."""

    def __init__(self, path: Path, max_usd: float):
        self.path = path
        self.max_usd = max_usd

    def rows(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text().splitlines() if line]

    def spent(self) -> float:
        return float(sum(r["usd"] for r in self.rows()))

    def check(self, model: str, tokens_in: int, tokens_out: int) -> None:
        """Refuse a call whose worst-case price would cross the cap."""
        worst = cost_usd(model, tokens_in, tokens_out)
        if self.spent() + worst > self.max_usd:
            raise CapReached(
                f"spent {self.spent():.4f} USD; a call of up to {worst:.4f} USD would cross "
                f"--max-usd {self.max_usd}"
            )

    def record(
        self, stage: str, model: str, tokens_in: int, tokens_out: int, *, estimated: bool = False
    ) -> None:
        row = {
            "t": time.time(),
            "stage": stage,
            "model": model,
            "in": tokens_in,
            "out": tokens_out,
            "usd": cost_usd(model, tokens_in, tokens_out),
            "estimated": estimated,
        }
        with self.path.open("a") as f:
            f.write(json.dumps(row) + "\n")


@dataclass
class Reply:
    text: str
    tokens_in: int
    tokens_out: int


# (system, user, max_tokens) -> Reply. A fake for tests and the offline mode.
Complete = Callable[[str, str, int], Reply]


def provider_complete(model: str) -> Complete:
    """One chat call through the bench's provider client (bench/judges.py)."""
    from bench.judges import achat

    def complete(system: str, user: str, max_tokens: int) -> Reply:
        async def call() -> tuple[str, dict[str, int]]:
            async with httpx.AsyncClient() as client:
                return await achat(
                    client, model, user, max_tokens=max_tokens, system=system, timeout_s=300.0
                )

        text, usage = asyncio.run(call())
        return Reply(text, usage["prompt_tokens"], usage["completion_tokens"])

    return complete


class Model:
    """A named model whose every call goes through the ledger."""

    def __init__(self, name: str, complete: Complete, ledger: Ledger, *, priced: bool = True):
        self.name = name
        self.complete = complete
        self.ledger = ledger
        # An offline stub costs nothing and is never recorded.
        self.priced = priced

    def __call__(self, stage: str, system: str, user: str, max_tokens: int) -> Reply:
        if self.priced:
            self.ledger.check(self.name, (len(system) + len(user)) // CHARS_PER_TOKEN, max_tokens)
        reply = self.complete(system, user, max_tokens)
        if self.priced:
            self.ledger.record(stage, self.name, reply.tokens_in, reply.tokens_out)
        return reply


def parse_json(text: str) -> dict[str, Any] | None:
    """The JSON object a judge returned, fences and surrounding prose tolerated."""
    from server.llm.json_text import strip_code_fence

    body = strip_code_fence(text)
    start, end = body.find("{"), body.rfind("}")
    if start < 0 or end < start:
        return None
    try:
        value = json.loads(body[start : end + 1])
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None
