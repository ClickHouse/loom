"""The mention lines of a symbol's name (code.symbol `uses`): the grep over the
namespace's chunks, each line classified as a comment, an include or code.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from server.code.app import symbol_uses as _uses_brief
from server.code.app.spans import code_span, span_attrs
from server.code.domain.extract.treesitter import lang_for_path
from server.code.domain.search_scope import LANG_EXTENSIONS, build_scope
from server.code.store.index import CodeRepo

logger = logging.getLogger(__name__)


def _bare_names(rows: list[dict[str, Any]]) -> list[str]:
    """Distinct bare names of the resolved definitions, in order: what a
    grep for the symbol's uses matches on. A qualified name greps its tail
    (`DB::IColumn::insert` -> `insert`); a `path:line` lookup resolves to
    rows whose own names are used. Lambdas (`$lambda`) and one-character
    names are skipped: their mentions are not uses of anything."""
    out: list[str] = []
    for r in rows:
        name = str(r.get("name") or "") or str(r.get("qualified_name") or "").split("::")[-1]
        name = name.strip()
        if len(name) < 2 or name.startswith("$") or name in out:
            continue
        out.append(name)
    return out[:3]


_COMMENT_STARTS = ("//", "/*", "*", "#", "--", "'''", '"""')


_INCLUDE_STARTS = ("#include", "import ", "from ", "#pragma", "using ")


def _line_kind(text: str) -> str:
    """Lexical label of a matching LINE (not of the use): `include` for an
    include / import line, `comment` for a line that is a comment from its
    first character, `code` otherwise. Cheap, deterministic, and enough to
    let a consumer drop the mentions that cannot be call sites."""
    t = text.lstrip()
    if t.startswith(_INCLUDE_STARTS):
        return "include"
    if t.startswith(_COMMENT_STARTS):
        return "comment"
    return "code"


def _uses_lines(
    chunks: list[dict[str, Any]],
    needles: list[str],
    spans: dict[tuple[str, str], tuple[int, int]],
) -> list[dict[str, Any]]:
    """Matching lines of the fetched chunks, the shape code.grep serves
    (path, line, text, scope, scope span) plus `kind`. Mirrors the grep
    handler's extraction so a line grep returns is a line this returns:
    case-sensitive literal, (path, line) deduped across windows of one
    oversized unit, the synthetic continuation header dropped."""
    from server.code.app.grep import _CONTINUED_PREFIX

    out: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    for c in chunks:
        lines = str(c["content"]).splitlines()
        if lines and lines[0].startswith(_CONTINUED_PREFIX):
            lines = lines[1:]
        scope = str(c.get("scope") or "")
        span = spans.get((str(c["path"]), scope), (0, 0)) if scope else (0, 0)
        for off, line in enumerate(lines):
            if not any(n in line for n in needles):
                continue
            key = (str(c["path"]), int(c.get("start_line") or 1) + off)
            if key in seen:
                continue
            seen.add(key)
            out.append(
                {
                    "path": key[0],
                    "line": key[1],
                    "text": line.strip()[:300],
                    "scope": scope,
                    "scope_start_line": span[0],
                    "scope_end_line": span[1],
                    "kind": _line_kind(line),
                }
            )
    return out


async def _symbol_uses(
    repo: CodeRepo,
    org: str,
    ns: str,
    rows: list[dict[str, Any]],
    *,
    limit: int,
    scope: Any = None,
) -> tuple[list[dict[str, Any]], bool]:
    """The `uses` leg of code.symbol: the lines mentioning the definitions' bare names,
    read from the chunk index the way code.grep reads them (case-sensitive literal, 3x
    over-fetch), with the enclosing unit's span attached, MINUS every line inside a
    returned definition's own span. Path/line ordered, capped at `limit`; the flag says
    when the cap or the chunk over-fetch held. Never raises."""
    from server.code.app.grep import _unit_spans

    needles = _bare_names(rows)
    if not needles:
        return [], False
    chunk_limit = min(200, limit * 3)
    try:
        with code_span("symbol.uses", results=len(needles)) as sp:
            fetched = await asyncio.gather(
                *(
                    repo.grep_chunks(
                        org, ns, n, case_sensitive=True, limit=chunk_limit, scope=scope
                    )
                    for n in needles
                )
            )
            chunks = [c for part in fetched for c in part]
            spans = await _unit_spans(repo, org, ns, chunks)
            span_attrs(sp, chunks_matched=len(chunks))
    except Exception as e:  # noqa: BLE001 — additive, never fatal
        logger.debug("code.symbol uses leg failed: %s", e)
        return [], False
    definition_spans = [
        (str(r.get("path") or ""), int(r.get("start_line") or 0), int(r.get("end_line") or 0))
        for r in rows
    ]
    lines = [
        u
        for u in _uses_lines(chunks, needles, spans)
        if not any(p == u["path"] and lo <= u["line"] <= hi for p, lo, hi in definition_spans)
    ]
    lines.sort(key=lambda u: (u["path"], u["line"]))
    truncated = len(lines) > limit or any(len(part) >= chunk_limit for part in fetched)
    return lines[:limit], truncated


async def _mention_leg(
    repo: CodeRepo, org: str, ns: str, rows: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], bool]:
    """The compact block's read for a TYPE: the `uses` grep for the name's mentions,
    fetched for the top of the block only and scoped to the definition's language with the
    noise directories excluded. The chunk read is path-ordered, so an unscoped small fetch
    of `Settings` would return `ci/*.py` and a README first."""
    langs = [
        lang
        for lang in {lang_for_path(str(r.get("path") or "")) for r in rows}
        if lang in LANG_EXTENSIONS
    ]
    scope = build_scope(langs=langs, exclude_globs=list(_uses_brief.MENTION_EXCLUDE_GLOBS))
    return await _symbol_uses(repo, org, ns, rows, limit=2 * _uses_brief.TOP_N, scope=scope)
