"""Optical character recognition of an image, in this process, through the
Tesseract and Leptonica C libraries (`libtesseract.so.5`, `libleptonica.so.6`)
loaded with ctypes: no Python package, and no process started, which an
extraction child may not do. The extractor image installs them (Debian's
`libtesseract5` and `tesseract-ocr-eng`); without them `engine()` is None and a
page or slide whose text only an image holds fails as needing OCR.

An image is given as the bytes of a file Leptonica reads (JPEG, PNG, TIFF, GIF,
WebP, JPEG 2000, PNM) and read as words, each with its box in the image's
pixels (top left origin), grouped into Tesseract's lines with each line's
baseline and height. An image below 200 pixels an inch where it is shown is
scaled to about 300 first, within the pixel bound.
"""

from __future__ import annotations

import ctypes
import re
import time
from collections.abc import Callable, Hashable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

# Not settings: bounds on hostile input.
MAX_PIXELS = 40_000_000  # an image larger than this, decoded or scaled, is not read
_MAX_RULE_BOXES = 4_000  # the rules one image's grid is read from
_DEADLINE_S = 20  # one image's recognition; past it the image is not read
# Not a setting: a picture this share of a page or slide, or larger, may carry
# text of its own and is read.
PICTURE_SHARE = 0.2
NOT_RUN = "a picture covers a fifth of it or more: its text needs OCR, which is not run"
_MIN_DPI = 200.0
# The clean-up's rule and band sizes are those of 600 pixels an inch at most: a
# finely shown image (a screenshot in a small box) is read at its own size.
_MAX_DPI = 600.0
_TARGET_DPI = 300.0
_MAX_SCALE = 3.0
_RIL_TEXTLINE = 2
_RIL_WORD = 3
# Where a Debian, Alpine or source build puts the English model.
_TESSDATA = (
    "/usr/share/tesseract-ocr/5/tessdata",
    "/usr/share/tessdata",
    "/usr/local/share/tessdata",
)


# What a table's rules read as, in a word that touches one: a stroke at its
# edge, or a word of strokes alone.
_STROKES = "|[]{}¦‖_—"
_ONLY_STROKES = re.compile(f"[{re.escape(_STROKES)}=~-]+")
# A word of vertical bars alone is a rule OCR read, near a rule it found or not
# (a light grid's): no text is a bar alone.
_BARS = re.compile("[|¦‖]+")
_NEAR = 6  # pixels: a word this close to a rule touches it
_CELL = 64  # pixels: the grid rules are found in by place


def _unruled(text: str) -> str:
    """A word OCR read beside a rule, without the rule: `[Owner` is `Owner`,
    `rota-8|` is `rota-8`, and `|` alone is nothing; a bracket the word closes
    (`arr[0]`, `[x]`) is its own."""
    if _ONLY_STROKES.fullmatch(text):
        return ""
    core = text.strip("|¦‖_—")
    for opening, closing in ("[]", "{}"):
        if core.startswith(opening) and closing not in core[1:]:
            core = core[1:]
        if core.endswith(closing) and opening not in core[:-1]:
            core = core[:-1]
    return core if core and any(ch.isalnum() for ch in core) else text


class NotDecoded(ValueError):
    """An image refused before it is decoded: its header is past the pixel bound,
    or it is in a format Leptonica does not know."""


@dataclass(frozen=True)
class Box:
    """A rule's box in an image's pixels, top left origin."""

    x0: int
    y0: int
    x1: int
    y1: int


@dataclass(frozen=True)
class Word:
    text: str
    x0: int
    y0: int  # top
    x1: int
    y1: int  # bottom
    baseline: float  # the line's baseline at the word, in pixels from the top
    height: float  # the line's height in pixels
    first: bool  # the first word of its line
    points: float  # its line's type size in points as shown (`page_points`; 0 when unread)


