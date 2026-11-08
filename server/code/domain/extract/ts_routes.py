"""HTTP routes as facets.

An agent asking "which handler serves `GET /api/users/:id`" has no symbol to name: the route is a
string in a decorator, a registration call or a file path. The plane already carries deterministic
facts a search cannot rank as facet rows (`code_test_facets`: kind, name, path, line) and answers
them exactly through `code.tests_for`; a route is one more kind, `http_route`, named `<METHOD>
<path>` at the handler's line.

Four spellings, one shape:
- Next.js pages router: `pages/api/**.ts` is `ANY /api/<path>` (`[id]` →
  `:id`, `[...slug]` → `*slug`, `index` dropped) at the default export;
- Next.js app router: `app/**/route.ts` exports `GET` … `OPTIONS` handlers,
  each `<METHOD> /<segments>` (route groups `(x)` dropped); `app/**/page.tsx`
  is `PAGE /<segments>`;
- NestJS: `@Controller('users')` on the class, `@Get(':id')` … `@All()` on
  methods → `GET /users/:id` at the method (its span starts at the
  decorator);
- Express / Fastify / Koa-router / Hono: `app.get('/x', …)`,
  `router.post('/x', …)`, `fastify.route({ method, url })` → `GET /x`.

Pure: regexes over the source; the file's path supplies the convention.
"""

from __future__ import annotations

import bisect
import posixpath
import re

from server.code.domain.extract.lang_sql_model import Facet
from server.code.domain.extract.treesitter import ParsedFile

FACET_KIND = "http_route"
# Each body stops at the next `@Controller(` / `.route(`: an unclosed one is not read to
# the end of the file again from every opener before it.
_CONTROLLER_RE = re.compile(
    r"""@Controller\(\s*+(?:['"`]([^'"`\n]*)['"`]|\{(?:(?!@Controller\()[^}])*?\bpath:\s*"""
    r"""['"`]([^'"`\n]*)['"`](?:(?!@Controller\()[^}])*\})?(?:(?!@Controller\()[^)])*\)""",
    re.S,
)
_ROUTE_DECORATOR_RE = re.compile(
    r"""@(Get|Post|Put|Patch|Delete|Head|Options|All)\(\s*+(?:['"`]([^'"`\n]*)['"`])?"""
    r"""(?:(?!@(?:Get|Post|Put|Patch|Delete|Head|Options|All)\()[^)])*\)"""
)
_REGISTRATION_RE = re.compile(
    r"""\b(?:app|router|server|api|fastify|instance|route[rs]?)"""
    r"""\.(get|post|put|patch|delete|head|options|all)\(\s*['"`]([^'"`]+)['"`]"""
)
_FASTIFY_ROUTE_RE = re.compile(
    r"""\.route\(\s*\{(?:(?!\.route\()[^}])*?\bmethod\s*:\s*['"`]([A-Za-z]+)['"`]"""
    r"""(?:(?!\.route\(|\bmethod\s*:)[^}])*?\burl\s*:\s*['"`]([^'"`]+)['"`]""",
    re.S,
)
_APP_HANDLER_RE = re.compile(
    r"^[^\S\n]*export\s+(?:async\s+)?(?:function\s+(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\b"
    r"|const\s+(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s*=)",
    re.M,
)
_DEFAULT_EXPORT_RE = re.compile(r"^[^\S\n]*export\s+default\b", re.M)
_PAGES_API_RE = re.compile(r"(?:^|/)pages/api/(.+)\.[tj]sx?$")
_APP_ROUTE_RE = re.compile(r"(?:^|/)app/(.*?)(?:^|/)?(route|page)\.[tj]sx?$")


def _url_join(prefix: str, sub: str) -> str:
    parts = [p.strip("/") for p in (prefix, sub) if p and p.strip("/")]
    return "/" + "/".join(parts) if parts else "/"


