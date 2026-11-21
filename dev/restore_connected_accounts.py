#!/usr/bin/env python3
"""End connected-account authority after restoring the configured deployment.

Stop all serving processes and workers first. This command does not erase mirror
contents; complete the deployment's mirror cleanup before serving again.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server.cli._apply import add_apply_flags
from server.cli._ch import reads, run_connected, writes
from server.knowledge.app.connected_restore import restore_all
from server.knowledge.app.crawl_restore import restore_all as restore_crawls


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    add_apply_flags(parser, writes="stop and purge this deployment's connected accounts")
    args = parser.parse_args(argv)

    async def run(ch: Any) -> int:
        await restore_all(apply=not args.dry_run, ch=ch)
        await restore_crawls(apply=not args.dry_run, ch=ch)
        print(
            "connected-account restore cleanup completed; connections must be established again"
            if not args.dry_run
            else "dry run: no connection authority was changed"
        )
        return 0

    gate = (
        reads("plans this deployment's connection restore cleanup")
        if args.dry_run
        else writes("purges this deployment's restored connection authority")
    )
    try:
        return run_connected(run, org_free=gate)
    except Exception:  # noqa: BLE001 — maintenance errors must not print credentials or stored metadata
        print(
            "connected-account restore cleanup failed; keep the deployment stopped", file=sys.stderr
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
