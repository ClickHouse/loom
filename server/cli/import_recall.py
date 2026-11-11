"""`loom import-recall`: an external vector-recall agent-memory SQLite
export → Loom (schema introspected at runtime), and the writer the
importers send their ops through."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import logging
import sqlite3
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from server.cli._client import refused_for_agent, resolve_token
from server.core.pg_lease import PublicationPending

log = logging.getLogger(__name__)


# ======================================================================
# import-recall — schema-introspecting importer for an external
# vector-recall agent-memory SQLite export (memories + conversations).
#
# Unlike import-clickgap (whose source schema is fixed
# and known), this store's SQLite layout is NOT known ahead of time —
# the file is excluded from our snapshots and the upstream tool has
# shipped several shapes. So this importer INTROSPECTS the DB at runtime:
#
#   1. Enumerate tables via sqlite_master.
#   2. Per table, read PRAGMA table_info and CLASSIFY it by column
#      heuristics into one of {messages, memories, ignore}.
#   3. memories → loom `memory.set`; messages → the conversations plane
#      (`conversation.create` + `conversation.append`), grouped by
#      conversation id. Rows/columns that don't match are skipped with a
#      logged warning — never a crash.
#
# The row→loom-write mapping is factored into pure functions
# (`_recall_*`) so it is unit-tested without a live server; the command
# body only wires the plan to an async writer (in-process handlers by
# default, HTTP when --base-url is given).
# ======================================================================

_RECALL_DEFAULT_AGENT = "recall"
_RECALL_SOURCE = "recall"  # provenance label (source:<>, <>://, <>_id:, tag)

# Column-name heuristics (matched case-insensitively). Order = priority.
# A memories/facts table needs a text/content column; the rest are
# optional enrichers.
_RECALL_CONTENT_COLS: tuple[str, ...] = (
    "content",
    "text",
    "memory",
    "fact",
    "value",
    "summary",
    "body",
    "statement",
    "note",
    "observation",
    "chunk",
)
_RECALL_TS_COLS: tuple[str, ...] = (
    "created_at",
    "created",
    "happened_at",
    "timestamp",
    "ts",
    "updated_at",
    "updated",
    "inserted_at",
    "sent_at",
    "time",
    "date",
)
_RECALL_META_COLS: tuple[str, ...] = (
    "metadata",
    "meta",
    "extra",
    "attributes",
    "attrs",
    "properties",
    "props",
    "tags_json",
    "json",
)
_RECALL_TYPE_COLS: tuple[str, ...] = (
    "memory_type",
    "mem_type",
    "type",
    "category",
    "kind",
    "class",
    "label",
)
_RECALL_ID_COLS: tuple[str, ...] = (
    "id",
    "memory_id",
    "uuid",
    "guid",
    "pk",
    "_id",
    "key",
    "memory_key",
)
# A conversations/messages table needs a conversation/session id, a role,
# and a content column.
_RECALL_CONV_COLS: tuple[str, ...] = (
    "conversation_id",
    "conversation",
    "conv_id",
    "session_id",
    "session",
    "thread_id",
    "thread",
    "chat_id",
    "dialog_id",
    "dialogue_id",
)
_RECALL_ROLE_COLS: tuple[str, ...] = (
    "role",
    "sender",
    "speaker",
    "author",
    "from",
    "sender_role",
    "actor",
    "from_role",
)
_RECALL_MSG_CONTENT_COLS: tuple[str, ...] = (
    "content",
    "text",
    "message",
    "body",
    "msg",
    "value",
)

# Roles the upstream store (and others) use → the loom conversations
# plane's fixed role vocabulary {user, assistant, system, tool}.
_RECALL_ROLE_MAP: dict[str, str] = {
    "user": "user",
    "human": "user",
    "customer": "user",
    "you": "user",
    "assistant": "assistant",
    "ai": "assistant",
    "bot": "assistant",
    "agent": "assistant",
    "model": "assistant",
    "gpt": "assistant",
    "system": "system",
    "tool": "tool",
    "function": "tool",
    "tool_call": "tool",
}

_RECALL_MIN_DT = datetime.min
_RECALL_DRYRUN_PREVIEW = 12
_RECALL_PREVIEW_CHARS = 120


# --- pure helpers (unit-tested) ---------------------------------------


def _recall_pick_column(names: list[str], candidates: tuple[str, ...]) -> str | None:
    """First `candidates` entry present in `names` (case-insensitive),
    returned in its original casing. None when nothing matches."""
    lower = {n.lower(): n for n in names}
    for cand in candidates:
        if cand in lower:
            return lower[cand]
    return None


def _recall_first_text_column(cols: list[tuple[str, str]], exclude: set[str]) -> str | None:
    """Fallback content detector: the first TEXT-ish column not already
    claimed by another role. Lets us import a memories table whose text
    column has an unrecognised name."""
    for name, decl in cols:
        if name in exclude:
            continue
        d = (decl or "").upper()
        if any(t in d for t in ("CHAR", "TEXT", "CLOB", "STRING")):
            return name
    return None


def _recall_detect_message_columns(
    cols: list[tuple[str, str]],
) -> dict[str, str | None] | None:
    """A messages table needs conversation-id + role + content. Requiring
    `role` (not just conv-id + content) is what stops a memories table that
    merely carries a session_id from being misread as a transcript."""
    names = [n for n, _ in cols]
    conv = _recall_pick_column(names, _RECALL_CONV_COLS)
    role = _recall_pick_column(names, _RECALL_ROLE_COLS)
    content = _recall_pick_column(names, _RECALL_MSG_CONTENT_COLS)
    if conv and role and content:
        return {
            "conversation_id": conv,
            "role": role,
            "content": content,
            "ts": _recall_pick_column(names, _RECALL_TS_COLS),
            "id": _recall_pick_column(names, _RECALL_ID_COLS),
        }
    return None


def _recall_detect_memory_columns(
    cols: list[tuple[str, str]],
) -> dict[str, str | None] | None:
    """A memories/facts table needs a text/content column (named, else the
    first TEXT column that isn't already an id/type/meta/ts/embedding). The
    rest are optional. None → not a memories table."""
    names = [n for n, _ in cols]
    type_col = _recall_pick_column(names, _RECALL_TYPE_COLS)
    meta_col = _recall_pick_column(names, _RECALL_META_COLS)
    ts_col = _recall_pick_column(names, _RECALL_TS_COLS)
    id_col = _recall_pick_column(names, _RECALL_ID_COLS)
    emb_col = next(
        (n for n in names if "embed" in n.lower() or "vector" in n.lower()),
        None,
    )
    content = _recall_pick_column(names, _RECALL_CONTENT_COLS)
    if content is None:
        claimed = {c for c in (type_col, meta_col, ts_col, id_col, emb_col) if c}
        content = _recall_first_text_column(cols, claimed)
    if content is None:
        return None
    if meta_col == content:  # a lone TEXT col can't be both
        meta_col = None
    return {
        "content": content,
        "type": type_col,
        "metadata": meta_col,
        "ts": ts_col,
        "id": id_col,
        "embedding": emb_col,
    }


def _recall_classify_table(
    cols: list[tuple[str, str]],
) -> tuple[str, dict[str, str | None]]:
    """('messages'|'memories'|'ignore', column-map). Messages first so a
    transcript with a content column isn't swallowed by the memory path."""
    msg = _recall_detect_message_columns(cols)
    if msg is not None:
        return "messages", msg
    mem = _recall_detect_memory_columns(cols)
    if mem is not None:
        return "memories", mem
    return "ignore", {}


def _recall_epoch_to_dt(x: float) -> datetime | None:
    """Unix seconds (or ms — magnitude-sniffed) → naive UTC datetime."""
    try:
        if x >= 1e11:  # milliseconds, not seconds
            x = x / 1000.0
        return datetime.fromtimestamp(x, tz=UTC).replace(tzinfo=None)
    except (OverflowError, OSError, ValueError):
        return None


def _recall_parse_dt(v: object) -> datetime | None:
    """Best-effort timestamp parse: ISO-8601 strings and unix epochs
    (int/float/numeric-string). Unparseable → None (never raises)."""
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return _recall_epoch_to_dt(float(v))
    s = str(v).strip()
    if not s:
        return None
    body = s[:-1] if s.endswith((".", ",")) else s
    if body.replace(".", "", 1).isdigit():
        return _recall_epoch_to_dt(float(body))
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _recall_parse_metadata(raw: object) -> dict[str, Any]:
    """Metadata is a JSON object (or already a dict). Anything else (NULL, a
    scalar, malformed JSON, a JSON array) → {} with a warning."""
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str | bytes | bytearray):
        return {}
    try:
        obj = json.loads(raw)
    except (ValueError, TypeError):
        log.warning("import-recall: unparseable metadata JSON, dropping: %.80r", raw)
        return {}
    return obj if isinstance(obj, dict) else {}


