"""TLA+ specifications and their TLC model configs.

A storage engine that ships its protocol specs beside the code
(`tla/CommitProtocol.tla`, `Recovery.tla`, `Replication.tla`, …, each with a
`.cfg` naming the invariants TLC checks) gives an agent the one artefact that
says what the code MUST do. Indexed as text they would be line windows nobody
could ask about; here they are symbols:

* the ``module`` (`CommitProtocol`), its ``constant`` and ``variable``
  declarations, every ``operator`` definition (`Init == …`, `StartTx(t) ==
  …`, `TypeOK == …`) as a symbol whose qualified name is
  `<Module>::<Operator>`, signature the header as written
  (`StartTx(t)`), doc the `\\*` / `(* *)` comment run right above, span the
  whole definition; ``theorem`` and ``assume`` when present;
* `EXTENDS A, B` → include targets `A.tla`, `B.tla` (the standard modules
  — Integers, Naturals, Sequences, FiniteSets, TLC, Bags, Reals — are not
  files and yield none);
* a reference to another operator inside a definition body is a ``calls``
  edge (`Next` → `StartTx`, `TypeOK` → `PrepStored`): in-file by name here,
  cross-module (`ReplicationFaults EXTENDS Replication`) through the
  namespace name table in `tla_external_edges`;
* one chunk per operator (`function`), the module header with its comment
  as `file_header`.

The TLC config (`<Module>.cfg`: `SPECIFICATION Spec`, `INVARIANT TypeOK`,
`PROPERTY …`, `CONSTANTS X = 3`) is language ``tla_config``: one symbol of
kind ``model`` named after the file, signature the checked names, plus a
``checks`` facet per invariant / property / spec so
`code.tests_for({facets:[{kind:"checks", name:"TypeOK"}]})` names the model
that checks it, and `calls` edges model → operator so `code.callers(TypeOK)`
answers "checked by CommitProtocol.cfg".

Parsing: `tree-sitter-tlaplus` when installed (no extra installs it), else a
line-based reader that finds the same top-level definitions by the
column-0 `Name(args) ==` shape. Outside the extractor stamp with the other
lane modules.
"""

from __future__ import annotations

import bisect
import posixpath
import re
from dataclasses import dataclass, field

from server.code.domain.extract import bounded_parse
from server.code.domain.extract.chunker import Chunk, _assign_ordinals
from server.code.domain.extract.edges_treesitter import Edge
from server.code.domain.extract.lang_sql_model import Facet
from server.code.domain.extract.treesitter import ParsedFile, Symbol

LANG_TLA = "tla"
LANG_TLA_CONFIG = "tla_config"
KIND_MODULE = "module"
KIND_OPERATOR = "operator"
KIND_CONSTANT = "constant"
KIND_VARIABLE = "variable"
KIND_THEOREM = "theorem"
KIND_MODEL = "model"
FACET_CHECKS = "checks"

# Standard modules TLC ships; EXTENDS-ing one names no repository file.
STANDARD_MODULES: frozenset[str] = frozenset(
    {
        "Integers",
        "Naturals",
        "Sequences",
        "FiniteSets",
        "TLC",
        "Bags",
        "Reals",
        "RealTime",
        "Randomization",
        "Json",
        "IOUtils",
        "CSV",
        "Functions",
        "SequencesExt",
        "FiniteSetsExt",
        "Folds",
        "Apalache",
    }
)

