"""The PR pack's `qa` section: open issues naming the
touched functions and the PR's own QA line, read live from the tracker namespace
bound to the code namespace; absent when unbound, restricted (a code
namespace's cache must never hold a restricted tracker namespace's rows), or
unavailable."""

from __future__ import annotations

from typing import Any

import pytest

import server.code.app.index.packs as packs
import server.identity.app.namespace_classification as ncls
import server.tracker.app.binding as binding
from server.tracker.domain.errors import TrackerUnavailable

ORG, NS, QA = "clickhouse", "code-clickhouse", "tracker-clickhouse"
REPO = "ClickHouse/ClickHouse"
SYMS = [
    {"qualified_name": "DB::MergeTreeData::loadDataParts", "path": "src/x.cpp", "symbol_id": "s1"},
    {"qualified_name": "DB::get", "path": "src/x.cpp", "symbol_id": "s2"},
]


class _Source:
    def __init__(self, *, issues=None, pr=None, down=False) -> None:
        self.issues, self.pr, self.down = issues or [], pr, down
        self.naming_calls: list[list[str]] = []

    async def open_issues_naming(self, org, ns, tokens, *, limit=5):
        if self.down:
            raise TrackerUnavailable("OPEN")
        self.naming_calls.append(list(tokens))
        return list(self.issues)

    async def items_by_number(self, org, ns, nums):
        return {n: self.pr for n in nums if self.pr}

    async def edges_from(self, org, ns, refs, rels=None):
        return [
            {
                "src_ref": f"{REPO}#77",
                "rel_type": "fixes",
                "dst_ref": f"{REPO}#12",
                "direction": "out",
            }
        ]


@pytest.fixture
def wired(monkeypatch):
    monkeypatch.setattr(binding, "tracker_namespace_for", lambda org, ns: QA if ns == NS else "")
    state: dict[str, Any] = {"restricted": False}

    async def _restricted(org, ns):
        return bool(state["restricted"])

    monkeypatch.setattr(ncls, "is_restricted", _restricted)
    holder: dict[str, Any] = {}
    import server.tracker.store.source as qs

    monkeypatch.setattr(qs, "TrackerSource", lambda org, ns: holder["src"])

    def _use(src: _Source, *, restricted: bool = False) -> _Source:
        holder["src"] = src
        state["restricted"] = restricted
        return src

    return _use


@pytest.mark.asyncio
async def test_section_renders_pr_line_and_open_issues_naming_frames(wired) -> None:
    src = wired(
        _Source(
            issues=[
                {
                    "number": 300,
                    "title": "Crash in loadDataParts on restart",
                    "severity_level": "critical",
                    "bisect_outcome": "CONVERGED",
                }
            ],
            pr={
                "repo": REPO,
                "number": 77,
                "is_pr": 1,
                "has_test": 0,
                "ci_result": "success",
                "reviewers": [],
                "labels": [],
            },
        )
    )
    lines = await packs._tracker_lines(ORG, NS, 77, SYMS)
    assert lines[:2] == ["", packs.SECTION_HEADERS["qa"]]
    assert lines[2].startswith("- [tracker] #77: adds no test; CI success; fixes #12")
    assert lines[3] == "- open #300 [critical, bisect CONVERGED]: Crash in loadDataParts on restart"
    assert src.naming_calls == [["loadDataParts"]]  # `get` is generic, never a token
    pack = "\n".join(["# PR #77: t", *lines, "", "## Changed files", "- src/x.cpp"])
    assert packs.filter_sections(pack, ["qa"]).count("## ") == 1


@pytest.mark.asyncio
async def test_section_is_absent_when_unbound_restricted_unavailable_or_empty(wired) -> None:
    src = wired(_Source(issues=[{"number": 1, "title": "t"}]))
    assert await packs._tracker_lines(ORG, "code-other", 77, SYMS) == []
    wired(src, restricted=True)
    assert await packs._tracker_lines(ORG, NS, 77, SYMS) == [] and src.naming_calls == []
    wired(_Source(down=True))
    assert await packs._tracker_lines(ORG, NS, 77, SYMS) == []
    wired(_Source(issues=[], pr=None))
    assert await packs._tracker_lines(ORG, NS, 0, SYMS) == []
