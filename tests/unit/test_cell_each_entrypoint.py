"""Cell administration starts on a client without loading server configuration."""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest


@pytest.mark.parametrize("entrypoint", ["console", "module"])
def test_each_help_never_imports_the_server_cli(entrypoint: str, tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("LOOM_JWT_SECRET=must-not-be-loaded\n", encoding="utf-8")
    source_root = Path(__file__).resolve().parents[2]
    script = textwrap.dedent(
        """
        import importlib.abc
        import os
        import runpy
        import sys

        sys.path.insert(0, sys.argv.pop(1))
        entrypoint = sys.argv.pop(1)

        class ClientOnly(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                forbidden = (
                    'server.cli.app', 'server.settings', 'server.wiring',
                    'server.core.ch', 'server.core.pg', 'server.cli._ch',
                    'server.providers', 'dotenv',
                )
                if any(fullname == p or fullname.startswith(p + '.') for p in forbidden):
                    raise AssertionError('server import: ' + fullname)
                return None

        sys.meta_path.insert(0, ClientOnly())
        sys.argv = ['loom', 'cell', 'each', '--help']
        try:
            if entrypoint == 'module':
                runpy.run_module('server.cli', run_name='__main__')
            else:
                from server.cli import main
                raise SystemExit(main())
        except SystemExit as exc:
            assert exc.code in (None, 0), exc.code
        assert os.environ.get('LOOM_JWT_SECRET') != 'must-not-be-loaded'
        """
    )
    result = subprocess.run(
        [sys.executable, "-P", "-c", script, str(source_root), entrypoint],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "--credentials" in result.stdout and "--resume" in result.stdout


def test_direct_app_call_hands_off_before_dotenv_or_wiring(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.cli import app

    def forbidden() -> None:
        raise AssertionError("each must hand off before dotenv and native wiring")

    monkeypatch.setattr(app, "_load_dotenv_like_the_container", forbidden)
    monkeypatch.setattr(app, "wire", forbidden)
    try:
        assert app.main(["cell", "each", "--help"]) == 0
    except SystemExit as exited:
        assert exited.code == 0