class Engine:
    """A loaded Tesseract: one model, reused for every image of an extraction."""

    def __init__(self, tess: ctypes.CDLL, lept: ctypes.CDLL, datapath: str, languages: str) -> None:
        self._t, self._l = tess, lept
        self._declare()
        self._api = self._t.TessBaseAPICreate()
        if (
            not self._api
            or self._t.TessBaseAPIInit3(self._api, datapath.encode(), languages.encode()) != 0
        ):
            raise OSError("tesseract did not load its model")
        for name, value in (("debug_file", "/dev/null"), ("user_defined_dpi", "300")):
            self._t.TessBaseAPISetVariable(self._api, name.encode(), value.encode())
        self.version = ctypes.string_at(self._t.TessVersion()).decode()

    def _declare(self) -> None:
        t, lp = self._t, self._l
        p, i, s = ctypes.c_void_p, ctypes.c_int, ctypes.c_char_p
        ip = ctypes.POINTER(ctypes.c_int)
        t.TessVersion.restype = p
        t.TessBaseAPICreate.restype = p
        t.TessBaseAPIInit3.argtypes = [p, s, s]
        t.TessBaseAPISetVariable.argtypes = [p, s, s]
        t.TessBaseAPISetImage2.argtypes = [p, p]
        t.TessBaseAPIRecognize.argtypes = [p, p]
        t.TessMonitorCreate.restype = p
        t.TessMonitorSetDeadlineMSecs.argtypes = [p, i]
        t.TessMonitorDelete.argtypes = [p]
        t.TessBaseAPIGetIterator.restype = p
        t.TessBaseAPIGetIterator.argtypes = [p]
        t.TessBaseAPIClear.argtypes = [p]
        t.TessResultIteratorGetPageIterator.restype = p
        t.TessResultIteratorGetPageIterator.argtypes = [p]
        t.TessResultIteratorGetUTF8Text.restype = p
        t.TessResultIteratorGetUTF8Text.argtypes = [p, i]
        t.TessResultIteratorDelete.argtypes = [p]
        t.TessPageIteratorBoundingBox.argtypes = [p, i, ip, ip, ip, ip]
        t.TessPageIteratorBaseline.argtypes = [p, i, ip, ip, ip, ip]
        t.TessPageIteratorIsAtBeginningOf.argtypes = [p, i]
        t.TessPageIteratorNext.argtypes = [p, i]
        t.TessDeleteText.argtypes = [p]
        t.TessResultIteratorWordFontAttributes.restype = s
        t.TessResultIteratorWordFontAttributes.argtypes = [p, ip, ip, ip, ip, ip, ip, ip, ip]
        lp.pixReadMem.restype = p
        lp.pixReadMem.argtypes = [s, ctypes.c_size_t]
        lp.pixReadHeaderMem.argtypes = [s, ctypes.c_size_t, ip, ip, ip, ip, ip, ip]
        lp.pixGetWidth.argtypes = [p]
        lp.pixGetHeight.argtypes = [p]
        lp.pixScale.restype = p
        lp.pixScale.argtypes = [p, ctypes.c_float, ctypes.c_float]
        lp.pixDestroy.argtypes = [ctypes.POINTER(p)]
        lp.pixConvertTo1.restype = p
        lp.pixConvertTo1.argtypes = [p, i]
        lp.pixConvertTo8.restype = p
        lp.pixConvertTo8.argtypes = [p, i]
        lp.pixInvert.restype = p
        lp.pixInvert.argtypes = [p, p]
        lp.pixCountPixels.argtypes = [p, ip, p]
        lp.pixPaintThroughMask.argtypes = [p, p, i, i, ctypes.c_uint32]
        lp.pixSubtract.restype = p
        lp.pixSubtract.argtypes = [p, p, p]
        lp.pixDilateBrick.restype = p
        lp.pixDilateBrick.argtypes = [p, p, i, i]
        # composite bricks: a long one costs a few passes, not one per pixel of it
        lp.pixOpenCompBrick.restype = p
        lp.pixOpenCompBrick.argtypes = [p, p, i, i]
        lp.pixDilateCompBrick.restype = p
        lp.pixDilateCompBrick.argtypes = [p, p, i, i]
        lp.pixConnCompBB.restype = p
        lp.pixConnCompBB.argtypes = [p, i]
        lp.boxaGetCount.argtypes = [p]
        lp.boxaGetBoxGeometry.argtypes = [p, i, ip, ip, ip, ip]
        lp.boxaDestroy.argtypes = [ctypes.POINTER(p)]

    def read(
        self, data: bytes, shown_inches: float = 0.0
    ) -> tuple[list[Word], list[Box], int, int]:
        """The words in the image file `data`, the rules it draws (long
        horizontal and vertical strokes, a scanned table's grid), and the
        image's width and height in the pixels the boxes are in. The rules are
        taken out before the words are read, as Tesseract reads a ruled table's
        cells as a picture; Tesseract is given the image in gray and sets its
        own threshold. `shown_inches` is the width it is shown at, which says
        whether it is scaled first. ValueError when Leptonica cannot read it,
        it is past the pixel bound, or its recognition passes _DEADLINE_S."""
        lp = self._l
        held: list[int] = []  # every image made here, destroyed at the end
        # its size from its header, before it is decoded: a file's own size may
        # say otherwise than the PDF's /Width and /Height
        pixels = _header_pixels(lp, data)
        if pixels > MAX_PIXELS:
            raise NotDecoded(f"its image is past {MAX_PIXELS:,} pixels")
        pix = lp.pixReadMem(data, len(data))
        if not pix:
            # a format Leptonica does not know cost nothing; a header it read may
            # have been decoded at length before the data failed
            raise (NotDecoded if pixels == 0 else ValueError)("its image could not be decoded")
        held.append(pix)
        try:
            w, h = lp.pixGetWidth(pix), lp.pixGetHeight(pix)
            if w <= 0 or h <= 0 or w * h > MAX_PIXELS:
                raise ValueError(f"its image is past {MAX_PIXELS:,} pixels")
            scale = 1.0
            if shown_inches > 0 and w / shown_inches < _MIN_DPI:
                scale = min(_MAX_SCALE, _TARGET_DPI / (w / shown_inches))
                if w * h * scale * scale > MAX_PIXELS:
                    scale = (MAX_PIXELS / (w * h)) ** 0.5
            if scale > 1.05 and (scaled := lp.pixScale(pix, scale, scale)):
                held.append(scaled)
                pix = scaled
                w, h = lp.pixGetWidth(pix), lp.pixGetHeight(pix)
            dpi = w / shown_inches if shown_inches > 0 else _TARGET_DPI
            gray = lp.pixConvertTo8(pix, 0)
            if not gray:
                raise ValueError("its image could not be turned to gray")
            held.append(gray)
            # where the strokes are, in black and white; not what OCR reads
            mono = self._mono(gray, held)
            dark = ctypes.c_int()
            lp.pixCountPixels(mono, ctypes.byref(dark), None)
            if dark.value > 0.5 * w * h:  # light text on dark: read as dark on light
                lp.pixInvert(gray, gray)
                mono = self._mono(gray, held)
            rules = self._unrule(gray, mono, min(dpi, _MAX_DPI), w, h, held)
            self._t.TessBaseAPISetImage2(self._api, gray)
            monitor = self._t.TessMonitorCreate()
            try:
                self._t.TessMonitorSetDeadlineMSecs(monitor, _DEADLINE_S * 1000)
                start = time.monotonic()
                failed = self._t.TessBaseAPIRecognize(self._api, monitor) != 0
                if time.monotonic() - start >= _DEADLINE_S:
                    raise ValueError(f"its image took past {_DEADLINE_S} s to read with OCR")
            finally:
                self._t.TessMonitorDelete(monitor)
            if failed:
                raise ValueError("its image could not be recognised")
            return list(self._words(rules, page_points(dpi))), rules, w, h
        finally:
            self._t.TessBaseAPIClear(self._api)
            for one in held:
                self._destroy(one)

    def _mono(self, gray: int, held: list[int]) -> int:
        mono = self._l.pixConvertTo1(gray, 160)
        if not mono:
            raise ValueError("its image could not be turned to black and white")
        held.append(mono)
        return int(mono)

    def _unrule(
        self, gray: int, mono: int, dpi: float, w: int, h: int, held: list[int]
    ) -> list[Box]:
        """The rules' boxes, painted out of the gray image: thin strokes, in
        black and white, longer than half an inch across or a third of an inch
        down at `dpi` (600 at most, so 300 and 210 pixels), longer than a
        letter's (one taller than that, about 36 points at 300 pixels an inch,
        loses its stem with them), outside any dark region (`mono` is the image
        in black and white)."""
        lp = self._l
        thin = max(4, round(dpi / 10))
        # A dark region thicker than `thin` both ways (a heading's band, a dark
        # panel), widened by as much: no stroke in or beside it is a rule, as the
        # gaps between its light letters would read as strokes.
        bands = lp.pixOpenCompBrick(None, mono, thin, thin)
        if bands:
            held.append(bands)
        near = lp.pixDilateCompBrick(None, bands, 2 * thin + 1, 2 * thin + 1) if bands else None
        if not near:  # read whole, a band's light letters would be painted out with it
            raise ValueError("its image's dark regions could not be found")
        held.append(near)
        rules: list[Box] = []
        across, down = min(w, max(8, round(0.5 * dpi))), min(h, max(8, round(0.35 * dpi)))
        for hsize, vsize in ((across, 1), (1, down)):
            lines = lp.pixOpenCompBrick(None, mono, hsize, vsize)
            if not lines:
                continue
            held.append(lines)
            strokes = lp.pixSubtract(None, lines, near)
            if not strokes:
                continue
            held.append(strokes)
            lines = strokes
            rules += self._boxes(lines)
            # a stroke's edges, a pixel or two either side, go with it, or OCR
            # reads what is left of them as quotes and colons
            wide = lp.pixDilateBrick(None, lines, 5, 5)
            if wide:
                held.append(wide)
                lines = wide
            lp.pixPaintThroughMask(gray, lines, 0, 0, 255)
        return rules[:_MAX_RULE_BOXES]

    def _boxes(self, pix: int) -> list[Box]:
        lp = self._l
        boxa = lp.pixConnCompBB(pix, 8)
        if not boxa:
            return []
        try:
            v = [ctypes.c_int() for _ in range(4)]
            out = []
            for i in range(min(lp.boxaGetCount(boxa), _MAX_RULE_BOXES)):
                lp.boxaGetBoxGeometry(boxa, i, *map(ctypes.byref, v))
                x, y, bw, bh = (b.value for b in v)
                out.append(Box(x, y, x + bw, y + bh))
            return out
        finally:
            ref = ctypes.c_void_p(boxa)
            lp.boxaDestroy(ctypes.byref(ref))

    def _destroy(self, pix: int) -> None:
        ref = ctypes.c_void_p(pix)
        self._l.pixDestroy(ctypes.byref(ref))

    def _words(self, rules: list[Box], to_page: Callable[[float], float]) -> Iterator[Word]:
        near = _RuleGrid(rules)
        ri = self._t.TessBaseAPIGetIterator(self._api)
        if not ri:
            return
        try:
            pi = self._t.TessResultIteratorGetPageIterator(ri)
            box = [ctypes.c_int() for _ in range(4)]
            font = [ctypes.c_int() for _ in range(8)]
            line = (0.0, 0.0, 0.0)  # the current line's baseline, height and point size
            while True:
                first = bool(self._t.TessPageIteratorIsAtBeginningOf(pi, _RIL_TEXTLINE))
                if first:
                    self._t.TessPageIteratorBoundingBox(pi, _RIL_TEXTLINE, *map(ctypes.byref, box))
                    top, bottom = box[1].value, box[3].value
                    if self._t.TessPageIteratorBaseline(pi, _RIL_TEXTLINE, *map(ctypes.byref, box)):
                        base = (box[1].value + box[3].value) / 2
                    else:
                        base = float(bottom)
                    self._t.TessResultIteratorWordFontAttributes(ri, *map(ctypes.byref, font))
                    line = (base, float(bottom - top), to_page(max(0, font[6].value)))
                ptr = self._t.TessResultIteratorGetUTF8Text(ri, _RIL_WORD)
                if ptr:
                    text = ctypes.string_at(ptr).decode("utf-8", "replace").strip()
                    self._t.TessDeleteText(ptr)
                    self._t.TessPageIteratorBoundingBox(pi, _RIL_WORD, *map(ctypes.byref, box))
                    x0, y0, x1, y1 = (b.value for b in box)
                    if _BARS.fullmatch(text):
                        text = ""
                    elif near.touches(x0, y0, x1, y1):
                        text = _unruled(text)
                    if text:
                        yield Word(text, x0, y0, x1, y1, line[0], line[1], first, line[2])
                if not self._t.TessPageIteratorNext(pi, _RIL_WORD):
                    return
        finally:
            self._t.TessResultIteratorDelete(ri)


