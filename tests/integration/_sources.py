"""A document source registry row seeded straight into ClickHouse (an add needs a
reachable store to probe), and the retirement state read back."""

from __future__ import annotations

import json
from typing import Any

NAME = "design-decks"
CONFIG = {"folder_ids": ["1AbCdEfGhIjKlMnOp"], "include": ["slides"]}


def seed_source(
    ch: Any, org: str, ns: str, name: str = NAME, credential: str = "not-a-sealed-value"
) -> None:
    ch.insert(
        "loom.knowledge_sources",
        [[org, ns, name, "gdrive", json.dumps(CONFIG), credential, "alice", True]],
        column_names=[
            "org",
            "namespace",
            "name",
            "kind",
            "config_json",
            "credential_enc",
            "created_by",
            "active",
        ],
    )


def retirement(ch: Any, org: str, ns: str) -> list[tuple[bool, str]]:
    """Each source row of the namespace as (active, credential_enc)."""
    rows = ch.query(
        "SELECT active, credential_enc FROM loom.knowledge_sources FINAL"
        " WHERE org = {org:String} AND namespace = {ns:String}",
        parameters={"org": org, "ns": ns},
    ).result_rows
    return [(bool(r[0]), str(r[1])) for r in rows]
