"""Re-embed stored vectors to the currently-configured embedder.

Loom does NOT auto-re-embed when LOOM_EMBEDDING_MODEL changes — search filters
by embedding_model, so rows on the old model become invisible to recall (not
mixed into the new vector space) until re-embedded. This module is the
documented re-index path: `loom reembed` after a model change of the same
width. On coded vector tables a change of width goes through Settings → Models
& keys, which rebuilds the tables at the new width.

It rewrites every row whose embedding_model differs from the live embedder,
across the three vector tables, preserving all columns. New-model rows coexist
with old ones (model is in the sort key for memory_embeddings /
conversation_embeddings; entity_vectors replaces in place via a newer
created_at), so a run is safe and resumable; --delete-old removes the stale
rows once verified.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Callable
from typing import Any

from server.llm import customer

# (text-source column, vector column) per table, plus an optional secondary
# (key text -> key vector, key dim) embedding.
_TABLES: dict[str, dict[str, Any]] = {
    "memory_embeddings": {
        "text": "content_excerpt",
        "vec": "embedding",
        "key_text": "memory_key",
        "key_vec": "memory_key_embedding",
        "key_dim": "memory_key_embedding_dim",
        "version": "version",
    },
    "entity_vectors": {
        "text": "entity_text",
        "vec": "entity_embedding",
        "version": "created_at",
    },
    "conversation_embeddings": {
        "text": "content_excerpt",
        "vec": "embedding",
        "version": None,
    },
}

_MATERIALIZED = "MATERIALIZED"


def _key_input(row: dict[str, Any]) -> str:
    """The key-axis text for a stored row: the same function the write paths
    use (`key_axis_text`), so no key embeds two ways."""
    from server.memory.domain.embed_text import key_axis_text

    return key_axis_text(str(row.get("memory_key") or ""))


def where_sql(
    cols: list[str],
    model: str,
    *,
    org: str | None,
    namespace: str | None,
    key_only: bool = False,
    exclude_orgs: frozenset[str] = frozenset(),
) -> tuple[str, dict[str, Any]]:
    """Pure: the row filter of a re-embed. Default selects off-model rows;
    `key_only` selects every live row of the scope regardless of model — the
    value vectors stay, only the key axis is rebuilt. `exclude_orgs` (orgs
    whose model calls are switched off) leaves their rows for a later run."""
    where = [] if key_only else [f"embedding_model != '{model}'"]
    where.append("embedding_dim > 0")
    params: dict[str, Any] = {}
    if "tombstoned_at" in cols:
        where.append("tombstoned_at IS NULL")
    if org:
        where.append("org = {org:String}")
        params["org"] = org
    if namespace:
        where.append("namespace = {ns:String}")
        params["ns"] = namespace
    if exclude_orgs:
        where.append("NOT has({off:Array(String)}, org)")
        params["off"] = sorted(exclude_orgs)
    return " AND ".join(where), params


def rewrite_row(
    row: dict[str, Any],
    cfg: dict[str, Any],
    model: str,
    dim: int,
    vec: list[float] | None,
    key_vec: list[float] | None,
) -> dict[str, Any]:
    """Pure: return a copy of `row` re-pointed at the new model/dim/vector,
    preserving every other column. `version` handling per the table engine.
    `vec=None` keeps the row's own value vector (key-only re-embed)."""
    out = dict(row)
    if vec is not None:
        out[cfg["vec"]] = vec
    out["embedding_model"] = model
    out["embedding_dim"] = dim
    if cfg.get("key_vec") and key_vec is not None:
        out[cfg["key_vec"]] = key_vec
        out[cfg["key_dim"]] = dim
    if cfg["version"] == "version":
        # One above the row it was read from: it replaces that row (the
        # key-only rewrite shares its sort key) and loses to any version a
        # live writer stamped after the snapshot (wall-clock ns), so a run
        # that races a write or a delete never brings the older content back.
        out["version"] = int(row.get("version") or 0) + 1
    elif cfg["version"] == "created_at":
        # newer created_at wins the ReplacingMergeTree(created_at) merge
        out.pop("created_at", None)
    return out


