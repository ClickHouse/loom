"""Application-owned, durable protection for one logical session family."""

from __future__ import annotations

import contextlib
import contextvars
import json
import os
import re
import stat
import uuid
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

_ROOT_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")
_AUTOMATIC: contextvars.ContextVar[tuple[SessionProtection, ...]] = contextvars.ContextVar(
    "loom_automatic_protection", default=()
)

_OBSERVERS: contextvars.ContextVar[tuple[SessionProtection, ...]] = contextvars.ContextVar(
    "loom_session_observers", default=()
)


class SessionProtectionUnavailable(Exception):
    """Private output is withheld when durable protection cannot be confirmed."""

    def __init__(self) -> None:
        super().__init__("Session protection state could not be confirmed")


class AutomaticCaptureSuppressed(Exception):
    """An automatic callback or dispatch was suppressed without persistence."""

    def __init__(self) -> None:
        super().__init__("Automatic session capture is suppressed")


def _private(info: os.stat_result, *, directory: bool = False) -> None:
    # POSIX mode bits are not authority for Windows DACLs.
    if os.name != "posix" or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise SessionProtectionUnavailable()
    if directory:
        if not stat.S_ISDIR(info.st_mode):
            raise SessionProtectionUnavailable()
    elif not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise SessionProtectionUnavailable()


def _directory(path: Path) -> None:
    _private(path.lstat(), directory=True)


@contextlib.contextmanager
def _file(path: Path, flags: int) -> Iterator[int]:
    fd = os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        _private(os.fstat(fd))
        yield fd
    finally:
        os.close(fd)


def _sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        _private(os.fstat(fd), directory=True)
        os.fsync(fd)
    finally:
        os.close(fd)


def _create(path: Path, value: bytes) -> None:
    with _file(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL) as fd:
        if os.write(fd, value) != len(value):
            raise SessionProtectionUnavailable()
        os.fsync(fd)
    _sync_directory(path.parent)


@dataclass(frozen=True)
class SessionProtection:
    """One shared capsule for an application's complete session family.

    Pass the same capsule to every client, including clients for another server
    or token. ``create`` is an explicit application decision for a new independent
    family. Resume opens its existing pointer; missing history never creates it.
    Native Windows requires a DACL-backed state adapter and currently refuses.
    """

    base: Path
    id: str

    @classmethod
    def create(cls, *, base: str | Path | None = None) -> SessionProtection:
        """Create before the family receives private output, never on restore."""
        try:
            target = Path(base) if base is not None else Path.home() / ".loom/session-protection/v1"
            if not target.is_absolute() or os.name != "posix":
                raise SessionProtectionUnavailable()
            target.mkdir(parents=True, mode=0o700, exist_ok=True)
            _directory(target)
            root = cls(target, str(uuid.uuid4()))
            directory = target / root.id
            directory.mkdir(mode=0o700)
            _create(directory / "state", b"0\n")
            _create(directory / "root.json", json.dumps({"version": 1, "id": root.id}).encode())
            _sync_directory(target)
            if root.protected:
                raise SessionProtectionUnavailable()
            return root
        except Exception:  # noqa: BLE001 — filesystem failures must not expose private output
            raise SessionProtectionUnavailable() from None

    @classmethod
    def open(cls, pointer: Mapping[str, Any]) -> SessionProtection:
        """Reopen a trusted application pointer without creating or repairing it."""
        try:
            if set(pointer) != {"base", "id"} or not isinstance(pointer["base"], str):
                raise SessionProtectionUnavailable()
            root = cls(Path(pointer["base"]), pointer["id"])
            root._location()
            return root
        except Exception:  # noqa: BLE001 — damaged restore metadata must refuse
            raise SessionProtectionUnavailable() from None

    def pointer(self) -> dict[str, str]:
        """Content-free control state, independent of token or server scope."""
        return {"base": str(self.base), "id": self.id}

    def require_ready(self) -> None:
        """Confirm existing readable and writable control state before admission."""
        try:
            directory = self._location()
            with _file(directory / "state", os.O_RDWR | os.O_APPEND) as fd:
                if os.read(fd, 3) not in (b"0\n", b"0\n1"):
                    raise SessionProtectionUnavailable()
        except Exception:  # noqa: BLE001 — missing control authority cannot promise protection
            raise SessionProtectionUnavailable() from None

    def _location(self) -> Path:
        if (
            not self.base.is_absolute()
            or not isinstance(self.id, str)
            or not _ROOT_ID.fullmatch(self.id)
        ):
            raise SessionProtectionUnavailable()
        _directory(self.base)
        directory = self.base / self.id
        _directory(directory)
        with _file(directory / "root.json", os.O_RDONLY) as fd:
            raw = os.read(fd, 1025)
        if len(raw) > 1024:
            raise SessionProtectionUnavailable()
        manifest = json.loads(raw)
        if (
            not isinstance(manifest, dict)
            or set(manifest) != {"id", "version"}
            or type(manifest["version"]) is not int
            or manifest["version"] != 1
            or manifest["id"] != self.id
        ):
            raise SessionProtectionUnavailable()
        return directory

    @property
    def protected(self) -> bool:
        """Unknown, damaged and inaccessible state all suppress automatic work."""
        try:
            directory = self._location()
            with _file(directory / "state", os.O_RDONLY) as fd:
                return os.read(fd, 3) != b"0\n"
        except Exception:  # noqa: BLE001 — unknown protection must suppress capture
            return True

    def mark(self) -> None:
        """Durably append protection before releasing a mirrored answer."""
        try:
            directory = self._location()
            with _file(directory / "state", os.O_WRONLY | os.O_APPEND) as fd:
                if os.write(fd, b"1\n") != 2:
                    raise SessionProtectionUnavailable()
                os.fsync(fd)
        except Exception:  # noqa: BLE001 — a failed mark must withhold the answer
            raise SessionProtectionUnavailable() from None

    def observe(self, value: Any) -> Any:
        """Observe response envelopes before typed conversion or caller output."""
        try:
            if _marked(value):
                self.mark()
            return value
        except Exception:  # noqa: BLE001 — malformed protection metadata cannot release output
            # Preserve protection for later callbacks even when parsing refuses.
            self.mark()
            raise SessionProtectionUnavailable() from None

    @contextlib.contextmanager
    def automatic(self) -> Iterator[None]:
        """Guard one ambient callback; explicit agent operations stay outside it."""
        require_automatic_allowed()
        if self.protected:
            raise AutomaticCaptureSuppressed()
        previous = _AUTOMATIC.get()
        token = _AUTOMATIC.set(previous if self in previous else (*previous, self))
        try:
            yield
        finally:
            _AUTOMATIC.reset(token)