_MODULE_RE = re.compile(r"^-{4,}\s*MODULE\s+(\w+)\s*-{4,}\s*$")
_END_RE = re.compile(r"^={4,}\s*$")
_EXTENDS_RE = re.compile(r"^EXTENDS(?=\s.)\s*+((?:.*\S)?)\s*$")
_CONSTANTS_RE = re.compile(r"^CONSTANTS?(?=\s.)\s*+((?:.*\S)?)\s*$")
_VARIABLES_RE = re.compile(r"^VARIABLES?\s*+((?:.*\S)?)\s*$")
_OPDEF_RE = re.compile(r"^([A-Za-z_]\w*)(\([^)]*\))?\s*==")
_THEOREM_RE = re.compile(r"^(THEOREM|ASSUME|LEMMA)\b\s*(\w+)?")
_IDENT_RE = re.compile(r"\b([A-Za-z_]\w*)\b")
_CFG_LINE_RE = re.compile(
    r"^\s*+(SPECIFICATION|INVARIANTS?|PROPERTY|PROPERTIES|INIT|NEXT|CONSTRAINTS?|SYMMETRY)\s++((?:.*\S)?)\s*$",
    re.I,
)
_CFG_HEAD_RE = re.compile(
    r"^[^\S\n]*(SPECIFICATION|INVARIANTS?|PROPERTY|PROPERTIES|CONSTANTS?|INIT|NEXT)\b", re.I | re.M
)
_TLA_KEYWORDS: frozenset[str] = frozenset(
    {
        "IF",
        "THEN",
        "ELSE",
        "LET",
        "IN",
        "CASE",
        "OTHER",
        "CHOOSE",
        "UNION",
        "SUBSET",
        "DOMAIN",
        "EXCEPT",
        "UNCHANGED",
        "ENABLED",
        "TRUE",
        "FALSE",
        "BOOLEAN",
        "STRING",
        "Nat",
        "Int",
        "Cardinality",
        "IsFiniteSet",
        "Len",
        "Seq",
        "Append",
        "Head",
        "Tail",
        "SubSeq",
        "SelectSeq",
        "Print",
        "PrintT",
        "Assert",
        "WF_vars",
        "SF_vars",
    }
)


def is_tlc_config(path: str, source: bytes) -> bool:
    """A `.cfg` is a TLC model config when its text starts with TLC's
    keywords — `.cfg` is otherwise every tool's config extension."""
    if not path.lower().endswith(".cfg"):
        return False
    head = source[:2048].decode("utf-8", "replace")
    return bool(_CFG_HEAD_RE.search(head))


def module_name_of(path: str) -> str:
    return posixpath.splitext(posixpath.basename(path))[0]


# ---------------------------------------------------------------------------
# .tla


@dataclass
class TlaUnits:
    module: str
    symbols: list[Symbol]
    includes: list[str]  # sibling module files (`Replication.tla`)
    refs: list[tuple[str, str, int]]  # (from operator qname, referenced bare name, line)
    header_end: int  # last line of the header + preamble before the first definition
    doc: str = ""


_ASSIGN_RE = re.compile(r"<-|=")  # each side is stripped: `\s*` around it is quadratic
_DOC_MAX = 2000  # not a setting: the cut every doc_comment of this lane takes


class _BlockDocs:
    """The `(* … *)` doc of a definition under a block's `*)`: the lines from the nearest
    `(*` above, cut to `_DOC_MAX`. Each opener's text is read forward once and shared by
    the definitions below it, so a block over many definitions is not read again for
    each; `upto` is asked in line order, as `tla_units` walks the definitions."""

    def __init__(self, lines: list[str]) -> None:
        self.lines = lines
        self.openers: list[int] = []  # each line's nearest `(*` at or above it, else -1
        last = -1
        for i, ln in enumerate(lines):
            if "(*" in ln:
                last = i
            self.openers.append(last)
        self.read: dict[int, tuple[int, str]] = {}  # opener -> (next line, text so far)

    def upto(self, i: int) -> str:
        j = self.openers[i]
        if j < 0:
            return ""
        k, text = self.read.get(j, (j, ""))
        while k <= i and len(text) < _DOC_MAX:
            piece = self.lines[k].strip()[:_DOC_MAX]
            text = piece if k == j else f"{text}\n{piece}"[:_DOC_MAX]
            k += 1
        self.read[j] = (k, text)
        return text


