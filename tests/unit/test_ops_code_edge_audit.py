"""scripts/ops/code_edge_audit.py stays a read-only, attributed census: every
statement is a SELECT, each namespace's reads carry its log_comment tag, the
namespace filter reaches the server, and the report file is replaced whole."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from scripts.ops import code_edge_audit as audit
from server.core.ch import query_tag_var


class _CH:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any] | None, str]] = []

    async def query_all(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        self.calls.append((sql, params, query_tag_var.get()))
        if "version()" in sql:
            return [{"v": "26.8"}]
        if "GROUP BY org, namespace" in sql:
            return [{"org": "o", "namespace": "code-x", "rows": 3}]
        if "disposition" in sql and "GROUP BY" in sql:
            return []
        return [{k: 0 for k in _ZERO}]


_ZERO = (
    "names",
    "sites",
    "live_calls",
    "live_calls_le_06",
    "dangling_le_06",
    "targets_le_06",
    "paths_le_06",
    "dangling_any",
    "targets_any",
)


def _args(**kw: Any) -> argparse.Namespace:
    return audit._parser().parse_args([f"--{k}={v}" for k, v in kw.items()])


def test_every_statement_in_the_module_is_a_read() -> None:
    sqls = [v for k, v in vars(audit).items() if k.startswith("_") and isinstance(v, str)]
    sqls = [s for s in sqls if "loom." in s]
    assert len(sqls) >= 6
    assert all(audit._READ_LEAD.match(s) for s in sqls)


def test_a_write_never_reaches_the_server() -> None:
    ch = _CH()
    with pytest.raises(ValueError, match="only reads"):
        asyncio.run(audit._read(ch, "INSERT INTO loom.code_edges SELECT 1"))
    assert ch.calls == []


def test_each_namespace_is_attributed_and_the_filter_is_pushed_down() -> None:
    ch = _CH()
    rc, report = asyncio.run(audit.audit(ch, _args(org="o", namespace="code-x")))
    assert rc == 0 and report["namespaces"][0]["namespace"] == "code-x"
    listing = next(c for c in ch.calls if "GROUP BY org, namespace" in c[0])
    assert listing[1] == {"org": "o", "nss": ["code-x"]}
    per_ns = [tag for sql, _, tag in ch.calls if "{ns:String}" in sql]
    assert per_ns and set(per_ns) == {"ops.code_edge_audit o/code-x"}
    assert query_tag_var.get() == ""  # the tag does not leak past the namespace


def test_the_report_file_is_replaced_whole(tmp_path: Path) -> None:
    out = tmp_path / "r.json"
    out.write_text("old")
    audit.write_report(out, {"b": 1, "a": 2})
    assert json.loads(out.read_text()) == {"a": 2, "b": 1}
    assert [p.name for p in tmp_path.iterdir()] == ["r.json"]
