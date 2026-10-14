"""Server code never reads the host's local clock.

Loom's naive datetimes are UTC: `CHClient.insert` writes them as UTC instants,
and query parameters reach ClickHouse as UTC wall time. A naive local reading
(`datetime.now()`, `datetime.today()`, `fromtimestamp(x)` without `tz`) lands
shifted by the host's offset on any host not in UTC; `utcnow` and
`utcfromtimestamp` are deprecated. Use `naive_utc_now()` or `datetime.now(UTC)`.
"""

from __future__ import annotations

import ast
from pathlib import Path

SERVER = Path(__file__).resolve().parents[2] / "server"
ALWAYS_LOCAL = {"today", "utcnow", "utcfromtimestamp"}


def _reads_local_clock(call: ast.Call) -> bool:
    func = call.func
    if not isinstance(func, ast.Attribute):
        return False
    target = func.value
    on_datetime = (isinstance(target, ast.Name) and target.id == "datetime") or (
        isinstance(target, ast.Attribute) and target.attr == "datetime"
    )
    if not on_datetime:
        return False
    has_tz = len(call.args) > {"now": 0, "fromtimestamp": 1}.get(func.attr, 0) or any(
        k.arg == "tz" for k in call.keywords
    )
    return func.attr in ALWAYS_LOCAL or (func.attr in ("now", "fromtimestamp") and not has_tz)


def test_no_server_module_reads_the_local_clock() -> None:
    found = [
        f"{path.relative_to(SERVER.parent)}:{node.lineno}"
        for path in sorted(SERVER.rglob("*.py"))
        for node in ast.walk(ast.parse(path.read_text(), str(path)))
        if isinstance(node, ast.Call) and _reads_local_clock(node)
    ]
    assert not found, "local-clock datetime reads (use naive_utc_now()):\n" + "\n".join(found)


def test_the_check_tells_local_readings_from_utc_ones() -> None:
    def judged(src: str) -> bool:
        call = ast.parse(src).body[0].value  # type: ignore[attr-defined]
        return _reads_local_clock(call)

    assert judged("datetime.now()") and judged("datetime.datetime.now()")
    assert judged("datetime.today()") and judged("datetime.utcnow()")
    assert judged("datetime.fromtimestamp(t)")
    assert not judged("datetime.now(UTC)") and not judged("datetime.now(tz=UTC)")
    assert not judged("datetime.fromtimestamp(t, UTC)")
    assert not judged("datetime.fromtimestamp(t, tz=UTC)")
    assert not judged("date.today()")
