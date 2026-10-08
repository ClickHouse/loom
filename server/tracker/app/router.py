"""The QA answer router: run a catalogue plan against one
tracker namespace and return sections a reader or a bot can use — each with the
template that produced it, the parameters it ran with, its rows, and its
citations — plus the plan itself, so the answer always says what was
searched.

A template whose access class the namespace cannot serve (a restricted
read through a standard namespace) is reported as `restricted_required`,
not silently skipped: the client learns that the answer exists behind the
restricted namespace. A template the source could not read is
`tracker_unavailable`. Absence of rows is `empty` — unknown, never "nothing
exists".
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from server.tracker.app import source as tsource
from server.tracker.domain import catalogue as cat
from server.tracker.domain.errors import TrackerUnavailable
from server.tracker.store.source import TrackerSource

logger = logging.getLogger("loom.tracker.router")

_KNOWN_COMPONENTS: tuple[str, ...] = (
    # The tracker source's own `primary_component` vocabulary, most frequent first
    # (read from the live source); a question names one of these and the route is set.
    # "Core" is left out: "core incidents" is the source's own name for a table
    "DataPlane",
    "Fuzzer",
    "ControlPlane",
    "Analyzer",
    "ClickPipes",
    "Ops",
    "MergeTree",
    "Joins",
    "Functions",
    "Documentation",
    "Infrastructure",
    "Formats",
    "DataLake",
    "Optimizer",
    "Mutations",
    "S3",
    "Distributed",
    "Backup",
    "SMT",
    "QueryExecution",
    "RBAC",
    "Keeper",
    "AggregateFunctions",
    "BuildSystem",
    "DataTypes",
    "MessageQueues",
    "Dictionary",
    "Interpreter",
    "ParallelReplicas",
    "Replication",
    "Protocols",
    "SkipIndex",
    "DateTime",
    "SQL",
    "Databases",
    "Projections",
    "MaterializedView",
    "DDL",
    "SystemTables",
    "SharedCatalog",
    "JSON",
    "CI",
    "TTL",
    "FullTextSearch",
    "CLI",
    "MultiDisk",
    "ForeignDB",
    "PeerDB",
    "TableFunctions",
    "Logging",
    "Aggregation",
    "Compression",
    "PostgreSQL",
    "Settings",
    "FilesystemCache",
    "S3Queue",
    "DistributedCache",
    "RefreshableMV",
)


@dataclass
class Section:
    template: str
    title: str
    access: str
    params: dict[str, Any]
    why: str
    status: str = "served"  # served | empty | restricted_required | tracker_unavailable
    rows: Any = None
    cites: str = ""
    ms: float = 0.0  # wall time of the read; 0 when nothing was read

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "template": self.template,
            "title": self.title,
            "access": self.access,
            "params": {k: v for k, v in self.params.items() if v not in ("", None, [], 0, False)},
            "why": self.why,
            "status": self.status,
            "cites": self.cites,
            "ms": self.ms,
        }
        if self.status == "served":
            out["rows"] = self.rows
        return out


@dataclass
class TrackerAnswer:
    intent: str
    anchors: dict[str, Any]
    sections: list[Section] = field(default_factory=list)
    namespace: str = ""
    restricted: bool = False

    @property
    def served(self) -> list[Section]:
        return [s for s in self.sections if s.status == "served"]

    def as_dict(self) -> dict[str, Any]:
        return {
            "intent": self.intent,
            "anchors": self.anchors,
            "namespace": self.namespace,
            "restricted": self.restricted,
            "sections": [s.as_dict() for s in self.sections],
            "searched": [searched(s) for s in self.sections],
        }


def searched(s: Section) -> str:
    args = ", ".join(f"{k}={v!r}" for k, v in s.params.items() if v not in ("", None, [], 0, False))
    return f"{s.template}({args})"


def make_plan(question: str, *, limit: int = 10) -> cat.Plan:
    return cat.plan(question, known_components=_KNOWN_COMPONENTS, default_limit=limit)


def _empty(rows: Any) -> bool:
    if rows is None:
        return True
    if isinstance(rows, dict):
        return not any(v for v in rows.values() if v not in ("", None, [], {}, 0))
    return len(rows) == 0


async def _run_step(
    source: TrackerSource, org: str, ns: str, step: cat.Step, restricted: bool
) -> Section:
    t = cat.TEMPLATES[step.template]
    sec = Section(
        template=t.name,
        title=_title(t, step.params),
        access=t.access,
        params=dict(step.params),
        why=step.why,
        cites=t.cites,
    )
    if t.access == cat.RESTRICTED and not restricted:
        sec.status = "restricted_required"
        _record(sec, 0.0, restricted)
        return sec
    started = time.monotonic()
    try:
        rows = await _call_reader(source, org, ns, t, step)
    except TrackerUnavailable as e:
        logger.info("tracker.answer %s unavailable: %s", t.name, e)
        sec.status = "tracker_unavailable"
        sec.ms = round((time.monotonic() - started) * 1000.0, 1)
        _record(sec, sec.ms, restricted)
        return sec
    except TypeError as e:  # a template / reader signature mismatch is a bug, not an answer
        logger.warning("tracker.answer %s: bad call (%s)", t.name, e)
        sec.status = "tracker_unavailable"
        sec.ms = round((time.monotonic() - started) * 1000.0, 1)
        _record(sec, sec.ms, restricted)
        return sec
    sec.rows = _jsonable(rows)
    sec.status = "empty" if _empty(rows) else "served"
    sec.ms = round((time.monotonic() - started) * 1000.0, 1)
    _record(sec, sec.ms, restricted)
    return sec


def _title(t: cat.Template, params: dict[str, Any]) -> str:
    """The section's heading: the template's, or for a cell of the board
    the severities and component it lists — "Open medium
    issues in MergeTree"."""
    sevs = [str(x) for x in (params.get("severities") or []) if x]
    if t.name == "area_open_issues" and sevs:
        comp = str(params.get("component") or "") or "this component"
        return f"Open {' / '.join(sevs)} issues in {comp}"
    return t.title


