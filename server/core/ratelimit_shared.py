"""Host-shared token buckets over a memory-mapped file.

`uvicorn --workers N` forks N control-plane processes; with a set of buckets
each, the limiter would divide every cap by N and a
keep-alive client — which the kernel pins to the one process that accepted
its connection — would see 1/N of the configured rate. This module gives
the N processes one table.

Layout: a 64-byte header (magic, version, slot count) followed by fixed
32-byte slots, each `<Qdd`: the 64-bit key hash, the token count and the
last refill instant (`time.monotonic()`, CLOCK_MONOTONIC on Linux — the
same clock in every process on the host). Slots form an open-addressed
hash table with linear probing over a bounded window; when the window is
full the stalest slot is evicted, which is what the in-process limiter's
`MAX_BUCKETS` pop did.

Concurrency: every read-modify-write of a slot happens under a POSIX
byte-range lock on that slot (`fcntl.lockf` on the backing fd), taken in
ascending slot order when one check spans several tiers so two processes
can never wait on each other. A refusal by any tier decrements none — the
invariant the in-process limiter promised — because all the tiers a check
touches are locked together. Record locks are per process, so a
`threading.Lock` serialises threads inside one process.

The key hash is `blake2b(key, digest_size=8)`, not `hash()`: Python's is
salted per process and the whole point is that four processes agree.
"""

from __future__ import annotations

import fcntl
import hashlib
import mmap
import os
import struct
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from server.core.shared_document import open_private

MAGIC = b"LRL1"
VERSION = 2  # 2: bucket keys carry the traffic class
HEADER_SIZE = 64
_HEADER = struct.Struct("<4sII")  # magic, version, slots
_SLOT = struct.Struct("<Qdd")  # key hash, tokens, last refill
SLOT_STRIDE = 32
PROBE_WINDOW = 64
MIN_SLOTS = 64


def key_hash(key: str) -> int:
    """Deterministic 64-bit hash of a bucket key; never 0 (0 marks an empty slot)."""
    h = int.from_bytes(hashlib.blake2b(key.encode("utf-8"), digest_size=8).digest(), "little")
    return h or 1


RUN_DIR = "/run/loom"
_TMP_DIR = "/tmp"


def runtime_dir() -> str:
    """The directory the table lives in: `$XDG_RUNTIME_DIR/loom`
    when the variable names a writable directory, else `/run/loom` when it
    exists or can be created, else `/tmp`. The first two are per-user or
    per-service tmpfs paths another local account cannot list; `/tmp` is
    the fallback for a container without one."""
    xdg = str(os.environ.get("XDG_RUNTIME_DIR") or "").strip()
    for base in ([os.path.join(xdg, "loom")] if xdg else []) + [RUN_DIR]:
        try:
            os.makedirs(base, mode=0o700, exist_ok=True)
            if os.access(base, os.W_OK | os.X_OK):
                return base
        except OSError:
            continue
    return _TMP_DIR


def server_file(name: str) -> str:
    """`loom-<name>-<server>` in the runtime dir, one per server: the workers of one
    `uvicorn --workers N` share a parent, named by its pid and start time so a
    later server reusing the pid (a restarted container) starts afresh."""
    ppid = os.getppid()
    try:
        # Field 22 of /proc/<pid>/stat, the start time; the fields after `comm`.
        started = open(f"/proc/{ppid}/stat").read().rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        started = "0"
    return os.path.join(runtime_dir(), f"loom-{name}-{ppid}-{started}")


def default_path() -> str:
    """One table per server (`server_file`); two servers on one host (two
    isolated worktrees, say) get two files."""
    return server_file("ratelimit") + ".buckets"


def open_table_fd(path: str) -> int:
    """Open (or create) the backing file the way a shared table must be
    opened (`open_private`): the file may predate this build or have been
    created by something else. Raises OSError; the limiter falls back to
    per-process buckets and names the reason in /v1/health."""
    return open_private(path, os.O_RDWR | os.O_CREAT)


@dataclass(frozen=True)
class SharedCheck:
    """Outcome of one multi-tier acquire against the shared table."""

    allowed: bool
    retry_after_s: float
    binding: int  # index into the entries passed to check()
    remaining: tuple[float, ...]  # tokens left per entry, after acquire or before refusal


