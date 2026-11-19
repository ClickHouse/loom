"""Read indexed code through the same dispatcher a model's selected tool call uses.

Needs an existing code index and a token with read access. No agent model is called.
Search uses the server's configured embedding provider. --help sends no requests.
"""

from __future__ import annotations

import argparse
import asyncio

from loom import Agent


async def lookup(args: argparse.Namespace) -> None:
    async with Agent(args.org, args.memory_namespace, "code-assistant") as bot:
        tools = bot.tools(
            code_namespace=args.code_namespace,
            only=["loom_code_lookup"],
            max_result_chars=12_000,
        )
        # In a model loop, pass tools.definitions("anthropic") to the provider,
        # then dispatch the model's selected name and arguments to execute().
        selected = {"kind": args.kind, "target": args.target}
        if args.depth is not None:
            selected["depth"] = args.depth
        print(await tools.execute("loom_code_lookup", selected))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target")
    parser.add_argument("--org", required=True)
    parser.add_argument("--memory-namespace", required=True)
    parser.add_argument("--code-namespace", required=True)
    parser.add_argument(
        "--kind", choices=["symbol", "search", "callers", "impact"], default="symbol"
    )
    parser.add_argument("--depth", type=int)
    asyncio.run(lookup(parser.parse_args()))


if __name__ == "__main__":
    main()
