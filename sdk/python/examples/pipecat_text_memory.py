"""Run one text-only Pipecat turn with Loom recall and inline capture.

Requires an installed Pipecat extra, a reachable Loom deployment, scoped app
credentials, and the application's EXISTING trusted session pointer file.
Open the same family on restore; this example never creates or repairs one.
A canned reply uses no Pipecat audio/model provider. Loom may embed/extract
with its configured providers. Pipeline completion alone does not prove capture.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime
from pathlib import Path

from loom import Agent
from loom.integrations.pipecat import LoomMemoryService
from loom.session_protection import SessionProtection
from pipecat.frames.frames import EndFrame, Frame, LLMContextAssistantTurnFrame, LLMContextFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.workers.runner import WorkerRunner

FACT = "I prefer aisle seats."
REPLY = "I will use your stated preference for aisle seats."


class CannedReply(FrameProcessor):
    """Append a fixed answer, then emit the native finalized-turn event."""

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, LLMContextFrame):
            frame.context.add_message({"role": "assistant", "content": REPLY})
            await self.push_frame(
                LLMContextAssistantTurnFrame(text=REPLY, timestamp=datetime.now(UTC).isoformat()),
                direction,
            )
            return
        await self.push_frame(frame, direction)


async def run_turn(agent: Agent, *, user_id: str, conversation_id: str) -> LLMContext:
    """Run real native processors and return context for independent observation."""
    memory = LoomMemoryService(agent, user_id=user_id, session_id=conversation_id, defer=False)
    context = LLMContext([{"role": "user", "content": FACT}])
    worker = PipelineWorker(
        Pipeline([memory.recall_processor(), CannedReply(), memory.capture_processor()])
    )
    await worker.queue_frames([LLMContextFrame(context=context), EndFrame()])
    runner = WorkerRunner(handle_sigint=False)
    await runner.add_workers(worker)
    await runner.run()
    return context


def open_application_family(pointer_path: Path) -> SessionProtection:
    """Open trusted application metadata; never initialize missing restore state."""
    return SessionProtection.open(json.loads(pointer_path.read_text()))


async def main() -> LLMContext:
    # Pointer metadata is application authority, never model input or a restore-time default.
    protection = await asyncio.to_thread(
        open_application_family, Path(os.environ["LOOM_SESSION_POINTER_FILE"])
    )
    async with Agent(
        org=os.environ["LOOM_ORG"],
        namespace=os.environ["LOOM_NAMESPACE"],
        agent=os.environ["LOOM_AGENT"],
        base_url=os.environ["LOOM_BASE_URL"],
        api_key=os.environ["LOOM_API_KEY"],
        session_protection=protection,
    ) as agent:
        context = await run_turn(
            agent,
            user_id=os.environ["LOOM_USER_ID"],
            conversation_id=os.environ["LOOM_CONVERSATION_ID"],
        )
    print("Pipecat text turn finished; verify recall and capture independently.")
    return context


if __name__ == "__main__":
    asyncio.run(main())
