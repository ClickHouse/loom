"""A newly created SCIP lock is available to its owner and group."""

import os
from pathlib import Path

from server.code.app.index import entry_lock


async def test_a_new_entry_lock_has_no_permissions_for_others(tmp_path: Path) -> None:
    path = tmp_path / "scip.entry.lock"
    fd = await entry_lock._acquire(path, wait=False)
    assert fd is not None
    try:
        assert os.fstat(fd).st_mode & 0o007 == 0
    finally:
        os.close(fd)