def _comment_run_above(lines: list[str], start_idx: int, blocks: _BlockDocs) -> str:
    """The `\\*` comment lines (or one `(* … *)` block) directly above line
    index `start_idx`, blank lines allowed between the comment and the
    definition but not inside the run."""
    out: list[str] = []
    i = start_idx - 1
    while i >= 0 and not lines[i].strip():
        i -= 1
    if i >= 0 and lines[i].rstrip().endswith("*)"):
        return blocks.upto(i)
    while i >= 0 and lines[i].lstrip().startswith("\\*"):
        out.append(lines[i].strip())
        i -= 1
    return "\n".join(reversed(out))[:_DOC_MAX]


def _definitions_regex(text: str) -> list[tuple[str, str, int, int, str]]:
    """(name, signature, start_line, end_line, kind) for every column-0
    definition; a definition ends where the next one (or the module's
    `====`) begins, trailing blank / comment lines trimmed."""
    lines = text.splitlines()
    starts: list[tuple[int, str, str, str]] = []
    depth = 0  # inside (* *) block comments
    for i, raw in enumerate(lines):
        depth += raw.count("(*") - raw.count("*)")
        if depth > 0 or raw.lstrip().startswith("\\*"):
            continue
        m = _OPDEF_RE.match(raw)
        if m:
            starts.append((i, m.group(1), m.group(1) + (m.group(2) or ""), KIND_OPERATOR))
            continue
        t = _THEOREM_RE.match(raw)
        if t and t.group(2):
            starts.append((i, t.group(2), raw.strip()[:120], KIND_THEOREM))
    end_idx = len(lines)
    for i, raw in enumerate(lines):
        if _END_RE.match(raw):
            end_idx = i
            break
    # a column-0 declaration between definitions ends the one above it
    decl_idx = [
        i
        for i, raw in enumerate(lines)
        if _EXTENDS_RE.match(raw)
        or _CONSTANTS_RE.match(raw)
        or _VARIABLES_RE.match(raw)
        or raw.startswith(("ASSUME", "THEOREM", "LEMMA", "----"))
    ]
    out: list[tuple[str, str, int, int, str]] = []
    for n, (i, name, sig, kind) in enumerate(starts):
        stop = starts[n + 1][0] if n + 1 < len(starts) else end_idx
        k = bisect.bisect_right(decl_idx, i)  # the first declaration after it, found once
        if k < len(decl_idx) and decl_idx[k] < stop:
            stop = decl_idx[k]
        j = stop - 1
        while j > i and (not lines[j].strip() or lines[j].lstrip().startswith("\\*")):
            j -= 1
        out.append((name, sig, i + 1, j + 1, kind))
    return out


def _definitions_grammar(source: bytes) -> list[tuple[str, str, int, int, str]] | None:
    """The same tuples through tree-sitter-tlaplus; None when the grammar is
    not installed or the parse has errors (the reader falls back)."""
    try:
        import tree_sitter_tlaplus as tl  # type: ignore[import-not-found]
        from tree_sitter import Language, Parser
    except Exception:  # noqa: BLE001 — optional grammar
        return None
    try:
        parser = Parser(Language(tl.language()))
        tree = bounded_parse.parse(parser, source)
    except Exception:  # noqa: BLE001 — no parse, or none in time: the reader falls back
        return None
    root = tree.root_node
    if root.has_error:
        return None
    mod = next((c for c in root.children if c.type == "module"), None)
    if mod is None:
        return None
    out: list[tuple[str, str, int, int, str]] = []
    kinds = ("operator_definition", "theorem", "assumption", "assume_prove")
    starts = [c.start_point[0] for c in mod.children if c.type in kinds]
    if len(set(starts)) < len(starts):
        return None  # two definitions on one line: each would read the whole line again
    for c in mod.children:
        if c.type == "operator_definition":
            name_node = next(
                (ch for ch in c.children if ch.type in ("identifier", "identifier_ref")), None
            )
            if name_node is None:
                continue
            name = source[name_node.start_byte : name_node.end_byte].decode("utf-8", "replace")
            header = (
                source[c.start_byte : c.end_byte]
                .decode("utf-8", "replace")
                .split("==", 1)[0]
                .strip()
            )
            out.append(
                (name, header or name, c.start_point[0] + 1, c.end_point[0] + 1, KIND_OPERATOR)
            )
        elif c.type in ("theorem", "assumption", "assume_prove"):
            text = source[c.start_byte : c.end_byte].decode("utf-8", "replace")
            m = _THEOREM_RE.match(text)
            name = (m.group(2) if m and m.group(2) else "").strip()
            if name:
                out.append(
                    (
                        name,
                        text.splitlines()[0][:120],
                        c.start_point[0] + 1,
                        c.end_point[0] + 1,
                        KIND_THEOREM,
                    )
                )
    return out


