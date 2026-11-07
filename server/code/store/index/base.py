"""The base every code-plane reader mixin builds on: the injected client,
the generic insert, the per-namespace index state (watermarks) and the
tombstone / move mirrors of a live row.
"""

from __future__ import annotations

from typing import Any

from server.code.domain.index.state import IndexState, StateEntry
from server.code.store.index.rows import (
    _TABLE_COLS,
    VERSION_INVARIANT_COLS,
    _mirror_row,
    namespace_blocks,
    version_now,
)
from server.core.datetimes import naive_utc_now as _now
from server.core.repo_base import _CHClient, _default_client


class CodeRepoBase:
    """The injected ClickHouse client plus the operations every domain uses:
    `insert_rows`, the `code_index_state` rows, and row mirroring."""

    def __init__(self, ch: _CHClient | None = None) -> None:
        self._ch = ch or _default_client()

    async def insert_rows(self, table: str, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        cols = _TABLE_COLS.get(table)
        if cols is not None:
            for r in rows:
                missing = set(cols) - set(r)
                if missing:
                    raise ValueError(
                        f"{table}: row missing columns {sorted(missing)} — use build_row()"
                    )
        if table == "code_chunks":
            # A label mapped to its own table (another vector width) is
            # written there; everything else keeps the one insert.
            from server.core.vectors.code_tables import code_vector_table

            by_table: dict[str, list[dict[str, Any]]] = {}
            for r in rows:
                label = str(r.get("embedding_model") or "")
                by_table.setdefault(code_vector_table(label), []).append(r)
            for target, part in by_table.items():
                for block in namespace_blocks(part):
                    await self._ch.insert(target, block)
            # A served-label row is mirrored, cut to its prefix, into every
            # configured twin table — tombstones and
            # placeholders too, so the twin follows the served rows' lifecycle.
            from server.code.domain.index.prefix_twin import twin_rows

            for target, part in twin_rows(rows).items():
                await self._ch.insert(target, part)
            return
        await self._ch.insert(f"loom.{table}", rows)

    async def get_state(self, org: str, ns: str, key: str) -> str:
        return await self.get_state_or_none(org, ns, key) or ""

    async def get_state_or_none(self, org: str, ns: str, key: str) -> str | None:
        """The `key` row's value, None when the namespace has no such row ("" is a
        row that holds an empty value)."""
        row = await self._ch.query_one(
            """
            SELECT state_value FROM loom.code_index_state FINAL
            WHERE org = {org:String} AND namespace = {ns:String}
              AND state_key = {key:String}
            """,
            {"org": org, "ns": ns, "key": key},
        )
        return str(row["state_value"] or "") if row else None

    async def state_values_for_keys(self, keys: list[str]) -> dict[str, dict[tuple[str, str], str]]:
        """key -> (org, namespace) -> value of the `keys` state rows in every namespace
        that has one, across orgs, in one read; the org stays in the key because two orgs
        can hold the same namespace name. A key with no rows maps to {}."""
        rows = await self._ch.query_all(
            """
            SELECT org, namespace, state_key, state_value FROM loom.code_index_state FINAL
            WHERE state_key IN {keys:Array(String)}
            """,
            {"keys": keys},
        )
        out: dict[str, dict[tuple[str, str], str]] = {k: {} for k in keys}
        for r in rows:
            key = (str(r["org"]), str(r["namespace"]))
            out[str(r["state_key"])][key] = str(r.get("state_value") or "")
        return out

    async def state_rows_with_prefix(self, prefix: str) -> list[dict[str, Any]]:
        """Every namespace's state rows whose key starts with `prefix`, across orgs:
        (org, namespace, state_key, state_value) — a family of keys every entry may
        write (the tier-health records), read as one directory."""
        return await self._ch.query_all(
            """
            SELECT org, namespace, state_key, state_value
            FROM loom.code_index_state FINAL
            WHERE startsWith(state_key, {p:String})
            """,
            {"p": prefix},
        )

    async def all_state(self, org: str, ns: str) -> IndexState:
        rows = await self._ch.query_all(
            """
            SELECT state_key, state_value, detail, updated_at
            FROM loom.code_index_state FINAL
            WHERE org = {org:String} AND namespace = {ns:String}
            ORDER BY state_key
            """,
            {"org": org, "ns": ns},
        )
        return {
            str(r["state_key"]): StateEntry(
                value=r["state_value"], detail=r["detail"], updated_at=r["updated_at"]
            )
            for r in rows
        }

    async def all_state_many(
        self, pairs: list[tuple[str, str]]
    ) -> dict[tuple[str, str], IndexState]:
        """Every namespace's state in one statement: one `IN` over the (org,
        namespace) pairs instead of one `code_index_state` read per
        configured namespace. A namespace with no rows maps to {}."""
        if not pairs:
            return {}
        rows = await self._ch.query_all(
            """
            SELECT org, namespace, state_key, state_value, detail, updated_at
            FROM loom.code_index_state FINAL
            WHERE (org, namespace) IN {pairs:Array(Tuple(String, String))}
            ORDER BY org, namespace, state_key
            """,
            {"pairs": [(str(o), str(n)) for o, n in pairs]},
        )
        out: dict[tuple[str, str], IndexState] = {(str(o), str(n)): {} for o, n in pairs}
        for r in rows:
            out.setdefault((str(r["org"]), str(r["namespace"])), {})[str(r["state_key"])] = (
                StateEntry(value=r["state_value"], detail=r["detail"], updated_at=r["updated_at"])
            )
        return out

    async def set_state(self, org: str, ns: str, key: str, value: str, detail: str = "") -> None:
        await self._ch.insert(
            "loom.code_index_state",
            [
                {
                    "org": org,
                    "namespace": ns,
                    "state_key": key,
                    "state_value": value,
                    "detail": detail,
                    "updated_at": _now(),
                    "version": version_now(),
                }
            ],
        )

    @staticmethod
    def tombstone_of(table: str, live_row: dict[str, Any]) -> dict[str, Any]:
        """Mirror a live row into its tombstone version row."""
        row = _mirror_row(table, live_row)
        row["tombstoned_at"] = _now()
        if table == "code_answers":
            # A tombstone only hides the entry. Written after an erasure or a redact deleted
            # the row it was read from, a copy of the text would bring back what they deleted.
            row["answer"] = row["citations"] = ""
        return row

    @staticmethod
    def moved_of(table: str, live_row: dict[str, Any], **updates: Any) -> dict[str, Any]:
        """Mirror a live row into a newer version with `updates` applied —
        how a content-identical chunk or symbol that MOVED in its file gets
        its span refreshed without re-embedding or re-deriving anything
        else. Same ReplacingMergeTree key, higher version: FINAL keeps this
        one. Unknown columns are a loud failure (schema drift)."""
        unknown = set(updates) - set(_TABLE_COLS[table])
        if unknown:
            raise ValueError(f"{table}: unknown columns {sorted(unknown)}")
        frozen = set(updates) & VERSION_INVARIANT_COLS.get(table, frozenset())
        if frozen:
            raise ValueError(
                f"{table}: {sorted(frozen)} may not change across versions of one key "
                "(the grep statements skip FINAL's exact-mode widening on that invariant)"
            )
        row = _mirror_row(table, live_row)
        row.update(updates)
        return row
