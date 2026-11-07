"""The reference extractor's PDF, DOCX and PPTX parsers, over files built here
byte by byte, and the server reading what they produce."""

from __future__ import annotations

import io
import json
import zipfile
from typing import Any

import pytest
from pypdf import PdfWriter

from loom_extractor import ExtractionError, Options, extract, ocr, ooxml
from loom_extractor.artifact import Artifact
from server.knowledge.domain.extract import InvalidArtifact, parse_artifact, structural
from tests.fixtures.office_files import (
    FORM_IMAGE,
    FORM_INLINE_IMAGE,
    IMAGE,
    INLINE_IMAGE,
    UNDRAWN_IMAGE,
    pdf,
)


@pytest.fixture(autouse=True)
def _no_ocr(monkeypatch: pytest.MonkeyPatch) -> None:
    """These files' pictures need OCR whether or not this machine has Tesseract."""
    monkeypatch.setattr(ocr, "engine", lambda: None)


_W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
_P = (
    'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
    'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
)
_MC = 'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006"'
_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def _zip(parts: dict[str, str | bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, xml in parts.items():
            z.writestr(name, xml)
    return buf.getvalue()


def _rels(*rels: tuple[str, str, str]) -> str:
    body = "".join(
        f'<Relationship Id="{i}" Type="{_REL}/{t}" Target="{target}"/>' for i, t, target in rels
    )
    return f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">{body}</Relationships>'


def _wp(text: str, style: str = "", extra: str = "") -> str:
    ppr = f'<w:pPr><w:pStyle w:val="{style}"/>{extra}</w:pPr>' if style or extra else ""
    return f"<w:p>{ppr}<w:r><w:t>{text}</w:t></w:r></w:p>"


def _docx(body: str, *, footnotes: str = "") -> bytes:
    styles = (
        f"<w:styles {_W}>"
        '<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/></w:style>'
        '<w:style w:type="paragraph" w:styleId="Heading2"><w:name w:val="heading 2"/></w:style>'
        '<w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/></w:style>'
        '<w:style w:type="paragraph" w:styleId="Rubric"><w:name w:val="Rubric"/>'
        '<w:basedOn w:val="Chapter"/></w:style>'
        '<w:style w:type="paragraph" w:styleId="Chapter"><w:name w:val="Chapter"/>'
        '<w:pPr><w:outlineLvl w:val="0"/></w:pPr></w:style>'
        "</w:styles>"
    )
    parts = {
        "word/document.xml": f"<w:document {_W} {_MC}><w:body>{body}</w:body></w:document>",
        "word/styles.xml": styles,
    }
    if footnotes:
        parts["word/footnotes.xml"] = f"<w:footnotes {_W}>{footnotes}</w:footnotes>"
    return _zip(parts)


def test_a_docx_title_paragraph_holds_the_headings_under_it() -> None:
    body = (
        _wp("Storage design", "Title")
        + _wp("Goals", "Heading1")
        + _wp("Limits", "Heading2")
        + _wp("Text")
    )
    a = extract(_docx(body), "t.docx")
    assert [(b.depth, b.text) for b in a.blocks if b.type == "heading"] == [
        (1, "Storage design"),
        (2, "Goals"),
        (3, "Limits"),
    ]
    assert a.blocks[-1].section_path == ("Storage design", "Goals", "Limits")
    # an outline level of its own goes under the title too
    outlined = extract(
        _docx(_wp("Plan", "Title") + _wp("X", extra='<w:outlineLvl w:val="0"/>')), "o.docx"
    )
    assert [b.depth for b in outlined.blocks] == [1, 2]
    # a title paragraph with an outline level of its own is still the top
    level = '<w:outlineLvl w:val="1"/>'
    leveled = extract(_docx(_wp("Plan", "Title", level) + _wp("Goals", "Heading1")), "l.docx")
    assert [b.depth for b in leveled.blocks] == [1, 2]
    # a heading before the title keeps its level; the ones after go under the title
    before = extract(
        _docx(_wp("Preface", "Heading1") + _wp("Plan", "Title") + _wp("Goals", "Heading1")),
        "p.docx",
    )
    assert [b.depth for b in before.blocks] == [1, 1, 2]
    # a second title is the top again, and the headings after it sit under it
    parts = (
        _wp("Part one", "Title")
        + _wp("Goals", "Heading1")
        + _wp("Part two", "Title")
        + _wp("Scope", "Heading1")
    )
    two = extract(_docx(parts + _wp("Text")), "w.docx")
    assert [b.depth for b in two.blocks if b.type == "heading"] == [1, 2, 1, 2]
    assert two.blocks[-1].section_path == ("Part two", "Scope")
    # without a title heading (none, one with no text, one set to body text) the
    # headings keep their own levels
    body_text = '<w:outlineLvl w:val="9"/>'
    for prefix in ("", _wp("", "Title"), _wp("Plan", "Title", body_text)):
        untitled = extract(
            _docx(prefix + _wp("Goals", "Heading1") + _wp("Limits", "Heading2")), "u.docx"
        )
        assert [b.depth for b in untitled.blocks if b.type == "heading"] == [1, 2]


def _texts(a: Artifact) -> list[tuple[str, str, tuple[str, ...]]]:
    return [(b.type, b.text, b.section_path) for b in a.blocks]


def test_docx_headings_lists_tables_and_notes_read_in_document_order() -> None:
    tracked = (
        "<w:p><w:r><w:t>Kept </w:t></w:r><w:ins><w:r><w:t>inserted</w:t></w:r></w:ins>"
        "<w:del><w:r><w:delText>co</w:delText><w:noBreakHyphen/><w:tab/>"
        "<w:delText>op</w:delText></w:r></w:del>"
        # A tracked move keeps its text in ordinary runs at both places.
        "<w:moveFrom><w:r><w:t> moved</w:t></w:r></w:moveFrom>"
        "<w:moveTo><w:r><w:t> moved</w:t></w:r></w:moveTo></w:p>"
    )
    boxed = (
        "<w:p><w:r><mc:AlternateContent><mc:Choice><w:t>Box text</w:t></mc:Choice>"
        "<mc:Fallback><w:t>Box text</w:t></mc:Fallback></mc:AlternateContent></w:r></w:p>"
    )
    gone = "<w:del><w:r><w:delText>retired</w:delText></w:r></w:del>"
    deleted_cell = '<w:tc><w:tcPr><w:cellDel w:id="1"/></w:tcPr>' + _wp("Was") + "</w:tc>"
    deleted_row = f'<w:tr><w:trPr><w:del w:id="2"/></w:trPr><w:tc><w:p>{gone}</w:p></w:tc></w:tr>'
    table = (
        "<w:tbl><w:tr><w:tc>"
        + _wp("System")
        + "</w:tc><w:tc>"
        + _wp("Owner")
        + "</w:tc>"
        + deleted_cell
        + "</w:tr><w:tr><w:tc>"
        + _wp("billing")
        + "</w:tc><w:tc>"
        + _wp("finance")
        + "</w:tc></w:tr>"
        + deleted_row
        + "</w:tbl>"
    )
    body = (
        _wp("Data policy", "Heading1")
        + _wp("Customer data stays in region.")
        + _wp("Retention", "Heading2")
        + _wp("Export on request", extra="<w:numPr/>")
        + _wp("Delete on request", "ListBullet")
        + tracked
        + "<w:sdt><w:sdtContent>"
        + _wp("Inside a control")
        + "</w:sdtContent></w:sdt>"
        + boxed
        + _wp("Appendix", "Rubric")  # an outline level through its basedOn chain
        + table
        + _wp("   ")
    )
    notes = (
        '<w:footnote w:type="separator" w:id="-1"><w:p><w:r><w:t>---</w:t></w:r></w:p></w:footnote>'
        '<w:footnote w:id="1">' + _wp("See the audit log.") + "</w:footnote>"
    )
    a = extract(_docx(body, footnotes=notes), "policy.docx")
    policy, retention, appendix = ("Data policy",), ("Data policy", "Retention"), ("Appendix",)
    assert _texts(a) == [
        ("heading", "Data policy", policy),
        ("paragraph", "Customer data stays in region.", policy),
        ("heading", "Retention", retention),
        ("list", "Export on request\nDelete on request", retention),
        ("paragraph", "Kept inserted moved", retention),
        ("paragraph", "Inside a control", retention),
        ("paragraph", "Box text", retention),
        ("heading", "Appendix", appendix),
        ("paragraph", "System | Owner\nbilling | finance", appendix),
        ("paragraph", "See the audit log.", appendix),
    ]
    assert a.blocks[8].attrs == {"table": {"rows": 2, "columns": 2}}
    assert a.blocks[9].attrs == {"note": "footnote"}
    assert [b.depth for b in a.blocks if b.type == "heading"] == [1, 2, 1]
    assert a.coverage.to_dict()["complete"] and a.coverage.unit == "paragraphs"
    assert a.coverage.total == 11  # ten body units with text and one footnote


def _slide(shapes: str, show: str = "") -> str:
    return f"<p:sld {_P}{show}><p:cSld><p:spTree>{shapes}</p:spTree></p:cSld></p:sld>"


def _sp(text: str, ph: str | None = None) -> str:
    nv = f'<p:nvPr><p:ph type="{ph}"/></p:nvPr>' if ph else "<p:nvPr/>"
    paras = "".join(f"<a:p><a:r><a:t>{line}</a:t></a:r></a:p>" for line in text.split("\n"))
    return f"<p:sp><p:nvSpPr>{nv}</p:nvSpPr><p:txBody>{paras}</p:txBody></p:sp>"


def _pptx() -> bytes:
    table = (
        "<p:graphicFrame><a:graphic><a:graphicData><a:tbl>"
        "<a:tr><a:tc><a:txBody><a:p><a:r><a:t>Region</a:t></a:r></a:p></a:txBody></a:tc>"
        "<a:tc><a:txBody><a:p><a:r><a:t>Growth</a:t></a:r></a:p></a:txBody></a:tc></a:tr>"
        "</a:tbl></a:graphicData></a:graphic></p:graphicFrame>"
    )
    notes = f"<p:notes {_P}><p:cSld><p:spTree>{_sp('1', 'sldNum')}{_sp('Mention EMEA.', 'body')}</p:spTree></p:cSld></p:notes>"
    parts = {
        "ppt/presentation.xml": (
            f"<p:presentation {_P}><p:sldIdLst>"
            '<p:sldId id="256" r:id="rB"/><p:sldId id="257" r:id="rA"/><p:sldId id="258" r:id="rC"/>'
            '<p:sldId id="259" r:id="rD"/></p:sldIdLst></p:presentation>'
        ),
        "ppt/_rels/presentation.xml.rels": _rels(
            ("rA", "slide", "slides/slide1.xml"),
            ("rB", "slide", "slides/slide2.xml"),
            ("rC", "slide", "slides/slide3.xml"),
            ("rD", "slide", "slides/slide4.xml"),
        ),
        # The deck's order is the id list's, not the part names'.
        "ppt/slides/slide2.xml": _slide(
            _sp("Revenue grew\nCosts flat")
            + _sp("Quarterly review", "title")
            + f"<p:grpSp>{_sp('Grouped note')}</p:grpSp>"
            + table
        ),
        "ppt/slides/_rels/slide2.xml.rels": _rels(
            ("n1", "notesSlide", "../notesSlides/notesSlide1.xml")
        ),
        "ppt/notesSlides/notesSlide1.xml": notes,
        "ppt/slides/slide1.xml": _slide(_sp("Untitled body"), show=' show="0"'),
        "ppt/slides/slide3.xml": _slide("<p:pic/>"),
        "ppt/slides/slide4.xml": _slide(""),
    }
    return _zip(parts)


def test_a_pptx_slide_of_text_with_a_picture_or_a_chart_names_it_as_not_read() -> None:
    chart = "<p:graphicFrame><a:graphic><a:graphicData/></a:graphic></p:graphicFrame>"
    notes = f"<p:notes {_P}><p:cSld><p:spTree>{_sp('Explain the chart.', 'body')}</p:spTree></p:cSld></p:notes>"
    parts = {
        "ppt/presentation.xml": (
            f"<p:presentation {_P}><p:sldIdLst>"
            '<p:sldId id="256" r:id="r1"/><p:sldId id="257" r:id="r2"/>'
            '<p:sldId id="258" r:id="r3"/><p:sldId id="259" r:id="r4"/>'
            '<p:sldId id="260" r:id="r5"/></p:sldIdLst></p:presentation>'
        ),
        "ppt/_rels/presentation.xml.rels": _rels(
            ("r1", "slide", "slides/slide1.xml"),
            ("r2", "slide", "slides/slide2.xml"),
            ("r3", "slide", "slides/slide3.xml"),
            ("r4", "slide", "slides/slide4.xml"),
            ("r5", "slide", "slides/slide5.xml"),
        ),
        "ppt/slides/slide1.xml": _slide(_sp("Plain text")),
        "ppt/slides/slide2.xml": _slide(_sp("Latency", "title") + "<p:pic/>"),
        "ppt/slides/slide3.xml": _slide(_sp("Growth", "title") + chart),
        # A picture and notes alone: the notes are the slide's text.
        "ppt/slides/slide4.xml": _slide("<p:pic/>"),
        # A shape filled with a picture holds text of its own; its picture is not read.
        "ppt/slides/slide5.xml": _slide(
            "<p:sp><p:nvSpPr><p:nvPr/></p:nvSpPr><p:spPr><a:blipFill/></p:spPr>"
            "<p:txBody><a:p><a:r><a:t>Logo text</a:t></a:r></a:p></p:txBody></p:sp>"
        ),
        "ppt/slides/_rels/slide4.xml.rels": _rels(("n1", "notesSlide", "../notesSlides/n4.xml")),
        "ppt/notesSlides/n4.xml": notes,
    }
    a = extract(_zip(parts), "deck.pptx")
    assert [b.text for b in a.blocks] == [
        "Plain text",
        "Latency",
        "Growth",
        "Explain the chart.",
        "Logo text",
    ]
    assert a.coverage.to_dict()["complete"]
    assert a.attrs == {"slides": 5, "images_unread": [2, 3, 4, 5]}
    assert a.warnings == (
        "pictures, charts, diagrams or embedded objects on 4 slides of text are not read: text"
        " inside them is not extracted (attrs.images_unread)",
    )


_A = 'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'
_DRAWINGML = "http://schemas.openxmlformats.org/drawingml/2006"
_WPS = "http://schemas.microsoft.com/office/word/2010/wordprocessing"


def _drawing(uri: str, inner: str) -> str:
    """A paragraph of one drawing whose graphic is `uri`'s, holding `inner`."""
    return (
        f'<w:p><w:r><w:drawing><a:graphic {_A}><a:graphicData uri="{uri}">{inner}'
        "</a:graphicData></a:graphic></w:drawing></w:r></w:p>"
    )


_TEXT_BOX = "<w:txbxContent><w:p><w:r><w:t>Sidebar</w:t></w:r></w:p></w:txbxContent>"
_BLIP = '<a:blip r:embed="rId9" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"/>'


def _picture() -> str:
    return _drawing(f"{_DRAWINGML}/picture", _BLIP)


def _run(paragraph: str) -> str:
    """The run a paragraph of one drawing holds."""
    return paragraph.removeprefix("<w:p><w:r>").removesuffix("</w:r></w:p>")


# A 3D model as Word writes it: a drawing in alternate content, its picture in the fallback.
_MODEL_3D = (
    '<w:p><w:r><mc:AlternateContent xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006">'
    '<mc:Choice Requires="am3d">'
    + _run(_drawing("http://schemas.microsoft.com/office/drawing/2017/model3d", ""))
    + f"</mc:Choice><mc:Fallback>{_run(_picture())}</mc:Fallback></mc:AlternateContent></w:r></w:p>"
)


# WordArt as a document saved for an older Word writes it: its text a VML path's.
_WORDART = (
    '<w:p><w:r><w:pict><v:shape xmlns:v="urn:schemas-microsoft-com:vml">'
    '<v:textpath string="Grand opening"/></v:shape></w:pict></w:r></w:p>'
)


# A text box as Word writes it: a shape in alternate content, its VML copy in the fallback.
_TEXT_BOX_AS_WORD_WRITES_IT = (
    '<w:p><w:r><mc:AlternateContent xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006">'
    f'<mc:Choice Requires="wps">{_run(_drawing(f"{_WPS}Shape", _TEXT_BOX))}</mc:Choice>'
    '<mc:Fallback><w:pict><v:shape xmlns:v="urn:schemas-microsoft-com:vml">'
    f"<v:textbox>{_TEXT_BOX}</v:textbox></v:shape></w:pict></mc:Fallback>"
    "</mc:AlternateContent></w:r></w:p>"
)


# WordArt as Word writes it now: its text a shape's, read, and a VML copy drawing it
# in the fallback.
_WARPED_WORDART = _TEXT_BOX_AS_WORD_WRITES_IT.replace(
    f"<v:textbox>{_TEXT_BOX}</v:textbox>", '<v:textpath string="Sidebar"/>'
)
# The same box filled with a pattern: its VML copy's fill names the pattern's tile.
_PATTERN_FILLED_BOX = _TEXT_BOX_AS_WORD_WRITES_IT.replace(
    "<v:textbox>",
    '<v:fill r:id="rId7" type="pattern" '
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"/><v:textbox>',
)


@pytest.mark.parametrize(
    "drawing",
    [
        _drawing(
            f"{_DRAWINGML}/picture", f'<pic:pic xmlns:pic="{_DRAWINGML}/picture">{_BLIP}</pic:pic>'
        ),
        _drawing(f"{_DRAWINGML}/chart", '<c:chart xmlns:c="x"/>'),
        _drawing(f"{_DRAWINGML}/diagram", ""),
        _drawing("http://schemas.microsoft.com/office/drawing/2014/chartex", ""),
        # A group of a picture and a captioned text box: the picture is not read.
        _drawing(f"{_WPS}Group", f'<wpg:wgp xmlns:wpg="y">{_BLIP}{_TEXT_BOX}</wpg:wgp>'),
        # A shape filled with a picture.
        _drawing(f"{_WPS}Shape", f"<a:blipFill>{_BLIP}</a:blipFill>{_TEXT_BOX}"),
        '<w:p><w:r><w:pict><v:shape xmlns:v="urn:schemas-microsoft-com:vml">'
        '<v:imagedata r:id="rId9" xmlns:r="z"/></v:shape></w:pict></w:r></w:p>',
        # A VML shape filled with a picture, as a document saved for an older Word writes it.
        '<w:p><w:r><w:pict><v:shape xmlns:v="urn:schemas-microsoft-com:vml">'
        '<v:fill r:id="rId5" type="frame" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"/>'
        "</v:shape></w:pict></w:r></w:p>",
        # The same fill naming its image by its Office relationship id.
        '<w:p><w:r><w:pict><v:shape xmlns:v="urn:schemas-microsoft-com:vml">'
        '<v:fill o:relid="rId5" type="frame" xmlns:o="urn:schemas-microsoft-com:office:office"/>'
        "</v:shape></w:pict></w:r></w:p>",
        _MODEL_3D,  # a drawing the reader does not know, its picture in the fallback
        _WORDART,
        # An embedded spreadsheet or diagram: an OLE object and its preview image.
        '<w:p><w:r><w:object><v:shape xmlns:v="urn:schemas-microsoft-com:vml">'
        '<v:imagedata r:id="rId5" xmlns:r="z"/></v:shape></w:object></w:r></w:p>',
    ],
    ids=[
        "picture",
        "chart",
        "diagram",
        "chartex",
        "group",
        "picture fill",
        "vml image",
        "vml picture fill",
        "vml picture fill by its office id",
        "3d model",
        "wordart",
        "embedded object",
    ],
)
def test_a_docx_of_text_with_a_picture_chart_diagram_or_object_warns_it_is_not_read(
    drawing: str,
) -> None:
    a = extract(_docx(_wp("Body") + drawing), "a.docx")
    assert a.blocks[0].text == "Body"
    assert a.coverage.to_dict()["complete"]
    assert a.warnings == (
        "its pictures, charts, diagrams or embedded objects are not read: text inside them is"
        " not extracted",
    )


@pytest.mark.parametrize(
    "drawing",
    [
        "",
        _drawing(f"{_WPS}Shape", _TEXT_BOX),  # a text box: its text is read
        # A horizontal line: nothing to read.
        '<w:p><w:r><w:pict><v:rect xmlns:v="urn:schemas-microsoft-com:vml" o:hr="t" xmlns:o="o"/>'
        "</w:pict></w:r></w:p>",
        _drawing(f"{_WPS}Shape", ""),
        # A picture in a text box that a tracked deletion removed is not the document's.
        _drawing(
            f"{_WPS}Shape",
            "<w:txbxContent><w:p><w:r><w:t>Sidebar</w:t></w:r><w:del><w:r>"
            + _run(_picture())
            + "</w:r></w:del></w:p></w:txbxContent>",
        ),
        _TEXT_BOX_AS_WORD_WRITES_IT,
        _PATTERN_FILLED_BOX,
        _WARPED_WORDART,
    ],
    ids=[
        "none",
        "text box",
        "rule",
        "shape",
        "deleted picture",
        "text box in alternate content",
        "pattern fill",
        "wordart with its text read",
    ],
)
def test_a_docx_of_text_with_no_picture_warns_of_nothing(drawing: str) -> None:
    assert extract(_docx(_wp("Body") + drawing), "a.docx").warnings == ()


def test_a_docx_picture_in_a_table_or_note_warns_and_a_deleted_one_does_not() -> None:
    def table(trpr: str) -> str:
        return (
            f"<w:tbl><w:tr><w:tc>{_wp('Cell')}</w:tc></w:tr>"
            f"<w:tr>{trpr}<w:tc>{_picture()}</w:tc></w:tr></w:tbl>"
        )

    assert extract(_docx(table("")), "a.docx").warnings
    # A deleted row or cell is not the document's, nor what it draws; nor a deleted run.
    assert extract(_docx(table("<w:trPr><w:del/></w:trPr>")), "b.docx").warnings == ()
    cell = f"<w:tbl><w:tr><w:tc>{_wp('Cell')}</w:tc><w:tc><w:tcPr><w:cellDel/></w:tcPr>{_picture()}</w:tc></w:tr></w:tbl>"
    assert extract(_docx(cell), "e.docx").warnings == ()
    run = f"<w:r>{_run(_picture())}</w:r>"
    deleted = f"<w:p><w:r><w:t>Body</w:t></w:r><w:del>{run}</w:del></w:p>"
    assert extract(_docx(deleted), "f.docx").warnings == ()
    note = f'<w:footnote w:id="1">{_picture()}{_wp("A note.")}</w:footnote>'
    assert extract(_docx(_wp("Body"), footnotes=note), "c.docx").warnings
    obj = "<w:p><w:r><w:object/></w:r><w:r><w:t>See it.</w:t></w:r></w:p>"
    note = f'<w:footnote w:id="1">{obj}</w:footnote>'
    assert extract(_docx(_wp("Body"), footnotes=note), "d.docx").warnings


@pytest.mark.parametrize(
    "drawing",
    [
        '<w:p><w:r><w:pict><v:rect xmlns:v="urn:schemas-microsoft-com:vml" o:hr="t" xmlns:o="o"/>'
        "</w:pict></w:r></w:p>",
        _drawing(f"{_WPS}Shape", ""),
        _TEXT_BOX_AS_WORD_WRITES_IT.replace(_TEXT_BOX, ""),
        _PATTERN_FILLED_BOX.replace(_TEXT_BOX, ""),
    ],
    ids=["line", "shape", "shape in alternate content", "pattern-filled shape"],
)
def test_a_docx_of_a_line_or_a_shape_and_no_text_reads_as_empty(drawing: str) -> None:
    a = extract(_docx(drawing), "a.docx")
    # A line or a shape holds nothing to read: no failed unit, nothing lost.
    assert a.blocks == () and a.coverage.failed == () and a.warnings == ()


def test_a_docx_of_an_embedded_object_and_no_text_is_a_failed_unit() -> None:
    obj = (
        '<w:p><w:r><w:object><v:shape xmlns:v="urn:schemas-microsoft-com:vml">'
        '<v:imagedata r:id="rId5" xmlns:r="z"/></v:shape></w:object></w:r></w:p>'
    )
    a = extract(_docx(obj), "a.docx")
    assert not a.coverage.to_dict()["complete"]
    assert [f.unit for f in a.coverage.failed] == ["document"]


def test_pptx_slides_read_in_deck_order_with_titles_tables_notes_and_hidden_slides() -> None:
    a = extract(_pptx(), "deck.pptx")
    title = ("Quarterly review",)
    assert _texts(a) == [
        ("heading", "Quarterly review", title),
        ("paragraph", "Revenue grew\nCosts flat", title),
        ("paragraph", "Grouped note", title),
        ("paragraph", "Region | Growth", title),
        ("paragraph", "Mention EMEA.", title),
        ("paragraph", "Untitled body", ()),  # no title: under no heading
    ]
    assert [b.locator.page_start for b in a.blocks] == [1, 1, 1, 1, 1, 2]
    assert a.blocks[3].attrs == {"table": {"rows": 1, "columns": 2}}
    assert a.blocks[4].attrs == {"notes": True}
    assert a.blocks[5].attrs == {"hidden": True}
    # A slide of pictures alone is a failed unit; an empty slide is not.
    assert a.coverage.to_dict() == {
        "unit": "slides",
        "total": 4,
        "extracted": 3,
        "failed": [
            {
                "unit": "slide 3",
                "reason": "no text: its pictures, charts, diagrams or embedded objects are not read",
            }
        ],
        "lost": [],
        "complete": False,
    }


def test_pdf_pages_are_cited_by_page_and_the_outline_gives_their_sections() -> None:
    data = pdf(
        ["Intro\nFirst line\nDetails\nDetail text", "Scope\nScope text", None],
        [("Intro", 0, [("Details", 0)]), ("Scope", 1, [])],
    )
    a = extract(data, "report.pdf")
    assert _texts(a) == [
        ("heading", "Intro", ("Intro",)),
        ("paragraph", "First line", ("Intro",)),
        ("heading", "Details", ("Intro", "Details")),
        ("paragraph", "Detail text", ("Intro", "Details")),
        ("heading", "Scope", ("Scope",)),
        ("paragraph", "Scope text", ("Scope",)),
    ]
    assert [b.locator.to_dict() for b in a.blocks][3:5] == [
        {"page_start": 1, "page_end": 1},
        {"page_start": 2, "page_end": 2},
    ]
    assert a.attrs == {"pages": 3}
    assert a.parser_version.startswith("4+pypdf")
    # The image-only page needs OCR: the extraction says so and is not complete.
    assert (a.coverage.unit, a.coverage.total, a.coverage.extracted) == ("pages", 3, 2)
    assert a.coverage.failed[0].unit == "page 3" and "OCR" in a.coverage.failed[0].reason


@pytest.mark.parametrize("scan", [FORM_IMAGE, INLINE_IMAGE, FORM_INLINE_IMAGE])
def test_a_pdf_page_drawing_its_image_through_a_form_or_inline_needs_ocr_too(scan: str) -> None:
    a = extract(pdf(["Text", scan], []), "scan.pdf")
    assert [f.unit for f in a.coverage.failed] == ["page 2"]


@pytest.mark.parametrize("kind", [IMAGE, FORM_IMAGE, INLINE_IMAGE, FORM_INLINE_IMAGE])
def test_a_pdf_page_of_text_that_draws_an_image_names_it_as_not_read(kind: str) -> None:
    a = extract(pdf(["Text only", ("See the figure", kind)], []), "figure.pdf")
    assert [b.text for b in a.blocks] == ["Text only", "See the figure"]
    # Its text is read, so the page is no failed unit; the image's text is named as not read.
    assert a.coverage.to_dict()["complete"]
    assert a.attrs["images_unread"] == [2]
    assert a.warnings == (
        "images on 1 page of text are not read: text drawn only inside a small image is not"
        " extracted (attrs.images_unread)",
    )


def test_a_pdf_page_whose_resources_hold_an_image_it_never_draws_is_not_listed() -> None:
    a = extract(pdf(["Text only", ("Shared resources", UNDRAWN_IMAGE)], []), "a.pdf")
    # The layout read the whole drawing and no image is drawn.
    assert "images_unread" not in a.attrs and a.warnings == ()


def test_a_pdf_heading_whose_title_is_on_no_line_opens_its_page() -> None:
    a = extract(pdf(["Body only"], [("Overview", 0, [])]), "a.pdf")
    assert _texts(a) == [
        ("heading", "Overview", ("Overview",)),
        ("paragraph", "Body only", ("Overview",)),
    ]


def test_a_pdf_encrypted_with_an_owner_password_alone_is_read() -> None:
    w = PdfWriter()
    w.append(io.BytesIO(pdf(["Readable text"], [])))
    w.encrypt(user_password="", owner_password="owner", algorithm="AES-256")
    buf = io.BytesIO()
    w.write(buf)
    assert [b.text for b in extract(buf.getvalue(), "a.pdf").blocks] == ["Readable text"]


def test_an_encrypted_pdf_is_unsupported_and_a_broken_one_corrupt() -> None:
    w = PdfWriter()
    w.add_blank_page(100, 100)
    w.encrypt(user_password="secret", algorithm="RC4-128")
    buf = io.BytesIO()
    w.write(buf)
    with pytest.raises(ExtractionError) as e:
        extract(buf.getvalue(), "a.pdf")
    assert e.value.code == "unsupported_format" and "password" in str(e.value)
    with pytest.raises(ExtractionError) as e:
        extract(b"%PDF-1.7\n" + b"\x00" * 64, "a.pdf")
    assert e.value.code == "corrupt"


def test_a_binary_format_takes_no_encoding() -> None:
    with pytest.raises(ExtractionError) as e:
        extract(_docx(_wp("x")), "a.docx", Options(encoding="latin-1"))
    assert e.value.code == "invalid_option"


@pytest.mark.parametrize(
    "xml",
    [
        f'<!DOCTYPE w:document [<!ENTITY e "boom">]><w:document {_W}><w:body>&e;</w:body></w:document>',
        f"<w:document {_W}><w:body>",
    ],
)
def test_a_part_with_a_doctype_or_that_is_not_xml_is_corrupt(xml: str) -> None:
    with pytest.raises(ExtractionError) as e:
        extract(_zip({"word/document.xml": xml}), "a.docx")
    assert e.value.code == "corrupt"


def test_a_doctype_in_a_utf16_part_is_refused_too() -> None:
    xml = f'<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE x [<!ENTITY e "boom">]><w:document {_W}/>'
    with pytest.raises(ExtractionError) as e:
        extract(_zip({"word/document.xml": xml.encode("utf-16")}), "a.docx")
    assert e.value.code == "corrupt"


def test_the_server_reads_page_spans_and_a_chunk_spans_its_blocks_pages() -> None:
    a = extract(pdf(["One", "Two"], []), "a.pdf")
    parsed = parse_artifact(a.to_json(), fmt="pdf", max_bytes=1 << 20)
    assert [b.locator.page_start for b in parsed.blocks] == [1, 2]
    chunks = structural.chunk_artifact(parsed, size=1000, stride=1000)
    assert [(c.text, c.locator.to_dict()) for c in chunks] == [
        ("One\n\nTwo", {"page_start": 1, "page_end": 2})
    ]


@pytest.mark.parametrize(
    "loc", [{"page_start": 2, "page_end": 1}, {"page_start": 1}, {"page_end": 3}]
)
def test_the_server_refuses_a_page_span_that_is_not_a_range(loc: dict[str, int]) -> None:
    d = json.loads(extract(pdf(["One"], []), "a.pdf").to_json())
    d["blocks"][0]["locator"] = loc
    with pytest.raises(InvalidArtifact, match="page_start..page_end"):
        parse_artifact(json.dumps(d).encode(), fmt="pdf", max_bytes=1 << 20)


def test_a_pdf_characters_lone_surrogate_reads_as_a_replacement_and_a_warning() -> None:
    """A font whose character map gives `A` a lone surrogate costs that character,
    not the page or the file: the server reads the artifact."""
    a = extract(pdf(["AB"], [], broken_cmap=True), "a.pdf")
    assert [b.text for b in a.blocks] == ["�B"]
    assert a.warnings == ("page 1: a character its font maps to a lone surrogate reads U+FFFD",)
    assert a.coverage.complete
    parse_artifact(a.to_json(), fmt="pdf", max_bytes=1 << 20)


def test_a_form_named_many_times_is_searched_for_an_image_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from loom_extractor import pdffile

    # These dictionaries hold no content stream to scan for an inline image.
    monkeypatch.setattr(pdffile, "_draws_inline_image", lambda contents, reader: False)

    visits = 0

    class _Dict(dict):  # a pypdf dictionary: resolves to itself
        def get_object(self) -> _Dict:
            return self

        def get(self, key: object, default: object = None) -> object:
            nonlocal visits
            if key == "/Resources":
                visits += 1
            return super().get(key, default)

    form = _Dict({"/Subtype": "/Form"})
    form["/Resources"] = _Dict({"/XObject": _Dict({f"/F{i}": form for i in range(50)})})
    page = _Dict({"/Resources": form["/Resources"]})
    assert pdffile._has_image(page, None) is False
    assert visits <= 2  # the page and the form once, not 50 ** depth


def _docx_parts(body: str) -> dict[str, str | bytes]:
    return {"word/document.xml": f"<w:document {_W}><w:body>{body}</w:body></w:document>"}


def test_docx_rows_and_cells_inside_content_controls_are_read() -> None:
    row = (
        "<w:tr><w:tc>"
        + _wp("a")
        + "</w:tc><w:sdt><w:sdtContent><w:tc>"
        + _wp("b")
        + "</w:tc></w:sdtContent></w:sdt></w:tr>"
    )
    table = f"<w:tbl><w:sdt><w:sdtContent>{row}</w:sdtContent></w:sdt><w:customXml>{row}</w:customXml></w:tbl>"
    a = extract(_zip(_docx_parts(f"<w:customXml>{_wp('Intro')}</w:customXml>{table}")), "a.docx")
    assert [b.text for b in a.blocks] == ["Intro", "a | b\na | b"]
    assert a.blocks[1].attrs == {"table": {"rows": 2, "columns": 2}}


@pytest.mark.parametrize(
    ("body", "unit"),
    [
        (_wp("Text") + "<w:altChunk/>", "embedded document"),
        (_picture(), "document"),
        (_drawing(f"{_DRAWINGML}/chart", ""), "document"),
        (_MODEL_3D, "document"),
        (_WORDART, "document"),
    ],
)
def test_docx_content_it_does_not_read_is_a_failed_unit(body: str, unit: str) -> None:
    a = extract(_zip(_docx_parts(body)), "a.docx")
    assert [f.unit for f in a.coverage.failed] == [unit] and not a.coverage.complete


def test_a_docx_drawing_beside_text_is_no_failure_and_an_odd_outline_level_no_heading() -> None:
    body = _picture() + _wp("Text", extra='<w:outlineLvl w:val="²"/>')
    a = extract(_zip(_docx_parts(body)), "a.docx")
    assert [(b.type, b.text) for b in a.blocks] == [("paragraph", "Text")]
    assert a.coverage.complete


def test_a_pptx_shape_in_alternate_content_is_read_once_and_a_chart_slide_fails() -> None:
    alt = (
        f'<mc:AlternateContent {_MC}><mc:Choice Requires="a14">{_sp("Equation")}</mc:Choice>'
        f"<mc:Fallback>{_sp('Equation')}</mc:Fallback></mc:AlternateContent>"
    )
    chart = "<p:graphicFrame><a:graphic><a:graphicData/></a:graphic></p:graphicFrame>"
    parts = {
        "ppt/presentation.xml": (
            f'<p:presentation {_P}><p:sldIdLst><p:sldId id="256" r:id="r1"/>'
            '<p:sldId id="257" r:id="r2"/></p:sldIdLst></p:presentation>'
        ),
        "ppt/_rels/presentation.xml.rels": _rels(
            ("r1", "slide", "slides/slide1.xml"), ("r2", "slide", "slides/slide2.xml")
        ),
        "ppt/slides/slide1.xml": _slide(alt),
        "ppt/slides/slide2.xml": _slide(chart),
    }
    a = extract(_zip(parts), "deck.pptx")
    assert [b.text for b in a.blocks] == ["Equation"]
    assert [f.unit for f in a.coverage.failed] == ["slide 2"]


def _deck(slides: dict[str, str]) -> dict[str, str | bytes]:
    ids = "".join(f'<p:sldId id="{256 + i}" r:id="r{i}"/>' for i in range(len(slides)))
    return {
        "ppt/presentation.xml": f"<p:presentation {_P}><p:sldIdLst>{ids}</p:sldIdLst></p:presentation>",
        "ppt/_rels/presentation.xml.rels": _rels(
            *((f"r{i}", "slide", f"slides/{name}") for i, name in enumerate(slides))
        ),
        **{f"ppt/slides/{name}": xml for name, xml in slides.items()},
    }


def test_a_pptx_slides_background_picture_or_picture_filled_group_is_named_as_not_read() -> None:
    background = (
        f"<p:sld {_P}><p:cSld><p:bg><p:bgPr><a:blipFill/></p:bgPr></p:bg>"
        f"<p:spTree>{_sp('Over a screenshot')}</p:spTree></p:cSld></p:sld>"
    )
    group = _slide(f"<p:grpSp><p:grpSpPr><a:blipFill/></p:grpSpPr>{_sp('Grouped')}</p:grpSp>")
    cell = (
        "<p:graphicFrame><a:graphic><a:graphicData><a:tbl><a:tr>"
        "<a:tc><a:txBody><a:p><a:r><a:t>Shot</a:t></a:r></a:p></a:txBody><a:tcPr><a:blipFill/></a:tcPr></a:tc>"
        "</a:tr></a:tbl></a:graphicData></a:graphic></p:graphicFrame>"
    )
    slides = {
        "a.xml": _slide(_sp("Plain")),
        "b.xml": background,
        "c.xml": group,
        "d.xml": _slide(cell),
    }
    a = extract(_zip(_deck(slides)), "deck.pptx")
    assert [b.text for b in a.blocks] == ["Plain", "Over a screenshot", "Grouped", "Shot"]
    assert a.attrs["images_unread"] == [2, 3, 4]


def test_a_pptx_slide_of_only_a_picture_background_is_a_failed_unit() -> None:
    bare = f"<p:sld {_P}><p:cSld><p:bg><p:bgPr><a:blipFill/></p:bgPr></p:bg><p:spTree/></p:cSld></p:sld>"
    a = extract(_zip(_deck({"a.xml": _slide(_sp("Plain")), "b.xml": bare})), "deck.pptx")
    assert [(f.unit, f.reason) for f in a.coverage.failed] == [
        ("slide 2", "no text: its pictures, charts, diagrams or embedded objects are not read")
    ]


def test_a_pptx_drawing_the_reader_does_not_know_is_named_by_its_fallback_picture() -> None:
    ink = (
        f'<mc:AlternateContent {_MC}><mc:Choice Requires="p14"><p14:contentPart xmlns:p14="p14"/>'
        "</mc:Choice><mc:Fallback><p:pic/></mc:Fallback></mc:AlternateContent>"
    )
    # An equation's math is no text the walk reads: its picture fallback counts.
    math = (
        '<a14:m xmlns:a14="http://schemas.microsoft.com/office/drawing/2010/main">'
        '<m:oMath xmlns:m="http://schemas.openxmlformats.org/officeDocument/2006/math">'
        "<m:r><m:t>x=1</m:t></m:r></m:oMath></a14:m>"
    )
    equation = (
        f'<mc:AlternateContent {_MC}><mc:Choice Requires="a14"><p:sp><p:nvSpPr><p:nvPr/></p:nvSpPr>'
        f"<p:txBody><a:p>{math}</a:p></p:txBody></p:sp></mc:Choice>"
        "<mc:Fallback><p:pic/></mc:Fallback></mc:AlternateContent>"
    )
    # A shape in alternate content whose choice is read: its fallback is a copy.
    shape = (
        f'<mc:AlternateContent {_MC}><mc:Choice Requires="a14">{_sp("Boxed")}</mc:Choice>'
        "<mc:Fallback><p:pic/></mc:Fallback></mc:AlternateContent>"
    )
    # A 3D model: its choice a graphic frame, read as a graphic the walk does not read.
    model = (
        f'<mc:AlternateContent {_MC}><mc:Choice Requires="am3d"><p:graphicFrame><a:graphic>'
        '<a:graphicData uri="http://schemas.microsoft.com/office/drawing/2017/model3d"/>'
        "</a:graphic></p:graphicFrame></mc:Choice><mc:Fallback><p:pic/></mc:Fallback></mc:AlternateContent>"
    )
    # A run's text filled with a picture, in a table cell, is no picture behind the cell.
    filled_text = (
        "<p:graphicFrame><a:graphic><a:graphicData><a:tbl><a:tr><a:tc><a:txBody><a:p><a:r>"
        "<a:rPr><a:blipFill/></a:rPr><a:t>Shiny</a:t></a:r></a:p></a:txBody></a:tc></a:tr>"
        "</a:tbl></a:graphicData></a:graphic></p:graphicFrame>"
    )
    slides = {
        "a.xml": _slide(_sp("Notes on ink") + ink),
        "b.xml": _slide(_sp("Proof") + equation),
        "c.xml": _slide(_sp("Model") + model),
        "d.xml": _slide(filled_text),
        "e.xml": _slide(shape),
    }
    a = extract(_zip(_deck(slides)), "deck.pptx")
    assert [b.text for b in a.blocks] == ["Notes on ink", "Proof", "Model", "Shiny", "Boxed"]
    assert a.attrs["images_unread"] == [1, 2, 3]


def test_a_slide_of_a_picture_filled_shape_and_no_text_is_a_failed_unit() -> None:
    filled = "<p:sp><p:nvSpPr><p:nvPr/></p:nvSpPr><p:spPr><a:blipFill/></p:spPr></p:sp>"
    a = extract(_zip(_deck({"a.xml": _slide(_sp("Text")), "b.xml": _slide(filled)})), "deck.pptx")
    assert [f.unit for f in a.coverage.failed] == ["slide 2"]


def test_a_damaged_or_oversized_slide_part_fails_that_slide_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from loom_extractor import ooxml

    parts = _deck(
        {"a.xml": _slide(_sp("Fine")), "b.xml": "not xml", "c.xml": _slide(_sp("x" * 3000))}
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, xml in parts.items():
            # A method OOXML does not use: refused for that part.
            method = zipfile.ZIP_BZIP2 if name.endswith("a.xml") else zipfile.ZIP_DEFLATED
            z.writestr(name, xml, compress_type=method)
    monkeypatch.setattr(ooxml, "MAX_PART_BYTES", 2000)
    a = extract(buf.getvalue(), "deck.pptx")
    reasons = {f.unit: f.reason for f in a.coverage.failed}
    assert set(reasons) == {"slide 1", "slide 2", "slide 3"}, reasons
    assert "method" in reasons["slide 1"] and "not XML" in reasons["slide 2"]
    assert "over 2000 bytes" in reasons["slide 3"]


def test_a_slide_part_with_a_bad_checksum_fails_that_slide_alone() -> None:
    data = bytearray(_zip(_deck({"a.xml": _slide(_sp("Fine")), "b.xml": _slide(_sp("Gone"))})))
    with zipfile.ZipFile(io.BytesIO(bytes(data))) as z:
        info = z.getinfo("ppt/slides/b.xml")
    # Flip a byte of the entry's data, past its local header.
    at = info.header_offset + 30 + len(info.filename) + len(info.extra) + 2
    data[at] ^= 0xFF
    a = extract(bytes(data), "deck.pptx")
    assert [b.text for b in a.blocks] == ["Fine"]
    assert [f.unit for f in a.coverage.failed] == ["slide 2"]


def test_docx_styles_or_notes_that_cannot_be_read_fail_that_unit_not_the_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from loom_extractor import ooxml

    parts = {
        **_docx_parts(_wp("Body text")),
        "word/styles.xml": "not xml",
        "word/footnotes.xml": f"<w:footnotes {_W}>" + "x" * 3000 + "</w:footnotes>",
    }
    monkeypatch.setattr(ooxml, "MAX_PART_BYTES", 2000)
    a = extract(_zip(parts), "a.docx")
    assert [b.text for b in a.blocks] == ["Body text"]
    assert [f.unit for f in a.coverage.failed] == ["styles", "footnotes"]


def test_a_docx_of_pictures_in_a_table_and_no_text_fails() -> None:
    table = f"<w:tbl><w:tr><w:tc>{_picture()}</w:tc></w:tr></w:tbl>"
    a = extract(_zip(_docx_parts(table)), "a.docx")
    assert [f.unit for f in a.coverage.failed] == ["document"]


def test_a_slide_keeps_its_text_when_its_notes_cannot_be_read_and_show_false_is_hidden() -> None:
    parts = _deck({"a.xml": _slide(_sp("Kept"), show=' show="false"')})
    parts["ppt/slides/_rels/a.xml.rels"] = _rels(("n", "notesSlide", "../notesSlides/n.xml"))
    parts["ppt/notesSlides/n.xml"] = "not xml"
    a = extract(_zip(parts), "deck.pptx")
    assert [(b.text, b.attrs) for b in a.blocks] == [("Kept", {"hidden": True})]
    assert [f.unit for f in a.coverage.failed] == ["slide 1 notes"]
    assert (a.coverage.total, a.coverage.extracted) == (1, 0)


def _patched(data: bytes, name: str, patch: Any) -> bytes:
    """`data` with `patch(buffer, info)` applied to entry `name`'s headers."""
    buf = bytearray(data)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        info = z.getinfo(name)
    patch(buf, info)
    return bytes(buf)


def test_a_part_that_inflates_past_its_declared_size_or_names_itself_badly_fails_its_slide() -> (
    None
):
    import struct

    deck = _zip(_deck({"a.xml": _slide(_sp("Fine")), "b.xml": _slide(_sp("y" * 4000))}))

    def shrink(buf: bytearray, info: zipfile.ZipInfo) -> None:
        # Declare 100 bytes uncompressed, in the local header and the directory.
        struct.pack_into("<I", buf, info.header_offset + 22, 100)
        name = info.filename.encode()
        at = buf.find(b"PK\x01\x02")
        while buf[at + 46 : at + 46 + len(name)] != name:
            at = buf.find(b"PK\x01\x02", at + 1)
        struct.pack_into("<I", buf, at + 24, 100)

    def bad_name(buf: bytearray, info: zipfile.ZipInfo) -> None:
        # The local header claims a UTF-8 name and holds a byte that is not UTF-8.
        flags = struct.unpack_from("<H", buf, info.header_offset + 6)[0]
        struct.pack_into("<H", buf, info.header_offset + 6, flags | 0x800)
        buf[info.header_offset + 30] = 0xFF

    for patch in (shrink, bad_name):
        a = extract(_patched(deck, "ppt/slides/b.xml", patch), "deck.pptx")
        assert [b.text for b in a.blocks] == ["Fine"], patch.__name__
        assert [f.unit for f in a.coverage.failed] == ["slide 2"], patch.__name__


def test_a_part_in_an_encoding_the_xml_parser_does_not_read_fails_its_slide() -> None:
    sjis = '<?xml version="1.0" encoding="shift_jis"?>' + _slide(_sp("x"))
    a = extract(_zip(_deck({"a.xml": _slide(_sp("Fine")), "b.xml": sjis})), "deck.pptx")
    assert [b.text for b in a.blocks] == ["Fine"] and [f.unit for f in a.coverage.failed] == [
        "slide 2"
    ]


def test_two_outline_entries_of_one_title_on_a_page_are_two_headings() -> None:
    nested = [("Overview", 0, [("Overview", 0)])]
    a = extract(pdf(["Overview\nA\nOverview\nB"], nested), "a.pdf")
    assert [(b.type, b.depth, b.text) for b in a.blocks] == [
        ("heading", 1, "Overview"),
        ("paragraph", 0, "A"),
        ("heading", 2, "Overview"),
        ("paragraph", 0, "B"),
    ]
    once = extract(pdf(["Overview\nA"], nested), "a.pdf")
    assert [(b.type, b.depth) for b in once.blocks] == [
        ("heading", 1),
        ("heading", 2),
        ("paragraph", 0),
    ]


def test_a_subsection_before_a_later_sibling_on_one_page_keeps_its_place() -> None:
    a = extract(pdf(["A\nx\nA1\ny\nB\nz"], [("A", 0, [("A1", 0)]), ("B", 0, [])]), "a.pdf")
    assert [(b.type, b.text, b.section_path) for b in a.blocks] == [
        ("heading", "A", ("A",)),
        ("paragraph", "x", ("A",)),
        ("heading", "A1", ("A", "A1")),
        ("paragraph", "y", ("A", "A1")),
        ("heading", "B", ("B",)),
        ("paragraph", "z", ("B",)),
    ]


def test_a_hidden_slides_title_is_marked_hidden_too() -> None:
    a = extract(
        _zip(_deck({"a.xml": _slide(_sp("Only a title", "title"), show=' show="0"')})), "d.pptx"
    )
    assert [(b.type, b.attrs) for b in a.blocks] == [("heading", {"hidden": True})]


def test_a_failed_pages_outline_headings_still_open_their_sections() -> None:
    """A scanned divider page that starts chapter B: the text after it is B's."""
    a = extract(pdf(["A text", None, "more"], [("A", 0, []), ("B", 1, [])]), "a.pdf")
    assert [(b.type, b.text, b.section_path) for b in a.blocks] == [
        ("heading", "A", ("A",)),
        ("paragraph", "A text", ("A",)),
        ("heading", "B", ("B",)),
        ("paragraph", "more", ("B",)),
    ]
    assert [f.unit for f in a.coverage.failed] == ["page 2"]


def test_a_slide_id_whose_relationship_is_missing_is_a_failed_slide() -> None:
    parts = _deck({"a.xml": _slide(_sp("Fine"))})
    parts["ppt/presentation.xml"] = parts["ppt/presentation.xml"].replace(
        "</p:sldIdLst>", '<p:sldId id="300" r:id="gone"/></p:sldIdLst>'
    )
    a = extract(_zip(parts), "deck.pptx")
    assert [b.text for b in a.blocks] == ["Fine"]
    assert (a.coverage.total, a.coverage.extracted) == (2, 1)
    assert [f.unit for f in a.coverage.failed] == ["slide 2"]


def test_an_outline_level_too_long_for_a_number_is_no_level_and_costs_nothing_else() -> None:
    long = "9" * 5000
    parts = {
        **_docx_parts(_wp("Chapter", "Odd") + _wp("Body", extra=f'<w:outlineLvl w:val="{long}"/>')),
        "word/styles.xml": (
            f'<w:styles {_W}><w:style w:type="paragraph" w:styleId="Odd"><w:name w:val="Odd"/>'
            f'<w:pPr><w:outlineLvl w:val="{long}"/></w:pPr></w:style></w:styles>'
        ),
    }
    a = extract(_zip(parts), "a.docx")
    assert [(b.type, b.text) for b in a.blocks] == [("paragraph", "Chapter"), ("paragraph", "Body")]
    assert a.coverage.complete


def test_an_outline_level_of_body_text_is_no_heading_on_a_style_or_a_paragraph() -> None:
    styles = (
        f"<w:styles {_W}>"
        '<w:style w:type="paragraph" w:styleId="Chapter"><w:name w:val="Chapter"/>'
        '<w:pPr><w:outlineLvl w:val="0"/></w:pPr></w:style>'
        '<w:style w:type="paragraph" w:styleId="NoToc"><w:name w:val="NoToc"/>'
        '<w:basedOn w:val="Chapter"/><w:pPr><w:outlineLvl w:val="9"/></w:pPr></w:style>'
        "</w:styles>"
    )
    body = (
        _wp("One", "Chapter")
        + _wp("Kept out of the contents", "NoToc")
        + _wp("Body text here", "Chapter", extra='<w:outlineLvl w:val="9"/>')
    )
    a = extract(_zip({**_docx_parts(body), "word/styles.xml": styles}), "a.docx")
    assert [(b.type, b.text, b.section_path) for b in a.blocks] == [
        ("heading", "One", ("One",)),
        ("paragraph", "Kept out of the contents", ("One",)),
        ("paragraph", "Body text here", ("One",)),
    ]


def test_a_slide_nested_past_what_the_walk_reads_fails_that_slide_alone() -> None:
    deep = "<p:grpSp>" * 3000 + _sp("Too deep") + "</p:grpSp>" * 3000
    a = extract(_zip(_deck({"a.xml": _slide(_sp("Fine")), "b.xml": _slide(deep)})), "deck.pptx")
    assert [b.text for b in a.blocks] == ["Fine"]
    assert [f.unit for f in a.coverage.failed] == ["slide 2"]


def test_a_docx_whose_only_picture_was_deleted_is_no_pictures_only_document() -> None:
    deleted = f"<w:p><w:del><w:r>{_run(_picture())}</w:r></w:del></w:p>"
    a = extract(_zip(_docx_parts(deleted)), "a.docx")
    assert a.blocks == () and a.coverage.failed == ()


def test_parts_past_the_package_total_fail_their_unit_and_a_document_part_the_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ooxml, "MAX_PACKAGE_BYTES", 15_000)
    deck = _deck({"a.xml": _slide(_sp("Fine")), "b.xml": _slide(_sp("x" * 20_000))})
    a = extract(_zip(deck), "deck.pptx")
    reasons = {f.unit: f.reason for f in a.coverage.failed}
    assert set(reasons) == {"slide 2"} and "past 15000 bytes" in reasons["slide 2"]
    with pytest.raises(ExtractionError) as e:
        extract(_zip(_docx_parts(_wp("y" * 20_000))), "a.docx")
    assert e.value.code == "corrupt"
