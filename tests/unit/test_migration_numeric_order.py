"""Migration files run in numeric-prefix order, not lexicographic order:
`1030_x.sql` follows `950_y.sql`."""

from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path

from server.core.paths import MIGRATIONS_DIR
from server.core.schema.journal import migration_order, sorted_migrations, through_target


def test_four_digit_prefix_sorts_after_three_digit() -> None:
    files = [
        Path("1030_namespace_retrieval_defaults.sql"),
        Path("220_a.sql"),
        Path("950_b.sql"),
        Path("1000_c.sql"),
    ]
    assert [p.name for p in sorted_migrations(files)] == [
        "220_a.sql",
        "950_b.sql",
        "1000_c.sql",
        "1030_namespace_retrieval_defaults.sql",
    ]


def test_same_prefix_falls_back_to_name() -> None:
    files = [Path("300_b.sql"), Path("300_a.sql")]
    assert [p.name for p in sorted_migrations(files)] == ["300_a.sql", "300_b.sql"]


def test_unprefixed_file_sorts_last() -> None:
    assert migration_order(Path("zzz.sql"))[0] > migration_order(Path("999_x.sql"))[0]


def test_tree_order_is_monotonic() -> None:
    tree = sorted_migrations(MIGRATIONS_DIR.glob("*.sql"))
    nums = [migration_order(p)[0] for p in tree]
    assert nums == sorted(nums)


def test_target_compares_numbers_not_names() -> None:
    files = sorted_migrations(
        [Path("990_a.sql"), Path("1000_b.sql"), Path("1030_c.sql"), Path("014_d.sql")]
    )
    assert [p.name for p in through_target(files, 1000)] == ["014_d.sql", "990_a.sql", "1000_b.sql"]
    assert [p.name for p in through_target(files, 14)] == ["014_d.sql"]
    assert through_target([Path("zzz.sql")], 10**6) == []


def test_migration_numbers_are_unique() -> None:
    """Numbers are claimed, not shared: two files with one number apply in the
    lexical order of their suffix, which is not the order anyone intended."""
    by_number: dict[str, set[str]] = defaultdict(set)
    for p in (Path(__file__).resolve().parents[2] / "schemas" / "migrations").glob("*.sql"):
        m = re.match(r"^(\d{3,4})_", p.name)
        assert m, f"migration without a 3- or 4-digit prefix: {p.name}"
        by_number[m.group(1)].add(p.name)
    dups = {n: sorted(fs) for n, fs in by_number.items() if len(fs) > 1}
    assert not dups, (
        f"duplicate migration numbers: {dups} — take the next free number "
        "(check origin/main AND open PRs) and keep it"
    )
