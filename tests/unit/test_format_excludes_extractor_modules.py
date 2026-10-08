"""`ruff format` leaves the extractor modules alone.

Their code tokens are the code index's version stamp: a reformat that
changes tokens (quotes, parentheses, trailing commas) moves the stamp and
every namespace re-chunks. The formatter's exclude list names each one;
this keeps the two lists equal.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from server.code.domain.extract import extractor_stamp

ROOT = Path(__file__).resolve().parents[2]


def test_every_extractor_module_is_excluded_from_formatting() -> None:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    excluded = set(config["tool"]["ruff"]["format"]["exclude"])
    stamped = {f"server/code/domain/extract/{m}" for m in extractor_stamp.EXTRACTOR_MODULES}
    code_index = {e for e in excluded if e.startswith("server/code/domain/extract/")}
    assert code_index == stamped
