"""Changing a live setting and reading back its changes: what `/v1/admin/settings.*`
and `loom settings` share. Every change writes one `setting.changed` audit row,
deployment-wide (org and namespace `-`), with the value before and after and where the
value before came from."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from server.core import settings_overrides
from server.core.ch import statement_timeout
from server.core.ch_bounded import BoundedReadError, decode_json
from server.core.repo_base import _CHClient
from server.insights.store import admin as admin_repo
from server.observability import audit

EVENT = "setting.changed"


async def change(
    name: str,
    value: Any = None,
    *,
    clear: bool = False,
    expected: Any = settings_overrides.NOT_GIVEN,
    actor: str,
    ch: _CHClient | None = None,
    deadline: float | None = None,
) -> dict[str, Any] | None:
    """`settings_overrides.change`, audited; returns the setting's row after."""
    if deadline is None:
        before, row = await settings_overrides.change(
            name, value, clear=clear, expected=expected, ch=ch
        )
    else:
        before, row = await settings_overrides.change_bounded(
            name,
            value,
            clear=clear,
            expected=expected,
            ch=ch,
            deadline=deadline,
        )
    subject = {
        "name": name,
        "value": row["value"] if row else None,
        "previous": before["value"] if before else None,
        "previous_source": before["source"] if before else None,
        "cleared": clear,
    }
    if deadline is None:
        await audit.emit(EVENT, org="-", namespace="-", actor=actor, subject=subject)
    else:
        remaining = min(2.0, deadline - time.monotonic())
        if remaining <= 0:
            raise BoundedReadError("Setting audit allowance exhausted")
        async with asyncio.timeout(remaining):
            with statement_timeout(remaining):
                await audit.emit(EVENT, org="-", namespace="-", actor=actor, subject=subject)
    return row


async def history(
    name: str = "",
    limit: int = 50,
    *,
    ch: _CHClient | None = None,
    deadline: float | None = None,
) -> list[dict[str, Any]]:
    """The recorded changes of setting `name` ("" = every setting), newest first."""
    if deadline is None:
        rows = await admin_repo.setting_changes(EVENT, name, limit, ch=ch)
    else:
        settings_overrides.check_selected(name)
        rows = await admin_repo.setting_changes_bounded(
            EVENT, name, limit, ch=ch, deadline=deadline
        )
    out = []
    for r in rows:
        subject = json.loads(r["subject"]) if deadline is None else decode_json(r["subject"])
        if deadline is not None and (
            not isinstance(subject, dict)
            or set(subject) != {"name", "value", "previous", "previous_source", "cleared"}
            or subject["name"] != name
            or type(subject["cleared"]) is not bool
            or subject["previous_source"] is not None
            and type(subject["previous_source"]) is not str
        ):
            raise BoundedReadError("Malformed setting-history subject")
        out.append(
            {
                "ts": r["ts"],
                "actor": r["actor"],
                "name": subject["name"],
                "value": subject["value"],
                "previous": subject["previous"],
                "previous_source": subject["previous_source"],
                "cleared": subject["cleared"],
            }
        )
    return out