def _file_route(segments: str) -> str:
    """`(dashboard)/users/[id]/index` → `/users/:id`; `[...slug]` → `*slug`."""
    out: list[str] = []
    for seg in segments.split("/"):
        if not seg or (seg.startswith("(") and seg.endswith(")")) or seg == "index":
            continue
        if seg.startswith("[...") and seg.endswith("]"):
            out.append("*" + seg[4:-1])
        elif seg.startswith("[") and seg.endswith("]"):
            out.append(":" + seg[1:-1].lstrip("."))
        else:
            out.append(seg)
    return "/" + "/".join(out) if out else "/"


def route_facets(pf: ParsedFile) -> list[Facet]:
    """Every route this file serves, as `http_route` facets."""
    text = pf.source.decode("utf-8", "replace")
    out: list[Facet] = []
    seen: set[tuple[str, int]] = set()
    newlines = [m.start() for m in re.finditer("\n", text)]

    def line_of(offset: int) -> int:  # by bisection: a count from the top per route is quadratic
        return bisect.bisect_left(newlines, offset) + 1

    def add(name: str, line: int, conf: float) -> None:
        if (name, line) in seen:
            return
        seen.add((name, line))
        out.append(
            Facet(kind=FACET_KIND, name=name, ordinal=len(out) + 1, line=line, confidence=conf)
        )

    m = _PAGES_API_RE.search(pf.path)
    if m:
        d = _DEFAULT_EXPORT_RE.search(text)
        line = line_of(d.start()) if d else 1
        add(f"ANY {_url_join('/api', _file_route(m.group(1)))}", line, 0.9)
    m = _APP_ROUTE_RE.search(pf.path)
    if m:
        route = _file_route(m.group(1))
        if m.group(2) == "route":
            for h in _APP_HANDLER_RE.finditer(text):
                add(f"{h.group(1) or h.group(2)} {route}", line_of(h.start()), 0.9)
        else:
            d = _DEFAULT_EXPORT_RE.search(text)
            add(f"PAGE {route}", line_of(d.start()) if d else 1, 0.9)
    controllers = list(_CONTROLLER_RE.finditer(text))
    closes = _brace_closes(text) if controllers else {}
    for k, c in enumerate(controllers):
        prefix = c.group(1) or c.group(2) or ""
        # a class body ends at its `}`, and no later than the next controller: an unclosed
        # one is not read again for every controller before it
        stop = controllers[k + 1].start() if k + 1 < len(controllers) else len(text)
        body_start = text.find("{", c.end(), stop)
        body_end = min(closes.get(body_start, stop), stop) if body_start >= 0 else stop
        body = text[body_start:body_end] if body_start >= 0 else ""
        for r in _ROUTE_DECORATOR_RE.finditer(body):
            method = r.group(1).upper()
            line = line_of(body_start + r.start())
            add(f"{method} {_url_join(prefix, r.group(2) or '')}", line, 1.0)
    for r in _REGISTRATION_RE.finditer(text):
        add(f"{r.group(1).upper()} {r.group(2)}", line_of(r.start()), 1.0)
    for r in _FASTIFY_ROUTE_RE.finditer(text):
        add(f"{r.group(1).upper()} {r.group(2)}", line_of(r.start()), 1.0)
    return out


def _brace_closes(text: str) -> dict[int, int]:
    """{offset of a `{`: offset of its `}`}, in one pass."""
    out: dict[int, int] = {}
    opens: list[int] = []
    for i, ch in enumerate(text):
        if ch == "{":
            opens.append(i)
        elif ch == "}" and opens:
            out[opens.pop()] = i
    return out


def is_route_candidate(path: str) -> bool:
    base = posixpath.basename(path)
    return (
        bool(_PAGES_API_RE.search(path) or _APP_ROUTE_RE.search(path))
        or base.endswith((".controller.ts", ".routes.ts", ".router.ts", ".routes.js", ".router.js"))
        or True
    )  # registrations may sit in any module; the regexes are cheap


__all__ = ["FACET_KIND", "is_route_candidate", "route_facets"]
