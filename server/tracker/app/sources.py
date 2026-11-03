"""tracker.source.* — the tracker sources an org has, read for the dashboard and the
CLI.

`tracker.source.list` is org-level (no namespace to gate on): any signed-in
principal of the tracker org (LOOM_TRACKER_ORG) sees every configured source, its contract verdict,
its breaker, and the tracker namespaces reading it — each with the directory
row's effective join rule and the caller's own standing there, so a card can
offer Join / Request / Open without another call. Secrets are never on the
wire: a source is named by host and database, never by user or password.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import re
from typing import Any

from server.auth.context import current_caller
from server.core.errors import AuthInsufficientError, ValidationError
from server.settings import settings
from server.tracker.api.models import (
    TrackerContractInput,
    TrackerContractOutput,
    TrackerSourceAddInput,
    TrackerSourceAddOutput,
    TrackerSourceCheckInput,
    TrackerSourceCheckOutput,
    TrackerSourceInspectInput,
    TrackerSourceInspectOutput,
    TrackerSourceListInput,
    TrackerSourceListOutput,
    TrackerSourceRemoveInput,
    TrackerSourceRemoveOutput,
    TrackerSourceRotateInput,
    TrackerSourceRotateOutput,
    TrackerSourceSetMappingInput,
    TrackerSourceSetMappingOutput,
)
from server.tracker.app import contract
from server.tracker.domain import contract as record_contract
from server.tracker.store.source import contract_reports

logger = logging.getLogger(__name__)


def _require_same_org(org: str) -> None:
    from server.auth.authz import may_act_in_org
    from server.auth.context import current_auth

    auth = current_auth()
    if auth is not None and not may_act_in_org(auth, org):
        raise AuthInsufficientError(f"principal cannot act in org {org!r}")


def _serves_trackers(org: str) -> bool:
    """Tracker sources and their namespaces belong to the one tracker org
    (LOOM_TRACKER_ORG, unset = none): another org has no source to see or check."""
    from server.settings import settings

    return bool(settings.tracker_org) and str(org) == str(settings.tracker_org)


async def _directory_rows(org: str, names: list[str]) -> dict[str, dict[str, Any]]:
    """The loom.namespaces rows of the tracker namespaces, by name."""
    if not names:
        return {}
    from server.core.ch import get_client

    rows = await get_client().query_all(
        """
        SELECT namespace, toString(visibility) AS visibility, toString(join_policy) AS join_policy,
               requires_approval, toString(classification) AS classification,
               toString(kind) AS kind, active
        FROM loom.namespaces FINAL
        WHERE org = {org:String} AND namespace IN {names:Array(String)}
        """,
        params={"org": org, "names": names},
    )
    return {str(r.get("namespace") or ""): dict(r) for r in rows}


async def _caller_grants(org: str, subject: str, names: list[str]) -> dict[str, dict[str, str]]:
    """The caller's live grant per tracker namespace: {namespace: {role, pending_role}}."""
    if not subject or not names:
        return {}
    from server.auth.aliases import subject_aliases
    from server.core.ch import get_client

    subs = await subject_aliases(org, subject)
    rows = await get_client().query_all(
        """
        SELECT namespace, toString(role) AS role, toString(status) AS status,
               toString(ifNull(pending_role, '')) AS pending_role
        FROM loom.namespace_grants FINAL
        WHERE org = {org:String} AND subject IN {subs:Array(String)}
          AND namespace IN {names:Array(String)} AND revoked_at IS NULL
        """,
        params={"org": org, "sub": subject, "subs": subs, "names": names},
    )
    out: dict[str, dict[str, str]] = {}
    for r in rows:
        ns = str(r.get("namespace") or "")
        status = str(r.get("status") or "active")
        role = str(r.get("role") or "")
        pending = str(r.get("pending_role") or "")
        if status == "pending":
            out[ns] = {"role": "", "pending_role": pending or role}
        else:
            out[ns] = {"role": role, "pending_role": pending}
    return out


CONTRACT_SNAPSHOT_KEY = "qa_contract_snapshot"  # storage key
FRESHNESS_SNAPSHOT_KEY = "qa_freshness_snapshot"


