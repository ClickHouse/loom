"""Changing a live setting and reading back its changes: what `/v1/admin/settings.*`
and `loom settings` share. Every change writes one `setting.changed` audit row,
deployment-wide (org and namespace `-`), with the value before and after and where the
value before came from."""

from __future__ import annotations

import json
from typing import Any

from server.core import settings_overrides
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
) -> dict[str, Any] | None:
    """`settings_overrides.change`, audited; returns the setting's row after."""
    before, row = await settings_overrides.change(
        name, value, clear=clear, expected=expected, ch=ch
    )
    await audit.emit(
        EVENT,
        org="-",
        namespace="-",
        actor=actor,
        subject={
            "name": name,
            "value": row["value"] if row else None,
            "previous": before["value"] if before else None,
            # Where `previous` came from: an override to set back, or the value of the
            # process that made the change (an env or a default), which a reset restores.
            # None for a setting this build lacks (the reset of a retired one's override).
            "previous_source": before["source"] if before else None,
            "cleared": clear,
        },
    )
    return row


async def history(
    name: str = "", limit: int = 50, *, ch: _CHClient | None = None
) -> list[dict[str, Any]]:
    """The recorded changes of setting `name` ("" = every setting), newest first."""
    out = []
    for r in await admin_repo.setting_changes(EVENT, name, limit, ch=ch):
        subject = json.loads(r["subject"])
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
