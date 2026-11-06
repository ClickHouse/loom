"""Sensitivity backfill and sweep (the tier is `memory.domain.sensitivity`).

`resolve_sensitivity` runs on the write paths — `memory.set`, `memory.bulk_set`,
`memory.update` and the extractor's entries. Rows that arrive by `observe.record`, `copy_to`, the
imports, or were written before the column existed carry `sensitivity = ''`
whatever they hold, so a row with an `sk-`-shaped key or an e-mail address can
sit unlabelled, embedded and returned by every search leg.

The sweep is the detector, run late:

  1. a coarse ClickHouse `match()` narrows live unlabelled rows to the
     shapes the detector could label;
  2. `detect_sensitivity` — the write path's own regexes, in Python —
     decides the level exactly, so a swept row is labelled as it would have
     been on write;
  3. `apply` writes ONE new `loom.memory` version per labelled row
     (`build_memory_version_row` carries every column; never `ALTER UPDATE`)
     with the level and its `sensitivity:<level>` tag. A `high` row's
     embedding rows and entity vectors are re-emitted with a tombstone — the
     sensitivity invariant, "a high row has no embedding row". A `medium`
     row's embedding rows are re-emitted with the tag, which is the only
     place the tier rides on the table search reads. Each relabelled row
     is announced to the namespace's webhooks as `memory.revised`.

Read-only by construction until `apply`; `scan` is what `--dry-run` prints.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from time import monotonic
from typing import Any

from server.core.datetimes import naive_utc_now
from server.core.repo_base import _CHClient, _default_client
from server.memory.app import webhooks
from server.memory.app.high_fence import fence_high, tombstone_chunks_of
from server.memory.domain.sensitivity import (
    ASSIGNMENT_KEYS,
    ASSIGNMENT_WORDS,
    HIGH,
    KEYED_SECRET_WORDS,
    MEDIUM,
    PIN_CODE_LABEL,
    PIN_LABEL,
    POSTCODE_LIKE_PIN_LABEL,
    SSN_KEYS,
    detect_sensitivity,
    detector_text,
    pin_grammar,
    sensitivity_tags,
)
from server.memory.domain.webhook_events import EventType, memory_change
from server.memory.store.sensitivity_sweep_sql import (
    AGENT_KEY,
    SORT_KEY,
    keys_sql,
    past_agent,
    scan_sql,
)

# The scan matches JSON text: the `json.dumps` text in `value`, and `toString(value_json)`
# for an object value. So an escape (`\n`, `\u000B`, `\/`) or `_` may start a PIN
# or IBAN label, and a control escape counts as whitespace, as the detector reads it
# decoded; `\v` is listed because RE2's `\s` lacks it.
_JSON_BOUNDARY = r"(?:\b|_|\\[bfnrt]|\\u[0-9a-fA-F]{4})"
_JSON_SPACE = r"(?:[\s\v]|\\[fnrt]|\\u000[bB])"
_PIN_FOR, _PIN_VALUE_AFTER = pin_grammar(_JSON_SPACE)
# `pin` and `pin code` take a 3-5 digit value, as the detector reads them, so a postal code
# (`PIN: 560001`, `PIN code 560001`) is not selected; every other label takes 3-8.
_POSTCODE_LIKE_PIN_VALUE = r"[0-9]{3,5}(?:[^0-9]|$)"
_COARSE_PIN = (
    rf"(?i:{_JSON_BOUNDARY}{PIN_LABEL}{_PIN_FOR}{_PIN_VALUE_AFTER}[0-9]{{3}}"
    rf"|{_JSON_BOUNDARY}{PIN_LABEL}{_JSON_SPACE}+[0-9]{{3}}"
    rf"|{_JSON_BOUNDARY}{POSTCODE_LIKE_PIN_LABEL}{_PIN_FOR}{_PIN_VALUE_AFTER}"
    rf"{_POSTCODE_LIKE_PIN_VALUE}"
    rf"|{_JSON_BOUNDARY}{PIN_CODE_LABEL}{_JSON_SPACE}+{_POSTCODE_LIKE_PIN_VALUE})"
)

# What separates a key's words, in JSON text: non-alphanumerics and escapes, up to the
# key's closing quote.
_KEY_SEP = r'(?:[^A-Za-z0-9"\\]|\\(?:["bfnrt/\\]|u[0-9a-fA-F]{4}))*'
_KEY_VALUE = rf'{_KEY_SEP}"{_JSON_SPACE}*:{_JSON_SPACE}*"?{_JSON_SPACE}*'


def _key_words(keys: Iterable[tuple[str, ...]]) -> str:
    """Key words as RE2, with a separator run between them, as the detector splits a key."""
    return "|".join(_KEY_SEP.join(words) for words in keys)


_PIN_KEYS = _key_words(w for w, label in KEYED_SECRET_WORDS.items() if label == "pin")
_OTHER_KEYS = _key_words(w for w, label in KEYED_SECRET_WORDS.items() if label != "pin")
# One character of a JSON string value as stored: a plain one or an escape.
_VALUE_CHAR = r'(?:[^\s"\\]|\\u[0-9a-fA-F]{4}|\\[\\/"b])'
# A credential key's words, with any separator run between them (`api.key`, `apiKey`).
_CREDENTIAL_KEYS = _key_words(ASSIGNMENT_KEYS)
# A key that ends in a label, then its value (3-5 digits under a `pin` key, so a postal
# code is not selected; 8 token characters under a credential key): a superset of the
# detector's key rule, as security.md describes.
_COARSE_KEYED = (
    rf"(?i:(?:{_PIN_KEYS}){_KEY_VALUE}[0-9]{{3,5}}(?:[^0-9]|$)"
    rf"|(?:{_OTHER_KEYS}|{_key_words(SSN_KEYS)}){_KEY_VALUE}[0-9]{{3}}"
    rf"|(?:{_CREDENTIAL_KEYS}){_KEY_VALUE}{_VALUE_CHAR}{{8}})"
)

# The whitespace the detector's Unicode `\s` reads (`str.isspace`; U+3000 is the last), as
# literal characters, since RE2's `\s` is ASCII and lacks `\v`; or a JSON escape.
_UNICODE_SPACES = "".join(c for c in map(chr, range(0x3001)) if c.isspace())
_ASSIGN_SPACE = rf"(?:[{_UNICODE_SPACES}]|\\[bfnrt]|\\u[0-9a-fA-F]{{4}})"
# A `password=…` assignment, as the detector reads one: the word, `:` or `=`, then 8
# characters of the value, which may start with a quote (`\"` in JSON text). So prose that
# only names the word ("I forgot my password") is not selected.
_COARSE_ASSIGNMENT = (
    rf"(?i:{_JSON_BOUNDARY}(?:{ASSIGNMENT_WORDS})\b{_ASSIGN_SPACE}*[:=]{_ASSIGN_SPACE}*"
    r"""(?:\\?"|')?[^\s"',;]{8})"""
)

