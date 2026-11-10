"""Tree-sitter parsing for Rust.

Same ``ParsedFile`` / ``Symbol`` shapes as the other fast-tier parsers.

* **Qualified names are crate paths.** Rust's own module system names a
  definition ``crate::capture::sse::SseDecoder::push``; the index writes
  ``<crate>::<module path>::<Type>::<fn>`` with the crate named after the
  directory that holds ``src/`` (``ai-gateway``, ``native``), the module
  path read from the file path (``mod.rs`` / ``lib.rs`` / ``main.rs`` name
  their directory, ``foo.rs`` names ``foo``), inline ``mod x { … }`` blocks
  adding a segment. No `.` ever appears, so the language-class gate
  reads every Rust definition as `native` — the class it shares with C++,
  the other compiled language.
* **Items** — ``fn`` (free, in ``impl``, in ``trait`` with a default body;
  a bodiless trait method is a declaration, `has_body=False`) →
  ``function``; ``struct`` → ``struct``; ``enum`` → ``enum``; ``trait`` →
  ``trait``; ``type`` → ``type``; ``const`` / ``static`` → ``const``;
  ``macro_rules!`` → ``macro``; ``mod`` → ``module``. An ``impl`` block is
  an ``impl`` symbol (``…::SseDecoder::impl`` for the inherent block,
  ``…::SseDecoder::impl Sink`` for a trait impl) so its method signatures
  chunk as one skeleton with the bodies elided, like a class; its methods
  are ``…::SseDecoder::push`` — the same name whether the type's inherent
  impl or its trait impl defines them, disambiguated by signature and, for
  a trait impl, by the trait name in the signature.
* **A trait impl for a type this file defines is an `inherits` edge**
  (struct → trait), the closest shape the edge table has for "implements".
* **``use`` and ``mod`` declarations are include targets**: ``use
  crate::a::b::C`` names ``src/a/b.rs`` or ``src/a/b/mod.rs`` (the deepest
  path that is a module; the last segment is usually an item, so both the
  full path and its parent are tried), ``use super::x`` resolves against
  the parent module, ``mod foo;`` names the sibling ``foo.rs`` /
  ``foo/mod.rs``. External crates (``std``, ``tokio``, ``serde``) resolve
  to nothing and are not edges. ``resolve_uses`` binds them against the
  namespace's live paths like `lang_typescript.resolve_imports`.
* **Doc comments** are the ``///`` run (or ``/** */``) above the item;
  attributes between the docs and the item are stepped over.

Outside the extractor stamp with the other lane modules (see
``tests/unit/test_code_extractor_stamp.py``): files no parser touched
before have no rows to heal; a fix here is applied by ``loom code backfill
--paths-file`` over a namespace's Rust tree.
"""

from __future__ import annotations

import posixpath
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from server.code.domain.extract import bounded_parse
from server.code.domain.extract.treesitter import ParsedFile, Symbol, node_text

LANG_RUST = "rust"
KIND_IMPL = "impl"
KIND_TRAIT = "trait"
KIND_MACRO = "macro"
_MAX_DOC = 2000
_FILE_MODULES = ("mod.rs", "lib.rs", "main.rs")
_MAX_CANDIDATES_PER_USE = 12
# a leaf's path repeats every prefix above it: a long prefix times many leaves is a copy each
_MAX_USE_CHARS = 256


@lru_cache(maxsize=1)
def _parser() -> Any:
    import tree_sitter
    import tree_sitter_rust

    return tree_sitter.Parser(tree_sitter.Language(tree_sitter_rust.language()))


def crate_scope(path: str) -> tuple[str, str]:
    """(crate name, module path) of a file: `ai-gateway/src/capture/sse.rs`
    → (`ai-gateway`, `capture::sse`); `src/lib.rs` at the root → (`crate`,
    ''); a file outside any `src/` (`build.rs`, `tests/x.rs`) is a module of
    its directory."""
    parts = path.split("/")
    if "src" in parts:
        i = parts.index("src")
        crate = parts[i - 1] if i > 0 else "crate"
        mods = parts[i + 1 :]
    else:
        # `packages/native/build.rs`, `tests/x.rs`: the directory is the crate
        crate = parts[-2] if len(parts) > 1 else "crate"
        mods = parts[-1:]
    if mods and mods[-1] in _FILE_MODULES:
        mods = mods[:-1]
    elif mods and mods[-1].endswith(".rs"):
        mods = [*mods[:-1], mods[-1][:-3]]
    return crate, "::".join(m for m in mods if m)


def module_scope_rust(path: str) -> str:
    crate, mods = crate_scope(path)
    return f"{crate}::{mods}" if mods else crate


def _doc_above(node: Any, source: bytes) -> str:
    """`///` lines (or one `/** */`) right above the item, attributes skipped."""
    parts: list[str] = []
    sib = node.prev_named_sibling
    expected_end = node.start_point[0]
    while sib is not None:
        if sib.type == "attribute_item":
            expected_end = sib.start_point[0]
            sib = sib.prev_named_sibling
            continue
        if sib.type not in {"line_comment", "block_comment"}:
            break
        if expected_end - sib.end_point[0] > 2:
            break
        text = node_text(sib, source).rstrip()
        is_doc = text.startswith(("///", "/**")) and not text.startswith("////")
        if not is_doc:
            break
        parts.append(text)
        expected_end = sib.start_point[0]
        sib = sib.prev_named_sibling
    return "\n".join(reversed(parts))[:_MAX_DOC] if parts else ""


