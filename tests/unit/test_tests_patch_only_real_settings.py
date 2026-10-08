"""A test patches only settings that exist.

`monkeypatch.setattr(settings, "name", value, raising=False)` creates the
attribute when `Settings` has no such field, and `getattr(settings, "name",
default)` answers the default. After a field is deleted either test keeps
passing: it sets or reads a name nothing else does, and the branch it meant
to exercise is gone. `settings_override` already refuses unknown names at
run time; this scan covers the direct `setattr` and `getattr` spellings in
every test file.
"""

from __future__ import annotations

import ast
from pathlib import Path

from server.settings import Settings

TESTS = Path(__file__).resolve().parents[1]
_KNOWN = set(Settings.model_fields) | set(dir(Settings))


def _singleton_names(tree: ast.Module) -> set[str]:
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "server.settings":
            names.update(a.asname or a.name for a in node.names if a.name == "settings")
    return names


def _patched_fields(path: Path) -> list[tuple[int, str]]:
    text = path.read_text(encoding="utf-8")
    if ("setattr" not in text and "getattr" not in text) or "settings" not in text:
        return []
    tree = ast.parse(text)
    singleton = _singleton_names(tree)
    out = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and len(node.args) >= 2):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name not in ("setattr", "getattr"):
            continue
        target, field = node.args[0], node.args[1]
        if name == "setattr" and isinstance(target, ast.Constant) and isinstance(target.value, str):
            # monkeypatch.setattr("server.settings.settings.<field>", value)
            prefix = "server.settings.settings."
            if target.value.startswith(prefix):
                out.append((node.lineno, target.value[len(prefix) :]))
            continue
        on_singleton = (isinstance(target, ast.Name) and target.id in singleton) or (
            isinstance(target, ast.Attribute)
            and target.attr == "settings"
            and isinstance(target.value, ast.Attribute | ast.Name)
            and ast.unparse(target.value) in ("server.settings", "settings_mod", "srv_settings")
        )
        if on_singleton and isinstance(field, ast.Constant) and isinstance(field.value, str):
            out.append((node.lineno, field.value))
    return out


def test_every_patched_settings_name_is_a_settings_field() -> None:
    unknown = [
        f"{path.relative_to(TESTS.parent)}:{line}: {field}"
        for path in sorted(TESTS.rglob("*.py"))
        if "fixtures" not in path.parts
        for line, field in _patched_fields(path)
        if field not in _KNOWN
    ]
    assert not unknown, "tests patch settings that do not exist:\n" + "\n".join(unknown)


def test_the_scan_sees_each_spelling(tmp_path: Path) -> None:
    probe = tmp_path / "test_probe.py"
    probe.write_text(
        "from server.settings import settings as s\n"
        "import server.settings\n"
        "def test(monkeypatch):\n"
        "    monkeypatch.setattr(s, 'gone_a', 1, raising=False)\n"
        "    monkeypatch.setattr(server.settings.settings, 'gone_b', 1)\n"
        "    monkeypatch.setattr('server.settings.settings.gone_c', 1)\n"
        "    setattr(s, 'gone_d', 1)\n"
        "    assert getattr(s, 'gone_e', None) is None\n"
        "    monkeypatch.setattr(other, 'not_settings', 1)\n"
        "    getattr(other, 'not_settings_either', None)\n",
        encoding="utf-8",
    )
    assert [f for _, f in _patched_fields(probe)] == [
        "gone_a",
        "gone_b",
        "gone_c",
        "gone_d",
        "gone_e",
    ]