async def _published(
    org: str, ns: str, key: str, max_age_s: float | None = None
) -> dict[str, Any] | None:
    """A view another process published within
    `max_age_s`, from `code_index_state`; None for none, stale or unparsable."""
    if max_age_s is None:
        max_age_s = settings.tracker_snapshot_ttl_s
    import json
    from datetime import UTC, datetime

    from server.code.app.reads import code_index

    try:
        rows = await code_index().all_state(org, ns)
    except Exception as e:  # noqa: BLE001 — reads live instead
        logger.debug("tracker.source.list: published read failed: %s", e)
        return None
    row = rows.get(key) if isinstance(rows, dict) else None
    if not isinstance(row, dict) or not row.get("value"):
        return None
    at = row.get("updated_at")
    if not isinstance(at, datetime):
        return None
    at_utc = at if at.tzinfo else at.replace(tzinfo=UTC)
    if (datetime.now(UTC) - at_utc).total_seconds() > max_age_s:
        return None
    try:
        out = json.loads(str(row["value"]))
    except Exception:  # noqa: BLE001 — reads live instead
        return None
    return out if isinstance(out, dict) else None


async def _publish(org: str, ns: str, key: str, view: dict[str, Any]) -> None:
    import json

    from server.code.app.reads import code_index

    try:
        await code_index().set_state(
            org,
            ns,
            key,
            json.dumps(view, default=str),
            detail="tracker view shared across processes",
        )
    except Exception as e:  # noqa: BLE001 — a failed publish costs nothing but the next miss
        logger.debug("tracker.source.list: publish failed: %s", e)


def _source_ns(name: str) -> str:
    """The `code_index_state` namespace a source's org-level rows live under."""
    return f"tracker-source:{name}"


async def _contract_view(
    name: str, database: str, configured: bool, org: str = ""
) -> dict[str, Any]:
    """The cached contract verdict, else one bounded check; never raises."""

    rep = contract_reports.cached(name)
    if rep is None and configured and org:
        # A verdict another process reached within the minute — the check itself costs ~120 ms
        # of handler time, which a cold listing would otherwise pay on most dashboard calls.
        published = await _published(org, _source_ns(name), CONTRACT_SNAPSHOT_KEY)
        if published is not None:
            return published
    checked_here = False
    if rep is None and configured and contract.enforced():
        try:
            rep = await asyncio.wait_for(
                contract.check(name, database), timeout=settings.tracker_contract_check_budget_s
            )
            checked_here = rep is not None
        except Exception as e:  # noqa: BLE001 — the card says "unchecked", not a 500
            logger.info("tracker.source.list: contract check of %r did not answer: %s", name, e)
            rep = None
    if rep is None:
        return {
            "ok": None,
            "summary": "unchecked",
            "checked_at": "",
            "optional_present": [],
            "features": {},
        }
    d = rep.to_dict()
    view = {
        "ok": d["ok"],
        "summary": d["summary"],
        "checked_at": _iso(rep.checked_at),
        "optional_present": d["optional_present"],
        "features": d["features"],
    }
    if checked_here and org:
        await _publish(org, _source_ns(name), CONTRACT_SNAPSHOT_KEY, view)
    return view


def _iso(monotonic_at: float) -> str:
    """A `time.monotonic()` stamp rendered as the wall-clock ISO instant it
    corresponds to ("" when never)."""
    import time
    from datetime import UTC, datetime

    if not monotonic_at:
        return ""
    wall = time.time() - (time.monotonic() - monotonic_at)
    return datetime.fromtimestamp(wall, UTC).replace(microsecond=0).isoformat()


def _breaker_view(name: str, configured: bool) -> dict[str, Any]:
    """The source's breaker; a source without a credential has nothing to
    break yet and says so."""
    from server.tracker.store.source import qa_intel

    if not configured:
        return {"state": "unconfigured", "consecutive_failures": 0}
    try:
        return dict(qa_intel.circuit_status(name))
    except Exception:  # noqa: BLE001 — no slot yet: nothing has failed
        return {"state": "CLOSED", "consecutive_failures": 0}


async def _freshness(org: str, namespace: str) -> dict[str, Any]:
    """Item counts and the newest update of the source, read through one
    standard namespace of it; {} when it cannot be read in time."""
    from server.tracker.app.source import tracker_source

    # The counts another process read within the
    # minute, before a bounded read of the source itself.
    published = await _published(org, namespace, FRESHNESS_SNAPSHOT_KEY)
    if published is not None:
        return published
    try:
        out = await asyncio.wait_for(
            tracker_source(org, namespace).item_counts(org, namespace),
            timeout=settings.tracker_freshness_budget_s,
        )
    except Exception as e:  # noqa: BLE001 — freshness is advisory
        logger.info("tracker.source.list: freshness of %s/%s did not answer: %s", org, namespace, e)
        return {}
    if isinstance(out, dict) and out:
        await _publish(org, namespace, FRESHNESS_SNAPSHOT_KEY, out)
    return out


