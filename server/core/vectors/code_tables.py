"""Which table holds a label's chunk rows.

`loom.code_chunks.embedding` is `Array(BFloat16) CODEC(Quantized('rabitq', 3072, 0))`:
the codec fixes the row width, so a second embedding model of another width cannot write
into the same column. Its rows go to a table created with its own codec width, and
`LOOM_CODE_VECTOR_TABLES` says which: JSON `{"<embedding_model label>": {"table":
"loom.x", "dim": 256}}`. Every label not named there lives in `loom.code_chunks`. The
insert path routes rows by this map, the vector leg reads by it, and the placeholder
rule pads an empty vector to the label's width.

Table names are interpolated into SQL, so they are validated against a strict
identifier grammar here and nowhere else.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache

DEFAULT_TABLE = "loom.code_chunks"
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?$")


@lru_cache(maxsize=8)
def _parse(raw: str) -> dict[str, tuple[str, int]]:
    raw = (raw or "").strip()
    if not raw:
        return {}
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("LOOM_CODE_VECTOR_TABLES must be a JSON object keyed by label")
    out: dict[str, tuple[str, int]] = {}
    for label, spec in data.items():
        if isinstance(spec, str):
            table, dim = spec, 0
        else:
            table, dim = str(spec.get("table") or ""), int(spec.get("dim") or 0)
        if not _IDENT.match(table):
            raise ValueError(f"LOOM_CODE_VECTOR_TABLES[{label!r}]: bad table name {table!r}")
        if "." not in table:
            table = f"loom.{table}"
        out[str(label)] = (table, dim)
    return out


def _mapping() -> dict[str, tuple[str, int]]:
    from server.settings import settings

    return _parse(settings.code_vector_tables)


def mapping() -> dict[str, tuple[str, int]]:
    """The configured label → (table, dim) map (a copy)."""
    return dict(_mapping())


def code_vector_table(label: str) -> str:
    """Fully qualified table holding `label`'s chunk rows."""
    return _mapping().get(label, (DEFAULT_TABLE, 0))[0]


def vector_dim_for_table(table: str) -> int:
    """The vector width a mapped table's codec demands, 0 when unmapped (the
    served model's `embedding_dim` applies)."""
    if "." not in table:
        table = f"loom.{table}"
    for t, dim in _mapping().values():
        if t == table:
            return dim
    return 0


def written_by_served_model(table: str) -> bool:
    """One of Loom's own coded tables that no label routes elsewhere: the served
    model writes it, and a model change rebuilds it at the new width."""
    from server.core.schema.vector_codec_ddl import CODED_TABLES

    if "." not in table:
        table = f"loom.{table}"
    return table.split(".", 1)[1] in CODED_TABLES and not vector_dim_for_table(table)


def is_chunks_table(table: str) -> bool:
    if "." not in table:
        table = f"loom.{table}"
    return table == DEFAULT_TABLE or any(t == table for t, _ in _mapping().values())


def reset() -> None:
    """Test hook: drop the parsed map so a settings patch takes effect."""
    _parse.cache_clear()
