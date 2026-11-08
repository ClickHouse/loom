"""Java: classes, interfaces, enums, records, methods, imports, and the SQL the file
runs.

The fast-tier parse of a `.java` file — type declarations as `class` / `interface` / `enum`
/ `record`, methods and constructors as `function` symbols qualified `<pkg
path>::<Class>.<method>`, `import a.b.C` as raw include specifiers — plus the SQL islands:
the argument of a JDBC (`prepareStatement`, `executeQuery`, `executeUpdate`, `execute`,
`addBatch`), Spring `JdbcTemplate` (`query`, `queryForObject`, `queryForList`, `update`,
`batchUpdate`), JPA (`createQuery`, `createNativeQuery`) or jOOQ (`fetch`, `execute`,
`resultQuery`) client method (`JAVA_CLIENT_METHODS`), the value of a `@Query` /
`@NativeQuery` / MyBatis `@Select` `@Insert` `@Update` `@Delete` annotation
(`JAVA_SQL_ANNOTATIONS`), `String.format` with `%s` holes, `+` concatenation, text blocks,
and bare SQL-shaped literals. Outside the extractor stamp with the other lane parsers.
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Any

from server.code.domain.extract import bounded_parse
from server.code.domain.extract.lang_code_islands import CodeSqlUnits, IslandGrammar, code_sql_units
from server.code.domain.extract.lang_python import module_scope
from server.code.domain.extract.treesitter import ParsedFile, Symbol, node_text

LANG_JAVA = "java"

JAVA_CLIENT_METHODS: dict[str, str] = {
    # JDBC
    "prepareStatement": "",
    "prepareCall": "",
    "executeQuery": "",
    "executeUpdate": "",
    "execute": "",
    "executeLargeUpdate": "",
    "addBatch": "",
    "nativeSQL": "",
    # Spring JdbcTemplate / NamedParameterJdbcTemplate
    "query": "",
    "queryForObject": "",
    "queryForList": "",
    "queryForMap": "",
    "queryForRowSet": "",
    "queryForStream": "",
    "update": "",
    "batchUpdate": "",
    # JPA / Hibernate
    "createQuery": "",
    "createNativeQuery": "",
    "createNamedQuery": "",
    "createSQLQuery": "",
    # jOOQ / R2DBC / clickhouse-java
    "fetch": "",
    "fetchOne": "",
    "fetchAny": "",
    "resultQuery": "",
    "sql": "",
    "executeAndWait": "clickhouse",
    "queryAll": "clickhouse",
    "queryRecords": "clickhouse",
}
JAVA_SQL_ANNOTATIONS: dict[str, str] = {
    "Query": "",
    "NativeQuery": "",
    "Select": "",
    "Insert": "",
    "Update": "",
    "Delete": "",
    "SqlQuery": "",
    "SqlUpdate": "",
    "Sql": "",
}
_STRING_NODES = frozenset({"string_literal", "text_block"})
_ESCAPES = {"\\n": "\n", "\\t": "\t", "\\\\": "\\", '\\"': '"', "\\'": "'"}
_ESCAPE_RE = re.compile(r"\\[ntr\\\"']")
_TYPE_KINDS = {
    "class_declaration": "class",
    "interface_declaration": "interface",
    "enum_declaration": "enum",
    "record_declaration": "record",
    "annotation_type_declaration": "interface",
}


@lru_cache(maxsize=1)
def _parser() -> Any:
    import tree_sitter
    import tree_sitter_java

    return tree_sitter.Parser(tree_sitter.Language(tree_sitter_java.language()))


def java_callee(call: Any, source: bytes) -> tuple[str, str]:
    """(method, object) of a `method_invocation`: `jdbc.query(...)` →
    ("query", "jdbc"); `String.format(...)` → ("format", "String")."""
    name = call.child_by_field_name("name")
    obj = call.child_by_field_name("object")
    obj_name = node_text(obj, source).rsplit(".", 1)[-1] if obj is not None else ""
    return (node_text(name, source) if name is not None else ""), obj_name


def java_string_value(node: Any, source: bytes) -> tuple[str, bool]:
    """A string literal with the common escapes decoded; a text block with
    its delimiters and the first line break stripped."""
    raw = node_text(node, source)
    block = raw.startswith('"""') and raw.endswith('"""') and len(raw) >= 6
    if node.type == "text_block" or block:
        body = raw[3:-3] if block else raw
        if body.startswith("\n"):
            body = body[1:]
        return body, False
    body = raw[1:-1] if len(raw) >= 2 and raw[0] == raw[-1] == '"' else raw
    return _ESCAPE_RE.sub(lambda m: _ESCAPES.get(m.group(0), m.group(0)), body), False


JAVA_ISLANDS = IslandGrammar(
    lang=LANG_JAVA,
    call_node="method_invocation",
    string_nodes=_STRING_NODES,
    methods=JAVA_CLIENT_METHODS,
    callee=java_callee,
    string_value=java_string_value,
    format_methods=frozenset({"format", "formatted"}),
    annotations=JAVA_SQL_ANNOTATIONS,
    annotation_node="annotation",
    assign_nodes=(
        ("variable_declarator", "name", "value"),
        ("assignment_expression", "left", "right"),
    ),
    aug_assign_node=("assignment_expression", "left", "operator", "right"),
    chain_methods=frozenset({"append"}),
    chain_seed_node="object_creation_expression",
)


def _params(node: Any, source: bytes) -> str:
    p = node.child_by_field_name("parameters")
    return node_text(p, source) if p is not None else "()"


def parse_java(path: str, source: bytes) -> ParsedFile:
    """Type declarations, methods, constructors and imports of one Java file."""
    tree = bounded_parse.parse(_parser(), source)
    pf = ParsedFile(path=path, lang=LANG_JAVA, tree=tree, source=source)
    mod = module_scope(path)
    imports: list[str] = []

    def add(name: str, kind: str, sig: str, node: Any, scope: list[str]) -> None:
        inner = ".".join([*scope, name])
        pf.symbols.append(
            Symbol(
                qualified_name=f"{mod}::{inner}",
                name=name,
                kind=kind,
                signature=sig,
                path=path,
                start_line=node.start_point[0] + 1,
                end_line=node.end_point[0] + 1,
            )
        )

    def visit(n: Any, scope: list[str]) -> None:
        t = n.type
        if t in _TYPE_KINDS:
            name = n.child_by_field_name("name")
            if name is not None:
                nm = node_text(name, source)
                add(nm, _TYPE_KINDS[t], f"{_TYPE_KINDS[t]} {nm}", n, scope)
                for c in n.children:
                    visit(c, [*scope, nm])
                return
        elif t in ("method_declaration", "constructor_declaration"):
            name = n.child_by_field_name("name")
            if name is not None:
                # `function`, the edge walker's caller kind, qualified under the class
                add(node_text(name, source), "function", _params(n, source), n, scope)
        elif t == "import_declaration":
            spec = node_text(n, source).removeprefix("import").strip().rstrip(";").strip()
            spec = spec.removeprefix("static").strip()
            if spec:
                imports.append(spec)
        for c in n.children:
            visit(c, scope)

    visit(tree.root_node, [])
    # `import a.b.C` names a class, not a repository file: no include edge (resolving it
    # would need a source-root resolver)
    pf.includes = []
    return pf


def java_imports(pf: ParsedFile) -> list[str]:
    """The file's import specifiers, from the tree."""
    out: list[str] = []
    if pf.tree is None:
        return out

    def visit(n: Any) -> None:
        if n.type == "import_declaration":
            spec = node_text(n, pf.source).removeprefix("import").strip().rstrip(";").strip()
            out.append(spec.removeprefix("static").strip())
        for c in n.children:
            visit(c)

    visit(pf.tree.root_node)
    return out


