"""Python routes, commands, decorators and async defs as facets.

A Python web service names its routes in decorators (`@app.get("/x")`,
`@router.post(...)`, `@bp.route("/x", methods=[...])`), in registration calls
(`web.get("/x", handler)`, `add_url_rule`) and in URL tables (Django's `path("x/",
view)`, Tornado's `(r"/x", Handler)` tuples); a CLI names its commands in
`@click.command()` / `@cli.command("name")` / `@app.command()` decorators and
`add_parser("name")` calls. An agent asks "which function serves GET /users/:id" or
"which function is the `sync` command" and has no symbol to name. The facet plane
answers such facts exactly; this module derives them for Python, and beside them two
facts about every def that the symbol row cannot carry without moving its id or its
chunk text:

- `http_route`: `<METHOD> <path>` at the handler's `def` line (a decorator
  route) or at the registration's line; an `APIRouter(prefix=...)` /
  `Blueprint(url_prefix=...)` declared in the same file, and an
  `app.include_router(router, prefix=...)` in the same file, prefix the
  paths written on that router; `ANY` when the framework leaves the method
  open (Django URL tables, Tornado handlers, `add_url_rule` without
  methods); `WS` for a websocket route.
- `command` (the Go lane's kind for a cobra command): the
  command's name — the decorator's string argument or `name=`, else the
  def's name with `_` written `-` as click and typer derive it; an
  argparse `add_parser("name")` at its line.
- `decorator`: every decorator of a def or class, by its dotted name
  without arguments (`pytest.mark.parametrize`, `property`,
  `functools.lru_cache`, `app.get`), at the decorated def's line — so
  "which functions are cached", "which tests are parametrized", "which
  methods are properties" are one facet read.
- `modifier`: `async` at an async def's line.

Pure over the parse and the text; the caller wraps it fail-open.
"""

from __future__ import annotations

import posixpath
import re
from typing import Any

from server.code.domain.extract.lang_sql_model import Facet
from server.code.domain.extract.text_lines import line_at, newline_offsets
from server.code.domain.extract.treesitter import ParsedFile

FACET_ROUTE = "http_route"
FACET_COMMAND = "command"
FACET_DECORATOR = "decorator"
FACET_MODIFIER = "modifier"

_MAX_FACETS_PER_FILE = 400
_MAX_NAME = 200
_HTTP = "get|post|put|patch|delete|head|options"
_STR = r"""r?['"]([^'"]*)['"]"""

_DEC_ROUTE_RE = re.compile(
    r"^@\s*([\w.]+)\.(" + _HTTP + r"|route|api_route|websocket|ws)\s*\(\s*(?:" + _STR + r")?(.*)$",
    re.S,
)
_DEC_COMMAND_RE = re.compile(r"^@\s*([\w.]+)\.(command|group)\b\s*(?:\((.*)\))?\s*$", re.S)
_METHODS_RE = re.compile(r"methods\s*=\s*[\[(]([^\[\]()]*)[\])]")  # stops at the next opener
_NAME_KW_RE = re.compile(r"\bname\s*=\s*" + _STR)
_FIRST_STR_RE = re.compile(r"^\s*" + _STR)
_ANY_STR_RE = re.compile(r"""['"]([^'"]+)['"]""")
_DEC_NAME_RE = re.compile(r"^@\s*([\w.]+)")
_PREFIX_DECL_RE = re.compile(
    # The arguments stop at the next constructor: an unclosed one is not searched on to
    # the end of the file from each.
    r"^[^\S\n]*([\w.]+)\s*=\s*(?:[\w.]+\.)?(?:APIRouter|Blueprint)\s*\("
    r"((?:(?!(?:APIRouter|Blueprint)\s*\()[^)])*)\)",
    re.M,
)
_PREFIX_KW_RE = re.compile(r"\b(?:url_)?prefix\s*=\s*" + _STR)
# A call's arguments stop at the next same call: an unclosed one is not searched on to
# the end of the file from each.
_INCLUDE_ROUTER_RE = re.compile(
    r"\.include_router\(\s*([\w.]+)\s*,(?:(?!\.include_router\()[^)])*?\bprefix\s*=\s*" + _STR,
)
_ADD_URL_RULE_RE = re.compile(r"\.add_url_rule\(\s*" + _STR + r"((?:(?!\.add_url_rule\()[^)])*)\)")
_AIOHTTP_RES = (
    re.compile(r"\bweb\.(" + _HTTP + r"|view)\(\s*" + _STR),
    re.compile(r"\.add_(" + _HTTP + r"|view)\(\s*" + _STR),
)
_AIOHTTP_ROUTE_RE = re.compile(r"\.add_route\(\s*['\"](\w+|\*)['\"]\s*,\s*" + _STR)
_DJANGO_RE = re.compile(r"\b(?:path|re_path|url)\(\s*" + _STR + r"\s*,(?!\s*include\b)")
_STARLETTE_RE = re.compile(
    r"\b(Route|WebSocketRoute)\(\s*" + _STR + r"((?:(?!\b(?:WebSocket)?Route\()[^)])*)\)"
)
_TORNADO_RE = re.compile(r"\(\s*r?['\"](/[^'\"]*)['\"]\s*,\s*[\w.]+\s*[,)]")
_ADD_PARSER_RE = re.compile(r"\.add_parser\(\s*" + _STR)