_ITEM_KEYS = frozenset(
    {
        "repo",
        "is_pr",
        "state",
        "title",
        "author",
        "primary_component",
        "severity_level",
        "url",
        "opened_at",
        "closed_at",
        "bisect_outcome",
        "reproduces_on",
        "has_test",
        "ci_result",
        "minor_version",
    }
)


async def _call_reader(
    source: TrackerSource, org: str, ns: str, t: cat.Template, step: cat.Step
) -> Any:
    """One template's reader call: the keyed readers take their key
    positionally, the board takes no `limit` (every cell),
    every other template passes its parameters by name."""
    reader = getattr(source, t.reader)
    p = step.params
    lim = p.get("limit", 10)
    if t.name == "item":
        rows = await reader(org, ns, p.get("numbers") or [])
        return [
            {"number": n, **{k: v for k, v in r.items() if k in _ITEM_KEYS}}
            for n, r in sorted(rows.items())
        ]
    if t.name == "flaky":
        return await reader(org, ns, p["test_name"], limit=lim)
    if t.name == "component_risk":
        rows = await reader(org, ns, p.get("components") or [])
        return [
            {"component": k, **{kk: _jsonable(vv) for kk, vv in v.items() if kk != "component"}}
            for k, v in rows.items()
        ]
    if t.name == "area_open_issues":
        # The board's cell — its severities ride along and the
        # catalogue set the limit to the cell ceiling, not the request's
        sevs = [str(x) for x in (p.get("severities") or []) if x]
        if sevs:
            return await reader(org, ns, p["component"], limit=lim, severities=sevs)
        return await reader(org, ns, p["component"], limit=lim)
    if t.name == "severity_board":
        return await reader(
            org, ns, components=list(p.get("components") or []), days=int(p.get("days") or 0)
        )
    kwargs = {k: v for k, v in p.items() if k != "limit"}
    return await reader(org, ns, limit=lim, **kwargs)


def _record(sec: Section, ms: float, restricted: bool) -> None:
    """One section's outcome to the QA telemetry; never raises."""
    try:
        from server.observability.metrics import record_tracker_section

        record_tracker_section(
            template=sec.template, status=sec.status, ms=ms, restricted=restricted
        )
    except Exception:  # noqa: BLE001 — telemetry is never the answer's problem
        pass


def _jsonable(v: Any) -> Any:
    if hasattr(v, "isoformat"):
        return v.isoformat()
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    return v


