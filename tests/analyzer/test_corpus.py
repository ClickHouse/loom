"""The statement corpus: every builder's shape set is the
committed one, and every shape passes the ClickHouse analyzer.

Run from the repo root with LOOM_CH_* pointing at a migrated ClickHouse
(the CI integration job's service; locally a throwaway CI-image container
with `python -m server.core.schema.migrate` applied — see tests/analyzer/README.md):

    pytest tests/analyzer -q

`test_every_shape_passes_the_analyzer` first compares the target's
`loom._migrations` ledger with `schemas/migrations/`: a target that lacks
migrations rejects every shape reading what they add (code 60 / 47), which is
the target being behind, not a statement defect — the test skips with the fix
in the reason (fails under `CI`, where the migrate step precedes it).

`test_shapes_file_matches_builders` needs no ClickHouse: it renders every
case and compares the shape set with `shapes.txt`. When a builder changes
shape on purpose, regenerate the file (`python -m tests.analyzer.corpus
--write`) and review the diff — a new line is a new statement shape.
"""

from __future__ import annotations

import os

import pytest

from tests.analyzer import corpus

_RENDERED: tuple | None = None


async def _render():
    """Render once per process. Several builders probe the server once per
    process (a column-existence read, the key-axis probe, a side-table
    readiness check) and cache the answer, so a second render in the same
    process issues fewer statements than a cold one; `shapes.txt` is the
    cold set, which is what the CLI and CI render."""
    global _RENDERED
    if _RENDERED is None:
        _RENDERED = await corpus.render_all()
    return _RENDERED


async def test_every_case_renders_a_statement() -> None:
    runs, _ = await _render()
    empty = [r.case.name for r in runs if not r.rendered]
    assert not empty, (
        f"cases that rendered no statement (a broken drive, not a covered builder): {empty}"
    )


async def test_no_drive_calls_a_missing_server_symbol() -> None:
    """A drive calling a removed or re-signed server function stops at that
    call, and every statement after it leaves the corpus as a "gone" shape."""
    runs, _ = await _render()
    defects = [f"{r.case.name}: {d}" for r in runs for d in r.defects]
    assert not defects, "drives that call a symbol that moved:\n" + "\n".join(defects)


async def test_every_axis_moves_a_shape() -> None:
    runs, _ = await _render()
    inert = corpus.inert_axes(runs)
    assert not inert, (
        f"axes that render the same shapes for every value: {inert}. Drive a value the "
        "builder branches on, or pin the axis to one value"
    )


async def test_shapes_file_matches_builders() -> None:
    _, shapes = await _render()
    committed = corpus.read_shapes()
    rendered = {s: shapes[s].head for s in shapes}
    new = sorted(set(rendered) - set(committed))
    gone = sorted(set(committed) - set(rendered))
    if new or gone:
        lines = [
            f"statement shapes changed: {len(new)} new, {len(gone)} gone.",
            "If the change is intended: python -m tests.analyzer.corpus --write, then review the diff.",
        ]
        for s in new[:20]:
            lines.append(f"  + {s}  {sorted(shapes[s].cases)}  {rendered[s][:120]}")
        for s in gone[:20]:
            lines.append(f"  - {s}  {committed[s][:120]}")
        pytest.fail("\n".join(lines))


def _require_migrated(ch_client) -> None:
    """The analyzer resolves names against the target's tables, so a target
    whose `loom._migrations` ledger lacks repo files rejects every shape that
    reads what those files add (code 60 / 47) — the target is behind, not the
    statement. Locally that is a skip with the fix in the reason; in CI the
    migrate step precedes this test, so a gap there is a broken job, not a
    reason to pass quietly."""
    pending = corpus.pending_migrations(ch_client)
    if not pending:
        return
    msg = (
        f"ClickHouse at {corpus.target_label()} lacks {len(pending)} of the repo's migrations "
        f"({pending[0]} … {pending[-1]}); the corpus checks against a migrated database — "
        "point LOOM_CH_* at one or run `python -m server.core.schema.migrate` (tests/analyzer/README.md)"
    )
    if os.environ.get("CI"):
        pytest.fail(msg)
    pytest.skip(msg)


@pytest.fixture
def migrated_ch(request: pytest.FixtureRequest):
    """`ch_client` on a reachable, migrated target. No target is a skip with
    the fix in the reason, and a failure under `CI`, where the job starts
    one."""
    try:
        client = request.getfixturevalue("ch_client")
    except Exception as exc:  # noqa: BLE001 — an unreachable target is the reason itself
        msg = (
            f"no ClickHouse at {corpus.target_label()} ({type(exc).__name__}); start a "
            "migrated one and point LOOM_CH_* at it (tests/analyzer/README.md)"
        )
        if os.environ.get("CI"):
            pytest.fail(msg)
        pytest.skip(msg)
    _require_migrated(client)
    return client


async def test_every_shape_passes_the_analyzer(migrated_ch) -> None:
    _, shapes = await _render()
    verdicts = corpus.check(shapes, migrated_ch)
    bad = [v for v in verdicts if not v.ok]
    checked = [v for v in verdicts if v.cls != corpus.UNCHECKED]
    assert checked, "nothing reached the analyzer"
    if bad:
        lines = [
            f"{len(bad)} of {len(checked)} statement shapes rejected by the analyzer at {corpus.target_label()}:"
        ]
        for v in bad:
            lines.append(f"  {v.shape} [{v.cls}] cases={sorted(shapes[v.shape].cases)}")
            lines.append(f"    {v.error[:300]}")
            lines.append(f"    {shapes[v.shape].head}")
        pytest.fail("\n".join(lines))