def java_file_dialect(pf: ParsedFile) -> str:
    """What the imports say the file talks to: clickhouse-java → clickhouse,
    org.postgresql → postgres, com.mysql → mysql, org.sqlite → sqlite."""
    for spec in java_imports(pf):
        low = spec.lower()
        if "clickhouse" in low:
            return "clickhouse"
        if "postgresql" in low:
            return "postgres"
        if "mysql" in low or "mariadb" in low:
            return "mysql"
        if "sqlite" in low:
            return "sqlite"
    return ""


def java_package(pf: ParsedFile) -> str:
    """The file's `package a.b.c;`, '' when absent."""
    if pf.tree is None:
        return ""
    for n in pf.tree.root_node.children:
        if n.type == "package_declaration":
            for c in n.named_children:
                if c.type in ("scoped_identifier", "identifier"):
                    return node_text(c, pf.source)
    return ""


def java_source_root(path: str, package: str) -> str:
    """The source root of a file from its own path and package:
    `x/src/main/java/com/acme/Foo.java` in `com.acme` → `x/src/main/java`; '' when the
    path does not end in the package's directories."""
    directory = path.rsplit("/", 1)[0] if "/" in path else ""
    suffix = package.replace(".", "/")
    if not suffix:
        return directory
    if directory == suffix:
        return ""
    if directory.endswith("/" + suffix):
        return directory[: -len(suffix) - 1]
    return ""


def java_import_candidates(spec: str, roots: list[str]) -> list[str]:
    """Repository files an `import a.b.C` may name under each source root
    (`<root>/a/b/C.java`); a wildcard import and a static member import name
    the class before the last segment; an import of a well-known external
    package names nothing."""
    spec = spec.strip().removeprefix("static").strip()
    if spec.startswith(
        (
            "java.",
            "javax.",
            "jakarta.",
            "org.junit.",
            "org.springframework.",
            "com.fasterxml.",
            "org.slf4j.",
            "com.google.",
            "kotlin.",
            "scala.",
            "lombok.",
        )
    ):
        return []
    parts = [p for p in spec.split(".") if p]
    if parts and parts[-1] == "*":
        parts = parts[:-1]
    out: list[str] = []
    for root in roots:
        prefix = root.strip("/") + "/" if root.strip("/") else ""
        out.append(prefix + "/".join(parts) + ".java")
        if len(parts) > 1:
            out.append(prefix + "/".join(parts[:-1]) + ".java")  # static member import
    return list(dict.fromkeys(out))


def java_resolve_imports(specs: list[str], roots: list[str], present: set[str]) -> list[str]:
    out: list[str] = []
    for spec in specs:
        hit = next((c for c in java_import_candidates(spec, roots) if c in present), None)
        if hit and hit not in out:
            out.append(hit)
    return out


def java_sql_units(pf: ParsedFile) -> CodeSqlUnits:
    return code_sql_units(pf, JAVA_ISLANDS, java_file_dialect(pf))


__all__ = [
    "JAVA_CLIENT_METHODS",
    "JAVA_ISLANDS",
    "JAVA_SQL_ANNOTATIONS",
    "LANG_JAVA",
    "java_callee",
    "java_file_dialect",
    "java_import_candidates",
    "java_imports",
    "java_package",
    "java_resolve_imports",
    "java_source_root",
    "java_sql_units",
    "java_string_value",
    "parse_java",
]
