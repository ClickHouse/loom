"""XLSX workbooks built part by part for the tests: sheets of cells, shared
strings, styles with a date format, merged ranges and a hidden sheet."""

from __future__ import annotations

import io
import zipfile
from typing import Any

_X = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
_R = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
# Style 1 is a date (built-in format 14), 2 a custom date and time code, 3 a
# built-in time (20, h:mm), 4 a custom elapsed time (`[h]:mm`), 5 the same in
# capitals (`[H]:MM:SS`), 6 an East Asian built-in date (57).
_STYLES = (
    f'<styleSheet {_X}><numFmts><numFmt numFmtId="164" formatCode="yyyy-mm-dd hh:mm"/>'
    '<numFmt numFmtId="165" formatCode="[h]:mm"/><numFmt numFmtId="166" formatCode="[H]:MM:SS"/>'
    '</numFmts><cellXfs><xf numFmtId="0"/><xf numFmtId="14"/><xf numFmtId="164"/>'
    '<xf numFmtId="20"/><xf numFmtId="165"/><xf numFmtId="166"/><xf numFmtId="57"/>'
    "</cellXfs></styleSheet>"
)


def cell(ref: str, value: Any = None, *, t: str = "", s: int = 0, f: str = "") -> str:
    """One `<c>`: `value` is written as the cell's `<v>` (None writes none)."""
    attrs = f' r="{ref}"' + (f' t="{t}"' if t else "") + (f' s="{s}"' if s else "")
    body = (f"<f>{f}</f>" if f else "") + ("" if value is None else f"<v>{value}</v>")
    if t == "inlineStr":
        body = f"<is><t>{value}</t></is>"
    return f"<c{attrs}>{body}</c>"


def sheet(rows: dict[int, list[str]], merges: tuple[str, ...] = ()) -> str:
    data = "".join(f'<row r="{n}">{"".join(cells)}</row>' for n, cells in rows.items())
    merged = (
        f'<mergeCells count="{len(merges)}">'
        + "".join(f'<mergeCell ref="{m}"/>' for m in merges)
        + "</mergeCells>"
        if merges
        else ""
    )
    return f"<worksheet {_X}><sheetData>{data}</sheetData>{merged}</worksheet>"


def workbook(
    sheets: list[tuple[str, str]],
    *,
    shared: tuple[str, ...] = (),
    hidden: tuple[str, ...] = (),
    date1904: bool = False,
    shared_items: tuple[str, ...] = (),
    chart: bool = False,
) -> bytes:
    """A workbook of `sheets` (name, sheet XML) in order, with `shared` strings
    (or `shared_items`, each an `<si>`'s inner XML), and, with `chart`, a chart sheet first."""
    entries = "".join(
        f'<sheet name="{name}" sheetId="{i + 1}" r:id="rId{i + 1}"'
        + (' state="hidden"' if name in hidden else "")
        + "/>"
        for i, (name, _) in enumerate(sheets)
    )
    pr = '<workbookPr date1904="1"/>' if date1904 else ""
    rels = "".join(
        f'<Relationship Id="rId{i + 1}" Type="{_REL}/worksheet" Target="worksheets/sheet{i + 1}.xml"/>'
        for i in range(len(sheets))
    )
    rels += f'<Relationship Id="rIdS" Type="{_REL}/styles" Target="styles.xml"/>'
    rels += f'<Relationship Id="rIdT" Type="{_REL}/sharedStrings" Target="sharedStrings.xml"/>'
    if chart:
        entries = '<sheet name="Chart" sheetId="99" r:id="rIdC"/>' + entries
        rels += f'<Relationship Id="rIdC" Type="{_REL}/chartsheet" Target="chartsheets/c.xml"/>'
    parts = {
        "xl/workbook.xml": f"<workbook {_X} {_R}>{pr}<sheets>{entries}</sheets></workbook>",
        "xl/_rels/workbook.xml.rels": (
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            f"{rels}</Relationships>"
        ),
        "xl/styles.xml": _STYLES,
        "xl/sharedStrings.xml": f"<sst {_X}>"
        + "".join(f"<si>{item}</si>" for item in shared_items or [f"<t>{s}</t>" for s in shared])
        + "</sst>",
        **{f"xl/worksheets/sheet{i + 1}.xml": xml for i, (_, xml) in enumerate(sheets)},
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, xml in parts.items():
            z.writestr(name, xml)
    return buf.getvalue()
