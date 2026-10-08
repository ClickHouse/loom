"""Every file in tests/unit/fixtures is documented, parses, has a consumer and
carries nothing personal.

A fixture is a test's exact input or golden output, so a malformed or orphaned
file should fail fast and name itself here, not deep inside its consumer. The
table in `fixtures/README.md` is the index this checks.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures"
_ROW = re.compile(
    r"^\|\s*`(?P<file>[^`]+)`\s*\|\s*`(?P<consumer>test_[a-z0-9_]+\.py)`\s*\|"
    r"[^|]*\|\s*(?P<shape>list|object)\s*\|"
)
_SHAPES = {"list": list, "object": dict}
# Personal or credential-shaped text a fixture must not carry.
_PERSONAL = re.compile(
    r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+\.[A-Za-z]{2,}"  # e-mail address
    r"|/home/[a-z_][a-z0-9_-]*"  # a home directory
    r"|eyJ[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}"  # JWT
    r"|\bsk-[A-Za-z0-9]{20,}"  # provider API key
)


def _rows() -> dict[str, tuple[str, str]]:
    rows = {}
    for line in (FIXTURES / "README.md").read_text(encoding="utf-8").splitlines():
        m = _ROW.match(line)
        if m:
            rows[m["file"]] = (m["consumer"], m["shape"])
    return rows


_FILES = sorted(p.name for p in FIXTURES.iterdir() if p.is_file() and p.name != "README.md")


def test_every_fixture_has_exactly_one_row_and_every_row_a_file() -> None:
    rows = _rows()
    assert _FILES, "no fixtures found"
    assert sorted(set(_FILES) - set(rows)) == [], "fixture without a README row"
    assert sorted(set(rows) - set(_FILES)) == [], "README row for a file that is gone"


@pytest.mark.parametrize("name", _FILES)
def test_a_fixture_parses_as_its_shape_and_its_consumer_reads_it(name: str) -> None:
    consumer, shape = _rows()[name]
    data = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    assert isinstance(data, _SHAPES[shape]) and data, f"{name}: not a non-empty {shape}"
    assert name in (HERE / consumer).read_text(encoding="utf-8"), f"{consumer} does not read it"


@pytest.mark.parametrize("name", _FILES)
def test_a_fixture_carries_no_personal_or_credential_text(name: str) -> None:
    hits = _PERSONAL.findall((FIXTURES / name).read_text(encoding="utf-8"))
    assert not hits, f"{name}: {sorted(set(hits))[:5]}"
