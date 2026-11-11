"""The Loom assistant, asked as this agent's token: `stream` yields an answer as it is
written, `ask` returns it whole. Both send `POST /v1/assistant/chat/completions` with the
agent's namespace; the server checks each citation the answer writes against Loom, as
the caller, when the answer ends. An answer that began is never resent: asking again
is a new answer, with its own spend.

    async for event in agent.assistant.stream("When are deploys frozen?"):
        if isinstance(event, AssistantText):
            print(event.text, end="")
        else:  # the AssistantAnswer, last
            print(event.support, [c.text for c in event.citations if c.verified])
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from loom.errors import LoomError, from_response

if TYPE_CHECKING:
    from loom.agent import Agent

ROUTE = "/v1/assistant/chat/completions"

# How long one read waits by default: the answer is silent while one tool call runs.
DEFAULT_READ_TIMEOUT_S = 300.0


@dataclass(frozen=True)
class AssistantCitation:
    """One citation the answer wrote: `memory`, `doc`, `help`, `code` or `sql`, its text,
    whether Loom verified it, why not, the memory key, document source, documentation
    page or code path it names (`ref`), and a verified help citation's section anchor."""

    kind: str
    text: str
    verified: bool
    reason: str = ""
    ref: str = ""
    anchor: str = ""


@dataclass(frozen=True)
class AssistantStep:
    """One tool call the answer made, as its ledger row shows it: the op, a line saying what
    it did (such as "Couldn't search memories" when the call failed or was refused), and
    whether it succeeded."""

    op: str
    text: str
    ok: bool


@dataclass(frozen=True)
class AssistantText:
    """The next piece of the answer's text."""

    text: str


@dataclass(frozen=True)
class AssistantAnswer:
    """The whole answer: its text, its id (the `run_id` of its ledger rows), the
    model, its checked citations, its token usage and how far it rests on what Loom
    holds (`support`): `supported`, `unsupported`, `unchecked` or `none`, as
    docs/architecture/assistant.md#support defines them; a value this SDK does not know,
    or none from an older server, reads as `unchecked`; and the op and LOOM_* names it
    wrote that the server neither serves nor documents (`unknown_names`).

    `steps`: what it read, oldest first, on a streamed answer; empty when the run made no
    call, when the server could not read the run, or from an older server. `steps_cut`: the
    run held more than 200 ledger rows, so `steps` lists its first ones."""

    text: str
    run_id: str
    model: str = ""
    citations: tuple[AssistantCitation, ...] = ()
    prompt_tokens: int = 0
    completion_tokens: int = 0
    support: str = "unchecked"
    unknown_names: tuple[str, ...] = ()
    steps: tuple[AssistantStep, ...] = ()
    steps_cut: bool = False

    @property
    def verified(self) -> tuple[AssistantCitation, ...]:
        """The citations Loom verified."""
        return tuple(c for c in self.citations if c.verified)


@dataclass
class _Reading:
    text: list[str] = field(default_factory=list)
    run_id: str = ""
    model: str = ""
    citations: tuple[AssistantCitation, ...] = ()
    prompt_tokens: int = 0
    completion_tokens: int = 0
    support: str = "unchecked"
    unknown_names: tuple[str, ...] = ()
    steps: tuple[AssistantStep, ...] = ()
    steps_cut: bool = False

    def answer(self) -> AssistantAnswer:
        return AssistantAnswer(
            "".join(self.text),
            self.run_id,
            self.model,
            self.citations,
            self.prompt_tokens,
            self.completion_tokens,
            self.support,
            self.unknown_names,
            self.steps,
            self.steps_cut,
        )


def _citations(raw: Any) -> tuple[AssistantCitation, ...]:
    return tuple(
        AssistantCitation(
            str(c.get("kind", "")),
            str(c.get("text", "")),
            c.get("verified") is True,
            str(c.get("reason", "")),
            str(c.get("ref", "")),
            str(c.get("anchor", "")),
        )
        for c in raw or []
        if isinstance(c, dict)
    )


def _steps(raw: Any) -> tuple[AssistantStep, ...]:
    return tuple(
        AssistantStep(str(s.get("op", "")), str(s.get("text", "")), s.get("ok") is True)
        for s in (raw if isinstance(raw, list) else [])
        if isinstance(s, dict)
    )


def _messages(question: str, history: Sequence[dict[str, str]] | None) -> list[dict[str, str]]:
    return [*(history or []), {"role": "user", "content": question}]


