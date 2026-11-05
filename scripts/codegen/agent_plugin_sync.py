"""Keep the Codex plugin's scripts, skills and icon byte-identical to the Claude Code plugin's.

The two coding-agent plugins differ only in their manifests and hook wiring
(`hooks/hooks.json`); the hook scripts (`hooks/*.mjs`), the shared library
(`lib/*.mjs`), the `bin/` helpers, the skills (`skills/**`) and the listing icon (`assets/`) are one
source of truth under `integrations/claude-code-plugin/`, copied here so each
plugin is a self-contained directory a marketplace can check out on its own
(Codex supports sparse checkouts; a `../` reference would break there).

    python scripts/codegen/agent_plugin_sync.py           # copy
    python scripts/codegen/agent_plugin_sync.py --check   # exit 1 when a copy is stale (CI)

A copy matches its source in bytes and in the executable bit.

Adding a hook script or a skill to the Claude Code plugin: run this once and
commit the copies; wire the new hook into the Codex `hooks.json` by hand if
Codex should run it (the Codex file carries the client name as argv[3]).
"""

from __future__ import annotations

import argparse
import filecmp
import stat
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:  # run as a file
    sys.path.insert(0, str(ROOT))

from scripts.codegen.common import write_atomic  # noqa: E402

SOURCE = ROOT / "integrations" / "claude-code-plugin"
TARGET = ROOT / "integrations" / "codex-plugin"
COMMAND = "python scripts/codegen/agent_plugin_sync.py"

# (subdirectory, glob) mirrored from SOURCE into TARGET
MIRRORED: tuple[tuple[str, str], ...] = (
    ("hooks", "*.mjs"),
    ("bin", "*"),  # the Node launcher and the MCP header helper
    ("lib", "*.mjs"),
    ("skills", "**/*"),
    ("assets", "*"),  # the listing icon
)


def _files(root: Path) -> list[Path]:
    return [
        p
        for subdir, pattern in MIRRORED
        for p in sorted((root / subdir).glob(pattern))
        if p.is_file()
    ]


def _pairs() -> list[tuple[Path, Path]]:
    return [(src, TARGET / src.relative_to(SOURCE)) for src in _files(SOURCE)]


def _stale_targets() -> list[Path]:
    """Files under the mirrored target dirs that no source file produces."""
    produced = {dst for _, dst in _pairs()}
    return [p for p in _files(TARGET) if p not in produced]


def _executable(path: Path) -> bool:
    """The one mode bit git records: whether the owner may execute the file."""
    return bool(path.stat().st_mode & stat.S_IXUSR)


def _mirror_mode(src: Path, dst: Path) -> bool:
    """Give `dst` the executable bit of `src`; True when that changed it."""
    if _executable(src) == _executable(dst):
        return False
    mode = dst.stat().st_mode
    exec_bits = stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
    # A new executable bit follows the read bits, as git's checkout does.
    wanted = mode | ((mode & 0o444) >> 2) if _executable(src) else mode & ~exec_bits
    dst.chmod(stat.S_IMODE(wanted))
    return True


def check() -> list[str]:
    problems: list[str] = []
    for src, dst in _pairs():
        if not dst.exists():
            problems.append(f"missing: {dst.relative_to(ROOT)}")
        elif not filecmp.cmp(src, dst, shallow=False):
            problems.append(f"differs: {dst.relative_to(ROOT)}")
        elif _executable(src) != _executable(dst):
            problems.append(f"mode differs (executable bit): {dst.relative_to(ROOT)}")
    for p in _stale_targets():
        problems.append(f"orphan (no source): {p.relative_to(ROOT)}")
    return problems


def sync() -> int:
    n = 0
    for src, dst in _pairs():
        wrote = write_atomic(dst, src.read_bytes())
        n += _mirror_mode(src, dst) or wrote
    for p in _stale_targets():
        p.unlink()
        n += 1
    return n


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--check", action="store_true", help="exit 1 when a copy is stale")
    args = ap.parse_args(argv)
    if args.check:
        problems = check()
        for p in problems:
            print(p, file=sys.stderr)
        if problems:
            print(f"codex plugin copies are stale — run: {COMMAND}", file=sys.stderr)
            return 1
        print("codex plugin copies are current")
        return 0
    n = sync()
    print(f"synced {n} file(s) into {TARGET.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