def _namespace_view(
    entry: Any, row: dict[str, Any] | None, grant: dict[str, str] | None
) -> dict[str, Any]:
    from server.identity.domain.join_policy import effective_join_policy

    if row is not None:
        join_policy = effective_join_policy(row)
        classification = str(row.get("classification") or "standard")
        visibility = str(row.get("visibility") or "private")
        registered = bool(row.get("active", True))
    else:
        # the row is written at the leader's next boot; until
        # then the map says what it will be
        join_policy = "approval" if entry.restricted else "open"
        classification = "restricted" if entry.restricted else "standard"
        visibility = "private"
        registered = False
    return {
        "namespace": entry.namespace,
        "kind": "tracker",
        "visibility": visibility,
        "repos": list(entry.repos),
        "classification": classification,
        "join_policy": join_policy,
        "registered": registered,
        "member_role": (grant or {}).get("role", ""),
        "pending_role": (grant or {}).get("pending_role", ""),
    }


async def handle_tracker_source_list(input: TrackerSourceListInput) -> TrackerSourceListOutput:
    from server.settings import settings
    from server.tracker.app import registry_sources
    from server.tracker.domain import sources

    _require_same_org(input.org)
    if not _serves_trackers(input.org):
        return TrackerSourceListOutput(
            sources=[], self_service=bool(settings.tracker_self_service_enabled)
        )
    # The TTL refresh runs on every listing, so a tracker added on another process shows up
    # here without waiting for this process's next tracker read; `fresh` (the page right after
    # its own add) forces a consistent re-read
    await registry_sources.refresh_db_sources(force=bool(input.fresh))
    subject, _ = current_caller()
    eps = sources.endpoints()
    entries = sources.namespace_entries()
    names = sorted(entries)
    rows, grants = await asyncio.gather(
        _directory_rows(input.org, names), _caller_grants(input.org, subject, names)
    )
    source_names = sorted(set(eps) | {e.source for e in entries.values()})
    out: list[dict[str, Any]] = []
    for name in source_names:
        ep = eps.get(name)
        configured = ep is not None and sources.credential_configured(name)
        ns_views = [
            _namespace_view(e, rows.get(ns), grants.get(ns))
            for ns, e in sorted(entries.items())
            if e.source == name
        ]
        fresh_ns = next((v["namespace"] for v in ns_views if v["classification"] == "standard"), "")
        contract_view, freshness = await asyncio.gather(
            _contract_view(name, ep.database if ep else "", configured, org=input.org),
            _freshness(input.org, fresh_ns) if (configured and fresh_ns) else _none(),
        )
        out.append(
            {
                "name": name,
                "origin": sources.source_origin(name),
                "host": ep.host if ep else "",
                "port": ep.port if ep else 0,
                "database": ep.database if ep else "",
                "configured": configured,
                "contract": contract_view,
                "breaker": _breaker_view(name, configured),
                "freshness": freshness,
                "namespaces": ns_views,
                # How the tracker is read — its own tables through a
                # mapping (no secret in it: table and column names), or loom's views
                "mode": "mapping" if (ep is not None and ep.mapping_json) else "views",
                "mapping": _mapping_view(ep.mapping_json if ep is not None else ""),
            }
        )
    return TrackerSourceListOutput(
        sources=out, self_service=bool(settings.tracker_self_service_enabled)
    )


def _mapping_view(raw: str) -> dict[str, Any] | None:
    if not raw:
        return None
    import json

    try:
        out = json.loads(raw)
    except ValueError:
        return None
    return out if isinstance(out, dict) else None


async def _none() -> dict[str, Any]:
    return {}


# ---- the write ops ---------------------------------------------

