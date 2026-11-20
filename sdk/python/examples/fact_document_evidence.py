"""Read a saved fact and a caller-selected document section for operator review.

Needs existing data and read access. No search, embedding or model call is made.
The Python result contains bodies; the CLI prints only presence and field names.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any

import httpx
from loom import Agent
from loom.errors import LoomError

FACT_FIELDS = (
    "agent",
    "user_id",
    "session_id",
    "value",
    "version",
    "status",
    "actor",
    "source_class",
    "source_trace",
    "source_resource_id",
)


async def read_pair(
    bot: Agent,
    *,
    memory_key: str,
    source_uri: str,
    section_path: list[str],
    user_id: str = "-",
    session_id: str = "-",
) -> dict[str, Any]:
    """Return separate reads, with no inferred relationship or support judgment."""
    if not section_path or any(not part.strip() for part in section_path):
        raise ValueError("Select a nonempty document section path")
    fact = await bot.memory.get(
        memory_key,
        user_id=user_id,
        session_id=session_id,
        fields=list(FACT_FIELDS),
    )
    expected = bot.identity(user_id=user_id, session_id=session_id)
    if fact is not None and any(
        fact.get(field) != expected[field] for field in ("agent", "user_id", "session_id")
    ):
        raise ValueError("Returned fact does not match the selected identity")
    passage = await bot.knowledge.outline(
        source_uri, section_path=section_path, include_children=False
    )
    return {
        "fact": (
            {"found": True, **{k: fact[k] for k in FACT_FIELDS if k in fact}}
            if fact is not None
            else {"found": False}
        ),
        "document_section": {
            k: passage[k]
            for k in ("found", "source_uri", "section_content", "section_chunk_idxs")
            if k in passage
        },
        "selection": {
            "memory_key": memory_key,
            "source_uri": source_uri,
            "section_path": list(section_path),
            "relationship": "caller_selected",
        },
        "support": "not_evaluated",
    }


def summary(result: dict[str, Any]) -> dict[str, Any]:
    """Keep returned text, locators and provenance values out of CLI logs."""
    fact, section = result["fact"], result["document_section"]
    return {
        "fact_found": bool(fact.get("found")),
        "section_found": bool(section.get("found")) and section.get("section_content") is not None,
        "fact_fields_returned": [k for k in FACT_FIELDS if k in fact],
        "section_fields_returned": [
            k for k in ("source_uri", "section_content", "section_chunk_idxs") if k in section
        ],
        "relationship": "caller_selected",
        "support": "not_evaluated",
    }


async def run(args: argparse.Namespace) -> dict[str, Any]:
    async with Agent(
        args.org,
        args.namespace,
        args.agent,
        auto_run=False,
        mentor_brief=False,
        pacing=False,
        ratelimit_max_wait_s=0,
        retry={"max_attempts": 1},
        timeout_s=20,
    ) as bot:
        if bot.agent != args.agent:
            raise ValueError("Select an explicit owning agent name")
        return await read_pair(
            bot,
            memory_key=args.memory_key,
            source_uri=args.source_uri,
            section_path=args.section,
            user_id=args.user_id,
            session_id=args.session_id,
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--org", required=True)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--agent", required=True, help="Agent identity that owns the fact")
    parser.add_argument("--user-id", default="-")
    parser.add_argument("--session-id", default="-")
    parser.add_argument("--memory-key", required=True)
    parser.add_argument("--source-uri", required=True)
    parser.add_argument("--section", nargs="+", required=True, help="Heading path components")
    args = parser.parse_args()
    try:
        result = asyncio.run(run(args))
    except (LoomError, httpx.HTTPError, ValueError):
        print("Read failed; inspect the deployment privately. No response body is logged.")
        return 1
    print(json.dumps(summary(result), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