def _scan_declarations(
    lines: list[str], defs: list[tuple[str, str, int, int, str]], module: str, path: str
) -> tuple[list[Symbol], list[str], int, list[str]]:
    """Column-0 EXTENDS / CONSTANTS / VARIABLES anywhere outside a definition
    span (TLA+ interleaves them) → (declaration symbols, include targets,
    header_end, header comment lines). The header is what precedes the
    first definition."""
    first_def_line = min((d[2] for d in defs), default=len(lines) + 1)
    in_def = [False] * (len(lines) + 2)
    for _n, _s, start, end, _k in defs:
        for li in range(start, min(end, len(lines)) + 1):
            in_def[li] = True
    syms: list[Symbol] = []
    includes: list[str] = []
    doc_lines: list[str] = []
    header_end = 1
    depth = 0
    for i, raw in enumerate(lines):
        line_no = i + 1
        if in_def[line_no] or _END_RE.match(raw):
            continue
        depth += raw.count("(*") - raw.count("*)")
        if depth > 0 or raw.lstrip().startswith("\\*"):
            if line_no < first_def_line:
                doc_lines.append(raw.strip(" \\*()"))
            continue
        m = _EXTENDS_RE.match(raw)
        if m:
            includes.extend(
                f"{n}.tla"
                for n in (x.strip() for x in m.group(1).split(","))
                if n and n not in STANDARD_MODULES
            )
        elif (m3 := _CONSTANTS_RE.match(raw)) is not None:
            for name in (x.strip().split("(")[0].strip() for x in m3.group(1).split(",")):
                if name:
                    syms.append(
                        Symbol(f"{module}::{name}", name, KIND_CONSTANT, "", path, line_no, line_no)
                    )
        elif _VARIABLES_RE.match(raw):
            m2 = _VARIABLES_RE.match(raw)
            assert m2 is not None
            names = [x.strip() for x in m2.group(1).split("\\*", 1)[0].split(",") if x.strip()]
            j = i + 1
            while (
                j < len(lines)
                and lines[j].startswith((" ", "\t"))
                and not _OPDEF_RE.match(lines[j])
            ):
                names.extend(x.strip() for x in lines[j].split("\\*", 1)[0].split(",") if x.strip())
                j += 1
            for name in (n.rstrip(",").strip() for n in names):
                if name and _IDENT_RE.fullmatch(name):
                    syms.append(
                        Symbol(
                            f"{module}::{name}",
                            name,
                            KIND_VARIABLE,
                            "",
                            path,
                            line_no,
                            max(line_no, j),
                        )
                    )
            if j < first_def_line:
                header_end = j
            continue
        elif not raw.strip() or _MODULE_RE.match(raw):
            continue
        if line_no < first_def_line:
            header_end = line_no
    if defs:
        header_end = min(header_end, first_def_line - 1)
    return syms, includes, max(header_end, 1), doc_lines


