"""The code-lane corpus's manifest: expected.json's form, the lane registry,
the fixtures' provenance and their hygiene, checked without parsing a fixture.

`test_code_lanes_corpus.py` derives every fixture and compares the result
with expected.json. This file checks that the golden file itself is
well-formed and canonical, that every fixture belongs to a registered lane
and a NOTICE source, and that a fixture which derives nothing says why."""

from __future__ import annotations

import json
import posixpath
import re
from collections import Counter
from pathlib import Path

import pytest

from tests.code_lanes.corpus import EXPECTED, LANES, ROOT, fixture_paths, lane_of

NOTICE = Path(ROOT) / "NOTICE.md"
MAX_FIXTURE_BYTES = 64 * 1024
# The languages whose summary row records includes resolved against the
# corpus's own paths; the others record the specifier as written.
RESOLVED_INCLUDE_LANGS = frozenset(
    {"typescript", "javascript", "rust", "proto", "gotemplate", "python"}
)
_EDGE_KEY = re.compile(r"^(?P<path>.+) \| (?P<kind>[a-z_]+) \| (?P<target>[a-z_?]+)$")
_SECRET = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----|AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{36}"
    r"|sk-[A-Za-z0-9]{32,}|xox[baprs]-[A-Za-z0-9-]{10,}"
)


@pytest.fixture(scope="module")
def expected() -> dict:
    with open(EXPECTED, encoding="utf-8") as fh:
        return json.load(fh)


def test_expected_json_is_in_written_form(expected: dict) -> None:
    """Sorted keys, one-space indent, trailing newline: what `--write`
    produces, so a regeneration diff shows only moved numbers."""
    text = Path(EXPECTED).read_text(encoding="utf-8")
    assert text == json.dumps(expected, indent=1, sort_keys=True, ensure_ascii=False) + "\n"


def test_expected_json_rows_have_their_schema(expected: dict) -> None:
    assert set(expected) == {"files", "edges", "misses"}
    for path, row in expected["files"].items():
        assert set(row) - {"specifiers"} == {"lang", "symbols", "facets", "includes"}, path
        assert isinstance(row["lang"], str) and row["lang"], path
        for counts in (row["symbols"], row["facets"]):
            assert all(isinstance(n, int) and n > 0 for n in counts.values()), path
        assert all(isinstance(p, str) and p for p in row["includes"]), path
        assert len(row["includes"]) == len(set(row["includes"])), (path, row["includes"])
    for key, n in expected["edges"].items():
        m = _EDGE_KEY.match(key)
        assert m and m["path"] in expected["files"], key
        assert isinstance(n, int) and n > 0, key
    for path, n in expected["misses"].items():
        assert path in expected["files"] and isinstance(n, int) and n > 0, path


def test_every_fixture_has_a_row_and_every_row_a_fixture(expected: dict) -> None:
    assert set(expected["files"]) == set(fixture_paths())


def test_every_fixture_sits_in_a_lane_with_its_language(expected: dict) -> None:
    wrong = {
        path: (row["lang"], sorted(lane_of(path).langs_for(path)))
        for path, row in expected["files"].items()
        if row["lang"] not in lane_of(path).langs_for(path)
    }
    assert not wrong, wrong


def test_every_lane_has_fixtures_that_derive_rows(expected: dict) -> None:
    edged = {m["path"] for key in expected["edges"] if (m := _EDGE_KEY.match(key))}
    for lane in LANES:
        rows = [
            p
            for p, row in expected["files"].items()
            if p.startswith(lane.prefix) and (row["symbols"] or row["facets"] or p in edged)
        ]
        assert rows, f"lane {lane.prefix} has no fixture that derives a symbol, facet or edge"


def test_lane_prefixes_are_disjoint() -> None:
    prefixes = [lane.prefix for lane in LANES]
    assert len(prefixes) == len(set(prefixes))
    assert not [(a, b) for a in prefixes for b in prefixes if a != b and b.startswith(a)]


def test_a_fixture_that_derives_nothing_is_support_or_a_named_limit(expected: dict) -> None:
    """A manifest, an include target or a package marker serves the files
    around it; anything else that derives nothing is dead weight, or the lane
    lost a construct, unless its lane names it in `inert`."""
    edged = {m["path"] for key in expected["edges"] if (m := _EDGE_KEY.match(key))}
    targets = {t for row in expected["files"].values() for t in row["includes"]}
    declared = {path: reason for lane in LANES for path, reason in lane.inert}
    silent = []
    for path, row in expected["files"].items():
        if row["symbols"] or row["facets"] or row["includes"] or path in edged:
            assert path not in declared, f"{path} derives rows now; drop it from `inert`"
            continue
        lane = lane_of(path)
        manifest = lane.langs_for(path) != frozenset({lane.lang})
        if path in declared or path in targets or manifest:
            continue
        silent.append(path)
    assert not silent, silent
    assert set(declared) <= set(expected["files"]), set(declared) - set(expected["files"])


def test_resolved_includes_name_corpus_files(expected: dict) -> None:
    files = expected["files"]
    dangling = {
        path: [t for t in row["includes"] if t not in files]
        for path, row in files.items()
        if row["lang"] in RESOLVED_INCLUDE_LANGS
        or (row["lang"] == "config" and posixpath.basename(path) == "Chart.yaml")
    }
    assert not {p: ts for p, ts in dangling.items() if ts}


def _notice_prefixes() -> list[str]:
    rows = [
        line for line in NOTICE.read_text(encoding="utf-8").splitlines() if line.startswith("| ")
    ]
    cells = [row.split("|")[1].strip() for row in rows[1:]]  # the header row goes
    return [p.strip() for cell in cells for p in cell.split(",") if p.strip()]


def test_notice_names_one_source_for_every_fixture() -> None:
    prefixes = _notice_prefixes()
    paths = fixture_paths()
    claims = Counter(p for path in paths for p in prefixes if path.startswith(p))
    unsourced = [path for path in paths if not any(path.startswith(p) for p in prefixes)]
    doubled = [path for path in paths if sum(path.startswith(p) for p in prefixes) > 1]
    assert not unsourced, f"fixtures NOTICE.md does not name a source for: {unsourced}"
    assert not doubled, f"fixtures under two NOTICE rows: {doubled}"
    assert not [p for p in prefixes if not claims[p]], "NOTICE rows that match no fixture"


@pytest.mark.parametrize("path", fixture_paths())
def test_fixture_is_small_text_without_credentials(path: str) -> None:
    data = (Path(ROOT) / path).read_bytes()
    assert len(data) <= MAX_FIXTURE_BYTES, f"{path}: {len(data)} bytes; cut it to the construct"
    assert b"\0" not in data, f"{path} is binary"
    text = data.decode("utf-8")
    assert not _SECRET.search(text), f"{path} holds a credential-shaped string"