def _recall_memory_type(raw: object) -> tuple[str, str]:
    """Map a source type/category to (loom_memory_type, original_label).

    loom's episodic/profile types are excluded from similarity search; a
    vector-recall store expects everything to be searchable, so a
    non-searchable label is remapped to 'semantic' and its original kept
    (returned as `original_label`) to survive as a kind:<> tag."""
    from server.memory.domain.memory_types import NON_SEARCHABLE_TYPES

    t = (str(raw).strip() if raw not in (None, "") else "") or "semantic"
    if t in NON_SEARCHABLE_TYPES:
        return "semantic", t
    return t, ""


def _recall_dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for it in items:
        if it and it not in seen:
            seen.add(it)
            out.append(it)
    return out


def _recall_memory_tags(
    *, source_id: object, meta: dict[str, Any], original_type_label: str
) -> list[str]:
    """Tags that preserve what the flat write can't:
    source:recall          provenance filter for the whole import.
    recall_id:<id>         source-row traceability (also in source_trace).
    kind:<label>           remapped-away memory_type + any metadata
                           category/type/kind.
    <tag>                  metadata `tags` (list or scalar), verbatim.
    """
    tags = [f"source:{_RECALL_SOURCE}"]
    if source_id not in (None, ""):
        tags.append(f"{_RECALL_SOURCE}_id:{source_id}")
    if original_type_label:
        tags.append(f"kind:{original_type_label}")
    mtags = meta.get("tags")
    if isinstance(mtags, list):
        for t in mtags:
            if isinstance(t, (str, int, float)) and str(t).strip():
                tags.append(str(t).strip())
    elif isinstance(mtags, str) and mtags.strip():
        tags.append(mtags.strip())
    for key in ("category", "type", "kind"):
        v = meta.get(key)
        if isinstance(v, str) and v.strip():
            tags.append(f"kind:{v.strip()}")
    return _recall_dedupe(tags)


