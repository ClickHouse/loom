"""Document-parsing bench: the reference extractor's parse of a labelled corpus
of Loom-shaped documents, scored against its gold labels.

    PYTHONPATH=$PWD .venv/bin/python -m bench.docparse run --out /tmp/new.jsonl
    PYTHONPATH=$PWD .venv/bin/python -m bench.docparse score \\
        before=/tmp/old.jsonl after=/tmp/new.jsonl --split heldout

`run` parses each file of `bench/docparse/corpus/` with the extractor in
`--extractor` (default this checkout's `extractor/`), one child process per
file, and writes one normalized parse per line (`run.py` has the shape; another
parser is compared by writing it). `score` prints, per category and over all
files and all PDFs, each metric's mean for every parse given and the paired
difference of each from the first with a 95% bootstrap interval:

- text recall and precision: the gold text's words found in the parse, and the
  parse's words found in the gold (word multisets; a table counted as its cells);
- reading order: the share of consecutive gold paragraphs held in order;
- heading F1 and level accuracy (levels compared by rank, numbering aside);
- table cell F1 (cells at their row and column) and table detection F1;
- locator accuracy: blocks and tables cited by the page (or slide) they are on;
  notes accuracy: speaker notes in a notes block of their slide;
- lost blocks and silent loss: gold blocks the parse dropped, and files reported
  complete that lost one (or a page's text, or a tenth of the words); must be 0;
- seconds and peak memory per file.

The split (`dev`, `heldout`, `all`) is in the gold: a parser change is tuned on
`dev` and ships only when `heldout` does not get worse.

`build` regenerates the corpus from its specs (`specs.py`, `corpus.py`): the
files are rendered in a tools container (Chromium, LibreOffice and poppler;
`Dockerfile.tools`) mounted on the corpus directory, which is the only
directory `build` writes into: it refuses, before making or removing anything,
a `--src` that lacks a PEP page, an `--out` that is a file or whose parent is
no directory, one that holds files but neither `gold.jsonl` nor a stopped
build's `_work`, and one the tools container does not see at /c. The host
needs `reportlab`, `python-docx`, `python-pptx`, `pillow` and `pypdf`, and the
FreeFont fonts (Debian's `fonts-freefont-ttf`) for the whiteboard photo:

    docker build -t docparse-tools:local -f bench/docparse/Dockerfile.tools bench/docparse
    docker run -d --name docparse-tools-run --user "$(id -u):$(id -g)" -e HOME=/tmp \\
        -v "$PWD/bench/docparse/corpus:/c" docparse-tools:local sleep infinity
    uv run --with reportlab --with python-docx --with python-pptx --with pillow --with pypdf \\
        python -m bench.docparse build --src <dir with the PEP pages>
    docker rm -f docparse-tools-run

The PEP pages are `https://peps.python.org/pep-0NNN/` saved as `pepNNN.html`
(public domain); everything else is synthetic.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
CORPUS = HERE / "corpus"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m bench.docparse",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="parse the corpus with the reference extractor")
    r.add_argument("--out", type=Path, required=True)
    r.add_argument("--extractor", type=Path, default=HERE.parent.parent / "extractor")
    r.add_argument(
        "--python",
        default=sys.executable,
        help="the interpreter that has the extractor's dependencies",
    )
    r.add_argument("--only", default="", help="comma-separated file names")
    r.add_argument("--corpus", type=Path, default=CORPUS)
    s = sub.add_parser("score", help="score parses against the gold labels")
    s.add_argument(
        "parses", nargs="+", help="NAME=PARSES.jsonl, the first the one the others are paired with"
    )
    s.add_argument("--split", choices=["dev", "heldout", "all"], default="all")
    s.add_argument("--json", type=Path)
    s.add_argument("--corpus", type=Path, default=CORPUS)
    b = sub.add_parser("build", help="regenerate the corpus and its gold labels")
    b.add_argument("--src", type=Path, required=True, help="the directory holding pepNNN.html")
    b.add_argument(
        "--out",
        type=Path,
        default=CORPUS,
        help="the directory mounted at /c in the tools container",
    )
    args = ap.parse_args(argv)
    if args.cmd == "run":
        from bench.docparse.run import run

        run(
            args.corpus,
            args.out,
            args.extractor,
            args.python,
            set(filter(None, args.only.split(","))) or None,
        )
    elif args.cmd == "score":
        from bench.docparse.score import score

        if bad := [p for p in args.parses if "=" not in p]:
            ap.error(f"a parse is NAME=PARSES.jsonl, not {bad[0]}")
        systems = [(p.split("=", 1)[0], Path(p.split("=", 1)[1])) for p in args.parses]
        score(args.corpus, systems, args.split, args.json)
    else:
        from bench.docparse.corpus import build

        build(args.out, args.src)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
