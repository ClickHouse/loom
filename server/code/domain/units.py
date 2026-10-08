"""CodeUnit assembly and token budgeting for code.* responses.

The quality contract: a result is always a syntactically
complete unit with a file:line pointer — and the response fits the caller's
token budget by ELIDING whole units (signature + pointer survive, body
drops), never by cutting a unit in half. Precision retrieval is itself a
quality feature: a tight, relevant context beats a 30K-token dump.
"""

from __future__ import annotations

import json
import re
from pathlib import PurePosixPath
from typing import Any

from pydantic import BaseModel, Field, SerializerFunctionWrapHandler, model_serializer

# Characters per token by content kind, measured with o200k_base over 300 randomly
# sampled chunks per kind from a ClickHouse index (cl100k within 1%): cpp 4.33, h 4.38,
# py 4.20, md 4.23, json 4.75 — the plain bytes/4 is right, slightly conservative, for
# those — but sql 3.43, sh 3.68, xml 3.74, yaml 2.78, where bytes/4 UNDER-counts by
# 14–44%. Kinds that measured at or above 4 keep 4.0 (a low estimate is the one that
# breaks a budget).
CHARS_PER_TOKEN: dict[str, float] = {
    "sql": 3.4,
    "sh": 3.7,
    "xml": 3.7,
    "yaml": 2.8,
    "default": 4.0,
}
_KIND_BY_EXT: dict[str, str] = {
    "sql": "sql",
    "sh": "sh",
    "bash": "sh",
    "zsh": "sh",
    "xml": "xml",
    "yaml": "yaml",
    "yml": "yaml",
}


def kind_of_path(path: str) -> str:
    ext = PurePosixPath(path or "").suffix.lstrip(".").lower()
    return _KIND_BY_EXT.get(ext, "default")


def estimate_tokens(text: str, path: str = "") -> int:
    """Token estimate for raw text of the kind `path` implies (default 4
    chars/token). Serving cost of a UNIT goes through estimate_unit_tokens,
    which counts the JSON envelope the agent actually ingests."""
    return int(len(text) / CHARS_PER_TOKEN.get(kind_of_path(path), 4.0))


def _json_len(value: Any) -> int:
    return len(json.dumps(value, separators=(",", ":"), ensure_ascii=False))


def estimate_json_tokens(value: Any) -> int:
    """Wire cost of a JSON value the agent ingests: the serialized length at the default
    rate (the envelope — keys, quotes, escapes — is what is paid, not the bare text).
    Used for rows that are not units: outline entries, candidate lists."""
    return int(_json_len(value) / 4.0)


def estimate_unit_tokens(unit: CodeUnit, *, body: str | None = None) -> int:
    """What serving this unit costs the agent: the JSON-serialized fields
    (keys, quotes, escaped newlines and quotes in the body) at the body's
    kind rate. Measured on 50 real code.search responses: the envelope is
    +46% over the bare bodies, and sum(len(body)//4) under-counted the
    served response by 22%; len(json)//4 was within 3.4%."""
    b = unit.body if body is None else body
    meta = unit.model_dump(exclude={"body"})
    meta_cost = _json_len(meta) / 4.0
    body_cost = (_json_len(b) - 2) / CHARS_PER_TOKEN.get(kind_of_path(unit.path), 4.0) if b else 0.0
    return int(meta_cost + body_cost)


# Enrichment fields that are omitted from the wire when they hold their
# empty default. They are enrichment around the unit's core shape (risk
# annotations, lessons, cards) and every consumer reads them optionally; the core
# fields — identity, signature, body, elided, tier, score, description — are always
# present so a client can index `hit["elided"]` directly. Measured: a
# card serialised with every default was 322 chars (~80 tokens) of which
# 189 were empty enrichment; dropping them costs the agent nothing and saves
# ~47 tokens per unit, every unit, every call.
_OPTIONAL_WHEN_EMPTY: frozenset[str] = frozenset(
    {
        "body_hash",
        "recent_prs",
        "examples",
        "lessons",
        "churn",
        "amp",
        "tested",
        "callees",
        "granularity",
        "next",
        "outline",
        "fan_in",
        "via",
        "lexical_rank",
        "vector_rank",
        "namespace",
    }
)

