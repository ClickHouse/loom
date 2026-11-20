"""Sign in to a Loom server with no token to paste: `loom.login()`.

`login()` runs the OAuth device authorization grant (RFC 8628). It prints a
URL and a short code; you open the URL on any browser (this machine or your
phone), sign in the way your Loom is set up (password or single sign-on) and
approve. The access and refresh tokens land in `~/.loom/credentials` (mode
0600), one entry per profile, and every `Agent` for that server that is
given no `api_key` and no `LOOM_API_KEY` uses them, refreshing the hour-long
access token as it goes. `logout()` revokes the session on the server and forgets it here.

A profile is one sign-in to one server: `profile="staging"` (or
`LOOM_PROFILE=staging`) keeps a second server's tokens beside the first.

The refresh token rotates on every use and the server ends the session when
a spent one comes back, so two processes must never refresh the same profile
at once: a refresh holds an exclusive lock on the credentials file and
re-reads it first, so the second process finds the new token and uses it.
The lock is a file lock (which older SDK builds take) and the directory
`~/.loom/.credentials.lock.d` (which the TypeScript SDK takes too).
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import json
import math
import os
import secrets
import shutil
import socket
import sys
import tempfile
import threading
import time
import webbrowser
from collections.abc import AsyncGenerator, Callable, Generator, Iterator
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from loom.errors import (
    INVALID_RESPONSE_CODE,
    AuthInvalid,
    BackendUnavailable,
    LoomError,
    RateLimited,
    _busy,
)

CREDENTIALS_FILE = Path("~/.loom/credentials")
DEFAULT_SERVER = "http://localhost:7777"
CLIENT_ID = "loom-cli"
_DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
# On every sign-in answer of a build that keeps sessions. Once a server has
# sent it, an answer without it came from a previous build during a rolling
# upgrade, which cannot know this session. A server never seen sending it (a
# proxy may strip it) has its refusals taken as they are.
_SESSIONS_HEADER = "Loom-Sessions"
# Refresh this long before the access token expires, so a call never leaves
# with a token that dies in flight.
_REFRESH_MARGIN_S = 60
# Longer than a refresh can take on the server, which asks the identity
# provider first: giving up sooner turns a slow answer into a lost one.
_REFRESH_TIMEOUT_S = 60.0
_REQUEST_TIMEOUT_S = 30.0
# Discovery, the exchange and its one retry together, so a refresh holds the
# lock for a bounded time. httpx times each phase of a request (connect, each
# write, each read), not the whole: every phase of every request is bounded by
# what the deadline leaves, so a server that trickles its answer can hold the
# lock past it. The TypeScript SDK bounds the whole refresh.
_REFRESH_DEADLINE_S = 90.0
# The lock both SDKs take: Node has no flock. Its holder touches the
# directory every heartbeat; one silent for the stale age is broken, and so
# is one silent for two heartbeats whose holder on this machine is no longer
# running.
_LOCK_DIR = ".credentials.lock.d"
_LOCK_HEARTBEAT_S = 10.0
_LOCK_SILENT_S = 2 * _LOCK_HEARTBEAT_S
_LOCK_STALE_S = 60.0
# How long a release waits for a breaker's claim to go.
_RELEASE_WAIT_S = 2.0
# What Windows refuses a rename with while another process has a file in the
# directory open, as a waiter reading the owner file does for a moment.
_BUSY = {errno.EACCES, errno.EPERM, errno.EBUSY}
_RENAME_TRIES = 5
_RENAME_WAIT_S = 0.02
# How often a release asks again for the claim a breaker holds.
_CLAIM_POLL_S = 0.02


def _path() -> Path:
    return CREDENTIALS_FILE.expanduser()


def default_profile() -> str:
    return os.environ.get("LOOM_PROFILE", "").strip() or "default"


def _read() -> dict[str, Any]:
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"profiles": {}}
    if not isinstance(data, dict) or not isinstance(data.get("profiles"), dict):
        return {"profiles": {}}
    return data


def _write(data: dict[str, Any]) -> None:
    """Replace the file atomically, readable by this user only."""
    path = _path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".credentials-")  # created 0600
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


# The lock between this process's threads (async requests refresh on worker
# threads); the file lock below is the one between processes.
_in_process = threading.Lock()


def _unlock_in_child() -> None:
    # A fork taken mid-refresh copies the lock held, with no thread to free it.
    global _in_process
    _in_process = threading.Lock()


if hasattr(os, "register_at_fork"):  # not on Windows
    os.register_at_fork(after_in_child=_unlock_in_child)


@contextlib.contextmanager
def _locked() -> Iterator[None]:
    """Hold the credentials lock: this process's, then a sibling file's (so the
    atomic replace of the credentials themselves never drops it), then the
    lock directory beside it."""
    path = _path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with _in_process, open(path.with_name(".credentials.lock"), "a") as lock:
        _lock_file(lock.fileno())
        try:
            with _lock_dir(path.with_name(_LOCK_DIR)):
                yield
        finally:
            _unlock_file(lock.fileno())


@contextlib.contextmanager
def _lock_dir(lock: Path) -> Iterator[None]:
    """Hold `lock`, a directory whose owner file names the holder; wait while
    another process holds it, and break it once stale."""
    owner = json.dumps({"pid": os.getpid(), "host": socket.gethostname(), "time": time.time()})
    _sweep(lock)
    stop = threading.Event()
    beat = threading.Thread(target=_heartbeat, args=(lock, owner, stop), daemon=True)
    # From the first try: an interrupt just after the rename that took the
    # lock, or a heartbeat that cannot start, still releases it.
    try:
        wait = 0.01
        while not _create(lock, owner):
            held = _owner(lock)
            if _stale(lock, held):
                _break(lock, held)
            time.sleep(wait)
            wait = min(wait * 2, 0.2)
        beat.start()
        yield
    finally:
        stop.set()
        if beat.ident is not None:  # started
            beat.join()
        with contextlib.suppress(OSError):
            _release(lock, owner)


def _create(lock: Path, owner: str) -> bool:
    """Take `lock` by renaming a private directory that holds the owner file
    onto it. The rename fails while the lock exists, and a lock never exists
    without its owner. Answers whether it was taken."""
    tmp = lock.with_name(f"{lock.name}.{secrets.token_hex(6)}.tmp")
    tmp.mkdir(mode=0o700)
    try:
        (tmp / "owner").write_text(owner, encoding="utf-8")
        _touch(tmp)
        tries = 0
        while True:
            try:
                os.rename(tmp, lock)
                return True
            except OSError as e:
                # POSIX allows no rename onto a non-empty directory, and
                # Windows none onto any directory, refused as access denied:
                # held while the lock is there. With no lock, the refusal is
                # a scanner holding a file in tmp open, or a holder renaming
                # the lock away meanwhile: tried again, then raised.
                if e.errno in (errno.EEXIST, errno.ENOTEMPTY):
                    return False
                if e.errno not in _BUSY:
                    raise
                if lock.exists():
                    return False
                tries += 1
                if tries == _RENAME_TRIES:
                    raise LoomError(
                        f"cannot take the credentials lock at {lock} ({e.strerror or e})"
                    ) from e
                time.sleep(_RENAME_WAIT_S)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)  # gone already once renamed


def _owner(lock: Path) -> str:
    """The owner file's text; empty when the lock is gone."""
    try:
        return (lock / "owner").read_text(encoding="utf-8")
    except OSError:
        return ""