def in_database_sql(
    table: str, cols: list[str], *, model: str, dim: int, credentials: str, where_sql: str
) -> str:
    """`INSERT INTO loom.<table> SELECT …` that re-embeds inside ClickHouse
    with `aiEmbed` (26.7+): the vector columns become
    `aiEmbed(<text>, <model>, map('credentials', <named collection>))` cast to
    the column type, model/dim columns are rewritten, the version column is
    bumped the way `rewrite_row` bumps it, every other column is copied. Pure
    so it can be tested without a server."""
    cfg = _TABLES[table]
    m = model.replace("'", "\\'")
    cred = credentials.replace("'", "\\'")

    def emb(text_col: str) -> str:
        return f"CAST(aiEmbed({text_col}, '{m}', map('credentials', '{cred}')) AS Array(BFloat16))"

    # No `AS <column>` aliases: the INSERT column list binds positionally, and
    # an alias named like a column would shadow that column inside WHERE
    # (`'new-model' AS embedding_model` made `embedding_model != 'new-model'`
    # select nothing — the alias-shadow class the SQL lint checks for).
    select: list[str] = []
    insert_cols: list[str] = []
    for c in cols:
        if cfg["version"] == "created_at" and c == "created_at":
            continue  # newer created_at wins the merge; let DEFAULT now() fill it
        insert_cols.append(c)
        if c == cfg["vec"]:
            select.append(emb(cfg["text"]))
        elif cfg.get("key_vec") and c == cfg["key_vec"]:
            select.append(emb(cfg["key_text"]))
        elif c == "embedding_model":
            select.append(f"'{m}'")
        elif c == "embedding_dim" or (cfg.get("key_dim") and c == cfg["key_dim"]):
            select.append(f"toUInt16({int(dim)})")
        elif cfg["version"] == "version" and c == "version":
            select.append("version + 1")  # as rewrite_row: never above a later write
        else:
            select.append(c)
    return (
        f"INSERT INTO loom.{table} ({', '.join(insert_cols)})\n"
        f"SELECT {', '.join(select)}\nFROM loom.{table} FINAL\nWHERE {where_sql}"
    )


