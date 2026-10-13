"""A file's definitions, fingerprinted and shaped, from the symbols its parse found."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from typing import Any

from server.code.domain.anchor import NO_SHAPE, Definition, fingerprint
from server.code.domain.declaration import Declarations, joined


def definitions(
    path: str, source: bytes, symbols: Sequence[Any], tree: Any = None, *, named: str = ""
) -> list[Definition]:
    """Each of `symbols` (the index's, of `source`) as a definition fingerprinted with
    every overload of its name; with `named`, only those of that bare name, and without
    rename shapes. `tree` is the index's parse of `source`, reused when it can be."""
    groups: dict[str, list[Any]] = defaultdict(list)
    for sym in sorted(symbols, key=lambda x: x.start_line):
        if not named or sym.name == named:
            groups[sym.qualified_name].append(sym)
    decls = Declarations.parse(path, source, tree)
    lines = source.decode("utf-8", "replace").split("\n")
    out: list[Definition] = []
    for qn, syms in groups.items():
        starts = [decls.start(s.start_line) if decls else s.start_line for s in syms]
        # every declaration of the name in its scopes (overload signatures, repeats, one
        # per `#if` branch), which the index folds or numbers
        at = [s.start_line for s in syms]
        prints = decls.fingerprints(at, syms[0].name, shapes=not named) if decls else None
        if prints:
            body, shape, shown = prints
        else:
            # each declaration on its own; its lines when the parser cannot single it out
            texts = [
                (decls.text(s.start_line, s.name) if decls else None)
                or "\n".join(lines[b - 1 : s.end_line])
                for b, s in zip(starts, syms, strict=True)
            ]
            printed = [
                (decls.printed(s.start_line, s.name) if decls else None) or text
                for s, text in zip(syms, texts, strict=True)
            ]
            body = fingerprint(printed)
            shape = joined(
                [
                    decls.shape(s.start_line, s.name) if decls and not named else NO_SHAPE
                    for s in syms
                ]
            )
            shown = "\n\n".join(
                (decls.shown(s.start_line, s.name) if decls else None) or text
                for s, text in zip(syms, texts, strict=True)
            )
        out.extend(
            Definition(
                path=path,
                qualified_name=qn,
                name=s.name,
                symbol_id=s.id,
                start_line=b,
                end_line=s.end_line,
                body_hash=body,
                shape=shape,
                kind=s.kind,
                shown=shown,
            )
            for b, s in zip(starts, syms, strict=True)
        )
    return out