def _touch(path: Path) -> None:
    # This machine's clock: utime with no time takes the file server's on a network home.
    now = time.time()
    os.utime(path, (now, now))


def _heartbeat(lock: Path, owner: str, stop: threading.Event) -> None:
    """Touch the lock while this process holds it, so waiters see it alive."""
    while not stop.wait(_LOCK_HEARTBEAT_S):
        if _owner(lock) == owner:
            with contextlib.suppress(OSError):
                _touch(lock)


def _stale(lock: Path, held: str) -> bool:
    """Whether the holder of `lock` is gone: silent for the stale age (a
    holder on another machine, a reused pid, a hung process), or silent for
    two heartbeats and no longer running on this machine. The pid is asked
    only after the heartbeat stopped: a holder in another pid namespace on
    this host (a container sharing the home directory) looks dead to it."""
    age = _age_s(lock)
    if age > _LOCK_STALE_S:
        return True
    if age <= _LOCK_SILENT_S:
        return False
    try:
        owner = json.loads(held)
    except ValueError:
        return False
    if not isinstance(owner, dict) or owner.get("host") != socket.gethostname():
        return False  # another machine's pid means nothing here
    pid = owner.get("pid")
    return isinstance(pid, int) and not isinstance(pid, bool) and pid > 0 and not _running(pid)


