"""OCR in the reference extractor: which pictures are read, how their words
land on the page, and what a page or slide says when OCR cannot run. The
engine is a fake: the tests need no Tesseract."""

from __future__ import annotations

import re
import zlib
from dataclasses import dataclass, field
from io import BytesIO
from types import SimpleNamespace

import pytest
from pypdf import PdfReader

from loom_extractor import extract, ocr, pdffile
from loom_extractor import pdflayout as layout
from tests.fixtures.office_files import GRAY_IMAGE, laid_out_pdf, rule, text_at

GRAY = GRAY_IMAGE  # a 4 by 2 black image


@dataclass
class FakeEngine:
    """Reads every image as `lines`, each a line of words, in a 1000 x 1000 image."""

    lines: list[list[str]]
    version: str = "5.0.0"
    step: int = 130  # from a word's left edge to the next one's, in pixels
    points: float = 11.0
    seen: list[tuple[bytes, float]] = field(default_factory=list)
    boxes: list[ocr.Box] = field(default_factory=list)

    def read(
        self, data: bytes, shown_inches: float = 0.0
    ) -> tuple[list[ocr.Word], list[ocr.Box], int, int]:
        self.seen.append((data, shown_inches))
        words = []
        for n, line in enumerate(self.lines):
            base = 100 + 60 * n
            for k, w in enumerate(line):
                x = 50 + self.step * k
                words.append(
                    ocr.Word(w, x, base - 30, x + 120, base, base, 40.0, k == 0, self.points)
                )
        return words, self.boxes, 1000, 1000


@pytest.fixture
def engine(monkeypatch: pytest.MonkeyPatch) -> FakeEngine:
    eng = FakeEngine([["Scanned", "runbook", "text"], ["second", "line", "here"]])
    monkeypatch.setattr(ocr, "engine", lambda: eng)
    return eng


def test_a_page_of_only_an_image_is_read_with_ocr_and_cited_by_its_page(engine: FakeEngine) -> None:
    a = extract(laid_out_pdf([b"q 400 0 0 400 100 200 cm /Im1 Do Q"], image=GRAY), "s.pdf")
    assert [b.text for b in a.blocks] == ["Scanned runbook text second line here"]
    assert a.blocks[0].attrs == {"ocr": True} and a.blocks[0].locator.page_start == 1
    assert a.coverage.complete and a.attrs["ocr_pages"] == [1]
    assert a.parser_version.endswith("+tesseract5.0.0")
    data, inches = engine.seen[0]
    assert data.startswith(b"P5\n4 2\n255\n") and inches == pytest.approx(400 / 72)


def test_ocr_words_land_where_the_picture_is_drawn(engine: FakeEngine) -> None:
    # the picture is 400 points square at (100, 200) from the bottom left: the
    # image's top left (pixel 0, 0) is at (100, 192) from the shown page's top
    reader = PdfReader(BytesIO(laid_out_pdf([b"q 400 0 0 400 100 200 cm /Im1 Do Q"], image=GRAY)))
    page = reader.pages[0]
    drew = layout.drawing(page, reader, lambda x, y: (x, y))
    pictures = drew.pictures
    assert drew.whole
    (pic,) = pictures
    assert pic.box == pytest.approx((100, 192, 500, 592))
    words, _, w, h = engine.read(b"", 0)
    spans = layout.picture_spans(page, lambda x, y: (x, y), pic, words, w, h)
    first = spans[0]
    assert (first.x0, first.x1, first.y) == pytest.approx((120, 168, 232))
    assert first.ocr and first.size == 11.0 and spans[1].space is True


def test_a_large_picture_on_a_text_page_is_read_and_a_small_one_is_not(engine: FakeEngine) -> None:
    text = text_at(72, 740, "A page of text with a whiteboard photo.")
    big = extract(laid_out_pdf([text + b"q 400 0 0 300 100 300 cm /Im1 Do Q"], image=GRAY), "b.pdf")
    assert "Scanned runbook text" in "\n".join(b.text for b in big.blocks)
    small = extract(laid_out_pdf([text + b"q 40 0 0 30 100 300 cm /Im1 Do Q"], image=GRAY), "s.pdf")
    assert [b.text for b in small.blocks] == ["A page of text with a whiteboard photo."]
    assert small.coverage.complete and len(engine.seen) == 1


def test_a_diagrams_labels_on_a_text_page_are_text_not_a_table_or_a_heading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # labels set out in columns, and larger than the page's text
    labels = [["ingest", "queue", "merge"], ["api", "events", "workers"], ["a", "b", "c"]]
    eng = FakeEngine(labels, step=330, points=24.0)
    monkeypatch.setattr(ocr, "engine", lambda: eng)
    text = text_at(72, 740, "The pipeline, as drawn on the whiteboard.")
    a = extract(laid_out_pdf([text + b"q 400 0 0 300 100 300 cm /Im1 Do Q"], image=GRAY), "d.pdf")
    assert not a.tables and not [b for b in a.blocks if b.type == "heading"]
    pictured = [b for b in a.blocks if b.attrs.get("ocr")]
    assert [b.text for b in pictured] == [" ".join(line) for line in labels]
    assert all(b.type == "paragraph" and b.locator.page_start == 1 for b in pictured)
    assert a.coverage.complete


def test_without_ocr_a_large_picture_leaves_its_page_partial_and_an_image_page_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ocr, "engine", lambda: None)
    text = text_at(72, 740, "A page of text with a whiteboard photo.")
    a = extract(laid_out_pdf([text + b"q 400 0 0 300 100 300 cm /Im1 Do Q"], image=GRAY), "b.pdf")
    assert [b.text for b in a.blocks] == ["A page of text with a whiteboard photo."]
    assert [(f.unit, f.reason) for f in a.coverage.failed] == [
        ("page 1", "a picture covers a fifth of it or more: its text needs OCR, which is not run")
    ]
    assert not a.coverage.complete and a.coverage.extracted == 0
    b = extract(laid_out_pdf([b"q 400 0 0 400 100 200 cm /Im1 Do Q"], image=GRAY), "s.pdf")
    assert b.coverage.failed[0].reason == pdffile._NEEDS_OCR


def test_past_the_ocr_bound_a_page_is_partial(
    engine: FakeEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ocr, "max_images", lambda: 1)
    pages = [b"q 400 0 0 400 100 200 cm /Im%d Do Q" % n for n in (1, 2)]
    a = extract(laid_out_pdf(pages, image=[GRAY, GRAY]), "s.pdf")
    assert a.attrs["ocr_pages"] == [1]
    assert [(f.unit, f.reason) for f in a.coverage.failed] == [
        (
            "page 2",
            "no text layer, and OCR read none of its images: past the 1 images OCR reads in one file",
        )
    ]


def test_an_image_drawn_on_many_pages_is_read_and_counted_once(
    engine: FakeEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ocr, "max_images", lambda: 1)
    page = b"q 400 0 0 400 100 200 cm /Im1 Do Q"
    a = extract(laid_out_pdf([page, page, page], image=GRAY), "s.pdf")
    assert a.coverage.complete and a.attrs["ocr_pages"] == [1, 2, 3]
    assert len(engine.seen) == 1