async def answer(
    question: str,
    *,
    org: str,
    tracker_namespace: str,
    limit: int = 10,
    explain_only: bool = False,
    anchors: dict[str, Any] | None = None,
) -> TrackerAnswer:
    """Plan, then run the plan's steps together against the tracker namespace.
    `anchors` are structured anchors a client already holds
    and merge with the prose; `explain_only` returns the plan with every
    section `planned` and no read."""
    if anchors:
        p = cat.plan_from_anchors(
            anchors, question=question, known_components=_KNOWN_COMPONENTS, default_limit=limit
        )
    else:
        p = make_plan(question, limit=limit)
    out = TrackerAnswer(intent=p.intent, anchors=p.anchors.as_dict(), namespace=tracker_namespace)
    if not p.steps:
        return out
    source = tsource.tracker_source(org, tracker_namespace)
    out.restricted = bool(source.cfg and source.cfg.restricted)
    if explain_only:
        for st in p.steps:
            t = cat.TEMPLATES[st.template]
            out.sections.append(
                Section(
                    template=t.name,
                    title=t.title,
                    access=t.access,
                    params=dict(st.params),
                    why=st.why,
                    status="planned",
                    cites=t.cites,
                )
            )
        return out
    out.sections = list(
        await asyncio.gather(
            *(_run_step(source, org, tracker_namespace, st, out.restricted) for st in p.steps)
        )
    )
    return out


# --- rendering for memory.ask (one line per fact, citations inline) -------------


def _d(v: Any) -> str:
    return str(v or "")[:10]


def render_lines(ans: TrackerAnswer, *, max_rows: int = 5) -> list[str]:
    """Prose lines a record answer can carry: a head per served section and
    up to `max_rows` cited rows; a `restricted_required` section is one line
    naming what exists behind the restricted namespace."""
    lines: list[str] = []
    for s in ans.sections:
        if s.status == "restricted_required":
            lines.append(
                f"{s.title}: available through the restricted tracker namespace only "
                f"(template {s.template})."
            )
            continue
        if s.status != "served":
            continue
        rows = s.rows
        if isinstance(rows, dict):
            lines.extend(_render_dict(s, rows, max_rows))
            continue
        if s.template == "severity_board":
            lines.extend(_render_board(s, rows, max_rows))
            continue
        lines.append(f"{s.title} ({len(rows)} shown; searched {s.template}):")
        for r in rows[:max_rows]:
            lines.append("- " + _row_line(s.template, r))
    return lines


