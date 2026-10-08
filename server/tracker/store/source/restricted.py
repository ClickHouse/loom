"""Reads only a restricted namespace serves: core incidents, support
escalations, customers, Slack threads and the cloud instances a release
runs on.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from server.settings import settings
from server.tracker.store.source.base import SourceBase
from server.tracker.store.source.rows import doc_row, str_list, upstream_dt
from server.tracker.store.source.sql import KIND_TABLES, all_tokens, text_match, text_tokens

# `top_function` values that are the crash machinery, not the crashing code
INCIDENT_NOISE_FRAMES: tuple[str, ...] = (
    "src/Common/Exception.cpp",
    "src/Common/SignalHandlers.cpp",
    "src/Common/AllocationInterceptors.cpp",
    "src/Common/StackTrace.cpp",
)

# the incident RCA columns (types verified live: arrays of refs, a
# LowCardinality confidence high | medium | low, free-text notes)
_RCA_COLS = (
    ", rca_confidence, rca_caused_by_prs, rca_fix_prs, rca_backport_pending,"
    " rca_affected_versions, rca_same_root_cause, rca_duplicates_of,"
    " substring(rca_notes, 1, 600) AS rca_notes"
)


def restricted_repo(kind: str) -> str:
    """The `Org/repo` whose numbers name `kind` records ("escalation", "incident"),
    from `tracker_restricted_repos_json`; '' when none is configured."""
    raw = settings.tracker_restricted_repos_json.strip()
    try:
        repos = json.loads(raw) if raw else {}
    except ValueError:
        return ""
    return str(repos.get(kind) or "").strip() if isinstance(repos, dict) else ""


class RestrictedReads(SourceBase):
    """Readers of the restricted tables; each passes `_require_restricted`."""

    def _area_conds(
        self, component: str, path_prefix: str, term: str, path_col: str
    ) -> tuple[list[str], dict[str, Any]]:
        """The OR of the area's predicates, each keyed or index-served:
        component equality, a path prefix on `path_col`, and the text index
        over title / body for the term's tokens (all tokens required)."""
        conds: list[str] = []
        params: dict[str, Any] = {}
        if component:
            conds.append("primary_component = {component:String}")
            params["component"] = component
        if path_prefix and path_col:
            conds.append(f"startsWith({path_col}, {{prefix:String}})")
            params["prefix"] = path_prefix
        toks = text_tokens(term)
        if toks and len(" ".join(toks)) >= 4:
            conds.append(text_match(toks, params))
        return conds, params

    async def incidents_in_area(
        self,
        org: str,
        ns: str,
        *,
        component: str = "",
        path_prefix: str = "",
        term: str = "",
        days: int = 90,
        untested: bool = False,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Core incidents (production crashes / logical errors) in an area:
        by component, by the crashing frame's path prefix (`top_function`
        holds the first src/ path of the stack trace), or by text; newest
        first; `untested` keeps those whose fix carried no test. Restricted."""
        self._require_restricted("incidents")
        conds, params = self._area_conds(component, path_prefix, term, "top_function")
        if not conds:
            return []
        # the area is any of its conditions; the filters below narrow it, so
        # they are AND-ed onto the group, never OR-ed into it
        narrow: list[str] = []
        if path_prefix:
            # 892 of 1,642 incidents (live) name the exception or signal
            # handler as their first src/ frame: a path read must not match them
            narrow.append("top_function NOT IN {noise:Array(String)}")
            params["noise"] = list(INCIDENT_NOISE_FRAMES)
        if untested:
            narrow.append("ifNull(has_test, 0) = 0")
        sql = f"""
            SELECT source_id, url, title, severity_level, error_type, error_name, signal,
                   top_function, primary_component, has_test, created_at, closed_at,
                   time_to_fix_days
            FROM {self.db}.core_incidents FINAL
            WHERE created_at >= now64(3) - INTERVAL {{days:UInt32}} DAY AND ({" OR ".join(conds)})
              {"".join(f" AND {c}" for c in narrow)}
            ORDER BY multiIf(severity_level = 'critical', 0, severity_level = 'major', 1, 2),
                     created_at DESC
            LIMIT {{limit:UInt32}}
        """
        params.update({"days": self._window(days), "limit": max(1, min(int(limit), 50))})
        rows = await self._rows(
            "incidents_area", sql, params, cache_key=params, budget_ms=self._text_budget()
        )
        return [doc_row(r, "core_incident") for r in rows]

    async def escalations_in_area(
        self,
        org: str,
        ns: str,
        *,
        component: str = "",
        path_prefix: str = "",
        term: str = "",
        days: int = 90,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Support escalations in an area — the customer-facing side of a
        feature's history — newest first, most severe first. Escalations carry
        coarse components upstream (DataPlane, Core, ControlPlane: verified
        live), so a server component name is searched as text too.
        Restricted."""
        self._require_restricted("escalations")
        conds, params = self._area_conds(component, "", term or component, "")
        if not conds:
            return []
        sql = f"""
            SELECT source_id, url, title, severity, severity_level, primary_component, org_name,
                   spoken_name, cloud_provider, cloud_region, cloud_tier, version_affected,
                   error_name, has_test, created_at, closed_at, first_response_days
            FROM {self.db}.support_cases FINAL
            WHERE created_at >= now64(3) - INTERVAL {{days:UInt32}} DAY AND ({" OR ".join(conds)})
            ORDER BY multiIf(severity_level = 'critical', 0, severity_level = 'medium', 1, 2),
                     created_at DESC
            LIMIT {{limit:UInt32}}
        """
        params.update({"days": self._window(days), "limit": max(1, min(int(limit), 50))})
        rows = await self._rows(
            "escalations_area", sql, params, cache_key=params, budget_ms=self._text_budget()
        )
        return [doc_row(r, "support_case") for r in rows]

    async def customers_in_area(
        self,
        org: str,
        ns: str,
        *,
        component: str = "",
        path_prefix: str = "",
        term: str = "",
        days: int = 365,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Which customers escalated in this area, how often, how severely,
        how recently — the prioritisation view. Restricted."""
        self._require_restricted("customers")
        conds, params = self._area_conds(component, "", term or component, "")
        if not conds:
            return []
        sql = f"""
            SELECT org_name, count() AS cases,
                   countIf(severity_level = 'critical') AS critical,
                   countIf(closed_at <= toDateTime64(0, 3)) AS open,
                   max(created_at) AS last_case, groupArray(3)(source_id) AS examples,
                   arrayDistinct(groupArray(version_affected)) AS versions
            FROM {self.db}.support_cases FINAL
            WHERE org_name != '' AND created_at >= now64(3) - INTERVAL {{days:UInt32}} DAY
              AND ({" OR ".join(conds)})
            GROUP BY org_name
            ORDER BY critical DESC, cases DESC, last_case DESC
            LIMIT {{limit:UInt32}}
        """
        params.update({"days": self._window(days), "limit": max(1, min(int(limit), 50))})
        rows = await self._rows(
            "customers_area", sql, params, cache_key=params, budget_ms=self._text_budget()
        )
        return [
            {
                "org_name": str(r.get("org_name") or ""),
                "cases": int(r.get("cases") or 0),
                "critical": int(r.get("critical") or 0),
                "open": int(r.get("open") or 0),
                "last_case": upstream_dt(r.get("last_case")),
                "examples": str_list(r.get("examples")),
                "versions": [v for v in str_list(r.get("versions")) if v],
            }
            for r in rows
        ]

    async def escalation_context(
        self, org: str, ns: str, *, number: int, limit: int = 20
    ) -> dict[str, Any]:
        """One escalation, everything the graph links to it (issues, PRs,
        incidents, Slack threads, either direction), and the cloud instances
        it names. Restricted."""
        self._require_restricted("escalation context")
        repo = restricted_repo("escalation")
        if not repo:
            return {}
        return await self._doc_context("support_cases", f"{repo}#{int(number)}", limit)

    async def incident_context(
        self, org: str, ns: str, *, number: int, limit: int = 20
    ) -> dict[str, Any]:
        """One core incident, the PRs that fix it and their backports, its
        incident.io lifecycle row when the number is an INC id. Restricted."""
        self._require_restricted("incident context")
        repo = restricted_repo("incident")
        ctx = (
            await self._doc_context("core_incidents", f"{repo}#{int(number)}", limit)
            if repo
            else {}
        )
        rows = await self._rows(
            "slack_incident",
            f"""SELECT channel_name, incident_id, title, status, lifecycle_state, urgency,
                       created_at, resolved_at, mttr_seconds, affected_orgs, affected_instances,
                       rca_status, url
                FROM {self.db}.slack_incidents FINAL
                WHERE incident_id = {{inc:String}} LIMIT 1""",
            {"inc": f"INC-{int(number)}"},
            cache_key=["inc", number],
            budget_ms=self._text_budget(),
        )
        if rows:
            r = rows[0]
            ctx["lifecycle"] = {
                "incident_id": str(r.get("incident_id") or ""),
                "status": str(r.get("status") or ""),
                "lifecycle_state": str(r.get("lifecycle_state") or ""),
                "urgency": str(r.get("urgency") or ""),
                "created_at": upstream_dt(r.get("created_at")),
                "resolved_at": upstream_dt(r.get("resolved_at")),
                "mttr_seconds": int(r.get("mttr_seconds") or 0),
                "affected_orgs": str_list(r.get("affected_orgs"))[:10],
                "affected_instances": len(str_list(r.get("affected_instances"))),
                "rca_status": str(r.get("rca_status") or ""),
                "url": str(r.get("url") or ""),
            }
        return ctx

    async def _doc_context(self, table: str, source_id: str, limit: int) -> dict[str, Any]:
        from server.tracker.domain.namespaces import doc_id as _doc_id

        # the document's id is derived from its source_id, so
        # the head row, its edges and its instance mentions read side by side
        kind = next(k for k, t in KIND_TABLES.items() if t == table)
        doc_id = _doc_id(kind, source_id)
        # an incident carries its recorded root cause (the RCA columns, about
        # one incident in five with a confidence): read it with the head
        extra = _RCA_COLS if table == "core_incidents" else ""
        head, linked, mentions = await asyncio.gather(
            self._rows(
                "doc_head",
                f"""SELECT id, source_id, url, title, severity_level, primary_component, created_at,
                           closed_at, substring(body, 1, 1200) AS excerpt{extra}
                    FROM {self.db}.{table} FINAL WHERE source_id = {{sid:String}} LIMIT 1""",
                {"sid": source_id},
                cache_key=[table, source_id],
            ),
            self._rows(
                "doc_edges",
                f"""SELECT e.rel_type AS rel_type,
                           if(e.from_id = {{id:UUID}}, 'out', 'in') AS direction,
                           if(e.from_id = {{id:UUID}}, e.to_type, e.from_type) AS other_type,
                           if(e.from_id = {{id:UUID}}, e.to_id, e.from_id) AS other_id
                    FROM {self.db}.edges AS e
                    WHERE e.from_id = {{id:UUID}} OR e.to_id = {{id:UUID}}
                    LIMIT {{limit:UInt32}}""",
                {"id": doc_id, "limit": max(1, min(int(limit) * 3, 120))},
                cache_key=["edges", doc_id],
            ),
            self._rows(
                "doc_instances",
                f"""SELECT spoken_name, instance_id, mention_count
                    FROM {self.db}.rel_instance_mentions
                    WHERE from_id = {{id:UUID}} ORDER BY mention_count DESC LIMIT 10""",
                {"id": doc_id},
                cache_key=["mentions", doc_id],
            ),
        )
        if not head:
            return {}
        doc = head[0]
        ids = sorted({str(e.get("other_id")) for e in linked if e.get("other_id")})[:120]
        kinds = {str(e.get("other_type") or "") for e in linked}
        rows = await self._resolve_ids(
            ids, kinds, tag="doc_resolve", cols="id, source_id, url, title"
        )
        resolved = {str(r["id"]): r for r in rows}
        links = []
        for e in linked:
            other = resolved.get(str(e.get("other_id")))
            if not other:
                continue
            links.append(
                {
                    "rel_type": str(e.get("rel_type") or ""),
                    "direction": str(e.get("direction") or ""),
                    "kind": str(other.get("kind") or e.get("other_type") or ""),
                    "source_id": str(other.get("source_id") or ""),
                    "title": str(other.get("title") or "")[:140],
                    "url": str(other.get("url") or ""),
                }
            )
        links.sort(key=lambda x: (x["rel_type"], x["kind"], x["source_id"]))
        out: dict[str, Any] = {
            "doc": {
                "source_id": str(doc.get("source_id") or ""),
                "url": str(doc.get("url") or ""),
                "title": str(doc.get("title") or ""),
                "severity": str(doc.get("severity_level") or ""),
                "component": str(doc.get("primary_component") or ""),
                "created_at": upstream_dt(doc.get("created_at")),
                "closed_at": upstream_dt(doc.get("closed_at")),
                "excerpt": str(doc.get("excerpt") or ""),
            },
            "links": links[: max(1, int(limit)) * 2],
            "instances": [
                {
                    "spoken_name": str(m.get("spoken_name") or ""),
                    "instance_id": int(m.get("instance_id") or 0),
                    "mentions": int(m.get("mention_count") or 0),
                }
                for m in mentions
            ],
        }
        if str(doc.get("rca_confidence") or ""):
            out["rca"] = {
                "confidence": str(doc.get("rca_confidence") or ""),
                "caused_by_prs": str_list(doc.get("rca_caused_by_prs")),
                "fix_prs": str_list(doc.get("rca_fix_prs")),
                "backport_pending": str_list(doc.get("rca_backport_pending")),
                "affected_versions": str_list(doc.get("rca_affected_versions")),
                "same_root_cause": str_list(doc.get("rca_same_root_cause")),
                "duplicates_of": str_list(doc.get("rca_duplicates_of")),
                "notes": str(doc.get("rca_notes") or "")[:600],
            }
        return out

    async def customer_state(
        self, org: str, ns: str, *, org_name: str, days: int = 365, limit: int = 10
    ) -> dict[str, Any]:
        """A customer across channels: their escalations (newest, most
        severe first), their cloud instances (count, versions, tiers,
        states) and the incidents that affected them. Restricted."""
        self._require_restricted("customer state")
        toks = text_tokens(org_name)
        if not toks:
            return {}
        lim = max(1, min(int(limit), 50))
        tp: dict[str, Any] = {}
        org_match = all_tokens("org_name", toks, tp)
        org_any = all_tokens("o", toks, tp)
        cases, instances, incidents = await asyncio.gather(
            self._rows(
                "customer_cases",
                f"""SELECT source_id, url, title, severity_level, primary_component, spoken_name,
                           version_affected, cloud_provider, created_at, closed_at, org_name
                    FROM {self.db}.support_cases FINAL
                    WHERE {org_match}
                      AND created_at >= now64(3) - INTERVAL {{days:UInt32}} DAY
                    ORDER BY multiIf(severity_level = 'critical', 0,
                                     severity_level = 'medium', 1, 2), created_at DESC
                    LIMIT {{limit:UInt32}}""",
                {**tp, "days": self._window(days), "limit": lim},
                cache_key=["cases", toks, days, lim],
            ),
            self._rows(
                "customer_instances",
                f"""SELECT count() AS instances, countIf(state = 'running') AS running,
                           arrayDistinct(groupArray(clickhouse_version)) AS versions,
                           arrayDistinct(groupArray(tier)) AS tiers,
                           arrayDistinct(groupArray(cloud)) AS clouds,
                           max(snapshotted_at) AS snapshot
                    FROM {self.db}.cloud_instances FINAL
                    WHERE {org_match}""",
                dict(tp),
                cache_key=["instances", toks],
            ),
            self._rows(
                "customer_incidents",
                f"""SELECT incident_id, title, status, created_at, resolved_at, url
                    FROM {self.db}.slack_incidents FINAL
                    WHERE arrayExists(o -> {org_any}, affected_orgs)
                      AND created_at >= now64(3) - INTERVAL {{days:UInt32}} DAY
                    ORDER BY created_at DESC LIMIT {{limit:UInt32}}""",
                {**tp, "days": self._window(days), "limit": lim},
                cache_key=["cust_inc", toks, days, lim],
            ),
        )
        inst = instances[0] if instances else {}
        return {
            "org_name": org_name,
            "escalations": [doc_row(r, "support_case") for r in cases],
            "instances": {
                "count": int(inst.get("instances") or 0),
                "running": int(inst.get("running") or 0),
                "versions": sorted(v for v in str_list(inst.get("versions")) if v)[-8:],
                "tiers": sorted(v for v in str_list(inst.get("tiers")) if v),
                "clouds": sorted(v for v in str_list(inst.get("clouds")) if v),
                "snapshot": upstream_dt(inst.get("snapshot")),
            }
            if inst
            else {},
            "incidents": [
                {
                    "incident_id": str(r.get("incident_id") or ""),
                    "title": str(r.get("title") or "")[:140],
                    "status": str(r.get("status") or ""),
                    "created_at": upstream_dt(r.get("created_at")),
                    "resolved_at": upstream_dt(r.get("resolved_at")),
                    "url": str(r.get("url") or ""),
                }
                for r in incidents
            ],
        }

    async def slack_threads_matching(
        self,
        org: str,
        ns: str,
        *,
        term: str = "",
        channel: str = "",
        days: int = 90,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Slack incident / on-call / release / project threads whose title or
        body carries every token of `term`, optionally in one channel prefix,
        humans first, most replied first. Restricted."""
        self._require_restricted("slack threads")
        toks = text_tokens(term)
        conds = ["created_at >= now64(3) - INTERVAL {days:UInt32} DAY", "is_bot = 0"]
        params: dict[str, Any] = {"days": self._window(days), "limit": max(1, min(int(limit), 50))}
        matched = len(" ".join(toks)) >= 3
        if matched:
            conds.append(text_match(toks, params))
        if channel:
            conds.append("startsWith(channel_name, {channel:String})")
            params["channel"] = channel
        if not matched and not channel:
            return []
        sql = f"""
            SELECT source_id, url, title, channel_name, reply_count, participants, severity_level,
                   created_at, substring(body, 1, 300) AS excerpt
            FROM {self.db}.slack_threads FINAL
            WHERE {" AND ".join(conds)}
            ORDER BY reply_count DESC, created_at DESC
            LIMIT {{limit:UInt32}}
        """
        rows = await self._rows(
            "slack_threads", sql, params, cache_key=params, budget_ms=self._text_budget()
        )
        return [
            {
                "source_id": str(r.get("source_id") or ""),
                "url": str(r.get("url") or ""),
                "title": str(r.get("title") or "")[:160],
                "channel": str(r.get("channel_name") or ""),
                "replies": int(r.get("reply_count") or 0),
                "participants": len(str_list(r.get("participants"))),
                "severity": str(r.get("severity_level") or ""),
                "created_at": upstream_dt(r.get("created_at")),
                "excerpt": str(r.get("excerpt") or "")[:300],
            }
            for r in rows
        ]

    async def version_exposure(
        self, org: str, ns: str, *, version: str, limit: int = 20
    ) -> dict[str, Any]:
        """Who runs a release: cloud instances whose version starts with the
        minor, by tier / cloud / state, and the customers with the most
        instances on it — the blast radius of a regression in it. Restricted."""
        self._require_restricted("version exposure")
        minor = str(version).strip()
        if not minor:
            return {}
        lim = max(1, min(int(limit), 50))
        totals, tiers, orgs = await asyncio.gather(
            self._rows(
                "exposure_totals",
                f"""
                SELECT count() AS instances, countIf(state = 'running') AS running,
                       uniqExact(org_id) AS orgs, max(snapshotted_at) AS snapshot
                FROM {self.db}.cloud_instances FINAL
                WHERE clickhouse_version = {{v:String}}""",
                {"v": minor},
                cache_key=["exp_t", minor],
            ),
            self._rows(
                "exposure_tiers",
                f"""
                SELECT tier, cloud, state, count() AS instances
                FROM {self.db}.cloud_instances FINAL
                WHERE clickhouse_version = {{v:String}}
                GROUP BY tier, cloud, state ORDER BY instances DESC LIMIT 24""",
                {"v": minor},
                cache_key=["exp_tiers", minor],
            ),
            self._rows(
                "exposure_orgs",
                f"""
                SELECT org_name, count() AS instances, arrayDistinct(groupArray(tier)) AS tiers
                FROM {self.db}.cloud_instances FINAL
                WHERE clickhouse_version = {{v:String}} AND org_name != ''
                GROUP BY org_name ORDER BY instances DESC LIMIT {{limit:UInt32}}""",
                {"v": minor, "limit": lim},
                cache_key=["exp_orgs", minor, lim],
            ),
        )
        t = totals[0] if totals else {}
        return {
            "version": minor,
            "instances": int(t.get("instances") or 0),
            "running": int(t.get("running") or 0),
            "orgs": int(t.get("orgs") or 0),
            "snapshot": upstream_dt(t.get("snapshot")),
            "by_tier": [
                {
                    "tier": str(r.get("tier") or ""),
                    "cloud": str(r.get("cloud") or ""),
                    "state": str(r.get("state") or ""),
                    "instances": int(r.get("instances") or 0),
                }
                for r in tiers
            ],
            "top_orgs": [
                {
                    "org_name": str(r.get("org_name") or ""),
                    "instances": int(r.get("instances") or 0),
                    "tiers": sorted(x for x in str_list(r.get("tiers")) if x),
                }
                for r in orgs
            ],
        }

    async def version_limits(
        self, org: str, ns: str, *, version: str = "", limit: int = 20
    ) -> list[dict[str, Any]]:
        """Instances that carry a version cap (`has_version_limit`) and the
        reason — incident-driven pins a release manager must know about;
        optionally those capped at or below `version`. Restricted."""
        self._require_restricted("version limits")
        # `version_limit_max` is empty on most capped instances (verified live:
        # the reason text carries the link); a version narrows only where it is set
        cond = (
            "AND (version_limit_max = '' OR startsWith(version_limit_max, {v:String}))"
            if version
            else ""
        )
        sql = f"""
            SELECT version_limit_reason, version_limit_max, count() AS instances,
                   uniqExact(org_id) AS orgs, arrayDistinct(groupArray(3)(org_name)) AS examples
            FROM {self.db}.cloud_instances FINAL
            WHERE has_version_limit = 1 {cond}
            GROUP BY version_limit_reason, version_limit_max
            ORDER BY instances DESC
            LIMIT {{limit:UInt32}}
        """
        lim = max(1, min(int(limit), 50))
        rows = await self._rows(
            "version_limits", sql, {"v": version, "limit": lim}, cache_key=["limits", version, lim]
        )
        return [
            {
                "reason": str(r.get("version_limit_reason") or "")[:200],
                "max": str(r.get("version_limit_max") or ""),
                "instances": int(r.get("instances") or 0),
                "orgs": int(r.get("orgs") or 0),
                "examples": [x for x in str_list(r.get("examples")) if x],
            }
            for r in rows
        ]
