"""Filesystem-backed blob storage for uploaded document originals.

Layout (relative to `settings.blob_dir`, env `LOOM_BLOB_DIR`, default `var/blobs`):

    {org}/{namespace}/{sha256[:2]}/{sha256}

Content-addressed and sharded by sha256[:2]: re-uploading the same
bytes hashes to the same path, so the CH row updates without orphaning
a blob, and no single directory grows unbounded.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import secrets
from pathlib import Path

from server.settings import settings


class BlobStore:
    def __init__(self, root: str | Path) -> None:
        self._root = Path(root).resolve()
        self._root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def hash_bytes(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    def _resolve(self, org: str, namespace: str, sha: str) -> Path:
        # Sanitize org/namespace so a slipped slash can't escape the root.
        safe_org = _safe_segment(org)
        safe_ns = _safe_segment(namespace)
        if not (len(sha) == 64 and all(c in "0123456789abcdef" for c in sha)):
            raise ValueError(f"invalid sha256: {sha!r}")
        return self._inside(self._root / safe_org / safe_ns / sha[:2] / sha)

    def _inside(self, path: Path) -> Path:
        """`path` resolved, refused unless it lies below the root."""
        resolved = path.resolve()
        if resolved == self._root or not resolved.is_relative_to(self._root):
            raise ValueError(f"blob path escapes root: {str(path)!r}")
        return resolved

    def put(self, org: str, namespace: str, data: bytes) -> tuple[str, str]:
        """Store `data`, return (sha256, blob_path relative to the root)."""
        sha = self.hash_bytes(data)
        path = self._resolve(org, namespace, sha)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Temp + rename so a crash mid-write can't leave a half-blob. The temp
        # name is per writer: the same bytes uploaded through two processes
        # share `path`, and a shared temp would be renamed away under the
        # other writer (its rename then fails, or publishes a partial file).
        tmp = path.parent / f".{sha}.{secrets.token_hex(8)}.tmp"
        try:
            with open(tmp, "xb") as f:
                f.write(data)
                # The bytes reach the disk before the name does: a rename that
                # outlives a crash must not publish an empty or short file
                # under a hash it does not match.
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp)
            raise
        rel = path.relative_to(self._root).as_posix()
        return sha, rel

    def read(self, blob_path: str) -> bytes:
        """The stored bytes; ValueError for a path that leaves the root."""
        return self._inside(self._root / blob_path).read_bytes()

    def delete(self, blob_path: str) -> bool:
        """Best-effort remove; True if it existed. The DB row is the
        source of truth, an orphaned file is harmless beyond disk. A path
        that leaves the root is never followed (False)."""
        try:
            self._inside(self._root / blob_path).unlink()
            return True
        except (FileNotFoundError, ValueError):
            return False


_BANNED_CHARS = set('/\\:*?"<>|')


def _safe_segment(s: str) -> str:
    """One path segment: separators and shell-hostile characters become `_`,
    and `.` / `..` (which would name the parent) become `_` too."""
    cleaned = "".join("_" if c in _BANNED_CHARS or c.isspace() else c for c in s)
    return "_" if cleaned in {"", ".", ".."} else cleaned


_default_store: BlobStore | None = None


def get_store() -> BlobStore:
    global _default_store
    if _default_store is None:
        _default_store = BlobStore(settings.blob_dir)
    return _default_store
