"""Build the document-parsing evaluation corpus: each file rendered from a spec
(specs.py), with gold labels taken from the spec and the page of each gold item
from the rendered file (poppler's pdftotext, a parser the scorer does not grade).

`python -m bench.docparse build` runs it; the module docstring of
`bench/docparse/__main__.py` says what it needs.
"""

from __future__ import annotations

import io
import json
import random
import shutil
import subprocess
import sys
from pathlib import Path

from docx import Document
from docx.enum.text import WD_BREAK
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt
from PIL import Image, ImageDraw, ImageFilter, ImageFont
from pptx import Presentation
from pptx.util import Inches
from pypdf import PdfReader, PdfWriter
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    PageBreak,
    PageTemplate,
    Paragraph,
    Preformatted,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from bench.docparse import specs as S

OUT = Path(".")
SRC = Path(".")
WORK = Path(".")
IMAGE = "docparse-tools:local"
CONTAINER = (
    "docparse-tools-run"  # docker run -d --name docparse-tools-run -v OUT:/c ... sleep infinity
)
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
FONT_B = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
HAND = "/usr/share/fonts/truetype/freefont/FreeSansOblique.ttf"


def docker(*cmd: str) -> None:
    subprocess.run(["docker", "exec", CONTAINER, *cmd], check=True, capture_output=True)


def _uid() -> str:
    import os

    return f"{os.getuid()}:{os.getgid()}"


def chromium_pdf(html_path: Path, pdf_path: Path) -> None:
    docker(
        "chromium",
        "--headless",
        "--no-sandbox",
        "--disable-gpu",
        "--no-pdf-header-footer",
        f"--print-to-pdf=/c/{pdf_path.relative_to(OUT)}",
        f"file:///c/{html_path.relative_to(OUT)}",
    )


def soffice_pdf(src: Path, outdir: Path) -> Path:
    docker(
        "soffice",
        "-env:UserInstallation=file:///tmp/lo",
        "--headless",
        "--convert-to",
        "pdf",
        "--outdir",
        f"/c/{outdir.relative_to(OUT)}",
        f"/c/{src.relative_to(OUT)}",
    )
    return outdir / (src.stem + ".pdf")


# ---- gold


def gold_doc(spec: dict) -> dict:
    blocks: list[dict] = []
    tables: list[dict] = []
    for e in spec["elements"]:
        if e[0] == "h":
            blocks.append({"kind": "heading", "level": e[1], "text": e[2]})
        elif e[0] == "p":
            blocks.append({"kind": "paragraph", "text": e[1]})
        elif e[0] in ("ul", "ol"):
            for depth, t in S.items_text(e[1]):
                blocks.append({"kind": "item", "depth": depth, "text": t})
        elif e[0] == "code":
            blocks.append({"kind": "code", "text": e[1]})
        elif e[0] == "table":
            tables.append({"rows": e[1]})
    return {"blocks": blocks, "tables": tables}


def gold_deck(spec: dict, *, notes: bool, image_text: bool = True) -> dict:
    blocks: list[dict] = []
    tables: list[dict] = []
    for n, s in enumerate(spec["elements"], start=1):
        blocks.append({"kind": "heading", "level": 1, "text": s["title"], "page": n})
        for b in s.get("bullets", []):
            blocks.append({"kind": "item", "text": b, "page": n})
        for lab in s.get("labels", []):
            blocks.append({"kind": "label", "text": lab, "page": n})
        if image_text:
            for lab in s.get("image", []):
                blocks.append({"kind": "image_text", "text": lab, "page": n})
        if "table" in s:
            tables.append({"rows": s["table"], "page_start": n, "page_end": n})
        if notes and s.get("notes"):
            blocks.append({"kind": "note", "text": s["notes"], "page": n})
    return {"blocks": blocks, "tables": tables}


def norm_words(s: str) -> list[str]:
    import re

    return re.findall(r"[a-z0-9]+", s.lower().replace("ﬁ", "fi").replace("ﬂ", "fl"))