def _row_line(template: str, r: dict[str, Any]) -> str:
    if template in ("area_incidents", "area_escalations", "similar_bugs", "area_open_issues"):
        bits = [str(r.get("source_id") or r.get("number") or "")]
        if r.get("severity") or r.get("severity_level"):
            bits.append(str(r.get("severity") or r.get("severity_level")))
        if r.get("error_type"):
            bits.append(str(r["error_type"]))
        if r.get("org_name"):
            bits.append(f"customer {r['org_name']}")
        state = "open" if r.get("open") or (r.get("state") == "open") else "closed"
        tail = " — no regression test" if r.get("has_test") is False else ""
        if r.get("partial"):
            # a most-tokens match: say how near it is
            tail += f" — partial match, {r.get('matched_tokens')} of {r.get('of_tokens')} terms"
        when = _d(r.get("created_at") or r.get("opened_at"))
        return f"{' · '.join(b for b in bits if b)} ({state}, {when}): {r.get('title', '')}{tail}"
    if template == "customers_in_area":
        return (
            f"{r.get('org_name')}: {r.get('cases')} case(s), {r.get('critical')} critical, "
            f"{r.get('open')} open, last {_d(r.get('last_case'))}; "
            f"e.g. {', '.join(r.get('examples') or [])}"
        )
    if template == "release_blockers":
        fixes = r.get("merged_fixes") or []
        rel = ", ".join(r.get("fix_releases") or [])
        return (
            f"{r.get('source_id')} [{r.get('severity') or 'unrated'}] {r.get('title')} — assignees "
            f"{', '.join(r.get('assignees') or ['none'])}; "
            + (
                f"fixed by {', '.join(fixes)} (first release {rel or 'pending'})"
                if fixes
                else f"no merged fix ({len(r.get('open_fixes') or [])} open)"
            )
        )
    if template == "changelog":
        return f"#{r.get('pr_number')} [{r.get('category')}] {r.get('entry') or r.get('title')}"
    if template == "prs_touching":
        return (
            f"#{r.get('pr_number')} ({_d(r.get('merged_at'))}, {r.get('author')}): "
            f"{r.get('title')} — "
            f"{', '.join(r.get('touched') or [])}"
        )
    if template == "test_gaps":
        return (
            f"{r.get('component')}: gap {float(r.get('gap_ratio') or 0):.2f} "
            f"({r.get('bugs_without_test')} of {r.get('total_bugs')} bugs untested), "
            f"risk {float(r.get('risk_score') or 0):.1f}, "
            f"trend {float(r.get('trend_delta') or 0):+.2f}"
        )
    if template == "component_risk":
        return (
            f"{r.get('component')}: {r.get('total_bugs')} bugs, {r.get('open_bugs')} open, "
            f"gap {float(r.get('gap_ratio') or 0):.2f}, risk {float(r.get('risk_score') or 0):.1f}"
        )
    if template == "slack_threads":
        return (
            f"{r.get('channel')} ({_d(r.get('created_at'))}, {r.get('replies')} replies): "
            f"{r.get('title') or r.get('excerpt', '')[:100]}"
        )
    if template == "version_limits":
        return (
            f"cap {r.get('max') or '?'}: {r.get('instances')} instance(s) "
            f"in {r.get('orgs')} org(s) — "
            f"{r.get('reason') or 'no reason recorded'}"
        )
    if template == "duplicates":
        return (
            f"{r.get('rel_type')} {r.get('direction')} {r.get('source_id')} "
            f"({'open' if r.get('open') else 'closed'}"
            f"{', ' + r['state_reason'] if r.get('state_reason') else ''}): {r.get('title')}"
        )
    if template == "flaky":
        return f"#{r.get('number')} ({r.get('state')}, {_d(r.get('opened_at'))}): {r.get('title')}"
    if template == "item":
        kind = "PR" if r.get("is_pr") else "issue"
        return f"#{r.get('number')} {kind} ({r.get('state')}): {r.get('title')}"
    return ", ".join(f"{k}={v}" for k, v in list(r.items())[:6])


def _render_board(s: Section, rows: list[dict[str, Any]], max_rows: int) -> list[str]:
    """The bug board as prose: one line per component, the
    cells in severity order, the components with the most open issues
    first — "MergeTree: 1 critical · 27 medium · 206 low; 200 untested"."""
    by: dict[str, dict[str, Any]] = {}
    for r in rows:
        c = by.setdefault(
            str(r.get("component") or "(no component)"),
            {"cells": [], "open": 0, "untested": 0, "flaky": 0, "bisected": 0},
        )
        c["cells"].append(f"{int(r.get('open') or 0)} {r.get('severity') or 'unrated'}")
        for k in ("open", "untested", "flaky", "bisected"):
            c[k] += int(r.get(k) or 0)
    total = sum(c["open"] for c in by.values())
    lines = [f"{s.title} ({len(by)} components, {total} open; searched {s.template}):"]
    for name, c in sorted(by.items(), key=lambda kv: (-kv[1]["open"], kv[0]))[:max_rows]:
        tail = f"{c['untested']} untested"
        if c["flaky"]:
            tail += f", {c['flaky']} flaky"
        if c["bisected"]:
            tail += f", {c['bisected']} bisected"
        lines.append(f"- {name}: {' · '.join(c['cells'])}; {tail}")
    return lines


