"""A PDF page's text as it is laid out, from the positions pypdf reports for
each run of text it shows and the lines and boxes the page draws.

A page is read as lines (runs on one baseline), each cut into segments at a gap
wider than its font size. From them, in this order:

- columns: a gutter that most lines leave empty, with text on both sides, makes
  the page read as its left column, then its right, between full-width lines;
- ruled tables: a grid of horizontal and vertical lines that meet, its cells
  the text inside each box;
- borderless tables: a run of lines cut into segments that sit in the same
  bands of the page, a line with text in the first band opening a row;
- page furniture: a line no table holds, at the top or bottom of the page,
  whose text (with a letter), digits aside, recurs there on other pages, or a
  page number on its own; it is left out of the text and named in the
  artifact (`attrs.furniture`);
- the rest as paragraphs (lines on one style, split at a gap wider than the
  document's line spacing), list items (a bullet or a number first) and code
  (a monospaced font), and headings: a short paragraph set larger than the
  body text, or in bold at its size, ranked by size into levels; numbering
  (`2.1`) gives the level among headings of one style.

A table whose rows go on at the top of the next page in the same columns is
one table, its repeated header row read once.

Positions are the page as shown: its `/Rotate` applied, y growing downward.
A run's width comes from its font's widths (a font's `/Widths` or `/W` through
its ToUnicode map, or the standard fonts' metrics); a font with none is read at
half an em a character.

Pure and I/O-free; bounded by the page's operators and the rules it draws.
"""

from __future__ import annotations

import bisect
import math
import re
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

import pypdf
from pypdf.generic import ContentStream

# Not settings: bounds on hostile input and the geometry's tolerances.
_MAX_RULES = 4_000  # the rules a page is searched for grids by
_MAX_OPERATIONS = 400_000  # the drawing operators a page's rules are read from
MAX_PICTURES = 50  # the images OCR reads on one page
_MAX_INLINE = 50  # the largest inline images a page's drawing keeps
_MAX_DRAWN = 1_000  # the distinct images a page's drawing is collected for
MAX_FORM_DEPTH = 4  # how deep form XObjects are walked for rules and pictures
_MAX_CMAP_BYTES = 1 << 20  # a ToUnicode map read for widths
_MAX_CMAP_CODES = 65_536  # the codes a map's ranges are read for, and a font's widths
MAX_LINES = 5_000  # a page with more lines is read as its text layer
_MAX_TABLE_LINES = 200  # the lines one borderless run holds: no page sets more rows legibly

# The standard fonts' widths of ASCII 32-126 (Adobe's metrics), for a font that
# names one and carries none of its own.
_STANDARD = {
    "helvetica": (
        "278 278 355 556 556 889 667 191 333 333 389 584 278 333 278 278 556 556 556 556 "
        "556 556 556 556 556 556 278 278 584 584 584 556 1015 667 667 722 722 667 611 778 "
        "722 278 500 667 556 833 722 778 667 778 722 667 611 722 667 944 667 667 611 278 "
        "278 278 469 556 333 556 556 500 556 556 278 556 556 222 222 500 222 833 556 556 "
        "556 556 333 500 278 556 500 722 500 500 500 334 260 334 584"
    ),
    "helvetica-bold": (
        "278 333 474 556 556 889 722 238 333 333 389 584 278 333 278 278 556 556 556 556 "
        "556 556 556 556 556 556 333 333 584 584 584 611 975 722 722 722 722 667 611 778 "
        "722 278 556 722 611 833 722 778 667 778 722 667 611 722 667 944 667 667 611 333 "
        "278 333 584 556 333 556 611 556 611 556 333 611 611 278 278 556 278 889 611 611 "
        "611 611 389 556 333 611 556 778 556 556 500 389 280 389 584"
    ),
    "times": (
        "250 333 408 500 500 833 778 180 333 333 500 564 250 333 250 278 500 500 500 500 "
        "500 500 500 500 500 500 278 278 564 564 564 444 921 722 667 667 722 611 556 722 "
        "722 333 389 722 611 889 722 722 556 722 667 556 611 722 722 944 722 722 611 333 "
        "278 333 469 500 333 444 500 444 500 444 333 500 500 278 278 500 278 778 500 500 "
        "500 500 333 389 278 500 500 722 500 500 444 480 200 480 541"
    ),
    "times-bold": (
        "250 333 555 500 500 1000 833 278 333 333 500 570 250 333 250 278 500 500 500 500 "
        "500 500 500 500 500 500 333 333 570 570 570 500 930 722 667 722 722 667 611 778 "
        "778 389 500 778 667 944 722 778 611 778 722 556 667 722 722 1000 722 722 667 333 "
        "278 333 581 500 333 500 556 444 556 444 333 500 556 278 333 556 278 833 556 500 "
        "556 556 444 389 333 556 500 722 500 500 444 394 220 394 520"
    ),
}
_BOLD = re.compile(r"bold|black|heavy|semibold|demi", re.I)
_MONO = re.compile(r"mono|courier|consol|menlo|code|typewriter", re.I)
_HEX = re.compile(rb"<([0-9A-Fa-f]*)>")
_RANGE_TOKEN = re.compile(rb"<[0-9A-Fa-f]*>|\[|\]")
_BULLET = re.compile(r"^(?:[•◦▪▫●○■□‣⁃∙·–—*\-\ue000-\uf8ff]|\(?\d{1,3}[.)]|\(?[a-zA-Z][.)])\s+\S")
# A list item's marker alone, set apart from its text: no column of its own. A
# symbol font's bullet decodes to the private use area (U+F0B7).
_MARKER = re.compile(r"^(?:[•◦▪▫●○■□‣⁃∙·–—*\-\ue000-\uf8ff]|\(?\d{1,3}[.)]|\(?[a-zA-Z][.)])$")
_NUMBERED = re.compile(r"^(\d{1,2}(?:\.\d{1,2}){0,5})\.?\s+\S")
_PAGE_NUMBER = re.compile(
    r"^(?:page\s*)?[-–(]?\s*\d{1,4}\s*[-–)]?(?:\s*(?:of|/)\s*\d{1,4})?$", re.I
)
_WORD = re.compile(r"\w+")
# A lone surrogate, which a font's broken character map can give and no UTF-8 reader takes.
_SURROGATE = re.compile("[\ud800-\udfff]")


