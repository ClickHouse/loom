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
    page = b"q 400 0 0 400 100 200 cm /Im1 Do Q"
    a = extract(laid_out_pdf([page, page], image=GRAY), "s.pdf")
    assert a.attrs["ocr_pages"] == [1]
    assert [(f.unit, f.reason) for f in a.coverage.failed] == [
        (
            "page 2",
            "no text layer, and OCR read none of its images: past the 1 images OCR reads in one file",
        )
    ]


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


def _deck(picture_cx: int) -> bytes:
    from tests.unit.test_document_extraction_office import _P, _rels, _slide, _sp, _zip

    pic = (
        '<p:pic><p:blipFill><a:blip r:embed="img"/></p:blipFill>'
        f'<p:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="{picture_cx}" cy="3000000"/></a:xfrm></p:spPr></p:pic>'
    )
    return _zip(
        {
            "ppt/presentation.xml": (
                f'<p:presentation {_P}><p:sldIdLst><p:sldId id="256" r:id="s1"/></p:sldIdLst>'
                '<p:sldSz cx="12192000" cy="6858000"/></p:presentation>'
            ),
            "ppt/_rels/presentation.xml.rels": _rels(("s1", "slide", "slides/slide1.xml")),
            "ppt/slides/slide1.xml": _slide(_sp("Whiteboard", "title") + pic),
            "ppt/slides/_rels/slide1.xml.rels": _rels(("img", "image", "../media/image1.png")),
            "ppt/media/image1.png": b"\x89PNG fake",
        }
    )


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
    ocr.configure(False, "eng", 30)
    assert ocr.engine() is None and loads == []
    ocr.configure(True, "eng+deu", 5)
    assert ocr.engine() is None and ocr.engine() is None and loads == ["eng+deu"]
    assert ocr.max_images() == 5
    ocr.configure(True, "eng", 30)