def test_a_jbig2_image_is_not_read_and_says_so(engine: FakeEngine) -> None:
    jbig2 = (
        b"/Width 4 /Height 2 /BitsPerComponent 1 /Filter /JBIG2Decode /Length 1 >>\nstream\n\x00"
    )
    a = extract(laid_out_pdf([b"q 400 0 0 400 100 200 cm /Im1 Do Q"], image=jbig2), "j.pdf")
    assert a.coverage.failed[0].reason == (
        "no text layer, and OCR read none of its images: its image is JBIG2, which OCR does not read"
    )
    assert engine.seen == []


@pytest.mark.parametrize(
    ("entries", "data", "head", "body"),
    [
        (
            b"/ColorSpace /DeviceGray /BitsPerComponent 8",
            bytes([0, 255, 10, 20]),
            b"P5\n2 2\n255\n",
            bytes([0, 255, 10, 20]),
        ),
        (
            b"/ColorSpace /DeviceGray /BitsPerComponent 1",
            bytes([0b01000000, 0b10000000]),
            b"P4\n2 2\n",
            bytes([0b10111111, 0b01111111]),
        ),
        (
            b"/ImageMask true /BitsPerComponent 1",
            bytes([0b01000000, 0b10000000]),
            b"P4\n2 2\n",
            bytes([0b10111111, 0b01111111]),
        ),
        (
            b"/ColorSpace [/Indexed /DeviceRGB 1 <000000ffffff>] /BitsPerComponent 8",
            bytes([0, 1, 1, 0]),
            b"P6\n2 2\n255\n",
            bytes([0, 0, 0, 255, 255, 255, 255, 255, 255, 0, 0, 0]),
        ),
        (
            b"/ColorSpace /DeviceCMYK /BitsPerComponent 8",
            bytes([0, 0, 0, 0, 0, 0, 0, 255] * 2),
            b"P5\n2 2\n255\n",
            bytes([255, 0, 255, 0]),
        ),
        (  # a negative's samples, turned back
            b"/ColorSpace /DeviceGray /BitsPerComponent 8 /Decode [1 0]",
            bytes([0, 255, 10, 20]),
            b"P5\n2 2\n255\n",
            bytes([255, 0, 245, 235]),
        ),
    ],
)
def test_raw_samples_become_a_file_leptonica_reads(
    entries: bytes, data: bytes, head: bytes, body: bytes
) -> None:
    flate = zlib.compress(data)
    image = (
        b"/Width 2 /Height 2 "
        + entries
        + b" /Filter /FlateDecode /Length %d >>\nstream\n" % len(flate)
        + flate
    )
    reader = PdfReader(BytesIO(laid_out_pdf([b""], image=image)))
    xobject = reader.pages[0]["/Resources"]["/XObject"]["/Im1"].get_object()
    assert pdffile._image_file(xobject) == head + body


def test_a_jpeg_image_is_passed_as_it_is() -> None:
    jpeg = b"\xff\xd8\xff\xe0fakejpeg\xff\xd9"
    image = (
        b"/Width 2 /Height 2 /ColorSpace /DeviceGray /BitsPerComponent 8 /Filter /DCTDecode /Length %d >>\nstream\n"
        % len(jpeg)
        + jpeg
    )
    reader = PdfReader(BytesIO(laid_out_pdf([b""], image=image)))
    assert (
        pdffile._image_file(reader.pages[0]["/Resources"]["/XObject"]["/Im1"].get_object()) == jpeg
    )


@pytest.mark.parametrize(
    ("raw", "read"),
    [
        ("[Owner", "Owner"),
        ("|", ""),
        ("rota-8|", "rota-8"),
        ("arr[0]", "arr[0]"),
        ("--", ""),
        ("[x]", "[x]"),
    ],
)
def test_a_tables_rules_are_taken_off_the_words_ocr_reads(raw: str, read: str) -> None:
    assert ocr._unruled(raw) == read


def _deck(picture_cx: int, slides: int = 1) -> bytes:
    """A deck of `slides` slides, each titled and showing the one image
    `picture_cx` EMU wide."""
    from tests.unit.test_document_extraction_office import _P, _rels, _slide, _sp, _zip

    pic = (
        '<p:pic><p:blipFill><a:blip r:embed="img"/></p:blipFill>'
        f'<p:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="{picture_cx}" cy="3000000"/></a:xfrm></p:spPr></p:pic>'
    )
    ids = "".join(f'<p:sldId id="{256 + n}" r:id="s{n}"/>' for n in range(1, slides + 1))
    parts = {
        "ppt/presentation.xml": (
            f"<p:presentation {_P}><p:sldIdLst>{ids}</p:sldIdLst>"
            '<p:sldSz cx="12192000" cy="6858000"/></p:presentation>'
        ),
        "ppt/_rels/presentation.xml.rels": _rels(
            *[(f"s{n}", "slide", f"slides/slide{n}.xml") for n in range(1, slides + 1)]
        ),
        "ppt/media/image1.png": b"\x89PNG fake",
    }
    for n in range(1, slides + 1):
        parts[f"ppt/slides/slide{n}.xml"] = _slide(_sp("Whiteboard", "title") + pic)
        parts[f"ppt/slides/_rels/slide{n}.xml.rels"] = _rels(
            ("img", "image", "../media/image1.png")
        )
    return _zip(parts)


def test_a_slides_large_picture_is_read_with_ocr_into_a_block_of_its_own(
    engine: FakeEngine,
) -> None:
    a = extract(_deck(9_000_000), "d.pptx")
    assert [(b.type, b.text, b.attrs) for b in a.blocks] == [
        ("heading", "Whiteboard", {}),
        ("paragraph", "Scanned runbook text\nsecond line here", {"ocr": True}),
    ]
    assert a.coverage.complete and a.attrs["ocr_slides"] == [1]
    assert engine.seen == [(b"\x89PNG fake", pytest.approx(9_000_000 / 914_400))]
    assert a.parser_version.endswith("+tesseract5.0.0")