_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{1,31}$")
# `tracker-<team>` or `qa-<team>`
_NS_RE = re.compile(r"^(?:tracker|qa)-[a-z0-9][a-z0-9-]{1,62}$")
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def _require_self_service() -> None:
    from server.settings import settings

    if not bool(settings.tracker_self_service_enabled):
        raise ValidationError(
            "registering tracker sources from the page is off on this deployment "
            "(LOOM_TRACKER_SELF_SERVICE_ENABLED); ask the operator",
            code="SELF_SERVICE_OFF",
        )


def _namespace_specs(input: Any) -> list[dict[str, Any]]:
    """Validated namespace specs: names, repositories, restricted, join rule."""
    specs: list[dict[str, Any]] = []
    seen: set[str] = set()
    for spec in input.namespaces:
        ns = str(spec.namespace).strip()
        if not _NS_RE.match(ns):
            raise ValidationError(
                f"{ns!r} is not a tracker namespace name "
                "(tracker-<team>, lower-case letters, digits, -)",
                code="BAD_NAMESPACE",
            )
        if ns in seen:
            raise ValidationError(f"{ns} is named twice", code="BAD_NAMESPACE")
        seen.add(ns)
        repos = sorted({str(r).strip() for r in spec.repos if str(r).strip()})
        bad = [r for r in repos if not _REPO_RE.match(r)]
        if bad:
            raise ValidationError(f"repositories must be Org/repo, not {bad[0]!r}", code="BAD_REPO")
        specs.append(
            {
                "namespace": ns,
                "repos": repos,
                "restricted": bool(spec.restricted),
                "join_policy": str(spec.join_policy or ""),
            }
        )
    if not specs:
        raise ValidationError("at least one namespace must read the source", code="BAD_NAMESPACE")
    return specs


async def _namespace_taken(org: str, namespace: str) -> bool:
    from server.auth.context import current_caller
    from server.auth.default_namespace import auth_off_subject
    from server.identity.app.registration import namespace_claimable
    from server.tracker.domain import sources

    if namespace in sources.namespace_entries():
        return True
    subject = current_caller()[0] or auth_off_subject()
    return not await namespace_claimable(org, namespace, subject)


async def _probe_or_refuse(
    *,
    host: str,
    port: int,
    secure: bool,
    database: str,
    user: str,
    password: str,
    name: str,
    refuse_contract: bool = True,
    mapping: Any | None = None,
) -> Any:
    """The three mechanical gates: public host, read-only credential, the record contract — or,
    with a `mapping`, the mapping over the team's own tables. Returns (probe, report); raises a
    ValidationError a person can act on. With `refuse_contract=False` (a dry run) a schema that
    fails the contract comes back as the report — the one refusal a form shows table by table
    rather than as a sentence."""
    from server.tracker.app import onboarding

    why = await onboarding.host_refusal(host)
    if why:
        raise ValidationError(why, code="HOST_REFUSED")
    try:
        probe = await onboarding.probe_credential(
            host=host,
            port=port,
            secure=secure,
            database=database,
            user=user,
            password=password,
            mapping=mapping,
        )
    except onboarding.HostRefused as e:  # The name changed its answer since the check above
        raise ValidationError(str(e), code="HOST_REFUSED") from None
    except onboarding.ReadonlyModeError as e:  # Reachable, but unusable
        raise ValidationError(str(e), code="CREDENTIAL_READONLY_MODE") from None
    except onboarding.ProbeError as e:
        raise ValidationError(str(e), code="SOURCE_UNREACHABLE") from None
    if not probe.grants.read_only:
        raise ValidationError(
            f"{user!r} holds write access ({', '.join(probe.grants.write_types[:4])}); loom "
            "takes a read-only user — SELECT on the contract views and system.columns only",
            code="CREDENTIAL_NOT_READONLY",
        )
    if mapping is not None:
        mapped = _mapped_report_or_refuse(name, database, user, mapping, probe, refuse_contract)
        return probe, mapped
    report = record_contract.diff(name, database, probe.described)
    if not report.ok and refuse_contract:
        raise ValidationError(
            f"{database} does not meet the record contract: {report.summary()} "
            "(docs/features/tracker/record-contract.md)",
            code="CONTRACT_FAILED",
        )
    # The columns are right but a row cannot be read through
    # the view with this credential — every read would fail after "Ready"
    blocked = {t: why for t, why in probe.unreadable.items() if t in record_contract.REQUIRED}
    if report.ok and blocked:
        denied = [t for t, why in blocked.items() if why.startswith("access denied")]
        fix = (
            f"recreate the views as `CREATE OR REPLACE VIEW {database}.<table> DEFINER = "
            f"CURRENT_USER SQL SECURITY DEFINER AS …` so {user!r} needs SELECT on the views "
            "only (a plain view reads with the reader's own privileges)"
            if denied
            else "check the views select without error"
        )
        raise ValidationError(
            f"{user!r} sees the views but cannot read a row through "
            + ", ".join(f"{t} ({why})" for t, why in sorted(blocked.items()))
            + f": {fix}",
            code="VIEWS_NOT_READABLE",
        )
    return probe, report