# RE2, for ClickHouse `match()`: a superset of the shapes in
# `memory/domain/sensitivity.py`'s high and medium patterns, by the shape `_shapes` gives
# the row. Anything this misses the detector would miss too; anything it over-selects the
# detector drops.
_COARSE_BY_SHAPE = {
    "credential": (
        # the detector reads these prefixes at a word boundary, so `task-` and `disk-` are not
        rf"{_JSON_BOUNDARY}(?:sk-|pk-lf-|AKIA|gh[opusr]_|github_pat_|xox[abprs]-|eyJ|AIza)"
        r"|PRIVATE KEY"
        rf"|{_COARSE_ASSIGNMENT}"
        # A card number, as `_has_card` reads one: a lone 15- or 16-digit group, or a
        # printed grouping that starts with a 4-digit group, each with a network's leading
        # digit. So epoch milliseconds and 19-digit ids are not selected.
        # The left edge may be a `\uXXXX` escape, whose last hex digit can be a digit.
        r"|(?:^|[^0-9]|\\u[0-9a-fA-F]{4})[2-6][0-9]{14,15}(?:[^0-9]|$)"
        r"|(?:^|[^0-9]|\\u[0-9a-fA-F]{4})[2-6][0-9]{3}[ -][0-9]{4,6}[ -][0-9]{4}"
        r"|[0-9]{3}-[0-9]{2}-[0-9]{4}"  # SSN
        rf"|{_COARSE_PIN}"
        rf"|{_COARSE_KEYED}"
    ),
    "email": r"@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
    # the detector allows one Unicode space between groups, which JSON text may escape
    "phone": (
        rf"[0-9]{{3}}(?:[\s.()-]|{_ASSIGN_SPACE})*"
        rf"[0-9]{{3}}(?:[\s.-]|{_ASSIGN_SPACE})*[0-9]{{4}}"
    ),
    "other": rf"{_JSON_BOUNDARY}[A-Z]{{2}}[0-9]{{2}}(?: ?[A-Z0-9]{{4}}){{2}}",  # IBAN
}