def _operator_refs(lines: list[str], syms: list[Symbol]) -> list[tuple[str, str, int]]:
    """(operator qualified name, referenced bare name, line) for every
    identifier a definition body names that is a module definition or a
    capitalised name (a possibly inherited operator); keywords and the
    operator's own name excluded."""
    defined = {s.name for s in syms if s.kind in (KIND_OPERATOR, KIND_CONSTANT, KIND_VARIABLE)}
    refs: list[tuple[str, str, int]] = []
    for s in syms:
        if s.kind not in (KIND_OPERATOR, KIND_THEOREM):
            continue
        seen: set[str] = set()
        for li in range(s.start_line, s.end_line + 1):
            body = lines[li - 1] if li - 1 < len(lines) else ""
            if li == s.start_line:
                body = body.split("==", 1)[1] if "==" in body else ""
            body = body.split("\\*", 1)[0]
            for m in _IDENT_RE.finditer(body):
                name = m.group(1)
                if name == s.name or name in _TLA_KEYWORDS or name in seen:
                    continue
                if name in defined or name[0].isupper():
                    seen.add(name)
                    refs.append((s.qualified_name, name, li))
    return refs


def tla_units(path: str, source: bytes) -> TlaUnits:
    text = source.decode("utf-8", "replace")
    lines = text.splitlines()
    module = module_name_of(path)
    for raw in lines[:5]:
        m = _MODULE_RE.match(raw)
        if m:
            module = m.group(1)
            break
    defs = _definitions_grammar(source) or _definitions_regex(text)
    decl_syms, includes, header_end, doc_lines = _scan_declarations(lines, defs, module, path)
    syms: list[Symbol] = [
        Symbol(
            module,
            module,
            KIND_MODULE,
            "MODULE " + module,
            path,
            1,
            len(lines),
            doc_comment="\n".join(x for x in doc_lines if x)[:_DOC_MAX],
        ),
        *decl_syms,
    ]
    blocks = _BlockDocs(lines)
    for name, sig, start, end, kind in defs:
        syms.append(
            Symbol(
                f"{module}::{name}",
                name,
                kind,
                sig,
                path,
                start,
                end,
                doc_comment=_comment_run_above(lines, start - 1, blocks),
            )
        )
    return TlaUnits(
        module=module,
        symbols=syms,
        includes=includes,
        refs=_operator_refs(lines, syms),
        header_end=header_end,
        doc=syms[0].doc_comment,
    )


def parse_tla(path: str, source: bytes) -> ParsedFile:
    pf = ParsedFile(path=path, lang=LANG_TLA, tree=None, source=source)
    units = tla_units(path, source)
    pf.symbols = units.symbols
    pf.includes = units.includes
    return pf


def tla_chunks(pf: ParsedFile) -> list[Chunk]:
    """The header (module line, comment, EXTENDS, declarations) as one
    `file_header`; each operator / theorem as a `function` chunk carrying its
    symbol id and doc comment; leftover runs between definitions as
    `comment_block` when they are comments, else `other`."""
    text = pf.source.decode("utf-8", "replace")
    lines = text.splitlines(keepends=True)
    units = tla_units(pf.path, pf.source)
    out: list[Chunk] = []
    module = next((s for s in pf.symbols if s.kind == KIND_MODULE), None)
    defs = sorted(
        (s for s in pf.symbols if s.kind in (KIND_OPERATOR, KIND_THEOREM)),
        key=lambda s: s.start_line,
    )
    first_def = defs[0].start_line if defs else len(lines) + 1
    head_end = min(units.header_end, first_def - 1) if defs else len(lines)
    if head_end >= 1:
        out.append(
            Chunk(
                pf.path,
                module.qualified_name if module else "",
                "file_header",
                1,
                head_end,
                "".join(lines[:head_end]),
                [module.id] if module else [],
            )
        )
    # constants and variables by line, so each gap takes its own by bisection
    decls = sorted(
        (d for d in pf.symbols if d.kind in (KIND_CONSTANT, KIND_VARIABLE)),
        key=lambda d: d.start_line,
    )
    decl_lines = [d.start_line for d in decls]
    cursor = head_end + 1
    for s in defs:
        if s.start_line > cursor:
            gap = "".join(lines[cursor - 1 : s.start_line - 1])
            if gap.strip():
                kind = (
                    "comment_block"
                    if all(
                        not ln.strip() or ln.lstrip().startswith(("\\*", "(*", "*"))
                        for ln in gap.splitlines()
                    )
                    else "other"
                )
                lo = bisect.bisect_left(decl_lines, cursor)
                inside = decls[lo : bisect.bisect_right(decl_lines, s.start_line - 1)]
                out.append(
                    Chunk(
                        pf.path,
                        module.qualified_name if module else "",
                        kind,
                        cursor,
                        s.start_line - 1,
                        gap,
                        [d.id for d in inside],
                    )
                )
        body = "".join(lines[s.start_line - 1 : s.end_line])
        content = (s.doc_comment + "\n" if s.doc_comment else "") + body
        out.append(
            Chunk(pf.path, s.qualified_name, "function", s.start_line, s.end_line, content, [s.id])
        )
        cursor = s.end_line + 1
    if cursor <= len(lines):
        tail = "".join(lines[cursor - 1 :])
        if tail.strip():
            out.append(
                Chunk(
                    pf.path,
                    module.qualified_name if module else "",
                    "other",
                    cursor,
                    len(lines),
                    tail,
                )
            )
    _assign_ordinals(out)
    return out


