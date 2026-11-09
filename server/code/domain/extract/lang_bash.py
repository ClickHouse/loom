"""Tree-sitter parsing for shell scripts (bash/sh) — fast tier only.

Same shapes as ``parse_cpp``/``parse_python``. Shell has no classes; the
symbols are function definitions, module-scoped exactly like Python's (see
lang_python's rationale — test trees define ``main``/``run`` in hundreds of
scripts, and path-free names would clobber under the RMT key). ``source``
/ ``.`` lines become include edges so "which scripts pull in this helper
library" is answerable.
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Any

from server.code.domain.extract import bounded_parse
from server.code.domain.extract.lang_python import module_scope
from server.code.domain.extract.treesitter import ParsedFile, Symbol, node_text


@lru_cache(maxsize=1)
def _bash_parser() -> Any:
    import tree_sitter
    import tree_sitter_bash

    return tree_sitter.Parser(tree_sitter.Language(tree_sitter_bash.language()))


_SOURCE_RE = re.compile(r"^[^\S\n]*(?:source|\.)\s+([\w./$-]+)", re.MULTILINE)


def parse_bash(path: str, source: bytes) -> ParsedFile:
    parser = _bash_parser()
    tree = bounded_parse.parse(parser, source)
    pf = ParsedFile(path=path, lang="shell", tree=tree, source=source)
    mod = module_scope(path)

    pf.includes = [
        t for t in _SOURCE_RE.findall(source.decode("utf-8", "replace"))
        if "$" not in t  # a variable-interpolated target is not resolvable
    ]

    def walk(node: Any) -> None:
        for child in node.named_children:
            if child.type == "function_definition":
                name_node = child.child_by_field_name("name")
                if name_node is not None:
                    name = node_text(name_node, source)
                    pf.symbols.append(
                        Symbol(
                            qualified_name=f"{mod}::{name}",
                            name=name,
                            kind="function",
                            signature="",
                            path=path,
                            start_line=child.start_point[0] + 1,
                            end_line=child.end_point[0] + 1,
                        )
                    )
            walk(child)

    walk(tree.root_node)
    return pf
