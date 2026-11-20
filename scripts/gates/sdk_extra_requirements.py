"""Print the requirements of the named SDK extras, one per line, for
`make test-sdk-adapters`: passed to `uv pip install -r` as direct requirements,
so `--resolution lowest-direct` lowers each to its bound."""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

PYPROJECT = Path(__file__).resolve().parents[2] / "sdk" / "python" / "pyproject.toml"


def main(extras: list[str]) -> int:
    declared = tomllib.loads(PYPROJECT.read_text())["project"]["optional-dependencies"]
    unknown = sorted(set(extras) - set(declared))
    if unknown:
        print(f"unknown SDK extras: {', '.join(unknown)}", file=sys.stderr)
        return 1
    for extra in extras:
        print("\n".join(declared[extra]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