def coarse_shape_re(shapes: set[str]) -> str:
    """The coarse filter for rows of the named shapes (every shape when none is named), so
    a pass that keeps only some shapes never selects a row it would drop: a phone-shaped
    row the scheduled pass leaves unlabelled would be selected again on every pass."""
    return "|".join(rx for shape, rx in _COARSE_BY_SHAPE.items() if not shapes or shape in shapes)


@dataclass
class SweepCandidate:
    memory_id: str
    org: str
    namespace: str
    agent: str
    user_id: str
    session_id: str
    memory_key: str
    level: str  # 'high' | 'medium'
    shapes: tuple[str, ...]  # 'credential' | 'email' | 'phone' | 'other'
    # The scanned row's sort key as stored: the fields above write an empty agent, user or
    # session as '-', which sorts after ''.
    sort_key: dict[str, Any]


@dataclass
class SweepReport:
    scanned: int = 0  # candidate rows the coarse filter returned
    candidates: list[SweepCandidate] = field(default_factory=list)
    # Whether the scan stopped before reading every key it meant to (pages, `limit` or
    # `seconds` ran out), or the candidates were cut to `limit`.
    truncated: bool = False
    # The sort key the next pass starts after: the last key read, or the last candidate's
    # when the candidates were cut to the limit, or its first columns alone (`past_agent`)
    # when the scan moved past that agent. None when the next pass should start at the
    # first key: the scan read from the first key to the last, or it reached the last key
    # with no page left to read the keys before its start.
    resume_after: dict[str, Any] | None = None
    # Whether the read that set `resume_after` stopped on a page with a stale key: one the
    # FINAL read gave no current row for, because its only match is an older version. Such
    # a page never moves the scan past its agent.
    last_page_had_stale_key: bool = False

    @property
    def by_level(self) -> Counter[str]:
        return Counter(c.level for c in self.candidates)

    @property
    def by_shape(self) -> Counter[str]:
        return Counter(s for c in self.candidates for s in c.shapes)

    def by_namespace(self) -> dict[tuple[str, str], Counter[str]]:
        out: dict[tuple[str, str], Counter[str]] = {}
        for c in self.candidates:
            out.setdefault((c.org, c.namespace), Counter())[c.level] += 1
        return out


def _shapes(text: str, level: str) -> tuple[str, ...]:
    shapes: list[str] = []
    # The `credential` shape names every row the detector reads as high, whatever it holds
    # (a key, a card number, an SSN, a stated PIN): the scheduled pass applies `credential`
    # and `email` rows by default. An IBAN-only row is `other`, applied by the operator's run.
    if level == HIGH:
        shapes.append("credential")
    from server.memory.domain.sensitivity import _MEDIUM_PATTERNS

    if _MEDIUM_PATTERNS[0].search(text):  # the detector's own email shape
        shapes.append("email")
    if _MEDIUM_PATTERNS[1].search(text):
        shapes.append("phone")
    return tuple(shapes) or ("other",)


