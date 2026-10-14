"""Canonical repository remotes for self-service code indexes."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

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
    if clean.endswith(".git"):
        clean = clean[:-4]
    if not clean or ".." in clean.split("/") or any(not part for part in clean.split("/")):
        raise ValueError("repository URL must name a repository path")
    return clean


def parse_remote(value: str) -> RemoteSpec:
    """Return a credential-free canonical identity and clone URL.

    A bare ``owner/name`` remains the backwards-compatible GitHub form. HTTPS and
    SSH remotes may point at any host; local paths and URLs containing credentials
    are refused because the registry is shared server state.
    """
    raw = (value or "").strip().rstrip("/")
    if not raw or any(c in raw for c in ("\n", "\r", "\0")):
        raise ValueError("repository must be owner/name or an HTTPS/SSH Git URL")
    if (
        _SLUG_RE.fullmatch(raw)
        and ".." not in raw
        and all(part not in {".", ".."} for part in raw.split("/"))
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
