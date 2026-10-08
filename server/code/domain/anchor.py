"""Code anchors — the primitive under the staleness oracle.

A memory that mentions code ("the fix is in resolveStorages.cpp") holds a
point-in-time claim that silently rots as the repo changes. To check it against
the live code index we must first turn the prose claim into an EXACT, resolved
anchor — because re-resolving a bare name at read time is irreducibly noisy:
common method names (`getValue`, `update`, `getColumn`) resolve to dozens of
definitions, so a "did it move?" check on the bare name false-flags constantly.

The fix is to resolve and disambiguate each claim against the index, yielding a
`symbol_id` (+ canonical path/line) that a verification can key on without
homonym noise. This module is that resolve/verify logic, kept network-free and
pure so the disambiguation rules are unit-testable; the caller supplies the
code.symbol definition rows (see server/code/app/symbol/symbol.py).

Disambiguation rule (the whole point): only anchor when the claim resolves
UNAMBIGUOUSLY — the claimed file matches exactly one current definition, or the
symbol has exactly one definition anywhere. A homonym that matches nothing
claimed is SKIPPED, never anchored — better no anchor than a false stale flag.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# A source path anywhere in the tree, and a code-shaped identifier. Kept strict
# (>=5 chars, real code shapes) so prose words never become spurious claims.
_PATH = re.compile(r"(?:[A-Za-z0-9_]+/)*[A-Za-z0-9_]+\.(?:cpp|h|hpp|cc)")
_SYM = re.compile(
    r"[A-Za-z_][A-Za-z0-9_]*(?:::[A-Za-z0-9_~]+)+"  # Ns::qualified::name
    r"|[a-z][a-z0-9]*[A-Z][A-Za-z0-9]{2,}"  # camelCase
    r"|[a-z]+_[a-z_]{3,}"  # snake_case
)
_PROX = 60  # a symbol must sit within this many chars of the path to pair


def _base(path: str) -> str:
    return (path or "").rsplit("/", 1)[-1]


@dataclass(frozen=True)
class CodeClaim:
    """A raw (symbol, file) claim lifted from memory text by proximity — not yet
    resolved. `claimed_path` is as-written (may be a bare basename)."""

    symbol: str
    claimed_path: str


@dataclass(frozen=True)
class CodeAnchor:
    """A resolved, disambiguated anchor that can be re-verified by symbol_id.
    `symbol_id` is the index's stable id; `path`/`line` are its canonical
    location at anchor time. `claim_matched` is True when the memory's claimed
    file agreed with the resolved location (a claim we can later detect drifting
    away from)."""

    symbol: str
    symbol_id: str
    path: str
    line: int
    claim_matched: bool
    # sha256 of the symbol's defining chunk at anchor time; "" = unknown
    # (an older anchor, or the index had no tagged chunk).
    content_hash: str = ""


def extract_claims(text: str, *, max_claims: int = 20) -> list[CodeClaim]:
    """Proximity extraction: pair a path with code-shaped symbols adjacent to it.
    Deduped, order-preserving, capped. Cartesian pairing is deliberately avoided
    — a symbol far from the path is not treated as a claim about it."""
    if not text:
        return []
    out: list[CodeClaim] = []
    seen: set[tuple[str, str]] = set()
    for pm in _PATH.finditer(text):
        s, e = pm.start(), pm.end()
        window = text[max(0, s - _PROX) : s] + " " + text[e : e + _PROX]
        for raw in _SYM.findall(window):
            sym = raw.split("::")[-1]
            if len(sym) < 5:
                continue
            key = (pm.group(0), sym)
            if key in seen:
                continue
            seen.add(key)
            out.append(CodeClaim(symbol=sym, claimed_path=pm.group(0)))
            if len(out) >= max_claims:
                return out
    return out


def _defs_view(defs: list[dict[str, Any]]) -> list[tuple[str, str, int, str]]:
    """(symbol_id, base_path, line, content_hash) per definition row from
    code.symbol. The hash is the unit's `body_hash` (or a row's
    `content_hash`), "" when the index did not supply one."""
    view = []
    for d in defs or []:
        view.append(
            (
                str(d.get("symbol_id") or d.get("unit_id") or ""),
                _base(str(d.get("path") or "")),
                int(d.get("start_line") or 0),
                str(d.get("content_hash") or d.get("body_hash") or ""),
            )
        )
    return view


def resolve_anchor(claim: CodeClaim, defs: list[dict[str, Any]]) -> CodeAnchor | None:
    """Turn a raw claim into an anchor, or None to SKIP (unresolvable / ambiguous
    homonym). `defs` are code.symbol(claim.symbol) definition rows.

    - claimed file matches exactly one definition -> anchor there, matched=True
    - symbol has exactly one definition anywhere    -> anchor there, matched=(file agrees)
    - otherwise (0 defs, or >1 with no claimed match) -> None (skip; a homonym we
      refuse to anchor rather than risk a false stale flag)
    """
    view = _defs_view(defs)
    if not view:
        return None
    want = _base(claim.claimed_path)
    matching = [v for v in view if v[1] == want]
    if len(matching) == 1:
        sid, path, line, h = matching[0]
        return CodeAnchor(claim.symbol, sid, path, line, claim_matched=True, content_hash=h)
    if len(matching) > 1:
        return None  # claimed file itself has homonyms — ambiguous, skip
    if len(view) == 1:
        sid, path, line, h = view[0]
        return CodeAnchor(claim.symbol, sid, path, line, claim_matched=False, content_hash=h)
    return None  # multiple defs, none in the claimed file -> ambiguous homonym
