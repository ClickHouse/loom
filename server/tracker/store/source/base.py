"""The source's core: the admission gate every read passes (kill switch,
configuration, contract, restricted classification), the privacy and
repository predicates, and the one bounded, cached read (`_rows`) with
the process-wide cache behind it. Every reader family subclasses
`SourceBase`. What admission and the read need from outside the store (the
source registry's refresh, the credential and client, the contract check,
the directory's classification) comes through a `SourceGate`, installed
once by the app layer (`server/tracker/app/source.py`).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Protocol

from server.core.errors import caller_text
from server.settings import settings
from server.tracker.domain.contract import ContractFailed
from server.tracker.domain.errors import TrackerUnavailable
from server.tracker.store.source import contract_reports
from server.tracker.store.source.sql import KIND_TABLES, RESTRICTED_TABLES

log = logging.getLogger(__name__)

_MAX_CACHE = 4096
_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}


def reset_cache_for_tests() -> None:
    _cache.clear()


class SourceGate(Protocol):
    """What a read needs from outside the store. `query_rows` raises
    `TrackerUnavailable` when the client cannot serve (breaker open,
    credential unset); `ensure_contract` raises `ContractFailed` when the
    schema falls short."""

    async def refresh_sources(self) -> None: ...

    def credential_configured(self, credential: str) -> bool: ...

    async def ensure_contract(self, cfg: Any) -> None: ...

    async def is_restricted(self, org: str, namespace: str) -> bool: ...

    async def query_rows(
        self,
        sql: str,
        params: dict[str, Any],
        *,
        credential: str,
        max_execution_s: float | None,
        query_settings: dict[str, Any] | None,
    ) -> list[dict[str, Any]]: ...


_gate: SourceGate | None = None


def install_gate(gate: SourceGate) -> None:
    global _gate
    _gate = gate


def _the_gate() -> SourceGate:
    if _gate is None:
        raise TrackerUnavailable("the tracker source gate is not installed")
    return _gate


class SourceBase:
    """One tracker namespace's identity, guards and bounded read, which every
    reader family builds on. Constructed per request (as `TrackerSource`);
    the cache and the clients are process-wide."""

    def __init__(self, org: str, namespace: str) -> None:
        from server.tracker.domain.namespaces import tracker_namespace

        self.org = org
        self.namespace = namespace
        self.cfg = tracker_namespace(org, namespace)
        # the source's database; an unknown namespace never reads
        self.db = self.cfg.database if self.cfg is not None else ""

    # -- guards ----------------------------------------------------------------

    @property
    def known(self) -> bool:
        return self.cfg is not None

    def _has_optional(self, table: str) -> bool:
        """Whether the source's last contract check found optional `table`.
        A mapping source names its tables, so it has it. An unchecked source
        (the first read after a boot) is taken not to: an optional table is
        optional, and assuming it would fail the whole read."""
        if self.cfg is None:
            return False
        rep = contract_reports.cached(self.cfg.credential)
        if rep is None:
            return False
        if rep.mode == "mapping":
            return True
        return table in rep.optional_present

    async def _admit(self) -> None:
        """Every read passes here first: kill switch, configuration, and the
        fail-closed classification check for a restricted namespace."""
        if not bool(settings.tracker_live_serve):
            raise TrackerUnavailable("LOOM_TRACKER_LIVE_SERVE is off")
        if self.cfg is None:
            # A source another process registered shows up in
            # this one's snapshot within `tracker_sources_refresh_seconds`
            from server.tracker.domain.namespaces import tracker_namespace

            await _the_gate().refresh_sources()
            self.cfg = tracker_namespace(self.org, self.namespace)
            if self.cfg is not None:
                self.db = self.cfg.database
        if self.cfg is None:
            raise TrackerUnavailable(
                f"{self.org}/{self.namespace} is not a configured tracker namespace"
            )
        if not _the_gate().credential_configured(self.cfg.credential):
            raise TrackerUnavailable(
                f"tracker credential {self.cfg.credential!r} is not configured"
            )
        await self._admit_contract()
        if self.cfg.restricted:
            try:
                ok = await _the_gate().is_restricted(self.org, self.namespace)
            except Exception as e:
                # The reason reaches the caller: the error's kind; the text is logged.
                log.warning("tracker: the classification read failed: %s: %s", type(e).__name__, e)
                raise TrackerUnavailable(f"classification unavailable: {caller_text(e)}") from e
            if not ok:
                raise TrackerUnavailable(
                    f"{self.namespace} is configured restricted but its directory row is not"
                )

    async def _admit_contract(self) -> None:
        """The source serves only while its schema meets the
        record contract (read once per process per TTL through its own
        credential). A failed check names what is missing; a check that
        cannot read is the source being unavailable."""
        try:
            await _the_gate().ensure_contract(self.cfg)
        except ContractFailed as e:
            raise TrackerUnavailable(str(e)) from e
        except TrackerUnavailable:
            raise
        except Exception as e:
            log.warning("tracker: the contract check failed: %s: %s", type(e).__name__, e)
            raise TrackerUnavailable(f"contract check: {caller_text(e)}") from e

    def _privacy(self) -> str:
        """The privacy predicate: standard namespaces never see a private row
        whatever the credential's policy says; restricted ones read their
        repositories only. An unconfigured namespace has no predicate to
        build — `TrackerUnavailable`, the same answer `_admit` gives."""
        if self.cfg is None:
            raise TrackerUnavailable(
                f"{self.org}/{self.namespace} is not a configured tracker namespace"
            )
        return "is_private = 0 AND " if not self.cfg.restricted else ""

    def repos(self) -> list[str]:
        if self.cfg is None:
            raise TrackerUnavailable(
                f"{self.org}/{self.namespace} is not a configured tracker namespace"
            )
        return list(self.cfg.repos)

    def _require_restricted(self, what: str) -> None:
        """Incidents, escalations, customers, Slack and instances name
        customers and production systems: only a RESTRICTED namespace serves
        them, whatever the credential could read."""
        if self.cfg is None or not self.cfg.restricted:
            raise TrackerUnavailable(
                f"{what} is served only through a restricted tracker namespace"
            )

    def _window(self, days: int) -> int:
        return max(1, min(int(days or 90), 3650))

    def _trunk_pr_pred(self) -> str:
        return (
            f"{self._privacy()}is_merged = 1 AND is_backport_pr = 0"
            " AND is_cherrypick_pr = 0"
            " AND splitByChar('#', source_id)[1] IN {repos:Array(String)}"
        )

    # -- bounded, cached read -------------------------------------------------

    @staticmethod
    def _text_budget() -> int:
        """Budget of a range scan — a text-index read (160–730 ms
        live) or the edge graph read: 300 ms is a point read's
        budget, and neither of these is a point read."""
        return int(settings.tracker_live_text_budget_ms)

    async def _rows(
        self,
        tag: str,
        sql: str,
        params: dict[str, Any],
        *,
        cache_key: Any,
        budget_ms: int | None = None,
        ttl_s: float | None = None,
        query_settings: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        await self._admit()
        assert self.cfg is not None
        # The statement names the contract's columns; the
        # source's report says how this source spells them
        report = contract_reports.cached(self.cfg.credential)
        if report is not None:
            sql = report.rewrite(sql, self.db)
        args = json.dumps(cache_key, sort_keys=True, default=str)
        key = f"{self.org}|{self.namespace}|{tag}|{args}"
        now = time.monotonic()
        hit = _cache.get(key)
        if hit is not None and hit[0] > now:
            return hit[1]
        gate = _the_gate()
        ms = budget_ms if budget_ms else int(settings.tracker_live_budget_ms)
        budget_s = max(0.05, ms / 1000.0)
        try:
            rows = await asyncio.wait_for(
                gate.query_rows(
                    sql,
                    params,
                    credential=self.cfg.credential,
                    max_execution_s=budget_s if budget_ms else None,
                    query_settings=query_settings,
                ),
                timeout=budget_s,
            )
        except TrackerUnavailable:
            raise
        except TimeoutError as e:
            # the caller's deadline, not the upstream's failure: the read is
            # unavailable, the breaker stays closed
            raise TrackerUnavailable(f"{tag} exceeded {int(budget_s * 1000)} ms") from e
        except Exception as e:
            log.warning("tracker %s failed: %s: %s", tag, type(e).__name__, e)
            raise TrackerUnavailable(f"{tag}: {caller_text(e)}") from e
        ttl = float(ttl_s) if ttl_s else float(settings.tracker_live_cache_ttl_s)
        if not rows:
            ttl = min(ttl, 60.0)  # an unknown number may land within minutes
        if len(_cache) >= _MAX_CACHE:
            _cache.clear()
        _cache[key] = (now + ttl, rows)
        return rows

    async def _resolve_ids(
        self,
        ids: list[str],
        kinds: set[str],
        *,
        tag: str,
        cols: str = "id, source_id",
        budget_ms: int | None = None,
    ) -> list[dict[str, Any]]:
        """`source_id` (and `cols`) for document ids, reading only the tables
        the edge types name — an id lookup is a scan (ids are not in any sort
        key), so every table left out is a scan saved. Unknown or empty types
        fall back to issues and PRs; restricted tables only on a restricted
        namespace."""
        assert self.cfg is not None
        tables = {KIND_TABLES[k] for k in kinds if k in KIND_TABLES} or {
            "github_issues",
            "github_prs",
        }
        if not self.cfg.restricted:
            tables -= RESTRICTED_TABLES
        if not ids or not tables:
            return []
        priv = self._privacy()
        parts = []
        for table in sorted(tables):
            kind = next(k for k, t in KIND_TABLES.items() if t == table)
            where = (
                f"{priv}id IN {{ids:Array(UUID)}}"
                if table in ("github_issues", "github_prs")
                else "id IN {ids:Array(UUID)}"
            )
            parts.append(
                f"SELECT {cols}, '{kind}' AS kind FROM {self.db}.{table} FINAL WHERE {where}"
            )
        return await self._rows(
            tag, " UNION ALL ".join(parts), {"ids": ids}, cache_key=[tag, ids], budget_ms=budget_ms
        )
