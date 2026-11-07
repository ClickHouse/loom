"""The floor tables of docs/architecture/document-extraction.md, read strictly:
the extraction gates (tests/unit/test_extraction_gates.py) and the retrieval
floors (tests/integration/test_knowledge_retrieval_floors.py) both declare their
floors there, as a Markdown table whose last column is the floor."""

from __future__ import annotations

import re


def read_floors(text: str, heading: str, columns: int) -> dict[tuple[str, ...], float]:
    """The key (every column but the last) to floor, from the first table under the
    heading line `heading` and up to the next heading of any level. A row that is
    not `columns` cells with a floor written as a decimal, or a repeated key, is a
    ValueError: a row skipped would go ungated while the page still lists it."""
    marker = f"\n{heading}\n"
    if marker not in text:
        raise ValueError(f"no {heading!r} heading")
    section = re.split(r"\n#{2,} ", text.split(marker, 1)[1], maxsplit=1)[0]
    rows = [line.strip() for line in section.splitlines() if line.strip().startswith("|")]
    floors: dict[tuple[str, ...], float] = {}
    for line in rows[2:]:  # past the header and its rule
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) != columns or not re.fullmatch(r"[01]\.\d+", cells[-1]):
            raise ValueError(f"not a row of {columns} cells ending in a floor: {line}")
        key = tuple(cells[:-1])
        if key in floors:
            raise ValueError(f"a repeated row: {line}")
        floors[key] = float(cells[-1])
    if not floors:
        raise ValueError(f"no floors under {heading!r}")
    return floors