def _render_dict(s: Section, d: dict[str, Any], max_rows: int) -> list[str]:
    lines: list[str] = []
    if s.template in ("escalation_context", "incident_context"):
        doc = d.get("doc") or {}
        lines.append(
            f"{s.title}: {doc.get('source_id')} [{doc.get('severity') or 'unrated'}, "
            f"{doc.get('component') or 'no component'}] {doc.get('title')} "
            f"({'open' if not doc.get('closed_at') else 'closed ' + _d(doc.get('closed_at'))})."
        )
        rca = d.get("rca")
        if rca:
            # the recorded root cause comes before the graph
            bits = [f"root cause ({rca.get('confidence')}): {rca.get('notes') or 'no notes'}"]
            if rca.get("caused_by_prs"):
                bits.append("caused by " + ", ".join(rca["caused_by_prs"][:4]))
            if rca.get("fix_prs"):
                bits.append("fixed by " + ", ".join(rca["fix_prs"][:4]))
            if rca.get("backport_pending"):
                bits.append("backport pending " + ", ".join(rca["backport_pending"][:6]))
            if rca.get("affected_versions"):
                bits.append("crashed on " + ", ".join(rca["affected_versions"][:4]))
            if rca.get("same_root_cause"):
                bits.append("same root cause as " + ", ".join(rca["same_root_cause"][:4]))
            if rca.get("duplicates_of"):
                bits.append("duplicate of " + ", ".join(rca["duplicates_of"][:3]))
            lines.append("- " + "; ".join(bits))
        life = d.get("lifecycle")
        if life:
            lines.append(
                f"- lifecycle {life.get('incident_id')}: {life.get('status')} / "
                f"{life.get('lifecycle_state')}, urgency {life.get('urgency')}, "
                f"MTTR {int(life.get('mttr_seconds') or 0) // 60} min, "
                f"affected orgs {', '.join(life.get('affected_orgs') or []) or 'none listed'}"
            )
        for link in (d.get("links") or [])[: max_rows * 2]:
            lines.append(
                f"- {link.get('rel_type')} {link.get('direction')} {link.get('kind')} "
                f"{link.get('source_id')}: {link.get('title')}"
            )
        if d.get("instances"):
            lines.append(
                "- instances named: "
                + ", ".join(
                    f"{i.get('spoken_name')} ×{i.get('mentions')}" for i in d["instances"][:6]
                )
            )
        return lines
    if s.template == "customer_state":
        inst = d.get("instances") or {}
        lines.append(
            f"Customer {d.get('org_name')}: {len(d.get('escalations') or [])} escalation(s) "
            f"in the window, "
            f"{inst.get('count', 0)} instance(s) ({inst.get('running', 0)} running; versions "
            f"{', '.join(inst.get('versions') or []) or 'n/a'}; "
            f"tiers {', '.join(inst.get('tiers') or []) or 'n/a'}), "
            f"{len(d.get('incidents') or [])} incident(s) affecting them."
        )
        for r in (d.get("escalations") or [])[:max_rows]:
            lines.append("- " + _row_line("area_escalations", r))
        for r in (d.get("incidents") or [])[:max_rows]:
            lines.append(
                f"- incident {r.get('incident_id')} ({r.get('status')}, "
                f"{_d(r.get('created_at'))}): {r.get('title')}"
            )
        return lines
    if s.template == "version_exposure":
        lines.append(
            f"Release {d.get('version')}: {d.get('instances')} instance(s) "
            f"({d.get('running')} running) "
            f"across {d.get('orgs')} org(s), snapshot {_d(d.get('snapshot'))}."
        )
        for r in (d.get("by_tier") or [])[:max_rows]:
            lines.append(
                f"- {r.get('tier')} / {r.get('cloud')} / {r.get('state')}: {r.get('instances')}"
            )
        for r in (d.get("top_orgs") or [])[:max_rows]:
            lines.append(
                f"- {r.get('org_name')}: {r.get('instances')} instance(s), "
                f"tiers {', '.join(r.get('tiers') or []) or 'n/a'}"
            )
        return lines
    if s.template == "person_activity":
        lines.append(
            f"@{d.get('login')} in the last {d.get('days')} days: "
            f"{len(d.get('merged') or [])} merged, "
            f"{len(d.get('reviewing') or [])} asked to review, "
            f"{len(d.get('opened_issues') or [])} issues opened"
            + (f", {len(d.get('escalated') or [])} escalations" if "escalated" in d else "")
            + "."
        )
        for r in (d.get("merged") or [])[:max_rows]:
            lines.append(
                f"- merged #{r.get('pr_number')} ({_d(r.get('merged_at'))}): {r.get('title')}"
                + (" — no test" if r.get("has_test") is False else "")
            )
        for r in (d.get("reviewing") or [])[:max_rows]:
            lines.append(
                f"- reviewing #{r.get('pr_number')} by {r.get('author')} ({r.get('state')}): "
                f"{r.get('title')}"
            )
        return lines
    lines.append(f"{s.title}: " + ", ".join(f"{k}={v}" for k, v in list(d.items())[:8]))
    return lines


__all__ = ["TrackerAnswer", "Section", "answer", "make_plan", "render_lines"]
