"""A newly created SCIP lock is available to its owner and group."""

import stat
import subprocess
import sys
from pathlib import Path

_CREATE_LOCK = """
import asyncio
import os
import sys
from pathlib import Path

from server.code.app.index import entry_lock

os.umask(0o002)
fd = asyncio.run(entry_lock._acquire(Path(sys.argv[1]), wait=False))
assert fd is not None
os.close(fd)
"""


def test_a_new_entry_lock_has_no_permissions_for_others(tmp_path: Path) -> None:
    path = tmp_path / "scip.entry.lock"
    # Set umask in the child so the ambient developer umask cannot mask the regression.
    subprocess.run(
        [sys.executable, "-c", _CREATE_LOCK, str(path)],
        check=True,
        timeout=30,
    )
    assert stat.S_IMODE(path.stat().st_mode) == 0o660