def tla_local_edges(pf: ParsedFile) -> tuple[list[Edge], list[tuple[str, str, int]]]:
    """In-file `calls` edges (operator → operator / constant / variable it
    names) and the references left for the name table (another module's
    operators)."""
    units = tla_units(pf.path, pf.source)
    by_name = {
        s.name: s
        for s in pf.symbols
        if s.kind in (KIND_OPERATOR, KIND_CONSTANT, KIND_VARIABLE, KIND_THEOREM)
    }
    by_q = {s.qualified_name: s for s in pf.symbols}
    edges: list[Edge] = []
    external: list[tuple[str, str, int]] = []
    for from_q, name, line in units.refs:
        a = by_q.get(from_q)
        b = by_name.get(name)
        if a is None:
            continue
        if b is None:
            if pf.includes:
                external.append((from_q, name, line))
            continue
        edges.append(
            Edge("calls", a.id, b.id, a.qualified_name, b.qualified_name, pf.path, line, 1.0)
        )
    return edges, external


def tla_external_edges(
    pf: ParsedFile,
    external: list[tuple[str, str, int]],
    table: dict[str, list[tuple[str, str, str]]],
) -> list[Edge]:
    """`calls` edges to operators of the modules this one EXTENDS, bound
    through the namespace name table: bare name → [(symbol_id, qualified
    name, kind)], kept when exactly one candidate is a TLA operator /
    constant / variable."""
    by_q = {s.qualified_name: s for s in pf.symbols}
    out: list[Edge] = []
    seen: set[tuple[str, str]] = set()
    for from_q, name, line in external:
        a = by_q.get(from_q)
        cands = [
            c for c in table.get(name, ()) if c[2] in (KIND_OPERATOR, KIND_CONSTANT, KIND_VARIABLE)
        ]
        if a is None or len(cands) != 1 or (from_q, name) in seen:
            continue
        seen.add((from_q, name))
        sid, qname, _kind = cands[0]
        out.append(Edge("calls", a.id, sid, a.qualified_name, qname, pf.path, line, 0.9))
    return out


# ---------------------------------------------------------------------------
# .cfg (TLC model config)


@dataclass
class TlcUnits:
    symbols: list[Symbol]
    facets: list[Facet]
    checked: list[tuple[str, str, int]] = field(default_factory=list)  # (role, name, line)
    constants: list[tuple[str, str]] = field(default_factory=list)


