"""Each citation an assistant answer writes, checked against what Loom holds and what the
answer itself read.

A memory citation is verified when one of the answer's own tool calls was returned its
key (`loom.memory_exposures`), and a `sql` block when one of its `insights.query` calls
ran that statement (`loom.agent_events`): both read from the rows the answer's calls
left under its run, which land in the background, so the check waits up to five seconds
for them. A document citation is read with `knowledge.outline`, sent through Loom's own
HTTP routes in process with the answer's token, so every gate a read passes applies to
it. A code citation is not checked. Every check together runs for at most ten seconds.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Any

import httpx

from server.assistant.api.models import AssistantCitation
from server.assistant.domain import support as codes
from server.assistant.domain.citations import Citation, parse, section_path
from server.assistant.store import ledger
from server.memory.app import exposure_ledger
from server.observability.event_args import text_digest
from server.observability.run_context import set_ledger_run

log = logging.getLogger(__name__)

# The rows an answer's calls left land in the background; later than this, unverified.
_LEDGER_WAIT_S = 5.0  # not a setting: the design's bound on an answer's last event
_LEDGER_POLL_S = 0.5  # not a setting: a fraction of the wait
# Every check of one answer together: past it, a check still running could not run.
_CHECKS_S = 10.0  # not a setting: the answer's slot and its caller wait on the checks
# An answer cites a handful of sources; past this many the rest are not checked.
_MAX_CHECKED = 50  # not a setting: bounds the reads one answer's checks make
# Document reads run at once, this many at a time.
_PARALLEL_READS = 4  # not a setting: a fraction of one answer's request budget

_NOT_RUN = "the check could not run"
# The memory reads that record what they return (`loom.memory_exposures`): a key returned
# by any other call (memory.history, memory.provenance, an insights.query row) is not seen.
# The agent's instructions name the same reads (integrations/librechat/agent.yaml).
RECORDING_READS = ("memory.search", "memory.ask", "memory.get", "memory.list", "memory.inspect")
_UNREAD = f"no {', '.join(RECORDING_READS)} call in this answer was returned this key"

# The app Loom serves, for the document checks' in-process requests (`routes.attach`).
_app: Any = None


def set_app(app: Any) -> None:
    global _app
    _app = app


@dataclass(frozen=True)
class Checked:
    """One citation as the answer gets it, and the code its check ended with (codes in
    server/assistant/domain/support.py), which decides the answer's support."""

    citation: AssistantCitation
    code: str


def _result(c: Citation, code: str, reason: str = "") -> Checked:
    verified = code == codes.VERIFIED
    return Checked(
        AssistantCitation(kind=c.kind, text=c.text, verified=verified, reason=reason, ref=c.ref),
        code,
    )


async def verify(
    answer_text: str, *, org: str, namespace: str, token: str, run_id: str, subject: str
) -> list[Checked]:
    """Every citation in `answer_text`, each marked verified or not, with why not, and
    its `ref` (the memory key, document source or code path it names)."""
    found = parse(answer_text)
    cited, unchecked = found[:_MAX_CHECKED], found[_MAX_CHECKED:]
    rest = [_result(c, codes.PAST_CAP, "past the citations one answer checks") for c in unchecked]
    if not cited:
        return rest
    out: dict[int, Checked] = {}
    try:
        async with asyncio.timeout(_CHECKS_S):
            await _check_all(cited, out, org, namespace, token, run_id, subject)
    except TimeoutError:
        log.warning("an answer's citation checks ran past %s s", _CHECKS_S)
    return [out.get(i) or _result(c, codes.NOT_RUN, _NOT_RUN) for i, c in enumerate(cited)] + rest


async def _check_all(
    cited: list[Citation],
    out: dict[int, Checked],
    org: str,
    namespace: str,
    token: str,
    run_id: str,
    subject: str,
) -> None:
    """Fill `out` (citation index to result) as each check ends, so a check cut by the
    bound leaves the others' results. Each check runs in a task of `gather`'s, with a copy
    of the answer's context: Loom's request setup, which an in-process request runs, sets
    the request's auth, run and cost, and it sets them in that copy, never the answer's."""
    ledgered = asyncio.create_task(_ledgered(org, namespace, run_id, subject, cited))
    # One outline per document, however many of its sections are cited.
    documents: dict[str, list[int]] = {}
    for i, c in enumerate(cited):
        if c.kind == "code":
            out[i] = _result(c, codes.CODE, "code citations are not checked")
        elif c.kind == "doc":
            documents.setdefault(c.source_uri, []).append(i)
    gate = asyncio.Semaphore(_PARALLEL_READS)
    async with httpx.AsyncClient(
        # An exception the app raises outside a handler is its 500, never the answer's.
        transport=httpx.ASGITransport(app=_app, raise_app_exceptions=False),
        base_url="http://loom",
        # No run id: the checks' own rows are not filed under the answer's run.
        headers={"Authorization": f"Bearer {token}"},
        timeout=_CHECKS_S,
    ) as loom:

        async def from_ledger() -> None:
            seen = await ledgered
            for i, c in enumerate(cited):
                if c.kind in ("memory", "sql"):
                    out[i] = _from_ledger(c, seen)

        async def document(uri: str, at: list[int]) -> None:
            # A refusal ledgered before the transport binds the request's run reads this
            # task's copy, which carries the answer's run: cleared, so such a row names none.
            set_ledger_run("")
            async with gate:
                sections = await _sections(loom, uri, org, namespace)
            for i in at:
                out[i] = _document(cited[i], sections)

        try:
            await asyncio.gather(from_ledger(), *(document(u, at) for u, at in documents.items()))
        finally:
            ledgered.cancel()


