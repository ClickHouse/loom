"""Files of the binary formats the reference extractor parses, built byte by byte
for the tests: a PDF of text pages and image pages, with an outline."""

from __future__ import annotations

import io

from pypdf import PdfWriter

# A page that draws its image through a form XObject, as many scans do, and one
# that draws it inline in its content stream, as some scanners write a page.
FORM_IMAGE = "\x00form"
INLINE_IMAGE = "\x00inline"
FORM_INLINE_IMAGE = "\x00form-inline"  # a form whose own stream draws the image inline
IMAGE = "\x00image"  # an image XObject drawn from the page, as None draws a page of one
UNDRAWN_IMAGE = "\x00undrawn"  # an image the page's resources hold and its content never draws


def pdf(
    pages: list[str | tuple[str, str] | None],
    outline: list[tuple[str, int, list[tuple[str, int]]]],
    *,
    broken_cmap: bool = False,
) -> bytes:
    """A PDF of `pages` with `outline` entries `(title, page index, children)`. A page
    is its lines; None, FORM_IMAGE, INLINE_IMAGE or FORM_INLINE_IMAGE for a page
    of one image; or `(lines, kind)` for a page of text that also draws an image
    of `kind` (any of the above, or IMAGE). UNDRAWN_IMAGE as `kind` names an
    image in the page's resources that its content never draws. With
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
        b"<< /Type /XObject /Subtype /Form /BBox [0 0 1 1]"
        b" /Resources << /XObject << /Im1 %d 0 R >> >>"
        b" /Length 7 >>\nstream\n/Im1 Do\nendstream" % image
    )
    inline = b"BI /W 1 /H 1 /CS /G /BPC 8 ID \x00 EI"
    form_inline = add(
        b"<< /Type /XObject /Subtype /Form /BBox [0 0 1 1] /Resources << >> /Length %d >>"
        b"\nstream\n%s\nendstream" % (len(inline), inline)
    )
    pages_id = len(objs) + 1 + 2 * len(pages)  # after every page and its contents
    page_ids = []
    # Each kind of image as the content that draws it and the XObjects it names.
    drawn = {
        INLINE_IMAGE: (b"q 100 0 0 100 0 0 cm BI /W 1 /H 1 /CS /G /BPC 8 ID \x00 EI Q", b""),
        FORM_INLINE_IMAGE: (b"q 100 0 0 100 0 0 cm /Fm1 Do Q", b"/Fm1 %d 0 R" % form_inline),
        IMAGE: (b"q 100 0 0 100 0 0 cm /Im1 Do Q", b"/Im1 %d 0 R" % image),
        FORM_IMAGE: (b"q 100 0 0 100 0 0 cm /Im1 Do Q", b"/Im1 %d 0 R" % form),
        UNDRAWN_IMAGE: (b"", b"/Im1 %d 0 R" % image),
    }
    for page in pages:
        if page is None:
            text, kind = "", IMAGE
        elif isinstance(page, tuple):
            text, kind = page
        elif page in drawn:
            text, kind = "", page
        else:
            text, kind = page, ""
        stream, xobjects = drawn.get(kind, (b"", b""))
        if text:
            lines = b" ".join(b"(%s) Tj 0 -20 Td" % line.encode() for line in text.split("\n"))
            stream = b"BT /F1 12 Tf 72 720 Td " + lines + b" ET " + stream
        fonts = b" /Font << /F1 %d 0 R >>" % font if text else b""
        images = b" /XObject << %s >>" % xobjects if xobjects else b""
        res = b"<<%s%s >>" % (fonts, images)
        contents = add(b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream))
        page_ids.append(
            add(
                b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 612 792]"
                b" /Contents %d 0 R /Resources %s >>" % (pages_id, contents, res)
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


# A 4 by 2 black image, an image XObject's entries and stream.
GRAY_IMAGE = (
    b"/Width 4 /Height 2 /ColorSpace /DeviceGray /BitsPerComponent 8 /Length 8 >>\nstream\n"
    + bytes(8)
)


def laid_out_pdf(
    pages: list[bytes],
    *,
    rotate: tuple[int, ...] = (),
    width: float = 612,
    image: bytes | list[bytes] = b"",
    forms: tuple[bytes, ...] = (),
    chain: tuple[bytes, ...] = (),
) -> bytes:
    """A PDF of `pages`, each its content stream as written: fonts F1
    (Helvetica), F2 (Helvetica-Bold) and F3 (Courier), `width` points wide and
    792 high, and `/Rotate 90` on the pages `rotate` lists. `image` is an image
    XObject's dictionary entries and stream (`/Width 1 ... >>\nstream\n...`),
    drawn as /Im1, or a list of them, drawn as /Im1, /Im2 and on. `forms` are
    form XObjects' content streams, drawn as /Fm1, /Fm2 and on. `chain` is form
    content streams each holding the next in its own resources (/Ch1 holds /Ch2,
    the last holds /Im1), as a real file nests them; the page holds /Ch1."""
    objs: list[bytes] = []

    def add(body: bytes) -> int:
        objs.append(body)
        return len(objs)

    fonts = b" ".join(
        b"/F%d %d 0 R"
        % (
            i,
            add(
                b"<< /Type /Font /Subtype /Type1 /BaseFont /%s /Encoding /WinAnsiEncoding >>" % name
            ),
        )
        for i, name in enumerate((b"Helvetica", b"Helvetica-Bold", b"Courier"), start=1)
    )
    entries = [
        b"/Im%d %d 0 R" % (n, add(b"<< /Type /XObject /Subtype /Image " + one + b"\nendstream"))
        for n, one in enumerate([image] if isinstance(image, bytes) else image, start=1)
        if one
    ] + [
        b"/Fm%d %d 0 R"
        % (
            n,
            add(
                b"<< /Type /XObject /Subtype /Form /BBox [0 0 1 1] /Length %d >>\nstream\n%s\nendstream"
                % (len(one), one)
            ),
        )
        for n, one in enumerate(forms, start=1)
    ]
    if chain:
        inner = b"/Im1 %d 0 R" % add(b"<< /Type /XObject /Subtype /Image " + GRAY_IMAGE + b"\nendstream")
        for n in range(len(chain), 0, -1):
            one = chain[n - 1]
            ref = add(
                b"<< /Type /XObject /Subtype /Form /BBox [0 0 1 1] /Resources << /XObject << %s >> >>"
                b" /Length %d >>\nstream\n%s\nendstream" % (inner, len(one), one)
            )
            inner = b"/Ch%d %d 0 R" % (n, ref)
        entries.append(inner)
    xobjects = b" /XObject << %s >>" % b" ".join(entries) if entries else b""
    pages_id = len(objs) + 1 + 2 * len(pages)
    page_ids = []
    for k, stream in enumerate(pages):
        contents = add(b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream))
        turn = b" /Rotate 90" if k in rotate else b""
        page_ids.append(
            add(
                b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 %d 792]%s"
                b" /Contents %d 0 R /Resources << /Font << %s >>%s >> >>"
                % (pages_id, int(width), turn, contents, fonts, xobjects)
            )
        )
    kids = b" ".join(b"%d 0 R" % p for p in page_ids)
    assert add(b"<< /Type /Pages /Kids [%s] /Count %d >>" % (kids, len(pages))) == pages_id
    return _assemble(objs, pages_id)


def text_at(x: float, y: float, text: str, font: int = 1, size: float = 11) -> bytes:
    """One run of text at (x, y) from the page's bottom left."""
    return b"BT /F%d %g Tf %g %g Td (%s) Tj ET\n" % (font, size, x, y, text.encode("latin-1"))


def rule(x0: float, y0: float, x1: float, y1: float) -> bytes:
    """A stroked line."""
    return b"%g %g m %g %g l S\n" % (x0, y0, x1, y1)