def _type_name(node: Any, source: bytes) -> str:
    """`Wrapper<T>` → Wrapper, `a::b::C` → C, `&mut X` → X."""
    if node is None:
        return ""
    t = node.type
    if t in {"generic_type", "reference_type", "dynamic_type", "abstract_type"}:
        inner = node.child_by_field_name("type") or next(
            (c for c in node.named_children if c.type != "type_arguments"), None
        )
        return _type_name(inner, source)
    if t == "scoped_type_identifier":
        name = node.child_by_field_name("name")
        return node_text(name, source) if name is not None else node_text(node, source)
    return node_text(node, source)


def _signature(fn: Any, source: bytes) -> str:
    params = fn.child_by_field_name("parameters")
    ret = fn.child_by_field_name("return_type")
    sig = node_text(params, source) if params is not None else "()"
    if ret is not None:
        sig += " -> " + node_text(ret, source)
    return sig


@dataclass
class _Walker:
    pf: ParsedFile
    source: bytes
    mod: str
    uses: list[str] = field(default_factory=list)
    _impls: dict[str, int] = field(default_factory=dict)

    def _add(
        self,
        scope: tuple[str, ...],
        name: str,
        kind: str,
        signature: str,
        node: Any,
        doc: str = "",
        has_body: bool = True,
    ) -> str:
        qname = "::".join((self.mod, *scope, name))
        self.pf.symbols.append(
            Symbol(
                qualified_name=qname,
                name=name,
                kind=kind,
                signature=signature,
                path=self.pf.path,
                start_line=node.start_point[0] + 1,
                end_line=node.end_point[0] + 1,
                doc_comment=doc,
                has_body=has_body,
            )
        )
        return qname

    def walk(self, node: Any, scope: tuple[str, ...]) -> None:
        for child in node.named_children:
            self._item(child, scope)

    def _item(self, node: Any, scope: tuple[str, ...]) -> None:
        t = node.type
        name_node = node.child_by_field_name("name")
        name = node_text(name_node, self.source) if name_node is not None else ""
        doc = _doc_above(node, self.source)
        if t == "function_item" and name:
            self._add(scope, name, "function", _signature(node, self.source), node, doc)
        elif t == "function_signature_item" and name:
            self._add(
                scope, name, "function", _signature(node, self.source), node, doc, has_body=False
            )
        elif t == "struct_item" and name:
            self._add(scope, name, "struct", "", node, doc)
        elif t == "union_item" and name:
            self._add(scope, name, "struct", "", node, doc)
        elif t == "enum_item" and name:
            self._add(scope, name, "enum", "", node, doc)
        elif t == "trait_item" and name:
            self._add(scope, name, KIND_TRAIT, "", node, doc)
            self._body(node, (*scope, name))
        elif t == "type_item" and name:
            self._add(scope, name, "type", "", node, doc)
        elif t in {"const_item", "static_item"} and name:
            self._add(scope, name, "const", "", node, doc)
        elif t == "macro_definition" and name:
            self._add(scope, name, KIND_MACRO, "", node, doc)
        elif t == "mod_item" and name:
            self._module(node, scope, name, doc)
        elif t == "impl_item":
            self._impl(node, scope, doc)
        elif t == "use_declaration":
            arg = node.child_by_field_name("argument")
            if arg is not None:
                self.uses.extend(_use_paths(arg, self.source))
        elif t in {"foreign_mod_item", "extern_block"}:
            self._body(node, scope)

    def _body(self, node: Any, scope: tuple[str, ...]) -> None:
        body = node.child_by_field_name("body") or next(
            (c for c in node.named_children if c.type == "declaration_list"), None
        )
        if body is not None:
            self.walk(body, scope)

    def _module(self, node: Any, scope: tuple[str, ...], name: str, doc: str) -> None:
        body = node.child_by_field_name("body")
        if body is None:
            self.uses.append(f"mod {name}")  # `mod foo;` names a sibling file
            return
        self._add(scope, name, "module", "", node, doc)
        self.walk(body, (*scope, name))

    def _impl(self, node: Any, scope: tuple[str, ...], doc: str) -> None:
        type_node = node.child_by_field_name("type")
        trait_node = node.child_by_field_name("trait")
        type_name = _type_name(type_node, self.source)
        if not type_name:
            return
        trait_name = _type_name(trait_node, self.source) if trait_node is not None else ""
        header = node_text(node, self.source).split("{", 1)[0].strip()
        base = f"impl {trait_name}" if trait_name else "impl"
        n = self._impls.get((*scope, type_name, base).__repr__(), 0)
        self._impls[(*scope, type_name, base).__repr__()] = n + 1
        impl_name = base if n == 0 else f"{base}#{n + 1}"
        self._add((*scope, type_name), impl_name, KIND_IMPL, header, node, doc)
        if trait_name:
            struct_qname = "::".join((self.mod, *scope, type_name))
            if any(
                s.qualified_name == struct_qname and s.kind in {"struct", "enum"}
                for s in self.pf.symbols
            ):
                self.pf.inherits.append((struct_qname, trait_name))
        self._body(node, (*scope, type_name))