def test_a_slides_small_picture_is_not_read_and_without_ocr_a_large_one_fails_its_slide(
    engine: FakeEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = extract(_deck(1_000_000), "d.pptx")
    assert (
        [b.text for b in a.blocks] == ["Whiteboard"] and engine.seen == [] and a.coverage.complete
    )
    monkeypatch.setattr(ocr, "engine", lambda: None)
    b = extract(_deck(9_000_000), "d.pptx")
    assert [b.text for b in b.blocks] == ["Whiteboard"]
    assert [(f.unit, f.reason) for f in b.coverage.failed] == [
        ("slide 1", "a picture covers a fifth of it or more: its text needs OCR, which is not run")
    ]


def test_the_services_ocr_settings_are_checked_when_it_starts() -> None:
    from loom_extractor.service import Config

    token = {"LOOM_EXTRACTOR_TOKEN": "x" * 16}
    c = Config.from_env(
        {**token, "LOOM_EXTRACTOR_OCR": "off", "LOOM_EXTRACTOR_OCR_LANGUAGES": "eng+deu"}
    )
    assert (c.ocr, c.ocr_languages, c.ocr_max_images) == (False, "eng+deu", 30)
    for bad in (
        {"LOOM_EXTRACTOR_OCR": "yes"},
        {"LOOM_EXTRACTOR_OCR_LANGUAGES": "../etc"},
        {"LOOM_EXTRACTOR_OCR_MAX_IMAGES": "-1"},
    ):
        with pytest.raises(SystemExit):
            Config.from_env({**token, **bad})


def test_ocr_is_off_when_configured_so_and_loads_once(monkeypatch: pytest.MonkeyPatch) -> None:
    loads: list[str] = []
    monkeypatch.setattr(ocr, "_load", lambda languages: loads.append(languages) or None)
    monkeypatch.setattr(ocr, "_config", ocr._Config())  # put back however the test ends
    monkeypatch.setattr(ocr, "_loaded", [])
    ocr.configure(False, "eng", 30)
    assert ocr.engine() is None and loads == []
    ocr.configure(True, "eng+deu", 5)
    assert ocr.engine() is None and ocr.engine() is None and loads == ["eng+deu"]
    assert ocr.max_images() == 5
    ocr.configure(True, "eng", 30)


def test_a_text_layer_over_a_page_image_is_read_once_and_not_again_with_ocr(
    engine: FakeEngine,
) -> None:
    # a scan with its text layer: twenty lines drawn over the full page image
    lines = b"".join(
        text_at(110, 580 - 18 * k, f"Line {k} of the scanned page text.") for k in range(20)
    )
    a = extract(
        laid_out_pdf([b"q 400 0 0 400 100 200 cm /Im1 Do Q\n" + lines], image=GRAY), "t.pdf"
    )
    assert engine.seen == [] and a.coverage.complete and "ocr_pages" not in a.attrs
    assert sum(b.text.count("Line 3 of") for b in a.blocks) == 1


def test_an_image_that_fails_to_decode_leaves_its_page_partial_and_its_text_read(
    engine: FakeEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(image: object) -> bytes:
        raise KeyError("/Filter")

    monkeypatch.setattr(pdffile, "_image_file", broken)
    text = text_at(72, 740, "A page of text with a whiteboard photo.")
    a = extract(laid_out_pdf([text + b"q 400 0 0 300 100 300 cm /Im1 Do Q"], image=GRAY), "b.pdf")
    assert [b.text for b in a.blocks] == ["A page of text with a whiteboard photo."]
    assert [(f.unit, f.reason) for f in a.coverage.failed] == [
        (
            "page 1",
            "OCR did not read all of its pictures: its image could not be read: KeyError: '/Filter'",
        )
    ]


def test_a_scan_whose_layout_is_not_trusted_keeps_its_ocr_text(
    engine: FakeEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fails(*args: object) -> None:
        raise RuntimeError("layout")

    monkeypatch.setattr(pdffile, "_lay_out_page", fails)
    a = extract(laid_out_pdf([b"q 400 0 0 400 100 200 cm /Im1 Do Q"], image=GRAY), "s.pdf")
    assert (
        " ".join(b.text for b in a.blocks).split()
        == "Scanned runbook text second line here".split()
    )
    assert all(b.attrs.get("ocr") for b in a.blocks) and a.coverage.complete


def test_a_word_is_taken_off_a_rule_only_beside_one() -> None:
    grid = ocr._RuleGrid([ocr.Box(0, 100, 900, 103), ocr.Box(400, 0, 403, 900)])
    assert grid.touches(10, 80, 60, 98)  # just over the horizontal rule
    assert grid.touches(404, 300, 450, 320)  # just right of the vertical one
    assert not grid.touches(10, 300, 60, 320)  # well clear of both: `timeout = 30` stays


def test_an_image_several_slides_show_is_read_once(
    engine: FakeEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ocr, "max_images", lambda: 1)
    a = extract(_deck(6_000_000, slides=3), "d.pptx")
    assert a.coverage.complete and a.attrs["ocr_slides"] == [1, 2, 3]
    assert len(engine.seen) == 1


def test_a_picture_read_leaves_the_text_parts_their_room() -> None:
    from loom_extractor.ooxml import Package

    pkg = Package(_deck(6_000_000))
    before = pkg._read
    assert pkg.media("ppt/media/image1.png", 1024) == b"\x89PNG fake"
    assert pkg._read == before and pkg._media == len(b"\x89PNG fake")


def test_the_real_engine_reads_a_ruled_image_end_to_end() -> None:
    if ocr.check("eng")[0]:
        pytest.skip("Tesseract is not installed here")
    eng = ocr._load("eng")
    assert eng is not None
    # a white image 600 x 400 with a horizontal and a vertical rule, 3 pixels wide
    rows = []
    for y in range(400):
        row = bytearray(b"\xff" * 600)
        if 200 <= y < 203:
            row[:] = b"\x00" * 600
        row[300:303] = b"\x00\x00\x00"
        rows.append(bytes(row))
    words, rules, w, h = eng.read(ocr.pnm(600, 400, 1, 8, b"".join(rows)), 2.0)
    assert (w, h) == (600, 400) and words == []
    assert {r.y1 - r.y0 <= 4 for r in rules} == {True, False}  # one rule each way


def test_the_service_refuses_a_missing_model_it_was_set_to_and_warns_without_ocr(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from loom_extractor.__main__ import check_ocr
    from loom_extractor.service import Config

    config = Config(token="t", ocr_languages="deu")
    monkeypatch.setattr(
        ocr, "check", lambda languages: ("model", "a model deu names is not installed")
    )
    with pytest.raises(SystemExit, match="LOOM_EXTRACTOR_OCR_LANGUAGES"):
        check_ocr(config, {"LOOM_EXTRACTOR_OCR_LANGUAGES": "deu"})
    monkeypatch.setattr(
        ocr, "check", lambda languages: ("libraries", "libtesseract.so.5 is not installed")
    )
    check_ocr(Config(token="t"), {})
    assert "OCR cannot run (libtesseract.so.5 is not installed)" in caplog.text


def test_an_image_drawn_again_on_its_page_is_read_once_there(engine: FakeEngine) -> None:
    page = b"q 400 0 0 400 100 200 cm /Im1 Do Q\n" * 5
    a = extract(laid_out_pdf([page], image=GRAY), "s.pdf")
    assert " ".join(b.text for b in a.blocks).count("Scanned runbook text") == 1


def test_a_page_past_its_picture_bound_is_partial(
    engine: FakeEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(layout, "MAX_PICTURES", 2)
    page = b"".join(
        b"q 100 0 0 100 %d 200 cm /Im%d Do Q\n" % (20 + 120 * n, n + 1) for n in range(3)
    )
    a = extract(laid_out_pdf([page], image=[GRAY] * 3), "s.pdf")
    assert [f.reason for f in a.coverage.failed] == [
        "OCR did not read all of its pictures: it shows more than 2 pictures OCR would read"
    ]


def test_a_slide_with_a_chart_beside_a_picture_ocr_read_is_not_complete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.unit.test_document_extraction_office import _P, _rels, _slide, _zip

    eng = FakeEngine([])  # the picture holds no text
    monkeypatch.setattr(ocr, "engine", lambda: eng)
    pic = (
        '<p:pic><p:blipFill><a:blip r:embed="img"/></p:blipFill>'
        '<p:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="6000000" cy="3000000"/></a:xfrm></p:spPr></p:pic>'
    )
    chart = "<p:graphicFrame><a:graphic><a:graphicData/></a:graphic></p:graphicFrame>"
    deck = _zip(
        {
            "ppt/presentation.xml": (
                f'<p:presentation {_P}><p:sldIdLst><p:sldId id="256" r:id="s1"/></p:sldIdLst>'
                '<p:sldSz cx="12192000" cy="6858000"/></p:presentation>'
            ),
            "ppt/_rels/presentation.xml.rels": _rels(("s1", "slide", "slides/slide1.xml")),
            "ppt/slides/slide1.xml": _slide(pic + chart),
            "ppt/slides/_rels/slide1.xml.rels": _rels(("img", "image", "../media/image1.png")),
            "ppt/media/image1.png": b"\x89PNG fake",
        }
    )
    a = extract(deck, "c.pptx")
    assert not a.coverage.complete and len(eng.seen) == 1
    assert [f.reason for f in a.coverage.failed] == [
        "no text: its pictures, charts or diagrams are not read"
    ]


def _one_slide(shapes: str, parts: dict[str, str] | None = None, size: bool = True) -> bytes:
    """A deck of one slide of `shapes` showing image `img`, with a slide size of
    12192000 x 6858000 EMU unless not `size`. `parts` adds parts to the deck or
    replaces them, the slide's own relationships included; the slide names a layout
    when it is given."""
    from tests.unit.test_document_extraction_office import _P, _rels, _slide, _zip

    rels = [("img", "image", "../media/image1.png")]
    if parts:
        rels.append(("lay", "slideLayout", "../slideLayouts/slideLayout1.xml"))
    sld_sz = '<p:sldSz cx="12192000" cy="6858000"/>' if size else ""
    return _zip(
        {
            "ppt/presentation.xml": (
                f'<p:presentation {_P}><p:sldIdLst><p:sldId id="256" r:id="s1"/></p:sldIdLst>'
                f"{sld_sz}</p:presentation>"
            ),
            "ppt/_rels/presentation.xml.rels": _rels(("s1", "slide", "slides/slide1.xml")),
            "ppt/slides/slide1.xml": _slide(shapes),
            "ppt/slides/_rels/slide1.xml.rels": _rels(*rels),
            "ppt/media/image1.png": b"\x89PNG fake",
            **(parts or {}),
        }
    )


def _pic(spPr: str = "<p:spPr/>", ph: str = "") -> str:
    return (
        f'<p:pic><p:nvPicPr><p:cNvPr id="4" name="p"/><p:cNvPicPr/><p:nvPr>{ph}</p:nvPr></p:nvPicPr>'
        f'<p:blipFill><a:blip r:embed="img"/></p:blipFill>{spPr}</p:pic>'
    )


def _holder(ph: str, cx: int) -> str:
    """A placeholder `ph` of a layout or master, `cx` EMU wide."""
    return (
        f'<p:sp><p:nvSpPr><p:cNvPr id="2" name="h"/><p:cNvSpPr/><p:nvPr>{ph}</p:nvPr></p:nvSpPr>'
        f'<p:spPr><a:xfrm><a:ext cx="{cx}" cy="5000000"/></a:xfrm></p:spPr></p:sp>'
    )


def _layout(layout: str, master: str) -> dict[str, str]:
    from tests.unit.test_document_extraction_office import _P, _rels

    def part(tag: str, shapes: str) -> str:
        return f"<p:{tag} {_P}><p:cSld><p:spTree>{shapes}</p:spTree></p:cSld></p:{tag}>"

    return {
        "ppt/slideLayouts/slideLayout1.xml": part("sldLayout", layout),
        "ppt/slideLayouts/_rels/slideLayout1.xml.rels": _rels(
            ("m", "slideMaster", "../slideMasters/slideMaster1.xml")
        ),
        "ppt/slideMasters/slideMaster1.xml": part("sldMaster", master),
    }


_UNSIZED = (
    '<p:sp><p:nvSpPr><p:cNvPr id="2" name="h"/><p:cNvSpPr/><p:nvPr>{}</p:nvPr></p:nvSpPr>'
    "<p:spPr/></p:sp>"
)


@pytest.mark.parametrize(
    ("ph", "parts", "read"),
    [
        # sized by its layout's placeholder of the same index, small or large
        (
            '<p:ph type="pic" idx="1"/>',
            _layout(_holder('<p:ph type="pic" idx="1"/>', 9_000_000), ""),
            True,
        ),
        (
            '<p:ph type="pic" idx="1"/>',
            _layout(_holder('<p:ph type="pic" idx="1"/>', 1_000_000), ""),
            False,
        ),
        # the index wins over a placeholder of its type that comes first
        (
            '<p:ph type="pic" idx="1"/>',
            _layout(
                _holder('<p:ph type="pic" idx="3"/>', 1_000_000)
                + _holder('<p:ph type="pic" idx="1"/>', 9_000_000),
                "",
            ),
            True,
        ),
        # an untyped placeholder is content: of no index the layout has, it takes the
        # layout's untyped one, not its body
        (
            '<p:ph sz="quarter" idx="5"/>',
            _layout(
                _holder('<p:ph type="body" idx="1"/>', 1_000_000)
                + _holder('<p:ph sz="quarter" idx="2"/>', 9_000_000),
                _holder('<p:ph type="body"/>', 1_000_000),
            ),
            True,
        ),
        # the layout's placeholder of its index has no size: the master's body has, and
        # another of its type in the layout is not taken for it
        (
            '<p:ph type="pic" idx="1"/>',
            _layout(
                _UNSIZED.format('<p:ph type="pic" idx="1"/>')
                + _holder('<p:ph type="pic" idx="2"/>', 1_000_000),
                _holder('<p:ph type="title"/>', 1_000_000)
                + _holder('<p:ph type="body"/>', 9_000_000),
            ),
            True,
        ),
        (
            '<p:ph type="pic" idx="1"/>',
            _layout(
                _UNSIZED.format('<p:ph type="pic" idx="1"/>'),
                _holder('<p:ph type="title"/>', 9_000_000)
                + _holder('<p:ph type="body"/>', 1_000_000),
            ),
            False,
        ),
        # a content placeholder is the master's body
        (
            '<p:ph idx="1"/>',
            _layout(_UNSIZED.format('<p:ph idx="1"/>'), _holder('<p:ph type="body"/>', 9_000_000)),
            True,
        ),
        # no layout: a size stated nowhere is taken as the slide's
        ('<p:ph type="pic" idx="1"/>', None, True),
    ],
)
def test_a_slides_picture_in_a_placeholder_is_sized_by_its_layout(
    engine: FakeEngine, ph: str, parts: dict[str, str] | None, read: bool
) -> None:
    from tests.unit.test_document_extraction_office import _sp

    a = extract(_one_slide(_sp("Architecture", "title") + _pic(ph=ph), parts), "p.pptx")
    assert a.coverage.complete and len(engine.seen) == int(read)
    assert [b.attrs for b in a.blocks][1:] == ([{"ocr": True}] if read else [])


@pytest.mark.parametrize("broken", [False, True])
def test_layouts_and_their_master_are_read_once_however_many_pictures_sit_in_them(
    engine: FakeEngine, monkeypatch: pytest.MonkeyPatch, broken: bool
) -> None:
    from loom_extractor.ooxml import Package
    from tests.unit.test_document_extraction_office import _P, _rels, _slide, _sp, _zip

    reads: list[str] = []
    xml = Package.xml
    monkeypatch.setattr(
        Package, "xml", lambda self, name, limit=None: reads.append(name) or xml(self, name, limit)
    )

    def part(tag: str, shapes: str) -> str:
        return f"<p:{tag} {_P}><p:cSld><p:spTree>{shapes}</p:spTree></p:cSld></p:{tag}>"

    # two layouts of one master, neither sizing its placeholder: each picture asks the master
    unsized = part("sldLayout", _UNSIZED.format('<p:ph type="pic" idx="1"/>'))
    master_rels = _rels(("m", "slideMaster", "../slideMasters/slideMaster1.xml"))
    pics = _sp("Architecture", "title") + "".join(
        _pic(ph=f'<p:ph type="pic" idx="{n}"/>') for n in (1, 2, 3)
    )
    parts: dict[str, str | bytes] = {
        "ppt/presentation.xml": (
            f"<p:presentation {_P}><p:sldIdLst>"
            + "".join(f'<p:sldId id="{255 + n}" r:id="s{n}"/>' for n in (1, 2, 3, 4))
            + '</p:sldIdLst><p:sldSz cx="12192000" cy="6858000"/></p:presentation>'
        ),
        "ppt/_rels/presentation.xml.rels": _rels(
            *[(f"s{n}", "slide", f"slides/slide{n}.xml") for n in (1, 2, 3, 4)]
        ),
        "ppt/media/image1.png": b"\x89PNG fake",
        # a master that is not XML fails each slide that asks it, read once all the same
        "ppt/slideMasters/slideMaster1.xml": "not xml"
        if broken
        else part("sldMaster", _holder('<p:ph type="body"/>', 1_000_000)),
    }
    for n in (1, 2):
        parts[f"ppt/slideLayouts/slideLayout{n}.xml"] = unsized
        parts[f"ppt/slideLayouts/_rels/slideLayout{n}.xml.rels"] = master_rels
    for n in (1, 2, 3, 4):
        parts[f"ppt/slides/slide{n}.xml"] = _slide(pics)
        parts[f"ppt/slides/_rels/slide{n}.xml.rels"] = _rels(
            ("img", "image", "../media/image1.png"),
            ("lay", "slideLayout", f"../slideLayouts/slideLayout{1 + n % 2}.xml"),
        )
    a = extract(_zip(parts), "p.pptx")
    assert engine.seen == []
    if broken:
        assert [f.unit for f in a.coverage.failed] == [f"slide {n}" for n in (1, 2, 3, 4)]
        assert all("its pictures could not be read" in f.reason for f in a.coverage.failed)
        assert [b.text for b in a.blocks] == ["Architecture"] * 4
    else:
        assert a.coverage.complete
    for n in (1, 2):
        assert reads.count(f"ppt/slideLayouts/slideLayout{n}.xml") == 1
        assert reads.count(f"ppt/slideLayouts/_rels/slideLayout{n}.xml.rels") == 1
    assert reads.count("ppt/slideMasters/slideMaster1.xml") == 1


def test_a_slide_whose_relationships_cannot_be_read_keeps_its_text_and_fails_once(
    engine: FakeEngine,
) -> None:
    from tests.unit.test_document_extraction_office import _sp

    large = '<p:spPr><a:xfrm><a:ext cx="9000000" cy="4500000"/></a:xfrm></p:spPr>'
    deck = _one_slide(
        _sp("Architecture", "title") + _pic(large),
        {"ppt/slides/_rels/slide1.xml.rels": "not xml"},
    )
    a = extract(deck, "r.pptx")
    assert [b.text for b in a.blocks] == ["Architecture"] and engine.seen == []
    assert [f.unit for f in a.coverage.failed] == ["slide 1"]
    assert a.coverage.failed[0].reason.startswith("its relationships could not be read")


def test_a_grouped_picture_is_sized_on_its_slide_and_a_deck_with_no_size_has_the_default(
    engine: FakeEngine,
) -> None:
    from tests.unit.test_document_extraction_office import _sp

    title = _sp("Architecture", "title")
    small = '<p:spPr><a:xfrm><a:ext cx="1000000" cy="500000"/></a:xfrm></p:spPr>'
    # a group drawn nine times the size of its own child space
    group = (
        '<p:grpSp><p:nvGrpSpPr><p:cNvPr id="3" name="g"/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>'
        '<p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="9000000" cy="4500000"/>'
        '<a:chOff x="0" y="0"/><a:chExt cx="1000000" cy="500000"/></a:xfrm></p:grpSpPr>'
        f"{_pic(small)}</p:grpSp>"
    )
    assert extract(_one_slide(title + _pic(small)), "g.pptx").coverage.complete
    assert engine.seen == []
    a = extract(_one_slide(title + group), "g.pptx")
    assert [b.attrs for b in a.blocks] == [{}, {"ocr": True}] and len(engine.seen) == 1
    large = '<p:spPr><a:xfrm><a:ext cx="9000000" cy="4500000"/></a:xfrm></p:spPr>'
    b = extract(_one_slide(title + _pic(large), size=False), "g.pptx")
    assert [b.attrs for b in b.blocks] == [{}, {"ocr": True}] and len(engine.seen) == 2


def test_a_chart_beside_a_picture_whose_text_ocr_read_still_fails_its_slide(
    engine: FakeEngine,
) -> None:
    large = '<p:spPr><a:xfrm><a:ext cx="9000000" cy="4500000"/></a:xfrm></p:spPr>'
    chart = "<p:graphicFrame><a:graphic><a:graphicData/></a:graphic></p:graphicFrame>"
    a = extract(_one_slide(_pic(large) + chart), "c.pptx")
    assert [b.attrs for b in a.blocks] == [{"ocr": True}]
    assert [(f.unit, f.reason) for f in a.coverage.failed] == [
        ("slide 1", "its charts or diagrams are not read")
    ]


def test_an_image_the_engine_cannot_decode_leaves_the_files_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Undecodable(FakeEngine):
        def read(
            self, data: bytes, shown_inches: float = 0.0
        ) -> tuple[list[ocr.Word], list[ocr.Box], int, int]:
            if data == b"emf":
                raise ocr.NotDecoded("its image could not be decoded")
            return super().read(data, shown_inches)

    eng = Undecodable([["text"]])
    monkeypatch.setattr(ocr, "engine", lambda: eng)
    images = ocr.Images(left=1)
    for n in range(3):
        with pytest.raises(ValueError, match="could not be decoded"):
            images.read(("emf", n), lambda: b"emf", 1.0)
    assert images.read("png", lambda: b"png", 1.0)[0][0].text == "text"
    with pytest.raises(ValueError, match="past the"):
        images.read("png2", lambda: b"png", 1.0)


@pytest.mark.parametrize(("pixels", "error"), [(0, ocr.NotDecoded), (100, ValueError)])
def test_a_failed_decode_is_not_decoded_only_when_its_header_was_not_read(
    monkeypatch: pytest.MonkeyPatch, pixels: int, error: type[ValueError]
) -> None:
    monkeypatch.setattr(ocr, "_header_pixels", lambda lp, data: pixels)
    eng = object.__new__(ocr.Engine)  # no libraries: only Leptonica's decode is called
    eng._l = SimpleNamespace(pixReadMem=lambda data, n: 0)
    with pytest.raises(ValueError, match="could not be decoded") as raised:
        eng.read(b"\x89PNG broken", 1.0)
    assert type(raised.value) is error


def test_an_image_whose_decode_fails_after_its_header_uses_the_files_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Broken(FakeEngine):
        def read(
            self, data: bytes, shown_inches: float = 0.0
        ) -> tuple[list[ocr.Word], list[ocr.Box], int, int]:
            raise ValueError("its image could not be decoded")

    monkeypatch.setattr(ocr, "engine", lambda: Broken([]))
    images = ocr.Images(left=1)
    with pytest.raises(ValueError, match="could not be decoded"):
        images.read("a", lambda: b"png", 1.0)
    with pytest.raises(ValueError, match="past the"):
        images.read("b", lambda: b"png", 1.0)


def test_an_image_page_whose_layout_fails_says_so(
    engine: FakeEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fails(*args: object) -> None:
        raise RuntimeError("layout")

    monkeypatch.setattr(pdffile, "_read_layout", fails)
    a = extract(laid_out_pdf([b"q 400 0 0 400 100 200 cm /Im1 Do Q"], image=GRAY), "s.pdf")
    assert [f.reason for f in a.coverage.failed] == [
        "no text layer, and its layout could not be read: RuntimeError"
    ]


def test_the_real_engine_reads_light_text_on_a_dark_band() -> None:
    if ocr.check("eng")[0]:
        pytest.skip("Tesseract is not installed here")
    eng = ocr._load("eng")
    assert eng is not None
    # a dark band 1200 x 400, its strokes no rules: what OCR reads of it is not painted out
    words, rules, _, _ = eng.read(ocr.pnm(1200, 400, 1, 8, bytes([40]) * 1200 * 400), 4.0)
    assert rules == [] and words == []


def test_a_text_page_of_many_small_pictures_is_complete(engine: FakeEngine) -> None:
    icons = b"".join(
        b"q 10 0 0 10 %d 300 cm /Im%d Do Q\n" % (20 + 11 * n, n + 1) for n in range(52)
    )
    text = text_at(72, 740, "A page of text with a row of icons.")
    a = extract(laid_out_pdf([text + icons], image=[GRAY] * 52), "i.pdf")
    assert a.coverage.complete and engine.seen == []


def test_an_image_past_the_files_bound_is_not_decoded(engine: FakeEngine) -> None:
    images = ocr.Images(left=0)
    decoded: list[int] = []
    with pytest.raises(ValueError, match="past the"):
        images.read((1, 0), lambda: decoded.append(1) or b"", 1.0)
    assert decoded == []


def test_an_image_page_names_why_its_image_was_not_read(
    engine: FakeEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    inline = b"q 400 0 0 400 100 200 cm BI /W 1 /H 1 /CS /G /BPC 8 ID \x00 EI Q"
    a = extract(laid_out_pdf([inline]), "i.pdf")
    assert a.coverage.failed[0].reason == (
        "no text layer: its image is drawn inline, which OCR does not read"
    )
    # an image the page holds and never draws shows nothing: the page is complete
    b = extract(laid_out_pdf([b"q Q"], image=GRAY), "n.pdf")
    assert b.coverage.complete

    def broken(*args: object) -> None:
        raise KeyError("/XObject")

    monkeypatch.setattr(layout, "drawing", broken)
    c = extract(laid_out_pdf([b"q 400 0 0 400 100 200 cm /Im1 Do Q"], image=GRAY), "d.pdf")
    assert c.coverage.failed[0].reason == "no text layer, and its drawing could not be read"


def test_the_real_engine_finds_no_rule_in_a_dark_band_between_its_letters() -> None:
    if ocr.check("eng")[0]:
        pytest.skip("Tesseract is not installed here")
    eng = ocr._load("eng")
    assert eng is not None
    # a light image 1800 x 900, a dark band 140 high holding light bars (a heading's
    # letters, the dark between them is no rule), and a thin rule below
    rows = []
    for y in range(900):
        row = bytearray(b"\xf0" * 1800)
        if 60 <= y < 200:
            row[:] = b"\x1e" * 1800
            if 100 <= y < 150:
                for x in range(80, 1200, 24):
                    row[x : x + 12] = b"\xff" * 12
        if 419 <= y < 422:
            row[60:1701] = b"\x00" * 1641
        rows.append(bytes(row))
    _, rules, _, _ = eng.read(ocr.pnm(1800, 900, 1, 8, b"".join(rows)), 6.0)
    assert [(r.y0, r.y1) for r in rules] == [(419, 422)]


def test_a_text_page_whose_pictures_are_not_all_collected_is_partial(
    engine: FakeEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(layout, "_MAX_DRAWN", 2)
    icons = b"".join(b"q 10 0 0 10 %d 300 cm /Im%d Do Q\n" % (20 + 11 * n, n + 1) for n in range(2))
    shot = b"q 400 0 0 300 100 300 cm /Im3 Do Q\n"  # a screenshot past the cap
    text = text_at(72, 740, "A page of text, two icons and a screenshot.")
    a = extract(laid_out_pdf([text + icons + shot], image=[GRAY] * 3), "c.pdf")
    assert not a.coverage.complete and engine.seen == []
    assert a.coverage.failed[0].reason == (
        "OCR did not read all of its pictures:"
        " not every picture it draws could be collected, so one may be unread"
    )


def test_a_page_past_its_drawing_bound_with_no_image_is_complete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(layout, "_MAX_OPERATIONS", 5)
    lines = b"".join(rule(72, 700 - 4 * n, 500, 700 - 4 * n) for n in range(20))
    a = extract(laid_out_pdf([text_at(72, 740, "A dense chart drawn in lines.") + lines]), "v.pdf")
    assert a.coverage.complete


def test_tesseracts_point_size_is_turned_to_the_size_shown() -> None:
    # Tesseract sizes a word at 300 pixels an inch; shown at 1,500, it is a fifth as large
    assert ocr.page_points(1500.0)(12.0) == pytest.approx(2.4)
    assert ocr.page_points(300.0)(12.0) == pytest.approx(12.0)


def test_with_ocr_off_a_page_past_its_drawing_bound_says_ocr_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ocr, "engine", lambda: None)
    monkeypatch.setattr(layout, "_MAX_DRAWN", 1)
    pictures = b"q 400 0 0 300 100 300 cm /Im1 Do Q\nq 400 0 0 300 100 300 cm /Im2 Do Q\n"
    text = text_at(72, 740, "A page of text and two screenshots.")
    a = extract(laid_out_pdf([text + pictures], image=[GRAY, GRAY]), "o.pdf")
    assert a.coverage.failed[0].reason == ocr.NOT_RUN


def test_a_turned_image_is_measured_along_its_own_width(engine: FakeEngine) -> None:
    # 400 points along the image's own x axis, drawn turned a quarter: its box is 300 wide
    extract(laid_out_pdf([b"q 0 400 -300 0 400 200 cm /Im1 Do Q"], image=GRAY), "t.pdf")
    assert engine.seen[0][1] == pytest.approx(400 / 72)


def test_a_spacer_under_a_point_wide_leaves_a_scanned_page_complete(engine: FakeEngine) -> None:
    page = b"q 400 0 0 400 100 200 cm /Im1 Do Q\nq 0.75 0 0 0.75 10 10 cm /Im2 Do Q\n"
    a = extract(laid_out_pdf([page], image=[GRAY, GRAY]), "s.pdf")
    assert a.coverage.complete and len(engine.seen) == 1


def test_sizes_under_six_points_compare_at_a_tenth() -> None:
    assert layout.size_class(2.4) != layout.size_class(2.6)
    assert layout.size_class(12.2) == layout.size_class(12.0)
    assert layout.size_class(5.97) == layout.size_class(6.0) == 6.0


def test_a_page_of_only_a_spacer_is_complete(engine: FakeEngine) -> None:
    a = extract(laid_out_pdf([b"q 0.75 0 0 0.75 10 10 cm /Im1 Do Q"], image=GRAY), "s.pdf")
    assert a.coverage.complete and engine.seen == []


def test_an_inline_scan_beside_a_spacer_still_fails_its_page(engine: FakeEngine) -> None:
    inline = b"q 400 0 0 400 100 200 cm BI /W 1 /H 1 /CS /G /BPC 8 ID \x00 EI Q\n"
    spacer = b"q 0.75 0 0 0.75 10 10 cm /Im1 Do Q"
    a = extract(laid_out_pdf([inline + spacer], image=GRAY), "i.pdf")
    assert a.coverage.failed[0].reason == (
        "no text layer: its image is drawn inline, which OCR does not read"
    )


def test_an_inline_scan_in_a_form_beside_a_spacer_still_fails_its_page(engine: FakeEngine) -> None:
    form = b"q 1 0 0 1 0 0 cm BI /W 1 /H 1 /CS /G /BPC 8 ID \x00 EI Q"
    page = b"q 400 0 0 400 100 200 cm /Fm1 Do Q\nq 0.75 0 0 0.75 10 10 cm /Im1 Do Q"
    a = extract(laid_out_pdf([page], image=GRAY, forms=(form,)), "f.pdf")
    assert a.coverage.failed[0].reason == (
        "no text layer: its image is drawn inline, which OCR does not read"
    )


def test_a_slides_large_linked_picture_leaves_it_partial(engine: FakeEngine) -> None:
    from tests.unit.test_document_extraction_office import _P, _rels, _slide, _sp, _zip

    pic = (
        '<p:pic><p:blipFill><a:blip r:link="img"/></p:blipFill>'
        '<p:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="6000000" cy="3000000"/></a:xfrm></p:spPr></p:pic>'
    )
    deck = _zip(
        {
            "ppt/presentation.xml": (
                f'<p:presentation {_P}><p:sldIdLst><p:sldId id="256" r:id="s1"/></p:sldIdLst>'
                '<p:sldSz cx="12192000" cy="6858000"/></p:presentation>'
            ),
            "ppt/_rels/presentation.xml.rels": _rels(("s1", "slide", "slides/slide1.xml")),
            "ppt/slides/slide1.xml": _slide(_sp("Architecture", "title") + pic),
        }
    )
    a = extract(deck, "l.pptx")
    assert [f.reason for f in a.coverage.failed] == [
        "a picture's image is not held in the file, so OCR cannot read it"
    ]
    assert engine.seen == []


def test_the_real_engine_refuses_an_image_past_its_pixels_from_its_header() -> None:
    if ocr.check("eng")[0]:
        pytest.skip("Tesseract is not installed here")
    eng = ocr._load("eng")
    assert eng is not None
    # a header of 10,000 by 10,000 pixels over a few bytes: refused before it is decoded
    with pytest.raises(ValueError, match="past 40,000,000 pixels"):
        eng.read(b"P5\n10000 10000\n255\n" + bytes(16), 1.0)


def test_a_text_page_with_a_large_inline_picture_is_partial(engine: FakeEngine) -> None:
    text = text_at(72, 740, "A caption above a screenshot.")
    shot = b"q 400 0 0 300 100 300 cm BI /W 1 /H 1 /CS /G /BPC 8 ID \x00 EI Q"
    a = extract(laid_out_pdf([text + shot]), "i.pdf")
    assert [f.reason for f in a.coverage.failed] == [
        "OCR did not read all of its pictures:"
        " a picture drawn inline covers a fifth of it, which OCR does not read"
    ]


def _gif(screen: tuple[int, int], frames: list[tuple[int, int]]) -> bytes:
    def u16(v: int) -> bytes:
        return v.to_bytes(2, "little")

    out = b"GIF89a" + u16(screen[0]) + u16(screen[1]) + b"\x00\x00\x00"
    out += b"\x21\xf9\x04\x00\x00\x00\x00\x00"  # a graphic control extension
    for w, h in frames:
        out += b"\x2c" + u16(0) + u16(0) + u16(w) + u16(h) + b"\x00" + b"\x02\x02\x44\x01\x00"
    return out + b"\x3b"


def test_a_gifs_pixels_are_its_screens_and_every_frames() -> None:
    # a GIF never reaches Leptonica's header reader, which decodes it whole
    assert ocr._header_pixels(None, _gif((30_000, 30_000), [])) == 900_000_000
    # a 1 by 1 screen over a frame of 30,000 by 30,000: the frame is what is decoded
    assert ocr._header_pixels(None, _gif((1, 1), [(30_000, 30_000)])) == 900_000_001
    assert ocr._header_pixels(None, _gif((10, 10), [(6, 6)] * 3)) == 208


def test_a_text_layer_over_a_large_inline_scan_is_complete(engine: FakeEngine) -> None:
    lines = b"".join(
        text_at(110, 580 - 18 * k, f"Line {k} of the scanned page text.") for k in range(20)
    )
    inline = b"q 400 0 0 400 100 200 cm BI /W 1 /H 1 /CS /G /BPC 8 ID \x00 EI Q\n"
    a = extract(laid_out_pdf([inline + lines]), "t.pdf")
    assert a.coverage.complete


def test_an_inline_scan_beside_a_readable_logo_is_not_complete(engine: FakeEngine) -> None:
    inline = b"q 400 0 0 400 100 200 cm BI /W 1 /H 1 /CS /G /BPC 8 ID \x00 EI Q\n"
    logo = b"q 60 0 0 40 20 700 cm /Im1 Do Q"
    a = extract(laid_out_pdf([inline + logo], image=GRAY), "l.pdf")
    assert not a.coverage.complete and len(engine.seen) == 1
    assert a.coverage.failed[0].reason == (
        "OCR did not read all of its pictures:"
        " it draws a picture inline beside those OCR read, which OCR does not read"
    )


def test_an_inline_icon_drawn_many_times_takes_no_picture_room(
    engine: FakeEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(layout, "_MAX_DRAWN", 2)
    icon = b"q 4 0 0 4 0 0 cm BI /W 1 /H 1 /CS /G /BPC 8 ID \x00 EI Q"
    icons = b"".join(b"q 1 0 0 1 %d 300 cm /Fm1 Do Q\n" % (20 + 6 * n) for n in range(10))
    shot = b"q 400 0 0 300 100 300 cm /Im1 Do Q\n"
    text = text_at(72, 740, "A page of text, a row of icons and a screenshot.")
    a = extract(laid_out_pdf([text + icons + shot], image=GRAY, forms=(icon,)), "i.pdf")
    assert a.coverage.complete and len(engine.seen) == 1


def test_a_text_page_checks_every_large_inline_picture_not_the_largest_alone(
    engine: FakeEngine,
) -> None:
    lines = b"".join(
        text_at(110, 580 - 18 * k, f"Line {k} of the scanned page text.") for k in range(20)
    )
    scan = b"q 400 0 0 400 100 200 cm BI /W 1 /H 1 /CS /G /BPC 8 ID \x00 EI Q\n"  # filled
    figure = b"q 400 0 0 300 100 0 cm BI /W 1 /H 1 /CS /G /BPC 8 ID \x00 EI Q\n"  # not filled
    a = extract(laid_out_pdf([scan + lines + figure]), "t.pdf")
    assert [f.reason for f in a.coverage.failed] == [
        "OCR did not read all of its pictures:"
        " a picture drawn inline covers a fifth of it, which OCR does not read"
    ]


def test_an_inline_spacer_beside_a_scan_leaves_it_complete(engine: FakeEngine) -> None:
    scan = b"q 400 0 0 400 100 200 cm /Im1 Do Q\n"
    spacer = b"q 0.75 0 0 0.75 10 10 cm BI /W 1 /H 1 /CS /G /BPC 8 ID \x00 EI Q"
    a = extract(laid_out_pdf([scan + spacer], image=GRAY), "s.pdf")
    assert a.coverage.complete


def test_a_text_page_whose_drawing_fails_beside_an_image_is_partial(
    engine: FakeEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*args: object) -> None:
        raise KeyError("/Resources")

    monkeypatch.setattr(layout, "drawing", broken)
    text = text_at(72, 740, "A page of text and a screenshot.")
    a = extract(laid_out_pdf([text + b"q 400 0 0 300 100 300 cm /Im1 Do Q"], image=GRAY), "d.pdf")
    assert [f.reason for f in a.coverage.failed] == [
        "OCR did not read all of its pictures:"
        " its drawing could not be read, so a picture on it may be unread"
    ]


def _dangling(pdf: bytes) -> bytes:
    """`pdf` with its /Im1 naming an object the file does not hold."""
    return re.sub(rb"/Im1 \d+ 0 R", b"/Im1 99 0 R", pdf, count=1)


def test_a_scan_whose_image_is_dangling_fails_its_page_not_the_file(engine: FakeEngine) -> None:
    pages = [b"q 400 0 0 400 100 200 cm /Im1 Do Q", text_at(72, 740, "A second page of text.")]
    a = extract(_dangling(laid_out_pdf(pages, image=GRAY)), "d.pdf")
    assert [(f.unit, f.reason) for f in a.coverage.failed] == [
        (
            "page 1",
            "no text layer, and OCR read none of its images:"
            " not every picture it draws could be collected, so one may be unread",
        )
    ]
    assert [b.text for b in a.blocks] == ["A second page of text."]


def test_a_text_page_whose_image_is_dangling_is_partial(engine: FakeEngine) -> None:
    text = text_at(72, 740, "A page of text and a screenshot.")
    a = extract(
        _dangling(laid_out_pdf([text + b"q 400 0 0 300 100 300 cm /Im1 Do Q"], image=GRAY)), "t.pdf"
    )
    assert [b.text for b in a.blocks] == ["A page of text and a screenshot."]
    assert [f.reason for f in a.coverage.failed] == [
        "OCR did not read all of its pictures:"
        " not every picture it draws could be collected, so one may be unread"
    ]


@pytest.mark.parametrize("texted", [True, False])
def test_a_picture_nested_past_the_form_depth_is_never_complete(
    engine: FakeEngine, texted: bool
) -> None:
    # page -> Ch1 -> ... -> Ch5 draws Im1, each form holding the next in its own resources
    chain = tuple(b"/Ch%d Do" % n for n in range(2, 6)) + (b"q 400 0 0 300 100 300 cm /Im1 Do Q",)
    text = text_at(72, 740, "A page of text over nested forms.") if texted else b""
    a = extract(laid_out_pdf([text + b"/Ch1 Do"], chain=chain), "n.pdf")
    reason = "not every picture it draws could be collected, so one may be unread"
    assert engine.seen == [] and [f.reason for f in a.coverage.failed] == [
        f"OCR did not read all of its pictures: {reason}"
        if texted
        else f"no text layer, and OCR read none of its images: {reason}"
    ]


def test_an_image_in_the_deepest_form_walked_is_read(engine: FakeEngine) -> None:
    # page -> Ch1 -> Ch2 -> Ch3 -> Ch4 draws Im1: four forms deep, as deep as the walk goes
    chain = tuple(b"/Ch%d Do" % n for n in range(2, 5)) + (b"q 400 0 0 400 100 200 cm /Im1 Do Q",)
    a = extract(laid_out_pdf([b"/Ch1 Do"], chain=chain), "d.pdf")
    assert a.coverage.complete and len(engine.seen) == 1


def test_shared_forms_past_the_depth_that_nothing_draws_leave_the_page_complete(
    engine: FakeEngine,
) -> None:
    # the page holds a chain of forms five deep (an image at its end) and draws none of them
    chain = tuple(b"/Ch%d Do" % n for n in range(2, 6)) + (b"q 400 0 0 300 100 300 cm /Im1 Do Q",)
    a = extract(laid_out_pdf([b"0 0 m 100 100 l S"], chain=chain), "v.pdf")
    assert a.coverage.complete and engine.seen == []


def test_a_text_page_whose_layout_fails_beside_an_image_is_partial(
    engine: FakeEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fails(*args: object) -> None:
        raise RuntimeError("layout")

    monkeypatch.setattr(pdffile, "_read_layout", fails)
    text = text_at(72, 740, "A page of text and a screenshot.")
    a = extract(laid_out_pdf([text + b"q 400 0 0 300 100 300 cm /Im1 Do Q"], image=GRAY), "l.pdf")
    assert [b.text for b in a.blocks] == ["A page of text and a screenshot."]
    assert [f.reason for f in a.coverage.failed] == [
        "its layout could not be read, so a picture on it may be unread: RuntimeError"
    ]


def test_a_page_whose_image_check_fails_too_fails_alone(
    engine: FakeEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fails(*args: object, **kwargs: object) -> None:
        raise RuntimeError("broken")

    monkeypatch.setattr(pdffile, "_read_layout", fails)
    monkeypatch.setattr(pdffile, "_has_image", fails)
    pages = [b"q 400 0 0 400 100 200 cm /Im1 Do Q", text_at(72, 740, "A second page of text.")]
    a = extract(laid_out_pdf(pages, image=GRAY), "f.pdf")
    assert [(f.unit, f.reason) for f in a.coverage.failed] == [
        ("page 1", "no text layer, and its layout could not be read: RuntimeError"),
        ("page 2", "its layout could not be read, so a picture on it may be unread: RuntimeError"),
    ]
    assert [b.text for b in a.blocks] == ["A second page of text."]