async def scan(
    *,
    org: str | None = None,
    namespace: str | None = None,
    since_days: int | None = None,
    limit: int,
    page_size: int,
    pages: int | None,
    only_shapes: Iterable[str] | None = None,
    start_after: dict[str, Any] | None = None,
    skip_noisy_agents: bool = False,
    seconds: float | None = None,
    ch: _CHClient | None = None,
) -> SweepReport:
    """Live unlabelled rows the detector labels, read from `value` and an object's
    `value_json`. Read-only. `only_shapes` narrows the ClickHouse filter to those probe
    shapes' alternatives (`credential`, `email`, `phone`, `other`) and keeps only candidates
    that carry one of them.

    Reads pages of `page_size` keys in the table's sort key order, then the current version
    of each, until `limit` candidates are found, the rows run out, `pages` pages are read
    (`None`: no page cap) or `seconds` have passed (`None`: no time cap), starting after the
    sort key `start_after`, or past its columns when it holds the first ones only
    (`past_agent`), or at the first key when None. `skip_noisy_agents` is the scheduled
    pass's paging; its rules are in docs/architecture/security.md, "Sensitivity"."""
    ch = ch or _default_client()
    keep = {str(x).strip().lower() for x in (only_shapes or ()) if str(x).strip()}
    report = SweepReport()
    shape_re = coarse_shape_re(keep)
    if not shape_re:  # no shape the detector gives
        return report
    params: dict[str, Any] = {"shape": shape_re, "lim": page_size}
    if org:
        params["org"] = org
    if namespace:
        params["ns"] = namespace
    if since_days:
        params["days"] = int(since_days)
    run = _Run(
        ch=ch,
        params=params,
        scope={"org": org, "namespace": namespace, "since_days": since_days},
        keep=keep,
        limit=limit,
        pages=pages,
        report=report,
        stop_at=None if seconds is None else monotonic() + seconds,
    )
    start = dict(start_after or {})
    first = await _read_range(run, after=start, skip=skip_noisy_agents)
    ranges = [first]
    report.resume_after = first.last
    report.last_page_had_stale_key = first.last_page_had_stale_key
    if first.reached_end:
        # The scan reached the last key, so the next pass starts at the first. With
        # `start_after`, read the keys before it too, or they wait for a later pass and can
        # leave the window; once they are all read, the next pass starts where this one did.
        report.resume_after = None
        if start_after:
            wrap = await _read_range(run, after={}, stop=start, skip=skip_noisy_agents)
            ranges.append(wrap)
            report.resume_after = start if wrap.reached_end else wrap.last or None
            report.last_page_had_stale_key = wrap.last_page_had_stale_key
    # The rest of each agent the scan moved past, with the pages left. Every pass starts
    # these reads after the same page, since the cursor names no key inside an agent: a
    # row behind more than a pass's pages of such keys in its agent waits until they leave
    # the window, or for the operator's run. The cursor stays where the reads above left it.
    for key in run.skipped:
        ranges.append(await _read_range(run, after=key, stop=past_agent(key), skip=False))
    report.truncated = not all(r.reached_end for r in ranges)
    if len(report.candidates) > limit:
        report.candidates = report.candidates[:limit]
        report.truncated = True
        report.resume_after = report.candidates[-1].sort_key
        report.last_page_had_stale_key = False
    return report


@dataclass
class _Run:
    """One scan's reads, its caps, and the agents it moved past."""

    ch: _CHClient
    params: dict[str, Any]
    scope: dict[str, Any]
    keep: set[str]
    limit: int
    pages: int | None
    report: SweepReport
    # The `monotonic()` time the scan reads no more pages after; None for no time cap.
    stop_at: float | None = None
    pages_read: int = 0
    # The last key read in each agent the scan moved past, kept for this scan only: the
    # stored cursor names no user or memory key.
    skipped: list[dict[str, Any]] = field(default_factory=list)

    def more_wanted(self) -> bool:
        """Pages and time are left and fewer than `limit` candidates were found."""
        pages_left = self.pages is None or self.pages_read < self.pages
        time_left = self.stop_at is None or monotonic() < self.stop_at
        return pages_left and time_left and len(self.report.candidates) < self.limit


