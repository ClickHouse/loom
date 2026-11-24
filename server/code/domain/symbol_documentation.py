"""The symbol description preview and its relationship to returned source."""

from __future__ import annotations

import ast
import re
from inspect import cleandoc
from textwrap import dedent

SYMBOL_DESCRIPTION_PREVIEW_CHARS = 500


def symbol_description_preview(doc_comment: str) -> str:
    """Return the description carried by a symbol unit."""
    return doc_comment[:SYMBOL_DESCRIPTION_PREVIEW_CHARS]


_DOC_MARKUP = re.compile(
    r"(?<!`)(?::[A-Za-z][\w-]*:)?(?P<literal>(?P<ticks>`+)[^`]+(?P=ticks))(?!`)"
    r"|(?P<unmatched>`+)|\\_|&nbsp;"
)


def _fold_doc_markup(text: str, *, rendered: bool) -> str | None:
    """Fold spacing and escapes from SCIP's rendered source documentation."""
    uncertain = False

    def fold(match: re.Match[str]) -> str:
        nonlocal uncertain
        if literal := match.group("literal"):
            uncertain |= len(literal.splitlines()) != 1
            return match.group() if rendered else literal
        if match.group("unmatched"):
            uncertain = True
        if rendered:
            return "_" if match.group() == r"\_" else " "
        return match.group()

    folded = _DOC_MARKUP.sub(fold, text)
    return None if uncertain else "\n".join(line.rstrip() for line in folded.splitlines())


def _python_doc(body: str, signature: str) -> str | None:
    source = dedent(body)
    try:
        declarations = ast.parse(source).body
    except (SyntaxError, ValueError, RecursionError):
        return None
    if len(declarations) != 1 or not isinstance(
        declaration := declarations[0], (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    ):
        return None
    header = source.splitlines()[declaration.lineno - 1 : declaration.body[0].lineno - 1]
    if "\n".join(header).strip() != signature:
        return None
    doc = ast.get_docstring(declaration, clean=False)
    return cleandoc(doc) if doc is not None and "\t" not in doc else None


def description_repeats_body(description: str, body: str, *, scip: bool) -> bool:
    """Check that the body carries the same declaration and doc excerpt."""
    reached_preview_limit = len(description) == SYMBOL_DESCRIPTION_PREVIEW_CHARS
    description = description.strip()
    if not description:
        return False
    if description in body:
        return True
    if not scip or not description.startswith("```python\n"):
        return False
    header, closing, excerpt = description.partition("\n```\n")
    signature = header.partition("\n")[2].strip()
    if not closing or not signature or signature not in body:
        return False
    if "```" in excerpt or any(line.startswith(("    ", "\t")) for line in excerpt.splitlines()):
        return False
    doc = _python_doc(body, signature)
    if doc is None:
        return False
    folded_excerpt = _fold_doc_markup(excerpt, rendered=True)
    source = _fold_doc_markup(doc, rendered=False)
    return bool(
        folded_excerpt
        and source
        and (
            source.startswith(folded_excerpt) if reached_preview_limit else source == folded_excerpt
        )
    )
