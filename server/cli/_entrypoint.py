"""Load server command modules only after selecting the client-only path."""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    raw = sys.argv[1:] if argv is None else argv
    if raw[:2] == ["cell", "each"]:
        from server.cli.cell_each_run import main as each_main

        return each_main(raw[2:])
    from server.cli.app import main as server_main

    return server_main(raw)
