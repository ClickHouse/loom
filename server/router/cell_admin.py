"""Closed public identities used by serial cell administration."""

from __future__ import annotations

import json
import re
from typing import Any
from uuid import UUID

from server.core.cell import normalise_host
from server.router import fingerprints, routing

MAX_CELLS = 4096  # not a setting: coordinator and directory share one finite fleet protocol
MAX_CENSUS_BYTES = 4 * 1024 * 1024  # not a setting: the client refuses larger census documents


class InvalidAdministration(ValueError):
    """The directory does not hold a complete supported administration identity."""


def _text(value: Any, maximum: int) -> str:
    try:
        size = len(value.encode("utf-8")) if type(value) is str else 0
    except UnicodeError as error:
        raise InvalidAdministration("Invalid administration identity") from error
    if (
        type(value) is not str
        or not value
        or size > maximum
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise InvalidAdministration("Invalid administration identity")
    return value


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    answer: dict[str, Any] = {}
    for key, value in pairs:
        if key in answer:
            raise InvalidAdministration("Repeated administration field")
        answer[key] = value
    return answer


def public_pair(raw: str) -> dict[str, dict[str, str]]:
    """Require two distinct public identities; empty legacy metadata refuses."""
    _text(raw, 1024)
    try:
        entries = json.loads(raw, object_pairs_hook=_unique)
    except (ValueError, RecursionError) as error:
        raise InvalidAdministration("Invalid administration metadata") from error
    if type(entries) is not list or len(entries) != 2:
        raise InvalidAdministration("Two administration identities are required")
    answer: dict[str, dict[str, str]] = {}
    for entry in entries:
        if type(entry) is not dict or entry.keys() != {"purpose", "name", "thumbprint"}:
            raise InvalidAdministration("Invalid administration metadata")
        purpose = entry["purpose"]
        if (
            type(purpose) is not str
            or purpose not in {"automation", "recovery"}
            or purpose in answer
        ):
            raise InvalidAdministration("Distinct administration purposes are required")
        name = _text(entry["name"], 256)
        thumbprint = _text(entry["thumbprint"], 43)
        if re.fullmatch(r"[A-Za-z0-9_-]{43}", thumbprint) is None:
            raise InvalidAdministration("Invalid public key thumbprint")
        answer[purpose] = {"name": name, "thumbprint": thumbprint}
    if any(answer["automation"][key] == answer["recovery"][key] for key in ("name", "thumbprint")):
        raise InvalidAdministration("Distinct administration identities are required")
    return answer


def identity(row: Any) -> dict[str, Any]:
    cell = _text(row["cell"], 32)
    if re.fullmatch(r"[a-z0-9-]{1,32}", cell) is None:
        raise InvalidAdministration("Invalid cell")
    return {
        "version": 1,
        "cell": cell,
        "deployment_id": _text(row["deployment_id"], 256),
        **public_pair(row["administration"]),
    }


def selected_record(raw: Any) -> dict[str, Any]:
    """The directory projects at most one KiB before the driver receives a row."""
    if type(raw) is not str or len(raw.encode("utf-8")) > 1024:
        raise InvalidAdministration("Administration projection is oversized")
    try:
        row = json.loads(raw, object_pairs_hook=_unique)
    except (ValueError, RecursionError) as error:
        raise InvalidAdministration("Invalid administration projection") from error
    if type(row) is not dict or row.keys() != {
        "cell",
        "deployment_id",
        "administration",
        "host",
        "state",
        "mode",
        "mutation_generation",
        "current_digest",
        "next_digest",
    }:
        raise InvalidAdministration("Invalid administration projection")
    return row


def provisioning_refusal(row: Any) -> str:
    """Permit a complete prepared later registration, without beginning attach."""
    if row.get("mode") != "enforced":
        return "automation provisioning requires enforced mode"
    if row.get("state") == "serving":
        return ""
    if (
        row.get("state") != "attaching"
        or row.get("is_first") is not False
        or "attach_started_at" not in row
        or row["attach_started_at"] is not None
        or type(row.get("placed_orgs")) is not int
        or row["placed_orgs"] != 0
    ):
        return "automation provisioning requires a serving or prepared later cell"
    try:
        host = _text(row["host"], 512)
        _text(row["cell"], 32)
        _text(row["deployment_id"], 256)
        for name in ("upstream_url", "probe_url"):
            value = _text(row[name], 2048)
            if routing.cell_address(value) != value:
                return "prepared cell addresses are incomplete"
        if normalise_host(host) != host or row["upstream_url"] == row["probe_url"]:
            return "prepared cell addresses are incomplete"
        if not fingerprints.readable(row["current_fp"]):
            return "prepared cell fingerprint is unreadable"
        return fingerprints.registration_pair_refusal(row, row["current_fp"], row["next_fp"])
    except (KeyError, TypeError, InvalidAdministration):
        return "prepared cell registration is incomplete"


def target(row: Any, *, prepared: bool = False) -> dict[str, Any]:
    public = identity(row)
    host = _text(row["host"], 512)
    if prepared:
        if reason := provisioning_refusal(row):
            raise InvalidAdministration(reason)
    if (
        normalise_host(host) != host
        or (row["state"] != "serving" and not prepared)
        or row["mode"] != "enforced"
    ):
        raise InvalidAdministration("Cell is not ready for administration")
    generation = str(row["mutation_generation"])
    try:
        if str(UUID(generation)) != generation:
            raise ValueError
    except ValueError as error:
        raise InvalidAdministration("Invalid cell generation") from error
    digests = [row["current_digest"]]
    if row["next_digest"] is not None:
        digests.append(row["next_digest"])
    if any(
        type(item) is not str or re.fullmatch(r"[0-9a-f]{64}", item) is None for item in digests
    ):
        raise InvalidAdministration("Missing configuration identity")
    answer = {key: value for key, value in public.items() if key != "version"}
    answer.update(
        origin="https://" + host,
        state=row["state"],
        mode="enforced",
        mutation_generation=generation,
        configuration_digests=sorted(set(digests)),
    )
    if len(json.dumps(answer, ensure_ascii=True, separators=(",", ":")).encode()) > 1024:
        raise InvalidAdministration("Administration identity is too large")
    return answer


def census(rows: list[Any]) -> dict[str, Any]:
    return census_targets([target(row) for row in rows])


def census_targets(cells: list[dict[str, Any]]) -> dict[str, Any]:
    if not 1 <= len(cells) <= MAX_CELLS:
        raise InvalidAdministration("Administration census is empty or too large")
    for key in ("cell", "deployment_id", "origin"):
        if len({row[key] for row in cells}) != len(cells):
            raise InvalidAdministration("Administration identities must be distinct")
    answer = {"version": 1, "cells": cells}
    if (
        len(json.dumps(answer, ensure_ascii=True, separators=(",", ":")).encode())
        > MAX_CENSUS_BYTES
    ):
        raise InvalidAdministration("Administration census is too large")
    return answer
