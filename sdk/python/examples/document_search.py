"""Search two Markdown documents, cite the passages, and abstain on a question
they do not answer.

    LOOM_BASE_URL=https://loom.example LOOM_API_KEY=… LOOM_ORG=acme python document_search.py

Registers a throwaway namespace (`py-example-*`), as agent_memory.py does, adds two
short documents with `knowledge.add`, searches them and prints each hit as a
citation, then asks an off-topic question through `search.query`. It deletes both
documents before it exits, and exits 1 when a check fails.

Run it with a real embedder (the released stack's `local` one, or a provider's): a
`fake-ngram-*` embedder ranks by character n-grams, so the checks pass or fail by
chance.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import sys

from loom import Agent, LoomError

# Two documents on separate subjects, each with its own source_uri and title.
DOCS = {
    "docs://example/refunds.md": (
        "Refund policy",
        "# Refund policy\n\n"
        "## Eligibility\n\n"
        "Customers can return an unopened item within 30 days of delivery for a full refund.\n"
        "Opened items are refunded at 80% of the price.\n\n"
        "## How refunds are paid\n\n"
        "Refunds go back to the original payment method within 5 business days.\n",
    ),
    "docs://example/shipping.md": (
        "Shipping times",
        "# Shipping times\n\n"
        "## Domestic\n\n"
        "Standard domestic orders arrive in 3 to 5 business days.\n\n"
        "## International\n\n"
        "International orders ship by air and arrive in 7 to 14 business days.\n",
    ),
}

# A question on each document's subject, and one neither answers.
SUBJECT_QUERIES = {
    "docs://example/refunds.md": "How many days do customers have to return an item for a refund?",
    "docs://example/shipping.md": "How long does international shipping take?",
}
OFF_TOPIC = "What is the boiling point of water at the top of Mount Everest?"


async def main() -> int:
    org = os.environ.get("LOOM_ORG", "dev")
    namespace = os.environ.get("LOOM_NAMESPACE") or f"py-example-{secrets.token_hex(3)}"
    async with Agent(org, namespace, "docs-assistant") as bot:
        # A throwaway namespace for the example. Registering one that already exists
        # takes its admin, so a refusal here means "it is there already".
        try:
            await bot.call(
                "namespace.register", {"owner_email": "example@loom.test", "kind": "bench"}
            )
        except LoomError:
            pass

        try:
            failures = await add_search_abstain(bot)
        finally:
            # Leave nothing behind, even when a step failed.
            for source_uri in DOCS:
                await bot.knowledge.delete(source_uri)

    for failure in failures:
        print(f"FAIL: {failure}")
    print("all checks passed" if not failures else f"{len(failures)} check(s) failed")
    return 1 if failures else 0


async def add_search_abstain(bot: Agent) -> list[str]:
    failures: list[str] = []

    # 1. Add the two documents.
    for source_uri, (title, content) in DOCS.items():
        await bot.knowledge.add(content, source_uri=source_uri, title=title)
        print(f"added {source_uri} ({title})")

    # 2. Search each subject and print the hits as citations. `namespace_scope: "this"`
    # keeps documents in the caller's other namespaces out of every check.
    for source_uri, query in SUBJECT_QUERIES.items():
        hits = await bot.knowledge.search(query, top_k=3, namespace_scope="this")
        print(f"\nQ: {query}")
        for h in hits:
            first_line = next((ln for ln in h.content.splitlines() if ln.strip()), "")
            cite = f"[{h.source_uri}#{h.chunk_idx}] {h.section or '-'}"
            print(f"  {cite}: {first_line}  ({h.similarity:.2f})")
        if not hits or hits[0].source_uri != source_uri:
            failures.append(f"{query!r}: the top hit is not {source_uri}")

    # 3. Ask what neither document says. knowledge.search alone would still return its
    # nearest chunks, so this goes through search.query, which drops knowledge hits
    # below the similarity floor (LOOM_MCP_LOOM_SEARCH_KNOWLEDGE_MIN_SIMILARITY).
    answer = await bot.client.search.query({"query": OFF_TOPIC, "namespace_scope": "this"})
    passages = (answer.get("knowledge") or {}).get("results") or []
    print(f"\nQ: {OFF_TOPIC}")
    if passages:
        for p in passages:
            print(
                f"  unexpected passage [{p['source_uri']}#{p['chunk_idx']}] "
                f"similarity {float(p.get('similarity') or 0):.2f}"
            )
        failures.append(f"{OFF_TOPIC!r}: {len(passages)} knowledge passage(s) came back")
    else:
        print("  no knowledge passage came back: the documents do not answer it")
    return failures


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
