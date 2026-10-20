"""Is `loom.memory_embeddings` patch-free?

The codes-first read is lazy materialization and ClickHouse skips the lazy
step while the table has an active patch part (ClickHouse issue #119885) — with
the flag on, a patched table reads MORE than a plain scan. So the main fact leg carries
`vector_search_use_quantized_codes = 1` only while this probe reads zero
patch parts: a regression (a writer that UPDATEs the table again, a rebuild
not yet run) turns the flag off on its own instead of landing silently. The
count is also the gauge `loom.memory_embeddings.patch_parts`.

One `system.parts` read per process per `memory_embeddings_patch_parts_probe_seconds`
(60 s); a failed probe reads as "not patch-free" — the safe side, it only
costs the codec path.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from server.core.ch_names import split_table

log = logging.getLogger(__name__)

TABLE = "loom.memory_embeddings"

PATCH_PARTS_SQL = (
    "SELECT countIf(startsWith(name, 'patch')) AS patch_parts "
    "FROM system.parts "
    "WHERE database = {db:String} AND table = {t:String} AND active"
)


# (reading or None when the last probe failed, monotonic time of that probe)
_cache: dict[str, tuple[int | None, float]] = {}


def reset() -> None:
    """Test seam."""
    _cache.clear()


def last_reading(table: str = TABLE) -> int | None:
    """The most recent probe's count (None: never probed or the probe failed)."""
    entry = _cache.get(table)
    return entry[0] if entry else None


def set_reading(value: int | None, table: str = TABLE) -> None:
    """Test seam and the boot probe's sink."""
    _cache[table] = (value, time.monotonic())


async def probe_patch_parts(ch: Any, table: str = TABLE) -> int | None:
    """Active patch parts of `table`; None when the read fails."""
    db, name = split_table(table)
    try:
        rows = await ch.query_all(PATCH_PARTS_SQL, params={"db": db, "t": name})
        value = int(rows[0].get("patch_parts") or 0) if rows else 0
    except Exception as e:  # noqa: BLE001 — the probe is advisory; a failure reads as "not patch-free"
        log.info("patch-part probe failed for %s: %s", table, str(e)[:160])
        value = None
    set_reading(value, table)
    return value


async def patch_free(ch: Any, *, table: str = TABLE, ttl_seconds: float | None = None) -> bool:
    """True only when a probe within the TTL read zero patch parts."""
    if ttl_seconds is None:
        from server.settings import settings

        ttl_seconds = float(settings.memory_embeddings_patch_parts_probe_seconds)
    entry = _cache.get(table)
    if entry is None or time.monotonic() - entry[1] >= ttl_seconds:
        await probe_patch_parts(ch, table)
        entry = _cache.get(table)
    return entry is not None and entry[0] == 0


def codes_first_mode(raw: Any) -> str:
    """`auto` | `on` | `off` for the main fact leg's codes flag."""
    s = str(raw if raw is not None else "auto").strip().lower()
    if s in ("on", "true", "1", "yes"):
        return "on"
    if s in ("off", "false", "0", "no"):
        return "off"
    return "auto"


async def fact_codes_first(ch: Any, *, settings_obj: Any | None = None, table: str = TABLE) -> bool:
    """Whether the main fact leg reads codes-first now: `on` pins it, `off`
    pins it off, `auto` (default) follows the patch-part probe. In every mode
    the setting name only ships when the replica probe says the server knows
    it."""
    if settings_obj is None:
        from server.settings import settings as _default_settings

        settings_obj = _default_settings
    from server.core.vectors.quantized_setting import quantized_codes_supported

    deployment_on = bool(settings_obj.vector_search_quantized_codes)
    if not (deployment_on and quantized_codes_supported()):
        return False
    mode = codes_first_mode(settings_obj.memory_search_fact_codes_first)
    if mode == "on":
        return True
    if mode == "off":
        return False
    return await patch_free(
        ch,
        table=table,
        ttl_seconds=float(settings_obj.memory_embeddings_patch_parts_probe_seconds),
    )


__all__ = [
    "PATCH_PARTS_SQL",
    "TABLE",
    "codes_first_mode",
    "fact_codes_first",
    "last_reading",
    "patch_free",
    "probe_patch_parts",
    "reset",
    "set_reading",
]
