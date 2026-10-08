"""A name the repository does not define is looked for in the
namespaces its entry depends on — presence rows under the caller's own
authorization; nothing asked for a location, a symbol_id or an entry without
dependencies; an authorization failure answers nothing, not an error."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from server.code.api.models_symbol import CodeSymbolInput
from server.code.app.symbol import symbol as h


def _cfgs(deps: tuple[str, ...]) -> list[Any]:
    return [
        SimpleNamespace(org="clickhouse", namespace="code-lumen", depends_on=deps),
        SimpleNamespace(org="clickhouse", namespace="code-weave", depends_on=()),
    ]


def _run(name: str, *, deps=("code-weave",), rows=None, fail=False, symbol_id=""):
    calls: list[Any] = []

    async def fake_run(inp, namespaces, sub_for, single, *, row_of, op):
        calls.append((list(namespaces), op, sub_for(namespaces[0]).namespace))
        if fail:
            raise PermissionError("not a member")
        return (
            rows
            if rows is not None
            else [
                {
                    "namespace": ns,
                    "verdict": "present",
                    "path": "include/weave/util/assert.h",
                    "line": 33,
                }
                for ns in namespaces
            ]
        )

    def fake_summarize(rs):
        return {
            "present": [r["namespace"] for r in rs if r.get("verdict") == "present"],
            "absent": [],
        }

    inp = CodeSymbolInput(org="clickhouse", namespace="code-lumen", name=name, symbol_id=symbol_id)
    with (
        patch("server.code.domain.index.repos.configured_repos", return_value=_cfgs(deps)),
        patch("server.code.app.presence.run", new=fake_run),
        patch("server.code.app.presence.summarize", new=fake_summarize),
    ):
        out = asyncio.run(h._dependency_presence(inp, "clickhouse", "code-lumen"))
    return out, calls


def test_a_miss_asks_every_dependency_once_as_a_plain_lookup() -> None:
    (rows, summary), calls = _run("weave::fibers::Future")
    assert calls == [(["code-weave"], "code.symbol", "code-weave")]
    assert rows[0]["namespace"] == "code-weave" and summary == {
        "present": ["code-weave"],
        "absent": [],
    }


def test_nothing_is_asked_without_dependencies_a_location_or_an_id() -> None:
    assert _run("weave::x", deps=()) == (([], {}), [])
    assert _run("src/a.cpp:12") == (([], {}), [])
    assert _run("weave::x", symbol_id="abc") == (([], {}), [])


def test_the_entry_itself_is_never_a_dependency_and_authz_failure_answers_nothing() -> None:
    (rows, summary), calls = _run("weave::x", deps=("code-lumen", "code-weave"))
    assert calls[0][0] == ["code-weave"], "the namespace does not depend on itself"
    (rows, summary), calls = _run("weave::x", fail=True)
    assert (rows, summary) == ([], {}) and len(calls) == 1