def _recall_entities(meta: dict[str, Any]) -> list[str]:
    ents = meta.get("entities")
    if isinstance(ents, list):
        return _recall_dedupe([str(e).strip() for e in ents if isinstance(e, (str, int, float))])
    return []


def _recall_memory_key(table: str, source_id: object, content: str) -> str:
    """Deterministic key so a re-import supersedes rather than duplicates.
    Prefers the source id; falls back to a content hash when the table has
    no id column."""
    if source_id not in (None, ""):
        return f"{_RECALL_SOURCE}_{table}_{source_id}"
    digest = hashlib.sha256(str(content).encode("utf-8")).hexdigest()[:24]
    return f"{_RECALL_SOURCE}_{table}_{digest}"


def _recall_source_trace(table: str, source_id: object) -> str:
    sid = source_id if source_id not in (None, "") else "-"
    return f"{_RECALL_SOURCE}://{table}/{sid}"


def _recall_map_memory_row(
    row: dict[str, Any], colmap: dict[str, str | None], *, table: str
) -> dict[str, Any] | None:
    """One memories-table row → memory.set payload (sans identity tuple).
    None when the row has no usable content (skip, don't crash)."""
    content_col = colmap.get("content")
    content = row.get(content_col) if content_col else None
    if content is None or str(content).strip() == "":
        return None
    content = str(content)

    _tc = colmap.get("type")
    raw_type = row.get(_tc) if _tc else None
    loom_type, original_label = _recall_memory_type(raw_type)
    _mc = colmap.get("metadata")
    meta = _recall_parse_metadata(row.get(_mc)) if _mc else {}
    _ic = colmap.get("id")
    source_id = row.get(_ic) if _ic else None
    _sc = colmap.get("ts")
    valid_at = _recall_parse_dt(row.get(_sc)) if _sc else None
    confidence = meta.get("confidence")
    return {
        "memory_key": _recall_memory_key(table, source_id, content),
        "memory_type": loom_type,
        "value": content,
        "tags": _recall_memory_tags(
            source_id=source_id, meta=meta, original_type_label=original_label
        ),
        "entities": _recall_entities(meta),
        "valid_at": valid_at,
        "confidence": (float(confidence) if isinstance(confidence, (int, float)) else 1.0),
        "source_trace": _recall_source_trace(table, source_id),
        # Copied from another system: a declared downgrade, never `asserted`.
        "source_class": "imported",
        "source_resource_id": (
            f"{_RECALL_SOURCE}:{table}:{source_id if source_id not in (None, '') else '-'}"
        ),
    }


