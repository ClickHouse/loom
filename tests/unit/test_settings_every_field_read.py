"""Every Settings field has a reader.

Retiring a branch deletes the code that read a flag; this gate makes the flag
go with it. A field counts as read when, outside `server/settings/`, code
under `server/` or `scripts/` names it as an attribute (`settings.x`,
`s.x`) or as a string literal (the worker registry's enable and interval
columns), or when a plain settings method or property reads `self.x` (a
validator that only checks the field does not count).
"""

from __future__ import annotations

import ast
from pathlib import Path

from server.settings import Settings

ROOT = Path(__file__).resolve().parents[2]
FIELDS = frozenset(Settings.model_fields)
_VALIDATORS = {"field_validator", "model_validator"}


def _is_validator(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    for dec in fn.decorator_list:
        target = dec.func if isinstance(dec, ast.Call) else dec
        name = target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", "")
        if name in _VALIDATORS:
            return True
    return False


def _settings_method_reads() -> set[str]:
    out: set[str] = set()
    for path in (ROOT / "server" / "settings").glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if _is_validator(node) or node.name.startswith("__"):
                continue
            for sub in ast.walk(node):
                if (
                    isinstance(sub, ast.Attribute)
                    and isinstance(sub.value, ast.Name)
                    and sub.value.id == "self"
                    and sub.attr in FIELDS
                ):
                    out.add(sub.attr)
    return out


def _outside_reads() -> set[str]:
    out: set[str] = set()
    settings_dir = ROOT / "server" / "settings"
    for top in ("server", "scripts"):
        for path in (ROOT / top).rglob("*.py"):
            if settings_dir in path.parents:
                continue
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Attribute) and node.attr in FIELDS:
                    out.add(node.attr)
                elif (
                    isinstance(node, ast.Constant)
                    and isinstance(node.value, str)
                    and node.value in FIELDS
                ):
                    out.add(node.value)
    return out


def test_every_settings_field_is_read() -> None:
    unread = sorted(FIELDS - _outside_reads() - _settings_method_reads())
    assert not unread, (
        "settings fields nothing reads (delete the field, its comment and its "
        f"env/deploy references, then regenerate the reference): {unread}"
    )


def test_a_validator_alone_is_not_a_reader() -> None:
    src = """
class S:
    @field_validator("x")
    def _check(cls, v):
        return self.x

    def used(self):
        return self.y
"""
    fns = [n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.FunctionDef)]
    assert [_is_validator(f) for f in fns] == [True, False]
