"""The `loom` CLI surface `server.cli.main` builds, pinned argument by argument.

`main` assembles one argparse tree out of dozens of subcommands (its own and
the ones `cli_code`, `cli_okf`, `cli_up`, `cli_demo`, `cli_init` and the
tracker modules register). A refactor of how that tree is written must not
move any of it, so this test captures the root parser `main` builds (by
stopping it at `parse_args`) and compares two snapshots:

  - ``snapshots/cli_tree.json``: every parser (prog, description, epilog,
    usage, defaults such as the ``func`` each subcommand dispatches to, its
    argument groups) and every argument in it (option strings, dest, action
    class, default, const, choices, nargs, type name, required, help,
    metavar), in declaration order, recursing into every subparser;
  - ``snapshots/cli_help.json``: the ``--help`` text of ``loom`` and of every
    ``loom <cmd> [<sub>...]`` parser, as its blank-line-separated sections
    with whitespace collapsed. argparse's own layout (line wrapping, where
    ``-o OUTPUT, --output OUTPUT`` became ``-o, --output OUTPUT``) changes
    between Python versions; the words and their order are the CLI's contract.

Callables are named by ``__module__:__qualname__``. The ``--version`` string
carries the installed package version and is masked. Env-derived defaults
(``serve --host/--port``, ``--base-url``, ``--token``) are read with every
``LOOM_*`` variable unset, and settings-derived ones at the field defaults.

Regenerate (only for an intended change to the CLI surface):
``LOOM_UPDATE_CLI_SNAPSHOT=1 python -m pytest tests/unit/test_cli_surface_snapshot.py``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Any

import pytest
from pydantic_core import PydanticUndefined

from server import cli
from server.cli import app
from server.settings import settings

_SNAPSHOTS = Path(__file__).parent / "snapshots"
_TREE = _SNAPSHOTS / "cli_tree.json"
_HELP = _SNAPSHOTS / "cli_help.json"
# Read at import: the test itself unsets every LOOM_* variable.
_UPDATE = os.environ.get("LOOM_UPDATE_CLI_SNAPSHOT") == "1"


class _Captured(Exception):
    def __init__(self, parser: argparse.ArgumentParser) -> None:
        super().__init__("captured")
        self.parser = parser


def _build_parser(monkeypatch: pytest.MonkeyPatch, home: Path) -> argparse.ArgumentParser:
    # Defaults read from the environment (`serve --host`, `--base-url`,
    # `--token`, ...) or from settings (`--org` = tracker_org) at build time
    # are pinned at their declared fallbacks, whatever the shell or CI sets.
    for var in [k for k in os.environ if k.startswith("LOOM_")]:
        monkeypatch.delenv(var, raising=False)
    # `--server-url` defaults read ~/.loom/config.json: an empty home.
    monkeypatch.setenv("HOME", str(home))
    for field_name, field in type(settings).model_fields.items():
        if field.default is not PydanticUndefined:
            monkeypatch.setattr(settings, field_name, field.default)
    monkeypatch.setenv("COLUMNS", "100")
    monkeypatch.setenv("LINES", "50")
    monkeypatch.setenv("NO_COLOR", "1")  # 3.14 argparse colours help on a tty
    monkeypatch.setattr(app, "_load_dotenv_like_the_container", lambda: None)

    def _capture(self: argparse.ArgumentParser, *_a: Any, **_k: Any) -> Any:
        raise _Captured(self)

    monkeypatch.setattr(argparse.ArgumentParser, "parse_args", _capture)
    with pytest.raises(_Captured) as ex:
        cli.main([])
    return ex.value.parser


def _name(value: Any) -> Any:
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, list | tuple):
        return [_name(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _name(v) for k, v in value.items()}
    if isinstance(value, set | frozenset):
        return sorted(repr(v) for v in value)
    if callable(value):
        mod = getattr(value, "__module__", "?")
        qual = getattr(value, "__qualname__", None) or type(value).__qualname__
        return f"{mod}:{qual}"
    return repr(value)


def _action(action: argparse.Action) -> dict[str, Any]:
    out: dict[str, Any] = {
        "option_strings": list(action.option_strings),
        "dest": action.dest,
        "action": type(action).__name__,
        "default": _name(action.default),
        "const": _name(action.const),
        "choices": None
        if action.choices is None or isinstance(action.choices, dict)
        else _name(list(action.choices)),
        "nargs": _name(action.nargs),
        "type": _name(action.type),
        "required": action.required,
        "help": action.help,
        "metavar": _name(action.metavar),
    }
    if isinstance(action, argparse._VersionAction):
        out["version"] = "<masked>"
    if isinstance(action, argparse._SubParsersAction):
        out["subcommands"] = {name: _parser(p) for name, p in action.choices.items()}
        out["aliases_of"] = {name: p.prog for name, p in action.choices.items()}
        out["choices_help"] = [
            {"dest": a.dest, "help": a.help, "metavar": _name(a.metavar)}
            for a in action._choices_actions
        ]
    return out


def _parser(p: argparse.ArgumentParser) -> dict[str, Any]:
    return {
        "prog": p.prog,
        "usage": p.usage,
        "description": p.description,
        "epilog": p.epilog,
        "formatter_class": _name(p.formatter_class),
        "add_help": p.add_help,
        "allow_abbrev": p.allow_abbrev,
        "defaults": _name(dict(p._defaults)),
        "groups": [
            {
                "title": g.title,
                "description": g.description,
                "dests": [a.dest for a in g._group_actions],
            }
            for g in p._action_groups
        ],
        "mutually_exclusive": [
            {"required": g.required, "dests": [a.dest for a in g._group_actions]}
            for g in p._mutually_exclusive_groups
        ],
        "arguments": [_action(a) for a in p._actions],
    }


# `-o OUTPUT, --output OUTPUT` (before 3.13) and `-o, --output OUTPUT` (3.13+)
# name the same option; the snapshot keeps the newer spelling.
_REPEATED_METAVAR = re.compile(r"(?<!\S)(-[^\s,-][^\s,]*) (\S+), (--[^\s,]+) \2(?=[\s,]|$)")


def _help_sections(text: str) -> list[str]:
    """Rendered help with argparse's version-dependent layout removed."""
    sections = (" ".join(s.split()) for s in text.split("\n\n"))
    return [_REPEATED_METAVAR.sub(r"\1, \3 \2", s) for s in sections if s]


def _helps(p: argparse.ArgumentParser, out: dict[str, list[str]]) -> dict[str, list[str]]:
    out[p.prog] = _help_sections(p.format_help())
    for a in p._actions:
        if isinstance(a, argparse._SubParsersAction):
            for child in a.choices.values():
                if child.prog not in out:
                    _helps(child, out)
    return out


def _check(path: Path, got: Any) -> None:
    rendered = json.dumps(got, indent=1, sort_keys=False, ensure_ascii=False) + "\n"
    if _UPDATE:
        path.write_text(rendered, encoding="utf-8")
        return
    want = path.read_text(encoding="utf-8")
    assert rendered == want, f"{path.name} drifted; see the module docstring to regenerate"


def test_cli_argument_tree_is_pinned(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _check(_TREE, _parser(_build_parser(monkeypatch, tmp_path)))


def test_cli_help_output_is_pinned(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _check(_HELP, _helps(_build_parser(monkeypatch, tmp_path), {}))