def _recall_normalize_role(role: object) -> str:
    return _RECALL_ROLE_MAP.get((str(role or "").strip().lower()), "user")


def _recall_normalize_message(
    row: dict[str, Any], colmap: dict[str, str | None]
) -> dict[str, Any] | None:
    """One messages-table row → a normalized message dict, or None to skip
    (no content). Timestamp is preserved for ordering + provenance."""
    content_col = colmap.get("content")
    content = row.get(content_col) if content_col else None
    if content is None or str(content).strip() == "":
        return None
    _cc = colmap.get("conversation_id")
    conv = row.get(_cc) if _cc else None
    conv_id = str(conv) if conv not in (None, "") else "-"
    _rc = colmap.get("role")
    role = row.get(_rc) if _rc else None
    _idc = colmap.get("id")
    msg_id = row.get(_idc) if _idc else None
    _tsc = colmap.get("ts")
    ts = _recall_parse_dt(row.get(_tsc)) if _tsc else None
    return {
        "conversation_id": conv_id,
        "role": _recall_normalize_role(role),
        "content": str(content),
        "ts": ts,
        "msg_id": str(msg_id) if msg_id not in (None, "") else "",
    }


def _recall_group_messages(
    messages: list[dict[str, Any]],
) -> list[tuple[str, list[dict[str, Any]]]]:
    """Group normalized messages by conversation_id (first-seen order),
    each group ordered by timestamp (undated last, original order as the
    stable tiebreak)."""
    groups: dict[str, list[tuple[int, dict[str, Any]]]] = {}
    order: list[str] = []
    for i, m in enumerate(messages):
        cid = m["conversation_id"]
        if cid not in groups:
            groups[cid] = []
            order.append(cid)
        groups[cid].append((i, m))
    out: list[tuple[str, list[dict[str, Any]]]] = []
    for cid in order:
        items = sorted(
            groups[cid],
            key=lambda p: (p[1]["ts"] is None, p[1]["ts"] or _RECALL_MIN_DT, p[0]),
        )
        out.append((cid, [m for _, m in items]))
    return out


