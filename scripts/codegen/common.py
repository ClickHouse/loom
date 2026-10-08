"""What the generators share: the repository root, name casing, the facts an
SDK table carries per op, ruff formatting, the atomic write, and the
write-or-check protocol for a generated file."""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from server.operations.base import Operation

ROOT = Path(__file__).resolve().parents[2]

_WORD_SPLIT = re.compile(r"[._\-\s]+")
_NON_IDENT = re.compile(r"[^A-Za-z0-9_]")


def pascal(name: str) -> str:
    """`memory.set_from_messages` → `MemorySetFromMessages`."""
    return "".join(p[:1].upper() + p[1:] for p in _WORD_SPLIT.split(name) if p)


def ident(name: str) -> str:
    """`name` with every non-identifier character replaced, never starting with a digit."""
    n = _NON_IDENT.sub("_", name)
    return f"_{n}" if n[:1].isdigit() else n


@dataclass(frozen=True)
class OpFacts:
    """The declared spec of one served op, as the SDK tables spell it."""

    name: str
    idempotent: bool
    role: str  # the role's name (viewer / writer / admin / org_admin), not its ordinal
    maturity: str
    budget_p99_ms: int
    audit: bool

    @classmethod
    def of(cls, op: Operation) -> OpFacts:
        return cls(
            name=op.name,
            idempotent=bool(op.idempotent),
            role=op.required_role.name.lower(),
            maturity=op.maturity,
            budget_p99_ms=int(op.budget_p99_ms),
            audit=bool(op.audit),
        )


def running_checkout() -> Path | None:
    """The checkout whose scripts/ or schemas/ holds the running script, or
    None when the entry point is not a repository script (pytest, python -c)."""
    main = getattr(sys.modules.get("__main__"), "__file__", None)
    if not main:
        return None
    script = Path(main).resolve()
    for d in script.parents:
        if (d / "scripts" / "__init__.py").is_file() and (d / "server" / "__init__.py").is_file():
            return d if script.relative_to(d).parts[0] in ("scripts", "schemas") else None
    return None


def refuse_foreign_checkout() -> None:
    """Exit when the running script's checkout is not the one this module (and
    so every generated path) belongs to: a script run by file path from one
    checkout that imported another's `scripts` package (an editable install)
    would write that other checkout's files from its code."""
    run = running_checkout()
    if run is not None and run != ROOT:
        raise SystemExit(
            f"refusing to write: {sys.modules['__main__'].__file__} belongs to {run}, but "
            f"`scripts` was imported from {ROOT}. Put {run} first on sys.path "
            f"(run it as `python -m` from {run}, or with PYTHONPATH={run})."
        )


def write_atomic(path: Path, data: bytes) -> bool:
    """Replace `path` with `data` through a sibling temporary file, so a reader
    never sees a half-written file. Returns False, writing nothing, when the
    file already holds `data`. An existing file keeps its mode. Refuses to
    write for a script of another checkout (`refuse_foreign_checkout`)."""
    refuse_foreign_checkout()
    if path.is_file() and path.read_bytes() == data:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        if path.exists():
            os.chmod(tmp, path.stat().st_mode & 0o7777)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return True


def ruff_format(text: str, stdin_filename: str) -> str:
    """`text` formatted by `ruff format` under the repository's config, as if
    it were the file `stdin_filename` (relative to the root). Tries the running
    interpreter's ruff, then one on PATH; raises when neither runs, so a
    generator never writes or compares unformatted text."""
    candidates: list[list[str]] = [[sys.executable, "-m", "ruff"]]
    exe = shutil.which("ruff")
    if exe:
        candidates.append([exe])
    for base in candidates:
        try:
            res = subprocess.run(
                [*base, "format", "--stdin-filename", stdin_filename, "-"],
                input=text,
                capture_output=True,
                text=True,
                check=True,
                cwd=str(ROOT),
            )
        except (OSError, subprocess.CalledProcessError):
            continue
        return res.stdout
    raise RuntimeError(f"ruff format failed for {stdin_filename}: install the dev extras")


def _unchanged(text: str) -> str:
    return text


@dataclass(frozen=True)
class GeneratedFile:
    """A checked-in file a generator owns. `normalise` is applied to both
    sides before `--check` compares them."""

    path: Path
    command: str  # how to regenerate it
    normalise: Callable[[str], str] = _unchanged

    @property
    def display(self) -> str:
        try:
            return self.path.resolve().relative_to(ROOT).as_posix()
        except ValueError:
            return str(self.path)

    def is_current(self, text: str) -> bool:
        current = self.path.read_text(encoding="utf-8") if self.path.exists() else ""
        return self.normalise(current) == self.normalise(text)

    def sync(self, text: str, *, check: bool) -> int:
        """`check`: 1 when the file differs from `text`, and never writes.
        Otherwise write `text` when its bytes differ from the file's."""
        if check:
            if not self.is_current(text):
                print(f"{self.display} is stale: run {self.command}", file=sys.stderr)
                return 1
            print(f"{self.display} is current")
            return 0
        if write_atomic(self.path, text.encode("utf-8")):
            print(f"wrote {self.display} ({text.count(chr(10))} lines)")
        else:
            print(f"{self.display} is current")
        return 0


def generator_main(
    doc: str | None, target: GeneratedFile, build: Callable[[], str], argv: list[str] | None
) -> int:
    """The command line every single-file generator shares: `--check`, `--out`."""
    ap = argparse.ArgumentParser(
        description=doc, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--check", action="store_true", help="exit 1 when the file is stale")
    ap.add_argument("--out", type=Path, default=target.path, help=f"default {target.display}")
    args = ap.parse_args(argv)
    return replace(target, path=args.out).sync(build(), check=args.check)
