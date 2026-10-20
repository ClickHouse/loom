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
     place the tier rides on the table search reads.

Read-only by construction until `apply`; `scan` is what `--dry-run` prints.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from server.core.datetimes import naive_utc_now
from server.core.repo_base import _CHClient, _default_client
from server.memory.app.high_fence import fence_high, tombstone_chunks_of
from server.memory.domain.sensitivity import (
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
from server.memory.store.sensitivity_sweep_sql import SORT_KEY, keys_sql, scan_sql

# The scan matches JSON text: the `json.dumps` text in `value`, and `toString(value_json)`
# for an object value. So an escape (`\n`, `\u000B`, `\/`) or `_` may start a PIN
# or IBAN label, and a control escape counts as whitespace, as the detector reads it
# decoded; `\v` is listed because RE2's `\s` lacks it.
_JSON_BOUNDARY = r"(?:\b|_|\\[bfnrt]|\\u[0-9a-fA-F]{4})"
_JSON_SPACE = r"(?:[\s\v]|\\[fnrt]|\\u000[bB])"
_PIN_FOR, _PIN_VALUE_AFTER = pin_grammar(_JSON_SPACE)
_COARSE_PIN = (
    rf"(?i:{_JSON_BOUNDARY}(?:{POSTCODE_LIKE_PIN_LABEL}|{PIN_LABEL})"
    rf"{_PIN_FOR}{_PIN_VALUE_AFTER}[0-9]{{3}}"
    rf"|{_JSON_BOUNDARY}(?:{PIN_CODE_LABEL}|{PIN_LABEL})"
    rf"{_JSON_SPACE}+[0-9]{{3}})"
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
# A key that ends in a label, then its value (3-5 digits under a `pin` key, so a postal
# code is not selected): a superset of the detector's key rule, as security.md describes.
_COARSE_KEYED = (
    rf"(?i:(?:{_PIN_KEYS}){_KEY_VALUE}[0-9]{{3,5}}(?:[^0-9]|$)"
    rf"|(?:{_OTHER_KEYS}|{_key_words(SSN_KEYS)}){_KEY_VALUE}[0-9]{{3}})"
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
        r"|(?:^|[^0-9])[2-6][0-9]{14,15}(?:[^0-9]|$)"
        r"|(?:^|[^0-9])[2-6][0-9]{3}[ -][0-9]{4,6}[ -][0-9]{4}"
        r"|[0-9]{3}-[0-9]{2}-[0-9]{4}"  # SSN
        rf"|{_COARSE_PIN}"
        rf"|{_COARSE_KEYED}"
    ),
    "email": r"@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
    "phone": r"[0-9]{3}[\s.()-]*[0-9]{3}[\s.-]*[0-9]{4}",
    "other": rf"{_JSON_BOUNDARY}[A-Z]{{2}}[0-9]{{2}}(?: ?[A-Z0-9]{{4}}){{2}}",  # IBAN
}
COARSE_SHAPE_RE = "|".join(_COARSE_BY_SHAPE.values())


def coarse_shape_re(shapes: set[str]) -> str:
    """The coarse filter for rows of the named shapes (every shape when none is named), so
    a pass that keeps only some shapes never selects a row it would drop: a phone-shaped
    row the scheduled pass leaves unlabelled would be selected again on every pass."""
    return "|".join(rx for shape, rx in _COARSE_BY_SHAPE.items() if not shapes or shape in shapes)


# A probe shape reported beside the detector's levels, so a dry run says how many
# e-mail-shaped rows it found.
_PROBE_EMAIL = r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"


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


@dataclass
class SweepReport:
    scanned: int = 0  # candidate rows the coarse filter returned
    candidates: list[SweepCandidate] = field(default_factory=list)
    truncated: bool = False

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
    import re

    shapes: list[str] = []
    # Every row the detector reads as high is a credential, whatever its shape (a key, a card
    # number, an SSN, a stated PIN): the scheduled pass applies `credential` and `email` rows
    # by default. An IBAN-only row is `other`, applied by the operator's run.
    if level == HIGH:
        shapes.append("credential")
    if re.search(_PROBE_EMAIL, text):
        shapes.append("email")
    from server.memory.domain.sensitivity import _MEDIUM_PATTERNS

    if _MEDIUM_PATTERNS[1].search(text):
        shapes.append("phone")
    return tuple(shapes) or ("other",)


async def scan(
    *,
    org: str | None = None,
    namespace: str | None = None,
    since_days: int | None = None,
    limit: int = 20000,
    pages: int | None,
    only_shapes: Iterable[str] | None = None,
    ch: _CHClient | None = None,
) -> SweepReport:
    """Live unlabelled rows the detector would label, exactly as the write
    path would have. Read-only. `only_shapes` keeps candidates carrying at
    least one of the named probe shapes (`credential`, `email`, `phone`,
    `other`) — the phone shape is a ten-digit run and the noisiest, so an
    operator can apply the others first.

    Reads pages of `limit` keys in the table's sort key order, then the current version of
    each, until `limit` candidates are found, the rows run out or `pages` pages are read
    (`None`: no page cap). Only rows of the shapes in `only_shapes` are selected. A selected
    row the detector leaves unlabelled stays unlabelled and is selected again on the next
    run; reading past it keeps a run of them from hiding a later row that is high."""
    ch = ch or _default_client()
    keep = {str(x).strip().lower() for x in (only_shapes or ()) if str(x).strip()}
    report = SweepReport()
    shape_re = coarse_shape_re(keep)
    if not shape_re:  # no shape the detector gives
        return report
    params: dict[str, Any] = {"shape": shape_re, "lim": limit}
    if org:
        params["org"] = org
    if namespace:
        params["ns"] = namespace
    if since_days:
        params["days"] = int(since_days)
    last: dict[str, Any] = {}  # the last key of the page before
    pages_read = 0
    while pages is None or pages_read < pages:
        pages_read += 1
        after = bool(last)
        page_params = dict(params)
        if after:
            page_params |= {f"after_{col}": last[col] for col in SORT_KEY}
        sql = keys_sql(org=org, namespace=namespace, since_days=since_days, after=after)
        keys = await ch.query_all(sql, params=page_params)
        rows: list[dict[str, Any]] = []
        if keys:
            # The FINAL read takes the page's key range as a subquery, not its keys as a list:
            # a list of thousands of keys is a parameter too long for the request URL.
            page_params |= {f"upto_{col}": keys[-1][col] for col in SORT_KEY}
            sql = scan_sql(org=org, namespace=namespace, since_days=since_days, after=after)
            rows = await ch.query_all(sql, params=page_params)
        report.scanned += len(rows)
        report.truncated = len(keys) >= limit
        _add_candidates(report, rows, keep)
        if not report.truncated or len(report.candidates) >= limit:
            break
        last = keys[-1]
    if len(report.candidates) > limit:
        report.candidates = report.candidates[:limit]
        report.truncated = True
    return report


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
            )
        )


@dataclass
class ApplyResult:
    relabelled: int = 0
    high: int = 0
    medium: int = 0
    embeddings_tombstoned: int = 0  # high rows' embedding rows
    embeddings_tagged: int = 0  # medium rows' embedding rows re-emitted with the tag
    errors: list[str] = field(default_factory=list)

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


async def apply(candidates: list[SweepCandidate], *, ch: _CHClient | None = None) -> ApplyResult:
    """Label each candidate as the write path would have: one new memory
    version, then the tier's mirror on the tables search reads."""
    import server.memory.store as memory_repo

    ch = ch or _default_client()
    result = ApplyResult()
    now = naive_utc_now()
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
            else:
                result.medium += 1
                result.embeddings_tagged += await _tag_embeddings(c, ch=ch)
        except Exception as e:  # noqa: BLE001 — one row's failure never stops the sweep
            result.errors.append(f"{c.namespace}/{c.memory_key}: {e}")
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
    "COARSE_SHAPE_RE",
    "HIGH_WITH_EMBEDDING_SQL",
    "ApplyResult",
    "SweepCandidate",
    "SweepReport",
    "apply",
    "coarse_shape_re",
    "high_with_embedding_count",
    "scan",
]