# ---- fonts


def _utf16(hexs: bytes) -> str:
    raw = bytes.fromhex(hexs.decode() if len(hexs) % 2 == 0 else hexs.decode() + "0")
    return raw.decode("utf-16-be", "replace") if len(raw) >= 2 else raw.decode("latin-1")


def _to_unicode(font: Any) -> dict[int, str]:
    """A font's ToUnicode map, code to text, read as far as its bounds allow."""
    ref = font.get("/ToUnicode")
    if ref is None:
        return {}
    try:
        data = ref.get_object().get_data()[:_MAX_CMAP_BYTES]
    except Exception:  # noqa: BLE001 — a damaged map costs the widths, not the text
        return {}
    out: dict[int, str] = {}
    budget = _MAX_CMAP_CODES
    for block in re.finditer(rb"beginbfchar(.*?)endbfchar", data, re.S):
        pairs = _HEX.findall(block.group(1))
        for src, dst in zip(pairs[::2], pairs[1::2], strict=False):
            if src:
                out[int(src, 16)] = _utf16(dst)
    for block in re.finditer(rb"beginbfrange(.*?)endbfrange", data, re.S):
        tokens = _RANGE_TOKEN.findall(block.group(1))
        i = 0
        while i + 2 < len(tokens) and budget > 0:
            lo, hi = tokens[i], tokens[i + 1]
            if not (lo.startswith(b"<") and hi.startswith(b"<")) or len(lo) < 3 or len(hi) < 3:
                i += 1
                continue
            a, b = int(lo[1:-1], 16), int(hi[1:-1], 16)
            span = max(0, min(b - a + 1, budget))
            budget -= span
            if tokens[i + 2] == b"[":
                j = i + 3
                k = 0
                while j < len(tokens) and tokens[j] != b"]":
                    if k < span:
                        out[a + k] = _utf16(tokens[j][1:-1])
                    k += 1
                    j += 1
                i = j + 1
            else:
                dst = tokens[i + 2][1:-1]
                if dst:
                    base = int(dst, 16)
                    width = len(dst)
                    for k in range(span):
                        out[a + k] = _utf16(f"{base + k:0{width}X}".encode())
                i += 3
    return out


class _Widths:
    """A font's character widths, in thousandths of an em, by the text pypdf
    decodes: what the page's codes show, through the font's own map."""

    def __init__(self, font: Any) -> None:
        self.map: dict[str, float] = {}
        self.default = 500.0
        self.bold = False
        self.mono = False
        if font is None:
            return
        name = str(font.get("/BaseFont", "")).lstrip("/").split("+")[-1]
        self.bold = bool(_BOLD.search(name))
        self.mono = bool(_MONO.search(name))
        try:
            self._read(font, name)
        except Exception:  # noqa: BLE001 — a damaged font dictionary costs exact widths, not text
            self.map = {}

    def _read(self, font: Any, name: str) -> None:
        codes = _to_unicode(font)
        widths: dict[int, float] = {}
        if font.get("/Subtype") == "/Type0":
            desc = font["/DescendantFonts"][0].get_object()
            self.default = float(desc.get("/DW", 1000))
            w = desc.get("/W")
            arr = list(w.get_object()) if w is not None else []
            i = 0
            budget = _MAX_CMAP_CODES  # values written, repeated ones counted again
            while i + 1 < len(arr) and budget > 0:
                first = int(arr[i])
                nxt = arr[i + 1].get_object() if hasattr(arr[i + 1], "get_object") else arr[i + 1]
                if isinstance(nxt, list):
                    for k, v in enumerate(nxt[:budget]):
                        widths[first + k] = float(v)
                    budget -= min(len(nxt), budget)
                    i += 2
                elif i + 2 < len(arr):
                    last, v = int(nxt), float(arr[i + 2])
                    for c in range(first, min(last, first + budget - 1) + 1):
                        widths[c] = v
                    budget -= max(1, min(last - first + 1, budget))
                    i += 3
                else:
                    break
        elif font.get("/Widths") is not None:
            first = int(font.get("/FirstChar", 0))
            for k, v in enumerate(list(font["/Widths"].get_object())[:_MAX_CMAP_CODES]):
                widths[first + k] = float(v)
            desc = font.get("/FontDescriptor")
            missing = desc.get_object().get("/MissingWidth") if desc is not None else None
            nonzero = [v for v in widths.values() if v > 0]
            self.default = (
                float(missing) if missing else (sum(nonzero) / len(nonzero) if nonzero else 500.0)
            )
            # a simple font's codes are its characters' (WinAnsi and Standard agree on ASCII)
            if not codes:
                codes = {c: chr(c) for c in widths if 32 <= c < 256}
        else:
            low = name.lower()
            key = (
                "times"
                if "times" in low
                else "helvetica"
                if ("helvetica" in low or "arial" in low)
                else ""
            )
            if "courier" in low:
                self.default = 600.0
                return
            if key:
                key += "-bold" if self.bold else ""
                for k, v in enumerate(_STANDARD[key].split()):
                    self.map[chr(32 + k)] = float(v)
                self.default = 500.0
            return
        for code, text in codes.items():
            w = widths.get(code)
            if w is None or not text:
                continue
            share = w / len(text)
            for ch in text:
                self.map.setdefault(ch, share)

    def of(self, text: str) -> float:
        return sum(self.map.get(ch, self.default) for ch in text)


# ---- spans and rules


@dataclass
class Span:
    x0: float
    x1: float
    y: float  # baseline, from the top of the page as shown
    size: float
    bold: bool
    mono: bool
    text: str
    # Whether a space parts it from the run before it on its line, once a line
    # has decided (None until then).
    space: bool | None = None
    ocr: bool = False  # read from an image


def _mul(m: list[float], n: list[float]) -> list[float]:
    a, b, c, d, e, f = m
    A, B, C, D, E, F = n
    return [
        a * A + b * C,
        a * B + b * D,
        c * A + d * C,
        c * B + d * D,
        e * A + f * C + E,
        e * B + f * D + F,
    ]