class AssistantClient:
    def __init__(self, agent: Agent) -> None:
        self._agent = agent

    def _headers(self, namespace: str | None) -> dict[str, str]:
        ns = namespace or self._agent.namespace
        return {"X-Loom-Namespace": ns} if ns else {}

    async def stream(
        self,
        question: str,
        *,
        history: Sequence[dict[str, str]] | None = None,
        namespace: str | None = None,
        read_timeout_s: float = DEFAULT_READ_TIMEOUT_S,
    ) -> AsyncIterator[AssistantText | AssistantAnswer]:
        """The answer to `question` about `namespace` (default: this agent's), with
        the earlier turns in `history` (`{"role": "user" | "assistant", "content"}`):
        an `AssistantText` for each piece as it is written, then one `AssistantAnswer`.
        A refusal before the answer starts raises its typed error; a failure once it
        has started (LibreChat failing, the answer limit) raises the same way, after
        the text written so far."""
        body = {"stream": True, "messages": _messages(question, history)}
        reading = _Reading()
        async with self._agent.transport.stream(
            ROUTE, json=body, headers=self._headers(namespace), read_timeout_s=read_timeout_s
        ) as resp:
            request_id = resp.headers.get("X-Request-Id", "")
            async for line in resp.aiter_lines():
                if not line.startswith("data: ") or line == "data: [DONE]":
                    continue
                try:
                    event = json.loads(line[6:])
                except ValueError:
                    continue  # not JSON: skipped, as the server does
                if not isinstance(event, dict):
                    continue
                if isinstance(event.get("error"), dict):
                    error = event["error"]
                    # A build that sent no status: the answer failed upstream, as a 503.
                    raise from_response(
                        {"detail": error}, _int(error.get("status")) or 503, request_id=request_id
                    )
                text = _read(event, reading)
                if text:
                    yield AssistantText(text)
        if not reading.run_id:
            err = LoomError(f"{ROUTE}: the stream ended before the answer began")
            err.request_id = request_id
            raise err
        yield reading.answer()

    async def ask(
        self,
        question: str,
        *,
        history: Sequence[dict[str, str]] | None = None,
        namespace: str | None = None,
        read_timeout_s: float = DEFAULT_READ_TIMEOUT_S,
    ) -> AssistantAnswer:
        """`stream`, whole: the `AssistantAnswer` once the answer ends."""
        async for event in self.stream(
            question, history=history, namespace=namespace, read_timeout_s=read_timeout_s
        ):
            if isinstance(event, AssistantAnswer):
                return event
        raise AssertionError("unreachable: stream ends with an answer or raises")


def _read(event: dict[str, Any], reading: _Reading) -> str:
    """Fold one chunk into `reading`; the text it adds."""
    if event.get("object") == "loom.citations":
        reading.citations = _citations(event.get("citations"))
        reading.support = _support(event.get("support"))
        names = event.get("unknown_names")
        reading.unknown_names = tuple(str(n) for n in names) if isinstance(names, list) else ()
        reading.steps = _steps(event.get("steps"))
        reading.steps_cut = event.get("steps_cut") is True
        return ""
    if event.get("object") == "loom.step":
        return ""  # the steps so far, while the answer streams: the answer carries them
    reading.run_id = reading.run_id or str(event.get("id") or "")
    reading.model = reading.model or str(event.get("model") or "")
    usage = event.get("usage")
    if isinstance(usage, dict):
        reading.prompt_tokens = _int(usage.get("prompt_tokens"))
        reading.completion_tokens = _int(usage.get("completion_tokens"))
    text = ""
    for choice in event.get("choices") or []:
        delta = choice.get("delta") if isinstance(choice, dict) else None
        if isinstance(delta, dict) and isinstance(delta.get("content"), str):
            text += delta["content"]
    reading.text.append(text)
    return text


_SUPPORTS = ("supported", "unsupported", "unchecked", "none")


def _support(value: Any) -> str:
    """The answer's support as the server sent it; an unknown or missing value is
    `unchecked`."""
    return value if isinstance(value, str) and value in _SUPPORTS else "unchecked"


def _int(value: Any) -> int:
    """A count the server sent, or 0 when it is not one."""
    try:
        return int(value or 0)
    except (TypeError, ValueError, OverflowError):  # Infinity is JSON to json.loads
        return 0


__all__ = [
    "AssistantAnswer",
    "AssistantCitation",
    "AssistantClient",
    "AssistantStep",
    "AssistantText",
]
