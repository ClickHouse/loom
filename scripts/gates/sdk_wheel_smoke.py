#!/usr/bin/env python3
"""Smoke-test an installed clickhouse-loom wheel, away from the repository.

The SDK's unit tests run inside the server's 3.12 environment, whose own
dependencies can hide one the SDK forgot to declare, and whose interpreter
hides stdlib the SDK's Python floor lacks. This script runs in a clean venv
holding only the wheel and its declared deps, on any supported Python: it
imports every submodule (an adapter whose framework is an absent extra must
fail with an ImportError naming it), sends a `memory.search` and a failing `memory.get`
through a mocked transport, and, given the wheel file, checks it ships
`py.typed` and the license.

    python scripts/gates/sdk_wheel_smoke.py [dist/clickhouse_loom-*.whl]

Checks raise SmokeFailure rather than `assert`, so `python -O` cannot turn
the smoke test into a pass.
"""

from __future__ import annotations

import asyncio
import importlib
import pkgutil
import sys
import zipfile

import httpx
import loom
from loom import Agent, NotFound

HIT = {
    "memory_key": "k",
    "memory_type": "semantic",
    "content_excerpt": "x",
    "distance": 0.1,
    "similarity": 0.9,
    "score_components": {"sim": 0.9},
}


class SmokeFailure(Exception):
    pass


def check(ok: bool, message: str) -> None:
    if not ok:
        raise SmokeFailure(message)


def check_wheel(path: str) -> None:
    with zipfile.ZipFile(path) as wheel:
        names = wheel.namelist()
    check("loom/py.typed" in names, f"{path} lacks loom/py.typed")
    check(any(n.endswith(".dist-info/licenses/LICENSE") for n in names), f"{path} lacks LICENSE")


def handler(req: httpx.Request) -> httpx.Response:
    if req.url.path == "/v1/memory.search":
        return httpx.Response(200, json={"results": [HIT]})
    return httpx.Response(
        404,
        json={"detail": {"code": "RESOURCE_NOT_FOUND", "message": "no such key"}},
        headers={"X-Request-Id": "r1"},
    )


async def exercise() -> None:
    async with Agent("o", "n", "bot", api_key="k", base_url="http://loom.test") as agent:
        await agent.transport._client.aclose()
        agent.transport._client = httpx.AsyncClient(
            base_url="http://loom.test", transport=httpx.MockTransport(handler)
        )
        hits = await agent.memory.search("q")
        got = [(h.memory_key, h.score_components) for h in hits]
        check(got == [("k", {"sim": 0.9})], f"memory.search decoded {got!r}")
        try:
            await agent.call("memory.get", {"memory_key": "missing"})
        except NotFound as e:
            got_err = (e.code, e.status, e.request_id)
            check(got_err == ("RESOURCE_NOT_FOUND", 404, "r1"), f"NotFound carried {got_err!r}")
        else:
            raise SmokeFailure("a 404 did not raise NotFound")


def import_submodules() -> None:
    for mod in pkgutil.walk_packages(loom.__path__, "loom."):
        try:
            importlib.import_module(mod.name)
        except ImportError as e:
            # A framework adapter needs its extra, and its error names it.
            check("pip install 'clickhouse-loom[" in str(e), f"{mod.name}: {e}")


def main(argv: list[str]) -> int:
    try:
        check(
            "site-packages" in loom.__file__, f"imported {loom.__file__}, not the installed wheel"
        )
        for wheel in argv:
            check_wheel(wheel)
        import_submodules()
        asyncio.run(exercise())
    except SmokeFailure as e:
        print(f"sdk-wheel-smoke: FAIL — {e}", file=sys.stderr)
        return 1
    print(f"clickhouse-loom {loom.__version__} ok on Python {sys.version.split()[0]}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