def _recall_plan_writes(
    sdb: Any, *, org: str, namespace: str, agent: str
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Introspect the whole DB and produce an ordered list of planned loom
    writes plus a per-table report. Pure w.r.t. the network/CH — reads the
    already-open (read-only) sqlite connection only, so it is unit-testable
    against a synthetic fixture DB."""
    writes: list[dict[str, Any]] = []
    report: dict[str, Any] = {
        "tables": {},
        "memories_imported": 0,
        "messages_imported": 0,
        "threads": 0,
        "skipped_rows": 0,
    }
    for tbl in _recall_list_tables(sdb):
        cols = _recall_table_columns(sdb, tbl)
        kind, colmap = _recall_classify_table(cols)
        info: dict[str, Any] = {
            "classification": kind,
            "columns": [n for n, _ in cols],
            "rows": 0,
            "imported": 0,
            "skipped": 0,
        }
        report["tables"][tbl] = info
        if kind == "ignore":
            info["rows"] = _recall_count(sdb, tbl)
            log.warning(
                "import-recall: table %r matched no memory/conversation "
                "heuristic (cols=%s) — ignored",
                tbl,
                info["columns"],
            )
            continue
        try:
            rows = _recall_fetch_rows(sdb, tbl)
        except Exception as e:  # noqa: BLE001 - a broken table must not abort the run
            info["classification"] = "error"
            info["error"] = str(e)
            log.warning("import-recall: failed to read table %r: %s — skipped", tbl, e)
            continue
        info["rows"] = len(rows)
        if kind == "memories":
            for r in rows:
                payload = _recall_map_memory_row(r, colmap, table=tbl)
                if payload is None:
                    info["skipped"] += 1
                    report["skipped_rows"] += 1
                    continue
                payload.update({"org": org, "namespace": namespace, "agent": agent})
                writes.append({"op": "memory.set", "payload": payload})
                info["imported"] += 1
                report["memories_imported"] += 1
        else:  # messages
            normalized: list[dict[str, Any]] = []
            for r in rows:
                m = _recall_normalize_message(r, colmap)
                if m is None:
                    info["skipped"] += 1
                    report["skipped_rows"] += 1
                    continue
                normalized.append(m)
            for cid, msgs in _recall_group_messages(normalized):
                thread_id = f"{_RECALL_SOURCE}_{tbl}_{cid}"
                writes.append(
                    {
                        "op": "conversation.create",
                        "payload": {
                            "org": org,
                            "namespace": namespace,
                            "agent": agent,
                            "thread_id": thread_id,
                            "title": "",
                            "metadata": {
                                "source": _RECALL_SOURCE,
                                "src_table": tbl,
                                "src_conversation_id": cid,
                            },
                        },
                    }
                )
                report["threads"] += 1
                for m in msgs:
                    meta = {"source": _RECALL_SOURCE, "src_table": tbl}
                    # conversation.append server-stamps ts (no ts input field),
                    # so preserve the source timestamp in metadata.
                    if m["ts"] is not None:
                        meta["src_ts"] = m["ts"].isoformat()
                    writes.append(
                        {
                            "op": "conversation.append",
                            "payload": {
                                "org": org,
                                "namespace": namespace,
                                "agent": agent,
                                "thread_id": thread_id,
                                "role": m["role"],
                                "content": m["content"],
                                "msg_id": m["msg_id"],
                                "metadata": meta,
                            },
                        }
                    )
                    info["imported"] += 1
                    report["messages_imported"] += 1
    return writes, report


# --- sqlite introspection (thin, side-effecting) ----------------------


def _recall_list_tables(sdb: Any) -> list[str]:
    rows = sdb.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return [r[0] for r in rows]


def _recall_table_columns(sdb: Any, table: str) -> list[tuple[str, str]]:
    safe = table.replace('"', '""')
    cur = sdb.execute(f'PRAGMA table_info("{safe}")')
    return [(r[1], r[2]) for r in cur.fetchall()]  # (name, declared_type)


def _recall_count(sdb: Any, table: str) -> int:
    safe = table.replace('"', '""')
    try:
        return int(sdb.execute(f'SELECT count(*) FROM "{safe}"').fetchone()[0])
    except sqlite3.Error:
        return 0


def _recall_fetch_rows(sdb: Any, table: str) -> list[dict[str, Any]]:
    safe = table.replace('"', '""')
    cur = sdb.execute(f'SELECT * FROM "{safe}"')
    return [dict(r) for r in cur.fetchall()]


# --- write side (in-process handlers or HTTP) -------------------------


def _recall_jsonable(payload: dict[str, Any]) -> dict[str, Any]:
    return {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in payload.items()}


def _recall_preview(payload: dict[str, Any]) -> dict[str, Any]:
    p = _recall_jsonable(payload)
    for key in ("value", "content"):
        val = p.get(key)
        if isinstance(val, str) and len(val) > _RECALL_PREVIEW_CHARS:
            p[key] = val[:_RECALL_PREVIEW_CHARS] + f"…<{len(payload[key])} chars>"
    return p


def _cmd_import_recall(args: argparse.Namespace) -> int:
    """Import an external vector-recall agent-memory SQLite export into Loom.

    Introspects the DB (no fixed schema assumed): every table is classified
    by its columns into memories (→ memory.set) or a conversation transcript
    (→ conversation.create + conversation.append, grouped by conversation
    id); anything else is ignored. Rows/columns that don't fit are skipped
    with a logged warning, never a crash.

    Preserves: content → value; a source type/category → memory_type (with
    the non-searchable episodic/profile labels remapped to 'semantic' and
    kept as a kind:<> tag); metadata tags/entities/confidence; the source
    timestamp → valid_at (memories) / metadata.src_ts (messages); and the
    source id → source_trace + a recall_id:<id> tag for traceability.

    Idempotent: deterministic memory_key / thread_id / msg_id, so a
    re-import supersedes rather than duplicates.

    Writes in-process against the configured ClickHouse by default (like
    import-clickgap); pass --base-url to POST the loom ops over HTTP instead
    (token via --token / $LOOM_API_KEY / ~/.loom/config.json / ~/.loom/token).

    Flags:
      --sqlite PATH         (required) path to the source SQLite export
      --org / --namespace   (required) Loom destination tuple
      --agent A             (default 'recall') Loom agent id
      --base-url URL        POST ops to a running server instead of writing
                            in-process
      --token T             JWT for --base-url mode
      --dry-run             introspect + plan, print the summary, no writes
    """
    import asyncio
    import sqlite3
    import sys

    async def _run() -> int:
        sqlite_path = args.sqlite
        if not sqlite_path:
            print("--sqlite is required", file=sys.stderr)
            return 2
        try:
            sdb = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
            sdb.row_factory = sqlite3.Row
        except sqlite3.Error as e:
            print(f"failed to open sqlite: {e}", file=sys.stderr)
            return 2

        org = args.org
        ns = args.namespace
        agent = args.agent or _RECALL_DEFAULT_AGENT
        try:
            writes, report = _recall_plan_writes(sdb, org=org, namespace=ns, agent=agent)
        finally:
            sdb.close()

        print(f"import-recall source: {sqlite_path}")
        for tbl, info in report["tables"].items():
            print(
                f"  table {tbl!r}: {info['classification']} "
                f"(cols={len(info['columns'])}, rows={info['rows']}, "
                f"imported={info['imported']}, skipped={info['skipped']})"
            )
        print(
            f"  → {report['memories_imported']} memories, "
            f"{report['messages_imported']} messages in "
            f"{report['threads']} threads, "
            f"{report['skipped_rows']} rows skipped"
        )
        if not writes:
            print(
                "no memory/conversation tables matched — nothing to import",
                file=sys.stderr,
            )
            return 0

        if args.dry_run:
            print("(dry-run; no writes)")
            for w in writes[:_RECALL_DRYRUN_PREVIEW]:
                print(
                    f"  would {w['op']} "
                    f"{json.dumps(_recall_preview(w['payload']), ensure_ascii=False)}"
                )
            if len(writes) > _RECALL_DRYRUN_PREVIEW:
                print(f"  ... and {len(writes) - _RECALL_DRYRUN_PREVIEW} more writes")
            return 0

        writer, closer = await _recall_make_writer(args)
        ok = failed = 0
        try:
            for w in writes:
                try:
                    await writer(w["op"], w["payload"])
                    ok += 1
                except Exception as e:  # noqa: BLE001 - one bad write shouldn't abort
                    failed += 1
                    ident = w["payload"].get("memory_key") or w["payload"].get("thread_id") or "?"
                    print(
                        f"  ! write failed ({w['op']} {ident}): {e}",
                        file=sys.stderr,
                    )
        finally:
            await closer()

        print(f"✓ import complete: {ok} writes ok, {failed} failed")
        return 0 if failed == 0 else 1

    return asyncio.run(_run())


_OpHandlers = dict[str, Callable[[dict[str, Any]], Awaitable[Any]]]


def _publication_pending(r: Any) -> bool:
    """Whether a write's answer is PUBLICATION_PENDING: committed, not to be repeated."""
    if r.status_code != 503:
        return False
    try:
        body = r.json()
    except ValueError:
        return False
    detail = body.get("detail") if isinstance(body, dict) else None
    return isinstance(detail, dict) and detail.get("code") == PublicationPending.code


async def _make_writer(
    args: argparse.Namespace, inproc_ops: Callable[[], _OpHandlers], *, timeout_s: float
) -> tuple[Any, Any]:
    """The (writer, closer) pair an importer writes through: `POST /v1/<op>`
    on --base-url with the resolved token when it is set, else the loom
    handlers in-process (`inproc_ops()` maps each op the importer sends to
    its handler) on a connected ClickHouse client."""
    base_url = getattr(args, "base_url", None)
    if base_url:
        import httpx

        token, source = resolve_token(args.token)
        if refused_for_agent(source):
            raise SystemExit(2)
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        client = httpx.AsyncClient(timeout=timeout_s, headers=headers)
        base = base_url.rstrip("/")

        async def _http_writer(op: str, payload: dict[str, Any]) -> None:
            r = await client.post(f"{base}/v1/{op}", json=_recall_jsonable(payload))
            if _publication_pending(r):
                return  # committed; the server's delivery worker publishes it
            r.raise_for_status()

        return _http_writer, client.aclose

    from server.cli._ch import connect_checked, disconnect
    from server.core.ch import get_client

    handlers = inproc_ops()
    ch = get_client()
    await connect_checked(ch, orgs=(args.org,))
    from server import boot
    from server.core.background import register_buffer_drainer
    from server.insights.app.provider_command import model_command

    register_buffer_drainer("boot-writes", boot.drain_writes)
    command = model_command()
    try:
        await command.__aenter__()
    except BaseException:
        await disconnect(ch)
        raise

    async def _inproc_writer(op: str, payload: dict[str, Any]) -> None:
        handler = handlers.get(op)
        if handler is None:
            raise ValueError(f"unknown op {op!r}")
        with contextlib.suppress(PublicationPending):  # committed; the worker publishes it
            await handler(payload)

    async def close() -> None:
        try:
            await command.__aexit__(None, None, None)
        finally:
            await disconnect(ch)

    return _inproc_writer, close


def _recall_inproc_ops() -> _OpHandlers:
    from server.knowledge.api.models_conversations import (
        ConversationAppendInput,
        ConversationCreateInput,
    )
    from server.knowledge.app.conversations import (
        handle_conversation_append,
        handle_conversation_create,
    )
    from server.memory.api.models_crud import MemorySetInput
    from server.memory.app.write import handle_memory_set

    return {
        "memory.set": lambda p: handle_memory_set(MemorySetInput(**p)),
        "conversation.create": lambda p: handle_conversation_create(ConversationCreateInput(**p)),
        "conversation.append": lambda p: handle_conversation_append(ConversationAppendInput(**p)),
    }


async def _recall_make_writer(args: argparse.Namespace) -> tuple[Any, Any]:
    """import-recall's writer: memory.set + conversation.create/append."""
    return await _make_writer(args, _recall_inproc_ops, timeout_s=30.0)


def _add_recall_import_parser(sub: Any) -> None:
    """The `import-recall` subcommand."""
    p_recall = sub.add_parser(
        "import-recall",
        help="Import an external vector-recall agent-memory SQLite export into "
        "Loom. Introspects the DB at runtime (schema not assumed): tables are "
        "classified by column heuristics into memories (→ memory.set) or "
        "conversation transcripts (→ conversation.append, grouped by "
        "conversation id); unmatched tables/rows are skipped, never crash. "
        "Idempotent via deterministic keys.",
    )
    p_recall.add_argument("--sqlite", required=True, help="Path to the source SQLite export")
    p_recall.add_argument("--org", required=True)
    p_recall.add_argument(
        "--namespace",
        required=True,
        help="Destination namespace (use a dedicated one, e.g. "
        "'recall', so rollback = namespace.delete)",
    )
    p_recall.add_argument(
        "--agent",
        default=_RECALL_DEFAULT_AGENT,
        help="Loom agent id for the imported rows (default: %(default)r)",
    )
    p_recall.add_argument(
        "--base-url",
        default=None,
        help="POST loom ops to a running server instead of "
        "writing in-process against the configured CH.",
    )
    p_recall.add_argument(
        "--token",
        default=None,
        help="JWT for --base-url mode (else $LOOM_API_KEY, ~/.loom/config.json or ~/.loom/token).",
    )
    p_recall.add_argument(
        "--dry-run",
        action="store_true",
        help="Introspect + plan + print the per-table summary, no writes.",
    )
    p_recall.set_defaults(func=_cmd_import_recall)