@dataclass
class _Range:
    """How one `_read_range` ended."""

    reached_end: bool  # the range's last key was read
    last: dict[str, Any]  # the key (or `past_agent` place) the next page would start after
    # The range stopped before its end on a page with a stale key in its last key's agent:
    # one the FINAL read gave no current row for, because its only match is an older version.
    last_page_had_stale_key: bool = False


async def _read_range(
    run: _Run,
    *,
    after: dict[str, Any],
    stop: dict[str, Any] | None = None,
    skip: bool,
) -> _Range:
    """Pages after `after` (from the first key when empty) and up to `stop`, while the scan
    wants more."""
    report = run.report
    last = dict(after)
    stale = False
    while run.more_wanted():
        run.pages_read += 1
        keys, rows = await _read_page(run.ch, run.params, run.scope, after=last, stop=stop)
        report.scanned += len(rows)
        found = len(report.candidates)
        _add_candidates(report, rows, run.keep)
        if len(keys) < run.params["lim"]:
            return _Range(reached_end=True, last=last)
        # A page moves the scan past its agent only when the FINAL read gave back a current
        # unlabelled row for each of its keys. A key whose only match is an older version
        # (a row `apply` labelled, or one deleted, before the parts merge) gives none, and
        # skipping on a page of those would leave the agent's later rows unread. The FINAL
        # read may also return keys written between the two reads, so compare keys, not
        # counts. Only the last key's agent counts: a stale key in an earlier agent sorts
        # before the cursor and does not hold the next agent back.
        last = keys[-1]
        current = _sort_keys(rows)
        stale = any(
            k not in current for k in _sort_keys(keys) if k[: len(AGENT_KEY)] == _agent(last)
        )
        if (
            skip
            and not stale
            and len(report.candidates) == found
            and _agent(keys[0]) == _agent(last)
        ):
            run.skipped.append(last)
            last = past_agent(last)
    return _Range(reached_end=False, last=last, last_page_had_stale_key=stale)