def _header_pixels(lp: ctypes.CDLL, data: bytes) -> int:
    """The pixels an image file's header declares, 0 when it cannot be read. A
    GIF's are read here: Leptonica decodes a GIF whole to give its header."""
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return _gif_pixels(data)
    fmt, w, h, bps, spp, cmap = (ctypes.c_int() for _ in range(6))
    refs = (ctypes.byref(v) for v in (fmt, w, h, bps, spp, cmap))
    if lp.pixReadHeaderMem(data, len(data), *refs) != 0:
        return 0
    return w.value * h.value


def _gif_pixels(data: bytes) -> int:
    """A GIF's pixels as decoded: its screen's and every frame's, as each frame
    is decoded at its own size. Its blocks are walked, never decoded."""

    def u16(at: int) -> int:
        return int.from_bytes(data[at : at + 2], "little")

    def skip_table(flags: int, at: int) -> int:
        return at + 3 * 2 ** ((flags & 7) + 1) if flags & 0x80 else at

    def skip_blocks(at: int) -> int:  # data sub-blocks, to their 0 terminator
        while at < len(data) and data[at]:
            at += data[at] + 1
        return at + 1

    if len(data) < 13:
        return 0
    total = u16(6) * u16(8)
    at = skip_table(data[10], 13)
    while at < len(data) and data[at] != 0x3B:  # the trailer
        if data[at] == 0x21:  # an extension: its label, then its sub-blocks
            at = skip_blocks(at + 2)
        elif data[at] == 0x2C and at + 10 <= len(data):  # an image: its descriptor
            total += u16(at + 5) * u16(at + 7)
            at = skip_blocks(skip_table(data[at + 9], at + 10) + 1)
        else:
            break
    return total


