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
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

# Not settings: bounds on hostile input.
MAX_PIXELS = 40_000_000  # an image larger than this, decoded or scaled, is not read
_MAX_RULE_BOXES = 4_000  # the rules one image's grid is read from
_MIN_DPI = 200.0
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


# What a table's rules read as: a stroke at a word's edge, or a word of strokes alone.
_STROKES = "|[]{}¦‖_—"
_ONLY_STROKES = re.compile(f"[{re.escape(_STROKES)}=~-]+")


def _unruled(text: str) -> str:
    """A word OCR read without the rules drawn beside it: `[Owner` is `Owner`,
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
    points: float  # its line's type size in points, as Tesseract reads it (0 when it cannot)


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
        t.TessBaseAPISetSourceResolution.argtypes = [p, i]
        t.TessResultIteratorWordFontAttributes.restype = s
        t.TessResultIteratorWordFontAttributes.argtypes = [p, ip, ip, ip, ip, ip, ip, ip, ip]
        lp.pixReadMem.restype = p
        lp.pixReadMem.argtypes = [s, ctypes.c_size_t]
        lp.pixGetWidth.argtypes = [p]
        lp.pixGetHeight.argtypes = [p]
        lp.pixScale.restype = p
        lp.pixScale.argtypes = [p, ctypes.c_float, ctypes.c_float]
        lp.pixDestroy.argtypes = [ctypes.POINTER(p)]
        lp.pixConvertTo1.restype = p
        lp.pixConvertTo1.argtypes = [p, i]
        lp.pixOpenBrick.restype = p
        lp.pixOpenBrick.argtypes = [p, p, i, i]
        lp.pixDilateBrick.restype = p
        lp.pixDilateBrick.argtypes = [p, p, i, i]
        lp.pixSubtract.restype = p
        lp.pixSubtract.argtypes = [p, p, p]
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
        cells as a picture. `shown_inches` is the width it is shown at, which
        says whether it is scaled first. ValueError when Leptonica cannot read
        it or it is past the pixel bound."""
        lp = self._l
        held: list[int] = []  # every image made here, destroyed at the end
        pix = lp.pixReadMem(data, len(data))
        if not pix:
            raise ValueError("its image could not be decoded")
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
            page, rules = self._unrule(pix, dpi, held)
            self._t.TessBaseAPISetImage2(self._api, page)
            self._t.TessBaseAPISetSourceResolution(
                self._api, max(1, round(dpi))
            )  # so a word's points are the page's
            if self._t.TessBaseAPIRecognize(self._api, None) != 0:
                raise ValueError("its image could not be recognised")
            return list(self._words()), rules, w, h
        finally:
            self._t.TessBaseAPIClear(self._api)
            for one in held:
                self._destroy(one)

    def _unrule(self, pix: int, dpi: float, held: list[int]) -> tuple[int, list[Box]]:
        """The image in black and white with its rules taken out, and the rules'
        boxes: strokes longer than half an inch across or a third of an inch
        down, longer than any letter's."""
        lp = self._l
        mono = lp.pixConvertTo1(pix, 160)
        if not mono:
            return pix, []
        held.append(mono)
        rules: list[Box] = []
        clean = mono
        for hsize, vsize in ((max(8, round(0.5 * dpi)), 1), (1, max(8, round(0.35 * dpi)))):
            lines = lp.pixOpenBrick(None, mono, hsize, vsize)
            if not lines:
                continue
            held.append(lines)
            rules += self._boxes(lines)
            # a stroke's edges, a pixel or two either side, go with it, or OCR
            # reads what is left of them as quotes and colons
            wide = lp.pixDilateBrick(None, lines, 5, 5)
            if wide:
                held.append(wide)
                lines = wide
            out = lp.pixSubtract(None, clean, lines)
            if out:
                held.append(out)
                clean = out
        return clean, rules[:_MAX_RULE_BOXES]

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

    def _words(self) -> Iterator[Word]:
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
                    line = (base, float(bottom - top), float(max(0, font[6].value)))
                ptr = self._t.TessResultIteratorGetUTF8Text(ri, _RIL_WORD)
                if ptr:
                    text = _unruled(ctypes.string_at(ptr).decode("utf-8", "replace").strip())
                    self._t.TessDeleteText(ptr)
                    self._t.TessPageIteratorBoundingBox(pi, _RIL_WORD, *map(ctypes.byref, box))
                    if text:
                        x0, y0, x1, y1 = (b.value for b in box)
                        yield Word(text, x0, y0, x1, y1, line[0], line[1], first, line[2])
                if not self._t.TessPageIteratorNext(pi, _RIL_WORD):
                    return
        finally:
            self._t.TessResultIteratorDelete(ri)


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


def _load(languages: str) -> Engine | None:
    datapath = next(
        (
            d
            for d in _TESSDATA
            if all((Path(d) / f"{lang}.traineddata").is_file() for lang in languages.split("+"))
        ),
        None,
    )
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
            body = bytes(255 - b for b in body)
        return b"P4\n%d %d\n" % (width, height) + body
    raise ValueError(f"its image's samples ({components} x {bits}-bit) are not read by OCR")
