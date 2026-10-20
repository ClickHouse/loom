"""Erasure canary: does a deletion reach every tier?

Writes one unmistakable token through every write path a memory takes
(memory.set with the token in the value, tags and entities; memory.link;
knowledge.add; a memory.search whose query carries the token), deletes what
it wrote through the public ops, waits for the derived tables to settle,
then scans EVERY String-shaped column of every `loom.*` table for the token
straight from ClickHouse. A hit is a tier the deletion did not reach.

The scan is driven by `system.columns`, so a table added next month is
covered without editing this file. Telemetry columns that keep the token
by design are allow-listed and reported separately (`search_trace_log.query`
is consent-gated user text; `audit_log.subject` is the audit trail).

    python bench/erasure_compliance.py --base-url http://localhost:7777 \\
        --token "$LOOM_API_KEY" --org <org> \\
        --ch-url http://localhost:8123 --ch-user loom --ch-password loomdev

Exit 0 when no unexpected tier holds the token, 1 otherwise. `--json`
prints the per-table hit map for the four-layer bench report.

Why a canary and not a unit test: a leak such as a dedup resolver that
tombstones `loom.memory` and nothing else (leaving live embedding rows) sits
in a path every unit test around it passes; only the live tables show it.

Behavioral verification (`--behavioral`): the scan above is
parametric — what the store holds — which is not the same as what the
system still answers. This mode plants one canary per substrate
(memory, a search whose query is the canary, a conversation turn, parked
context, a code-plane search where the namespace allows it),
proves the tenant can read each back, erases through the saga (a
namespace_list purge, then a user_id scope), then drives EVERY read-shaped
operation loom registers as the same tenant and counts the canary in the
tables outside the census. Gate: no canary in any read
op's response, no census gap, zero canary rows in the census tables.

    python bench/erasure_compliance.py --behavioral --base-url http://127.0.0.1:7866 \
        --token "$TOKEN" --org erasure-bench \
        --ch-url https://<host>:8443 --ch-user default --ch-password ... --json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# (table, column) pairs allowed to keep the token after erasure, with the reason.
ALLOWED_RESIDUE: dict[tuple[str, str], str] = {
    # search_trace_log.query is not listed: the trace is a census table and its
    # rows leave with the namespace.
    ("audit_log", "subject"): "audit trail of the deletion itself",
    ("erasure_requests", "scope_value"): "the erasure request names its scope",
    ("agent_events", "memory_key"): "operation telemetry names the key it acted on",
    ("agent_events", "args"): "operation telemetry names the key it acted on",
    ("memory_usage_events", "memory_id"): "usage ledger keyed by id, not content",
}

STRING_TYPES_SQL = (
    "(type LIKE 'String%' OR type LIKE 'LowCardinality(String%' "
    "OR type LIKE 'Nullable(String%' OR type LIKE 'Array(String%' "
    "OR type LIKE 'Array(LowCardinality(String%' OR type LIKE 'FixedString%' "
    "OR type LIKE 'Map(%String%')"
)


@dataclass
class ScanHit:
    table: str
    column: str
    rows: int
    allowed: bool
    reason: str = ""


@dataclass
class CanaryReport:
    token: str
    namespace: str
    columns_scanned: int = 0
    hits: list[ScanHit] = field(default_factory=list)
    # Census tables the scan did not cover; empty is the gate.
    census_gaps: list[str] = field(default_factory=list)

    @property
    def unexpected(self) -> list[ScanHit]:
        return [h for h in self.hits if not h.allowed]

    def to_json(self) -> dict[str, Any]:
        return {
            "token": self.token,
            "namespace": self.namespace,
            "columns_scanned": self.columns_scanned,
            "hits": [h.__dict__ for h in self.hits],
            "unexpected_tiers": len(self.unexpected),
            "census_gaps": list(self.census_gaps),
        }


# ----------------------------------------------------------------------------
# pure helpers (unit-tested)
# ----------------------------------------------------------------------------


def scan_predicate(column: str, col_type: str) -> str:
    """SQL predicate testing whether `column` of `col_type` holds the token
    bound as {tok:String}. Arrays search each element, maps their values."""
    if col_type.startswith("Array("):
        return (
            f"arrayExists(x -> positionCaseInsensitive(toString(x), {{tok:String}}) > 0, "
            f"`{column}`)"
        )
    if col_type.startswith("Map("):
        return (
            f"arrayExists(x -> positionCaseInsensitive(toString(x), {{tok:String}}) > 0, "
            f"mapValues(`{column}`))"
        )
    return f"positionCaseInsensitive(toString(`{column}`), {{tok:String}}) > 0"


def scan_plan(columns: list[tuple[str, str, str]]) -> list[tuple[str, str, str]]:
    """(table, column, predicate) for every String-shaped column. `columns`
    is (table, name, type) as read from system.columns."""
    plan: list[tuple[str, str, str]] = []
    for table, name, col_type in columns:
        if table.startswith(".inner") or table.startswith("_"):
            continue
        plan.append((table, name, scan_predicate(name, col_type)))
    return plan


def census_coverage(plan_tables: set[str], census_tables: list[str]) -> list[str]:
    """Tables the saga's residue census counts that this scan did not plan: a census
    table with no String-shaped column, or one the plan skipped, is a tier the canary
    cannot see. `census_tables` are `loom.`-qualified as the repo names them; the plan
    uses bare names."""
    bare = {t.split(".", 1)[-1] for t in census_tables}
    return sorted(t for t in bare if t not in plan_tables)


def classify_hits(
    counts: list[tuple[str, str, int]],
    allowed: dict[tuple[str, str], str] | None = None,
) -> list[ScanHit]:
    allowed = ALLOWED_RESIDUE if allowed is None else allowed
    out: list[ScanHit] = []
    for table, column, rows in counts:
        if rows <= 0:
            continue
        reason = allowed.get((table, column), "")
        out.append(
            ScanHit(table=table, column=column, rows=rows, allowed=bool(reason), reason=reason)
        )
    return sorted(out, key=lambda h: (h.allowed, h.table, h.column))


# ----------------------------------------------------------------------------
# I/O
# ----------------------------------------------------------------------------


def _post(base_url: str, path: str, body: dict[str, Any], token: str) -> dict[str, Any]:
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        base_url + path,
        data=data,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            return json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:400]
        raise RuntimeError(f"{path} → HTTP {e.code}: {detail}") from None


def _ch(url: str, user: str, password: str, sql: str, params: dict[str, str] | None = None) -> str:
    """One read-only statement (`readonly = 2`); the credentials ride the auth
    headers, never the URL, so they stay out of proxy and query logs."""
    q = {"default_format": "TSV", "readonly": "2"}
    for k, v in (params or {}).items():
        q[f"param_{k}"] = v
    req = urllib.request.Request(
        url.rstrip("/") + "/?" + urllib.parse.urlencode(q),
        data=sql.encode(),
        headers={"X-ClickHouse-User": user, "X-ClickHouse-Key": password},
    )
    with urllib.request.urlopen(req, timeout=300) as r:
        return r.read().decode()


def write_canary(base: str, token: str, org: str, ns: str, agent: str, canary: str) -> list[str]:
    """Every write path, each carrying the canary token. Returns the keys written."""
    k1, k2 = f"canary_{canary}_a", f"canary_{canary}_b"
    ident = {"org": org, "namespace": ns, "agent": agent}
    _post(
        base,
        "/v1/memory.set",
        {
            **ident,
            "memory_key": k1,
            "memory_type": "semantic",
            "value": f"The canary token is {canary}. Alice prefers {canary} for lunch.",
            "tags": [canary],
            "entities": [canary],
        },
        token,
    )
    _post(
        base,
        "/v1/memory.set",
        {
            **ident,
            "memory_key": k2,
            "memory_type": "preference",
            "value": f"Second canary row mentioning {canary} twice: {canary}.",
        },
        token,
    )
    # A consolidated summary built FROM k1 (what the dense worker writes,
    # minus the LLM): tagged `src:<k1>` and carrying the token verbatim. It
    # lives under the same identity here; deleting k1 — its only source —
    # must retire it too, or the summary is a tier the deletion did not reach.
    _post(
        base,
        "/v1/memory.set",
        {
            **ident,
            "memory_key": f"consolidated:canary_{canary}",
            "memory_type": "semantic",
            "value": f"Topic memory: Alice prefers {canary} for lunch (from {k1}).",
            "tags": ["consolidated", f"src:{k1}"],
        },
        token,
    )
    optional = [
        (
            "/v1/memory.link",
            {
                "org": org,
                "namespace": ns,
                "from_key": k1,
                "to_key": k2,
                "relation": "supports",
                "explanation": f"canary {canary}",
            },
        ),
        (
            "/v1/knowledge.add",
            {
                "org": org,
                "namespace": ns,
                "source_uri": f"canary://{canary}",
                "title": f"canary {canary}",
                "content": f"# canary\n\nThis document carries {canary}.",
            },
        ),
        ("/v1/memory.search", {**ident, "query": f"what is {canary}", "top_k": 5}),
    ]
    for path, body in optional:
        try:
            _post(base, path, body, token)
        except Exception as e:  # noqa: BLE001 — optional tier; the scan still covers it
            print(f"  {path} skipped: {e}", file=sys.stderr)
    return [k1, k2]


def delete_canary(
    base: str,
    token: str,
    org: str,
    ns: str,
    agent: str,
    keys: list[str],
    canary: str,
) -> None:
    ident = {"org": org, "namespace": ns, "agent": agent}
    for k in keys:
        _post(base, "/v1/memory.delete", {**ident, "memory_key": k}, token)
    try:
        _post(
            base,
            "/v1/knowledge.delete",
            {"org": org, "namespace": ns, "source_uri": f"canary://{canary}"},
            token,
        )
    except Exception as e:  # noqa: BLE001
        print(f"  knowledge.delete skipped: {e}", file=sys.stderr)


def scan(
    ch_url: str,
    user: str,
    password: str,
    canary: str,
    database: str = "loom",
) -> CanaryReport:
    cols_tsv = _ch(
        ch_url,
        user,
        password,
        "SELECT c.table, c.name, c.type FROM system.columns AS c "
        "INNER JOIN system.tables AS t ON t.database = c.database AND t.name = c.table "
        f"WHERE c.database = {{db:String}} AND t.engine != 'MaterializedView' "
        f"AND {STRING_TYPES_SQL} ORDER BY c.table, c.name",
        {"db": database},
    )
    columns = [tuple(line.split("\t")) for line in cols_tsv.splitlines() if line]
    plan = scan_plan([(t, n, ty) for t, n, ty in columns])
    report = CanaryReport(token=canary, namespace="", columns_scanned=len(plan))
    # Every table the saga census counts must be a table this scan reads: the
    # canary's `entities` land in `entity_vectors` under the memory's key.
    try:
        from server.erasure.store.census import census_tables as _census_tables

        report.census_gaps = census_coverage({t for t, _c, _p in plan}, _census_tables("namespace"))
    except Exception as e:  # noqa: BLE001 — the bench runs without the server package too
        print(f"  census coverage not checked (server package unavailable): {e}", file=sys.stderr)
    counts: list[tuple[str, str, int]] = []
    by_table: dict[str, list[tuple[str, str]]] = {}
    for table, column, pred in plan:
        by_table.setdefault(table, []).append((column, pred))
    for table, cols in by_table.items():
        selects = ", ".join(f"countIf({pred}) AS `{column}`" for column, pred in cols)
        try:
            out = _ch(
                ch_url,
                user,
                password,
                f"SELECT {selects} FROM {database}.`{table}`",
                {"tok": canary},
            )
        except Exception as e:  # noqa: BLE001 — reported as a gap, which fails the run
            print(f"  scan {table} failed: {str(e)[:120]}", file=sys.stderr)
            # A table the scan could not read is a table it did not cover.
            report.census_gaps.append(f"{table} (scan failed)")
            continue
        values = out.strip().split("\t") if out.strip() else []
        for (column, _pred), v in zip(cols, values, strict=False):
            counts.append((table, column, int(v or 0)))
    report.hits = classify_hits(counts)
    return report


# ----------------------------------------------------------------------------
# behavioral verification
# ----------------------------------------------------------------------------

# Predicates that find the canary in the tables that sit outside the census, plus the
# two core tables. {tok:String} is the content canary (values, query text, turn content,
# parked context), {ktok:String} the key canary (one memory key carries it, so the
# exposure ledger's `returned_keys` can be checked without ever sending the content
# canary as a request identifier). Tables with a soft-delete count LIVE rows: a
# user-scope erasure tombstones, and the bytes leave on merge.
FOCUS_TABLES: dict[str, str] = {
    "memory": "positionCaseInsensitive(value, {tok:String}) > 0 AND tombstoned_at IS NULL",
    "memory_embeddings": (
        "positionCaseInsensitive(content_excerpt, {tok:String}) > 0 AND tombstoned_at IS NULL"
    ),
    "search_trace_log": (
        "positionCaseInsensitive(query, {tok:String}) > 0 OR "
        "arrayExists(x -> positionCaseInsensitive(x, {ktok:String}) > 0, returned_keys)"
    ),
    "memory_exposures": (
        "arrayExists(x -> positionCaseInsensitive(x, {ktok:String}) > 0, returned_keys)"
    ),
    "conversations": "positionCaseInsensitive(content, {tok:String}) > 0 AND tombstoned_at IS NULL",
    "context_blobs": "positionCaseInsensitive(content, {tok:String}) > 0 AND tombstoned_at IS NULL",
    "code_search_trace": "positionCaseInsensitive(query, {tok:String}) > 0",
    "events": (
        "positionCaseInsensitive(payload, {tok:String}) > 0 OR "
        "arrayExists(x -> positionCaseInsensitive(x, {ktok:String}) > 0, keys)"
    ),
}
# Focus tables the user-scope saga cannot reach (no user axis names them); a user-scope
# run reports them and gates on the rest.
NAMESPACE_ONLY_FOCUS = {"search_trace_log"}
# Read ops that still serve the canary after an erasure of a given scope, with
# the reason — the known behavioral residue. Reported in
# every run; the exit code counts only what this map does not explain.
KNOWN_READ_RESIDUE: dict[tuple[str, str], str] = {}
# Read-shaped ops the leakage suite skips as writes, plus the role-10 ops
# that still write on the caller's behalf.
_BEHAVIORAL_EXTRA_SKIP = {
    "memory.cross_promote",
    "conflicts.propose_revision",
    "code.outcome",
    "memory.demonstrate",
    "namespace.validate",
    "knowledge.synthesize",
}


def embedding_cache_key(model: str, text: str, org: str) -> str:
    """The shared embedding cache's row key: the server's own
    `server/embedding/_cache._key` (sha1 of the org, the model, a version salt
    and the normalised text; the salt is versioned while the read side embeds
    the normalised text). The text is not stored; the key is the only way to ask
    whether a text's vector is still in the table, so it must be computed by
    the one function the server writes rows with."""
    from server.embedding._cache import _key

    return _key(model, text, org)


def write_shaped_ops() -> set[str]:
    """The leakage suite's write-shaped list (it is the one place the ops
    that mutate on the caller's behalf are named) plus the role-10 ops that
    still write."""
    root = Path(__file__).resolve().parents[1]
    for entry in (str(root), str(root / "tests" / "integration")):
        if entry not in sys.path:
            sys.path.insert(0, entry)
    import test_cross_principal_leakage as leak

    return set(leak._SKIP_OPS) | _BEHAVIORAL_EXTRA_SKIP


def read_shaped_ops(all_operations: list[Any], skip: set[str]) -> list[Any]:
    """Every registered op a viewer may call, minus the write-shaped ones."""
    return [op for op in all_operations if int(op.required_role) <= 10 and op.name not in skip]


@dataclass
class Planted:
    canary: str
    user_id: str
    key_canary: str = ""
    entity: str = ""
    keys: list[str] = field(default_factory=list)
    memory_ids: list[str] = field(default_factory=list)
    texts: list[str] = field(default_factory=list)
    thread_id: str = ""
    blob_key: str = ""
    # substrate → "planted" | "skipped: <why>"
    substrates: dict[str, str] = field(default_factory=dict)


@dataclass
class ScopeReport:
    scope_type: str
    scope_value: str
    erasure: dict[str, Any] = field(default_factory=dict)
    before: dict[str, int] = field(default_factory=dict)
    # canary rows right after the saga (the gate) and again after the read
    # walk (the probe's own queries re-log trace rows; reported, not gated)
    after: dict[str, int] = field(default_factory=dict)
    after_walk: dict[str, int] = field(default_factory=dict)
    positive_control: bool = False
    read_ops: int = 0
    read_leaks: dict[str, list[str]] = field(default_factory=dict)
    bad_status: dict[str, int] = field(default_factory=dict)
    unbuildable: list[str] = field(default_factory=list)
    scan: dict[str, Any] = field(default_factory=dict)

    @property
    def gated_tables(self) -> set[str]:
        out = set(FOCUS_TABLES)
        if self.scope_type == "user_id":
            out -= NAMESPACE_ONLY_FOCUS
        return out

    @property
    def residue_tables(self) -> dict[str, int]:
        """Tables still holding the canary; -1 (the count failed) is residue too,
        so a table the bench could not read never passes as clean."""
        return {t: n for t, n in self.after.items() if n != 0}

    @property
    def unexplained_leaks(self) -> dict[str, list[str]]:
        return {
            op: n
            for op, n in self.read_leaks.items()
            if (self.scope_type, op) not in KNOWN_READ_RESIDUE
        }

    def passed(self, *, strict_status: bool = False) -> bool:
        gated = {t: n for t, n in self.residue_tables.items() if t in self.gated_tables}
        return (
            self.erasure.get("status") == "completed"
            and int(self.erasure.get("residue_count") or 0) == 0
            and self.positive_control
            and not self.unexplained_leaks
            and (not strict_status or not self.bad_status)
            and not self.scan.get("census_gaps")
            and not gated
        )


def _try(base: str, token: str, op: str, body: dict[str, Any]) -> dict[str, Any] | None:
    try:
        return _post(base, f"/v1/{op}", body, token)
    except Exception as e:  # noqa: BLE001 — the substrate is reported as skipped
        print(f"  {op} skipped: {str(e)[:120]}", file=sys.stderr)
        return None


def plant(
    base: str, token: str, org: str, ns: str, agent: str, user_id: str, canary: str
) -> Planted:
    """One canary per substrate, under one user. Identifiers (keys, thread,
    blob key, entity) never carry the content canary: a response that echoes
    the request's own identifier is not a leak, and the walker cannot tell
    the two apart. One memory key carries a second token for the key-bearing
    ledgers."""
    pl = Planted(canary=canary, user_id=user_id, key_canary=f"kc{uuid.uuid4().hex[:10]}")
    ident = {"org": org, "namespace": ns, "agent": agent, "user_id": user_id}
    hexid = uuid.uuid4().hex[:6]
    entity = pl.entity = f"Project {hexid}"
    v1 = f"The launch code for {entity} is {canary}; do not share it."
    v2 = f"Second canary row: the rotation secret is {canary}."
    for key, value in ((f"canary_{hexid}_0", v1), (f"canary_{pl.key_canary}", v2)):
        r = _try(
            base,
            token,
            "memory.set",
            {
                **ident,
                "memory_key": key,
                "memory_type": "semantic",
                "value": value,
                "tags": [canary],
                "entities": [entity],
            },
        )
        if r is not None:
            pl.keys.append(key)
            pl.memory_ids.append(str(r.get("memory_id") or ""))
            pl.texts.append(value)
    pl.substrates["memory"] = "planted" if pl.keys else "skipped: memory.set failed"
    # A search whose query IS the canary: search_trace_log.query, the
    # exposure ledger's returned_keys and the events stream payload.
    q = f"what is the launch code {canary}"
    r = _try(
        base, token, "memory.search", {**ident, "query": q, "top_k": 5, "namespace_scope": "this"}
    )
    pl.texts.append(q)
    pl.substrates["search"] = "planted" if r is not None else "skipped: memory.search failed"
    # Conversation turn (logging is per-namespace, default off).
    pl.thread_id = f"thread-{hexid}"
    on = _try(
        base, token, "conversation.set_logging", {"org": org, "namespace": ns, "enabled": True}
    )
    c = _try(
        base,
        token,
        "conversation.create",
        {**ident, "thread_id": pl.thread_id, "title": f"canary thread {hexid}"},
    )
    a = _try(
        base,
        token,
        "conversation.append",
        {
            **ident,
            "thread_id": pl.thread_id,
            "role": "user",
            "content": f"user turn carrying {canary} twice: {canary}",
        },
    )
    stored = bool(a and a.get("stored", True))
    pl.substrates["conversation"] = (
        "planted"
        if (on is not None and c is not None and a is not None and stored)
        else (
            f"skipped: set_logging={on is not None} create={c is not None} "
            f"append={a is not None} stored={stored}"
        )
    )
    # Parked context.
    b = _try(
        base,
        token,
        "context.offload",
        {
            **ident,
            "content": f"parked context with {canary} inside",
            "blob_key": f"blob-{hexid}",
        },
    )
    pl.blob_key = str((b or {}).get("blob_key") or "")
    pl.substrates["context_blob"] = (
        "planted" if b is not None else "skipped: context.offload failed"
    )
    # Code plane: a search logs a trace row even when the namespace holds no index.
    cs = _try(
        base,
        token,
        "code.search",
        {
            "org": org,
            "namespace": ns,
            "agent": agent,
            "user_id": user_id,
            "query": f"canary {canary}",
            "top_k": 5,
        },
    )
    pl.substrates["code_search"] = "planted" if cs is not None else "skipped: code.search refused"
    # Session summary: written by a worker, no public op — reported, not planted.
    pl.substrates["session_summary"] = "skipped: no public op writes session_summaries (worker)"
    return pl


def focus_counts(
    ch_url: str,
    user: str,
    password: str,
    canary: str,
    *,
    model: str,
    texts: list[str],
    org: str,
    key_canary: str = "",
    database: str = "loom",
) -> dict[str, int]:
    """Canary rows per focus table, plus the shared embedding cache by key."""
    out: dict[str, int] = {}
    params = {"tok": canary, "ktok": key_canary or canary}
    for table, pred in FOCUS_TABLES.items():
        try:
            v = _ch(
                ch_url,
                user,
                password,
                f"SELECT count() FROM {database}.`{table}` WHERE {pred}",
                params,
            )
            out[table] = int(v.strip() or 0)
        except Exception as e:  # noqa: BLE001
            print(f"  count {table} failed: {str(e)[:120]}", file=sys.stderr)
            out[table] = -1
    keys = [embedding_cache_key(model, t, org) for t in texts]
    if keys:
        try:
            v = _ch(
                ch_url,
                user,
                password,
                f"SELECT count() FROM {database}.embedding_cache "
                "WHERE model = {m:String} AND key IN {ks:Array(String)}",
                {"m": model, "ks": "[" + ",".join(f"'{k}'" for k in keys) + "]"},
            )
            out["embedding_cache"] = int(v.strip() or 0)
        except Exception as e:  # noqa: BLE001
            print(f"  count embedding_cache failed: {str(e)[:120]}", file=sys.stderr)
            out["embedding_cache"] = -1
    return out


def erase(
    base: str,
    token: str,
    org: str,
    scope_type: str,
    scope_value: str,
    *,
    requester: str,
    purge: bool = False,
    external: bool = True,
) -> dict[str, Any]:
    """erasure.request runs the saga inline; the response is the ledger row."""
    return _post(
        base,
        "/v1/erasure.request",
        {
            "org": org,
            "scope_type": scope_type,
            "scope_value": scope_value,
            "requester": requester,
            "purge": purge,
            "external": external,
        },
        token,
    )


def walk_read_ops(
    base: str,
    token: str,
    org: str,
    ns: str,
    pl: Planted,
    *,
    user_id: str | None = None,
) -> tuple[int, dict[str, list[str]], dict[str, int], list[str]]:
    """Drive every read-shaped op as the same tenant (and, when `user_id` is
    given, as the erased subject) with the leakage suite's payload synthesiser
    and walker. Returns (exercised, leaks, bad statuses, unbuildable)."""
    skip = write_shaped_ops()
    import test_cross_principal_leakage as leak

    from server.operations.registry import ALL_OPERATIONS

    seed = leak._Seed(org, ns, ns)
    seed.nonces = [pl.canary.lower()]
    seed.keys = pl.keys or ["canary_missing_a", "canary_missing_b"]
    seed.memory_ids = pl.memory_ids or [str(uuid.uuid4()), str(uuid.uuid4())]
    seed.entity = pl.entity or f"Project {uuid.uuid4().hex[:6]}"  # identifiers stay canary-free
    seed.value_text = pl.texts[0] if pl.texts else pl.canary
    seed.ns_c = ns
    leaks: dict[str, list[str]] = {}
    bad: dict[str, int] = {}
    unbuildable: list[str] = []
    exercised = 0
    for op in read_shaped_ops(list(ALL_OPERATIONS), skip):
        payload = leak._synth_payload(op.input_model, seed=seed, namespace=ns)
        if payload is None:
            unbuildable.append(op.name)
            continue
        if user_id is not None and "user_id" in op.input_model.model_fields:
            payload["user_id"] = user_id
        # an id-shaped field the suite's hints do not know lands a random id, not
        # the literal "canary" (a UUID column turns that into a 500, not a probe)
        for fname, val in list(payload.items()):
            if fname.endswith("_id") and val == "canary":
                payload[fname] = str(uuid.uuid4())
        if "thread_id" in op.input_model.model_fields and pl.thread_id:
            payload["thread_id"] = pl.thread_id
        if "blob_key" in op.input_model.model_fields and pl.blob_key:
            payload["blob_key"] = pl.blob_key
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            base + f"/v1/{op.name}",
            data=data,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                status, text = r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            status, text = e.code, e.read().decode(errors="replace")
        except Exception as e:  # noqa: BLE001
            bad[op.name] = -1
            print(f"  {op.name}: {str(e)[:100]}", file=sys.stderr)
            continue
        if status in (400, 422):
            unbuildable.append(op.name)
            continue
        exercised += 1
        if status not in (200, 403, 404):
            bad[op.name] = status
        found = seed.leaked(text)
        if found:
            leaks[op.name] = found
    return exercised, leaks, bad, unbuildable


def run_scope(
    args: argparse.Namespace,
    *,
    scope_type: str,
    ns: str,
    user_id: str,
    model: str,
) -> ScopeReport:
    canary = f"zq{uuid.uuid4().hex[:12]}"
    base, token, org = args.base_url, args.token, args.org
    print(f"[{scope_type}] canary {canary} → {org}/{ns} user {user_id}")
    pl = plant(base, token, org, ns, args.agent, user_id, canary)
    for k, v in pl.substrates.items():
        print(f"  plant {k:<16} {v}")
    time.sleep(args.settle_seconds)
    ch = (args.ch_url, args.ch_user, args.ch_password)
    rep = ScopeReport(scope_type=scope_type, scope_value=ns if scope_type != "user_id" else user_id)
    counts = dict(
        model=model, texts=pl.texts, org=org, key_canary=pl.key_canary, database=args.ch_database
    )
    rep.before = focus_counts(*ch, canary, **counts)
    # positive control: the tenant reads its own canary back before the erasure
    r = _try(
        base,
        token,
        "memory.search",
        {
            "org": org,
            "namespace": ns,
            "agent": args.agent,
            "user_id": user_id,
            "query": pl.texts[0] if pl.texts else canary,
            "top_k": 5,
            "namespace_scope": "this",
        },
    )
    rep.positive_control = bool(r) and canary.lower() in json.dumps(r).lower()
    print(f"  positive control: {rep.positive_control}; before={rep.before}")
    # the erasure (admin grant on the target first: erasure.request checks the grants table)
    _try(
        base,
        token,
        "grants.create",
        {
            "org": org,
            "namespace": ns,
            "subject": args.subject,
            "role": "admin",
            "granted_by": args.subject,
        },
    )
    if scope_type == "namespace_list":
        scope_value = json.dumps({"namespaces": [ns], "purge": True})
        out = erase(
            base,
            token,
            org,
            "namespace_list",
            scope_value,
            requester=f"{args.subject}@bench",
            purge=True,
            external=False,
        )
    else:
        out = erase(base, token, org, "user_id", user_id, requester=f"{args.subject}@bench")
    rep.erasure = {
        "erasure_id": out.get("erasure_id"),
        "status": out.get("status"),
        "residue_count": out.get("residue_count"),
        "failure_reason": out.get("failure_reason", ""),
        "steps": len(out.get("steps") or []),
        "residue": {t: n for t, n in (out.get("residue") or {}).items() if n},
    }
    print(
        f"  erasure {rep.erasure['status']} steps={rep.erasure['steps']} "
        f"residue={rep.erasure['residue']} {rep.erasure['failure_reason'][:120]}"
    )
    time.sleep(args.settle_seconds)
    rep.after = focus_counts(*ch, canary, **counts)
    print(f"  after erasure={rep.after}")
    # The parametric scan reads every String column of every table;
    # under a purge the bytes leave the parts, under a tombstone they stay
    # until the merge — the census is the gate for that scope, the scan the map.
    scan_report = scan(*ch, canary, args.ch_database)
    rep.scan = {
        "columns_scanned": scan_report.columns_scanned,
        "hits": [h.__dict__ for h in scan_report.hits],
        "unexpected": [f"{h.table}.{h.column}" for h in scan_report.unexpected],
        "census_gaps": scan_report.census_gaps,
    }
    print(f"  scan unexpected={rep.scan['unexpected']} gaps={rep.scan['census_gaps']}")
    rep.read_ops, rep.read_leaks, rep.bad_status, rep.unbuildable = walk_read_ops(
        base,
        token,
        org,
        ns,
        pl,
        user_id=user_id if scope_type == "user_id" else None,
    )
    print(
        f"  read ops exercised={rep.read_ops} leaks={rep.read_leaks} bad={rep.bad_status}"
        f" unbuildable={len(rep.unbuildable)}"
    )
    rep.after_walk = focus_counts(*ch, canary, **counts)
    print(f"  after the walk={rep.after_walk} (the probe's own queries re-log traces)")
    return rep


# `--behavioral` PURGES the namespaces it plants in, so it only ever runs in a
# throwaway one under this prefix (a bench prefix: the namespace GC also collects
# what an aborted run leaves behind).
BEHAVIORAL_NS_PREFIX = "bench-canary-"


def behavioral(args: argparse.Namespace) -> int:
    hexid = uuid.uuid4().hex[:8]
    if args.namespace and not args.namespace.startswith(BEHAVIORAL_NS_PREFIX):
        print(
            f"--behavioral purges its namespace; {args.namespace!r} is not a throwaway "
            f"{BEHAVIORAL_NS_PREFIX}* name. Omit --namespace to mint one.",
            file=sys.stderr,
        )
        return 2
    ns_a = args.namespace or f"{BEHAVIORAL_NS_PREFIX}{hexid}-a"
    ns_b = f"{ns_a}-u"
    model = args.embedding_model
    reports: list[ScopeReport] = []
    try:
        reports.append(
            run_scope(
                args,
                scope_type="namespace_list",
                ns=ns_a,
                user_id=f"canary-user-{hexid}-a",
                model=model,
            )
        )
        reports.append(
            run_scope(
                args, scope_type="user_id", ns=ns_b, user_id=f"canary-user-{hexid}-b", model=model
            )
        )
    finally:
        # The user scope leaves its namespace behind, and an aborted run leaves both:
        # purge them so the bench leaves nothing (a purge of an absent one is a no-op).
        _try(
            args.base_url,
            args.token,
            "erasure.request",
            {
                "org": args.org,
                "scope_type": "namespace_list",
                "scope_value": json.dumps({"namespaces": [ns_a, ns_b], "purge": True}),
                "requester": f"{args.subject}@bench",
                "purge": True,
                "external": False,
            },
        )
    strict = bool(args.strict_status)
    ok = all(r.passed(strict_status=strict) for r in reports)
    if args.json:
        print(
            json.dumps(
                {
                    "passed": ok,
                    "scopes": [
                        {
                            **r.__dict__,
                            "passed": r.passed(strict_status=strict),
                            "residue_tables": r.residue_tables,
                            "gated_tables": sorted(r.gated_tables),
                            "unexplained_leaks": r.unexplained_leaks,
                        }
                        for r in reports
                    ],
                },
                indent=2,
                default=str,
            )
        )
    else:
        for r in reports:
            verdict = "PASS" if r.passed(strict_status=strict) else "FAIL"
            print(
                f"{r.scope_type}: {verdict} erasure={r.erasure.get('status')} "
                f"read_leaks={r.read_leaks} unexplained={r.unexplained_leaks} "
                f"residue={r.residue_tables} 5xx={r.bad_status}"
            )
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--base-url", default="http://localhost:7777")
    ap.add_argument("--token", default="")
    ap.add_argument("--org", default="", help="required unless --scan-only: the org it writes into")
    ap.add_argument(
        "--namespace",
        default="",
        help="default: bench-erasure-canary; with --behavioral a fresh "
        f"{BEHAVIORAL_NS_PREFIX}<hex>-a (a given one must carry that prefix)",
    )
    ap.add_argument("--agent", default="canary")
    ap.add_argument("--ch-url", default="http://localhost:8123")
    ap.add_argument("--ch-user", default="loom")
    ap.add_argument(
        "--ch-password",
        default=os.environ.get("LOOM_CH_PASSWORD", "loomdev"),
        help="default LOOM_CH_PASSWORD, else the dev stack's password",
    )
    ap.add_argument("--ch-database", default="loom")
    ap.add_argument(
        "--settle-seconds",
        type=float,
        default=20.0,
        help="wait for async derived-table writes and the reconcile pass",
    )
    ap.add_argument("--scan-only", default="", help="skip writes; scan for this token")
    ap.add_argument("--json", action="store_true")
    ap.add_argument(
        "--behavioral",
        action="store_true",
        help="plant per substrate, erase through the saga, walk every read op",
    )
    ap.add_argument(
        "--strict-status",
        action="store_true",
        help="also fail on a read op answering 5xx (handler defects, not leaks)",
    )
    ap.add_argument(
        "--subject", default="alice", help="the token's subject (admin grant for erasure.request)"
    )
    ap.add_argument(
        "--embedding-model",
        default="text-embedding-3-large",
        help="the shared embedding cache's model name (its key is the server's "
        "`_cache._key(model, text, org)`)",
    )
    args = ap.parse_args()

    if not args.org and (args.behavioral or not args.scan_only):
        print(
            "--org required: the canary is written (and, with --behavioral, purged)",
            file=sys.stderr,
        )
        return 2
    if args.behavioral:
        if not args.token:
            print("--token required", file=sys.stderr)
            return 2
        return behavioral(args)

    args.namespace = args.namespace or "bench-erasure-canary"
    canary = args.scan_only or f"zq{uuid.uuid4().hex[:12]}"
    if not args.scan_only:
        if not args.token:
            print("--token required unless --scan-only", file=sys.stderr)
            return 2
        print(f"canary token {canary} → {args.namespace}")
        keys = write_canary(args.base_url, args.token, args.org, args.namespace, args.agent, canary)
        time.sleep(min(args.settle_seconds, 5.0))
        delete_canary(args.base_url, args.token, args.org, args.namespace, args.agent, keys, canary)
        print(f"deleted {len(keys)} memories + knowledge; settling {args.settle_seconds:.0f}s")
        time.sleep(args.settle_seconds)

    report = scan(args.ch_url, args.ch_user, args.ch_password, canary, args.ch_database)
    report.namespace = args.namespace
    if args.json:
        print(json.dumps(report.to_json(), indent=2))
    else:
        print(f"scanned {report.columns_scanned} String columns for {canary}")
        for h in report.hits:
            flag = "allowed " if h.allowed else "LEAK    "
            print(f"  {flag} {h.table}.{h.column:<28} {h.rows:>6} rows  {h.reason}")
        print(f"unexpected tiers holding the token: {len(report.unexpected)}")
        if report.census_gaps:
            print(f"census tables this scan did not cover: {', '.join(report.census_gaps)}")
    return 1 if report.unexpected or report.census_gaps else 0


if __name__ == "__main__":
    sys.exit(main())