async def _read_page(
    ch: _CHClient,
    params: dict[str, Any],
    scope: dict[str, Any],
    *,
    after: dict[str, Any],
    stop: dict[str, Any] | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """One page: its sort keys after `after` (from the first key when empty) and up to
    `stop`, then the current version of each."""
    # `after` and `stop` hold the sort key, or the first columns of it when they name the
    # place past (or up to the end of) an agent, a namespace or an org.
    page_params = dict(params) | {f"after_{col}": value for col, value in after.items()}
    page_params |= {f"stop_{col}": value for col, value in (stop or {}).items()}
    sql = keys_sql(**scope, after=tuple(after), stop=tuple(stop or ()))
    keys = await ch.query_all(sql, params=page_params)
    if not keys:
        return [], []
    # The FINAL read takes the page's key range as a subquery, not its keys as a list: a
    # list of thousands of keys is a parameter too long for the request URL.
    page_params |= {f"upto_{col}": keys[-1][col] for col in SORT_KEY}
    rows = await ch.query_all(scan_sql(**scope, after=tuple(after)), params=page_params)
    return keys, rows


def _agent(key: dict[str, Any]) -> tuple[str, ...]:
    return tuple(key[col] for col in AGENT_KEY)


def _sort_keys(rows: list[dict[str, Any]]) -> set[tuple[str, ...]]:
    return {tuple(r[col] for col in SORT_KEY) for r in rows}


def scan_text(row: dict[str, Any]) -> str:
    """The text the detector reads for a scanned row: `value`, and `object_text` (value_json)
    when it holds an object, each decoded as the write path reads it (in the stored JSON a
    token after a newline follows `\\n`, and the detector's word boundary never matches
    there)."""
    texts = [row.get("value") or ""]
    if (row.get("object_text") or "{}") != "{}":
        texts.append(row["object_text"])
    return detector_text([_decoded(t) for t in texts])


def _decoded(text: str) -> Any:
    try:
        return json.loads(text)
    except (ValueError, RecursionError):  # raw text, or nested too deep to parse
        return text


def _add_candidates(report: SweepReport, rows: list[dict[str, Any]], keep: set[str]) -> None:
    for r in rows:
        text = scan_text(r)
        level = detect_sensitivity(text)
        if level not in (HIGH, MEDIUM):
            continue
        shapes = _shapes(text, level)
        if keep and not keep.intersection(shapes):
            continue
        report.candidates.append(
            SweepCandidate(
                memory_id=str(r["memory_id"]),
                org=str(r["org"]),
                namespace=str(r["namespace"]),
                agent=str(r.get("agent") or "-"),
                user_id=str(r.get("user_id") or "-"),
                session_id=str(r.get("session_id") or "-"),
                memory_key=str(r["memory_key"]),
                level=level,
                shapes=shapes,
                sort_key={col: r[col] for col in SORT_KEY},
            )
        )


@dataclass
class ApplyResult:
    relabelled: int = 0
    high: int = 0
    medium: int = 0
    embeddings_tombstoned: int = 0  # high rows' embedding rows
    embeddings_tagged: int = 0  # medium rows' embedding rows re-emitted with the tag
    failed: list[tuple[SweepCandidate, str]] = field(default_factory=list)  # with the error

    @property
    def errors(self) -> list[str]:
        return [error for _, error in self.failed]

    def as_dict(self) -> dict[str, Any]:
        return {
            "relabelled": self.relabelled,
            "high": self.high,
            "medium": self.medium,
            "embeddings_tombstoned": self.embeddings_tombstoned,
            "embeddings_tagged": self.embeddings_tagged,
            "errors": len(self.errors),
        }


async def _tag_embeddings(c: SweepCandidate, *, ch: _CHClient) -> int:
    """Re-emit a medium row's live embedding rows with the `sensitivity:`
    tag (the embeddings table has no sensitivity column; the ranking reads
    the tag)."""
    from server.memory.store import (
        EMBEDDING_PERSISTED_COLS,
        build_embedding_row,
        insert_embedding_rows,
        live_embedding_rows_for_key,
        reemit_version,
        retire_copies_under_unembedded_heads,
    )

    rows = await live_embedding_rows_for_key(
        ch,
        org=c.org,
        namespace=c.namespace,
        agent=c.agent,
        user_id=c.user_id,
        session_id=c.session_id,
        memory_key=c.memory_key,
    )
    out: list[dict[str, Any]] = []
    for r in rows:
        tags = list(r.get("tags") or [])
        extra = sensitivity_tags(c.level, tags)
        if not extra:
            continue
        out.append(
            build_embedding_row(
                **(
                    {col: val for col, val in r.items() if col in EMBEDDING_PERSISTED_COLS}
                    | {"tags": [*tags, *extra], "version": reemit_version(r)}
                )
            )
        )
    if out:
        await insert_embedding_rows(out, ch=ch)
        await retire_copies_under_unembedded_heads(out, ch=ch)
    return len(out)


async def _close_mined_facts(c: SweepCandidate, version: int, *, ch: _CHClient) -> None:
    """A row relabelled high: what its text gave the graph closes (its triples, the edges
    of entities not on the row); the edges of the entities on the row stay, its writer's
    or the extractor's when it tagged the row, as the row keeps those entities. `version`:
    the relabel's, which dates the dropped edges' end."""
    from server.memory.app.edge_links import drop_undeclared_edges
    from server.memory.app.projection_errors import ProjectionScope
    from server.memory.app.triple_projections import reconcile_row_facts

    scope = ProjectionScope(org=c.org, namespace=c.namespace, agent=c.agent, user_id=c.user_id)
    await drop_undeclared_edges(scope, {c.memory_id: version}, ch=ch, op="sensitivity_sweep")
    await reconcile_row_facts(scope, [c.memory_id], reopen=False, op="sensitivity_sweep", ch=ch)


async def apply(candidates: list[SweepCandidate], *, ch: _CHClient | None = None) -> ApplyResult:
    """Label each candidate as the write path would have: one new memory
    version, then the tier's mirror on the tables search reads."""
    import server.memory.store as memory_repo

    ch = ch or _default_client()
    result = ApplyResult()
    now = naive_utc_now()
    # Announced in one insert, in a `finally`: a pass cut short still announces
    # what it relabelled, since the next scan reads only unlabelled rows.
    relabelled: list[tuple[EventType, dict[str, Any]]] = []
    try:
        for c in candidates:
            try:
                version = memory_repo.next_version()  # the relabel's, which the high retire carries

                def _relabel(
                    existing: Any, _c: SweepCandidate = c, _v: int = version
                ) -> dict[str, Any]:
                    tags = list(getattr(existing, "tags", None) or [])
                    return {
                        "sensitivity": _c.level,
                        "tags": [*tags, *sensitivity_tags(_c.level, tags)],
                        "updated_at": now,
                        "version": _v,
                    }

                if c.level == HIGH:
                    # Vectors and chunks go first, at the relabel's version: if they fail,
                    # the row keeps its empty label and the next pass retries it (the
                    # scan only reads unlabelled rows).
                    key = (c.org, c.namespace, c.agent, c.user_id, c.session_id, c.memory_key)
                    retired = await memory_repo.retire_vectors_for_keys([(key, version)], ch=ch)
                    await fence_high([(key, version)], ch=ch)
                n = await memory_repo.write_versions_by_ids(c.org, [c.memory_id], _relabel, ch=ch)
                if not n:
                    continue  # gone between scan and apply
                result.relabelled += n
                relabelled.append(("memory.revised", memory_change(c, version)))
                if c.level == HIGH:
                    result.high += 1
                    result.embeddings_tombstoned += retired
                    # Again now the label is in: a chunk writer re-checks the head after its
                    # insert, and so does a reindex, so chunks or a vector that landed between
                    # the fence and the relabel saw an unlabelled head and stayed. The vector
                    # retire takes a version drawn now: a reindexed row sits above the fence.
                    await tombstone_chunks_of([key], ch=ch)
                    heads = await memory_repo.memory_rows_by_identity_keys(
                        [key], ch=ch, sequential=memory_repo.read_your_writes_enabled()
                    )
                    head = heads.get(key)
                    if head is not None and int(head.version) == version:  # still this relabel
                        await memory_repo.retire_vectors_for_keys(
                            [(key, memory_repo.next_version())], ch=ch
                        )
                    await _close_mined_facts(c, version, ch=ch)
                else:
                    result.medium += 1
                    result.embeddings_tagged += await _tag_embeddings(c, ch=ch)
            except Exception as e:  # noqa: BLE001 — one row's failure never stops the sweep
                result.failed.append((c, f"{c.namespace}/{c.memory_key}: {e}"))
    finally:
        await webhooks.announce(relabelled)
    return result


# The sensitivity invariant ("a high row has no embedding row"), as a count (SQL shared with
# the state probe's `loom.integrity.high_with_embedding` gauge).
from server.memory.store.integrity import HIGH_WITH_EMBEDDING_SQL  # noqa: E402


async def high_with_embedding_count(
    *, org: str | None = None, namespace: str | None = None, ch: _CHClient | None = None
) -> int:
    ch = ch or _default_client()
    where = ""
    params: dict[str, Any] = {}
    if org:
        where += " AND org = {org:String}"
        params["org"] = org
    if namespace:
        where += " AND namespace = {ns:String}"
        params["ns"] = namespace
    row = await ch.query_one(
        f"SELECT count() AS n FROM ({HIGH_WITH_EMBEDDING_SQL}) WHERE 1 {where}", params=params
    )
    return int((row or {}).get("n") or 0)


__all__ = [
    "HIGH_WITH_EMBEDDING_SQL",
    "ApplyResult",
    "SweepCandidate",
    "SweepReport",
    "apply",
    "coarse_shape_re",
    "high_with_embedding_count",
    "scan",
]
