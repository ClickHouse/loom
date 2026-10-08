"""Provenance tier of a typed triple.

A triple in `loom.entity_triples` comes from one of three places:

- ``extracted``: a `(subject, verb, object)` the extractor returned whose
  subject and object both appear verbatim in the fact sentence that carries
  it. The graph edge restates the text; a reader can check it.
- ``inferred``: a relation the extractor returned whose endpoints are not
  both in the carrier text: a resolved coreference, a canonical name the
  sentence never spells, or a model inference. Useful, less trustworthy.
- ``facet``: a code-native or topic edge loom itself mints from the fact's
  `files`, `concepts` and `topics` (`<actor> references <file>`,
  `<actor> about <topic>`), where the subject is the writing agent, not a
  thing the text talks about. These are navigation edges, not facts.

The stored `confidence` cannot tell these apart; this tier can,
deterministically and with no model call. An unset tier reads as inferred.
"""

from __future__ import annotations

import re

EXTRACTED = "extracted"
INFERRED = "inferred"
FACET = "facet"

_WS = re.compile(r"\s+")
_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)


def normalize(text: str) -> str:
    """Lower-case, `_` and punctuation to spaces, whitespace collapsed: the
    form both a canonical entity (`new_york`) and prose (`New York,`) reach."""
    t = str(text or "").lower().replace("_", " ")
    t = _PUNCT.sub(" ", t)
    return _WS.sub(" ", t).strip()


def endpoint_in_text(endpoint: str, text_norm: str) -> bool:
    """Whether a canonical endpoint occurs in already-normalised text as a
    whole token sequence (so `ph d` matches "Ph.D." and `art` does not match
    "party")."""
    e = normalize(endpoint)
    if not e or not text_norm:
        return False
    return f" {e} " in f" {text_norm} "


def classify(subject: str, obj: str, text_norm: str) -> str:
    """EXTRACTED when both endpoints occur in the carrier text (already
    `normalize`d, so a caller classifying many triples normalises once),
    else INFERRED."""
    if endpoint_in_text(subject, text_norm) and endpoint_in_text(obj, text_norm):
        return EXTRACTED
    return INFERRED


def carrier_text(value: object) -> str:
    """The prose a triple can be checked against: a string value as is; a
    structured value flattened to its string leaves."""
    if isinstance(value, str):
        return value
    parts: list[str] = []

    def _walk(v: object) -> None:
        if isinstance(v, str):
            parts.append(v)
        elif isinstance(v, dict):
            for k, x in v.items():
                parts.append(str(k))
                _walk(x)
        elif isinstance(v, (list, tuple)):
            for x in v:
                _walk(x)
        elif v is not None:
            parts.append(str(v))

    _walk(value)
    return " ".join(parts)
