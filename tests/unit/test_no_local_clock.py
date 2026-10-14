"""Catches local-clock calls by shape; misses naive .timestamp(), time.ctime(), time.strftime()."""

from __future__ import annotations

import ast
from pathlib import Path

SERVER = Path(__file__).resolve().parents[2] / "server"
# today() takes no zone at all.
BANNED = {"utcnow", "utcfromtimestamp", "localtime", "mktime", "today"}
# Where each call takes the argument that makes it UTC by position (a zone, or
# asctime's time tuple); none there reads the local clock.
UTC_ARG_POSITION = {"now": 0, "astimezone": 0, "asctime": 0, "fromtimestamp": 1}


def _reads_local_clock(call: ast.Call) -> bool:
    if not isinstance(call.func, ast.Attribute):
        return False
    name = call.func.attr
    if name in BANNED:
        return True
    if name not in UTC_ARG_POSITION:
        return False
    args = call.args[UTC_ARG_POSITION[name] :] + [k.value for k in call.keywords if k.arg == "tz"]
    return all(isinstance(z, ast.Constant) and z.value is None for z in args)  # none, or None


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
        "datetime.today(UTC)",
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
