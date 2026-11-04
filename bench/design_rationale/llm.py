"""Model calls under a spend cap.

Every request is priced before it is sent, at its whole body (a character as a token, over
three: tokenizers rarely do worse) plus its full output cap, and refused when that would
take the ledger past `--max-usd`; after it the ledger records what the provider billed. A
provider client that resends (a learned field, a raised cap for a starved reply) is
checked and recorded request by request. The extraction leg runs in the server's code, so
its calls enter the ledger as an estimate charged before the run (`estimated: true`), and
the server's own model and embedding spend in the run's orgs is read back from
`cost.summary` after each stage (`stage: server`): recorded after the fact, and kept from
running past the cap by each namespace's daily cost cap, not by this ledger.
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

# The input estimate of the cap check: one token per three characters, a padded count.
CHARS_PER_TOKEN_WORST = 3


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
        self._append(
            {
                "stage": stage,
                "model": model,
                "in": tokens_in,
                "out": tokens_out,
                "usd": cost_usd(model, tokens_in, tokens_out),
                "estimated": estimated,
            }
        )

    def record_usd(self, stage: str, usd: float, **extra: Any) -> None:
        """Spend the ledger did not price itself (the server's, read from cost.summary)."""
        self._append({"stage": stage, "usd": usd, "estimated": False, **extra})

    def _append(self, row: dict[str, Any]) -> None:
        with self.path.open("a") as f:
            f.write(json.dumps({"t": time.time(), **row}) + "\n")


@dataclass
class Reply:
    text: str
    tokens_in: int
    tokens_out: int


# (system, user, max_tokens) -> Reply. A fake for tests and the offline mode.
Complete = Callable[[str, str, int], Reply]


def _usage(data: Any) -> tuple[int, int]:
    """(input, output) tokens of a provider reply, either provider's shape."""
    u = data.get("usage") if isinstance(data, dict) else None
    if not isinstance(u, dict):
        return 0, 0
    tin = u.get("prompt_tokens", u.get("input_tokens", 0))
    tout = u.get("completion_tokens", u.get("output_tokens", 0))
    return int(tin or 0), int(tout or 0)


class ProviderComplete:
    """One chat call through the bench's provider client (bench/judges.py), every HTTP
    request it sends checked against the cap and every reply recorded."""

    def __init__(self, model: str, ledger: Ledger):
        self.model = model
        self.ledger = ledger
        self.stage = ""

    async def _before(self, request: httpx.Request) -> None:
        body = json.loads(request.content or b"{}")
        cap = body.get("max_completion_tokens") or body.get("max_tokens") or 0
        self.ledger.check(self.model, len(request.content) // CHARS_PER_TOKEN_WORST, int(cap))

    async def _after(self, response: httpx.Response) -> None:
        await response.aread()
        try:
            tin, tout = _usage(response.json())
        except ValueError:
            tin = tout = 0
        self.ledger.record(self.stage, self.model, tin, tout)

    def __call__(self, system: str, user: str, max_tokens: int) -> Reply:
        from bench.judges import achat

        async def call() -> tuple[str, dict[str, int]]:
            hooks: dict[str, list[Callable[..., Any]]] = {
                "request": [self._before],
                "response": [self._after],
            }
            async with httpx.AsyncClient(event_hooks=hooks) as client:
                return await achat(
                    client, self.model, user, max_tokens=max_tokens, system=system, timeout_s=300.0
                )

        text, usage = asyncio.run(call())
        return Reply(text, usage["prompt_tokens"], usage["completion_tokens"])


class Model:
    """A named model whose every call goes through the ledger."""

    def __init__(self, name: str, complete: Complete, ledger: Ledger, *, priced: bool = True):
        self.name = name
        self.complete = complete
        self.ledger = ledger
        # An offline stub costs nothing and is never recorded.
        self.priced = priced

    def __call__(self, stage: str, system: str, user: str, max_tokens: int) -> Reply:
        if isinstance(self.complete, ProviderComplete):
            # The client checks and records each request it sends.
            self.complete.stage = stage
            return self.complete(system, user, max_tokens)
        if self.priced:
            worst_in = (len(system) + len(user)) // CHARS_PER_TOKEN_WORST
            self.ledger.check(self.name, worst_in, max_tokens)
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
