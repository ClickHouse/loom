"""The task inbox plane's enumerations: the values the wire and the rows share."""

from __future__ import annotations

import re
from typing import Literal

TaskStatus = Literal[
    "triage",
    "todo",
    "in_progress",
    "blocked",
    "deferred",
    "done",
    "wont_do",
    "failed",
    "cancelled",
]
Disclosure = Literal["confidential", "internal", "public_ok"]
PolicyMode = Literal["deny", "review", "auto"]
BlockedOn = Literal["sender", "human", "external"]
RunStatus = Literal["active", "completed", "failed", "cancelled"]

# An inbox's `webhook_secret_ref` must name an environment variable with this
# prefix: any other name could hand a server secret to a URL the caller chose
# as the signing key.
WEBHOOK_SECRET_PREFIX = "LOOM_WEBHOOK_SECRET_"


def webhook_secret_org_prefix(org: str) -> str:
    """The start of every webhook secret name `org` may use:
    `LOOM_WEBHOOK_SECRET_<ORG>__`, ORG the org id with a-z upper-cased, digits
    kept and every other byte (A-Z too) as `_` and two hex digits, so no two
    orgs share it (`acme-x` → `ACME_2DX`, `acme_x` → `ACME_5FX`). Without it
    any tenant could name another tenant's secret and have Loom sign with it."""
    encoded = "".join(
        chr(b).upper() if chr(b) in "abcdefghijklmnopqrstuvwxyz0123456789" else f"_{b:02X}"
        for b in org.encode()
    )
    return f"{WEBHOOK_SECRET_PREFIX}{encoded}__"


def webhook_secret_ref_ok(org: str, ref: str) -> bool:
    """Whether `ref` names one of `org`'s own webhook secrets: the org prefix,
    then letters and digits only, so no other org's prefix can match it."""
    name = ref.removeprefix(webhook_secret_org_prefix(org))
    return name != ref and bool(re.fullmatch(r"[A-Za-z0-9]+", name))
