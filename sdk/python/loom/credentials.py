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
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
import tempfile
import time
import webbrowser
from collections.abc import AsyncGenerator, Callable, Generator, Iterator
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from loom.errors import AuthInvalid, BackendUnavailable, LoomError

CREDENTIALS_FILE = Path("~/.loom/credentials")
DEFAULT_SERVER = "http://localhost:7777"
CLIENT_ID = "loom-cli"
_DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
# Refresh this long before the access token expires, so a call never leaves
# with a token that dies in flight.
_REFRESH_MARGIN_S = 60
# Longer than a refresh can take on the server, which asks the identity
# provider first (discovery and token calls, 10 s each): giving up sooner
# would resend a token the server has already spent, which ends the session.
_REFRESH_TIMEOUT_S = 45.0


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


@contextlib.contextmanager
def _locked() -> Iterator[None]:
    """Hold the credentials lock (a sibling file, so the atomic replace of the
    credentials themselves never drops it). No-op where flock does not exist."""
    path = _path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        import fcntl
    except ImportError:  # Windows: one process per profile is on the user
        yield
        return
    with open(path.with_name(".credentials.lock"), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


@contextlib.contextmanager
def _server(url: str) -> Iterator[httpx.Client]:
    """A client for `url` whose failures to reach it come out as
    BackendUnavailable: callers catch LoomError, never httpx."""
    try:
        with httpx.Client(base_url=url, timeout=30.0) as client:
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


def _endpoints(client: httpx.Client) -> dict[str, str]:
    """The server's sign-in endpoints, each checked to be on the server itself,
    so a code or token only ever goes where the user chose to sign in."""
    r = client.get("/.well-known/oauth-authorization-server")
    if r.status_code == 404:
        raise LoomError(
            f"{client.base_url} has no sign-in: it runs without auth (no login needed) "
            "or predates `loom login`"
        )
    if r.status_code != 200:
        raise _failed(r, "reading the server's sign-in endpoints")
    meta = _json(r)
    server = urlsplit(str(client.base_url))
    endpoints = {}
    for name in ("device_authorization_endpoint", "token_endpoint", "revocation_endpoint"):
        url = urlsplit(str(meta.get(name, "")))
        if (url.scheme, url.netloc) != (server.scheme, server.netloc):
            raise LoomError(
                f"{client.base_url} signs in at {url.scheme}://{url.netloc}; use that as the server"
            )
        endpoints[name] = str(meta[name])
    return endpoints


def _failed(r: httpx.Response, doing: str) -> LoomError:
    """The typed SDK error for a refused sign-in request: a caller catches
    LoomError, never an httpx one."""
    error = _oauth_error(r) or f"HTTP {r.status_code}"
    if r.status_code == 429 or r.status_code >= 500:
        return BackendUnavailable(f"{doing}: {error}; retry")
    if r.status_code in (401, 403) or error == "invalid_grant":
        return AuthInvalid(f"{doing}: {error}; run `loom login` again")
    return LoomError(f"{doing}: {error}")


def _oauth_error(r: httpx.Response) -> str:
    try:
        body = r.json()
    except ValueError:
        return ""
    detail = body.get("detail", body) if isinstance(body, dict) else {}
    return str(detail.get("error", "")) if isinstance(detail, dict) else ""


def _save(profile: str, server: str, tokens: dict[str, Any]) -> None:
    data = _read()
    data["profiles"][profile] = {
        "server": server,
        "access_token": tokens["access_token"],
        "refresh_token": tokens.get("refresh_token", ""),
        "expires_at": int(time.time()) + int(tokens.get("expires_in", 3600)),
    }
    _write(data)


def _say(line: str) -> None:
    # Flushed: a piped or non-terminal stdout must still show the code now.
    print(line, flush=True)


def login(
    server: str | None = None,
    *,
    profile: str | None = None,
    open_browser: bool = True,
    echo: Callable[[str], None] = _say,
) -> dict[str, Any]:
    """Sign in to `server` (default `LOOM_BASE_URL`, else localhost) and store
    the tokens under `profile`. Blocks until the sign-in is approved, denied
    or expires (ten minutes). Returns what `whoami()` returns."""
    server = (server or os.environ.get("LOOM_BASE_URL") or DEFAULT_SERVER).rstrip("/")
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
        tokens = _poll(client, meta["token_endpoint"], grant)
    with _locked():  # another process may be rotating a profile in the same file
        _save(profile, server, tokens)
    info = whoami(profile=profile)
    # Said every time: a code someone else approved signs you in as them.
    echo(f"Signed in to {server} as {info.get('subject')} ({info.get('org')}).")
    return info


def _poll(client: httpx.Client, token_endpoint: str, grant: dict[str, Any]) -> dict[str, Any]:
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
            continue  # the user may already have approved: keep asking until the deadline
        if r.status_code >= 500:
            continue
        if r.status_code == 200:
            return _json(r)
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


def _refresh(profile: str, refused: str) -> dict[str, Any]:
    """Rotate the profile's tokens. Called with the lock held."""
    entry = _read()["profiles"].get(profile)
    if not entry:
        raise AuthInvalid(f"not signed in (profile {profile!r}); run `loom login`")
    if _usable(entry, refused):
        return dict(entry)  # another process refreshed it while we waited
    if not entry.get("refresh_token"):
        raise AuthInvalid(f"the session for profile {profile!r} ended; run `loom login`")
    form = {
        "grant_type": "refresh_token",
        "refresh_token": entry["refresh_token"],
        "client_id": CLIENT_ID,
    }
    with _server(entry["server"]) as client:
        endpoint = _endpoints(client)["token_endpoint"]
        try:
            r = client.post(endpoint, data=form, timeout=_REFRESH_TIMEOUT_S)
        except (httpx.ConnectError, httpx.ConnectTimeout):
            # Nothing reached the server, so nothing was spent: try once more.
            r = client.post(endpoint, data=form, timeout=_REFRESH_TIMEOUT_S)
    if r.status_code == 400 and _oauth_error(r) == "invalid_grant":
        _drop(profile)
        raise AuthInvalid(f"the session for profile {profile!r} ended; run `loom login`")
    if r.status_code != 200:
        # A 503 spent nothing (the identity provider could not be asked): retry later.
        raise _failed(r, f"refreshing the session at {entry['server']}")
    _save(profile, entry["server"], _json(r))
    return dict(_read()["profiles"][profile])


def access_token(profile: str | None = None, *, refused: str = "") -> str:
    """A live access token for the profile, refreshed first when it is about
    to expire or is the one the server just `refused`."""
    profile = profile or default_profile()
    entry = _read()["profiles"].get(profile)
    if not entry:
        raise AuthInvalid(f"not signed in (profile {profile!r}); run `loom login`")
    if not _usable(entry, refused):
        try:
            with _locked():
                entry = _refresh(profile, refused)
        except BackendUnavailable:
            # Early, not yet expired, and not the one refused: it still works.
            if entry["access_token"] == refused or entry["expires_at"] <= time.time():
                raise
    return str(entry["access_token"])


def forget(profile: str) -> None:
    """Drop the profile's tokens from the credentials file."""
    with _locked():
        _drop(profile)


def logout(profile: str | None = None) -> bool:
    """End the profile's session on the server and forget it here. Returns
    whether the server confirmed; the local tokens are gone either way."""
    profile = profile or default_profile()
    entry = _read()["profiles"].get(profile)
    if not entry:
        return False
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
    except LoomError:
        return False
    finally:
        forget(profile)


def whoami(profile: str | None = None) -> dict[str, Any]:
    """Who the profile's token says you are, as the server verifies it."""
    profile = profile or default_profile()
    token = access_token(profile)
    server = _read()["profiles"][profile]["server"]
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
    if not entry or entry["server"] != server.rstrip("/"):
        return None
    return StoredLogin(profile)


class StoredLogin(httpx.Auth):
    """httpx auth that sends the profile's access token, refreshing it before
    it expires and once more when the server answers 401."""

    def __init__(self, profile: str) -> None:
        self.profile = profile

    def current_token(self) -> str:
        entry = _read()["profiles"].get(self.profile) or {}
        return str(entry.get("access_token", ""))

    def sync_auth_flow(
        self, request: httpx.Request
    ) -> Generator[httpx.Request, httpx.Response, None]:
        token = access_token(self.profile)
        request.headers["Authorization"] = f"Bearer {token}"
        response = yield request
        if response.status_code == 401:
            request.headers["Authorization"] = f"Bearer {access_token(self.profile, refused=token)}"
            yield request

    async def async_auth_flow(
        self, request: httpx.Request
    ) -> AsyncGenerator[httpx.Request, httpx.Response]:
        # File locking and the refresh call block; keep them off the loop.
        token = await asyncio.to_thread(access_token, self.profile)
        request.headers["Authorization"] = f"Bearer {token}"
        response = yield request
        if response.status_code == 401:
            token = await asyncio.to_thread(access_token, self.profile, refused=token)
            request.headers["Authorization"] = f"Bearer {token}"
            yield request


def signed_in_server(profile: str | None = None) -> str | None:
    """The server the profile signed in to, or None when it has not."""
    entry = _read()["profiles"].get(profile or default_profile())
    return str(entry["server"]) if entry else None