def _mapped_report_or_refuse(
    name: str, database: str, user: str, mapping: Any, probe: Any, refuse_contract: bool
) -> Any:
    """The mapping checked over the tables it names, and one
    row read through each mapped projection with the credential."""

    report = record_contract.diff_mapped(name, database, mapping, probe.described)
    if not report.ok and refuse_contract:
        raise ValidationError(
            f"the mapping does not fit {database}: {report.summary()}", code="MAPPING_FAILED"
        )
    if report.ok and probe.unreadable:
        denied = sorted(
            {
                mapping.tables[t].source
                for t, why in probe.unreadable.items()
                if why.startswith("access denied") and t in mapping.tables
            }
        )
        fix = (
            "grant it SELECT on " + ", ".join(denied)
            if denied
            else "check each mapped column reads as the type loom expects"
        )
        raise ValidationError(
            f"{user!r} cannot read a row through "
            + ", ".join(f"{t} ({why})" for t, why in sorted(probe.unreadable.items()))
            + f": {fix}",
            code="VIEWS_NOT_READABLE",
        )
    return report


async def _make_admin(org: str, namespace: str, subject: str) -> None:
    from server.identity.app.registration import grant_self_admin

    await grant_self_admin(org, namespace, subject)


async def _register_namespaces(
    org: str, specs: list[dict[str, Any]], subject: str
) -> list[dict[str, Any]]:
    from server.tracker.app.registry import ensure_tracker_registered

    out: list[dict[str, Any]] = []
    for spec in specs:
        classification = "restricted" if spec["restricted"] else "standard"
        join_policy = "approval" if spec["restricted"] else (spec["join_policy"] or "open")
        await ensure_tracker_registered(
            org, spec["namespace"], classification=classification, join_policy=join_policy
        )
        if subject:
            await _make_admin(org, spec["namespace"], subject)
        out.append(
            {
                "namespace": spec["namespace"],
                "repos": spec["repos"],
                "classification": classification,
                "join_policy": join_policy,
            }
        )
    return out


async def _admissible(input: Any) -> tuple[str, list[dict[str, Any]]]:
    """The checks of `tracker.source.add` that read only loom: self-service,
    org, name, namespaces, no clash, the quota. Returns (name, specs)."""
    from server.settings import settings
    from server.tracker.app import registry_sources
    from server.tracker.domain import sources

    _require_self_service()
    _require_same_org(input.org)
    if not settings.tracker_org:
        raise ValidationError(
            "this deployment serves no tracker org (LOOM_TRACKER_ORG is unset)",
            code="BAD_ORG",
        )
    if str(input.org) != str(settings.tracker_org):
        raise ValidationError(
            f"this deployment serves tracker sources for org {settings.tracker_org!r}",
            code="BAD_ORG",
        )
    name = str(input.name).strip().lower()
    if not _NAME_RE.match(name):
        raise ValidationError(
            "source name: 2–32 lower-case letters, digits, - or _, starting with a letter",
            code="BAD_NAME",
        )
    specs = _namespace_specs(input)
    await registry_sources.refresh_db_sources(force=True)
    if name in sources.endpoints() or registry_sources.find(input.org, name) is not None:
        raise ValidationError(
            f"a tracker source named {name!r} already exists", code="SOURCE_EXISTS"
        )
    for spec in specs:
        if await _namespace_taken(input.org, spec["namespace"]):
            raise ValidationError(
                f"{spec['namespace']} already exists; join it instead", code="NAMESPACE_TAKEN"
            )
    cap = int(settings.tracker_self_service_max_sources)
    if await registry_sources.active_count(input.org) >= cap:
        raise ValidationError(
            f"this org already holds {cap} self-service tracker sources; retire one or ask the "
            "operator to raise LOOM_TRACKER_SELF_SERVICE_MAX_SOURCES",
            code="SOURCE_QUOTA",
        )
    return name, specs