def _running(pid: int) -> bool:
    if sys.platform == "win32":
        return True  # os.kill would end the process there: the stale age decides
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True  # EPERM: running, as another user
    return True


def _break(lock: Path, held: str) -> None:
    """Remove the stale lock whose owner file read `held`. A claim directory
    beside it lets one waiter at a time do it, or the holder release it, and
    the owner is read again under the claim, so a lock taken over meanwhile
    is left alone."""
    claim = lock.with_name(f"{lock.name}.breaking")
    try:
        claim.mkdir(mode=0o700)
    except FileExistsError:
        # Left by a process that died holding it: once as old as a stale
        # lock, renamed away for the sweep to delete.
        if _age_s(claim) > _LOCK_STALE_S:
            with contextlib.suppress(OSError):
                os.rename(claim, claim.with_name(f"{claim.name}.{secrets.token_hex(6)}.stale"))
        return
    except FileNotFoundError:
        return  # the directory holding it is gone: try again
    except OSError as e:
        raise _unbreakable(lock, e) from e
    try:
        with contextlib.suppress(OSError):
            _touch(claim)
        if _owner(lock) == held:
            _remove(lock)
    except OSError as e:
        # Busy on Windows: try again after the wait, until the lock is twice
        # the stale age, so a lock that is never renamable still raises.
        if e.errno in _BUSY and _age_s(lock) <= 2 * _LOCK_STALE_S:
            return
        raise _unbreakable(lock, e) from e
    finally:
        with contextlib.suppress(OSError):
            claim.rmdir()


def _release(lock: Path, owner: str) -> None:
    """Remove our own lock, holding the claim a breaker takes: a breaker that
    judged the lock stale renames it under the claim, and a release between
    its check and its rename would let a third process take the path that
    breaker then renames away. Without the claim in time the lock is left,
    never renamed, and broken once its heartbeat has stopped."""
    if _owner(lock) != owner:
        return  # broken as stale: another process's by now
    claim = lock.with_name(f"{lock.name}.breaking")
    deadline = time.monotonic() + _RELEASE_WAIT_S
    while True:
        try:
            claim.mkdir(mode=0o700)
            break
        except FileExistsError:
            if time.monotonic() > deadline:
                return
            time.sleep(_CLAIM_POLL_S)
    try:
        with contextlib.suppress(OSError):
            _touch(claim)  # as a breaker's is, so it never looks old to the next one
        if _owner(lock) == owner:
            _remove(lock)
    finally:
        with contextlib.suppress(OSError):
            claim.rmdir()


def _unbreakable(lock: Path, e: OSError) -> LoomError:
    return LoomError(
        f"the credentials lock at {lock} is stale but cannot be broken "
        f"({e.strerror or e}); remove it by hand"
    )


def _remove(lock: Path) -> None:
    """Rename `lock` away, then delete it: the lock path is never a
    half-removed directory, empty, which a rename could replace."""
    tomb = lock.with_name(f"{lock.name}.{secrets.token_hex(6)}.stale")
    for attempt in range(_RENAME_TRIES):
        try:
            os.rename(lock, tomb)
            break
        except FileNotFoundError:
            return  # released meanwhile
        except OSError as e:
            if e.errno not in _BUSY or attempt == _RENAME_TRIES - 1:
                raise
            time.sleep(_RENAME_WAIT_S)  # a waiter reading the owner file closes it in a moment
    shutil.rmtree(tomb, ignore_errors=True)


def _sweep(lock: Path) -> None:
    """Delete what a process that died taking or breaking the lock left
    beside it: its private directory, or the lock or claim it renamed away."""
    for left in lock.parent.glob(f"{lock.name}.*"):
        if left.suffix in (".tmp", ".stale") and _age_s(left) > _LOCK_STALE_S:
            shutil.rmtree(left, ignore_errors=True)