def _display(
    page: pypdf.PageObject,
) -> tuple[Callable[[float, float], tuple[float, float]], float, float]:
    """The page's user space to its shown space (y down), and the shown width and height."""
    box = page.cropbox
    llx, lly, urx, ury = (float(v) for v in (box.left, box.bottom, box.right, box.top))
    rot = (page.rotation or 0) % 360
    w, h = urx - llx, ury - lly
    if rot == 90:
        return (lambda x, y: (y - lly, x - llx)), h, w
    if rot == 180:
        return (lambda x, y: (urx - x, y - lly)), w, h
    if rot == 270:
        return (lambda x, y: (ury - y, urx - x)), h, w
    return (lambda x, y: (x - llx, ury - y)), w, h


Turn = Callable[[float, float], tuple[float, float]]

# A page's text read in each of the four directions, as the turn that makes it
# read left to right (y down) given the shown width and height.
_TURNS: dict[int, Callable[[float, float], Turn]] = {
    0: lambda w, h: lambda x, y: (x, y),
    90: lambda w, h: lambda x, y: (y, w - x),  # text running down the page
    180: lambda w, h: lambda x, y: (w - x, h - y),
    270: lambda w, h: lambda x, y: (h - y, x),  # text running up the page
}


def spans(
    page: pypdf.PageObject, dropped: list[Span] | None = None
) -> tuple[list[Span], list[Span], float, float, Turn]:
    """The page's runs of text, turned so that the direction most of its text
    runs in reads left to right: those runs, the others (text in another
    direction, in place), the turned page's width and height, and the turn
    from the shown page (y down) to it."""
    to, width, height = _display(page)
    fonts: dict[int, _Widths] = {}
    # Each run: its start and end in user space, its size, its font, its text.
    raw: list[list[Any]] = []
    last: list[Any] = [None]  # the matrices of the call before
    pad: list[Any] = [None]  # spaces an operator showed before its next piece: (matrices, width)

    def visit(text: str, cm: list[float], tm: list[float], font: Any, size: float) -> None:
        key = id(font)
        if key not in fonts:
            fonts[key] = _Widths(font)
        fw = fonts[key]
        body = _SURROGATE.sub("\ufffd", text.replace("\n", " "))
        m = _mul([float(v) for v in tm], [float(v) for v in cm])
        sx = math.hypot(m[0], m[1]) or 1.0
        fsize = abs(float(size or 0)) or 1.0
        ux, uy = m[0] / sx, m[1] / sx
        # pypdf reports the matrices a text operator started at: a piece of one
        # operator (cut where its font changes, a ligature from another font)
        # comes with the same ones, and goes on from the end of the piece before.
        here = (tuple(tm), tuple(cm))
        same = last[0] == here
        last[0] = here
        if same and raw and raw[-1][5] == here:
            run = raw[-1]
            if body.strip() and run[3] is fw and run[4].strip() == body.strip():
                # the same text, in the same font, at the same place again: drawn
                # twice (a faked bold, a shadow), read once
                if dropped is not None:
                    dropped.append(Span(0, 0, 0, fsize, fw.bold, fw.mono, body.strip()))
                return
            adv = fw.of(body) / 1000 * fsize * sx
            run[1] = (run[1][0] + ux * adv, run[1][1] + uy * adv)
            run[4] += body
            return
        if not body.strip():  # spaces open no run: they push the next piece of their operator on
            pad[0] = (
                here,
                (pad[0][1] if same and pad[0] and pad[0][0] == here else 0.0)
                + fw.of(body) / 1000 * fsize * sx,
            )
            return
        lead = len(body) - len(body.lstrip())
        start = fw.of(body[:lead]) / 1000 * fsize * sx
        if pad[0] and pad[0][0] == here:
            start += pad[0][1]
        pad[0] = None
        adv = fw.of(body[lead:]) / 1000 * fsize * sx
        ox, oy = m[4] + ux * start, m[5] + uy * start
        raw.append(
            [
                (ox, oy),
                (ox + ux * adv, oy + uy * adv),
                fsize * math.hypot(m[2], m[3]),
                fw,
                body[lead:],
                here,
            ]
        )

    page.extract_text(visitor_text=visit)
    runs = []
    for a, b, size, fw, text, _ in raw:
        stripped = text.rstrip()
        if not stripped:
            continue
        cut = fw.of(text[len(stripped) :]) / 1000 * size  # the trailing spaces' width
        length = math.hypot(b[0] - a[0], b[1] - a[1]) or 1.0
        end = (b[0] - (b[0] - a[0]) / length * cut, b[1] - (b[1] - a[1]) / length * cut)
        runs.append((to(*a), to(*end), size, fw, stripped))

    def direction(a: tuple[float, float], b: tuple[float, float]) -> int:
        return round(math.degrees(math.atan2(b[1] - a[1], b[0] - a[0])) / 90) % 4 * 90

    weight: Counter[int] = Counter()
    for a, b, _, _, text in runs:
        weight[direction(a, b)] += len(text)
    angle = weight.most_common(1)[0][0] if weight else 0
    turn = _TURNS[angle](width, height)
    if angle in (90, 270):
        width, height = height, width
    out: list[Span] = []
    others: dict[int, list[Span]] = defaultdict(list)  # text in another direction, by direction
    for a, b, size, fw, text in runs:
        d = direction(a, b)
        own = turn if d == angle else _TURNS[d](*_shown_size(width, height, angle))
        (x0, y0), (x1, _) = own(*a), own(*b)
        (out if d == angle else others[d]).append(Span(x0, x1, y0, size, fw.bold, fw.mono, text))
    # each other direction's runs as its lines, read in that direction
    other = [
        Span(ln.x0, ln.x1, ln.y, ln.size, ln.bold, ln.mono, ln.text)
        for d in sorted(others)
        for ln in lines_of(others[d], dropped=dropped)
    ]
    return out, other, width, height, turn


def _shown_size(width: float, height: float, angle: int) -> tuple[float, float]:
    """The shown page's width and height, from the turned page's."""
    return (height, width) if angle in (90, 270) else (width, height)


@dataclass
class _Rule:
    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def horizontal(self) -> bool:
        return abs(self.y1 - self.y0) < 1.5


@dataclass
class Picture:
    """An image the page draws: its XObject (None for one drawn inline, which OCR
    does not read), the matrix it is drawn with (its unit square to user
    space), and its box as shown and turned."""

    image: Any
    ctm: list[float]
    box: tuple[float, float, float, float]

    @property
    def area(self) -> float:
        return (self.box[2] - self.box[0]) * (self.box[3] - self.box[1])


