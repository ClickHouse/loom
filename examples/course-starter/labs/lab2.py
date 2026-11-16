"""Lab 2: document search with citations.

The lab: https://github.com/ClickHouse/loom/blob/main/docs/guides/teaching.md#lab-2-document-search-with-citations
The SDK calls it uses: https://github.com/ClickHouse/loom/blob/main/docs/features/knowledge.md

Write your own notes in labs/notes.md, fill in the constants and the two
functions marked TODO, then run `python labs/lab2.py`. `python grading/grade.py`
checks them.
"""

import asyncio
from pathlib import Path

from loom import Agent

NOTES = Path(__file__).with_name("notes.md")
# The document's name in Loom. Keep it: the checks search for it.
SOURCE_URI = "course://lab2/notes.md"

# TODO: a question notes.md answers, and one it does not.
QUESTION = ""
UNANSWERABLE = ""
# TODO: the similarity that separates them (Lab 2's second exercise).
CUTOFF = 1.0


def make_agent() -> Agent:
    # The server comes from LOOM_BASE_URL, else http://localhost:7777.
    return Agent(org="dev", namespace="local", agent="lab2-student", consumer="course-lab2")


async def ingest(agent: Agent) -> None:
    """Publish NOTES as the document SOURCE_URI and wait until it is searchable."""
    # TODO: agent.knowledge.ingest_file(...)
    raise NotImplementedError("Lab 2: ingest")


async def answerable(agent: Agent, question: str) -> bool:
    """Whether the documents answer `question`: the best hit's similarity is at
    least CUTOFF."""
    # TODO: agent.knowledge.search(...), then compare the hits' similarity with CUTOFF
    raise NotImplementedError("Lab 2: answerable")


async def main() -> None:
    async with make_agent() as agent:
        await ingest(agent)
        for question in (QUESTION, UNANSWERABLE):
            hits = await agent.knowledge.search(question, top_k=1)
            if hits:
                print(f"{question!r}: {hits[0].source_uri} > {hits[0].section}")
                print(f"  similarity {hits[0].similarity:.3f}")
            print("  answerable:", await answerable(agent, question))


if __name__ == "__main__":
    asyncio.run(main())
