"""Case modules of the analyzer statement corpus.

Each module exposes `cases() -> list[Case]`. Add a module here when a new
plane grows statement builders; keep each case's axis product at or under
`corpus.MAX_COMBOS`.
"""

from __future__ import annotations

import importlib

from tests.analyzer.corpus import Case

MODULES = (
    "search",
    "legs",
    "code",
    "erasure",
    "planes",
    "workers",
    "tracker",
    "auth",
)


def all_cases() -> list[Case]:
    out: list[Case] = []
    for name in MODULES:
        mod = importlib.import_module(f"tests.analyzer.cases.{name}")
        out.extend(mod.cases())
    return out
