"""The migration journal as data: file order, baselines, statements and the
objects each file creates or drops. Pure: nothing here talks to ClickHouse.

The runner (`migrate`), the health and read-guard view (`status`), the
rebuild tooling (`table_rebuild`) and `/v1/admin/schema.status` read the
journal through this module, so they agree on what a file means.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

_NUMBER = re.compile(r"^(\d+)_")


def migration_order(path: Path) -> tuple[int, str]:
    """Sort key for migration files: the numeric prefix, then the name.
    Names sort lexicographically otherwise, so `1030_x.sql` would run before
    `220_y.sql` on a fresh replay."""
    m = _NUMBER.match(path.name)
    return (int(m.group(1)) if m else 10**9, path.name)


def sorted_migrations(files: Iterable[Path]) -> list[Path]:
    return sorted(files, key=migration_order)


def migration_number(name: str) -> int | None:
    m = _NUMBER.match(name)
    return int(m.group(1)) if m else None


def through_target(files: Iterable[Path], target: int) -> list[Path]:
    """The files `--target N` applies: numbered N or lower, by number."""
    return [f for f in files if (n := migration_number(f.name)) is not None and n <= target]


_BASELINE_RE = re.compile(r"^--[ \t]*loom:[ \t]*baseline[ \t]+covers[ \t]+(\d+)[ \t]*$", re.M)


def baseline_covers(path: Path) -> int | None:
    """N for a file whose header declares `-- loom: baseline covers N`, else None."""
    head = "\n".join(path.read_text().splitlines()[:12])
    m = _BASELINE_RE.search(head)
    return int(m.group(1)) if m else None


def baseline_map(files: Iterable[Path]) -> dict[str, int]:
    return {f.name: n for f in files if (n := baseline_covers(f)) is not None}


@dataclass(frozen=True)
class LedgerClasses:
    """Ledger filenames and repo files, sorted into what each means.

    `pending`: repo files still to run. `baselined`: pending baselines the
    ledger already covers, so they are recorded without running. `stranded`:
    baselines whose replaced files the ledger holds only in part (not file N),
    never run nor recorded. `superseded`: ledger rows a baseline in the repo
    replaces. `ledger_only`: ledger rows nothing in the repo accounts for (a
    file renamed after it ran).
    """

    pending: tuple[str, ...]
    baselined: tuple[str, ...]
    superseded: tuple[str, ...]
    ledger_only: tuple[str, ...]
    stranded: tuple[str, ...] = ()


def classify_ledger(
    repo: Iterable[str], ledger: Iterable[str], covers: dict[str, int]
) -> LedgerClasses:
    """Repo filenames, ledger filenames and `{baseline file: N}` → classes."""
    repo_names, ledger_names = list(repo), set(ledger)
    repo_set = set(repo_names)
    cover = max(covers.values(), default=0)
    gone = sorted(n for n in ledger_names if n not in repo_set)
    superseded = tuple(n for n in gone if (migration_number(n) or 10**9) <= cover)
    ledger_only = tuple(n for n in gone if n not in superseded)
    pending = [n for n in repo_names if n not in ledger_names]
    covering = {
        n: [s for s in superseded if (migration_number(s) or 10**9) <= covers[n]]
        for n in pending
        if n in covers
    }
    baselined = tuple(
        n
        for n, held in covering.items()
        if held and any(migration_number(s) == covers[n] for s in held)
    )
    stranded = tuple(n for n, held in covering.items() if held and n not in baselined)
    return LedgerClasses(
        pending=tuple(n for n in pending if n not in baselined and n not in stranded),
        baselined=baselined,
        superseded=superseded,
        ledger_only=ledger_only,
        stranded=stranded,
    )


def without_comments(sql: str) -> str:
    """`sql` without its full-line `--` comments."""
    return "\n".join(line for line in sql.splitlines() if not line.strip().startswith("--"))


def split_statements(sql: str) -> list[str]:
    """Split SQL on `;` at the end of a line, stripping comment lines. A `;`
    inside a string literal that ends a line would split it: migrations keep
    such statements out."""
    parts = re.split(r";\s*(?:\n|\Z)", without_comments(sql))
    return [p.strip() for p in parts if p.strip()]


_OBJECT_DDL = re.compile(
    r"\b(CREATE)\s+(?:OR\s+REPLACE\s+)?(?:TABLE|MATERIALIZED\s+VIEW|VIEW|DICTIONARY)\s+"
    r"(?:IF\s+NOT\s+EXISTS\s+)?(?:`?loom`?\.)?`?([A-Za-z_]\w*)`?"
    r"|\b(DROP)\s+(?:TABLE|VIEW|DICTIONARY)\s+(?:IF\s+EXISTS\s+)?(?:`?loom`?\.)?`?([A-Za-z_]\w*)`?",
    re.IGNORECASE,
)


def object_ddl(sql: str) -> Iterator[tuple[str, str]]:
    """`("create" | "drop", name)` for each loom object `sql` creates or drops,
    in order, comment lines skipped."""
    for m in _OBJECT_DDL.finditer(without_comments(sql)):
        if m.group(1):
            yield "create", m.group(2)
        else:
            yield "drop", m.group(4)


def created_objects(sql: str) -> tuple[str, ...]:
    """The objects `sql` creates, first mention first."""
    return tuple(dict.fromkeys(name for op, name in object_ddl(sql) if op == "create"))


def live_objects(files: Iterable[Path]) -> set[str]:
    """The `loom.*` objects a replay of `files` leaves: each statement's CREATE
    or DROP applied in migration order (a migration may drop an insert-time
    view)."""
    names: set[str] = set()
    for path in sorted_migrations(files):
        for op, name in object_ddl(path.read_text("utf-8")):
            if op == "create":
                names.add(name)
            else:
                names.discard(name)
    return names