@dataclass
class Drawing:
    """What a page draws besides its text: its rules, its image XObjects (each
    once, where it is first drawn), whether every one was collected, and the
    largest images it draws inline (up to `_MAX_INLINE`), which OCR does not
    read."""

    rules: list[_Rule]
    pictures: list[Picture]
    whole: bool
    inline: list[Picture]


def drawing(page: pypdf.PageObject, reader: pypdf.PdfReader, turn: Turn) -> Drawing:
    """The horizontal and vertical lines the page draws (stroked lines and thin
    filled boxes), the images it draws, as shown and turned as its text is, and
    whether every image was collected (not when the operators' bound,
    `_MAX_DRAWN` or `MAX_FORM_DEPTH` was reached, or a drawn name resolved to
    no object). Past the rules' bound or the operators' there are no rules."""
    shown, _, _ = _display(page)

    def to(x: float, y: float) -> tuple[float, float]:
        return turn(*shown(x, y))

    found: list[_Rule] = []
    pictures: list[Picture] = []  # each image once, where it is first drawn
    collected: set[object] = set()
    cut = [False]  # a picture not collected: past a bound, or a name of no object
    largest_inline: list[Picture] = []  # the largest images drawn inline
    budget = [_MAX_OPERATIONS]

    def add(ax: float, ay: float, bx: float, by: float) -> None:
        if len(found) > _MAX_RULES:
            return
        (x0, y0), (x1, y1) = to(ax, ay), to(bx, by)
        if abs(y1 - y0) < 1.5 and abs(x1 - x0) > 3:
            found.append(_Rule(min(x0, x1), (y0 + y1) / 2, max(x0, x1), (y0 + y1) / 2))
        elif abs(x1 - x0) < 1.5 and abs(y1 - y0) > 3:
            found.append(_Rule((x0 + x1) / 2, min(y0, y1), (x0 + x1) / 2, max(y0, y1)))

    def walk(contents: Any, resources: Any, ctm: list[float], depth: int) -> None:
        stack: list[list[float]] = []
        path: list[tuple[str, list[tuple[float, float]]]] = []
        point = [0.0, 0.0]

        def tp(x: float, y: float) -> tuple[float, float]:
            a, b, c, d, e, f = ctm
            return a * x + c * y + e, b * x + d * y + f

        for operands, op in ContentStream(contents, reader).operations:
            budget[0] -= 1
            if budget[0] <= 0:
                return
            if op == b"q":
                stack.append(list(ctm))
            elif op == b"Q":
                if stack:
                    ctm = stack.pop()
            elif op == b"cm" and len(operands) == 6:
                ctm = _mul([float(v) for v in operands], ctm)
            elif op == b"m" and len(operands) == 2:
                point = [float(operands[0]), float(operands[1])]
                path.append(("line", [tp(*point)]))
            elif op == b"l" and len(operands) == 2:
                point = [float(operands[0]), float(operands[1])]
                if path and path[-1][0] == "line":
                    path[-1][1].append(tp(*point))
            elif op == b"re" and len(operands) == 4:
                x, y, w, h = (float(v) for v in operands)
                path.append(("rect", [tp(x, y), tp(x + w, y), tp(x + w, y + h), tp(x, y + h)]))
            elif op in (b"S", b"s", b"f", b"F", b"f*", b"B", b"B*", b"b", b"b*"):
                stroke = op in (b"S", b"s", b"B", b"B*", b"b", b"b*")
                for kind, pts in path:
                    if kind == "line" and stroke:
                        for (ax, ay), (bx, by) in zip(pts, pts[1:], strict=False):
                            add(ax, ay, bx, by)
                    elif kind == "rect":
                        xs = [p[0] for p in pts]
                        ys = [p[1] for p in pts]
                        w, h = max(xs) - min(xs), max(ys) - min(ys)
                        if stroke and w > 3 and h > 3:
                            for a, b in zip(pts, pts[1:] + pts[:1], strict=True):
                                add(*a, *b)
                        elif min(w, h) <= 2.5:  # a thin filled box is a line
                            cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
                            if w >= h:
                                add(min(xs), cy, max(xs), cy)
                            else:
                                add(cx, min(ys), cx, max(ys))
                path = []
            elif op == b"n":
                path = []
            elif op == b"INLINE IMAGE":  # its box, though OCR reads no inline image
                corners = [to(*tp(u, v)) for u, v in ((0, 0), (1, 0), (1, 1), (0, 1))]
                xs, ys = [c[0] for c in corners], [c[1] for c in corners]
                largest_inline.append(
                    Picture(None, list(ctm), (min(xs), min(ys), max(xs), max(ys)))
                )
                if len(largest_inline) > _MAX_INLINE:  # the smallest goes: only size matters
                    largest_inline.remove(min(largest_inline, key=lambda p: p.area))
            elif op == b"Do" and operands and resources is not None:
                xobjects = resources.get_object().get("/XObject")
                if not xobjects:
                    continue
                ref = xobjects.get_object().get(operands[0])
                form = ref.get_object() if ref is not None else None
                # a name that resolves to no object: it may have been an image
                if form is None or not hasattr(form, "get"):
                    cut[0] = True
                    continue
                if form.get("/Subtype") == "/Image":
                    corners = [to(*tp(u, v)) for u, v in ((0, 0), (1, 0), (1, 1), (0, 1))]
                    xs, ys = [c[0] for c in corners], [c[1] for c in corners]
                    # an image drawn again (a tiled logo) is collected once
                    ref = getattr(form, "indirect_reference", None)
                    key = (ref.idnum, ref.generation) if ref is not None else id(form)
                    if key not in collected and len(pictures) >= _MAX_DRAWN:
                        cut[0] = True
                    elif key not in collected:
                        collected.add(key)
                        pictures.append(
                            Picture(form, list(ctm), (min(xs), min(ys), max(xs), max(ys)))
                        )
                    continue
                if form.get("/Subtype") != "/Form":
                    continue
                if depth >= MAX_FORM_DEPTH:  # a form nested too deep to walk: it may draw a picture
                    cut[0] = True
                    continue
                matrix = [float(v) for v in form.get("/Matrix", [1, 0, 0, 1, 0, 0])]
                walk(form, form.get("/Resources") or resources, _mul(matrix, ctm), depth + 1)

    contents = page.get_contents()
    if contents is not None:
        walk(contents, page.get("/Resources"), [1.0, 0.0, 0.0, 1.0, 0.0, 0.0], 0)
    # Past the rules' or the operators' bound a grid may be half read: no rules. Past
    # the operators' bound, the image cap or the form depth, or with a name of no
    # object, the pictures are not all collected.
    return Drawing(
        found if len(found) <= _MAX_RULES and budget[0] > 0 else [],
        pictures,
        budget[0] > 0 and not cut[0],
        largest_inline,
    )


