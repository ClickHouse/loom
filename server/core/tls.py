"""TLS on the web port: the context uvicorn serves (its `ssl_context_factory`), which
serves a renewed certificate and key without a restart.

`LOOM_TLS_CERT_FILE` and `LOOM_TLS_KEY_FILE` name the pair. scripts/serve.sh (through
`server.core.web_server`) and `loom serve` start uvicorn with `uvicorn_options()`, which
loads the pair once, so a pair that does not load stops the start; uvicorn then builds
the context in each web process. At each handshake the context compares the device,
inode, size and modification time of both files with the pair it serves. After a change
it loads the files into a new context, which serves that handshake and the ones after
it. A pair whose content does not load (one file written and the other not yet, or a
key that does not match the certificate) leaves the served pair in place until the files
change again; files it cannot read (permissions, a missing file) leave it in place until
a handshake can read them. A Kubernetes Secret volume swaps both files in one rename.
"""

from __future__ import annotations

import logging
import os
import ssl
import stat
from collections.abc import Callable
from typing import Any

from server.settings import settings

log = logging.getLogger(__name__)

# LOOM_TLS_MIN_VERSION's values.
MIN_VERSIONS: dict[str, ssl.TLSVersion] = {
    "1.2": ssl.TLSVersion.TLSv1_2,
    "1.3": ssl.TLSVersion.TLSv1_3,
}

# One version of the pair: (device, inode, size, mtime_ns) of the certificate, then the key.
_Stamp = tuple[tuple[int, int, int, int], ...]

# True once uvicorn built this process's context (`context_factory`).
_served = False


class TLSRefused(RuntimeError):
    """The TLS settings are set, and the web port cannot serve them."""


def _stamp(files: tuple[str, str]) -> _Stamp:
    stats = [os.stat(f) for f in files]
    return tuple((s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns) for s in stats)


def _no_passphrase() -> bytes:
    """The passphrase OpenSSL gets for an encrypted key: none, so the load fails rather
    than prompting on a terminal from the event loop."""
    return b""


def _load(files: tuple[str, str], min_version: ssl.TLSVersion) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = min_version
    context.load_cert_chain(certfile=files[0], keyfile=files[1], password=_no_passphrase)
    return context


class RenewingContext:
    """The TLS context of one web process. `context` is the one uvicorn listens with: its
    server-name callback runs at each handshake, reads the files again when they changed,
    and hands the handshake the context of the newest pair that loaded."""

    def __init__(self, cert_file: str, key_file: str, min_version: ssl.TLSVersion) -> None:
        self._files = (cert_file, key_file)
        self._min_version = min_version
        self._stamp = _stamp(self._files)
        self._serving = _load(self._files, min_version)
        # The change that did not load, and whether the files could not be read: each
        # is logged once, not at every handshake.
        self._unloadable: _Stamp | None = None
        self._unreadable = False
        self.context = self._serving
        self.context.sni_callback = self._pick

    def _pick(
        self, connection: ssl.SSLObject, _server_name: str | None, _context: ssl.SSLContext
    ) -> None:
        self._renew()
        if connection.context is not self._serving:
            connection.context = self._serving

    def _renew(self) -> None:
        try:
            stamp = _stamp(self._files)
            if stamp in (self._stamp, self._unloadable):
                self._unreadable = False
                return
            renewed = _load(self._files, self._min_version)
        except ssl.SSLError as e:  # the content, which only another change can fix
            self._unloadable = stamp
            log.warning(
                "TLS: the changed certificate and key do not load (%s); serving the last pair", e
            )
            return
        except OSError as e:  # permissions, a missing file: tried again at the next handshake
            if not self._unreadable:
                log.warning(
                    "TLS: cannot read the certificate or key (%s); serving the last pair", e
                )
            self._unreadable = True
            return
        self._serving, self._stamp, self._unloadable = renewed, stamp, None
        self._unreadable = False
        log.info("TLS: serving the renewed certificate from %s", self._files[0])


def _from_settings() -> RenewingContext:
    return RenewingContext(
        settings.tls_cert_file, settings.tls_key_file, MIN_VERSIONS[settings.tls_min_version]
    )


def context_factory(_config: Any, _default: Callable[[], ssl.SSLContext]) -> ssl.SSLContext:
    """uvicorn's `ssl_context_factory`: called in each web process as its config loads,
    before the lifespan starts. Module level, so a spawned worker unpickles it."""
    global _served
    context = _from_settings().context
    _served = True
    return context


def uvicorn_options() -> dict[str, Any]:
    """The `uvicorn.run` arguments for the TLS settings: none without a pair. The pair is
    loaded once here, so one that does not load refuses before a web process starts; the
    reason is logged."""
    if not settings.tls_cert_file:
        return {}
    try:
        _from_settings()
    except OSError as e:  # ssl.SSLError is an OSError
        log.error("TLS: the certificate and key do not load: %s", e)
        raise TLSRefused("LOOM_TLS_CERT_FILE and LOOM_TLS_KEY_FILE do not load") from e
    if os.stat(settings.tls_key_file).st_mode & stat.S_IROTH:
        log.warning("TLS: every user on the host can read LOOM_TLS_KEY_FILE")
    return {"ssl_context_factory": context_factory}


def require_served() -> None:
    """Refuse to start a process that would serve plain HTTP with the TLS settings set: one
    uvicorn started without `uvicorn_options()` (its own CLI, `make server`)."""
    if settings.tls_cert_file and not _served:
        raise TLSRefused(
            "refusing to start: LOOM_TLS_CERT_FILE is set, but this process serves plain "
            "HTTP. Start it with scripts/serve.sh or `loom serve`, which serve the pair."
        )
