"""FixedString hashes and shas come back from the client as NUL-padded bytes;
every code_repo reader turns them into hex text with the one decoder
(`sql._hex`), never `str(bytes)` or the text 'None'."""

from __future__ import annotations

import asyncio
from typing import Any

from server.code.store.index import CodeRepo

SHA = "a" * 40


class _CH:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    async def query_all(self, sql: str, params: dict[str, Any] | None = None) -> list[dict]:
        return self.rows


def test_merge_commits_decode_the_fixed_string() -> None:
    repo = CodeRepo(_CH([{"pr_number": 7, "sha": SHA.encode()}]))  # type: ignore[arg-type]
    assert asyncio.run(repo.merge_commits_of("o", "n", [7])) == {7: SHA}
    one = CodeRepo(_CH([{"merge_commit_sha": SHA.encode()}]))  # type: ignore[arg-type]
    assert asyncio.run(one.merge_commit_of("o", "n", 7)) == SHA


def test_live_file_hashes_decode_padding_and_never_render_none() -> None:
    rows = [
        {"path": "a.py", "content_hash": b"ab" + b"\x00" * 62},
        {"path": "b.py", "content_hash": None},
    ]
    repo = CodeRepo(_CH(rows))  # type: ignore[arg-type]
    assert asyncio.run(repo.live_file_hashes("o", "n")) == {"a.py": "ab", "b.py": ""}