def _use_paths(arg: Any, source: bytes) -> list[str]:
    """The module paths a `use` argument names, one per leaf: `a::{b, c::D}`
    → `a::b`, `a::c::D`; `a::*` → `a`; `a as b` → `a`."""
    t = arg.type
    if t == "scoped_use_list":
        path = arg.child_by_field_name("path")
        prefix = node_text(path, source) if path is not None else ""
        lst = arg.child_by_field_name("list")
        out: list[str] = []
        for item in lst.named_children if lst is not None else []:
            for p in _use_paths(item, source):
                if len(prefix) + len(p) < _MAX_USE_CHARS:
                    out.append(f"{prefix}::{p}" if prefix else p)
        return out
    if t == "use_list":
        out = []
        for item in arg.named_children:
            out.extend(_use_paths(item, source))
        return out
    if t == "use_as_clause":
        path = arg.child_by_field_name("path")
        return _use_paths(path, source) if path is not None else []
    if t == "use_wildcard":
        inner = arg.named_children[0] if arg.named_children else None
        return [node_text(inner, source)] if inner is not None else []
    if t in {"scoped_identifier", "identifier", "crate", "self", "super"}:
        return [node_text(arg, source)]
    return []


def parse_rust(path: str, source: bytes) -> ParsedFile:
    tree = bounded_parse.parse(_parser(), source)
    pf = ParsedFile(path=path, lang=LANG_RUST, tree=tree, source=source)
    w = _Walker(pf=pf, source=source, mod=module_scope_rust(path))
    w.walk(tree.root_node, ())
    pf.includes = list(dict.fromkeys(w.uses))
    return pf


# --------------------------------------------------------------------- uses


def _src_root(path: str) -> str:
    """The `src/` directory of the file's crate, or its directory."""
    parts = path.split("/")
    if "src" in parts:
        return "/".join(parts[: parts.index("src") + 1])
    return posixpath.dirname(path)


def _module_files(base: str) -> list[str]:
    return [base + ".rs", posixpath.join(base, "mod.rs")]


def use_candidates(from_path: str, use_path: str) -> list[str]:
    """Repository files a `use` / `mod` path may name, deepest first: the
    full path (when its last segment is a module), then its parent (the
    last segment is usually an item). Pure."""
    root = _src_root(from_path)
    here = posixpath.dirname(from_path)
    if use_path.startswith("mod "):
        name = use_path[4:].strip()
        base = here if posixpath.basename(from_path) in _FILE_MODULES else from_path[:-3]
        return _module_files(posixpath.join(base, name))
    segs = [s for s in use_path.replace("{", "").replace("}", "").split("::") if s]
    if not segs:
        return []
    head, rest = segs[0], segs[1:]
    if head == "crate":
        base = root
    elif head in {"self", "super"}:
        base = here if posixpath.basename(from_path) in _FILE_MODULES else from_path[:-3]
        for s in segs:
            if s == "super":
                base = posixpath.dirname(base)
        rest = [s for s in segs if s not in {"self", "super"}]
    else:
        # `use foo::bar` may name a sibling module `foo` of the current
        # module, or the crate root's `foo` (2018 edition path resolution)
        if posixpath.basename(from_path) in _FILE_MODULES:
            base = here
        else:
            base = from_path[:-3]
        rest = segs
    bases = (base,) if head in {"crate", "self", "super"} else (base, root)
    # the deepest stems up to the cap only: a long path has one per segment
    out: dict[str, None] = {}
    for b in bases:
        for n in range(len(rest), 0, -1):
            out.update(dict.fromkeys(_module_files(posixpath.join(b, *rest[:n]))))
            if len(out) >= _MAX_CANDIDATES_PER_USE:
                return list(out)[:_MAX_CANDIDATES_PER_USE]
    return list(out)


def resolve_uses(from_path: str, uses: list[str], present: set[str]) -> list[str]:
    """The indexed module files the `use` / `mod` paths name (one per path,
    the deepest that exists). Pure; `present` is the oracle."""
    out: dict[str, None] = {}
    for u in uses:
        hit = next((c for c in use_candidates(from_path, u) if c in present), None)
        if hit and hit != from_path:
            out[hit] = None
    return list(out)


def wanted_paths(from_path: str, uses: list[str]) -> list[str]:
    out: dict[str, None] = {}
    for u in uses:
        out.update(dict.fromkeys(use_candidates(from_path, u)))
    return list(out)


__all__ = [
    "KIND_IMPL",
    "KIND_MACRO",
    "KIND_TRAIT",
    "LANG_RUST",
    "crate_scope",
    "module_scope_rust",
    "parse_rust",
    "resolve_uses",
    "use_candidates",
    "wanted_paths",
]
