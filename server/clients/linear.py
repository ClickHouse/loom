"""Minimal client for a project tracker's documents, read over its GraphQL API with
an API key.

The key goes in the `Authorization` header as it is (the tracker's API keys take
no `Bearer` prefix). The host is a constant here, and every id travels as a
GraphQL variable, never inside the query text.

Errors carry the HTTP status and the error's type, never the response body, which
can name documents or the tracker's own hosts. The API reports most failures as
GraphQL errors, often with HTTP 200 or 400, so each error's `extensions.code` and
`extensions.type` are read as well as the status: a rate limit raises `RateLimited`
(a 429, or any error typed `ratelimited`), a refused key `CredentialRefused`, the
tracker's own not-found error `NotFound` (an answer that merely lacks the object is
a `LinearError`, never evidence of absence), an answer over the caller's size bound
`TooLarge`, and any other failure `LinearError`. An answer with any GraphQL error
raises, so a listing is never taken from a partial answer. Nothing here retries: the
sync worker's next pass is the retry. The errors are this client's own, not those of
`server.clients._vendor`: they carry the GraphQL error's type, which says more than
the status.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

import httpx

from server.clients._http import get_client
from server.clients._vendor import read_capped

_GRAPHQL_URL = "https://api.linear.app/graphql"
# Documents per listing page: the API's own default page size.
PAGE_SIZE = 50  # not a setting: the API's page size, the same on every deployment
# Seconds between listing pages, so a long list stays under the API's hourly
# request limit per key.
PAGE_PACE_S = 0.5  # not a setting: the API's limit, the same on every deployment
_sleep = asyncio.sleep  # the pause between pages; a test replaces it here alone
# A bound on one listing page's answer (50 nodes of ids, titles, stamps and links).
_PAGE_MAX_BYTES = 4 * 1024 * 1024  # not a setting: far above any real page
# The tracker's ids are UUIDs.
_ID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_KEY_RE = re.compile(r"[\x21-\x7e]{8,512}")

_PROJECT_DOCUMENTS = """
query ProjectDocuments($id: String!, $first: Int!, $after: String) {
  project(id: $id) {
    id
    documents(first: $first, after: $after) {
      nodes { id title updatedAt url }
      pageInfo { hasNextPage endCursor }
    }
  }
}
"""

_DOCUMENT = """
query Document($id: String!) {
  document(id: $id) { id content updatedAt url }
}
"""

_DOCUMENT_STATE = """
query DocumentState($id: String!) {
  document(id: $id) { id trashed archivedAt project { id } }
}
"""


class LinearError(RuntimeError):
    """`reason` is a short label for what failed (`http_500`, `not_found`,
    `graphql_internal_error`); `status` is the HTTP status the tracker answered, 0
    when it gave none (a network error, a bound passed here); `kind` is the first
    `extensions.type` or `extensions.code` among the errors, lower-cased with `_` as
    a space, '' when there was none."""

    def __init__(self, what: str, reason: str, *, status: int = 0, kind: str = "") -> None:
        super().__init__(f"{what}: {reason}")
        self.reason = reason
        self.status = status
        self.kind = kind

    @property
    def refused(self) -> bool:
        """The tracker refused this request, as opposed to failing to answer: by
        the GraphQL error's type when it gave one (it answers most errors with a
        400, its internal errors too), else by a 4xx status."""
        return self.kind in _REFUSED_KINDS if self.kind else 400 <= self.status < 500


class RateLimited(LinearError):
    pass


class CredentialRefused(LinearError):
    pass


class NotFound(LinearError):
    pass


class TooLarge(LinearError):
    pass


# GraphQL error types that say the request itself was refused.
_REFUSED_KINDS = {"forbidden", "invalid input", "feature not accessible", "user error"}


def valid_id(value: str) -> bool:
    """Whether `value` is an id the tracker hands out (a lower-case UUID)."""
    return bool(_ID_RE.fullmatch(value))


def valid_key(api_key: str) -> bool:
    """Whether `api_key` can be an API key: printable ASCII with no space, so it
    travels in a header unchanged."""
    return bool(_KEY_RE.fullmatch(api_key))


def _error_kinds(errors: list[Any]) -> list[str]:
    """Each GraphQL error's `extensions.type` and `extensions.code`, lower-cased with
    `_` read as a space (`RATELIMITED`, `AUTHENTICATION_ERROR`), in order."""
    kinds = []
    for error in errors:
        ext = error.get("extensions") if isinstance(error, dict) else None
        ext = ext if isinstance(ext, dict) else {}
        for key in ("type", "code"):
            if value := ext.get(key):
                kinds.append(str(value).lower().replace("_", " "))
    return kinds


def _not_found(errors: list[Any]) -> bool:
    """The tracker answers an id the key cannot see with `Entity not found`."""
    return any(
        isinstance(e, dict) and str(e.get("message") or "").lower().startswith("entity not found")
        for e in errors
    )


def _raise_for(status: int, body: Any, what: str) -> None:
    errors = body.get("errors") if isinstance(body, dict) else None
    errors = errors if isinstance(errors, list) else []
    kinds = _error_kinds(errors)
    kind = kinds[0] if kinds else ""
    if status == 429 or "ratelimited" in kinds:
        raise RateLimited(what, "rate_limited", status=status, kind="ratelimited")
    if status == 401 or "authentication error" in kinds:
        raise CredentialRefused(what, "credential_refused", status=status, kind=kind)
    if _not_found(errors):
        raise NotFound(what, "not_found", status=status, kind=kind)
    if kind:
        # The error's own type says more than the status, which is often a 400.
        raise LinearError(what, f"graphql_{kind.replace(' ', '_')}", status=status, kind=kind)
    if status >= 400:
        raise LinearError(what, f"http_{status}", status=status)
    if errors:
        raise LinearError(what, "graphql_error", status=status)


async def _query(
    api_key: str, query: str, variables: dict[str, Any], what: str, *, max_bytes: int
) -> dict[str, Any]:
    """The `data` of one GraphQL request, its answer read up to `max_bytes`."""
    api_key = api_key.strip()  # a pasted key often ends in a newline
    if not valid_key(api_key):
        raise CredentialRefused(what, "not_an_api_key")
    try:
        async with get_client().stream(
            "POST",
            _GRAPHQL_URL,
            headers={"Authorization": api_key, "Content-Type": "application/json"},
            json={"query": query, "variables": variables},
        ) as resp:
            raw = await read_capped(resp, max_bytes)
    except httpx.RequestError as e:
        raise LinearError(what, "unreachable") from e
    if raw is None:
        raise TooLarge(what, "too_large")
    try:
        body = json.loads(raw)
    except ValueError:
        body = None
    _raise_for(resp.status_code, body, what)
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, dict):
        raise LinearError(what, "no_data", status=resp.status_code)
    return data


async def documents_page(
    api_key: str, project_id: str, *, after: str = ""
) -> tuple[list[dict[str, Any]], str]:
    """One page of a project's documents (id, title, updatedAt, url) and the cursor
    of the next page, '' after the last. NotFound when the tracker says the key
    cannot see the project."""
    what = f"project {project_id}"
    variables: dict[str, Any] = {"id": project_id, "first": PAGE_SIZE}
    if after:
        variables["after"] = after
    data = await _query(api_key, _PROJECT_DOCUMENTS, variables, what, max_bytes=_PAGE_MAX_BYTES)
    project = data.get("project")
    if not isinstance(project, dict):
        # Not the tracker's not-found error: no evidence the project is gone.
        raise LinearError(what, "no_project")
    connection = project.get("documents")
    if not isinstance(connection, dict):
        raise LinearError(what, "no_documents")
    nodes = [n for n in connection.get("nodes") or [] if isinstance(n, dict)]
    info = connection.get("pageInfo")
    if not isinstance(info, dict):
        # Without it there is no telling whether more pages follow.
        raise LinearError(what, "no_page_info")
    if not info.get("hasNextPage"):
        return nodes, ""
    cursor = info.get("endCursor")
    if not isinstance(cursor, str) or not cursor or cursor == after:
        # A next page with no cursor to reach it: the listing cannot be completed.
        raise LinearError(what, "no_cursor")
    return nodes, cursor


async def project_documents(api_key: str, project_id: str) -> list[dict[str, Any]]:
    """Every document of a project, every page read and paced; a failure on any page
    raises."""
    found: list[dict[str, Any]] = []
    cursor = ""
    while True:
        nodes, cursor = await documents_page(api_key, project_id, after=cursor)
        found.extend(nodes)
        if not cursor:
            return found
        await _sleep(PAGE_PACE_S)


def _checked(document_id: str) -> str:
    if not valid_id(document_id):
        raise ValueError("not a document id")
    return document_id


async def document(api_key: str, document_id: str, *, max_bytes: int) -> dict[str, Any]:
    """One document's Markdown `content`, `updatedAt` and `url`, its answer read up to
    `max_bytes`; NotFound when the tracker says the key cannot see it."""
    what = f"document {document_id}"
    data = await _query(
        api_key, _DOCUMENT, {"id": _checked(document_id)}, what, max_bytes=max_bytes
    )
    doc = data.get("document")
    if not isinstance(doc, dict):
        # Not the tracker's not-found error: no evidence the document is gone.
        raise LinearError(what, "no_document")
    return doc


async def document_state(api_key: str, document_id: str) -> dict[str, Any]:
    """A document's `trashed`, `archivedAt` and `project`; NotFound when the key
    cannot see it."""
    what = f"document {document_id}"
    data = await _query(
        api_key,
        _DOCUMENT_STATE,
        {"id": _checked(document_id)},
        what,
        max_bytes=_PAGE_MAX_BYTES,
    )
    doc = data.get("document")
    if not isinstance(doc, dict):
        # Not the tracker's not-found error: no evidence the document is gone.
        raise LinearError(what, "no_document")
    return doc


__all__ = [
    "PAGE_PACE_S",
    "PAGE_SIZE",
    "CredentialRefused",
    "LinearError",
    "NotFound",
    "RateLimited",
    "TooLarge",
    "document",
    "document_state",
    "documents_page",
    "project_documents",
    "valid_id",
    "valid_key",
]