def in_database_settings(rows: int) -> dict[str, Any]:
    """Session settings for one in-database re-embed statement: the
    experimental gate, batches of 100 texts per request, and per-query
    quotas sized to the row count (2 calls per row leaves room for the key
    vector and retries)."""
    return {
        "allow_experimental_ai_functions": 1,
        "ai_function_embedding_max_batch_size": 100,
        "ai_function_max_api_calls_per_query": max(1000, rows // 100 * 2 + 100),
        "ai_function_max_input_tokens_per_query": max(1_000_000, rows * 2 * 600),
        "ai_function_max_retries": 3,
        "async_insert": 0,
        "wait_for_async_insert": 1,
    }


async def reembed_table_in_database(
    ch: Any,
    table: str,
    *,
    model: str,
    dim: int,
    credentials: str,
    org: str | None = None,
    namespace: str | None = None,
    dry_run: bool = False,
    exclude_orgs: frozenset[str] = frozenset(),
    log: Callable[..., None] = print,
) -> int:
    """Re-embed one table's off-model rows inside ClickHouse.
    One INSERT … SELECT per table; ClickHouse batches the embedding calls.
    Returns the off-model row count the statement covered."""
    cfg = _TABLES[table]
    cols = await _cols(ch, table)
    where = [f"embedding_model != '{model}'", "embedding_dim > 0"]
    params: dict[str, Any] = {}
    if "tombstoned_at" in cols:
        where.append("tombstoned_at IS NULL")
    if org:
        where.append("org = {org:String}")
        params["org"] = org
    if namespace:
        where.append("namespace = {ns:String}")
        params["ns"] = namespace
    if exclude_orgs:  # aiEmbed calls the provider too: a switched-off org's rows wait
        where.append("NOT has({off:Array(String)}, org)")
        params["off"] = sorted(exclude_orgs)
    where_sql = " AND ".join(where)
    total = (
        await ch.query_one(
            f"SELECT count() AS n FROM loom.{table} FINAL WHERE {where_sql}", params=params
        )
    )["n"]
    log(
        f"[{table}] {total} off-model rows"
        + (" (dry-run)" if dry_run else f" -> {model}/{dim} via aiEmbed in ClickHouse")
    )
    if dry_run or total == 0:
        return 0
    _ = cfg
    sql = in_database_sql(
        table, cols, model=model, dim=dim, credentials=credentials, where_sql=where_sql
    )
    await ch.execute(sql, params=params, settings=in_database_settings(int(total)))
    log(f"  {table}: {total} rows re-embedded in one statement")
    return int(total)


async def _cols(ch: Any, table: str) -> list[str]:
    rows = await ch.query_all(
        f"SELECT name FROM system.columns WHERE database='loom' AND table='{table}' "
        f"AND default_kind != '{_MATERIALIZED}' ORDER BY position"
    )
    return [r["name"] for r in rows]


async def reembed_table(
    ch: Any,
    embedder: Any,
    table: str,
    *,
    org: str | None = None,
    namespace: str | None = None,
    batch: int = 200,
    dry_run: bool = False,
    key_only: bool = False,
    start_after: list[str] | None = None,
    exclude_orgs: frozenset[str] = frozenset(),
    left_orgs: set[str] | None = None,
    log: Callable[..., None] = print,
) -> int:
    """Re-embed one table's off-model rows to `embedder`. Returns rows written.
    Pages of `batch` rows in sort-key order; `start_after` (a resume token's
    key) continues after that row. `left_orgs` collects the orgs switched off
    during the run whose rows were left unwritten.
    `key_only`: every live row of the scope gets its key-axis vector rebuilt
    from `key_axis_text` (the value vector is kept); tables without a key
    axis are skipped."""
    cfg = _TABLES[table]
    if key_only and not cfg.get("key_text"):
        return 0
    model, dim = embedder.model_name, embedder.dim
    cols = await _cols(ch, table)
    where_clause, params = where_sql(
        cols, model, org=org, namespace=namespace, key_only=key_only, exclude_orgs=exclude_orgs
    )
    total = (
        await ch.query_one(
            f"SELECT count() AS n FROM loom.{table} FINAL WHERE {where_clause}", params=params
        )
    )["n"]
    log(
        f"[{table}] {total} {'rows (key axis only)' if key_only else 'off-model rows'}"
        + (" (dry-run)" if dry_run else f" -> {model}/{dim}")
    )
    if dry_run or total == 0:
        return 0
    keys = _KEYS[table]
    order = ", ".join(keys)
    after = list(start_after) if start_after else None
    written = 0
    while True:
        page_where, page_params = where_clause, dict(params)
        if after is not None:
            marks = ", ".join(f"{{k{i}:String}}" for i in range(len(keys)))
            page_where += f" AND ({order}) > ({marks})"
            page_params.update({f"k{i}": str(v) for i, v in enumerate(after)})
        # One page at a time in sort-key order: memory is bounded by `batch`
        # and the key of the page's last row is the resume point.
        rows = await ch.query_all(
            f"SELECT {', '.join(cols)}, tuple({order}) AS _k FROM loom.{table} FINAL "
            f"WHERE {page_where} ORDER BY {order} LIMIT {{page:UInt32}}",
            params={**page_params, "page": int(batch)},
        )
        if not rows:
            break
        left = await _rewrite_page(ch, embedder, table, [_without_key(r) for r in rows], key_only)
        written += len(rows) - sum(left.values())
        if left:
            if left_orgs is not None:
                left_orgs.update(left)
            log(
                f"  {table}: {sum(left.values())} rows of an org switched off during the run "
                "left for later"
            )
        after = [str(x) for x in rows[-1]["_k"]]
        log(f"  {table}: {written}/{total}  resume: {encode_resume(table, after)}")
        if len(rows) < batch:
            break
    return written


def _without_key(row: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in row.items() if k != "_k"}


async def _rewrite_page(
    ch: Any, embedder: Any, table: str, chunk: list[dict[str, Any]], key_only: bool
) -> dict[str, int]:
    """Re-embed and write `chunk`; returns the rows it left unwritten per org,
    of an org whose model calls were switched off during the run."""
    cfg = _TABLES[table]
    model, dim = embedder.model_name, embedder.dim
    vecs: list[list[float]] | None = [[] for _ in chunk] if not key_only else None
    kvecs: list[list[float]] | None = [[] for _ in chunk] if cfg.get("key_text") else None
    # A page can span two orgs (the keyset leads with org): one call per org,
    # since a model call names a single customer.
    left: set[int] = set()
    left_by_org: dict[str, int] = {}
    groups = await customer.by_org(str(r.get("org") or "") for r in chunk)
    for org, positions in groups.items():
        try:
            with customer.for_org(org):
                if vecs is not None:
                    texts = [str(chunk[i].get(cfg["text"]) or "") for i in positions]
                    for i, v in zip(positions, await embedder.embed_many(texts), strict=True):
                        vecs[i] = v
                if kvecs is not None:
                    keys = [_key_input(chunk[i]) for i in positions]
                    for i, v in zip(positions, await embedder.embed_many(keys), strict=True):
                        kvecs[i] = v
        except customer.ModelCallsDisabled:
            left.update(positions)  # switched off since the run began: its rows wait
            left_by_org[org] = len(positions)
    out = [
        rewrite_row(r, cfg, model, dim, vecs[i] if vecs else None, kvecs[i] if kvecs else None)
        for i, r in enumerate(chunk)
        if i not in left
    ]
    if out:
        await ch.insert(
            f"loom.{table}", out, settings={"async_insert": 0, "wait_for_async_insert": 1}
        )
    return left_by_org


# Each table's sort key as strings (the Enum `scope` by name), the keyset a
# run pages through and resumes after.
_KEYS: dict[str, tuple[str, ...]] = {
    "memory_embeddings": (
        "org",
        "namespace",
        "agent",
        "user_id",
        "session_id",
        "toString(scope)",
        "embedding_model",
        "memory_key",
    ),
    "entity_vectors": ("org", "namespace", "agent", "user_id", "entity_text", "memory_key"),
    "conversation_embeddings": ("org", "namespace", "embedding_model", "thread_id", "msg_id"),
}


def encode_resume(table: str, key: list[str]) -> str:
    """The resume token a run prints after each page: `--resume <token>`
    continues after that row (tables before it are done)."""
    raw = json.dumps({"table": table, "key": key}, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def decode_resume(token: str) -> tuple[str, list[str]]:
    try:
        data = json.loads(base64.urlsafe_b64decode(token.encode("ascii")).decode("utf-8"))
        table, key = str(data["table"]), [str(x) for x in data["key"]]
    except (ValueError, KeyError, TypeError) as e:
        raise ValueError(f"not a reembed resume token: {e}") from None
    if table not in _TABLES or len(key) != len(_KEYS[table]):
        raise ValueError(f"not a reembed resume token for a known table: {table!r}")
    return table, key


async def delete_off_model(
    ch: Any,
    table: str,
    model: str,
    *,
    org: str | None = None,
    namespace: str | None = None,
    keep_orgs: frozenset[str] = frozenset(),
) -> None:
    """Delete the off-model rows a run replaced. `keep_orgs`: orgs whose rows
    the run left unwritten (switched off); their old vectors are all they have."""
    where = [f"embedding_model != '{model}'"]
    params: dict[str, Any] = {}
    if org:
        where.append("org = {org:String}")
        params["org"] = org
    if namespace:
        where.append("namespace = {ns:String}")
        params["ns"] = namespace
    if keep_orgs:
        where.append("NOT has({off:Array(String)}, org)")
        params["off"] = sorted(keep_orgs)
    await ch.execute(f"DELETE FROM loom.{table} WHERE {' AND '.join(where)}", params=params)


async def reembed_all(
    ch: Any,
    embedder: Any,
    *,
    org: str | None = None,
    namespace: str | None = None,
    batch: int = 200,
    dry_run: bool = False,
    delete_old: bool = False,
    in_database: str | None = None,
    key_only: bool = False,
    resume: str | None = None,
    log: Callable[..., None] = print,
) -> dict[str, int]:
    """`in_database` names the ClickHouse named collection holding the
    embedding provider's endpoint and key; when given, every table is
    re-embedded by `aiEmbed` inside ClickHouse instead of a Python loop.
    `resume`: a token a previous run printed; tables before its table are
    skipped and its table continues after the row it names.
    Raises CodedWidthMismatchError, before reading any row, when the embedder's
    width is not the one a coded vector table was created at."""
    from server.core.vectors import codes_state

    for table in _TABLES:
        await codes_state.refuse_width(table, embedder.dim)
    written: dict[str, int] = {}
    # A switched-off org's rows are left out and wait for a later run.
    off = await customer.switched_off_orgs()
    if org:
        off = off & {org}
    waiting = 0
    left_orgs: set[str] = set()
    resume_table, resume_key = decode_resume(resume) if resume else ("", None)
    tables = list(_TABLES)
    if resume_table:
        if in_database:
            raise ValueError("--resume pages the Python path; it does not apply to --in-database")
        tables = tables[tables.index(resume_table) :]
    for table in tables:
        if in_database:
            written[table] = await reembed_table_in_database(
                ch,
                table,
                model=embedder.model_name,
                dim=embedder.dim,
                credentials=in_database,
                org=org,
                namespace=namespace,
                dry_run=dry_run,
                exclude_orgs=off,
                log=log,
            )
        else:
            written[table] = await reembed_table(
                ch,
                embedder,
                table,
                org=org,
                namespace=namespace,
                batch=batch,
                dry_run=dry_run,
                key_only=key_only,
                start_after=resume_key if table == resume_table else None,
                exclude_orgs=off,
                left_orgs=left_orgs,
                log=log,
            )
        if off:
            waiting += await _waiting(ch, table, embedder.model_name, off, org, namespace, key_only)
    if off:
        written["skipped_switched_off"] = waiting
        log(f"{waiting} rows of {len(off)} switched-off org(s) left for a later run")
    if delete_old and not dry_run:
        # Every org whose rows wait keeps its old vectors: off at the start, off
        # during the run, or off by now.
        keep = frozenset(off | left_orgs | await customer.switched_off_orgs())
        for table in _TABLES:
            await delete_off_model(
                ch, table, embedder.model_name, org=org, namespace=namespace, keep_orgs=keep
            )
            log(f"[{table}] deleted off-model rows")
    return written


async def _waiting(
    ch: Any,
    table: str,
    model: str,
    off: frozenset[str],
    org: str | None,
    namespace: str | None,
    key_only: bool,
) -> int:
    """The rows of `table` a run left out because their org is switched off."""
    if key_only and not _TABLES[table].get("key_text"):
        return 0
    cols = await _cols(ch, table)
    where_clause, params = where_sql(cols, model, org=org, namespace=namespace, key_only=key_only)
    row = await ch.query_one(
        f"SELECT count() AS n FROM loom.{table} FINAL "
        f"WHERE {where_clause} AND has({{off:Array(String)}}, org)",
        params={**params, "off": sorted(off)},
    )
    return int((row or {}).get("n") or 0)
