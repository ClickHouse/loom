"""Server code does not ask for the host's local time.

Loom's naive datetimes are UTC: `CHClient.insert` writes them as UTC instants,
and query parameters reach ClickHouse as UTC wall time. A local reading lands
shifted by the host's offset on any host not in UTC. Use `naive_utc_now()` or
`datetime.now(UTC)`.

The check is by call shape, whatever the receiver is named: `.now()`,
`.today()` and `.astimezone()` with no zone, `.fromtimestamp(x)` and
`.asctime()` with none, and the calls that are local or deprecated outright. It
cannot see `naive.timestamp()`, which reads a naive value as local (use
`time.time_ns()` for a version), nor `time.ctime()` / `strftime(fmt)`, whose
shapes a datetime's own methods share.
"""

from __future__ import annotations

import ast
from pathlib import Path

SERVER = Path(__file__).resolve().parents[2] / "server"
BANNED = {"utcnow", "utcfromtimestamp", "localtime", "mktime"}
# Where each call takes its zone (or time tuple) by position; none there is local.
TZ_POSITION = {"now": 0, "today": 0, "astimezone": 0, "asctime": 0, "fromtimestamp": 1}


def _reads_local_clock(call: ast.Call) -> bool:
    if not isinstance(call.func, ast.Attribute):
        return False
    name = call.func.attr
    if name in BANNED:
        return True
    if name not in TZ_POSITION:
        return False
    zones = call.args[TZ_POSITION[name] :] + [k.value for k in call.keywords if k.arg == "tz"]
    return all(isinstance(z, ast.Constant) and z.value is None for z in zones)  # none, or None


def test_no_server_module_reads_the_local_clock() -> None:
    found = [
        f"{path.relative_to(SERVER.parent)}:{node.lineno}"
        for path in sorted(SERVER.rglob("*.py"))
        for node in ast.walk(ast.parse(path.read_text(), str(path)))
        if isinstance(node, ast.Call) and _reads_local_clock(node)
    ]
    assert not found, "local-clock reads (use naive_utc_now()):\n" + "\n".join(found)


def test_the_check_tells_local_readings_from_utc_ones() -> None:
    def judged(src: str) -> bool:
        call = ast.parse(src, mode="eval").body
        assert isinstance(call, ast.Call)
        return _reads_local_clock(call)

    local = [
        "datetime.now()",
        "datetime.datetime.now()",
        "_dt.now()",
        "datetime.now(None)",
        "datetime.now(tz=None)",
        "date.today()",
        "datetime.fromtimestamp(t)",
        "x.astimezone()",
        "time.asctime()",
        "datetime.utcnow()",
        "time.localtime()",
        "time.mktime(t)",
    ]
    utc = [
        "datetime.now(UTC)",
        "datetime.now(tz=UTC)",
        "datetime.fromtimestamp(t, UTC)",
        "datetime.fromtimestamp(t, tz=UTC)",
        "x.astimezone(UTC)",
        "time.asctime(time.gmtime())",
        "naive_utc_now()",
        "time.time_ns()",
    ]
    assert [s for s in local if not judged(s)] == []
    assert [s for s in utc if judged(s)] == []
