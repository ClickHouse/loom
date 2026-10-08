"""Every unit-test snapshot has one owner, a documented update command and
no orphan.

`tests/unit/snapshots/README.md` has one table row per snapshot. The row
names the owning test and the command that regenerates the file. A snapshot
nobody reads, a row for a deleted file, or an owner that no longer reads its
file or honours its update switch would let a pinned surface drift silently,
so each of these fails here.
"""

from __future__ import annotations

import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
SNAPSHOTS = HERE / "snapshots"
README = SNAPSHOTS / "README.md"
_ROW = re.compile(
    r"^\|\s*`(?P<file>[^`]+)`\s*\|\s*`(?P<owner>test_[a-z0-9_]+\.py)`\s*\|(?P<rest>.*)\|$"
)


def _rows() -> dict[str, tuple[str, str]]:
    rows = {}
    for line in README.read_text(encoding="utf-8").splitlines():
        m = _ROW.match(line)
        if m:
            rows[m["file"]] = (m["owner"], m["rest"])
    return rows


def test_every_snapshot_file_has_exactly_one_row_and_every_row_a_file() -> None:
    files = {p.name for p in SNAPSHOTS.iterdir() if p.is_file() and p.name != "README.md"}
    rows = _rows()
    assert files, "no snapshots found"
    assert sorted(files - set(rows)) == [], "snapshot without a README row"
    assert sorted(set(rows) - files) == [], "README row for a file that is gone"


def test_each_owner_reads_its_snapshot_and_honours_the_update_switch() -> None:
    rows = _rows()
    for name, (owner, rest) in rows.items():
        source = (HERE / owner).read_text(encoding="utf-8")
        assert name in source, f"{owner} does not read {name}"
        switches = set(re.findall(r"LOOM_UPDATE_[A-Z_]+=1", rest)) or {
            s
            for _, (o, r) in rows.items()
            if o == owner
            for s in re.findall(r"LOOM_UPDATE_[A-Z_]+=1", r)
        }
        assert switches, f"{name}: no update command in its row"
        for switch in switches:
            assert switch.removesuffix("=1") in source, f"{owner} ignores {switch}"