def page_points(dpi: float) -> Callable[[float], float]:
    """A word's point size as Tesseract reports it to the size it is shown at:
    Tesseract sizes a word at the 300 pixels an inch it is set to read at
    (`user_defined_dpi`), and the image is shown at `dpi`."""
    return lambda points: points * _TARGET_DPI / dpi


class _RuleGrid:
    """Rules found by place: whether a word's box touches one."""

    def __init__(self, rules: list[Box]) -> None:
        self._rules = rules
        self._cells: dict[tuple[int, int], list[int]] = {}
        for n, r in enumerate(rules):
            for cell in self._cover(r.x0 - _NEAR, r.y0 - _NEAR, r.x1 + _NEAR, r.y1 + _NEAR):
                self._cells.setdefault(cell, []).append(n)

    @staticmethod
    def _cover(x0: int, y0: int, x1: int, y1: int) -> Iterator[tuple[int, int]]:
        for cx in range(max(0, x0) // _CELL, max(0, x1) // _CELL + 1):
            for cy in range(max(0, y0) // _CELL, max(0, y1) // _CELL + 1):
                yield cx, cy

    def touches(self, x0: int, y0: int, x1: int, y1: int) -> bool:
        for cell in self._cover(x0, y0, x1, y1):
            for n in self._cells.get(cell, ()):
                r = self._rules[n]
                if (
                    x0 <= r.x1 + _NEAR
                    and r.x0 - _NEAR <= x1
                    and y0 <= r.y1 + _NEAR
                    and r.y0 - _NEAR <= y1
                ):
                    return True
        return False


def _why(e: Exception) -> str:
    return f"{type(e).__name__}: {str(e)[:200]}" if str(e) else type(e).__name__


@dataclass
class Images:
    """OCR in one file: the images it may still read, and each image's reading
    by its own object (`key`), so one drawn on many pages or slides is read,
    and counted, once. The engine is loaded at the first image to read."""

    left: int = field(default_factory=lambda: max_images())
    _done: dict[Hashable, tuple[list[Word], list[Box], int, int] | str] = field(
        default_factory=dict
    )

    def read(
        self, key: Hashable, data: Callable[[], bytes], shown_inches: float
    ) -> tuple[list[Word], list[Box], int, int]:
        """The image's words, rules and size in pixels (`Engine.read`), from the
        file `data()` gives; ValueError naming why it is not read. Only an image
        the engine decodes counts against the file's bound, and one past it is
        not decoded."""
        got = self._done.get(key)
        if got is None:
            got, keep = self._read(data, shown_inches)
            if keep:
                self._done[key] = got
        if isinstance(got, str):
            raise ValueError(got)
        return got

    def _read(
        self, data: Callable[[], bytes], shown_inches: float
    ) -> tuple[tuple[list[Word], list[Box], int, int] | str, bool]:
        """The reading or why there is none, and whether it is the image's own
        (kept for its next showing) rather than this file's bound or OCR off."""
        eng = engine()
        if eng is None:
            return NOT_RUN, False
        if self.left <= 0:
            return f"past the {max_images()} images OCR reads in one file", False
        try:
            file = data()
        except MemoryError:
            raise  # the extraction's limit, not this image's defect
        except ValueError as e:
            return str(e), True
        except Exception as e:  # noqa: BLE001 — an image that cannot be decoded leaves its page or slide partial
            return f"its image could not be read: {_why(e)}", True
        charged = True
        try:
            return eng.read(file, shown_inches), True
        except NotDecoded as e:  # refused before it was decoded: no work spent on it
            charged = False
            return str(e), True
        except MemoryError:
            raise
        except ValueError as e:
            return str(e), True
        except Exception as e:  # noqa: BLE001 — an image OCR fails on leaves its page or slide partial
            return f"its image could not be read: {_why(e)}", True
        finally:
            if charged:
                self.left -= 1

    @property
    def version(self) -> str:
        """The Tesseract version that read this file's images, when one did."""
        eng = engine() if any(not isinstance(v, str) for v in self._done.values()) else None
        return eng.version if eng else ""


@dataclass
class _Config:
    enabled: bool = True
    languages: str = "eng"
    max_images: int = 30


_config = _Config()
_loaded: list[Engine | None] = []


def configure(enabled: bool, languages: str, max_images: int) -> None:
    """The service's OCR settings for this process, before its first extraction."""
    _config.enabled, _config.languages, _config.max_images = enabled, languages, max_images
    _loaded.clear()


def max_images() -> int:
    """The images OCR reads in one file at most; past them a page is partial."""
    return _config.max_images


def engine() -> Engine | None:
    """This process's OCR engine, loaded on first use, or None when OCR is off
    or its libraries or model are not installed."""
    if not _config.enabled:
        return None
    if not _loaded:
        _loaded.append(_load(_config.languages))
    return _loaded[0]


def _datapath(languages: str) -> str | None:
    """The directory that holds every model `languages` names, or None."""
    return next(
        (
            d
            for d in _TESSDATA
            if all((Path(d) / f"{lang}.traineddata").is_file() for lang in languages.split("+"))
        ),
        None,
    )


def check(languages: str) -> tuple[str, str]:
    """What keeps OCR with `languages` from running here, `libraries` or
    `model`, and why, or two empty strings when it can; the service checks it
    when it starts."""
    for lib in ("libtesseract.so.5", "libleptonica.so.6"):
        try:
            ctypes.CDLL(lib)
        except OSError:
            return "libraries", f"{lib} is not installed"
    if _datapath(languages) is None:
        return (
            "model",
            f"a model {languages} names is not installed (looked in {', '.join(_TESSDATA)})",
        )
    return "", ""


def _load(languages: str) -> Engine | None:
    datapath = _datapath(languages)
    if datapath is None:
        return None
    # The service starts a child with OpenMP held to one thread (`_NATIVE_ENV`):
    # a child may start none (RLIMIT_NPROC).
    try:
        tess = ctypes.CDLL("libtesseract.so.5")
        lept = ctypes.CDLL("libleptonica.so.6")
        return Engine(tess, lept, datapath, languages)
    except (OSError, AttributeError):
        return None


# ---- image files from a PDF's image streams

INVERT = bytes(range(255, -1, -1))  # a byte's bits, or a sample's value, turned over


def pnm(
    width: int, height: int, components: int, bits: int, samples: bytes, black_is_one: bool = False
) -> bytes:
    """Raw samples as a PNM file: 8-bit gray or RGB, or 1-bit gray (in a PDF,
    0 is black unless `black_is_one`; in a PBM, 1 is)."""
    if bits == 8 and components in (1, 3):
        magic = b"P5" if components == 1 else b"P6"
        need = width * height * components
        return b"%s\n%d %d\n255\n" % (magic, width, height) + samples[:need].ljust(need, b"\xff")
    if bits == 1 and components == 1:
        row = (width + 7) // 8
        need = row * height
        body = samples[:need].ljust(need, b"\xff" if not black_is_one else b"\x00")
        if not black_is_one:
            body = body.translate(INVERT)
        return b"P4\n%d %d\n" % (width, height) + body
    raise ValueError(f"its image's samples ({components} x {bits}-bit) are not read by OCR")