def tlc_units(path: str, source: bytes) -> TlcUnits:
    text = source.decode("utf-8", "replace")
    lines = text.splitlines()
    stem = module_name_of(path)
    checked: list[tuple[str, str, int]] = []
    constants: list[tuple[str, str]] = []
    mode = ""
    for i, raw in enumerate(lines, start=1):
        s = raw.split("\\*", 1)[0].strip()
        if not s:
            continue
        m = _CFG_LINE_RE.match(s)
        if m:
            role = m.group(1).upper().rstrip("S")
            role = {"PROPERTIE": "PROPERTY", "INVARIANT": "INVARIANT"}.get(role, role)
            mode = ""
            for name in re.split(r"[,\s]+", m.group(2).strip()):
                if name and _IDENT_RE.fullmatch(name):
                    checked.append((role.lower(), name, i))
            continue
        if re.match(r"^CONSTANTS?\b", s, re.I):
            mode = "CONSTANTS"
            rest = re.sub(r"^CONSTANTS?\b", "", s, flags=re.I).strip()
            if rest:
                s = rest
            else:
                continue
        if mode == "CONSTANTS" or "=" in s or "<-" in s:
            for part in s.split(","):
                if "=" in part or "<-" in part:
                    k, v = _ASSIGN_RE.split(part, maxsplit=1)
                    if _IDENT_RE.fullmatch(k.strip()):
                        constants.append((k.strip(), v.strip()))
    sig = ", ".join(f"{role} {name}" for role, name, _l in checked)[:300]
    model = Symbol(
        f"{stem}::TLC",
        stem,
        KIND_MODEL,
        sig,
        path,
        1,
        max(1, len(lines)),
        doc_comment=("; ".join(f"{k} = {v}" for k, v in constants))[:500],
    )
    facets = [Facet(FACET_CHECKS, name, 1, line, 1.0) for _role, name, line in checked]
    return TlcUnits(symbols=[model], facets=facets, checked=checked, constants=constants)


def parse_tlc_config(path: str, source: bytes) -> ParsedFile:
    pf = ParsedFile(path=path, lang=LANG_TLA_CONFIG, tree=None, source=source)
    units = tlc_units(path, source)
    pf.symbols = units.symbols
    pf.includes = [f"{module_name_of(path)}.tla"]
    return pf


def tlc_chunks(pf: ParsedFile) -> list[Chunk]:
    text = pf.source.decode("utf-8", "replace")
    model = pf.symbols[0] if pf.symbols else None
    return [
        Chunk(
            pf.path,
            model.qualified_name if model else "",
            "other",
            1,
            max(1, text.count("\n") + (0 if text.endswith("\n") else 1)),
            text,
            [model.id] if model else [],
        )
    ]


def tlc_edges(pf: ParsedFile, table: dict[str, list[tuple[str, str, str]]]) -> list[Edge]:
    """`calls` edges model → the operators it checks (SPECIFICATION,
    INVARIANT, PROPERTY, INIT, NEXT), bound by bare name through the name
    table to TLA operators — the checked module is the includer's stem, so
    a candidate under that module wins when several exist."""
    if not pf.symbols:
        return []
    model = pf.symbols[0]
    stem = module_name_of(pf.path)
    units = tlc_units(pf.path, pf.source)
    out: list[Edge] = []
    seen: set[str] = set()
    for _role, name, line in units.checked:
        if name in seen:
            continue
        cands = [c for c in table.get(name, ()) if c[2] == KIND_OPERATOR]
        pick = [c for c in cands if c[1].startswith(stem + "::")] or cands
        if len(pick) != 1:
            continue
        seen.add(name)
        sid, qname, _k = pick[0]
        out.append(Edge("calls", model.id, sid, model.qualified_name, qname, pf.path, line, 1.0))
    return out


__all__ = [
    "FACET_CHECKS",
    "KIND_CONSTANT",
    "KIND_MODEL",
    "KIND_MODULE",
    "KIND_OPERATOR",
    "KIND_THEOREM",
    "KIND_VARIABLE",
    "LANG_TLA",
    "LANG_TLA_CONFIG",
    "STANDARD_MODULES",
    "TlaUnits",
    "TlcUnits",
    "is_tlc_config",
    "module_name_of",
    "parse_tla",
    "parse_tlc_config",
    "tla_chunks",
    "tla_external_edges",
    "tla_local_edges",
    "tla_units",
    "tlc_chunks",
    "tlc_edges",
    "tlc_units",
]
