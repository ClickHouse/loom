"""A statement corpus through the ClickHouse analyzer.

Loom's statements are Python strings built by ~200 builders under hundreds
of flags; unit stubs accept any string, so an alias that shadows a column, a
CTE named in one statement and read in another, or an aggregate aliased to
its own source column (`max(version) AS version`, code 184) reaches staging
as a leg that fails open. The analyzer needs no rows: `EXPLAIN QUERY TREE`
against the migrated CI tables rejects every one of those shapes in ~5 ms.

This module drives each BUILDER (not each constant) over the product of its
flag axes through a recording stub client, renders the statements with the
builder's own typed parameters (missing placeholders get typed dummies),
dedups them by the statement digest (literals masked, so a
vector or a namespace name does not make a new shape), and

- `render_all()`      → every unique shape and which cases produced it;
- `shapes.txt`        → the committed shape list (`<shape12>\t<masked head>`),
                        sorted, so a new or vanished shape is visible in
                        review and `test_corpus.py` fails when a builder's
                        shape set changed without the file;
- `check()`           → each shape through `EXPLAIN QUERY TREE`; INSERT …
                        SELECT contributes its SELECT, a mutation (`ALTER …
                        UPDATE/DELETE`, `DELETE FROM`) its predicate and
                        assignments as a SELECT over the same table; DDL,
                        `SYSTEM …`, `OPTIMIZE` and VALUES inserts are listed
                        as unchecked.

CLI (from the repo root, LOOM_CH_* pointing at a migrated ClickHouse):

    python -m tests.analyzer.corpus --report              # coverage table
    python -m tests.analyzer.corpus --write               # rewrite shapes.txt
    python -m tests.analyzer.corpus --check [--import capture.jsonl ...]

`LOOM_CH_CAPTURE_STATEMENTS=<path>` on a server appends one JSON line per
executed statement (`server/core/ch.py`); `--import` runs those shapes too, so an
acceptance run contributes what production actually executes.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import dataclasses
import datetime as _dt
import itertools
import json
import os
import re
import sys
import time
import traceback
import uuid
from collections.abc import Awaitable, Callable, Iterator, Sequence
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server.core.ch_statement_text import (  # noqa: E402
    _DIGEST_LITERALS,
    _DIGEST_WS,
    statement_digest,
)

SHAPES_PATH = Path(__file__).with_name("shapes.txt")
HEAD_CHARS = 200
MAX_COMBOS = 64
# A drive that ends on one of these called a server symbol that is gone or
# whose signature moved: every statement after the call drops out of the
# corpus, and `--write` would record the loss as "gone" shapes. A stub row's
# 0 for an unknown column raises TypeError / AttributeError too, so only the
# messages below count.
_DEFECT_MESSAGE = re.compile(
    r"^module '[\w.]+' has no attribute"
    r"|unexpected keyword argument"
    r"|missing \d+ required (?:positional|keyword-only) argument"
    r"|takes \d+ positional arguments? but \d+ (?:was|were) given"
)


def is_drive_defect(exc: BaseException) -> bool:
    if isinstance(exc, (ImportError, NameError)):
        return True
    return isinstance(exc, (AttributeError, TypeError)) and bool(_DEFECT_MESSAGE.search(str(exc)))


Drive = Callable[["RecordingCH", dict[str, Any]], Awaitable[None]]


# --------------------------------------------------------------------------- cases


@dataclasses.dataclass(frozen=True)
class Case:
    """One builder (or one entry point that renders several) and its axes.

    `axes` maps a name to the values it takes; the product of every axis is
    driven, one fresh `RecordingCH` per combination. An axis whose name is a
    `settings` field is applied to `server.settings.settings` for the
    duration of the drive (and restored); every axis, settings or not, is
    passed to `drive` in the combination dict.
    """

    name: str
    module: str
    drive: Drive
    axes: dict[str, Sequence[Any]] = dataclasses.field(default_factory=dict)
    rows: Callable[[str, dict[str, Any]], list[dict[str, Any]]] | None = None

    def combos(self) -> list[dict[str, Any]]:
        names = list(self.axes)
        out = [
            dict(zip(names, vals, strict=True)) for vals in itertools.product(*self.axes.values())
        ]
        if len(out) > MAX_COMBOS:
            raise ValueError(f"case {self.name}: {len(out)} combinations, cap is {MAX_COMBOS}")
        return out


@dataclasses.dataclass
class Rendered:
    case: str
    module: str
    combo: dict[str, Any]
    sql: str
    params: dict[str, Any]
    kind: str  # query_all | query_one | execute | command


@dataclasses.dataclass
class Shape:
    shape: str
    head: str
    sql: str
    params: dict[str, Any]
    cases: set[str] = dataclasses.field(default_factory=set)
    modules: set[str] = dataclasses.field(default_factory=set)


class _Row(dict[str, Any]):
    """A stub row: unknown columns read as 0 so a builder that reads a
    count from the previous statement carries on to its next one."""

    def __missing__(self, key: str) -> Any:
        return 0


class RecordingCH:
    """Async stub for `server.core.ch.CHClient`: records every statement, returns
    what `rows(sql, params)` says (default: nothing)."""

    def __init__(self, rows: Callable[[str, dict[str, Any]], list[dict[str, Any]]] | None = None):
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self._rows = rows
        self.inserts: list[tuple[str, int]] = []

    def _answer(self, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        if self._rows is None:
            return []
        return [r if isinstance(r, _Row) else _Row(r) for r in self._rows(sql, params)]

    async def query_all(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        p = dict(params or {})
        self.calls.append(("query_all", sql, p))
        return self._answer(sql, p)

    async def query_one(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any] | None:
        p = dict(params or {})
        self.calls.append(("query_one", sql, p))
        rows = self._answer(sql, p)
        return rows[0] if rows else None

    async def execute(
        self,
        sql: str,
        params: dict[str, Any] | None = None,
        *,
        settings: dict[str, Any] | None = None,
        external_data: Any = None,
    ) -> None:
        # The real client's keywords: its settings and external data change no statement.
        self.calls.append(("execute", sql, dict(params or {})))

    async def command(self, sql: str, params: dict[str, Any] | None = None) -> None:
        self.calls.append(("command", sql, dict(params or {})))

    async def insert(self, table: str, rows: list[dict[str, Any]], *a: Any, **kw: Any) -> None:
        self.inserts.append((table, len(rows)))

    # The fake-embedding and sequencing helpers some services touch.
    generation = 0


# --------------------------------------------------------------------------- driving


def _settings_fields() -> set[str]:
    from server.settings import settings

    return set(type(settings).model_fields)


@contextlib.contextmanager
def _settings_applied(values: dict[str, Any]) -> Iterator[None]:
    from server.settings import settings

    before = dict(settings.__dict__)
    try:
        for k, v in values.items():
            setattr(settings, k, v)
        yield
    finally:
        settings.__dict__.clear()
        settings.__dict__.update(before)


@contextlib.contextmanager
def _client_patched(ch: RecordingCH) -> Iterator[None]:
    """Every `get_client` the server imported resolves to `ch`: the one in
    `server.core.ch` and each module-level `from server.core.ch import get_client`."""
    import server.core.ch as ch_mod

    original = ch_mod.get_client

    def _stub() -> RecordingCH:
        return ch

    patched: list[Any] = []
    for name, mod in list(sys.modules.items()):
        if not (name == "server" or name.startswith("server.")) or mod is None:
            continue
        if getattr(mod, "get_client", None) is original:
            mod.get_client = _stub
            patched.append(mod)
    try:
        yield
    finally:
        for mod in patched:
            mod.get_client = original
        # A module first imported while the stub was in place bound the stub
        # with `from server.core.ch import get_client`; put the original back.
        for name, mod in list(sys.modules.items()):
            if (name == "server" or name.startswith("server.")) and mod is not None:
                if getattr(mod, "get_client", None) is _stub:
                    mod.get_client = original


@contextlib.contextmanager
def _auth_context() -> Iterator[None]:
    from server.auth.context import current_auth_var
    from server.auth.jwt import AuthInfo
    from server.operations.base import Role

    token = current_auth_var.set(
        AuthInfo(
            subject="corpus@example.test",
            org="o",
            namespaces=("n", "n2"),
            roles=(Role.ORG_ADMIN,),
            is_service_account=False,
        )
    )
    try:
        yield
    finally:
        current_auth_var.reset(token)


@dataclasses.dataclass
class CaseRun:
    case: Case
    rendered: list[Rendered]
    errors: list[str]
    defects: list[str] = dataclasses.field(default_factory=list)


def _drop_read_caches() -> None:
    """Reads that are cached in-process (per org or namespace, for seconds
    to minutes) answer from the cache when anything earlier in the same
    process filled it (a previous case, a second render, unit tests
    collected into the same run), and the statement behind them would drop
    out of the corpus as "gone". Every combo starts from empty caches."""
    from server.auth import aliases
    from server.identity.app import namespace_classification, namespace_kinds, namespace_visibility
    from server.insights.app import agents
    from server.memory.app import (
        embedder_calibration,
        facts,
        namespace_health,
        namespace_retrieval_defaults,
    )
    from server.memory.app.retrieval import key_axis

    namespace_classification.reset()
    namespace_visibility.reset()
    namespace_kinds.reset_cache()
    namespace_health.reset_cache()
    namespace_retrieval_defaults.clear_cache()
    embedder_calibration.clear_ns_overrides()
    agents.reset_connected_cache()
    facts.reset_traverse_cache()
    aliases._CACHE.clear()
    key_axis._KEY_AXIS_AUTO.clear()


async def run_case(case: Case) -> CaseRun:
    fields = _settings_fields()
    rendered: list[Rendered] = []
    errors: list[str] = []
    defects: list[str] = []
    for combo in case.combos():
        _drop_read_caches()
        ch = RecordingCH(case.rows)
        setting_values = {k: v for k, v in combo.items() if k in fields}
        with _settings_applied(setting_values), _client_patched(ch), _auth_context():
            try:
                await case.drive(ch, combo)
            except Exception as exc:  # noqa: BLE001 — a builder that stops after its first statement still rendered it
                tail = traceback.extract_tb(exc.__traceback__)[-1]
                message = (
                    f"{combo}: {type(exc).__name__}: {str(exc)[:160]} "
                    f"@ {Path(tail.filename).name}:{tail.lineno} {tail.line}"
                )
                errors.append(message)
                if is_drive_defect(exc):
                    defects.append(message)
        for kind, sql, params in ch.calls:
            rendered.append(Rendered(case.name, case.module, combo, sql, params, kind))
    return CaseRun(case, rendered, errors, defects)


def inert_axes(runs: Sequence[CaseRun]) -> list[tuple[str, str]]:
    """(case, axis) pairs where no two combinations that differ only in that
    axis render different shape lists. Such an axis multiplies the drive
    without checking anything new: the analyzer sees one parameter set per
    shape. It usually means the axis drives a value the builder never
    branches on."""
    out: list[tuple[str, str]] = []
    for run in runs:
        by_combo: dict[str, list[str]] = {}
        for r in run.rendered:
            by_combo.setdefault(_combo_key(r.combo), []).append(shape_of(r.sql))
        combos = run.case.combos()
        for axis, values in run.case.axes.items():
            if len(values) < 2:
                continue
            moved = False
            for combo in combos:
                seen = {tuple(by_combo.get(_combo_key({**combo, axis: v}), [])) for v in values}
                if len(seen) > 1:
                    moved = True
                    break
            if not moved:
                out.append((run.case.name, axis))
    return out


def _combo_key(combo: dict[str, Any]) -> str:
    return json.dumps(combo, sort_keys=True, default=str)


def all_cases() -> list[Case]:
    from tests.analyzer.cases import all_cases as _all

    cases = _all()
    names = [c.name for c in cases]
    dupes = {n for n in names if names.count(n) > 1}
    if dupes:
        raise ValueError(f"duplicate case names: {sorted(dupes)}")
    return cases


# --------------------------------------------------------------------------- shapes


def shape_of(sql: str) -> str:
    return statement_digest(sql).split(" ", 1)[0][len("sql:") :]


def masked_head(sql: str) -> str:
    flat = _DIGEST_WS.sub(" ", str(sql or "")).strip()
    return _DIGEST_LITERALS.sub("?", flat)[:HEAD_CHARS].rstrip()


def collect(runs: Sequence[CaseRun]) -> dict[str, Shape]:
    shapes: dict[str, Shape] = {}
    for run in runs:
        for r in run.rendered:
            s = shape_of(r.sql)
            entry = shapes.get(s)
            if entry is None:
                entry = shapes[s] = Shape(s, masked_head(r.sql), r.sql, r.params)
            entry.cases.add(r.case)
            entry.modules.add(r.module)
    return shapes


async def render_all(cases: Sequence[Case] | None = None) -> tuple[list[CaseRun], dict[str, Shape]]:
    from server.core.vectors import patch_parts
    from server.core.vectors.quantized_setting import set_quantized_codes_supported
    from server.wiring import wire

    # The platform seams hold their domain pieces, as in every server process.
    wire()
    # Render the production form of every vector-ranked read: the codes flag
    # on and the patch-part probe answered "patch-free".
    # `check()` strips the flag for a server that does not know the setting,
    # exactly as `server/core/ch.py` does at run time. The builders AND the
    # server-side answer with the deployment flag
    # (`settings.vector_search_quantized_codes`), so that flag is pinned on
    # for the render too: a `.env` that turns it off would otherwise move
    # every vector-ranked shape (142 of them) between the CLI and CI. A case
    # that enumerates the flag as an axis still renders both forms.
    set_quantized_codes_supported(True)
    patch_parts.reset()
    with _flushers_held(), _settings_applied({"vector_search_quantized_codes": True}):
        try:
            runs = [await run_case(c) for c in (cases or all_cases())]
        finally:
            set_quantized_codes_supported(None)
            patch_parts.reset()
            await _drop_detached_flushers()
    return runs, collect(runs)


@contextlib.contextmanager
def _flushers_held() -> Iterator[None]:
    """A handler driven here would start the usage-event flusher — a
    detached 0.5 s task that resolves the real client at fire time and logs
    a failure per tick; hold it off for the render. Events still buffer, and
    `_drop_detached_flushers` drops them."""
    try:
        from server.memory.store import usage as usage
    except Exception:  # noqa: BLE001 — nothing to hold
        yield
        return
    saved = usage._ensure_usage_flusher_started
    usage._ensure_usage_flusher_started = lambda: None
    try:
        yield
    finally:
        usage._ensure_usage_flusher_started = saved


async def _drop_detached_flushers() -> None:
    """A handler driven here may start the usage-event flusher (a detached
    0.5 s task that resolves the real client at fire time); cancel it and
    drop its buffer so nothing outlives the render."""
    try:
        from server.memory.store import usage as usage
    except Exception:  # noqa: BLE001 — nothing to drop
        return
    task = getattr(usage, "_USAGE_FLUSH_TASK", None)
    if task is not None and not task.done():
        task.cancel()
        with contextlib.suppress(BaseException):
            await task
    usage._USAGE_FLUSH_TASK = None
    usage._USAGE_BUFFER.clear()


def read_shapes(path: Path = SHAPES_PATH) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        shape, _, head = line.partition("\t")
        out[shape] = head
    return out


def shapes_text(heads: dict[str, str]) -> str:
    """The file `write_shapes` writes for `{shape: head}`: header, then one
    sorted line per shape."""
    lines = [
        "# Statement shapes the analyzer corpus renders.",
        "# <shape12>\\t<first 200 chars, literals masked>; sorted. Regenerate with",
        "#   python -m tests.analyzer.corpus --write",
        "# and review the diff: a new line is a new statement shape.",
    ]
    lines += [f"{s}\t{heads[s]}" for s in sorted(heads)]
    return "\n".join(lines) + "\n"


def write_shapes(shapes: dict[str, Shape], path: Path = SHAPES_PATH) -> None:
    path.write_text(shapes_text({s: shape.head for s, shape in shapes.items()}), encoding="utf-8")


# --------------------------------------------------------------------------- params

_PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*):((?:[^{}]|\{[^{}]*\})+)\}")


def placeholders(sql: str) -> dict[str, str]:
    """`{name:Type}` → type, first occurrence wins."""
    out: dict[str, str] = {}
    for m in _PLACEHOLDER.finditer(sql):
        out.setdefault(m.group(1), m.group(2).strip())
    return out


def _split_top(s: str) -> list[str]:
    parts, depth, cur = [], 0, []
    for ch in s:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    if cur:
        parts.append("".join(cur).strip())
    return parts


def dummy_value(ch_type: str) -> Any:
    """A typed dummy for one ClickHouse type — enough for the analyzer."""
    t = ch_type.strip()
    for wrapper in ("Nullable", "LowCardinality"):
        if t.startswith(wrapper + "(") and t.endswith(")"):
            return dummy_value(t[len(wrapper) + 1 : -1])
    if t.startswith("Array(") and t.endswith(")"):
        return [dummy_value(t[6:-1])]
    if t.startswith("Tuple(") and t.endswith(")"):
        return tuple(
            dummy_value(x.split(" ", 1)[-1] if " " in x and not x.startswith("(") else x)
            for x in _split_top(t[6:-1])
        )
    if t.startswith("Map(") and t.endswith(")"):
        k, v = _split_top(t[4:-1])
        return {dummy_value(k): dummy_value(v)}
    if t.startswith(("FixedString", "String", "Enum")):
        return "x"
    if t.startswith(("UInt", "Int")):
        return 1
    if t.startswith(("Float", "Decimal")):
        return 0.5
    if t == "Bool":
        return True
    if t.startswith("DateTime64"):
        return _dt.datetime(2026, 1, 1, 0, 0, 0)
    if t.startswith("DateTime"):
        return _dt.datetime(2026, 1, 1, 0, 0, 0)
    if t.startswith("Date"):
        return _dt.date(2026, 1, 1)
    if t == "UUID":
        return uuid.UUID(int=1)
    if t.startswith("IPv"):
        return "127.0.0.1"
    return "x"


def params_for(sql: str, given: dict[str, Any] | None = None) -> dict[str, Any]:
    """The builder's own parameters where it supplied them, typed dummies
    for every placeholder it did not (a captured production statement has
    none); parameters the statement does not name are dropped."""
    given = given or {}
    out: dict[str, Any] = {}
    for name, typ in placeholders(sql).items():
        value = given.get(name)
        out[name] = value if value is not None and _fits(value, typ) else dummy_value(typ)
    return out


def _fits(value: Any, ch_type: str) -> bool:
    """Whether a builder-supplied value can be sent for `ch_type` — a case
    that passes 'b1' for a UUID placeholder gets the typed dummy instead, so
    the analyzer sees the statement, not the dummy."""
    t = ch_type.strip()
    for wrapper in ("Nullable", "LowCardinality"):
        if t.startswith(wrapper + "(") and t.endswith(")"):
            return _fits(value, t[len(wrapper) + 1 : -1])
    if t.startswith("Array("):
        inner = t[6:-1]
        return isinstance(value, (list, tuple)) and all(_fits(v, inner) for v in value)
    if t.startswith(("Tuple(", "Map(")):
        return True
    if t == "UUID":
        try:
            uuid.UUID(str(value))
            return True
        except ValueError:
            return False
    if t.startswith(("UInt", "Int")):
        return isinstance(value, int) and not isinstance(value, bool)
    if t.startswith(("Float", "Decimal")):
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if t == "Bool":
        return isinstance(value, (bool, int))
    if t.startswith("Date"):
        if isinstance(value, (_dt.date, _dt.datetime)):
            return True
        try:
            _dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            return True
        except ValueError:
            return False
    return isinstance(value, str)


# --------------------------------------------------------------------------- analyzer

_LEAD = re.compile(r"^\s*(?:--[^\n]*\n\s*|/\*.*?\*/\s*)*([A-Za-z]+)", re.S)
_INSERT_SELECT = re.compile(
    r"^\s*INSERT\s+INTO\s+\S+\s*(?:\([^)]*\)\s*)?(?:SETTINGS\s+[^\n]*?\s+)?(SELECT|WITH)\b(.*)$",
    re.I | re.S,
)
_ALTER = re.compile(
    r"^\s*ALTER\s+TABLE\s+(\S+)\s+(UPDATE|DELETE)\b(.*?)\bWHERE\b(.*)$", re.I | re.S
)
_LW_DELETE = re.compile(r"^\s*DELETE\s+FROM\s+(\S+)\s+WHERE\b(.*)$", re.I | re.S)
_TRAILING_SETTINGS = re.compile(r"\bSETTINGS\s+[^;]*$", re.I | re.S)

UNCHECKED = "unchecked"


def leading_keyword(sql: str) -> str:
    m = _LEAD.match(sql or "")
    return m.group(1).upper() if m else ""


def _split_assignments(body: str) -> list[str]:
    out = []
    for part in _split_top(body):
        col, eq, expr = part.partition("=")
        if not eq or not col.strip():
            raise ValueError(part)
        out.append(f"({expr.strip()}) AS {col.strip()}")
    return out


def analyzable(sql: str) -> tuple[str, str | None]:
    """(class, statement to hand to EXPLAIN QUERY TREE or None).

    - select:       SELECT / WITH as written;
    - insert_select: the SELECT of an INSERT … SELECT;
    - mutation:     `ALTER TABLE t UPDATE a = x WHERE p` → `SELECT (x) AS a
                    FROM t WHERE p`; `… DELETE WHERE p` and `DELETE FROM t
                    WHERE p` → `SELECT count() FROM t WHERE p`;
    - unchecked:    everything else (DDL, SYSTEM, OPTIMIZE, VALUES inserts).
    """
    kw = leading_keyword(sql)
    if kw in ("SELECT", "WITH"):
        return "select", sql
    if kw == "INSERT":
        m = _INSERT_SELECT.match(sql)
        if m:
            return "insert_select", f"{m.group(1)}{m.group(2)}"
        return UNCHECKED, None
    if kw == "ALTER":
        m = _ALTER.match(sql)
        if not m:
            return UNCHECKED, None
        table, verb, body, where = m.group(1), m.group(2).upper(), m.group(3), m.group(4)
        where = _TRAILING_SETTINGS.sub("", where).strip()
        if verb == "DELETE":
            return "mutation", f"SELECT count() FROM {table} WHERE {where}"
        try:
            cols = ", ".join(_split_assignments(body))
        except ValueError:
            cols = "count()"
        return "mutation", f"SELECT {cols} FROM {table} WHERE {where}"
    if kw == "DELETE":
        m = _LW_DELETE.match(sql)
        if not m:
            return UNCHECKED, None
        where = _TRAILING_SETTINGS.sub("", m.group(2)).strip()
        return "mutation", f"SELECT count() FROM {m.group(1)} WHERE {where}"
    return UNCHECKED, None


def server_knows_setting(client: Any, name: str) -> bool:
    rows = client.query(
        "SELECT count() FROM system.settings WHERE name = {n:String}", parameters={"n": name}
    ).result_rows
    return bool(rows and rows[0][0])


@dataclasses.dataclass
class Verdict:
    shape: str
    cls: str
    ok: bool
    error: str = ""
    ms: float = 0.0


def check(
    shapes: dict[str, Shape], client: Any, *, quantized_known: bool | None = None
) -> list[Verdict]:
    """Every shape through the analyzer on `client` (a sync clickhouse_connect
    client against a migrated database). Returns one verdict per shape;
    `unchecked` shapes are listed with ok=True and no round trip."""
    from server.core.ch_errors import strip_quantized_setting

    if quantized_known is None:
        quantized_known = server_knows_setting(client, "vector_search_use_quantized_codes")
    out: list[Verdict] = []
    for s in sorted(shapes):
        entry = shapes[s]
        cls, stmt = analyzable(entry.sql)
        if stmt is None:
            out.append(Verdict(s, cls, True))
            continue
        if not quantized_known:
            stmt = strip_quantized_setting(stmt)
        params = params_for(stmt, entry.params)
        t0 = time.perf_counter()
        try:
            client.query(f"EXPLAIN QUERY TREE {stmt}", parameters=params)
            out.append(Verdict(s, cls, True, ms=(time.perf_counter() - t0) * 1000))
        except Exception as exc:  # noqa: BLE001 — the message is the finding
            out.append(Verdict(s, cls, False, str(exc)[:400], (time.perf_counter() - t0) * 1000))
    return out


def import_capture(path: Path, shapes: dict[str, Shape]) -> int:
    """Merge a `LOOM_CH_CAPTURE_STATEMENTS` file (one JSON object per line,
    `sql` + `digest`) into `shapes`; returns the number of new shapes."""
    added = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        sql = str(obj.get("sql") or "")
        if not sql:
            continue
        s = shape_of(sql)
        if s not in shapes:
            shapes[s] = Shape(s, masked_head(sql), sql, {})
            added += 1
        shapes[s].cases.add(f"capture:{path.name}")
        shapes[s].modules.add("capture")
    return added


# --------------------------------------------------------------------------- reporting


def sql_constant_census(root: Path = ROOT / "server") -> dict[str, int]:
    """SQL-shaped string constants per module (the eval's denominator): a
    string literal or f-string carrying SELECT/INSERT/ALTER/DELETE/WITH and a
    FROM/INTO/TABLE keyword."""
    import ast

    pat = re.compile(
        r"\b(SELECT|INSERT|ALTER|DELETE|WITH|OPTIMIZE|SYSTEM)\b.*\b(FROM|INTO|TABLE|SELECT)\b",
        re.S | re.I,
    )
    out: dict[str, int] = {}
    for py in sorted(root.rglob("*.py")):
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        n = 0
        for node in ast.walk(tree):
            text = None
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                text = node.value
            elif isinstance(node, ast.JoinedStr):
                text = "".join(
                    v.value
                    for v in node.values
                    if isinstance(v, ast.Constant) and isinstance(v.value, str)
                )
            if text and len(text) > 20 and pat.search(text):
                n += 1
        if n:
            out[str(py.relative_to(ROOT))] = n
    return out


def module_of_case(case_module: str) -> str:
    return case_module.replace(".", "/") + ".py"


def report(
    runs: Sequence[CaseRun], shapes: dict[str, Shape], verdicts: Sequence[Verdict] | None
) -> str:
    census = sql_constant_census()
    by_module: dict[str, set[str]] = {}
    for s in shapes.values():
        for m in s.modules:
            by_module.setdefault(module_of_case(m), set()).add(s.shape)
    lines = []
    lines.append(
        f"cases: {len(runs)}   combinations: {sum(len(r.case.combos()) for r in runs)}   "
        f"statements: {sum(len(r.rendered) for r in runs)}   unique shapes: {len(shapes)}"
    )
    if verdicts is not None:
        checked = [v for v in verdicts if v.cls != UNCHECKED]
        bad = [v for v in verdicts if not v.ok]
        ms = sorted(v.ms for v in checked)
        p50 = ms[len(ms) // 2] if ms else 0.0
        lines.append(
            f"checked: {len(checked)}   unchecked: {len(verdicts) - len(checked)}   "
            f"rejected: {len(bad)}   analyzer ms/shape p50 {p50:.1f} total {sum(ms):.0f}"
        )
        for v in bad:
            lines.append(f"  REJECTED {v.shape} [{v.cls}] {v.error}")
            lines.append(f"    cases: {sorted(shapes[v.shape].cases)}")
            lines.append(f"    {shapes[v.shape].head}")
    lines.append("")
    lines.append(f"{'module':60} {'constants':>9} {'shapes':>6}")
    covered_modules = 0
    for mod in sorted(set(census) | set(by_module), key=lambda m: (-census.get(m, 0), m)):
        n = len(by_module.get(mod, ()))
        if n:
            covered_modules += 1
        if census.get(mod, 0) >= 5 or n:
            lines.append(f"{mod:60} {census.get(mod, 0):>9} {n:>6}")
    lines.append(
        f"modules with SQL constants: {len(census)}   reached by a case: {covered_modules}"
    )
    errs = [(r.case.name, e) for r in runs for e in r.errors]
    empty = [r.case.name for r in runs if not r.rendered]
    if empty:
        lines.append(f"cases that rendered nothing: {empty}")
    lines.append(f"drive errors (a builder that stopped after its first statement): {len(errs)}")
    return "\n".join(lines)


def target_label() -> str:
    """`host:port` of the ClickHouse the check hits (the LOOM_CH_* environment)."""
    return f"{os.environ.get('LOOM_CH_HOST', 'localhost')}:{os.environ.get('LOOM_CH_PORT', '8123')}"


def pending_migrations(client: Any) -> tuple[str, ...]:
    """Repo migration files the target's `loom._migrations` ledger lacks.

    The analyzer resolves names against the target's tables, so a shape that
    reads a table or column a pending migration adds is rejected with code 60
    or 47 — a target that is behind, not a statement defect. The check runs
    against a migrated database (README: "Running"); callers use this to tell
    the two apart before reading verdicts."""
    from server.core.schema.migrate import applied_migrations, ledger_report

    files = sorted((ROOT / "schemas" / "migrations").glob("*.sql"))
    return ledger_report(applied_migrations(client), files).pending


def _ch_client() -> Any:
    import clickhouse_connect

    port = int(os.environ.get("LOOM_CH_PORT", "8123"))
    secure = port == 8443 or os.environ.get("LOOM_CH_SECURE", "").lower() == "true"
    return clickhouse_connect.get_client(
        host=os.environ.get("LOOM_CH_HOST", "localhost"),
        port=port,
        username=os.environ.get("LOOM_CH_USER", "loom"),
        password=os.environ.get("LOOM_CH_PASSWORD", "loomdev"),
        database=os.environ.get("LOOM_CH_DATABASE", "loom"),
        secure=secure,
    )


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--write", action="store_true", help="rewrite shapes.txt from the builders")
    ap.add_argument(
        "--check", action="store_true", help="run every shape through EXPLAIN QUERY TREE"
    )
    ap.add_argument("--report", action="store_true", help="print the coverage table")
    ap.add_argument(
        "--import",
        dest="imports",
        action="append",
        default=[],
        help="a LOOM_CH_CAPTURE_STATEMENTS file to check too",
    )
    ap.add_argument(
        "--case", action="append", default=[], help="only cases whose name contains this"
    )
    ap.add_argument("--errors", action="store_true", help="print drive errors per case")
    ap.add_argument("--dump", help="write every unique shape's full SQL to this file (JSON lines)")
    args = ap.parse_args(argv)

    cases = all_cases()
    if args.case:
        cases = [c for c in cases if any(f in c.name for f in args.case)]
    runs, shapes = asyncio.run(render_all(cases))
    for p in args.imports:
        n = import_capture(Path(p), shapes)
        print(f"imported {p}: {n} new shapes")
    verdicts = None
    if args.check:
        client = _ch_client()
        pending = pending_migrations(client)
        if pending:
            print(
                f"WARNING: {target_label()} lacks {len(pending)} migration(s) ({pending[0]} … {pending[-1]}); "
                "rejections naming their tables or columns are the target, not the statement — "
                "python -m server.core.schema.migrate"
            )
        verdicts = check(shapes, client)
    if args.write:
        write_shapes(shapes)
        print(f"wrote {len(shapes)} shapes to {SHAPES_PATH}")
    if args.dump:
        with open(args.dump, "w", encoding="utf-8") as fh:
            for s in sorted(shapes):
                fh.write(
                    json.dumps({"shape": s, "cases": sorted(shapes[s].cases), "sql": shapes[s].sql})
                    + "\n"
                )
    if args.errors:
        for r in runs:
            for e in r.errors:
                print(f"{r.case.name}: {e}")
    if args.report or args.check or not (args.write or args.dump):
        print(report(runs, shapes, verdicts))
    return 1 if verdicts is not None and any(not v.ok for v in verdicts) else 0


if __name__ == "__main__":
    raise SystemExit(main())
