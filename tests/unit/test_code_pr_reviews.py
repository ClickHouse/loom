"""Review comments on merged PRs: parsing, rows, and the pack section."""

from __future__ import annotations

import asyncio
from typing import Any

from server.clients.github import parse_review_comment
from server.code.app.index.packs import (
    SECTION_HEADERS,
    build_pr_pack,
    estimate_tokens,
    filter_sections,
    fit_pack_budget,
    render_review_lines,
)
from server.code.store.index import CodeRepo
from server.code.store.pr_reviews_rows import review_rows


def _gh(**over):
    base = {
        "id": 555,
        "path": "src/Core/Settings.cpp",
        "line": 42,
        "body": "x" * 50,
        "in_reply_to_id": None,
        "created_at": "2026-09-01T10:00:00Z",
        "user": {"login": "alice", "type": "User"},
    }
    base.update(over)
    return base


def test_parse_flags_bots_and_truncates_and_falls_back_to_original_line() -> None:
    c = parse_review_comment(_gh())
    assert c and c.comment_id == 555 and c.path == "src/Core/Settings.cpp" and c.line == 42
    assert not c.is_bot and c.in_reply_to == 0
    bot = parse_review_comment(_gh(user={"login": "clickhouse-gh[bot]", "type": "Bot"}))
    assert bot and bot.is_bot
    suffix_bot = parse_review_comment(
        _gh(user={"login": "robot-ch-test-poll[bot]", "type": "User"})
    )
    assert suffix_bot and suffix_bot.is_bot
    long = parse_review_comment(_gh(body="y" * 5000))
    assert long and len(long.body) == 2000
    outdated = parse_review_comment(_gh(line=None, original_line=7))
    assert outdated and outdated.line == 7
    assert parse_review_comment(_gh(body="")) is None
    assert parse_review_comment({"body": "no id"}) is None
    reply = parse_review_comment(_gh(in_reply_to_id=554))
    assert reply and reply.in_reply_to == 554


def test_review_rows_carry_identity_and_columns() -> None:
    c = parse_review_comment(_gh())
    assert c
    rows = review_rows("o", "ns", 118136, [c])
    assert rows[0]["org"] == "o" and rows[0]["pr_number"] == 118136
    assert set(rows[0]) >= {
        "comment_id",
        "path",
        "line",
        "author",
        "is_bot",
        "body",
        "in_reply_to",
        "comment_created_at",
    }


def test_render_review_lines_one_per_path_newest_first_excerpted() -> None:
    reviews = [
        {
            "pr_number": 2,
            "path": "src/a.cpp",
            "line": 10,
            "author": "bob",
            "body": "Please  add a\nchangelog entry when you touch settings " + "z" * 200,
        },
        {
            "pr_number": 1,
            "path": "src/a.cpp",
            "line": 3,
            "author": "ann",
            "body": "older on same file" * 4,
        },
        {
            "pr_number": 3,
            "path": "src/b.cpp",
            "line": 0,
            "author": "cy",
            "body": "Use the X pattern here instead of Y please",
        },
    ]
    lines = render_review_lines(reviews, cap=5, excerpt_chars=60)
    assert len(lines) == 2
    assert lines[0].startswith('- src/a.cpp:10 — "Please add a changelog entry')
    assert lines[0].endswith('…" (#2, @bob)')
    assert lines[1].startswith('- src/b.cpp — "Use the X pattern')


def test_pr_pack_gets_a_reviews_section_that_the_filter_can_keep() -> None:
    class _NoRows:
        async def query_all(self, *_a, **_k):
            return []

    async def review_comments_for(org, ns, paths, *, exclude_pr, limit):
        assert exclude_pr == 7 and "src/a.cpp" in paths
        return [
            {
                "pr_number": 5,
                "path": "src/a.cpp",
                "line": 12,
                "author": "ann",
                "body": "Settings need an entry in SettingsChangesHistory.cpp as well",
            }
        ]

    repo: Any = CodeRepo(_NoRows())  # type: ignore[arg-type]
    repo.review_comments_for = review_comments_for
    pack = asyncio.run(
        build_pr_pack(
            repo,
            "o",
            "ns",
            pr_number=7,
            title="t",
            commit_sha="c" * 40,
            changed_paths=["src/a.cpp"],
            changed_symbols=[],
        )
    )
    assert SECTION_HEADERS["reviews"] in pack
    assert '- src/a.cpp:12 — "Settings need an entry' in pack
    kept = filter_sections(pack, ["reviews"])
    assert SECTION_HEADERS["reviews"] in kept and SECTION_HEADERS["files"] not in kept


def test_cached_pack_is_trimmed_to_the_callers_budget_without_losing_sections() -> None:
    pack = (
        "# PR 7 — small change\n"
        + SECTION_HEADERS["tests"]
        + "\n"
        + "\n".join(f"- tests/unit/test_case_{i}.py covers changed symbols" for i in range(80))
        + "\n"
        + SECTION_HEADERS["files"]
        + "\n"
        + "\n".join(f"- server/feature_{i}.py" for i in range(20))
    )
    fitted, truncated = fit_pack_budget(pack, 200)
    assert truncated and estimate_tokens(fitted) <= 200
    assert SECTION_HEADERS["tests"] in fitted and SECTION_HEADERS["files"] in fitted
    assert "[context truncated to token budget]" in fitted
    assert fit_pack_budget("# PR 7", 200) == ("# PR 7", False)


def test_empty_fetch_writes_a_marker_row_so_the_pr_is_not_refetched() -> None:
    from server.code.store.pr_reviews_rows import FETCHED_MARKER_ID, fetched_marker_row

    rows = review_rows("o", "ns", 4242, [])
    assert len(rows) == 1 and rows[0]["comment_id"] == FETCHED_MARKER_ID == 0
    assert rows[0]["pr_number"] == 4242 and rows[0]["body"] == "" and rows[0]["is_bot"] is False
    assert fetched_marker_row("o", "ns", 1)["path"] == ""
