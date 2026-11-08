"""Where a tracker namespace's record lives.

A QA *source* is one ClickHouse endpoint and database that holds the record
contract (`docs/features/tracker/record-contract.md`); a QA *namespace* names the source it
reads, the repositories it may name in a predicate, and whether it is
restricted. Every source — the default one included — is configured the same
way:

    LOOM_TRACKER_SOURCES_JSON      {"sources": {name: {host, port, secure, database}},
                               "namespaces": {ns: {source, repos, restricted}}}
    LOOM_TRACKER_CREDENTIALS_JSON  {name: {user, password}}        (a secret)

Self-service sources registered through `tracker.source.add` merge under the
operator's JSON, which wins by name.

Everything here is pure configuration parsing: no I/O, no client. The client
(`server/tracker/store/source/qa_intel.py`) asks for an endpoint and a credential by
source name; the directory (`server/tracker/domain/namespaces.py`) asks for the
namespace entries.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from server.settings import settings

log = logging.getLogger("loom.tracker.sources")

# the source the default tracker record is configured under
DEFAULT_SOURCE = "qa"
# A registered source's name: 2-32 lower-case letters, digits, - or _, starting
# with a letter (matched whole, with fullmatch). Document sources
# (`knowledge.source.add`) take the same rule.
SOURCE_NAME_RE = re.compile(r"[a-z][a-z0-9_-]{1,31}")

_warned: set[str] = set()


def _warn_once(key: str, message: str) -> None:
    if key not in _warned:
        _warned.add(key)
        log.error(message)


def reset_warnings_for_tests() -> None:
    _warned.clear()


@dataclass(frozen=True)
class TrackerEndpoint:
    """One ClickHouse service and database that holds the record contract."""

    name: str
    host: str
    port: int = 8443
    secure: bool = True
    # No default database — every tracker names its own
    database: str = ""
    # The tracker's schema mapping as canonical JSON; '' = the
    # database exposes loom's logical tables directly (views)
    mapping_json: str = ""
    # The tracker's CI-checks history — a FROM clause (a table
    # or table function) and the repository whose CI it is; '' = none here
    checks_from: str = ""
    checks_repo: str = ""
    # The tracker's bisect conventions — which path prefixes are
    # source code (ClickHouse: ("src/",)); () = any path outside docs/tests/CI
    code_prefixes: tuple[str, ...] = ()
    # the tracker's own names for contract columns: (table, contract column,
    # live column); () = the tables use the contract's names
    column_renames: tuple[tuple[str, str, str], ...] = ()

    @property
    def configured(self) -> bool:
        return bool(self.host.strip()) and bool(self.database.strip())


@dataclass(frozen=True)
class NamespaceEntry:
    """A tracker namespace's row in the source map."""

    namespace: str
    source: str
    repos: tuple[str, ...]
    restricted: bool


@dataclass(frozen=True)
class DbSource:
    """A self-service source: the entry an operator would have written, as a
    `loom.qa_sources` row holds it (the password decrypted)."""

    org: str
    name: str
    host: str
    port: int
    secure: bool
    database: str
    user: str
    password: str
    # namespace → {"repos": [...], "restricted": bool, "join_policy": "" | open | approval}
    namespaces: dict[str, dict[str, Any]] = field(default_factory=dict)
    created_by: str = ""
    # The tracker's schema mapping as JSON ('' = loom's logical
    # tables exposed directly, the views case)
    mapping_json: str = ""
    # When the source was registered; None until the first row is written.
    # Every later version row (rotate, set_mapping, retire) carries it.
    created_at: datetime | None = None


def host_of(raw: str) -> str:
    """The bare hostname of a host setting: `https://x.example/` and
    `x.example` name the same service (the first value was
    written as a URL, and the client wants a host). Userinfo (`user:pass@`)
    is dropped: the credential is its own field, never part of the host."""
    value = (raw or "").strip()
    if "://" in value:
        value = value.split("://", 1)[1]
    authority = value.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    return authority.rsplit("@", 1)[-1].split(":", 1)[0].strip()


def _json_object(raw: str, setting: str) -> dict[str, Any]:
    text = (raw or "").strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
    except Exception:  # noqa: BLE001 — a malformed setting is ignored, warned once
        _warn_once(setting, f"{setting} is not valid JSON; its entries are ignored")
        return {}
    if not isinstance(data, dict):
        _warn_once(setting, f"{setting} is not a JSON object; its entries are ignored")
        return {}
    return data


_registered: list[DbSource] = []


def set_registered(rows: list[DbSource]) -> None:
    """Replace the self-service registry's snapshot (the refresh of
    `loom.qa_sources` in `server/tracker/app/registry_sources.py` calls it)."""
    global _registered
    _registered = list(rows)