def test_the_analyzer_check_accepts_and_rejects(migrated_ch) -> None:
    """Positive and negative controls for `check`: a sound read passes, and
    the fail-open defects the corpus exists for (a CTE read without its
    `WITH`, a column the table lacks, an outer alias inside a subquery, an
    aggregate alias shadowing a filtered column) are rejected, so a green
    corpus is not an analyzer that answers yes to everything."""
    stmts = {
        "sound": "SELECT count() FROM loom.memory WHERE org = {org:String}",
        "cte_without_with": "SELECT memory_key FROM latest WHERE org = {org:String}",
        "unknown_column": "SELECT no_such_column FROM loom.memory WHERE org = {org:String}",
        "outer_alias_in_subquery": (
            "SELECT memory_key AS k FROM loom.memory WHERE org IN (SELECT k) "
            "AND namespace = {org:String}"
        ),
        "alias_shadows_filter": (
            "SELECT argMax(agent, updated_at) AS agent FROM loom.memory WHERE agent = {org:String}"
        ),
    }
    shapes = {name: corpus.Shape(name, sql, sql, {"org": "o"}) for name, sql in stmts.items()}
    ok = {v.shape: v.ok for v in corpus.check(shapes, migrated_ch)}
    assert ok == {
        "sound": True,
        "cte_without_with": False,
        "unknown_column": False,
        "outer_alias_in_subquery": False,
        "alias_shadows_filter": False,
    }


def test_analyzable_rewrites_mutations_and_inserts() -> None:
    cls, stmt = corpus.analyzable(
        "ALTER TABLE loom.memory UPDATE tombstoned_at = {now:DateTime}, status = 'x' WHERE org = {org:String} SETTINGS mutations_sync = 0"
    )
    assert cls == "mutation"
    assert (
        stmt
        == "SELECT ({now:DateTime}) AS tombstoned_at, ('x') AS status FROM loom.memory WHERE org = {org:String}"
    )
    cls, stmt = corpus.analyzable(
        "DELETE FROM loom.memory_chunks WHERE (org = {org:String}) SETTINGS lightweight_deletes_sync = 0"
    )
    assert (
        cls == "mutation"
        and stmt == "SELECT count() FROM loom.memory_chunks WHERE (org = {org:String})"
    )
    cls, stmt = corpus.analyzable("INSERT INTO loom.t (a, b) SELECT a, b FROM loom.s WHERE x = 1")
    assert cls == "insert_select" and stmt == "SELECT a, b FROM loom.s WHERE x = 1"
    assert corpus.analyzable("OPTIMIZE TABLE loom.memory FINAL") == (corpus.UNCHECKED, None)
    assert corpus.analyzable("SYSTEM START MERGES loom.memory") == (corpus.UNCHECKED, None)
    assert corpus.analyzable("  -- lead\n  WITH 1 AS x SELECT x")[0] == "select"


def test_params_for_types_every_placeholder() -> None:
    sql = "SELECT * FROM t WHERE id = {id:UUID} AND n IN {ns:Array(String)} AND k = {k:Nullable(UInt32)} AND d > {d:DateTime64(3)} AND m = {m:Map(String, Float32)}"
    out = corpus.params_for(sql, {"id": "not-a-uuid", "ns": ["a"], "k": "9"})
    assert str(out["id"]) == "00000000-0000-0000-0000-000000000001"  # the unfit value is replaced
    assert out["ns"] == ["a"] and out["k"] == 1 and out["d"].year == 2026 and out["m"] == {"x": 0.5}


def test_shape_masks_literals() -> None:
    a = corpus.shape_of("SELECT 1 FROM t WHERE org = 'a' AND k = 12")
    b = corpus.shape_of("SELECT   1 FROM t WHERE org = 'bbb'   AND k = 999")
    assert a == b and len(a) == 12
    assert corpus.masked_head("SELECT 'x', 42") == "SELECT ?, ?"


def test_drive_defects_are_moved_symbols_not_stub_rows() -> None:
    assert corpus.is_drive_defect(AttributeError("module 'server.x' has no attribute 'f'"))
    assert corpus.is_drive_defect(TypeError("f() got an unexpected keyword argument 'y'"))
    assert corpus.is_drive_defect(TypeError("f() missing 1 required keyword-only argument: 'ch'"))
    assert corpus.is_drive_defect(ImportError("cannot import name 'g'"))
    # a stub row answers 0 for a column it was not scripted with
    assert not corpus.is_drive_defect(TypeError("'int' object is not iterable"))
    assert not corpus.is_drive_defect(AttributeError("'int' object has no attribute 'isoformat'"))


def test_shapes_file_is_in_written_form() -> None:
    """Sorted, one line per shape, the header `--write` puts there: a hand
    edit or a merge that duplicated or reordered lines shows here."""
    text = corpus.SHAPES_PATH.read_text(encoding="utf-8")
    heads = corpus.read_shapes()
    assert all(len(s) == 12 and int(s, 16) >= 0 for s in heads), "a shape key is not 12 hex digits"
    assert all(len(h) <= corpus.HEAD_CHARS for h in heads.values())
    assert text == corpus.shapes_text(heads), (
        "shapes.txt is not in the form `python -m tests.analyzer.corpus --write` writes"
    )


def test_every_case_module_is_registered() -> None:
    """A case module missing from `cases.MODULES` contributes no shapes and
    fails nothing."""
    from pathlib import Path

    from tests.analyzer import cases

    on_disk = {
        p.stem for p in Path(cases.__file__).parent.glob("*.py") if not p.stem.startswith("_")
    }
    assert on_disk == set(cases.MODULES)
