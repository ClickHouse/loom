"""One maintained endpoint-identity target with deterministic corpus replay."""

from __future__ import annotations

import sys
from pathlib import Path


def main() -> None:
    if len(sys.argv) == 3 and sys.argv[1] == "--replay":
        from tests.fuzz.embedding_identity_oracle import check_input

        paths = sorted(Path(sys.argv[2]).iterdir())
        if not paths or not all(path.is_file() for path in paths):
            raise ValueError("Replay requires a nonempty directory of seed files")
        for path in paths:
            check_input(path.read_bytes())
        print(f"Replayed {len(paths)} endpoint-identity seeds")
        return

    import atheris

    with atheris.instrument_imports():
        from tests.fuzz.embedding_identity_oracle import check_input

    atheris.Setup(sys.argv, check_input)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
