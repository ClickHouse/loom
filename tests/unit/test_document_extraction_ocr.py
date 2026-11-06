"""OCR in the reference extractor: which pictures are read, how their words
land on the page, and what a page or slide says when OCR cannot run. The
engine is a fake: the tests need no Tesseract."""

from __future__ import annotations

import zlib
from dataclasses import dataclass, field
from io import BytesIO

import pytest
from pypdf import PdfReader

from loom_extractor import extract, ocr, pdffile
from loom_extractor import pdflayout as layout
from tests.fixtures.office_files import laid_out_pdf, text_at

GRAY = (
    b"/Width 4 /Height 2 /ColorSpace /DeviceGray /BitsPerComponent 8 /Length 8 >>\nstream\n"
    + bytes(8)
)


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
    _, pictures = layout.drawing(page, reader, lambda x, y: (x, y))
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
        "OCR did not read all of its pictures: it draws more than 2 pictures, which OCR reads"
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