def _planned_namespaces(specs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """What `_register_namespaces` would write, said without writing it."""
    out: list[dict[str, Any]] = []
    for spec in specs:
        restricted = bool(spec["restricted"])
        out.append(
            {
                "namespace": spec["namespace"],
                "repos": spec["repos"],
                "classification": "restricted" if restricted else "standard",
                "join_policy": "approval" if restricted else (spec["join_policy"] or "open"),
            }
        )
    return out


async def handle_tracker_source_add(input: TrackerSourceAddInput) -> TrackerSourceAddOutput:
    from server.tracker.app import registry_sources
    from server.tracker.domain import sources

    name, specs = await _admissible(input)
    host = sources.host_of(str(input.host))
    dry_run = bool(input.dry_run)
    mp = _parsed_mapping(input.mapping)
    probe, report = await _probe_or_refuse(
        host=host,
        port=int(input.port),
        secure=bool(input.secure),
        database=str(input.database),
        user=str(input.user),
        password=str(input.password),
        name=name,
        refuse_contract=not dry_run,
        mapping=mp,
    )
    subject, _ = current_caller()
    if dry_run:
        # Every gate ran, nothing was written; a failing schema
        # comes back with the views that would meet the contract
        return TrackerSourceAddOutput(
            name=name,
            host=host,
            database=str(input.database),
            namespaces=_planned_namespaces(specs),
            contract=report.to_dict(),
            roles=list(probe.grants.roles),
            my_role="admin" if subject else "",
            dry_run=True,
            written=False,
            view_templates=record_contract.view_templates(report) if mp is None else {},
        )
    src = sources.DbSource(
        org=input.org,
        name=name,
        host=host,
        port=int(input.port),
        secure=bool(input.secure),
        database=str(input.database),
        user=str(input.user),
        password=str(input.password),
        namespaces={
            s["namespace"]: {
                "repos": s["repos"],
                "restricted": s["restricted"],
                "join_policy": s["join_policy"],
            }
            for s in specs
        },
        created_by=subject,
        mapping_json=mp.to_json() if mp is not None else "",
    )
    await registry_sources.insert_source(src)
    await registry_sources.refresh_db_sources(force=True)
    contract_reports.remember(name, report)
    registered = await _register_namespaces(input.org, specs, subject)
    return TrackerSourceAddOutput(
        name=name,
        host=host,
        database=str(input.database),
        namespaces=registered,
        contract=report.to_dict(),
        roles=list(probe.grants.roles),
        my_role="admin" if subject else "",
    )


def _parsed_mapping(raw: Any) -> Any:
    """A submitted mapping, parsed; None for none."""
    from server.tracker.domain import mapping

    try:
        return mapping.parse(raw)
    except (mapping.MappingError, ValueError, TypeError) as e:
        raise ValidationError(f"mapping: {e}", code="BAD_MAPPING") from None


async def handle_tracker_source_inspect(
    input: TrackerSourceInspectInput,
) -> TrackerSourceInspectOutput:
    """Connect with the read-only credential and say what the
    database holds — its tables and columns — and a first mapping onto loom's
    logical model. Writes nothing; the same host and credential gates as add."""
    from server.tracker.app import onboarding
    from server.tracker.domain import automap, sources

    _require_self_service()
    _require_same_org(input.org)
    if str(input.name or "").strip():
        src = await _authority(input.org, str(input.name).strip().lower())
        host, port, secure, database = src.host, src.port, src.secure, src.database
        user, password = src.user, src.password
    else:
        if not all(str(v).strip() for v in (input.host, input.database, input.user)):
            raise ValidationError("host, database and user are required", code="BAD_INPUT")
        host, port, secure = sources.host_of(str(input.host)), int(input.port), bool(input.secure)
        database, user, password = str(input.database), str(input.user), str(input.password)
    why = await onboarding.host_refusal(host)
    if why:
        raise ValidationError(why, code="HOST_REFUSED")
    try:
        probe = await onboarding.probe_credential(
            host=host,
            port=port,
            secure=secure,
            database=database,
            user=user,
            password=password,
            inspect=True,
        )
    except onboarding.HostRefused as e:
        raise ValidationError(str(e), code="HOST_REFUSED") from None
    except onboarding.ReadonlyModeError as e:
        raise ValidationError(str(e), code="CREDENTIAL_READONLY_MODE") from None
    except onboarding.ProbeError as e:
        raise ValidationError(str(e), code="SOURCE_UNREACHABLE") from None
    if not probe.grants.read_only:
        raise ValidationError(
            f"{user!r} holds write access ({', '.join(probe.grants.write_types[:4])}); "
            "loom takes a read-only user",
            code="CREDENTIAL_NOT_READONLY",
        )
    repos = sorted({str(r).strip() for r in (input.repos or []) if str(r).strip()})
    proposal = automap.propose(probe.tables, database=database, repos=repos)
    return TrackerSourceInspectOutput(
        host=host,
        database=database,
        tables=probe.tables,
        roles=list(probe.grants.roles),
        proposal=proposal,
    )


async def handle_tracker_source_set_mapping(
    input: TrackerSourceSetMappingInput,
) -> TrackerSourceSetMappingOutput:
    """Change a registered tracker's mapping — the same
    checks as add (mapping over the live tables, a row read through each),
    then a new version of its registry row. `mapping: null` returns it to
    the views case."""
    from server.tracker.app import registry_sources

    _require_self_service()
    name = str(input.name).strip().lower()
    src = await _authority(input.org, name)
    mp = _parsed_mapping(input.mapping)
    dry_run = bool(input.dry_run)
    _probe, report = await _probe_or_refuse(
        host=src.host,
        port=src.port,
        secure=src.secure,
        database=src.database,
        user=src.user,
        password=src.password,
        name=name,
        refuse_contract=not dry_run,
        mapping=mp,
    )
    if not dry_run:
        await registry_sources.insert_source(
            dataclasses.replace(src, mapping_json=mp.to_json() if mp is not None else "")
        )
        await registry_sources.refresh_db_sources(force=True)
        contract_reports.remember(name, report)
    return TrackerSourceSetMappingOutput(
        name=name, contract=report.to_dict(), dry_run=dry_run, written=not dry_run
    )


async def _authority(org: str, name: str) -> Any:
    """The registry row, once the caller is its registrant or an admin of one
    of its namespaces. A source the operator's JSON owns is retired by
    editing the deployment, not from the page."""
    from server.core.ch import get_client
    from server.tracker.app import registry_sources
    from server.tracker.domain import sources

    _require_same_org(org)
    await registry_sources.refresh_db_sources(force=True)
    origin = sources.source_origin(name)
    if origin == "config":
        raise ValidationError(
            f"{name!r} comes from the deployment's configuration; change it there and swap",
            code="SOURCE_FROM_CONFIG",
        )
    src = registry_sources.find(org, name)
    if src is None:
        raise ValidationError(f"{name!r} is not a registered tracker source", code="NOT_A_SOURCE")
    subject, _ = current_caller()
    if not subject:
        return src  # auth off: dev mode
    if subject == src.created_by:
        return src
    from server.auth.aliases import subject_aliases

    subs = await subject_aliases(org, subject)
    row = await get_client().query_one(
        """
        SELECT 1 AS one FROM loom.namespace_grants FINAL
        WHERE org = {org:String} AND subject IN {subs:Array(String)} AND role = 'admin'
          AND status = 'active' AND revoked_at IS NULL
          AND namespace IN {names:Array(String)}
        LIMIT 1
        """,
        params={"org": org, "sub": subject, "subs": subs, "names": sorted(src.namespaces)},
    )
    if row is None:
        raise AuthInsufficientError(
            f"principal {subject!r} does not administer tracker source {name!r}"
        )
    return src


_DB_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")


async def handle_tracker_contract(input: TrackerContractInput) -> TrackerContractOutput:
    """The contract before a connection — the tables, their
    columns with what each holds, and a blank view per table."""

    _require_same_org(input.org)
    database = str(input.database or "").strip()
    out = record_contract.catalogue(database if _DB_RE.match(database) else "<database>")
    return TrackerContractOutput(**out)


async def handle_tracker_source_check(input: TrackerSourceCheckInput) -> TrackerSourceCheckOutput:
    from server.settings import settings
    from server.tracker.app import registry_sources
    from server.tracker.domain import sources

    _require_same_org(input.org)
    if not _serves_trackers(input.org):
        raise ValidationError(
            "this deployment serves no tracker sources for this org", code="BAD_ORG"
        )
    await registry_sources.refresh_db_sources()
    name = str(input.name).strip().lower()
    ep = sources.endpoint(name)
    if ep is None or not sources.credential_configured(name):
        raise ValidationError(
            f"{name!r} is not a configured tracker source (endpoint or credential missing)",
            code="NOT_A_SOURCE",
        )
    try:
        rep = await asyncio.wait_for(
            contract.check(name, ep.database), timeout=settings.tracker_source_check_timeout_s
        )
    except Exception as e:  # noqa: BLE001 — the source did not answer
        return TrackerSourceCheckOutput(
            name=name,
            database=ep.database,
            reachable=False,
            contract={"ok": None, "summary": f"unreachable: {e}"[:300]},
        )
    return TrackerSourceCheckOutput(
        name=name,
        database=ep.database,
        reachable=True,
        contract=rep.to_dict(),
        view_templates=record_contract.view_templates(rep),
    )


async def handle_tracker_source_rotate(
    input: TrackerSourceRotateInput,
) -> TrackerSourceRotateOutput:
    from datetime import UTC, datetime

    from server.tracker.app import registry_sources
    from server.tracker.store.source import qa_intel

    _require_self_service()
    name = str(input.name).strip().lower()
    src = await _authority(input.org, name)
    _probe, report = await _probe_or_refuse(
        host=src.host,
        port=src.port,
        secure=src.secure,
        database=src.database,
        user=str(input.user),
        password=str(input.password),
        name=name,
        # a mapped source is checked through its own mapping, as add did
        mapping=_parsed_mapping(src.mapping_json) if src.mapping_json else None,
    )
    # Only the credential changes: the new version row carries every other
    # column (the mapping above all) forward
    rotated = dataclasses.replace(src, user=str(input.user), password=str(input.password))
    await registry_sources.insert_source(rotated)
    await registry_sources.refresh_db_sources(force=True)
    await qa_intel.forget(name)
    contract_reports.remember(name, report)
    return TrackerSourceRotateOutput(
        name=name,
        contract=report.to_dict(),
        rotated_at=datetime.now(UTC).replace(microsecond=0).isoformat(),
    )


def source_add_audit_subject(input: TrackerSourceAddInput, response: Any) -> dict[str, Any]:
    """What an add's audit row records: the request without the password, with
    the name and host as stored. `loom.audit_log` outlives the source and must
    hold no secret."""
    subject = input.model_dump(mode="json", exclude={"password"})
    subject["name"] = response["name"]
    subject["host"] = response["host"]
    return subject


def source_rotate_audit_subject(input: TrackerSourceRotateInput, response: Any) -> dict[str, Any]:
    """What a rotation's audit row records: which source and the new user, never
    the password."""
    return {"org": input.org, "name": response["name"], "user": input.user}


async def handle_tracker_source_remove(
    input: TrackerSourceRemoveInput,
) -> TrackerSourceRemoveOutput:
    from server.identity.api.models import NamespaceDeleteInput
    from server.tracker.app import registry_sources
    from server.tracker.store.source import qa_intel

    _require_self_service()
    name = str(input.name).strip().lower()
    if str(input.confirm).strip().lower() != name:
        raise ValidationError(
            f"type the source name ({name}) in `confirm` to retire it", code="CONFIRM_REQUIRED"
        )
    src = await _authority(input.org, name)
    deactivated = await registry_sources.deactivate_source(input.org, name)
    await registry_sources.refresh_db_sources(force=True)
    await qa_intel.forget(name)
    retired: list[str] = []
    from server.identity.app.namespace import handle_namespace_delete

    for ns in sorted(src.namespaces):
        try:
            out = await handle_namespace_delete(NamespaceDeleteInput(org=input.org, namespace=ns))
            if getattr(out, "deleted", False):
                retired.append(ns)
        except Exception as e:  # noqa: BLE001 — the source is retired either way
            logger.warning(
                "tracker.source.remove: namespace %s/%s not retired: %s", input.org, ns, e
            )
    return TrackerSourceRemoveOutput(name=name, deactivated=deactivated, namespaces_retired=retired)


__all__ = [
    "handle_tracker_contract",
    "handle_tracker_source_inspect",
    "handle_tracker_source_set_mapping",
    "handle_tracker_source_add",
    "handle_tracker_source_check",
    "handle_tracker_source_list",
    "handle_tracker_source_remove",
    "handle_tracker_source_rotate",
]