def registered() -> list[DbSource]:
    """The self-service registry's snapshot. Empty until the first refresh,
    and empty when self-service is off."""
    return list(_registered) if bool(settings.tracker_self_service_enabled) else []


def _registry_as_json() -> tuple[dict[str, Any], dict[str, Any]]:
    """The self-service registry's snapshot in the two JSON
    shapes, for the org this deployment serves."""
    sources: dict[str, Any] = {}
    namespaces: dict[str, Any] = {}
    creds: dict[str, Any] = {}
    for s in registered():
        if s.org != str(settings.tracker_org):
            continue
        sources[s.name] = {
            "host": s.host,
            "port": s.port,
            "secure": s.secure,
            "database": s.database,
            **({"mapping": s.mapping_json} if s.mapping_json else {}),
        }
        creds[s.name] = {"user": s.user, "password": s.password}
        for ns, spec in s.namespaces.items():
            namespaces[ns] = {
                "source": s.name,
                "repos": list(spec.get("repos") or []),
                "restricted": bool(spec.get("restricted", False)),
            }
    return {"sources": sources, "namespaces": namespaces}, creds


def _merged_sources_json() -> dict[str, Any]:
    """The sources map, two inputs merged section by section and entry by
    entry, later wins by name: the self-service registry, then the operator's
    JSON."""
    out: dict[str, Any] = {"sources": {}, "namespaces": {}}
    registry, _ = _registry_as_json()
    for section in ("sources", "namespaces"):
        out[section].update(registry.get(section) or {})
    data = _json_object(str(settings.tracker_sources_json), "LOOM_TRACKER_SOURCES_JSON")
    for section in ("sources", "namespaces"):
        raw = data.get(section) or {}
        if isinstance(raw, dict):
            out[section].update(raw)
    return out


def _merged_credentials() -> dict[str, Any]:
    out: dict[str, Any] = {}
    out.update(_registry_as_json()[1])
    data = _json_object(str(settings.tracker_credentials_json), "LOOM_TRACKER_CREDENTIALS_JSON")
    out.update(data)
    return out


def source_origin(name: str) -> str:
    """Where a source's entry comes from: `config` (the operator's JSON),
    `registry` (self-service), or "" when unknown. The operator's entry wins on a collision, so a
    registry row shadowed by config reports `config`."""
    data = _json_object(str(settings.tracker_sources_json), "LOOM_TRACKER_SOURCES_JSON")
    if name in (data.get("sources") or {}):
        return "config"
    if name in _registry_as_json()[0]["sources"]:
        return "registry"
    return ""


# --- the parser (one for every source) ------------------------------------------


def _endpoint_from_json(name: str, raw: Any) -> TrackerEndpoint | None:
    if not isinstance(raw, dict):
        _warn_once(f"source:{name}", f"LOOM_TRACKER_SOURCES_JSON source {name!r} is not an object")
        return None
    host = host_of(str(raw.get("host") or ""))
    database = str(raw.get("database") or "").strip()
    if not host or not database:
        _warn_once(
            f"source:{name}",
            f"LOOM_TRACKER_SOURCES_JSON source {name!r} needs `host` and `database`; ignored",
        )
        return None
    try:
        port = int(raw.get("port") or 8443)
    except (TypeError, ValueError):
        port = 8443
    return TrackerEndpoint(
        name=name,
        host=host,
        port=port,
        secure=bool(raw.get("secure", True)),
        database=database,
        mapping_json=_mapping_json(name, raw.get("mapping")),
        checks_from=str((raw.get("checks") or {}).get("from") or "").strip()
        if isinstance(raw.get("checks"), dict)
        else "",
        checks_repo=str((raw.get("checks") or {}).get("repo") or "").strip()
        if isinstance(raw.get("checks"), dict)
        else "",
        code_prefixes=tuple(
            str(p).strip()
            for p in ((raw.get("bisect") or {}).get("code_prefixes") or [])
            if str(p).strip()
        )
        if isinstance(raw.get("bisect"), dict)
        else (),
        column_renames=_column_renames(name, raw.get("columns")),
    )


_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _column_renames(name: str, raw: Any) -> tuple[tuple[str, str, str], ...]:
    """A source entry's `columns` — {table: {contract column: live column}} — as
    sorted triples. Only plain identifiers are kept: the live name lands in SQL."""
    if raw is None:
        return ()
    if not isinstance(raw, dict):
        _warn_once(f"columns:{name}", f"tracker source {name!r}: `columns` is not an object")
        return ()
    out: list[tuple[str, str, str]] = []
    for table, renames in raw.items():
        for col, live in (renames or {}).items() if isinstance(renames, dict) else ():
            triple = (str(table), str(col), str(live))
            if all(_IDENT_RE.match(x) for x in triple):
                out.append(triple)
            else:
                _warn_once(
                    f"columns:{name}:{table}.{col}",
                    f"tracker source {name!r}: column rename {table}.{col} → {live!r} "
                    "is not a plain identifier; ignored",
                )
    return tuple(sorted(out))


