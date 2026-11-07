"""The stored-revision read of a document source, run on clickhouse-local against the
real `loom.documents`: on a full listing it says which stored copies still hold a
picture's bytes as a reference definition, the shapes a native document's raw export
writes, and not a definition of a link."""

from __future__ import annotations

import json

import pytest

from server.core.hashing import sha256_text
from server.knowledge.domain.convert import _DATA_DEFINITION
from server.knowledge.domain.sources import SYNC_WORKER_ACTOR, source_tag
from server.knowledge.store import sources as store
from tests.unit.clickhouse_local import CLICKHOUSE, LocalCH, ddl

pytestmark = pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")

DOCS = {
    "angle": "# A\n\n![][image1]\n\n[image1]: <data:image/png;base64,AAAA>\n",
    "bare": "# B\n\n[image1]: data:image/png;base64,AAAA\n",
    "indented": "# C\n\n   [image2]: <data:image/jpeg;base64,BBBB>\n",
    "link": "# D\n\nSee [the spec][spec].\n\n[spec]: https://example.com/spec\n",
    "inline": "# E\n\nThe [image1]: <data:image/png;base64,AAAA> sits inside a line.\n",
}


def _row(name: str, content: str) -> str:
    meta = json.dumps({"ingested_from": name, "source_revision": "r1"}).replace("'", "\\'")
    text = content.replace("'", "\\'")
    return (
        "INSERT INTO loom.documents (org, namespace, source_uri, content, tags,"
        " metadata, version, actor) VALUES ('o', 'ns',"
        f" 'https://docs.example.com/document/d/{name}/edit', '{text}',"
        f" ['{source_tag('drive')}'], '{meta}', 1, '{SYNC_WORKER_ACTOR}')"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("pictures", [True, False])
async def test_a_full_listing_learns_which_stored_copies_hold_pictures_bytes(
    pictures: bool,
) -> None:
    ch = LocalCH(ddl("documents"), *(_row(n, c) for n, c in DOCS.items()))
    rows = await store.source_revisions(
        "o", "ns", "drive", searchable_only=False, pictures=pictures, ch=ch
    )
    flagged = {r["file_id"] for r in rows if r["inline_pictures"]}
    assert len(rows) == len(DOCS)
    assert flagged == ({"angle", "bare", "indented"} if pictures else set())
    hashes = {r["file_id"]: r["stored_content_sha256"] for r in rows}
    expected = {n: sha256_text(c) if n in flagged else "" for n, c in DOCS.items()}
    assert hashes == expected


# Each line, whether the stored check flags it and whether the conversion drops it: the
# check flags every line the conversion drops, and a line that only starts like a
# definition too, which the stored-copy comparison then finds unchanged.
LINES = [
    ("[image1]: <data:image/png;base64,AAAA>", True, True),
    ("[image1]: data:image/png;base64,AAAA", True, True),
    ("   [image2]: <data:image/jpeg;base64,BBBB>  ", True, True),
    ('[image3]: <data:image/png;base64,CCCC> "Figure 1"', True, True),
    ("[image5]: <data:image/png;base64,EEEE> (Figure 2)", True, True),
    ("[image4]: <data:image/png;base64,DDDD>\r", True, True),
    ("[Step 1]: data: the raw input", True, False),
    ("[spec]: https://example.com/spec", False, False),
    ("The [image1]: <data:image/png;base64,AAAA> sits inside a line.", False, False),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("line", "flagged", "dropped"), LINES)
async def test_the_stored_check_flags_every_line_the_conversion_drops(
    line: str, flagged: bool, dropped: bool
) -> None:
    rows = await LocalCH().query_all(
        "SELECT match({t:String}, {pic:String}) AS m", {"t": line, "pic": store._INLINE_PICTURE}
    )
    assert bool(rows[0]["m"]) is flagged
    assert (_DATA_DEFINITION.fullmatch(line) is not None) is dropped
