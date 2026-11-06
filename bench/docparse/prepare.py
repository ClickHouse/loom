"""The corpus build's first step, before anything is rendered or removed: the
PEP pages it reads are there, the directory is a corpus (or one a build
started in), and the tools container sees it at /c; then the directory is
emptied. Apart from `corpus.py` so it runs without the rendering libraries."""

from __future__ import annotations

import shutil
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path

# The public-domain PEPs in the corpus, and the elements each keeps.
PEPS = [(518, 40), (621, 45), (668, 45), (723, 40), (735, 40), (691, 40)]


@dataclass(frozen=True)
class _Dirs:
    """The corpus directory (mounted at /c in the tools container) and its scratch."""

    out: Path
    work: Path

    def in_container(self, path: Path) -> str:
        """`path`, under the corpus directory, as the container sees it."""
        return f"/c/{path.relative_to(self.out)}"


CONTAINER = "docparse-tools-run"


def docker(*cmd: str) -> None:
    subprocess.run(["docker", "exec", CONTAINER, *cmd], check=True, capture_output=True)


def prepare(out: Path, src: Path) -> _Dirs:
    """`out` emptied for a build, once it is safe to: SystemExit, removing
    nothing, when a PEP page is missing from `src`, `out` holds files but
    neither `gold.jsonl` nor a stopped build's `_work`, or the tools container
    does not see `out` at /c."""
    d = _Dirs(out.resolve(), out.resolve() / "_work")
    src = src.resolve()
    d.out.mkdir(parents=True, exist_ok=True)
    missing = [n for n, _ in PEPS if not (src / f"pep{n}.html").is_file()]
    if missing:
        raise SystemExit(
            f"{src} lacks pep{missing[0]}.html: save each from https://peps.python.org/pep-0NNN/"
        )
    left = {p.name for p in d.out.iterdir()}
    # a corpus holds gold.jsonl; one a build started in, its _work
    if left and not {"gold.jsonl", "_work"} & left:
        raise SystemExit(
            f"{d.out} holds files and no gold.jsonl: build writes only into a corpus directory"
        )
    # The container's /c must be this directory: a file of a fresh name, written
    # here, is looked for there before anything here is removed.
    d.work.mkdir(exist_ok=True)
    marker = d.work / f"mount-{uuid.uuid4().hex}"
    marker.touch()
    try:
        docker("test", "-f", d.in_container(marker))
    except subprocess.CalledProcessError as e:
        reason = e.stderr.decode(errors="replace").strip() or f"its /c is not {d.out}"
        raise SystemExit(
            f"the tools container {CONTAINER} cannot be used ({reason}): mount {d.out} at /c"
        ) from None
    finally:
        marker.unlink()
    for p in d.out.iterdir():  # an interrupted build's scratch too
        shutil.rmtree(p) if p.is_dir() else p.unlink()
    d.work.mkdir()
    return d
