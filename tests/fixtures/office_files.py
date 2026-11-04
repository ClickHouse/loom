"""Files of the binary formats the reference extractor parses, built byte by byte
for the tests: a PDF of text pages and image pages, with an outline."""


from __future__ import annotations

import io

from pypdf import PdfWriter

# A page that draws its image through a form XObject, as many scans do, and one
# that draws it inline in its content stream, as some scanners write a page.
FORM_IMAGE = "\x00form"
INLINE_IMAGE = "\x00inline"


def pdf(
    pages: list[str | None],
    outline: list[tuple[str, int, list[tuple[str, int]]]],
    *,
    broken_cmap: bool = False,
) -> bytes:
    """A PDF of `pages` (each its lines, None for a page of one image, FORM_IMAGE or
    INLINE_IMAGE) with `outline` entries `(title, page index, children)`. With
    `broken_cmap`, the font's character map gives `A` a lone surrogate."""
    objs: list[bytes] = []

    def add(body: bytes) -> int:
        objs.append(body)
        return len(objs)

    cmap = b""
    if broken_cmap:
        body = (
            b"/CIDInit /ProcSet findresource begin 12 dict begin begincmap"
            b" 1 begincodespacerange <00> <FF> endcodespacerange"
            b" 1 beginbfchar <41> <D800> endbfchar endcmap end end"
        )
        cmap = b" /ToUnicode %d 0 R" % add(
            b"<< /Length %d >>\nstream\n%s\nendstream" % (len(body), body)
        )
    font = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica%s >>" % cmap)
    image = add(
        b"<< /Type /XObject /Subtype /Image /Width 1 /Height 1 /ColorSpace /DeviceGray"
        b" /BitsPerComponent 8 /Length 1 >>\nstream\n\x00\nendstream"
    )
    form = add(
        b"<< /Type /XObject /Subtype /Form /BBox [0 0 1 1] /Resources << /XObject << /Im1 %d 0 R >> >>"
        b" /Length 7 >>\nstream\n/Im1 Do\nendstream" % image
    )
    pages_id = len(objs) + 1 + 2 * len(pages)  # after every page and its contents
    page_ids = []
    for text in pages:
        if text == INLINE_IMAGE:
            stream, res = b"q 100 0 0 100 0 0 cm BI /W 1 /H 1 /CS /G /BPC 8 ID \x00 EI Q", b"<< >>"
        elif text is None or text == FORM_IMAGE:
            stream, res = (
                b"q 100 0 0 100 0 0 cm /Im1 Do Q",
                b"<< /XObject << /Im1 %d 0 R >> >>" % (image if text is None else form),
            )
        else:
            lines = b" ".join(b"(%s) Tj 0 -20 Td" % line.encode() for line in text.split("\n"))
            stream, res = (
                b"BT /F1 12 Tf 72 720 Td " + lines + b" ET",
                b"<< /Font << /F1 %d 0 R >> >>" % font,
            )
        contents = add(b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream))
        page_ids.append(
            add(
                b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 612 792] /Contents %d 0 R /Resources %s >>"
                % (pages_id, contents, res)
            )
        )
    kids = b" ".join(b"%d 0 R" % p for p in page_ids)
    assert add(b"<< /Type /Pages /Kids [%s] /Count %d >>" % (kids, len(pages))) == pages_id
    writer = PdfWriter()
    writer.append(io.BytesIO(_assemble(objs, pages_id)))
    for title, page, children in outline:
        parent = writer.add_outline_item(title, page)
        for child, child_page in children:
            writer.add_outline_item(child, child_page, parent=parent)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def _assemble(objs: list[bytes], pages_id: int) -> bytes:
    catalog = len(objs) + 1
    objs = [*objs, b"<< /Type /Catalog /Pages %d 0 R >>" % pages_id]
    out, offsets = bytearray(b"%PDF-1.7\n"), []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (i, body)
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root %d 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objs) + 1,
        catalog,
        xref,
    )
    return bytes(out)
