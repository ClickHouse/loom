"""Pure repository paths and timestamp bounds shared by GitHub readers."""

import re
from datetime import datetime, timedelta

PAGE_SIZE = 100  # not a setting: the provider's per_page ceiling defines a full page


class FeedInputError(ValueError):
    """A source value cannot be represented without guessing its identity."""


def positive_id(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 0 < value < 2**64:
        raise FeedInputError("invalid upstream number or id")
    return value


def repository(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}/[A-Za-z0-9_.-]{1,100}", value):
        raise FeedInputError("repository must be owner/name")
    if value.split("/")[1] in {".", ".."}:
        raise FeedInputError("repository must be owner/name")
    return value


def since_parameter(value: datetime | None) -> str | None:
    return (value - timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ") if value else None
