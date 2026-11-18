"""Bounded transformations for the team's GitHub record."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from server.core.github_values import PAGE_SIZE
from server.core.github_values import FeedInputError as FeedInputError
from server.core.github_values import positive_id as positive_id
from server.core.github_values import repository as repository
from server.core.github_values import since_parameter as since_parameter
from server.tracker.domain.columns import COLUMNS_READ

EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
SECRET_PURPOSE = "tracker-github-feed"


class FeedCapacityError(RuntimeError):
    """The bounded scan cannot complete its current timestamp boundary."""


def writer_grants(lines: list[str], database: str) -> None:
    """Accept dedicated direct grants, refusing unknown or role-derived authority."""
    quoted = "`" + database.replace("\\", "\\\\").replace("`", "\\`") + "`"
    record_scopes = {database + ".*", quoted + ".*"}
    metadata_scopes = {
        f"{db}.{table}"
        for db in ("system", "`system`")
        for name in ("columns", "tables", "databases")
        for table in (name, f"`{name}`")
    }
    allowed = {"SELECT", "INSERT", "CREATE TABLE", "CREATE VIEW", "SHOW TABLES", "SHOW COLUMNS"}
    if not lines:
        raise FeedInputError("writer grants could not be verified")
    for line in lines:
        types, on, rest = line.removeprefix("GRANT ").partition(" ON ")
        scope, to, recipient = rest.rpartition(" TO ")
        access = {name.strip().upper() for name in types.split(",")}
        if (
            not line.startswith("GRANT ")
            or not on
            or not to
            or not recipient
            or " WITH " in recipient.upper()
            or not (
                scope in record_scopes
                and access <= allowed
                or scope in metadata_scopes
                and access == {"SELECT"}
            )
        ):
            raise FeedInputError("writer grants must be direct and limited to the feed database")


def timestamp(value: Any, *, optional: bool = False) -> datetime:
    if not value and optional:
        return EPOCH
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("missing timezone")
        return parsed.astimezone(UTC)
    except (ValueError, OverflowError) as exc:
        raise FeedInputError("invalid upstream timestamp") from exc


def next_cursor(
    items: list[dict[str, Any]], *, complete: bool, previous: datetime | None
) -> datetime | None:
    if not items:
        return previous
    stamps = [timestamp(item.get("updated_at")) for item in items]
    # Provider ordering can carry high updated_at outliers. The beginning of
    # the final page is a conservative replay boundary; its maximum is not.
    boundary = max(stamps) if complete else min(stamps[-PAGE_SIZE:])
    if not complete and previous is not None and boundary <= previous:
        raise FeedCapacityError("timestamp boundary exceeds the page budget; increase max-pages")
    return max(previous or EPOCH, boundary)


def _default(family: str) -> Any:
    if family.startswith("Array("):
        return []
    if family == "String":
        return ""
    if family == "DateTime":
        return EPOCH
    if family == "UUID":
        return uuid.UUID(int=0)
    return 0


def _names(value: Any, key: str) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(row.get(key) or "")[:256] for row in value if isinstance(row, dict)][:100]


def item_row(repo: str, data: dict[str, Any], *, is_pr: bool) -> dict[str, Any]:
    repo = repository(repo)
    number = positive_id(data.get("number"))
    kind = "github_pr" if is_pr else "github_issue"
    row = {name: _default(family) for name, family in COLUMNS_READ[kind + "s"].items()}
    source_id = f"{repo}#{number}"
    user = data.get("user") or {}
    row.update(
        id=uuid.uuid5(uuid.NAMESPACE_DNS, f"{kind}:{source_id}"),
        source_id=source_id,
        number=number,
        url=f"https://github.com/{repo}/{'pull' if is_pr else 'issues'}/{number}",
        title=str(data.get("title") or "")[:1024],
        body=str(data.get("body") or "")[:65536],
        author=str(user.get("login") or "")[:256],
        created_at=timestamp(data.get("created_at")),
        updated_at=timestamp(data.get("updated_at")),
        closed_at=timestamp(data.get("closed_at"), optional=True),
        has_test=None,
    )
    if is_pr:
        merged = timestamp(data.get("merged_at"), optional=True)
        row.update(
            pr_number=number,
            state="merged" if merged != EPOCH else str(data.get("state") or ""),
            is_merged=int(merged != EPOCH),
            merged_at=merged,
            review_requests_test=None,
            base_branch=str((data.get("base") or {}).get("ref") or "")[:256],
            additions=max(0, int(data.get("additions") or 0)),
            deletions=max(0, int(data.get("deletions") or 0)),
        )
    else:
        row.update(
            labels=_names(data.get("labels"), "name"),
            assignees=_names(data.get("assignees"), "login"),
            state_reason=str(data.get("state_reason") or "")[:64],
        )
    return row


def comment_row(repo: str, data: dict[str, Any], *, review: bool) -> dict[str, Any]:
    repo = repository(repo)
    url = str(data.get("pull_request_url" if review else "issue_url") or "")
    parsed = urlsplit(url)
    owner, _, path = parsed.path.removeprefix("/repos/").partition("/")
    name, _, _ = path.partition("/")
    prefix = f"/repos/{owner}/{name}/{'pulls' if review else 'issues'}/"
    suffix = parsed.path.removeprefix(prefix)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "api.github.com"
        or f"{owner}/{name}".lower() != repo.lower()
        or not parsed.path.startswith(prefix)
        or not suffix.isdecimal()
        or int(suffix) <= 0
        or parsed.query
        or parsed.fragment
    ):
        raise FeedInputError("comment parent does not match the configured repository")
    ident = positive_id(data.get("id"))
    return dict(
        comment_id=ident,
        item_number=positive_id(int(suffix)),
        review=int(review),
        body=str(data.get("body") or "")[:65536],
        author=str((data.get("user") or {}).get("login") or "")[:256],
        created_at=timestamp(data.get("created_at")),
        updated_at=timestamp(data.get("updated_at")),
    )
