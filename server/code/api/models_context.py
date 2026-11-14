"""Orientation bundles: code.context_pack, code.map and the precomputed
per-PR packs code.context_packs lists.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, PrivateAttr

from server.code.api.models_common import _CodeIdentity, _LessonsOut, _ResultLessons

# ============================================================
# code.context_pack / code.map
# ============================================================


class CodeContextPackInput(_CodeIdentity, _ResultLessons):
    # Indexed merged PR -> precomputed pack; 0 -> orientation map; unknown PR
    # -> explicit abstention. ge=0 because a negative number would reach
    # ClickHouse as a UInt32 bind and surface as a 500.
    pr_number: int = Field(default=0, ge=0)
    # The change about to be made ("add a new tracker op"). Without a PR, a task gets a
    # briefing — where it lives, what such a change also touches, the merged PRs that
    # did one — instead of the map.
    task: str = Field(default="", max_length=500)
    # Token budget for `context` (200..8000): a precomputed pack is cut to it, a task briefing
    # or orientation map is built within it.
    token_budget: int = Field(default=2500, ge=200, le=8000)
    # Precomputed packs only. Empty = whole pack. Otherwise keep the header
    # plus these sections, in pack order: symbols (touched symbols + callers),
    # co_change (usually changed together), tests, reviews (what reviewers
    # said on these files before), prs, files. A hypothesis-forming agent
    # treats the callers/co-change block as leads and chases every one (a
    # large cost increase); a lookup-and-act agent wants it. The caller knows which kind it is.
    sections: list[Literal["symbols", "co_change", "tests", "reviews", "prs", "qa", "files"]] = (
        Field(default_factory=list)
    )


class CodeContextPackOutput(_LessonsOut):
    # The pack as Markdown text; for `unavailable`, a short note saying the PR has no pack yet.
    context: str
    # Estimated tokens of `context`.
    tokens_used: int
    # precomputed (the PR's stored pack) | task (a briefing for `task`) | orientation (the
    # repository map) | unavailable (no pack for the PR).
    source: Literal["precomputed", "task", "orientation", "unavailable"]
    # True when a precomputed pack was cut to `token_budget`; false for the other sources.
    truncated: bool = False
    # The PR's merge commit, for a precomputed pack; '' otherwise.
    merge_commit_sha: str = ""
    # Id of this call's trace row; pass it to `code.outcome`.
    trace_run_id: str = ""
    # The files a task briefing names, best first: what its `lessons` are read for.
    _paths: list[str] = PrivateAttr(default_factory=list)


class CodeMapInput(_CodeIdentity):
    # Token budget for the map (200..4000); entries past it are dropped.
    token_budget: int = Field(default=1000, ge=200, le=4000)
    # Only modules and hubs under this repo-relative prefix; '' = the whole repository.
    module_prefix: str = ""


class CodeMapOutput(BaseModel):
    # The repository map as Markdown: module cards and call-graph hubs, then the most-called
    # symbols when the fan-in stats exist. Only the header line means nothing matched.
    map: str
    # Estimated tokens of `map`.
    tokens_used: int
    # The fast-tier watermark (`git_head`) the map reflects; '' before the first pass.
    commit_sha: str = ""
    # Id of this call's trace row; pass it to `code.outcome`.
    trace_run_id: str = ""


# ============================================================
# code.context_packs — list precomputed per-PR packs
# ============================================================


class CodeContextPacksInput(_CodeIdentity):
    # At most this many packs per page, newest first (1..200).
    limit: int = Field(default=50, ge=1, le=200)
    # Packs to skip before the page starts.
    offset: int = Field(default=0, ge=0)
    # Optional exact PR filter (0 = all).
    pr_number: int = 0


class CodeContextPackRow(BaseModel):
    pr_number: int
    merge_commit_sha: str = ""
    pack_tokens: int = 0
    created_at: str = ""
    title: str = ""
    author: str = ""
    merged_at: str = ""


class CodeContextPacksOutput(BaseModel):
    # Live packs, newest built first; title, author and merged_at are '' when the PR history
    # lacks the PR.
    rows: list[CodeContextPackRow]
    # Every live pack matching the filter, not only this page.
    total: int = 0
