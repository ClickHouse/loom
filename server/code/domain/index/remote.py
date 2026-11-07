"""Canonical repository remotes for self-service code indexes."""

from __future__ import annotations

import hashlib
import posixpath
import re
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit, urlunsplit

_SLUG_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_SCP_RE = re.compile(r"^(?P<user>[A-Za-z0-9_.-]+)@(?P<host>[A-Za-z0-9.-]+):(?P<path>[^\s]+)$")
_KNOWN_HOSTS = {
    "github.com": "github",
    "gitlab.com": "gitlab",
    "bitbucket.org": "bitbucket",
}


@dataclass(frozen=True, slots=True)
class RemoteSpec:
    repo: str
    url: str
    provider: str
    host: str


def _clean_path(path: str) -> str:
    clean = path.strip("/")
    if clean.lower().endswith(".git"):
        clean = clean[:-4]
    parts = clean.split("/")
    # A dot segment, spelled out or percent-encoded, names the same repository as the
    # path without it: refused, so one repository has one spelling here.
    if not clean or any(not part or unquote(part) in {".", ".."} for part in parts):
        raise ValueError("repository URL must name a repository path")
    return clean


def parse_remote(value: str) -> RemoteSpec:
    """Return a credential-free canonical identity and clone URL.

    A bare ``owner/name`` is a GitHub repository. HTTPS and
    SSH remotes may point at any host; local paths and URLs containing credentials
    are refused because the registry is shared server state.
    """
    raw = (value or "").strip().rstrip("/")
    if not raw or any(c in raw for c in ("\n", "\r", "\0")):
        raise ValueError("repository must be owner/name or an HTTPS/SSH Git URL")
    if (
        _SLUG_RE.fullmatch(raw)
        and ".." not in raw
        and all(part.lower() not in {".", "..", ".git"} for part in raw.split("/"))
    ):
        return RemoteSpec(raw, f"https://github.com/{raw}.git", "github", "github.com")

    scp = _SCP_RE.fullmatch(raw)
    if scp:
        host = scp.group("host").lower()
        path = _clean_path(scp.group("path"))
        provider = _KNOWN_HOSTS.get(host, "git")
        repo = path if provider == "github" else f"{host}/{path}"
        return RemoteSpec(repo, raw if raw.endswith(".git") else f"{raw}.git", provider, host)

    parsed = urlsplit(raw)
    if parsed.scheme not in {"https", "ssh"} or not parsed.hostname:
        raise ValueError("repository must use HTTPS or SSH; local and file remotes are not allowed")
    if parsed.username and parsed.scheme == "https":
        raise ValueError(
            "repository URLs must not contain credentials; pass the access token separately"
        )
    if parsed.password or parsed.query or parsed.fragment:
        raise ValueError("repository URL must not contain credentials, a query, or a fragment")
    host = parsed.hostname.lower()
    if not host.isascii() or "%" in host:
        raise ValueError("repository host must be an ASCII name (punycode for any other)")
    path = _clean_path(parsed.path)
    provider = _KNOWN_HOSTS.get(host, "git")
    repo = path if provider == "github" else f"{host}/{path}"
    netloc = host
    if parsed.port:
        netloc = f"{host}:{parsed.port}"
    if parsed.scheme == "ssh" and parsed.username:
        netloc = f"{parsed.username}@{netloc}"
    url = urlunsplit((parsed.scheme, netloc, f"/{path}.git", "", ""))
    return RemoteSpec(repo, url, provider, host)


# Hosts that serve one host's repositories under another name, and the hosts that take
# any port for their own. Used by `repo_key` alone.
_HOST_ALIASES = {
    "www.github.com": "github.com",
    "www.gitlab.com": "gitlab.com",
    "www.bitbucket.org": "bitbucket.org",
    "ssh.github.com": "github.com",
    "altssh.gitlab.com": "gitlab.com",
    "altssh.bitbucket.org": "bitbucket.org",
}
_ANY_PORT_HOSTS = {"github.com", "gitlab.com", "bitbucket.org"}
_DEFAULT_PORTS = {None, 22, 80, 443}


def _remote_parts(value: str) -> tuple[str, int | None, str] | None:
    """(host, port, path) of a URL or `user@host:path` remote, None for anything else."""
    scp = _SCP_RE.fullmatch(value)
    if scp:
        return scp.group("host"), None, scp.group("path")
    if "://" not in value:
        return None
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return None
    if parsed.scheme == "file" or not parsed.hostname:
        return None  # a local mirror: the entry's name says which repository it is
    return parsed.hostname, port, parsed.path


def _host_key(host: str) -> str:
    host = host.lower()
    try:
        host = host.encode("idna").decode("ascii")  # NFKC-maps a fullwidth letter
    except UnicodeError:
        pass
    host = host.rstrip(".")
    return _HOST_ALIASES.get(host, host)


def repo_key(repo: str, git_remote: str) -> str:
    """One key per repository, for telling whether two entries index the same one, and
    for nothing else: clones, mirrors and provider calls use `parse_remote`. The remote
    decides when it is a URL or `user@host:path` with a host; a local path or `file://`
    mirror leaves the entry's `repo` to decide (a bare `owner/name` is GitHub unless its
    first segment holds a dot, which makes it the host).
    The host is lower-cased and IDNA-folded, its trailing dot, default port (any port on
    github.com, gitlab.com and bitbucket.org) and aliases dropped; the path is
    percent-decoded, its dot segments and empty parts folded, its leading and trailing
    `/` and `.git` dropped, and lower-cased."""
    raw = (repo or "").strip()
    parts = _remote_parts((git_remote or "").strip()) or _remote_parts(raw)
    if parts is None:
        # A GitHub login holds no dot, so a dotted first segment is the host.
        first, _, rest = raw.partition("/")
        if "." in first and rest:
            name, _, digits = first.partition(":")
            parts = (name, int(digits) if digits.isdigit() else None, rest)
        else:
            parts = ("github.com" if _SLUG_RE.fullmatch(raw) else "", None, raw)
    host, port, path = parts
    host = _host_key(host)
    if port not in _DEFAULT_PORTS and host not in _ANY_PORT_HOSTS:
        host = f"{host}:{port}"
    path = posixpath.normpath("/" + unquote(path)).strip("/").lower()
    while path.endswith(".git"):
        path = path[:-4].rstrip("/")
    return f"{host}/{path}" if host else path


def provider_for(remote: str) -> str:
    try:
        return parse_remote(remote).provider
    except ValueError:
        return "git"


def repo_tag(remote: str) -> str:
    """The `repo:<id>` tag the coding plugins stamp on session memories for this remote:
    10 hex of sha256 over `host/owner/name`, lower-cased, with the scheme, user, port and
    `.git` dropped (the plugins' `repoIdentity`). '' when the remote has no owner/name."""
    u = re.sub(r"^[a-z+]+://", "", (remote or "").strip(), flags=re.I)
    u = re.sub(r"^[^@/]+@", "", u)
    u = re.sub(r":(\d+)?/", "/", u, count=1).replace(":", "/", 1)
    u = re.sub(r"\.git$", "", u.rstrip("/"), flags=re.I).rstrip("/").lower()
    parts = [p for p in u.split("/") if p]
    if len(parts) < 3:
        return ""
    return "repo:" + hashlib.sha256("/".join(parts[:3]).encode()).hexdigest()[:10]