def _pixel_to_page(
    page: pypdf.PageObject, turn: Turn, picture: Picture, width: int, height: int
) -> Callable[[float, float], tuple[float, float]]:
    """Where a pixel of `picture` (its `width` by `height` pixels, top left
    origin) lands on the page as shown and turned."""
    shown, _, _ = _display(page)
    a, b, c, d, e, f = picture.ctm

    def at(px: float, py: float) -> tuple[float, float]:
        u, v = px / width, 1 - py / height
        return turn(*shown(a * u + c * v + e, b * u + d * v + f))

    return at


def picture_spans(
    page: pypdf.PageObject, turn: Turn, picture: Picture, words: list[Any], width: int, height: int
) -> list[Span]:
    """OCR's words in `picture` (boxes in its pixels) as runs where the page
    shows them, each on its line's baseline."""
    at = _pixel_to_page(page, turn, picture, width, height)
    out: list[Span] = []
    for w in words:
        # Tesseract's point size, from its line's x-height, when it gives one
        # (already turned to the size shown, `ocr.page_points`); else the line's
        # height less the room above and below.
        size = w.points or abs(at(w.x0, w.baseline)[1] - at(w.x0, w.baseline - w.height)[1]) / 1.2
        x0, y = at(w.x0, w.baseline)
        x1, _ = at(w.x1, w.baseline)
        out.append(
            Span(
                x0=min(x0, x1),
                x1=max(x0, x1),
                y=y,
                size=size,
                bold=False,
                mono=False,
                text=w.text,
                space=None if w.first else True,
                ocr=True,
            )
        )
    return out


def picture_rules(
    page: pypdf.PageObject, turn: Turn, picture: Picture, boxes: list[Any], width: int, height: int
) -> list[_Rule]:
    """The rules OCR found in `picture` (boxes in its pixels) where the page shows them."""
    at = _pixel_to_page(page, turn, picture, width, height)
    out: list[_Rule] = []
    for box in boxes:
        (x0, y0), (x1, y1) = at(box.x0, box.y0), at(box.x1, box.y1)
        x0, x1, y0, y1 = min(x0, x1), max(x0, x1), min(y0, y1), max(y0, y1)
        if x1 - x0 >= y1 - y0:
            out.append(_Rule(x0, (y0 + y1) / 2, x1, (y0 + y1) / 2))
        else:
            out.append(_Rule((x0 + x1) / 2, y0, (x0 + x1) / 2, y1))
    return out


# ---- lines


@dataclass
class Line:
    spans: list[Span]
    segments: list[list[Span]] = field(default_factory=list)

    @property
    def x0(self) -> float:
        return self.spans[0].x0

    @property
    def x1(self) -> float:
        return max(s.x1 for s in self.spans)

    @property
    def y(self) -> float:
        return self.spans[0].y

    @property
    def size(self) -> float:
        return max(s.size for s in self.spans)

    @property
    def bold(self) -> bool:
        return all(s.bold for s in self.spans)

    @property
    def mono(self) -> bool:
        return all(s.mono for s in self.spans)

    @property
    def text(self) -> str:
        return " ".join(seg_text(s) for s in self.segments)


def seg_text(seg: list[Span]) -> str:
    out = seg[0].text
    for a, b in zip(seg, seg[1:], strict=False):
        out += (" " if _spaced(a, b) else "") + b.text
    return out


def _spaced(a: Span, b: Span) -> bool:
    if b.space is not None:
        return b.space
    return b.x0 - a.x1 > 0.15 * max(a.size, b.size)


def seg_box(seg: list[Span]) -> tuple[float, float]:
    return seg[0].x0, max(s.x1 for s in seg)


def lines_of(
    found: Iterable[Span], words: Counter[str] | None = None, dropped: list[Span] | None = None
) -> list[Line]:
    """Runs on one baseline as lines, top to bottom, each cut into segments at a
    gap wider than its font size. With `words` (the page's text layer), two runs
    the geometry sets together are parted when the word they would make is not
    one of the page's and both of its parts are: a width read too wide is no
    reason to join two words. A run drawn twice in place is read once, the
    second copy added to `dropped`."""
    ordered = sorted(found, key=lambda s: (round(s.y, 1), s.x0))
    groups: list[list[Span]] = []
    for s in ordered:
        if groups and abs(s.y - groups[-1][0].y) <= 0.3 * min(s.size, groups[-1][0].size):
            groups[-1].append(s)
        else:
            groups.append([s])
    out: list[Line] = []
    for g in groups:
        g.sort(key=lambda s: s.x0)
        kept: list[Span] = []
        for s in g:  # a run drawn twice in place (a faked bold, a shadow) is read once
            if kept and s.text == kept[-1].text and abs(s.x0 - kept[-1].x0) < 1:
                if dropped is not None:
                    dropped.append(s)
                continue
            kept.append(s)
        line = Line(kept)
        seg = [kept[0]]
        end = kept[0].x1  # the segment's right edge so far
        for a, b in zip(kept, kept[1:], strict=False):
            marker = len(seg) == 1 and not line.segments and _MARKER.match(seg[0].text)
            if not marker and b.x0 - end > 0.9 * max(a.size, b.size):
                line.segments.append(seg)
                seg = [b]
                end = b.x1
            else:
                seg.append(b)
                end = max(end, b.x1)
        line.segments.append(seg)
        if words is not None:
            for seg in line.segments:
                for a, b in zip(seg, seg[1:], strict=False):
                    if b.space is None:
                        b.space = _spaced(a, b) or _parted(a.text, b.text, words)
        out.append(line)
    return out