def pages_text(pdf: Path) -> list[list[str]]:
    rel = pdf.relative_to(OUT)
    out = subprocess.run(
        ["docker", "exec", CONTAINER, "pdftotext", "-raw", f"/c/{rel}", "-"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [norm_words(p) for p in out.split("\f")[:-1] or [out]]


def _run(probe: list[str], pw: list[str]) -> bool:
    n = len(probe)
    return any(pw[j : j + n] == probe for j in range(len(pw) - n + 1))


def _find(words: list[str], pages: list[list[str]], from_end: bool = False) -> int:
    """The 1-based page holding this run of words (its first or last 6), 0 if none."""
    probe = words[-6:] if from_end else words[:6]
    if len(words) > 6 and not from_end and not any(_run(probe, pw) for pw in pages):
        probe = words[:3]
    if not probe:
        return 0
    n = len(probe)
    for i, pw in enumerate(pages, start=1):
        for j in range(len(pw) - n + 1):
            if pw[j : j + n] == probe:
                return i
    return 0


def locate(gold: dict, pdf: Path) -> list[str]:
    """Fill each block's and table's page from the rendered PDF; returns what was not found."""
    pages = pages_text(pdf)
    missing: list[str] = []
    for b in gold["blocks"]:
        if "page" in b:
            continue
        w = norm_words(b["text"])
        p0, p1 = _find(w, pages), _find(w, pages, from_end=True)
        if not p0:
            missing.append(b["text"][:60])
        b["page"] = p0 or p1
        if p1 and p1 != p0:
            b["page_end"] = p1
    for t in gold["tables"]:
        if "page_start" in t:
            continue
        first = norm_words(" ".join(t["rows"][0]))
        last = norm_words(" ".join(t["rows"][-1]))
        t["page_start"] = _find(first, pages) or _find(norm_words(t["rows"][0][0]), pages)
        t["page_end"] = _find(last, pages, from_end=True) or t["page_start"]
        if not t["page_start"]:
            missing.append("table " + t["rows"][0][0])
    gold["pages"] = len(pages)
    return missing


# ---- HTML (Chromium prints it as a browser-exported document does)

CSS = {
    "doc": """@page { size: Letter; margin: 0.9in 1in; }
body { font-family: 'Liberation Sans', Arial, sans-serif; font-size: 11pt; line-height: 1.35; }
h1 { font-size: 22pt; font-weight: 400; } h2 { font-size: 15pt; font-weight: 400; margin-top: 18pt; }
h3 { font-size: 12.5pt; color: #434343; } h4 { font-size: 11.5pt; }
pre { font-family: 'Liberation Mono', monospace; font-size: 9.5pt; background: #f3f3f3; padding: 6pt; white-space: pre-wrap; }
table { border-collapse: collapse; margin: 6pt 0; } td, th { border: 1px solid #000; padding: 3pt 6pt; vertical-align: top; text-align: left; }
""",
    "borderless": "td, th { border: none !important; padding: 2pt 14pt 2pt 0; } th { border-bottom: 1px solid #555 !important; font-weight: 700; }",
    "two_col": ".cols { column-count: 2; column-gap: 0.35in; } h1 { column-span: all; }",
    "footer": "@page { @bottom-center { content: 'TITLE · page ' counter(page); font-size: 8pt; color: #666; } @top-right { content: 'Internal draft'; font-size: 8pt; color: #666; } }",
}


def html_doc(spec: dict, *, style: str = "", footer: bool = False) -> str:
    css = (
        CSS["doc"]
        + (CSS["borderless"] if "borderless" in style else "")
        + (CSS["two_col"] if "two_col" in style else "")
    )
    if footer:
        css += CSS["footer"].replace("TITLE", spec["title"].replace("'", ""))
    body: list[str] = []
    for e in spec["elements"]:
        if e[0] == "h":
            body.append(f"<h{e[1]}>{S.esc(e[2])}</h{e[1]}>")
        elif e[0] == "p":
            body.append(f"<p>{S.esc(e[1])}</p>")
        elif e[0] in ("ul", "ol"):
            body.append(_html_list(e[0], e[1]))
        elif e[0] == "code":
            body.append(f"<pre>{S.esc(e[1])}</pre>")
        elif e[0] == "table":
            rows = e[1]
            head = "".join(f"<th>{S.esc(c)}</th>" for c in rows[0])
            trs = "".join(
                "<tr>" + "".join(f"<td>{S.esc(c)}</td>" for c in r) + "</tr>" for r in rows[1:]
            )
            body.append(f"<table><thead><tr>{head}</tr></thead><tbody>{trs}</tbody></table>")
        elif e[0] == "break":
            body.append('<div style="break-after: page"></div>')
    inner = "\n".join(body)
    if "two_col" in style:
        first = inner.index("</h1>") + 5
        inner = inner[:first] + '<div class="cols">' + inner[first:] + "</div>"
    return f"<!doctype html><html><head><meta charset='utf-8'><title>{S.esc(spec['title'])}</title><style>{css}</style></head><body>{inner}</body></html>"


def _html_list(tag: str, items: list) -> str:
    out = [f"<{tag}>"]
    for it in items:
        if isinstance(it, tuple):
            out.append(
                f"<li>{S.esc(it[0])}<ul>"
                + "".join(f"<li>{S.esc(s)}</li>" for s in it[1])
                + "</ul></li>"
            )
        else:
            out.append(f"<li>{S.esc(it)}</li>")
    out.append(f"</{tag}>")
    return "".join(out)


def html_slides(spec: dict) -> str:
    css = """@page { size: 960px 540px; margin: 0; } body { margin: 0; font-family: 'Liberation Sans', Arial; }
.s { width: 960px; height: 540px; position: relative; break-after: page; overflow: hidden; }
.t { position: absolute; left: 60px; top: 40px; font-size: 34px; } ul { position: absolute; left: 60px; top: 130px; font-size: 22px; width: 820px; }
table { position: absolute; left: 60px; top: 140px; border-collapse: collapse; font-size: 18px; } td, th { border: 1px solid #999; padding: 6px 12px; }
.box { position: absolute; width: 170px; height: 70px; border: 2px solid #3367d6; border-radius: 8px; text-align: center; line-height: 70px; font-size: 17px; }
.arrow { position: absolute; height: 2px; background: #333; } .img { position: absolute; left: 60px; top: 130px; }"""
    out: list[str] = []
    for s in spec["elements"]:
        parts = [f"<div class='t'>{S.esc(s['title'])}</div>"]
        if s.get("bullets"):
            parts.append("<ul>" + "".join(f"<li>{S.esc(b)}</li>" for b in s["bullets"]) + "</ul>")
        if s.get("table"):
            rows = s["table"]
            parts.append(
                "<table><tr>"
                + "".join(f"<th>{S.esc(c)}</th>" for c in rows[0])
                + "</tr>"
                + "".join(
                    "<tr>" + "".join(f"<td>{S.esc(c)}</td>" for c in r) + "</tr>" for r in rows[1:]
                )
                + "</table>"
            )
        if s.get("labels"):
            for k, lab in enumerate(s["labels"]):
                x, y = 60 + (k % 2) * 480, 150 + (k // 2) * 170
                parts.append(f"<div class='box' style='left:{x}px;top:{y}px'>{S.esc(lab)}</div>")
            parts.append("<div class='arrow' style='left:230px;top:185px;width:310px'></div>")
        if s.get("image"):
            parts.append(f"<img class='img' src='{s['_img']}' width='820'>")
        out.append(f"<div class='s'>{''.join(parts)}</div>")
    return f"<!doctype html><html><head><meta charset='utf-8'><style>{css}</style></head><body>{''.join(out)}</body></html>"


# ---- DOCX


def docx_doc(spec: dict, path: Path, *, footer: bool = False, grid: bool = True) -> None:
    d = Document()
    if footer:
        p = d.sections[0].footer.paragraphs[0]
        p.text = f"{spec['title']} · Confidential · page "
        _field(p, "PAGE")
    first = True
    for e in spec["elements"]:
        if e[0] == "h":
            d.add_heading(e[2], level=0 if (first and e[1] == 1) else min(e[1] - 1, 9) or 1)
            first = False
        elif e[0] == "p":
            d.add_paragraph(e[1])
        elif e[0] in ("ul", "ol"):
            base = "List Bullet" if e[0] == "ul" else "List Number"
            for depth, t in S.items_text(e[1]):
                d.add_paragraph(t, style=base if depth == 1 else f"{base} 2")
        elif e[0] == "code":
            p = d.add_paragraph()
            lines = e[1].split("\n")
            for k, line in enumerate(lines):
                r = p.add_run(line)
                r.font.name = "Courier New"
                r.font.size = Pt(9)
                if k < len(lines) - 1:
                    r.add_break()
        elif e[0] == "table":
            rows = e[1]
            t = d.add_table(rows=len(rows), cols=len(rows[0]))
            if grid:
                t.style = "Table Grid"
            for i, r in enumerate(rows):
                for j, c in enumerate(r):
                    t.cell(i, j).text = c
        elif e[0] == "break":
            d.add_paragraph().add_run().add_break(WD_BREAK.PAGE)
    d.core_properties.title = spec["title"]
    d.save(path)


def _field(p, code: str) -> None:
    r = p.add_run()
    for kind, text in (("begin", None), (None, code), ("end", None)):
        if kind:
            el = OxmlElement("w:fldChar")
            el.set(qn("w:fldCharType"), kind)
        else:
            el = OxmlElement("w:instrText")
            el.text = text
        r._r.append(el)


# ---- PPTX


def pptx_deck(spec: dict, path: Path) -> None:
    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
    for i, s in enumerate(spec["elements"]):
        layout = prs.slide_layouts[0 if i == 0 else (1 if s.get("bullets") else 5)]
        sl = prs.slides.add_slide(layout)
        sl.shapes.title.text = s["title"]
        if i == 0:
            sl.placeholders[1].text = s["bullets"][0]
        elif s.get("bullets"):
            body = sl.placeholders[1].text_frame
            body.text = s["bullets"][0]
            for b in s["bullets"][1:]:
                body.add_paragraph().text = b
        if s.get("table"):
            rows = s["table"]
            gt = sl.shapes.add_table(
                len(rows),
                len(rows[0]),
                Inches(0.8),
                Inches(1.8),
                Inches(11),
                Inches(0.5 * len(rows)),
            ).table
            for r, row in enumerate(rows):
                for c, val in enumerate(row):
                    gt.cell(r, c).text = val
        if s.get("labels"):
            grp = sl.shapes.add_group_shape()
            for k, lab in enumerate(s["labels"]):
                box = grp.shapes.add_shape(
                    1, Inches(0.8 + (k % 2) * 6), Inches(2 + (k // 2) * 2.2), Inches(3), Inches(1.1)
                )
                box.text_frame.text = lab
            grp.shapes.add_connector(1, Inches(3.8), Inches(2.55), Inches(6.8), Inches(2.55))
        if s.get("image"):
            sl.shapes.add_picture(s["_img"], Inches(0.8), Inches(1.6), width=Inches(11.5))
        if s.get("notes"):
            sl.notes_slide.notes_text_frame.text = s["notes"]
    prs.save(path)


# ---- images: a whiteboard photo and scanned pages


def whiteboard(labels: list[str], path: Path, seed: int) -> None:
    r = random.Random(seed)
    img = Image.new("RGB", (1640, 820), (246, 244, 236))
    d = ImageDraw.Draw(img)
    f = ImageFont.truetype(HAND, 46)
    for k, lab in enumerate(labels):
        x, y = 90 + k * 520, 220 + r.randint(-40, 40)
        d.rounded_rectangle([x, y, x + 440, y + 170], radius=24, outline=(30, 60, 160), width=6)
        d.text((x + 30, y + 58), lab, font=f, fill=(20, 20, 20))
        if k:
            d.line([x - 80, y + 85, x, y + 85], fill=(150, 30, 30), width=6)
    img = img.rotate(r.uniform(-1.5, 1.5), expand=False, fillcolor=(246, 244, 236)).filter(
        ImageFilter.GaussianBlur(0.8)
    )
    img.save(path)


def scan(pdf: Path, out: Path, seed: int, dpi: int = 110) -> None:
    r = random.Random(seed)
    prefix = WORK / f"scan_{out.stem}"
    docker(
        "pdftoppm",
        "-gray",
        "-r",
        str(dpi),
        "-png",
        f"/c/{pdf.relative_to(OUT)}",
        f"/c/{prefix.relative_to(OUT)}",
    )
    pages = sorted(WORK.glob(f"scan_{out.stem}-*.png"))
    imgs = []
    for p in pages:
        im = Image.open(p).convert("L")
        im = im.rotate(r.uniform(-0.9, 0.9), expand=False, fillcolor=255, resample=Image.BICUBIC)
        noise = Image.effect_noise(im.size, 18).point(lambda v: 255 if v > 120 else 225)
        im = Image.composite(noise, im, im.point(lambda v: 255 if v > 200 else 0))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=45)
        imgs.append(Image.open(io.BytesIO(buf.getvalue())))
        p.unlink()
    imgs[0].save(out, "PDF", save_all=True, append_images=imgs[1:], resolution=dpi)


# ---- ReportLab: layouts a browser or office export does not make


def rl_story(spec: dict, *, borderless: bool = False, heading_bold_only: bool = False) -> list:
    ss = getSampleStyleSheet()
    body = ParagraphStyle(
        "b", parent=ss["BodyText"], fontName="Times-Roman", fontSize=10.5, leading=13
    )
    hs = {
        1: ParagraphStyle(
            "h1", parent=ss["Heading1"], fontName="Helvetica-Bold", fontSize=18, leading=22
        ),
        2: ParagraphStyle(
            "h2", parent=ss["Heading2"], fontName="Helvetica-Bold", fontSize=13.5, leading=17
        ),
        3: ParagraphStyle(
            "h3", parent=ss["Heading3"], fontName="Helvetica-Bold", fontSize=11.5, leading=14
        ),
    }
    if heading_bold_only:  # headings the size of the text, set apart by weight alone
        for k in hs:
            hs[k] = ParagraphStyle(
                f"hb{k}",
                parent=body,
                fontName="Times-Bold",
                fontSize=10.5 + (3 - k) * 0.0,
                spaceBefore=8,
            )
        hs[1] = ParagraphStyle("hb1", parent=body, fontName="Times-Bold", fontSize=16, leading=20)
    out: list = []
    for e in spec["elements"]:
        if e[0] == "h":
            out.append(Paragraph(S.esc(e[2]), hs[min(e[1], 3)]))
        elif e[0] == "p":
            out.append(Paragraph(S.esc(e[1]), body))
        elif e[0] in ("ul", "ol"):
            for k, (depth, t) in enumerate(S.items_text(e[1]), start=1):
                mark = "•" if e[0] == "ul" or depth > 1 else f"{k}."
                out.append(
                    Paragraph(
                        f"{mark} {S.esc(t)}",
                        ParagraphStyle("li", parent=body, leftIndent=14 * depth),
                    )
                )
        elif e[0] == "code":
            out.append(
                Preformatted(
                    e[1], ParagraphStyle("code", fontName="Courier", fontSize=8.5, leading=10.5)
                )
            )
        elif e[0] == "table":
            cell = ParagraphStyle("c", parent=body, fontSize=9.5, leading=11.5)
            data = [[Paragraph(S.esc(c), cell) for c in r] for r in e[1]]
            t = Table(data, repeatRows=1, hAlign="LEFT")
            st = [("VALIGN", (0, 0), (-1, -1), "TOP")]
            st += (
                [("LINEBELOW", (0, 0), (-1, 0), 0.8, colors.black)]
                if borderless
                else [("GRID", (0, 0), (-1, -1), 0.5, colors.black)]
            )
            t.setStyle(TableStyle(st))
            out.append(Spacer(1, 4))
            out.append(t)
            out.append(Spacer(1, 6))
        elif e[0] == "break":
            out.append(PageBreak())
    return out


def rl_footer(title: str):
    def draw(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 8)
        canvas.drawString(72, 30, f"{title} — revision 3")
        canvas.drawRightString(A4[0] - 72, 30, f"Page {doc.page}")
        canvas.drawString(72, A4[1] - 34, "Engineering design review")
        canvas.restoreState()

    return draw


def rl_pdf(
    spec: dict,
    path: Path,
    *,
    two_col: bool = False,
    borderless: bool = False,
    footer: bool = False,
    bold_only: bool = False,
) -> None:
    story = rl_story(spec, borderless=borderless, heading_bold_only=bold_only)
    onpage = rl_footer(spec["title"]) if footer else (lambda c, d: None)
    if two_col:
        doc = BaseDocTemplate(str(path), pagesize=A4)
        w, h = A4
        doc.addPageTemplates(
            [
                PageTemplate(
                    id="two",
                    onPage=onpage,
                    frames=[
                        Frame(50, 50, w / 2 - 60, h - 100, id="l"),
                        Frame(w / 2 + 10, 50, w / 2 - 60, h - 100, id="r"),
                    ],
                )
            ]
        )
        doc.build(story)
    else:
        SimpleDocTemplate(str(path), pagesize=A4, title=spec["title"]).build(
            story, onFirstPage=onpage, onLaterPages=onpage
        )


def rotate_pages(src: Path, out: Path, which: set[int], angle: int = 90) -> None:
    w = PdfWriter()
    for i, p in enumerate(PdfReader(src).pages):
        if i in which:
            p.rotate(angle)
        w.add_page(p)
    with open(out, "wb") as f:
        w.write(f)


def landscape_table_pdf(spec: dict, path: Path) -> None:
    """A landscape page (a wide table) inside a portrait document, stored as a
    portrait page whose content is drawn turned and shown with /Rotate."""
    story = rl_story(spec)
    a = WORK / f"{path.stem}_a.pdf"
    SimpleDocTemplate(str(a), pagesize=landscape(A4)).build(story)
    from pypdf import Transformation

    w = PdfWriter(clone_from=str(a))
    for p in w.pages:
        pw, ph = float(p.mediabox.width), float(p.mediabox.height)
        p.add_transformation(Transformation().rotate(90).translate(ph, 0))
        p.mediabox.upper_right = (ph, pw)
        p.rotate(90)
    with open(path, "wb") as f:
        w.write(f)


def build(out: Path, src: Path) -> None:
    global OUT, SRC, WORK
    OUT, SRC, WORK = out.resolve(), src.resolve(), out.resolve() / "_work"
    OUT.mkdir(parents=True, exist_ok=True)
    for p in OUT.iterdir():
        shutil.rmtree(p) if p.is_dir() else p.unlink()
    WORK.mkdir()
    gold_all: list[dict] = []

    def add(
        name: str,
        category: str,
        fmt: str,
        gold: dict,
        *,
        src: str,
        ocr: bool = False,
        path: Path | None = None,
    ) -> None:
        path = path or OUT / name
        if fmt == "pdf":
            miss = [] if ocr else locate(gold, path)
            if miss:
                print("UNLOCATED", name, miss[:5], file=sys.stderr)
        gold_all.append(
            {"file": name, "category": category, "format": fmt, "source": src, "ocr": ocr, **gold}
        )

    # Browser-exported design docs (the shape of a Docs export: Skia text, no outline)
    seeds = iter(range(100, 1000))
    for k in range(6):
        sp = S.design_doc(
            next(seeds), sections=5 + k % 3, numbered=k % 2 == 0, tables=1 + k % 2, table_rows=4 + k
        )
        h = WORK / f"web_doc_{k}.html"
        h.write_text(html_doc(sp, footer=k % 3 == 1))
        chromium_pdf(h, OUT / f"web_doc_{k}.pdf")
        add(f"web_doc_{k}.pdf", "pdf_export", "pdf", gold_doc(sp), src="synthetic")
    for k in range(3):
        sp = S.design_doc(next(seeds), sections=5, tables=2, table_rows=4, table_style="borderless")
        h = WORK / f"web_borderless_{k}.html"
        h.write_text(html_doc(sp, style="borderless", footer=k == 1))
        chromium_pdf(h, OUT / f"web_borderless_{k}.pdf")
        add(f"web_borderless_{k}.pdf", "pdf_borderless_table", "pdf", gold_doc(sp), src="synthetic")
    for k in range(3):
        sp = S.design_doc(next(seeds), sections=4, tables=1, table_rows=48 + 20 * k)
        h = WORK / f"web_longtable_{k}.html"
        h.write_text(html_doc(sp, footer=True))
        chromium_pdf(h, OUT / f"web_longtable_{k}.pdf")
        add(f"web_longtable_{k}.pdf", "pdf_table_span", "pdf", gold_doc(sp), src="synthetic")
    for k in range(2):
        sp = S.design_doc(next(seeds), sections=6, tables=0, code=False)
        h = WORK / f"web_twocol_{k}.html"
        h.write_text(html_doc(sp, style="two_col"))
        chromium_pdf(h, OUT / f"web_twocol_{k}.pdf")
        add(f"web_twocol_{k}.pdf", "pdf_two_column", "pdf", gold_doc(sp), src="synthetic")
    # Public-domain PEPs, printed from their HTML
    peps = [(518, 40), (621, 45), (668, 45), (723, 40), (735, 40), (691, 40)]
    for n, cut in peps:
        sp = S.pep(SRC / f"pep{n}.html", cut)
        h = WORK / f"pep{n}.html"
        h.write_text(html_doc(sp, footer=n % 2 == 1))
        chromium_pdf(h, OUT / f"pep{n}.pdf")
        add(
            f"pep{n}.pdf",
            "pdf_public",
            "pdf",
            gold_doc(sp),
            src=f"PEP {n}, public domain (peps.python.org)",
        )
    # Office documents and their office-suite PDF export
    for k in range(6):
        sp = S.design_doc(
            next(seeds), sections=5 + k % 2, numbered=k % 3 == 0, tables=1 + k % 2, table_rows=4 + k
        )
        docx_doc(sp, OUT / f"office_{k}.docx", footer=k % 2 == 0, grid=k != 4)
        add(f"office_{k}.docx", "docx", "docx", gold_doc(sp), src="synthetic")
        if k < 4:
            shutil.copy(OUT / f"office_{k}.docx", WORK / f"office_export_{k}.docx")
            pdf = soffice_pdf(WORK / f"office_export_{k}.docx", WORK)
            pdf.rename(OUT / f"office_export_{k}.pdf")
            add(f"office_export_{k}.pdf", "pdf_export", "pdf", gold_doc(sp), src="synthetic")
    # Typeset PDFs: no outline, headings by font, two columns, borderless, running furniture
    for k, kw in enumerate(
        [
            {"footer": True},
            {"bold_only": True},
            {"two_col": True, "footer": True},
            {"borderless": True},
        ]
    ):
        sp = S.design_doc(
            next(seeds), sections=5, numbered=k == 1, tables=1 + (k == 3), table_rows=6
        )
        rl_pdf(sp, OUT / f"typeset_{k}.pdf", **kw)
        cat = (
            "pdf_two_column"
            if kw.get("two_col")
            else "pdf_borderless_table"
            if kw.get("borderless")
            else "pdf_native"
        )
        add(f"typeset_{k}.pdf", cat, "pdf", gold_doc(sp), src="synthetic")
    sp = S.design_doc(next(seeds), sections=4, tables=1, table_rows=70)
    rl_pdf(sp, OUT / "typeset_longtable.pdf", footer=True)
    add("typeset_longtable.pdf", "pdf_table_span", "pdf", gold_doc(sp), src="synthetic")
    # Rotated pages
    sp = S.design_doc(next(seeds), sections=4, tables=1, table_rows=6)
    rl_pdf(sp, WORK / "rot_src.pdf")
    rotate_pages(WORK / "rot_src.pdf", OUT / "rotated_0.pdf", {0})
    add("rotated_0.pdf", "pdf_rotated", "pdf", gold_doc(sp), src="synthetic")
    sp = S.design_doc(next(seeds), sections=3, tables=1, table_rows=8)
    landscape_table_pdf(sp, OUT / "rotated_1.pdf")
    add("rotated_1.pdf", "pdf_rotated", "pdf", gold_doc(sp), src="synthetic")
    # Decks: PPTX, its office export, and a browser-exported deck
    for k in range(6):
        sp = S.deck(next(seeds), slides=5 + k % 3, image=k % 2 == 1)
        for i, s in enumerate(sp["elements"]):
            if s.get("image"):
                img = WORK / f"wb_deck{k}_{i}.png"
                whiteboard(s["image"], img, k * 10 + i)
                s["_img"] = str(img)
        pptx_deck(sp, OUT / f"deck_{k}.pptx")
        add(f"deck_{k}.pptx", "pptx", "pptx", gold_deck(sp, notes=True), src="synthetic")
        if k < 3:
            shutil.copy(OUT / f"deck_{k}.pptx", WORK / f"deck_export_{k}.pptx")
            pdf = soffice_pdf(WORK / f"deck_export_{k}.pptx", WORK)
            pdf.rename(OUT / f"deck_export_{k}.pdf")
            g = gold_deck(sp, notes=False)
            add(f"deck_export_{k}.pdf", "pdf_slides", "pdf", g, src="synthetic")
    for k in range(3):
        sp = S.deck(next(seeds), slides=6, notes=False, image=k == 2)
        for i, s in enumerate(sp["elements"]):
            if s.get("image"):
                img = WORK / f"wb_web{k}_{i}.png"
                whiteboard(s["image"], img, 500 + k * 10 + i)
                s["_img"] = img.name
        h = WORK / f"web_deck_{k}.html"
        h.write_text(html_slides(sp))
        chromium_pdf(h, OUT / f"web_deck_{k}.pdf")
        add(f"web_deck_{k}.pdf", "pdf_slides", "pdf", gold_deck(sp, notes=False), src="synthetic")
    # Scans and a whiteboard photo: text only an OCR reads
    for k, src in enumerate(
        ["office_export_1.pdf", "web_doc_2.pdf", "typeset_0.pdf", "web_deck_1.pdf"]
    ):
        scan(OUT / src, OUT / f"scan_{k}.pdf", seed=k)
        g = json.loads(json.dumps(next(x for x in gold_all if x["file"] == src)))
        for key in ("file", "category", "format", "source", "ocr"):
            g.pop(key)
        add(f"scan_{k}.pdf", "pdf_scanned", "pdf", g, src=f"synthetic, a scan of {src}", ocr=True)
    labels = ["ingest api", "kafka topic", "merge workers"]
    whiteboard(labels, WORK / "wb.png", 99)
    Image.open(WORK / "wb.png").convert("RGB").save(OUT / "whiteboard.pdf", "PDF", resolution=150)
    add(
        "whiteboard.pdf",
        "pdf_scanned",
        "pdf",
        {
            "blocks": [{"kind": "image_text", "text": t, "page": 1} for t in labels],
            "tables": [],
            "pages": 1,
        },
        src="synthetic",
        ocr=True,
    )

    # dev / held-out: alternate within each category, in file order
    seen: dict[str, int] = {}
    for g in gold_all:
        n = seen.get(g["category"], 0)
        seen[g["category"]] = n + 1
        g["split"] = "dev" if n % 2 == 0 else "heldout"
    shutil.rmtree(WORK)
    with open(OUT / "gold.jsonl", "w") as f:
        for g in gold_all:
            f.write(json.dumps(g, ensure_ascii=False, sort_keys=True) + "\n")
    print(len(gold_all), "files;", {c: n for c, n in sorted(seen.items())})