class SharedBucketStore:
    """Token buckets in a memory-mapped file shared by every process that
    opens the same path with the same slot count.

    `check(entries)` takes `(key, capacity, rate)` triples — one per tier —
    and acquires one token from every bucket or from none.
    """

    def __init__(
        self,
        path: str,
        slots: int,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if slots < MIN_SLOTS:
            raise ValueError(f"slots must be >= {MIN_SLOTS}, got {slots}")
        self.path = path
        self.slots = int(slots)
        self._clock = clock
        self._lock = threading.Lock()
        # Per-process memo: key -> (hash, last known slot). The slot is
        # re-verified under its lock on every use, so a stale entry costs one
        # probe, never a wrong bucket. Keys carry actors and namespaces, so the
        # memo is bounded: past `slots` entries (more than the table can hold
        # live) it starts over.
        self._memo: dict[str, tuple[int, int]] = {}
        size = HEADER_SIZE + self.slots * SLOT_STRIDE
        fd = open_table_fd(path)
        try:
            # Header handshake under a lock on the header bytes: the first
            # process writes it, the rest must agree with it or stay out —
            # two processes hashing into tables of different sizes would
            # silently split every bucket.
            fcntl.lockf(fd, fcntl.LOCK_EX, HEADER_SIZE, 0)
            try:
                st = os.fstat(fd)
                if st.st_size < HEADER_SIZE:
                    os.ftruncate(fd, size)
                    os.pwrite(fd, _HEADER.pack(MAGIC, VERSION, self.slots), 0)
                else:
                    magic, ver, hslots = _HEADER.unpack(os.pread(fd, _HEADER.size, 0))
                    if magic != MAGIC or ver != VERSION or hslots != self.slots:
                        raise ValueError(
                            f"{path}: existing table is {magic!r} v{ver} slots={hslots}, "
                            f"this process wants v{VERSION} slots={self.slots}"
                        )
                    if st.st_size < size:
                        os.ftruncate(fd, size)
            finally:
                fcntl.lockf(fd, fcntl.LOCK_UN, HEADER_SIZE, 0)
            self._mm = mmap.mmap(fd, size, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE)
        except Exception:
            os.close(fd)
            raise
        # Kept open for the process lifetime: POSIX record locks live on the
        # (process, file) pair and vanish if any fd to the file is closed.
        self._fd = fd

    # -- slot primitives --------------------------------------------------

    def _off(self, idx: int) -> int:
        return HEADER_SIZE + idx * SLOT_STRIDE

    def _read(self, idx: int) -> tuple[int, float, float]:
        key, tokens, last = _SLOT.unpack_from(self._mm, self._off(idx))
        return int(key), float(tokens), float(last)

    def _write(self, idx: int, key: int, tokens: float, last: float) -> None:
        _SLOT.pack_into(self._mm, self._off(idx), key, tokens, last)

    def _lock_slot(self, idx: int) -> None:
        fcntl.lockf(self._fd, fcntl.LOCK_EX, SLOT_STRIDE, self._off(idx))

    def _unlock_slot(self, idx: int) -> None:
        fcntl.lockf(self._fd, fcntl.LOCK_UN, SLOT_STRIDE, self._off(idx))

    def _find_or_claim(self, key: str, h: int, capacity: float, now: float) -> int:
        """Slot holding `h`, claiming an empty one (or evicting the stalest in
        the probe window) if none does. Claims happen under the slot lock so
        two processes racing for one empty slot cannot both take it."""
        memo = self._memo.get(key)
        if memo is not None and self._read(memo[1])[0] == h:
            return memo[1]
        base = h % self.slots
        stalest, stalest_last = base, float("inf")
        for p in range(PROBE_WINDOW):
            idx = (base + p) % self.slots
            k, _tokens, last = self._read(idx)
            if k == h:
                self._remember(key, h, idx)
                return idx
            if k == 0:
                self._lock_slot(idx)
                try:
                    k2 = self._read(idx)[0]
                    if k2 == 0:
                        self._write(idx, h, capacity, now)
                    if k2 in (0, h):
                        self._remember(key, h, idx)
                        return idx
                finally:
                    self._unlock_slot(idx)
                continue  # a peer claimed it first for another key
            if last < stalest_last:
                stalest, stalest_last = idx, last
        # Window full: evict the least recently touched slot in it.
        self._lock_slot(stalest)
        try:
            self._write(stalest, h, capacity, now)
        finally:
            self._unlock_slot(stalest)
        self._remember(key, h, stalest)
        return stalest

    def _remember(self, key: str, h: int, idx: int) -> None:
        if len(self._memo) >= self.slots and key not in self._memo:
            self._memo.clear()
        self._memo[key] = (h, idx)

    # -- public API -------------------------------------------------------

    def check(self, entries: Sequence[tuple[str, float, float]], cost: float = 1.0) -> SharedCheck:
        """Acquire `cost` from every `(key, capacity, rate)` bucket, or from none.

        Refill is applied (and written back) whether or not the acquire
        succeeds, matching the in-process bucket's peek-then-take. The
        binding index is the longest wait on refusal (earlier tiers win
        ties) and the smallest remaining fraction on success.
        """
        n = len(entries)
        if n == 0:
            raise ValueError("check() needs at least one bucket")
        now = self._clock()
        with self._lock:
            hashes: list[int] = []
            idxs: list[int] = []
            for key, capacity, _rate in entries:
                memo = self._memo.get(key)
                h = memo[0] if memo is not None else key_hash(key)
                hashes.append(h)
                idxs.append(self._find_or_claim(key, h, capacity, now))
            order = sorted(range(n), key=idxs.__getitem__)
            for i in order:
                self._lock_slot(idxs[i])
            try:
                tokens: list[float] = []
                for i in range(n):
                    _key, capacity, rate = entries[i]
                    k, t, last = self._read(idxs[i])
                    if k != hashes[i]:
                        # Evicted between the probe and the lock: a fresh bucket.
                        t, last = capacity, now
                    elapsed = now - last
                    if elapsed < 0:
                        elapsed = 0.0
                    tokens.append(min(capacity, t + elapsed * rate))
                waits = [
                    0.0 if tokens[i] >= cost else (cost - tokens[i]) / max(entries[i][2], 1e-9)
                    for i in range(n)
                ]
                if any(w > 0 for w in waits):
                    for i in range(n):
                        self._write(idxs[i], hashes[i], tokens[i], now)
                    j = max(range(n), key=lambda i: (waits[i], -i))
                    return SharedCheck(False, waits[j], j, tuple(tokens))
                for i in range(n):
                    tokens[i] -= cost
                    self._write(idxs[i], hashes[i], tokens[i], now)
                j = min(range(n), key=lambda i: (tokens[i] / max(entries[i][1], 1.0), i))
                return SharedCheck(True, 0.0, j, tuple(tokens))
            finally:
                for i in reversed(order):
                    self._unlock_slot(idxs[i])

    def peek(self, key: str) -> tuple[float, float] | None:
        """(tokens, last_refill) for `key` as stored, or None. Test/diagnostic aid."""
        h = key_hash(key)
        base = h % self.slots
        for p in range(PROBE_WINDOW):
            k, t, last = self._read((base + p) % self.slots)
            if k == h:
                return t, last
            if k == 0:
                return None
        return None

    def reset(self) -> None:
        """Zero every slot (test hook; the header stays)."""
        body = self.slots * SLOT_STRIDE
        with self._lock:
            fcntl.lockf(self._fd, fcntl.LOCK_EX, body, HEADER_SIZE)
            try:
                self._mm[HEADER_SIZE : HEADER_SIZE + body] = bytes(body)
            finally:
                fcntl.lockf(self._fd, fcntl.LOCK_UN, body, HEADER_SIZE)
            self._memo.clear()

    def status(self) -> dict[str, object]:
        return {
            "path": self.path,
            "slots": self.slots,
            "bytes": HEADER_SIZE + self.slots * SLOT_STRIDE,
        }

    def close(self) -> None:
        try:
            self._mm.close()
        finally:
            os.close(self._fd)
