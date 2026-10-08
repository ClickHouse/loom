"""Build the tracker source configuration the way a deploy sets it: one
`LOOM_TRACKER_SOURCES_JSON` and one `LOOM_TRACKER_CREDENTIALS_JSON`."""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any


def tracker_sources_json(
    repo_map: str | dict[str, str] | None = None,
    *,
    restricted: str | Iterable[str] = "",
    source: str = "qa",
    host: str = "qa.example",
    port: int = 8443,
    secure: bool = True,
    database: str = "qa_intelligence",
    extra_sources: dict[str, Any] | None = None,
    extra_namespaces: dict[str, Any] | None = None,
) -> str:
    """`repo_map` is `{repo: namespace}` (a dict or its JSON text); `restricted` a namespace
    name, a comma-separated list or an iterable. Returns the JSON text."""
    if isinstance(repo_map, str):
        repo_map = json.loads(repo_map) if repo_map.strip() else {}
    repo_map = repo_map or {}
    if isinstance(restricted, str):
        flagged = {s.strip() for s in restricted.split(",") if s.strip()}
    else:
        flagged = {str(s) for s in restricted}
    namespaces: dict[str, Any] = {}
    for repo, ns in repo_map.items():
        entry = namespaces.setdefault(
            ns, {"source": source, "repos": [], "restricted": ns in flagged}
        )
        entry["repos"].append(repo)
    namespaces.update(extra_namespaces or {})
    sources: dict[str, Any] = {
        source: {"host": host, "port": port, "secure": secure, "database": database}
    }
    sources.update(extra_sources or {})
    return json.dumps({"sources": sources, "namespaces": namespaces})


def tracker_credentials_json(
    user: str = "ro", password: str = "pw", source: str = "qa", **others: dict[str, str]
) -> str:
    creds: dict[str, Any] = {source: {"user": user, "password": password}} if user else {}
    creds.update(others)
    return json.dumps(creds)