def _age_s(path: Path) -> float:
    try:
        return time.time() - path.stat().st_mtime
    except FileNotFoundError:
        return 0.0  # released meanwhile: take it on the next try


if sys.platform == "win32":
    import msvcrt

    def _lock_file(fd: int) -> None:
        while True:
            try:
                msvcrt.locking(fd, msvcrt.LK_LOCK, 1)  # retries for 10 s, then raises
                return
            except OSError as e:
                if e.errno not in (errno.EDEADLOCK, errno.EACCES):  # not just still held
                    raise

    def _unlock_file(fd: int) -> None:
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _lock_file(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_EX)

    def _unlock_file(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


@contextlib.contextmanager
def _server(url: str) -> Iterator[httpx.Client]:
    """A client for `url` whose failures to reach it come out as
    BackendUnavailable: callers catch LoomError, never httpx."""
    try:
        with httpx.Client(base_url=url, timeout=_REQUEST_TIMEOUT_S) as client:
            yield client
    except httpx.TransportError as e:
        raise BackendUnavailable(f"cannot reach {url}: {type(e).__name__}") from e


def _json(r: httpx.Response) -> dict[str, Any]:
    """A 200's JSON object; anything else is the server's fault, said as one."""
    try:
        body = r.json()
    except ValueError as e:
        raise BackendUnavailable(f"{r.request.url} answered something that is not JSON") from e
    if not isinstance(body, dict):
        raise BackendUnavailable(f"{r.request.url} answered something that is not a JSON object")
    return body


_DEFAULT_PORTS = {"http": 80, "https": 443}


def _origin(url: str) -> tuple[str, str, int | None]:
    """(scheme, host, port) compared as a browser compares origins: the host
    as httpx sends it (lower case, IDNA-encoded), the default port written or
    not."""
    u = httpx.URL(url)
    host = u.raw_host.decode("ascii").lower()
    return u.scheme, host, u.port or _DEFAULT_PORTS.get(u.scheme)


def same_server(a: str, b: str) -> bool:
    return _origin(a) == _origin(b) and urlsplit(a).path.rstrip("/") == urlsplit(b).path.rstrip("/")


def _endpoints(
    client: httpx.Client, *, signed_in: bool = False, timeout: float = _REQUEST_TIMEOUT_S
) -> dict[str, str]:
    """The server's sign-in endpoints, each checked to be on the server itself,
    so a code or token only ever goes where the user chose to sign in."""
    r = client.get("/.well-known/oauth-authorization-server", timeout=timeout)
    if r.status_code not in (200, 404):
        raise _failed(r, "reading the server's sign-in endpoints")
    meta = _json(r) if r.status_code == 200 else {}
    if "device_authorization_endpoint" not in meta:  # no sign-in, or none of `loom login`'s
        if signed_in:
            # It signed this session in, so a previous build answered: ask again later.
            raise BackendUnavailable(
                f"{client.base_url} answered from a build without `loom login`; retry "
                "(if it no longer signs in, run `loom logout`)"
            )
        raise LoomError(
            f"{client.base_url} has no sign-in: it runs without auth (no login needed) "
            "or predates `loom login`"
        )
    endpoints = {}
    for name in ("device_authorization_endpoint", "token_endpoint", "revocation_endpoint"):
        url = str(meta.get(name, ""))
        if _origin(url) != _origin(str(client.base_url)):
            raise LoomError(f"{client.base_url} signs in at {url}; use that as the server")
        endpoints[name] = url
    return endpoints


def _failed(r: httpx.Response, doing: str) -> LoomError:
    """The typed SDK error for a refused sign-in request: a caller catches
    LoomError, never an httpx one."""
    error = _oauth_error(r) or f"HTTP {r.status_code}"
    if r.status_code == 429 or r.status_code >= 500:
        return _busy(f"{doing}: {error}; retry", r.status_code)
    if r.status_code in (401, 403) or error == "invalid_grant":
        return AuthInvalid(f"{doing}: {error}; run `loom login` again")
    return LoomError(f"{doing}: {error}")


def _oauth_description(r: httpx.Response) -> str:
    try:
        body = r.json()
    except ValueError:
        return ""
    detail = body.get("detail", body) if isinstance(body, dict) else {}
    return str(detail.get("error_description", "")) if isinstance(detail, dict) else ""


def _oauth_error(r: httpx.Response) -> str:
    try:
        body = r.json()
    except ValueError:
        return ""
    detail = body.get("detail", body) if isinstance(body, dict) else {}
    return str(detail.get("error", "")) if isinstance(detail, dict) else ""


def _save(
    profile: str, server: str, tokens: dict[str, Any], session: str, *, marked: bool = False
) -> None:
    """`session` names one sign-in: a login makes a new one, a refresh keeps it.
    `marked`: the server's answer carried the sessions header."""
    data = _read()
    data["profiles"][profile] = {
        "server": server,
        "session": session,
        "marked": marked,
        "access_token": tokens["access_token"],
        "refresh_token": tokens.get("refresh_token", ""),
        "expires_at": int(time.time()) + int(tokens.get("expires_in", 3600)),
    }
    _write(data)


def _say(line: str) -> None:
    # Flushed: a piped or non-terminal stdout must still show the code now.
    print(line, flush=True)


# Seconds `loom login --org` waits out the router's 429s before it refuses.
ORG_LOOKUP_WAIT_S = 120.0
# The lookup's clock, a seam for tests (the sleep is `time.sleep`).
_monotonic = time.monotonic


def org_server(server: str, org: str, *, wait_s: float = ORG_LOOKUP_WAIT_S) -> str:
    """The server to sign in at for `org`: `https://` and the cell host the router's
    `GET /cells/lookup` names (docs/architecture/cells.md). `server` itself on a
    404 (a deployment with no router) or on `NO_CELL` (no cell registered yet); a 429
    is waited out for at most `wait_s`, then refused as a rate refusal; any other
    answer refuses, naming the router, so no sign-in starts at a cell that may not
    hold the org."""
    _check_wait(wait_s)
    deadline = _monotonic() + wait_s
    with _server(server) as client:
        while True:
            r = client.get("/cells/lookup", params={"org": org})
            if r.status_code == 200:
                return _cell_url(server, org, _lookup_body(r).get("host"))
            if r.status_code == 404 or (
                r.status_code == 503 and _lookup_body(r).get("code") == "NO_CELL"
            ):
                return server
            if r.status_code == 429:
                wait = _retry_after(r)
                left = deadline - _monotonic()
                if wait > left:
                    limited = RateLimited(
                        f"the router at {server} rate-limits the lookup of org {org!r}: it "
                        f"asks for {wait:g}s and {max(left, 0):g}s of the wait is left",
                        retry_after_s=wait,
                    )
                    limited.status = 429
                    raise limited
                time.sleep(wait)
                continue
            refused = (
                f"the router at {server} could not look up org {org!r} "
                f"(HTTP {r.status_code}); no sign-in was started"
            )
            err = _busy(refused, r.status_code) if r.status_code >= 500 else LoomError(refused)
            err.status = r.status_code
            raise err


def _check_wait(wait_s: float) -> None:
    if not math.isfinite(wait_s) or wait_s < 0:
        raise LoomError(f"wait_s is a number of seconds, 0 or more, not {wait_s!r}")


def _lookup_body(r: httpx.Response) -> dict[str, Any]:
    try:
        body = r.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _cell_url(server: str, org: str, host: Any) -> str:
    """`https://<host>` for a host the lookup named; a host that is not a bare
    `name[:port]` the sign-in can reach refuses, naming the router, as the router's
    fault (`INVALID_RESPONSE`). Always https: the router's own URL may be a local
    port-forward, and the cell is reached in the open."""
    refused = LoomError(f"the router at {server} named no usable host for org {org!r}: {host!r}")
    refused.code = INVALID_RESPONSE_CODE
    refused.status = 200  # the router answered; its answer is what is wrong
    if not isinstance(host, str) or not host or any(c.isspace() for c in host):
        raise refused
    try:
        parts = urlsplit(f"//{host}")
        _ = parts.port  # a port that is not a number raises here
        url = httpx.URL(f"https://{host}")  # the parser the sign-in itself uses
    except (ValueError, httpx.InvalidURL):
        raise refused from None
    bare = parts.hostname and not (parts.username or parts.password or parts.path)
    # The whole of `host` is its netloc: no path, query or fragment, not even an empty `?`.
    if not bare or parts.netloc != host or url.host != parts.hostname:
        raise refused
    return f"https://{host}"


def _retry_after(r: httpx.Response) -> float:
    """The router's Retry-After in seconds, 1 to 60 (its window), as the transport
    reads it (a number or an HTTP-date; 1 when absent or unreadable)."""
    from loom.transport import parse_retry_after  # the transport imports this module

    return min(max(parse_retry_after(r.headers.get("retry-after")) or 1.0, 1.0), 60.0)


def login(
    server: str | None = None,
    *,
    profile: str | None = None,
    open_browser: bool = True,
    echo: Callable[[str], None] = _say,
    org: str | None = None,
    wait_s: float = ORG_LOOKUP_WAIT_S,
) -> dict[str, Any]:
    """Sign in to `server` (default `LOOM_BASE_URL`, else localhost) and store
    the tokens under `profile`. With `org`, sign in at that org's cell instead, as
    the router's lookup names it (`org_server`). Blocks until the sign-in is
    approved, denied or expires (ten minutes). Returns what `whoami()` returns."""
    server = (server or os.environ.get("LOOM_BASE_URL") or DEFAULT_SERVER).rstrip("/")
    _check_wait(wait_s)
    if org is not None:
        if not org:
            raise LoomError("org= names no org: pass the org to sign in at, or leave it out")
        cell = org_server(server, org, wait_s=wait_s)
        if not same_server(cell, server):
            echo(f"Org {org} is at {cell}.")
        server = cell
    profile = profile or default_profile()
    with _server(server) as client:
        meta = _endpoints(client)
        r = client.post(meta["device_authorization_endpoint"], data={"client_id": CLIENT_ID})
        if r.status_code != 200:
            raise _failed(r, "starting the sign-in")
        grant = _json(r)
        missing = {"device_code", "user_code", "verification_uri", "verification_uri_complete"}
        if missing - grant.keys():
            raise LoomError(f"{server} answered a sign-in without {sorted(missing - grant.keys())}")
        echo(
            f"To sign in, open {grant['verification_uri']} and enter the code "
            f"{grant['user_code']}\n(or open {grant['verification_uri_complete']})"
        )
        if open_browser and sys.stdout.isatty():
            with contextlib.suppress(webbrowser.Error):
                webbrowser.open(grant["verification_uri_complete"])
        marked = _SESSIONS_HEADER in r.headers
        answer = _poll(client, meta["token_endpoint"], grant, marked=marked)
    with _locked():  # another process may be rotating a profile in the same file
        replaced = _read()["profiles"].get(profile)
        _save(
            profile,
            server,
            _json(answer),
            session=secrets.token_hex(8),
            marked=_SESSIONS_HEADER in answer.headers,
        )
    if replaced:  # the session this sign-in replaces ends too, on its own server
        _end_session(replaced)
    info = whoami(profile=profile)
    # Said every time: a code someone else approved signs you in as them.
    echo(f"Signed in to {server} as {info.get('subject')} ({info.get('org')}).")
    return info


def _poll(
    client: httpx.Client, token_endpoint: str, grant: dict[str, Any], *, marked: bool
) -> httpx.Response:
    interval = int(grant.get("interval", 5))
    deadline = time.monotonic() + int(grant.get("expires_in", 600))
    while time.monotonic() < deadline:
        time.sleep(interval)
        try:
            r = client.post(
                token_endpoint,
                data={
                    "grant_type": _DEVICE_GRANT,
                    "device_code": grant["device_code"],
                    "client_id": CLIENT_ID,
                },
            )
        except httpx.TransportError:
            # Not approved yet, keep asking. (Had the approved answer been the
            # one lost, the next poll is refused and login says to run again.)
            continue
        if r.status_code >= 500:
            continue
        if r.status_code == 200:
            return r
        if marked and _SESSIONS_HEADER not in r.headers:
            continue  # a previous build's: the grant lives on the one that started it
        error = _oauth_error(r)
        if error == "authorization_pending":
            continue
        if error == "slow_down":
            interval += 5  # RFC 8628 §3.5
            continue
        if error == "access_denied":
            raise AuthInvalid("the sign-in was denied")
        if error == "expired_token":
            break
        raise _failed(r, "sign-in failed")
    raise AuthInvalid("the sign-in code expired; run `loom login` again")


def _usable(entry: dict[str, Any], refused: str) -> bool:
    fresh = int(entry["expires_at"]) - _REFRESH_MARGIN_S > time.time()
    return fresh and entry["access_token"] != refused


def _drop(profile: str) -> None:
    data = _read()
    if data["profiles"].pop(profile, None) is not None:
        _write(data)


def _refresh(profile: str, refused: str, server: str | None) -> dict[str, Any]:
    """Rotate the profile's tokens. Called with the lock held."""
    entry = _entry_for(profile, server)  # read again: another process may have changed it
    if _usable(entry, refused):
        return dict(entry)  # another process refreshed it while we waited
    if not entry.get("refresh_token"):
        raise AuthInvalid(f"the session for profile {profile!r} ended; run `loom login`")
    form = {
        "grant_type": "refresh_token",
        "refresh_token": entry["refresh_token"],
        "client_id": CLIENT_ID,
    }
    deadline = time.monotonic() + _REFRESH_DEADLINE_S

    def left(usual: float) -> float:
        # At most `usual`, and no more than the deadline leaves, as the
        # timeout of each phase of the next request; nothing left, nothing is sent.
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise BackendUnavailable(
                f"refreshing the session at {entry['server']} took over "
                f"{_REFRESH_DEADLINE_S:.0f} s; retry"
            )
        return min(usual, remaining)

    with _server(entry["server"]) as client:
        endpoint = _endpoints(client, signed_in=True, timeout=left(_REQUEST_TIMEOUT_S))[
            "token_endpoint"
        ]
        try:
            r = client.post(endpoint, data=form, timeout=left(_REFRESH_TIMEOUT_S))
        except (httpx.ConnectError, httpx.ConnectTimeout):
            # Nothing reached the server, so nothing was spent: try once more.
            # (A lost answer cannot be asked again: the server spent the token,
            # and presenting it twice ends the session.)
            r = client.post(endpoint, data=form, timeout=left(_REFRESH_TIMEOUT_S))
    marked = bool(entry.get("marked"))
    if marked and 400 <= r.status_code < 500 and _SESSIONS_HEADER not in r.headers:
        raise BackendUnavailable(
            f"{entry['server']} answered from a build that predates this session; retry, "
            "or run `loom login` if it persists"
        )
    if r.status_code == 400 and _oauth_error(r) == "invalid_grant":
        _drop(profile)
        why = _oauth_description(r)
        if "presented again" in why:
            # Maybe a stolen copy: what it could have minted is the owner's to check.
            why += "; if you did not just lose a refresh, check `loom call /v1/me/service_accounts`"
        raise AuthInvalid(
            f"the session for profile {profile!r} ended ({why or 'expired'}); run `loom login`"
        )
    if r.status_code != 200:
        # A 503 spent nothing (the identity provider could not be asked): retry later.
        raise _failed(r, f"refreshing the session at {entry['server']}")
    _save(
        profile,
        entry["server"],
        _json(r),
        session=entry.get("session", ""),
        # Seen once, kept: a previous build's refusal must not end it later.
        marked=marked or _SESSIONS_HEADER in r.headers,
    )
    return dict(_read()["profiles"][profile])


def _entry_for(profile: str, server: str | None) -> dict[str, Any]:
    """The profile's tokens, which must still be the ones for `server` (when
    given): a profile signed in again to another server never lends its
    token to a client made for the first."""
    entry = _read()["profiles"].get(profile)
    if not entry:
        raise AuthInvalid(f"not signed in (profile {profile!r}); run `loom login`")
    if server is not None and entry["server"] != server:
        raise AuthInvalid(
            f"profile {profile!r} is now signed in to {entry['server']}, not {server}"
        )
    return dict(entry)


def access_token(
    profile: str | None = None, *, refused: str = "", server: str | None = None
) -> str:
    """A live access token for the profile, refreshed first when it is about
    to expire or is the one the server just `refused`. With `server`, only
    while the profile still signs in there."""
    profile = profile or default_profile()
    entry = _entry_for(profile, server)
    if not _usable(entry, refused):
        try:
            with _locked():
                entry = _refresh(profile, refused, server)
        except BackendUnavailable:
            # Early, not yet expired, and not the one refused: it still works.
            if entry["access_token"] == refused or entry["expires_at"] <= time.time():
                raise
    return str(entry["access_token"])


def forget(profile: str, session: str | None = None) -> None:
    """Drop the profile's tokens from the credentials file; with `session`,
    only while the profile still holds that sign-in."""
    with _locked():
        entry = _read()["profiles"].get(profile)
        if entry and (session is None or entry.get("session", "") == session):
            _drop(profile)


def logout(profile: str | None = None) -> bool:
    """End the profile's session on the server and forget it here. Returns
    whether the server confirmed; the local tokens are gone either way."""
    profile = profile or default_profile()
    entry = _read()["profiles"].get(profile)
    if not entry:
        return False
    try:
        return _end_session(entry)
    finally:
        # A login made meanwhile is a new session: it stays.
        forget(profile, session=entry.get("session", ""))


def _end_session(entry: dict[str, Any]) -> bool:
    """End a stored session at the server it signed in to; whether it confirmed."""
    try:
        with _server(entry["server"]) as client:
            r = client.post(
                _endpoints(client)["revocation_endpoint"],
                data={
                    "token": entry.get("refresh_token") or entry["access_token"],
                    "client_id": CLIENT_ID,
                },
            )
        return r.status_code == 200
    except (LoomError, httpx.HTTPError):
        return False


def whoami(profile: str | None = None) -> dict[str, Any]:
    """Who the profile's token says you are, as the server verifies it."""
    profile = profile or default_profile()
    server = _entry_for(profile, None)["server"]
    token = access_token(profile, server=server)  # this server's token, or none
    with _server(server) as client:
        r = client.get("/v1/whoami", headers={"Authorization": f"Bearer {token}"})
    if r.status_code != 200:
        raise _failed(r, "asking the server who you are")
    info = _json(r)
    return {"profile": profile, "server": server, **info}


def stored_login(server: str, profile: str | None = None) -> StoredLogin | None:
    """The signed-in profile when it signed in to `server`, else None: a
    profile's token is never sent anywhere else."""
    profile = profile or default_profile()
    entry = _read()["profiles"].get(profile)
    if not entry or not same_server(entry["server"], server):
        return None
    return StoredLogin(profile, entry["server"])


class StoredLogin(httpx.Auth):
    """httpx auth that sends the profile's access token, refreshing it before
    it expires and once more when the server answers 401. Bound to the
    server it was made for: every read, refresh and request checks it."""

    def __init__(self, profile: str, server: str) -> None:
        self.profile = profile
        self.server = server.rstrip("/")

    def current_token(self) -> str:
        entry = _read()["profiles"].get(self.profile) or {}
        return str(entry.get("access_token", "")) if entry.get("server") == self.server else ""

    def _token(self, request: httpx.Request, refused: str = "") -> str:
        if _origin(str(request.url)) != _origin(self.server):
            raise AuthInvalid(f"refusing to send {self.server}'s session to {request.url.host}")
        return access_token(self.profile, refused=refused, server=self.server)

    def sync_auth_flow(
        self, request: httpx.Request
    ) -> Generator[httpx.Request, httpx.Response, None]:
        token = self._token(request)
        request.headers["Authorization"] = f"Bearer {token}"
        response = yield request
        if response.status_code == 401:
            request.headers["Authorization"] = f"Bearer {self._token(request, token)}"
            yield request

    async def async_auth_flow(
        self, request: httpx.Request
    ) -> AsyncGenerator[httpx.Request, httpx.Response]:
        # File locking and the refresh call block; keep them off the loop.
        token = await asyncio.to_thread(self._token, request)
        request.headers["Authorization"] = f"Bearer {token}"
        response = yield request
        if response.status_code == 401:
            token = await asyncio.to_thread(self._token, request, token)
            request.headers["Authorization"] = f"Bearer {token}"
            yield request


def signed_in_server(profile: str | None = None) -> str | None:
    """The server the profile signed in to, or None when it has not."""
    entry = _read()["profiles"].get(profile or default_profile())
    return str(entry["server"]) if entry else None
