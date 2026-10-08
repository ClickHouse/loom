"""A scripted `server.llm.client.LLMClient` for unit tests.

    llm = ScriptedLLM(['{"facts": []}', TimeoutError("provider")])
    monkeypatch.setattr("server.llm.client.get_llm_client", lambda: llm)
    ...
    assert "Extract" in llm.calls[0].system

Replies are served in order; an exception in the script is raised at that
call (fault injection). With `repeat_last` the final reply answers every
later call; without it, a call past the script fails the test.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from server.llm.client import LLMResult


@dataclass(frozen=True)
class Prompt:
    system: str
    user: str
    max_tokens: int
    response_format: dict[str, str] | None


@dataclass
class ScriptedLLM:
    replies: list[str | BaseException]
    model: str = "fake-scripted"
    provider: str = "fake"
    repeat_last: bool = False
    calls: list[Prompt] = field(default_factory=list)

    async def complete(
        self,
        system: str,
        user: str,
        max_tokens: int = 1024,
        response_format: dict[str, str] | None = None,
    ) -> LLMResult:
        self.calls.append(Prompt(system, user, max_tokens, response_format))
        i = len(self.calls) - 1
        if i >= len(self.replies):
            if not (self.repeat_last and self.replies):
                raise AssertionError(f"unscripted LLM call #{i + 1}: {user[:200]!r}")
            i = len(self.replies) - 1
        reply = self.replies[i]
        if isinstance(reply, BaseException):
            raise reply
        return LLMResult(
            text=reply,
            tokens_in=len(system.split()) + len(user.split()),
            tokens_out=len(reply.split()),
            provider=self.provider,
            model=self.model,
        )