def _parted(left: str, right: str, words: Counter[str]) -> bool:
    """Whether the text layer reads `left` and `right` as two words where they meet."""
    tail = _WORD.findall(left[-40:])
    head = _WORD.findall(right[:40])
    if not tail or not head or not left[-1].isalnum() or not right[0].isalnum():
        return False
    a, b = tail[-1].lower(), head[0].lower()
    return (a + b) not in words and a in words and b in words


# ---- furniture


def shape(text: str) -> str:
    """A line's text with its digits as `#`: the same footer on every page."""
    return re.sub(r"\d+", "#", " ".join(text.lower().split()))


def furniture(pages: list[tuple[list[Line], float]]) -> dict[int, set[int]]:
    """Per page, the indexes of its lines that may be page furniture: a header
    or footer line with a letter whose shape (digits aside) recurs in that zone
    on other pages, or a page number alone in it. A line a table holds as a row
    is not, which the caller decides once it has found the tables."""
    zone: list[list[tuple[int, str]]] = []
    for lines, height in pages:
        cand = [(i, ln) for i, ln in enumerate(lines) if ln.y < 0.1 * height or ln.y > 0.9 * height]
        zone.append([(i, shape(ln.text)) for i, ln in cand])
    seen: Counter[str] = Counter()
    for z in zone:
        seen.update({s for _, s in z})
    n = len(pages)
    out: dict[int, set[int]] = defaultdict(set)
    for p, z in enumerate(zone):
        for i, s in z:
            text = pages[p][0][i].text.strip()
            if _PAGE_NUMBER.match(text):
                out[p].add(i)
            # a line of digits alone (a table's row of numbers) is no running header
            elif (
                n >= 2
                and seen[s] >= max(2, math.ceil(0.4 * n))
                and any(ch.isalpha() for ch in text)
            ):
                out[p].add(i)
    return out


# ---- columns


def gutter(lines: list[Line], width: float) -> float | None:
    """The x of a gutter between two columns of text, or None: a band in the
    middle of the page that at most a twentieth of the lines cross, with most
    lines' text on either side of it reaching up to it."""
    if len(lines) < 8:
        return None
    step = max(2.0, width / 2000)  # at most a few thousand slots, however wide the page
    bins = int(width / step) + 1
    occupied = [0] * bins
    for ln in lines:
        for seg in ln.segments:
            a, b = seg_box(seg)
            for k in range(max(0, int(a / step)), min(bins, int(b / step) + 1)):
                occupied[k] += 1
    lo, hi = int(0.3 * width / step), int(0.7 * width / step)
    free = [k for k in range(lo, hi) if occupied[k] <= 0.05 * len(lines)]
    if not free:
        return None
    # the widest run of free bins
    best = (free[0], free[0])
    start = prev = free[0]
    for k in [*free[1:], -1]:
        if k != prev + 1:
            if prev - start > best[1] - best[0]:
                best = (start, prev)
            start = k
        prev = k
    gx = (best[0] + best[1]) / 2 * step
    left = [seg_box(s) for ln in lines for s in ln.segments if seg_box(s)[1] <= gx]
    right = [seg_box(s) for ln in lines for s in ln.segments if seg_box(s)[0] >= gx]
    if len(left) < 4 or len(right) < 4:
        return None

    # prose: a good share of each side's segments run most of its column's width
    def filled(boxes: list[tuple[float, float]]) -> float:
        extent = max(b for _, b in boxes) - min(a for a, _ in boxes)
        return sum(1 for a, b in boxes if b - a > 0.6 * extent) / len(boxes)

    if filled(left) < 0.35 or filled(right) < 0.35:
        return None
    return gx


def split_columns(lines: list[Line], gx: float) -> list[Line]:
    """The lines in reading order: between full-width lines, the left column's
    lines, then the right's."""
    out: list[Line] = []
    left: list[Line] = []
    right: list[Line] = []

    def flush() -> None:
        out.extend(left)
        out.extend(right)
        left.clear()
        right.clear()

    for ln in lines:
        ls = [s for s in ln.spans if s.x1 <= gx + 1]
        rs = [s for s in ln.spans if s.x0 >= gx - 1]
        if len(ls) + len(rs) < len(ln.spans):  # a run across the gutter: a full-width line
            flush()
            out.append(ln)
            continue
        if ls:
            left.extend(lines_of(ls))
        if rs:
            right.extend(lines_of(rs))
    flush()
    return out


# ---- tables


@dataclass
class TableBox:
    rows: list[list[str]]
    x: list[float]  # each column's left edge
    top: float
    bottom: float
    lines: set[int] = field(default_factory=set)  # the page lines it holds
    row_pages: list[int] = field(default_factory=list)  # each row's page, once it spans pages


def _merge(vals: list[float], tol: float) -> list[float]:
    out: list[float] = []
    for v in sorted(vals):
        if out and v - out[-1] <= tol:
            continue
        out.append(v)
    return out


