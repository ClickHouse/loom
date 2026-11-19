"""Convert an already exported, owner-bound conversation without database access."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from server.assistant.domain.history import convert


def _read(path: Path) -> dict[str, Any]:
    if path.stat().st_size > 4_000_000:
        raise ValueError("the input exceeds the offline conversion bound")
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError("the input must be a JSON object")
    return value


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--owner-bindings", type=Path, required=True)
    parser.add_argument("--subject", required=True)
    parser.add_argument("--org", required=True)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    archive = convert(
        _read(args.input),
        subject=args.subject,
        org=args.org,
        namespace=args.namespace,
        owner_bindings=_read(args.owner_bindings),
    )
    # Never overwrite a source export or an existing archive.
    with args.output.open("x") as output:
        json.dump(archive, output, ensure_ascii=False, indent=2)
        output.write("\n")


if __name__ == "__main__":
    main()