def column_renames(name: str) -> dict[str, dict[str, str]]:
    """The source's configured renames, table → {contract column: live column}."""
    ep = endpoint(name)
    out: dict[str, dict[str, str]] = {}
    for table, col, live in ep.column_renames if ep is not None else ():
        out.setdefault(table, {})[col] = live
    return out


def _mapping_json(name: str, raw: Any) -> str:
    """A source entry's `mapping` (an object, or its JSON text) as canonical
    JSON; '' for none. One that does not parse is kept as given — the
    contract check then fails it with the parse error, visibly, rather than
    this map silently reading the tracker as views."""
    if raw in (None, "", {}):
        return ""
    from server.tracker.domain import mapping

    try:
        parsed = mapping.parse(raw)
    except Exception as e:  # noqa: BLE001 — kept as given; the contract check fails it visibly
        _warn_once(f"mapping:{name}", f"tracker source {name!r}: mapping does not parse: {e}")
        return raw if isinstance(raw, str) else json.dumps(raw, sort_keys=True)
    return parsed.to_json() if parsed is not None else ""


def mapping_for(name: str) -> Any:
    """The source's `mapping.SourceMapping`, or None (the views case).
    Raises `mapping.MappingError` for a mapping that does not parse."""
    ep = endpoint(name)
    if ep is None or not ep.mapping_json:
        return None
    from server.tracker.domain import mapping

    return mapping.parse(ep.mapping_json)


def endpoints() -> dict[str, TrackerEndpoint]:
    """Source name → endpoint, from the merged map."""
    out: dict[str, TrackerEndpoint] = {}
    for name, raw in _merged_sources_json()["sources"].items():
        key = str(name or "").strip()
        if not key:
            continue
        ep = _endpoint_from_json(key, raw)
        if ep is not None:
            out[key] = ep
    return out


def endpoint(name: str) -> TrackerEndpoint | None:
    return endpoints().get(name)


def _entry_from_json(ns: str, raw: Any, known_sources: set[str]) -> NamespaceEntry | None:
    if not isinstance(raw, dict):
        _warn_once(f"ns:{ns}", f"LOOM_TRACKER_SOURCES_JSON namespace {ns!r} is not an object")
        return None
    source = str(raw.get("source") or "").strip()
    if source not in known_sources:
        _warn_once(
            f"ns:{ns}",
            f"LOOM_TRACKER_SOURCES_JSON namespace {ns!r} names source {source!r}, which is not "
            "configured; the namespace does not serve",
        )
        return None
    repos_raw = raw.get("repos") or []
    if not isinstance(repos_raw, list):  # kept with no repositories: it stays restricted and in use
        _warn_once(
            f"repos:{ns}", f"LOOM_TRACKER_SOURCES_JSON namespace {ns!r}: repos is not a list"
        )
        repos_raw = []
    repos = tuple(sorted({str(r).strip() for r in repos_raw if str(r).strip()}))
    return NamespaceEntry(
        namespace=ns, source=source, repos=repos, restricted=bool(raw.get("restricted", False))
    )


def namespace_entries() -> dict[str, NamespaceEntry]:
    """Namespace → entry, from the merged map. A namespace may name a source
    that has no endpoint yet (the directory, the boot registry and the
    bindings know the namespace; the client refuses the read) — only a
    source named nowhere at all is dropped, with one error line."""
    merged = _merged_sources_json()
    known = set(merged["sources"]) | {DEFAULT_SOURCE}
    out: dict[str, NamespaceEntry] = {}
    for ns, raw in merged["namespaces"].items():
        key = str(ns or "").strip()
        if not key:
            continue
        entry = _entry_from_json(key, raw, known)
        if entry is not None:
            out[key] = entry
    return out


def credentials_for(source: str) -> tuple[str, str]:
    """(user, password) for a source from the merged credentials; ("", "")
    when none — the client then refuses the read as unconfigured."""
    raw = _merged_credentials().get(source)
    if isinstance(raw, dict) and str(raw.get("user") or "").strip():
        return str(raw.get("user") or "").strip(), str(raw.get("password") or "")
    return "", ""


def credential_configured(credential: str = DEFAULT_SOURCE) -> bool:
    """An endpoint with a host and a database, and a user for it."""
    ep = endpoint(credential)
    return ep is not None and ep.configured and bool(credentials_for(credential)[0])


__all__ = [
    "DEFAULT_SOURCE",
    "SOURCE_NAME_RE",
    "NamespaceEntry",
    "TrackerEndpoint",
    "credential_configured",
    "credentials_for",
    "endpoint",
    "endpoints",
    "host_of",
    "namespace_entries",
    "reset_warnings_for_tests",
    "source_origin",
]
