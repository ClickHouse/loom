"""`assistant.ask`: the bridge's whole answer, as an op."""

from __future__ import annotations

from server.assistant.api.models import AssistantAskInput, AssistantAskOutput
from server.assistant.app import bridge, service
from server.auth.context import current_auth
from server.core.errors import AssistantUnavailableError


async def handle_assistant_ask(inp: AssistantAskInput) -> AssistantAskOutput:
    auth = current_auth()
    if auth is None:
        # Each answer reads under a token delegated from the caller's.
        raise AssistantUnavailableError("the assistant answers as its caller: it needs auth on")
    messages = [m.model_dump() for m in inp.history] + [{"role": "user", "content": inp.question}]
    answer = await bridge.ask(auth, inp.org, inp.namespace, messages)
    return AssistantAskOutput(
        answer="".join(answer.text),
        run_id=answer.run_id,
        model=service.current_definition().model,
    )