def _url_join(prefix: str, sub: str) -> str:
    parts = [p.strip("/") for p in (prefix, sub) if p and p.strip("/")]
    joined = "/" + "/".join(parts) if parts else "/"
    if sub.endswith("/") and joined != "/":
        joined += "/"  # Django keeps the trailing slash; it is part of the route
    return joined


def _django_path(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith("^"):
        raw = raw[1:]
    if raw.endswith("$"):
        raw = raw[:-1]
    return "/" + raw.lstrip("/") if raw else "/"


def _methods(args: str, default: str) -> list[str]:
    m = _METHODS_RE.search(args or "")
    if not m:
        return [default]
    found = [s.upper() for s in _ANY_STR_RE.findall(m.group(1))]
    return found or [default]


def _prefixes(text: str) -> dict[str, str]:
    """Router / blueprint variable → the path prefix its declaration and
    a same-file `include_router(..., prefix=)` give it."""
    out: dict[str, str] = {}
    for m in _PREFIX_DECL_RE.finditer(text):
        kw = _PREFIX_KW_RE.search(m.group(2))
        out[m.group(1).rsplit(".", 1)[-1]] = kw.group(1) if kw else ""
    for m in _INCLUDE_ROUTER_RE.finditer(text):
        name = m.group(1).rsplit(".", 1)[-1]
        # cut: a route name keeps only `_MAX_NAME` characters, and a prefix joined again
        # per call would otherwise grow with every call
        out[name] = _url_join(m.group(2), out.get(name, ""))[: 2 * _MAX_NAME]
    return out


class _Collector:
    def __init__(self) -> None:
        self.out: list[Facet] = []
        self.seen: set[tuple[str, str, int]] = set()

    def add(self, kind: str, name: str, line: int, conf: float = 1.0) -> None:
        name = name.strip()[:_MAX_NAME]
        if not name or (kind, name, line) in self.seen or len(self.out) >= _MAX_FACETS_PER_FILE:
            return
        self.seen.add((kind, name, line))
        self.out.append(
            Facet(kind=kind, name=name, ordinal=len(self.out) + 1, line=line, confidence=conf)
        )


def _decorated(root: Any, source: bytes, col: _Collector, prefixes: dict[str, str]) -> None:
    """Every decorated def / class: its decorator facets, its route or
    command, and `async` for a coroutine def."""

    def text(n: Any) -> str:
        return source[n.start_byte : n.end_byte].decode("utf-8", "replace")

    def visit(node: Any) -> None:
        for child in node.named_children:
            if child.type == "function_definition" and any(
                c.type == "async" for c in child.children
            ):
                col.add(FACET_MODIFIER, "async", child.start_point[0] + 1)
            if child.type == "decorated_definition":
                inner = child.child_by_field_name("definition")
                if inner is not None:
                    line = inner.start_point[0] + 1
                    name_node = inner.child_by_field_name("name")
                    def_name = text(name_node) if name_node is not None else ""
                    for d in child.named_children:
                        if d.type == "decorator":
                            _one_decorator(text(d), def_name, line, col, prefixes)
            visit(child)

    visit(root)


def _one_decorator(
    dec: str,
    def_name: str,
    line: int,
    col: _Collector,
    prefixes: dict[str, str],
) -> None:
    m = _DEC_NAME_RE.match(dec)
    if m:
        col.add(FACET_DECORATOR, m.group(1), line)
    r = _DEC_ROUTE_RE.match(dec)
    if r:
        obj, verb, path, rest = r.group(1).rsplit(".", 1)[-1], r.group(2), r.group(3), r.group(4)
        if path is None:
            return  # `@app.route` with no path is not a route
        full = _url_join(prefixes.get(obj, ""), path)
        if verb in ("websocket", "ws"):
            col.add(FACET_ROUTE, f"WS {full}", line)
        elif verb in ("route", "api_route"):
            for meth in _methods(rest, "GET"):
                col.add(FACET_ROUTE, f"{meth} {full}", line)
        else:
            col.add(FACET_ROUTE, f"{verb.upper()} {full}", line)
        return
    c = _DEC_COMMAND_RE.match(dec)
    if c and def_name:
        args = c.group(3) or ""
        kw = _NAME_KW_RE.search(args)
        first = _FIRST_STR_RE.match(args)
        name = kw.group(1) if kw else first.group(1) if first else def_name.replace("_", "-")
        col.add(FACET_COMMAND, name, line)


def _registrations(text: str, path: str, col: _Collector) -> None:
    """Routes and commands named in calls rather than decorators."""
    newlines = newline_offsets(text)
    for m in _ADD_URL_RULE_RE.finditer(text):
        for meth in _methods(m.group(2), "ANY"):
            col.add(
                FACET_ROUTE, f"{meth} {_url_join('', m.group(1))}", line_at(newlines, m.start())
            )
    for rx in _AIOHTTP_RES:
        for m in rx.finditer(text):
            verb = "ANY" if m.group(1) == "view" else m.group(1).upper()
            col.add(FACET_ROUTE, f"{verb} {m.group(2)}", line_at(newlines, m.start()))
    for m in _AIOHTTP_ROUTE_RE.finditer(text):
        verb = "ANY" if m.group(1) == "*" else m.group(1).upper()
        col.add(FACET_ROUTE, f"{verb} {m.group(2)}", line_at(newlines, m.start()))
    if posixpath.basename(path) == "urls.py" or "django.urls" in text or "django.conf.urls" in text:
        for m in _DJANGO_RE.finditer(text):
            col.add(FACET_ROUTE, f"ANY {_django_path(m.group(1))}", line_at(newlines, m.start()))
    if "starlette.routing" in text or "fastapi.routing" in text:
        for m in _STARLETTE_RE.finditer(text):
            line = line_at(newlines, m.start())
            if m.group(1) == "WebSocketRoute":
                col.add(FACET_ROUTE, f"WS {m.group(2)}", line)
            else:
                for meth in _methods(m.group(3), "GET"):
                    col.add(FACET_ROUTE, f"{meth} {m.group(2)}", line)
    if "tornado" in text:
        for m in _TORNADO_RE.finditer(text):
            col.add(FACET_ROUTE, f"ANY {m.group(1)}", line_at(newlines, m.start()), 0.9)
    for m in _ADD_PARSER_RE.finditer(text):
        col.add(FACET_COMMAND, m.group(1), line_at(newlines, m.start()))


def py_facets(pf: ParsedFile) -> list[Facet]:
    """Every route, command, decorator and async def of a Python file."""
    if pf.lang != "python" or pf.tree is None:
        return []
    text = pf.source.decode("utf-8", "replace")
    col = _Collector()
    _decorated(pf.tree.root_node, pf.source, col, _prefixes(text))
    _registrations(text, pf.path, col)
    return col.out


__all__ = [
    "FACET_COMMAND",
    "FACET_DECORATOR",
    "FACET_MODIFIER",
    "FACET_ROUTE",
    "py_facets",
]
