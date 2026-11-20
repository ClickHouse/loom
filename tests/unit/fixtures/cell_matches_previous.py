"""Fingerprint reader for the previous supported cell protocol."""

from collections.abc import Mapping
from typing import Any

FINGERPRINT_VERSION = 1

DEFAULTS: dict[str, Any] = {
    "public_host": "",
    "oauth_issuer_host": "",
    "oidc_issuer": "",
    "oidc_client_id": "",
    "scim": False,
    "bearer_issuers": [],
    "configured_orgs": [],
    "end_user_orgs": [],
    "deployment_id": "",
    "bucket": "",
    "bucket_prefix": "",
}


def matches(mine: Mapping[str, Any], value: Mapping[str, Any] | None) -> bool:
    """Whether this process's fields match an accepted value: each field both know is
    equal, and a field the value lacks is at its default here. Fields only the value
    knows (a newer build's) are not this build's to judge."""
    if not isinstance(value, Mapping) or not value:
        return False
    # A later version changed what a field means: this build cannot judge it. A field
    # added keeps the version (the default rule covers it).
    if type(value.get("v")) is not int or value["v"] != FINGERPRINT_VERSION:
        return False
    theirs = value.get("fields")
    if not isinstance(theirs, Mapping):
        return False
    for name, mine_value in mine.items():
        expected = theirs[name] if name in theirs else DEFAULTS.get(name)
        if mine_value != expected:
            return False
    return True
