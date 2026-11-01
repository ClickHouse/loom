"""The Python and TypeScript SDKs expose the same operations under the same names.

Both are generated from `server.operations.registry.ALL_OPERATIONS`:
- the op tables (`sdk/python/loom/ops.py`, `sdk/typescript/src/generated/ops.ts`)
  name the same ops;
- every op has one method in each SDK, at the same group, the method name
  camel-cased in TypeScript (`agent.client.tracker.source_add` ↔
  `loom.tracker.sourceAdd`), and a Python keyword spelled with a trailing
  underscore (`agent.client.memory.import_` ↔ `loom.memory.import`);
- each Python method calls exactly its own op.

The `--check` of each generator keeps the files current; this test keeps them
equal to each other.
"""

from __future__ import annotations

import asyncio
import keyword
import re
from pathlib import Path
from typing import Any

import pytest
from loom import Agent, OpsClient
from loom.ops import OPS as PY_OPS

from scripts.codegen.sdk_py_client_gen import split_op
from server.operations.registry import ALL_OPERATIONS

ROOT = Path(__file__).resolve().parents[2]
TS_OPS = ROOT / "sdk" / "typescript" / "src" / "generated" / "ops.ts"
_TS_ROW = re.compile(r'\{ name: "([^"]+)", group: "([^"]+)", method: "([^"]+)"')


def _ts_rows() -> dict[str, tuple[str, str]]:
    rows = {name: (group, method) for name, group, method in _TS_ROW.findall(TS_OPS.read_text())}
    assert rows, f"no OPS rows parsed from {TS_OPS}"
    return rows


def _camel(snake: str) -> str:
    head, *rest = snake.split("_")
    return head + "".join(p[:1].upper() + p[1:] for p in rest)


def _python_method(op: str) -> tuple[str, str]:
    group, _, rest = op.partition(".")
    method = rest.replace(".", "_")
    return group, (f"{method}_" if keyword.iskeyword(method) else method)


def test_the_generator_names_methods_as_this_test_does() -> None:
    assert split_op("tracker.source.add") == ("tracker", "source_add")
    # A keyword takes a trailing underscore; camel-casing drops it again for TypeScript.
    assert split_op("memory.import") == ("memory", "import_") and _camel("import_") == "import"
    assert all(split_op(name) == _python_method(name) for name in PY_OPS)


class _Recorder:
    consumer = ""

    def __init__(self) -> None:
        self.paths: list[str] = []

    async def post(self, path: str, payload: dict[str, Any], key: Any, **_: Any) -> dict[str, Any]:
        self.paths.append(path)
        return {}

    async def close(self) -> None:
        return None


@pytest.fixture
def agent() -> Agent:
    a = Agent(org="o", namespace="n", agent="a", api_key="k", base_url="http://x")
    a.transport = _Recorder()  # type: ignore[assignment]
    return a


def test_both_op_tables_name_every_served_op() -> None:
    served = {op.name for op in ALL_OPERATIONS}
    assert set(PY_OPS) == served
    assert set(_ts_rows()) == served


def test_every_op_has_the_same_group_and_method_in_both_sdks(agent: Agent) -> None:
    assert isinstance(agent.client, OpsClient)
    mismatched: list[str] = []
    for op, (ts_group, ts_method) in sorted(_ts_rows().items()):
        group, method = _python_method(op)
        client = getattr(agent.client, group, None)
        if ts_group != group or _camel(method) != ts_method:
            mismatched.append(f"{op}: python {group}.{method} vs typescript {ts_group}.{ts_method}")
        elif not callable(getattr(client, method, None)):
            mismatched.append(f"{op}: no agent.client.{group}.{method}")
    assert not mismatched, "\n".join(mismatched)


def test_each_python_method_calls_its_own_op(agent: Agent) -> None:
    recorder = agent.transport
    assert isinstance(recorder, _Recorder)
    for op in sorted(PY_OPS):
        group, method = _python_method(op)
        asyncio.run(getattr(getattr(agent.client, group), method)())
        assert recorder.paths[-1] == f"/v1/{op}", op


def test_the_blocking_twin_exposes_the_same_methods() -> None:
    from loom import SyncAgent

    sa = SyncAgent(org="o", namespace="n", agent="a", api_key="k", base_url="http://x")
    try:
        sa._agent.transport = _Recorder()  # type: ignore[assignment]
        assert sa.client.tracker.source_add({"name": "x"}) == {}
        assert sa._agent.transport.paths == ["/v1/tracker.source.add"]
    finally:
        sa.close()
