"""A statement's text as telemetry: its shape digest (appended to
`log_comment` when `ch_log_comment_statement_digest` is on) and the opt-in
capture of every executed statement for the analyzer corpus."""

from __future__ import annotations

import hashlib
import json
import logging
import re

log = logging.getLogger(__name__)


_DIGEST_HEAD_CHARS = 200
_DIGEST_WS = re.compile(r"\s+")
# Quoted strings, then numbers (a vector literal is thousands of them): the
# shape of a statement is what remains once its literals are masked.
_DIGEST_LITERALS = re.compile(r"'(?:[^'\\]|\\.)*'|-?\b\d+(?:\.\d+)?(?:[eE][+-]?\d+)?\b")


def _flat(sql: str) -> str:
    return _DIGEST_WS.sub(" ", str(sql or "")).strip()


def _shape(flat: str) -> str:
    return "sql:" + hashlib.sha1(_DIGEST_LITERALS.sub("?", flat).encode("utf-8")).hexdigest()[:12]


def statement_shape(sql: str) -> str:
    """`sql:<shape12>`: a 12-hex sha1 of the statement with string and
    numeric literals masked, so it holds none of them. Two searches with
    different query vectors share a shape; two statements that differ in
    their SELECT list or joins do not."""
    return _shape(_flat(sql))


def statement_digest(sql: str) -> str:
    """`statement_shape(sql)` then the first 200 characters, whitespace collapsed."""
    flat = _flat(sql)
    return f"{_shape(flat)} {flat[:_DIGEST_HEAD_CHARS]}"


def capture_statement(sql: str, path: str) -> None:
    """Append one JSON line per executed statement to `path`
    (`LOOM_CH_CAPTURE_STATEMENTS`) — `digest` and the text — so an acceptance run
    feeds the analyzer corpus the shapes production actually executes. A write
    failure never fails the statement."""
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"digest": statement_digest(sql), "sql": sql}) + "\n")
    except OSError as exc:  # pragma: no cover - a full disk is not a query failure
        log.debug("statement capture skipped: %s", exc)


__all__ = ["capture_statement", "statement_digest", "statement_shape"]