# Every served unit says which leg found it. `via` is one of VIA_VALUES; the ranks are
# 1-based positions inside the leg's pool before fusion (0 = the leg did not return this
# chunk). Computed from the leg results BEFORE the fusion priors reorder anything, so
# they answer "what did each leg say", not "where did it end up".
VIA_VALUES: tuple[str, ...] = ("lexical", "vector", "both", "symbol")
_DEFAULT_GRANULARITY = "unit"


class CodeUnit(BaseModel):
    """One whole semantic unit of code, as returned by every code.* op."""

    @model_serializer(mode="wrap")
    def _compact(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        data: dict[str, Any] = handler(self)
        for k in _OPTIONAL_WHEN_EMPTY:
            v = data.get(k)
            if v in ("", 0, False, [], None) or (k == "granularity" and v == _DEFAULT_GRANULARITY):
                data.pop(k, None)
        if self.granularity == "skeleton":
            data["outline"] = data.get("outline", [])  # the key is the contract, even when empty
        return data

    unit_id: str = ""  # chunk_id or symbol_id
    path: str
    start_line: int = 0
    end_line: int = 0
    symbol: str = ""  # qualified name of the unit's scope ('' for file-level)
    kind: str = ""  # function | class_skeleton | file_header | other | symbol
    signature: str = ""
    body: str = ""  # whole unit, or "" when elided
    elided: bool = False
    # sha256 of the unit's defining chunk in the index ("" when unknown). What
    # a memory anchors to so "the code you describe was rewritten" is one
    # comparison later, independent of whether the body was served.
    body_hash: str = ""
    description: str = ""
    tier: str = ""  # scip | treesitter | '' (chunks carry no tier)
    score: float = 0.0
    # enrichment blocks (code.symbol fills these; empty lists otherwise)
    recent_prs: list[dict[str, Any]] = Field(default_factory=list)
    examples: list[dict[str, Any]] = Field(default_factory=list)
    lessons: list[dict[str, Any]] = Field(default_factory=list)
    # risk annotations + one-hop context (code.search fills these when
    # annotate=True; see code/app/risk.py for exact semantics — churn
    # is a floor bounded by PR-history coverage, tested means "a mapped
    # in-process test reaches this path", amp is 1-hop caller count)
    churn: int = 0
    amp: int = 0
    tested: bool = False
    callees: list[dict[str, Any]] = Field(default_factory=list)
    # how the body was served: "unit" (whole semantic unit), "card"
    # (signature+doc+pointer, body elided), "file" (expanded to the whole
    # indexed file under a generous budget), "skeleton" (a large class or
    # struct: the class text with method bodies elided, plus `outline`)
    granularity: str = "unit"
    # the one pasteable escalation when the body was withheld
    next: str = ""
    # granularity="skeleton" only: the container's member declarations in
    # source order — {kind, qualified_name, signature, start_line, end_line,
    # tier} — each one code.symbol call away. Empty otherwise (omitted).
    outline: list[dict[str, Any]] = Field(default_factory=list)
    # How much of the repo depends on this unit: live call sites reaching its symbol
    # with test/example/fuzzer/vendor call sites and symbols outside the repo excluded
    # (loom.code_symbol_stats, recomputed weekly). 0 = none, or the stats are not
    # enabled — dropped from the wire either way. Differs from `amp`, which is the
    # one-hop caller count the risk annotator reads live from code_edges.
    fan_in: int = 0
    # provenance: which leg found this unit and where each leg ranked it; dropped from
    # the wire when empty
    via: str = ""
    lexical_rank: int = 0
    vector_rank: int = 0
    # which namespace served this unit: set only by a code.search over several
    # namespaces; "" (off the wire) otherwise
    namespace: str = ""

    @property
    def pointer(self) -> str:
        return f"{self.path}:{self.start_line}-{self.end_line}"


def unit_from_chunk(row: dict[str, Any], *, score: float = 0.0) -> CodeUnit:
    return CodeUnit(
        unit_id=str(row.get("chunk_id") or ""),
        path=str(row.get("path") or ""),
        start_line=int(row.get("start_line") or 0),
        end_line=int(row.get("end_line") or 0),
        symbol=str(row.get("scope") or ""),
        kind=str(row.get("kind") or ""),
        body=str(row.get("content") or ""),
        description=str(row.get("context_description") or ""),
        score=round(score, 4),
    )


_SCIP_PLACEHOLDER_SIG = re.compile(r"^\(scip:[0-9a-f]+\)$")


def readable_signature(value: object) -> str:
    """A signature a reader can use, or "". The compiler tier stores an overload hash as
    its signature (`(scip:49f6e7a0…)`): an identity, not a signature. Served as-is, a
    model reads the hash as the signature, fetches the body to find the real one, then
    greps the checkout."""
    sig = str(value or "")[:300]
    return "" if _SCIP_PLACEHOLDER_SIG.match(sig) else sig


def unit_from_symbol(row: dict[str, Any], *, body: str = "") -> CodeUnit:
    return CodeUnit(
        unit_id=str(row.get("symbol_id") or ""),
        path=str(row.get("path") or ""),
        start_line=int(row.get("start_line") or 0),
        end_line=int(row.get("end_line") or 0),
        symbol=str(row.get("qualified_name") or ""),
        kind="symbol",
        signature=readable_signature(row.get("signature")),
        body=body,
        description=str(row.get("doc_comment") or "")[:500],
        tier=str(row.get("tier") or ""),
    )


def budget_units(
    units: list[CodeUnit], token_budget: int, *, bodies: str = "auto"
) -> tuple[list[CodeUnit], int]:
    """Two-pass budget fill. Returns (units, tokens_used).

    Pass 1 reserves HEAD room (signature + doc + pointer) for as many ranked units as
    fit, so every requested slot is represented at least elided. Pass 2 spends the
    remainder on whole bodies by rank. A single pass would break off entirely once fat
    top bodies exhausted the budget — an agent asking for 10 results would silently get
    6, losing the tail pointers (a rank-7 correct answer dropped for want of its
    ~40-token head while rank-3's 1.5K-token body was kept).

    bodies="none" skips pass 2 entirely: every unit ships as a signature+ doc+pointer
    card, elided, with get-the-body one op away. Serving skeleton cards instead of
    bodies cuts ~70-80% of served tokens without hurting localization — the right
    default for survey-shaped queries where the agent will only ever open one of the k
    results.
    """
    # A head is the unit as its elided card would serve — pointer, next
    # hint, JSON envelope included — so the reservation is the wire cost.
    heads = [estimate_unit_tokens(_card_of(u)) for u in units]
    kept = 0
    reserved = 0
    for h in heads:
        if reserved + h > token_budget:
            break
        reserved += h
        kept += 1

    used = reserved
    out: list[CodeUnit] = []
    for u, _h in zip(units[:kept], heads[:kept], strict=True):
        body_cost = estimate_unit_tokens(u) - _h if u.body else 0
        if not u.body:
            out.append(u)  # bodyless (symbol units): whole, not "elided"
        elif bodies != "none" and used + body_cost <= token_budget:
            out.append(u)
            used += body_cost
        else:
            out.append(_card_of(u))
    return out, used


def _card_of(u: CodeUnit) -> CodeUnit:
    """The unit as an elided card: signature + doc + pointer, body dropped,
    plus the one pasteable escalation (measured to reduce agent flailing
    when a card wins): exact-name symbol fetch, or the pointer form for
    scope-less chunks."""
    return u.model_copy(
        update={
            "body": "",
            "elided": True,
            "granularity": "card",
            "next": (
                f"code.outline path={u.path}"
                if u.outline or u.granularity == "skeleton"
                else f"code.symbol name={u.symbol}"
                if u.symbol
                else f"code.grep pattern=<literal> path_prefix={u.path}"
            ),
        }
    )