def grids(found: list[_Rule]) -> list[tuple[list[float], list[float]]]:
    """Each grid the rules draw: its column edges and its row edges, from rules
    that meet (two or more of each kind)."""
    hs = [r for r in found if r.horizontal]
    vs = [r for r in found if not r.horizontal]
    if len(hs) < 2 or len(vs) < 2:
        return []
    parent = list(range(len(hs) + len(vs)))

    def root(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    order = sorted(range(len(vs)), key=lambda j: vs[j].x0)
    xs_sorted = [vs[j].x0 for j in order]
    for i, h in enumerate(hs):
        # only the vertical rules within the horizontal one's span
        for k in range(
            bisect.bisect_left(xs_sorted, h.x0 - 2), bisect.bisect_right(xs_sorted, h.x1 + 2)
        ):
            j = order[k]
            v = vs[j]
            if v.y0 - 2 <= h.y0 <= v.y1 + 2:
                parent[root(i)] = root(len(hs) + j)
    comps: dict[int, tuple[list[_Rule], list[_Rule]]] = defaultdict(lambda: ([], []))
    for i, h in enumerate(hs):
        comps[root(i)][0].append(h)
    for j, v in enumerate(vs):
        comps[root(len(hs) + j)][1].append(v)
    out = []
    for across, down in comps.values():
        ys = _merge([r.y0 for r in across], 2.0)
        xs = _merge([r.x0 for r in down], 2.0)
        if len(ys) >= 2 and len(xs) >= 2 and (len(ys) >= 3 or len(xs) >= 3):
            out.append((xs, ys))
    return out


def ruled_tables(lines: list[Line], found: list[_Rule]) -> list[TableBox]:
    out: list[TableBox] = []
    by_y = sorted(range(len(lines)), key=lambda i: lines[i].y)
    ys_sorted = [lines[i].y for i in by_y]
    for xs, ys in grids(found):
        cells: dict[tuple[int, int], list[Span]] = defaultdict(list)
        held: set[int] = set()
        # only the lines within the grid's height
        span = by_y[
            bisect.bisect_left(ys_sorted, ys[0] - 1) : bisect.bisect_right(ys_sorted, ys[-1] + 1)
        ]
        for i in sorted(span):
            ln = lines[i]
            top = ln.y - 0.6 * ln.size
            if not (ys[0] - 1 <= top and ln.y <= ys[-1] + 1):
                continue
            inside = [s for s in ln.spans if xs[0] - 1 <= s.x0 and s.x1 <= xs[-1] + 2]
            if len(inside) < len(ln.spans):
                continue
            held.add(i)
            r = max(0, min(len(ys) - 2, _bisect(ys, ln.y - 0.3 * ln.size)))
            for s in inside:
                c = max(0, min(len(xs) - 2, _bisect(xs, s.x0 + 0.5)))
                cells[(r, c)].append(s)
        if not held:
            continue
        grid = [
            [_cell_text(cells.get((r, c), [])) for c in range(len(xs) - 1)]
            for r in range(len(ys) - 1)
        ]
        keep_rows = [r for r in grid if any(r)]
        keep_cols = [c for c in range(len(xs) - 1) if any(row[c] for row in keep_rows)]
        rows = [[row[c] for c in keep_cols] for row in keep_rows]
        if len(rows) < 2 or len(keep_cols) < 2:
            continue  # a box around one block of text is no table
        out.append(TableBox(rows, [xs[c] for c in keep_cols], ys[0], ys[-1], held))
    return out


def _bisect(edges: list[float], v: float) -> int:
    k = 0
    while k + 1 < len(edges) and edges[k + 1] <= v:
        k += 1
    return k


def _cell_text(found: list[Span]) -> str:
    if not found:
        return ""
    return " ".join(ln.text for ln in lines_of(found)).strip()


def borderless_tables(
    lines: list[Line], skip: set[int], width: float, edges: set[int] | None = None
) -> list[TableBox]:
    """Runs of lines whose segments sit in the same two or more bands of the
    page: a line with text in the first band opens a row, one without adds to
    the row above it. A line of `edges` (a furniture candidate: a running
    header, a footer, a page number) at either end of a run stays in it only
    when it reads as a row of it (`_edges_kept`); else it is no row."""
    edges = edges or set()
    out: list[TableBox] = []
    i = 0
    while i < len(lines):
        if i in skip or not _multi(lines[i], width):
            i += 1
            continue
        run = [i]
        j = i + 1
        while j < len(lines) and j not in skip and len(run) < _MAX_TABLE_LINES:
            gap = lines[j].y - lines[run[-1]].y
            if gap > 2.6 * lines[j].size or gap <= 0:
                break
            run.append(j)
            j += 1
        # the bands from the multi-segment lines, a furniture candidate's aside
        multis = [k for k in run if _multi(lines[k], width)]
        inner = [k for k in multis if k not in edges] or multis
        if len(multis) < 2:
            i += 1
            continue
        bands = _bands([seg_box(s) for k in inner for s in lines[k].segments])
        if len(bands) < 2:
            i += 1
            continue
        run = _edges_kept(lines, run, edges, bands)
        rows: list[dict[int, list[str]]] = []
        used: list[int] = []
        for k in run:
            placed = _placed(lines[k], bands)
            if placed is None:
                break
            if 0 in placed or not rows:
                rows.append({n: [t] for n, t in placed.items()})
            else:
                for n, t in placed.items():
                    rows[-1].setdefault(n, []).append(t)
            used.append(k)
        multi_rows = sum(1 for r in rows if len(r) >= 2)
        if len(rows) >= 2 and multi_rows >= 2 and multi_rows >= 0.5 * len(rows):
            grid = [[" ".join(r.get(n, [])) for n in range(len(bands))] for r in rows]
            out.append(
                TableBox(
                    grid,
                    [p for p, _ in bands],
                    lines[used[0]].y - lines[used[0]].size,
                    lines[used[-1]].y,
                    set(used),
                )
            )
            i = used[-1] + 1
        else:
            i += 1
    return out


def _placed(line: Line, bands: list[tuple[float, float]]) -> dict[int, str] | None:
    """`line`'s segment texts by the band each sits in, or None when a segment
    sits in no band, in two, or in one another segment holds."""
    placed: dict[int, str] = {}
    for seg in line.segments:
        a, b = seg_box(seg)
        n = [k for k, (p, q) in enumerate(bands) if a < q - 0.5 and b > p + 0.5]
        if len(n) != 1 or n[0] in placed:
            return None
        placed[n[0]] = seg_text(seg)
    return placed


def _edges_kept(
    lines: list[Line], run: list[int], edges: set[int], bands: list[tuple[float, float]]
) -> list[int]:
    """`run` without the `edges` lines at its ends that are no rows of it. A
    page number alone never stays; another candidate stays when it fills every
    band, or, within half again the run's row spacing of it, when it opens a
    row (text in the first band) or, at the top, fills every band but the first
    (a header over a column of row names)."""
    if len(run) < 3:
        return run
    gaps = sorted(lines[b].y - lines[a].y for a, b in zip(run, run[1:], strict=False))
    spacing = gaps[len(gaps) // 2]

    def row(k: int, gap: float, top: bool) -> bool:
        placed = _placed(lines[k], bands)
        if placed is None or _PAGE_NUMBER.match(lines[k].text.strip()):
            return False
        if len(placed) == len(bands):
            return True
        stub = top and set(placed) == set(range(1, len(bands)))
        return (0 in placed or stub) and gap <= 1.5 * spacing

    while (
        len(run) >= 3
        and run[0] in edges
        and not row(run[0], lines[run[1]].y - lines[run[0]].y, top=True)
    ):
        run = run[1:]
    while (
        len(run) >= 3
        and run[-1] in edges
        and not row(run[-1], lines[run[-1]].y - lines[run[-2]].y, top=False)
    ):
        run = run[:-1]
    return run


def _multi(ln: Line, width: float) -> bool:
    return len(ln.segments) >= 2 and all(
        seg_box(s)[1] - seg_box(s)[0] < 0.6 * width for s in ln.segments
    )


def _bands(boxes: list[tuple[float, float]]) -> list[tuple[float, float]]:
    out: list[list[float]] = []
    for a, b in sorted(boxes):
        if out and a < out[-1][1] - 0.5:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


# ---- elements


@dataclass
class Element:
    """A page's paragraph, list item, code block, heading candidate or table."""

    kind: str  # "text", "item", "code", "table"
    lines: list[Line] = field(default_factory=list)
    table: TableBox | None = None
    level: int = 0  # set when it is a heading
    title: str = ""  # an outline entry's title, for a heading no line holds

    @property
    def text(self) -> str:
        if self.title:
            return self.title
        if self.kind == "code":
            return _code_text(self.lines)
        return " ".join(ln.text for ln in self.lines).strip()

    @property
    def ocr(self) -> bool:
        return any(s.ocr for ln in self.lines for s in ln.spans)

    @property
    def style(self) -> tuple[float, bool]:
        return (size_class(max(ln.size for ln in self.lines)), all(ln.bold for ln in self.lines))


def size_class(size: float) -> float:
    """`size` to the step sizes are compared at: half a point, or a tenth of one
    under 6 points (OCR's text in a finely shown image, whose whole-point
    readings turn to fractions)."""
    # divided by 10, not times 0.1, so a class is exact (5.97 is 6.0, not 6.000000000000001)
    return round(size * 10) / 10 if size < 6 else round(size * 2) / 2


def _code_text(lines: list[Line]) -> str:
    left = min(ln.x0 for ln in lines)
    out = []
    for ln in lines:
        em = 0.6 * ln.size or 1.0
        out.append(" " * max(0, round((ln.x0 - left) / em)) + ln.text)
    return "\n".join(out)


def elements(
    lines: list[Line], tables: list[TableBox], spacing: Callable[[float], float]
) -> list[Element]:
    """The page's lines and tables as elements in reading order."""
    at: dict[int, TableBox] = {}
    held: set[int] = set()
    for t in tables:
        at[min(t.lines)] = t
        held |= t.lines
    out: list[Element] = []
    prev: Line | None = None
    for i, ln in enumerate(lines):
        if i in at:
            out.append(Element("table", table=at[i]))
            prev = None
            continue
        if i in held:
            continue
        kind = "code" if ln.mono else ("item" if _BULLET.match(ln.text) else "text")
        cur = out[-1] if out and out[-1].kind != "table" else None
        if cur is not None and prev is not None and _continues(cur, prev, ln, kind, spacing):
            cur.lines.append(ln)
        else:
            out.append(Element(kind, [ln]))
        prev = ln
    return out


def _continues(
    cur: Element, prev: Line, ln: Line, kind: str, spacing: Callable[[float], float]
) -> bool:
    gap = ln.y - prev.y
    if gap <= 0 or gap > 1.3 * spacing(ln.size) + 0.5:
        return False
    if cur.kind == "code" or kind == "code":
        return cur.kind == kind
    if kind == "item":
        return False  # a bullet opens an item
    if (size_class(ln.size), ln.bold) != (size_class(prev.size), prev.bold):
        return False
    return True


def body_style(all_lines: Iterable[Line]) -> tuple[float, Callable[[float], float]]:
    """The body text's size (the size most characters are set in) and, per
    size, the document's line spacing (the most common gap between lines)."""
    chars: Counter[float] = Counter()
    gaps: dict[float, Counter[float]] = defaultdict(Counter)
    prev: Line | None = None
    for ln in all_lines:
        size = size_class(ln.size)
        chars[size] += len(ln.text)
        if prev is not None and size_class(prev.size) == size and 0 < ln.y - prev.y < 3 * size:
            # a gap to a whole point, or a tenth for type under six points
            gaps[size][round(ln.y - prev.y, 0 if size >= 6 else 1)] += 1
        prev = ln
    body = chars.most_common(1)[0][0] if chars else 10.0
    common = {s: c.most_common(1)[0][0] for s, c in gaps.items() if c}

    def spacing(size: float) -> float:
        return common.get(size_class(size), 1.25 * size)

    return body, spacing


def heading_levels(els: list[Element], body: float) -> None:
    """Mark the elements that read as headings with a level: a short text
    element set larger than the body, or bold at its size; larger sizes rank
    higher, and numbering ranks headings of one style."""
    cands: list[Element] = []
    for e in els:
        # a numbered heading (`1. Scope`) reads as a list item until its style says otherwise
        if e.kind not in ("text", "item") or len(e.lines) > (3 if e.kind == "text" else 1):
            continue
        text = e.text
        words = len(text.split())
        size, bold = e.style
        if not 1 <= words <= 20 or len(text) > 160 or not _WORD.search(text):
            continue
        # OCR reads a size in whole points at 300 pixels an inch (12 for 12.5), so
        # its step up is smaller
        if size >= body * (1.08 if e.ocr else 1.12) or (bold and size >= body * 0.95):
            if text.endswith((".", ",", ";")) and not _NUMBERED.match(text):
                continue
            cands.append(e)
    styles = sorted({e.style for e in cands}, key=lambda s: (-s[0], not s[1]))
    by_style: dict[tuple[float, bool], list[Element]] = defaultdict(list)
    for e in cands:
        by_style[e.style].append(e)
    level = 0
    for style in styles:
        group = by_style[style]
        depths = {len(m.group(1).split(".")) for e in group if (m := _NUMBERED.match(e.text))}
        base = level + 1
        for e in group:
            m = _NUMBERED.match(e.text)
            e.level = min(
                6, base + (len(m.group(1).split(".")) - min(depths) if m and depths else 0)
            )
        level = max(e.level for e in group)


def text_words(text: str) -> Counter[str]:
    return Counter(w.lower() for w in _WORD.findall(text))


def text_chars(text: str) -> Counter[str]:
    """The letters and digits of `text`, apart from spacing and case."""
    return Counter(ch for ch in text.lower() if ch.isalnum())