def automatic_roots(root: SessionProtection | None = None) -> tuple[SessionProtection, ...]:
    """Snapshot automatic producer control, retaining its bound root after rebinding."""
    roots = _AUTOMATIC.get()
    if roots and root is not None and root not in roots:
        roots = (*roots, root)
    return roots


@contextlib.contextmanager
def automatic_for(roots: tuple[SessionProtection, ...]) -> Iterator[None]:
    """Join retained producer controls without replacing the ambient callback."""
    require_automatic_allowed()
    with contextlib.ExitStack() as stack:
        for root in roots:
            stack.enter_context(root.automatic())
        yield


@contextlib.contextmanager
def observation_scope(roots: tuple[SessionProtection, ...]) -> Iterator[None]:
    """Observe known families without classifying explicit actions as automatic."""
    joined = tuple(dict.fromkeys((*_OBSERVERS.get(), *roots)))
    token = _OBSERVERS.set(joined)
    try:
        yield
    finally:
        _OBSERVERS.reset(token)


def response_roots(root: SessionProtection | None) -> tuple[SessionProtection, ...]:
    """Freeze all known response observers before opening a retained response."""
    roots = tuple(dict.fromkeys((*_AUTOMATIC.get(), *_OBSERVERS.get())))
    return (*roots, root) if root is not None and root not in roots else roots


def require_automatic_allowed(root: SessionProtection | None = None) -> None:
    """Recheck after awaits and before each dispatch or retry."""
    roots = _AUTOMATIC.get()
    if any(family.protected for family in roots) or (roots and root is not None and root.protected):
        raise AutomaticCaptureSuppressed()


def has_protection(root: SessionProtection | None) -> bool:
    """Unbound clients retain their ordinary response parsing behavior."""
    return bool(response_roots(root))


def refuse_response(root: SessionProtection | None) -> NoReturn:
    """Protect every known family when the response cannot be observed."""
    roots = response_roots(root)
    for family in roots:
        try:
            family.mark()
        except SessionProtectionUnavailable:
            # The response refuses regardless; attempt every other family.
            pass
    raise SessionProtectionUnavailable()


def observe_response(value: Any, root: SessionProtection | None) -> Any:
    """Every known app-owned family observes before any response escapes."""
    roots = response_roots(root)
    refused = False
    for family in roots:
        try:
            family.require_ready()
            family.observe(value)
        except SessionProtectionUnavailable:
            refused = True
    if refused:
        raise SessionProtectionUnavailable()
    return value


def _marked(value: Any) -> bool:
    pending = [value]
    visited: dict[int, dict[str, Any]] = {}
    remaining = 10000
    text_bytes = 4 * 1024 * 1024
    while pending:
        remaining -= 1
        if remaining < 0:
            raise SessionProtectionUnavailable()
        item = pending.pop()
        if not isinstance(item, dict):
            continue
        if id(item) in visited:
            raise SessionProtectionUnavailable()
        visited[id(item)] = item
        if "mirrored" in item and type(item["mirrored"]) is not bool:
            raise SessionProtectionUnavailable()
        if item.get("mirrored") is True:
            return True
        for key in ("result", "structuredContent"):
            if isinstance(item.get(key), dict):
                pending.append(item[key])
        if isinstance(item.get("error"), dict) and isinstance(item["error"].get("data"), dict):
            pending.append(item["error"]["data"])
        if isinstance(item.get("items"), list):
            if len(pending) + len(item["items"]) > remaining:
                raise SessionProtectionUnavailable()
            pending.extend(item["items"])
        if isinstance(item.get("content"), list):
            if len(pending) + len(item["content"]) > remaining:
                raise SessionProtectionUnavailable()
            remaining -= len(item["content"])
            for block in item["content"]:
                if (
                    not isinstance(block, dict)
                    or block.get("type") != "text"
                    or not isinstance(block.get("text"), str)
                ):
                    continue
                text = block["text"]
                if len(text) > text_bytes:
                    raise SessionProtectionUnavailable()
                text_bytes -= len(text.encode("utf8"))
                if text_bytes < 0:
                    raise SessionProtectionUnavailable()
                if text.lstrip().startswith("{"):
                    pending.append(json.loads(text))
    return False
