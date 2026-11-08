"""Tree-sitter parsing and symbol extraction for the fast tier.

Extracts function/class/struct/enum symbols with qualified names from C++
sources without compiling. This tier is name-resolved (approximate); the
scip tier is compiler-resolved (exact). Both tiers key rows by
``symbol_id()`` defined HERE — the single shared hashing function — so a
symbol they agree on lands under the same id (overloads carry the signature
hash, so `read(int)` and `read(String)` never collapse).

`.h` files are forced through the C++ grammar (never C): ClickHouse headers
are C++, and the C grammar silently drops classes/namespaces.

tree_sitter / tree_sitter_cpp are imported lazily so the server can boot
without the `code` extras when the code workers are disabled.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from server.code.domain.extract import bounded_parse

_CPP_EXTS = {".cpp", ".cc", ".cxx", ".h", ".hpp", ".hxx", ".ipp", ".inl"}

_WS_RE = re.compile(r"\s+")


def lang_for_path(path: str) -> str:
    """Language tag stored on code_files. cpp/python/shell parse to symbols
    and edges (see lang_python/lang_bash for the non-C++ parsers); sql,
    config, and doc chunk line-wise without symbols BY DESIGN — they have
    no symbol semantics worth graphing."""
    lower = path.lower()
    dot = lower.rfind(".")
    ext = lower[dot:] if dot >= 0 else ""
    if ext in _CPP_EXTS:
        return "cpp"
    if ext in {".py"}:
        return "python"
    if ext in {".sql"}:
        return "sql"
    if ext in {".md", ".rst"}:
        return "doc"
    if ext in {".xml", ".yml", ".yaml", ".json", ".toml", ".ini", ".cmake"} or lower.endswith(
        "cmakelists.txt"
    ):
        return "config"
    if ext in {".sh", ".bash"}:
        return "shell"
    return ""


def normalize_signature(sig: str) -> str:
    """Whitespace-collapsed signature used in the symbol_id hash.

    Default-argument values are stripped (`= expr` to the next ',' or ')')
    because the scip side never sees them; parameter names stay — imperfect
    cross-tier agreement is accepted (reads merge by qualified_name with
    tier preference), but within a tier this is deterministic and stable.
    """
    sig = _WS_RE.sub(" ", sig).strip()
    # Strip default-argument values with paren/angle awareness: skip from
    # '=' to the next ',' or the ')' that closes the parameter list, so a
    # nested call like `int b = f(x)` doesn't eat the closing paren.
    out: list[str] = []
    depth = 0
    i = 0
    while i < len(sig):
        ch = sig[i]
        if ch in "(<":
            depth += 1
        elif ch in ")>":
            depth -= 1
        if ch == "=" and depth >= 1:
            skip_depth = depth
            i += 1
            while i < len(sig):
                c = sig[i]
                if c in "(<":
                    skip_depth += 1
                elif c in ")>":
                    skip_depth -= 1
                    if skip_depth < 1:
                        break  # closing paren of the parameter list
                elif c == "," and skip_depth == 1:
                    break
                i += 1
            continue
        out.append(ch)
        i += 1
    sig = "".join(out)
    sig = re.sub(r"\s*([(),<>&*])\s*", r"\1", sig)
    return sig


def symbol_id(qualified_name: str, signature: str) -> str:
    """128-bit hex id keyed on (qualified_name, normalized signature).

    The ONE shared function: the treesitter extractor, the SCIP delta
    converter, and the read path all call this — never inline the hash.
    """
    key = f"{qualified_name}|{normalize_signature(signature)}"
    return hashlib.sha256(key.encode("utf-8", "replace")).hexdigest()[:32]


def file_symbol_id(path: str) -> str:
    """Pseudo symbol id for file-level edges (edge_kind='includes')."""
    return hashlib.sha256(f"file:{path}".encode("utf-8", "replace")).hexdigest()[:32]


_FILE_UNIT_LANGS = frozenset({"typescript", "javascript", "python"})


def file_unit(pf: ParsedFile) -> FileUnit | None:
    """The file itself as the unit of its top-level code —
    the caller of a module-level `register()` and of the calls inside an
    anonymous callback handed to `app.get(...)`. Its id is the file pseudo
    symbol the `includes` edges already use, so importing the module and
    running its top level are edges of one node. Script languages only: a
    C++ or Go file has no code outside its declarations."""
    if pf.lang not in _FILE_UNIT_LANGS:
        return None
    lines = pf.source.count(b"\n") + 1
    return FileUnit(
        qualified_name=pf.path, name=pf.path.rsplit("/", 1)[-1], kind="file", signature="",
        path=pf.path, start_line=1, end_line=lines,
    )


@dataclass(frozen=True)
class Symbol:
    qualified_name: str
    name: str
    kind: str  # function | class | struct | enum
    signature: str
    path: str
    start_line: int  # 1-indexed, inclusive
    end_line: int
    doc_comment: str = ""
    # False only for body-less declarations we index (pure-virtual methods).
    has_body: bool = True
    # The one named type a function returns — its annotation,
    # a JSDoc @returns, or the class every `return new T()` constructs; ''
    # when the tree does not say. Not part of the id.
    return_type: str = ""

    @property
    def id(self) -> str:
        return symbol_id(self.qualified_name, self.signature)


@dataclass(frozen=True)
class FileUnit(Symbol):
    """A file standing as the unit of its own top-level code."""

    @property
    def id(self) -> str:
        return file_symbol_id(self.path)


@dataclass
class ParsedFile:
    path: str
    lang: str
    symbols: list[Symbol] = field(default_factory=list)
    includes: list[str] = field(default_factory=list)  # raw include targets
    # (qualified_name of derived class, base name as written) pairs
    inherits: list[tuple[str, str]] = field(default_factory=list)
    # pytest fixture requests: (requestor qualified_name, fixture name,
    # line). Filled by lang_python only; resolved to fixture_use edges in
    # edges_treesitter with pytest's nearest-first ladder.
    fixture_requests: list[tuple[str, str, int]] = field(default_factory=list)
    tree: Any = None  # tree_sitter.Tree for chunker/edges reuse
    source: bytes = b""


@lru_cache(maxsize=1)
def _cpp_language() -> Any:
    import tree_sitter
    import tree_sitter_cpp

    return tree_sitter.Language(tree_sitter_cpp.language())


@lru_cache(maxsize=1)
def _cpp_parser() -> Any:
    import tree_sitter

    return tree_sitter.Parser(_cpp_language())


def node_text(node: Any, source: bytes) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8", "replace")


def line_of(text: str, offset: int) -> int:
    """1-based line of a character offset."""
    return text.count("\n", 0, offset) + 1


_SCOPE_NODES = {
    "namespace_definition",
    "class_specifier",
    "struct_specifier",
}

_FN_KINDS = {"function_definition"}
# A googletest body `TEST(Suite, Name) { … }` parses as a
# function named after the MACRO; the two arguments are the test's name.
_GTEST_MACROS = frozenset({"TEST", "TEST_F", "TEST_P", "TYPED_TEST", "TYPED_TEST_P", "FRIEND_TEST"})
_GTEST_ARGS_RE = re.compile(r"^\(\s*([A-Za-z_]\w*)\s*,\s*([A-Za-z_]\w*)\s*\)$")


def _gtest_name(name: str, sig: str) -> tuple[str, str] | None:
    """`("Suite::Name", "TEST_F(Suite, Name)")` for a googletest body, None
    for an ordinary function."""
    if name not in _GTEST_MACROS:
        return None
    m = _GTEST_ARGS_RE.match(sig.strip())
    if not m:
        return None
    return f"{m.group(1)}::{m.group(2)}", f"{name}({m.group(1)}, {m.group(2)})"


def _declarator_name(node: Any, source: bytes) -> str:
    """Innermost name of a declarator subtree: identifier, qualified_identifier,
    field_identifier, destructor_name, operator_name."""
    cur = node
    while cur is not None:
        t = cur.type
        if t in {
            "identifier",
            "qualified_identifier",
            "field_identifier",
            "destructor_name",
            "operator_name",
        }:
            return node_text(cur, source)
        # descend into the declarator field when present
        nxt = cur.child_by_field_name("declarator")
        if nxt is None:
            # pointer/reference declarators wrap another declarator child
            nxt = next(
                (c for c in cur.named_children if "declarator" in c.type or c.type in {
                    "qualified_identifier", "identifier", "field_identifier",
                    "destructor_name", "operator_name",
                }),
                None,
            )
        cur = nxt
    return ""


def _function_declarator(node: Any) -> Any:
    """Find the function_declarator inside a function_definition/declaration."""
    stack = [node]
    while stack:
        cur = stack.pop()
        if cur.type == "function_declarator":
            return cur
        decl = cur.child_by_field_name("declarator")
        if decl is not None:
            stack.append(decl)
        else:
            stack.extend(c for c in cur.named_children if "declarator" in c.type)
    return None


def _doc_comment(node: Any, source: bytes) -> str:
    """Comment block immediately above the node (gap <= 1 line)."""
    sib = node.prev_named_sibling
    parts: list[str] = []
    expected_end = node.start_point[0]
    while sib is not None and sib.type == "comment":
        if expected_end - sib.end_point[0] > 2:
            break
        parts.append(node_text(sib, source))
        expected_end = sib.start_point[0]
        sib = sib.prev_named_sibling
    if not parts:
        return ""
    doc = "\n".join(reversed(parts))
    return doc[:2000]


_INCLUDE_RE = re.compile(r'#\s*include\s*[<"]([^>"]+)[>"]')


def parse_cpp(path: str, source: bytes) -> ParsedFile:
    """Parse one C++ file: symbols with qualified names, includes, inherits."""
    parser = _cpp_parser()
    tree = bounded_parse.parse(parser, source)
    pf = ParsedFile(path=path, lang="cpp", tree=tree, source=source)

    pf.includes = _INCLUDE_RE.findall(source.decode("utf-8", "replace"))

    def walk(node: Any, scope: tuple[str, ...]) -> None:
        for child in node.named_children:
            t = child.type
            if t == "namespace_definition":
                name_node = child.child_by_field_name("name")
                ns = node_text(name_node, source) if name_node is not None else ""
                body = child.child_by_field_name("body")
                if body is not None:
                    walk(body, scope + tuple(ns.split("::")) if ns else scope)
                continue
            if t in {"class_specifier", "struct_specifier"}:
                name_node = child.child_by_field_name("name")
                body = child.child_by_field_name("body")
                # Body-less = forward declaration (`class Field;`). NEVER
                # index it: it shares the RMT key with the real definition
                # (same qualified_name, empty signature), so whichever row
                # lands last would clobber the other — a forward decl in an
                # unrelated header would hide the real class.
                if name_node is not None and body is not None:
                    cls_name = node_text(name_node, source)
                    qname = "::".join(scope + (cls_name,))
                    kind = "class" if t == "class_specifier" else "struct"
                    pf.symbols.append(
                        Symbol(
                            qualified_name=qname,
                            name=cls_name.split("::")[-1],
                            kind=kind,
                            signature="",
                            path=path,
                            start_line=child.start_point[0] + 1,
                            end_line=child.end_point[0] + 1,
                            doc_comment=_doc_comment(child, source),
                        )
                    )
                    bases = next(
                        (c for c in child.named_children if c.type == "base_class_clause"),
                        None,
                    )
                    if bases is not None:
                        for b in bases.named_children:
                            if b.type in {
                                "type_identifier", "qualified_identifier", "template_type",
                            }:
                                base = node_text(b, source)
                                base = re.sub(r"<.*", "", base).strip()
                                if base:
                                    pf.inherits.append((qname, base))
                    if body is not None:
                        walk(body, scope + (cls_name,))
                continue
            if t == "enum_specifier":
                name_node = child.child_by_field_name("name")
                # Same forward-declaration rule as classes.
                if name_node is not None and child.child_by_field_name("body") is not None:
                    en = node_text(name_node, source)
                    pf.symbols.append(
                        Symbol(
                            qualified_name="::".join(scope + (en,)),
                            name=en,
                            kind="enum",
                            signature="",
                            path=path,
                            start_line=child.start_point[0] + 1,
                            end_line=child.end_point[0] + 1,
                            doc_comment=_doc_comment(child, source),
                        )
                    )
                continue
            if t == "function_definition":
                fdecl = _function_declarator(child)
                if fdecl is None:
                    continue
                name = _declarator_name(fdecl, source)
                if not name:
                    continue
                params_node = fdecl.child_by_field_name("parameters")
                sig = node_text(params_node, source) if params_node is not None else "()"
                gtest = _gtest_name(name, sig)
                if gtest is not None:
                    name, sig = gtest  # `Suite::Name`, not the macro's name
                # An out-of-class definition (`void A::B::f(...)`) carries the
                # CLASS qualification in the declarator, but the enclosing
                # namespace scope still applies: `MergeTreeData::f` defined
                # inside `namespace DB` is DB::MergeTreeData::f. Without the
                # prefix, header declarations (DB::X::f) and .cpp definitions
                # (X::f) of the same symbol land under different names.
                if "::" in name:
                    qname = "::".join(scope) + "::" + name if scope else name
                else:
                    qname = "::".join(scope + (name,)) if scope else name
                pf.symbols.append(
                    Symbol(
                        qualified_name=qname,
                        name=name.split("::")[-1],
                        # A googletest body is a test case, not a function anything
                        # calls: otherwise `f.descriptor()` inside
                        # `TEST(buffered_file_test, descriptor)` would bind to the test
                        kind="test" if gtest is not None else "function",
                        signature=sig,
                        path=path,
                        start_line=child.start_point[0] + 1,
                        end_line=child.end_point[0] + 1,
                        doc_comment=_doc_comment(child, source),
                    )
                )
                continue
            if t == "field_declaration" and scope:
                # Pure-virtual method declarations (`virtual void f(...) = 0;`)
                # have no definition anywhere — the declaration IS the symbol.
                # Non-pure declarations are skipped: their definition (inline
                # or in a .cpp) is indexed instead, so decl/def rows never
                # fight over the same RMT key.
                txt = node_text(child, source)
                if re.search(r"=\s*0\s*;?\s*$", txt):
                    fdecl = _function_declarator(child)
                    if fdecl is not None:
                        name = _declarator_name(fdecl, source)
                        params_node = fdecl.child_by_field_name("parameters")
                        if name and params_node is not None:
                            pf.symbols.append(
                                Symbol(
                                    qualified_name="::".join(scope + (name,)),
                                    name=name.split("::")[-1],
                                    kind="function",
                                    signature=node_text(params_node, source),
                                    path=path,
                                    start_line=child.start_point[0] + 1,
                                    end_line=child.end_point[0] + 1,
                                    doc_comment=_doc_comment(child, source),
                                    has_body=False,
                                )
                            )
                continue
            if t == "template_declaration":
                walk(child, scope)
                continue
            if t in {
                "linkage_specification", "declaration_list",
                "preproc_ifdef", "preproc_if", "preproc_else",
            }:
                walk(child, scope)
                continue
        return

    walk(tree.root_node, ())
    return pf