@dataclass
class _Ledgered:
    """What the answer's calls left under its run: the statements its `insights.query`
    calls ran (digests) and which cited memory keys its reads were returned. Either is
    None when it cannot be read."""

    digests: set[str] | None
    keys: set[str] | None


async def _ledgered(
    org: str, namespace: str, run_id: str, subject: str, cited: list[Citation]
) -> _Ledgered:
    """The answer's rows once they hold every cited statement and key, or after the wait;
    a kind found whole is not read again. A kind that cannot be read (no run id, its
    rows not recorded, a failed read) is None."""
    want_digests = {text_digest(c.sql) for c in cited if c.kind == "sql"}
    want_keys = {c.key for c in cited if c.kind == "memory"}
    if not run_id:
        # With no run id any of the caller's rows with none would match.
        return _Ledgered(None, None)
    digests: set[str] | None = set()
    keys: set[str] | None = set() if exposure_ledger.enabled() else None
    loop = asyncio.get_running_loop()
    until = loop.time() + _LEDGER_WAIT_S
    while True:
        reads: dict[str, Any] = {}
        if digests is not None and not want_digests <= digests:
            reads["digests"] = _read(ledger.query_digests(org, namespace, run_id, subject))
        if keys is not None and not want_keys <= keys:
            reads["keys"] = _read(
                ledger.returned_keys(org, namespace, run_id, subject, sorted(want_keys))
            )
        got = dict(zip(reads, await asyncio.gather(*reads.values()), strict=True))
        digests = got.get("digests", digests)
        keys = got.get("keys", keys)
        if (digests is None or want_digests <= digests) and (keys is None or want_keys <= keys):
            break  # found whole (or nothing cited, or nothing readable)
        if loop.time() >= until:
            break
        await asyncio.sleep(_LEDGER_POLL_S)
    return _Ledgered(digests, keys)


async def _read(read: Awaitable[set[str]]) -> set[str] | None:
    """One ledger read; None when it fails."""
    try:
        return set(await read)
    except Exception as e:  # noqa: BLE001 — a check that cannot run never fails an answer
        log.warning("reading an answer's ledgered calls failed: %s", e)
        return None


def _from_ledger(c: Citation, seen: _Ledgered) -> Checked:
    if c.kind == "sql":
        if seen.digests is None:
            return _result(c, codes.NOT_RUN, _NOT_RUN)
        if text_digest(c.sql) in seen.digests:
            return _result(c, codes.VERIFIED)
        return _result(c, codes.SQL_NOT_RUN, "no insights.query in this answer ran it")
    if seen.keys is None:
        if not exposure_ledger.enabled():
            return _result(c, codes.NOT_RECORDED, "this deployment does not record memory reads")
        return _result(c, codes.NOT_RUN, _NOT_RUN)
    if c.key in seen.keys:
        return _result(c, codes.VERIFIED)
    return _result(c, codes.UNREAD, _UNREAD)


class _NotChecked(Exception):
    """Loom refused the check itself (a rate limit, an outage), not the citation."""


def _found(r: httpx.Response) -> dict[str, Any]:
    """The read's answer; a refused read (other than not found) leaves the citation
    unchecked rather than unverified."""
    if r.status_code == 200:
        return dict(r.json())
    if r.status_code in (403, 404):
        return {}
    raise _NotChecked(f"HTTP {r.status_code}")


async def _sections(
    loom: httpx.AsyncClient, uri: str, org: str, namespace: str
) -> set[tuple[str, ...]] | None:
    """The section paths of the live document at `uri`, as the answer's token reads them;
    an empty set when no live document is readable there, None when the read could not
    run."""
    body = {"org": org, "namespace": namespace, "source_uri": uri}
    try:
        out = _found(await loom.post("/v1/knowledge.outline", json=body))
    except (httpx.HTTPError, _NotChecked) as e:
        log.warning("checking a citation of %s failed: %s", uri, e)
        return None
    if not out.get("found"):
        return set()
    # The document's own entry: a document with no headings still answers found.
    paths = {section_path(" > ".join(s.get("path") or [])) for s in out.get("sections") or []}
    return paths | {()}


def _document(c: Citation, sections: set[tuple[str, ...]] | None) -> Checked:
    if sections is None:
        return _result(c, codes.NOT_RUN, _NOT_RUN)
    if not sections:
        return _result(c, codes.NO_DOCUMENT, "no live document of this source is readable")
    if c.section not in sections:
        return _result(c, codes.NO_SECTION, "the document has no such section")
    return _result(c, codes.VERIFIED)


__all__ = ["RECORDING_READS", "Checked", "set_app", "verify"]
