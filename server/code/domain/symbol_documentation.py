"""The symbol description preview and its relationship to returned source."""

from __future__ import annotations

import ast
import re
from inspect import cleandoc
from textwrap import dedent

SYMBOL_DESCRIPTION_PREVIEW_CHARS = 500
# The extra AST comparison runs on the synchronous MCP reply path.
_STRUCTURAL_DOC_BODY_MAX_CHARS = 16_000


def symbol_description_preview(doc_comment: str) -> str:
    """Return the description carried by a symbol unit."""
    return doc_comment[:SYMBOL_DESCRIPTION_PREVIEW_CHARS]


_DOC_MARKUP = re.compile(
    r"(?<!`)(?::[A-Za-z][\w-]*:)?(?P<literal>(?P<ticks>`+)[^`]+(?P=ticks))(?!`)"
    r"|(?P<unmatched>`+)|\\_|&nbsp;"
)
_Function = ast.FunctionDef | ast.AsyncFunctionDef
_Declaration = ast.ClassDef | _Function


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


def _declaration(source: str) -> _Declaration | None:
    try:
        declarations = ast.parse(source).body
    except (SyntaxError, ValueError, RecursionError):
        return None
    if len(declarations) != 1 or not isinstance(declaration := declarations[0], _Declaration):
        return None
    return declaration


def _python_doc(body: str, signature: str) -> str | None:
    source = dedent(body)
    declaration = _declaration(source)
    if declaration is None:
        return None
    header = source.splitlines()[declaration.lineno - 1 : declaration.body[0].lineno - 1]
    if "\n".join(header).strip() != signature:
        return None
    doc = ast.get_docstring(declaration, clean=False)
    return cleandoc(doc) if doc is not None and "\t" not in doc else None


def _simple_forward_reference(value: ast.expr | None) -> ast.expr | None:
    if (
        isinstance(value, ast.Constant)
        and isinstance(value.value, str)
        and value.value.isidentifier()
    ):
        return ast.Name(id=value.value, ctx=ast.Load())
    return value


def _function_header(declaration: _Function) -> str:
    # SCIP can wrap the declaration or unquote simple forward references.
    args = declaration.args
    for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg]:
        if arg is not None:
            arg.annotation = _simple_forward_reference(arg.annotation)
    declaration.returns = _simple_forward_reference(declaration.returns)
    declaration.body = []
    return ast.dump(declaration)


def _same_raw_function_doc(body: str, signature: str, excerpt: str, *, preview: bool) -> bool:
    if (
        len(body) > _STRUCTURAL_DOC_BODY_MAX_CHARS
        or not signature.endswith(":")
        or "#" in signature
    ):
        return False
    rendered = _declaration(signature + "\n    pass\n")
    if (
        not isinstance(rendered, _Function)
        or len(rendered.body) != 1
        or not isinstance(rendered.body[0], ast.Pass)
    ):
        return False
    source = _declaration(dedent(body))
    if not isinstance(source, _Function):
        return False
    doc = ast.get_docstring(source, clean=False)
    try:
        if _function_header(source) != _function_header(rendered):
            return False
    except RecursionError:
        return False
    if not excerpt:
        return doc is None
    if doc is None or "\t" in doc or "\t" in excerpt:
        return False
    source_doc, shown_doc = cleandoc(doc), cleandoc(excerpt)
    return bool(
        shown_doc and (source_doc.startswith(shown_doc) if preview else source_doc == shown_doc)
    )


def _same_rendered_doc(body: str, signature: str, excerpt: str, *, preview: bool) -> bool:
    if signature not in body or "```" in excerpt:
        return False
    if any(line.startswith(("    ", "\t")) for line in excerpt.splitlines()):
        return False
    doc = _python_doc(body, signature)
    if doc is None:
        return False
    folded_excerpt = _fold_doc_markup(excerpt, rendered=True)
    source = _fold_doc_markup(doc, rendered=False)
    return bool(
        folded_excerpt
        and source
        and (source.startswith(folded_excerpt) if preview else source == folded_excerpt)
    )


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
    header, closing, suffix = description.removeprefix("```python\n").partition("\n```")
    signature = header.strip()
    if not closing or not signature or (suffix and not suffix.startswith("\n")):
        return False
    excerpt = suffix.removeprefix("\n")
    return _same_rendered_doc(body, signature, excerpt, preview=reached_preview_limit) or (
        _same_raw_function_doc(body, signature, excerpt, preview=reached_preview_limit)
    )
