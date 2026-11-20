"""The existing Drive Markdown pipeline keeps coarse H3 anchors and document text."""

from server.knowledge.domain import convert
from server.knowledge.domain.chunking import chunk_context, chunk_document


def test_drive_transcript_headings_keep_ancestry_order_and_footer_without_turn_times() -> None:
    document = """## **Example meeting transcript**

# **Attendees**

Speaker A, Speaker B

# **Transcript**

### 00:05:00

Speaker A: The first paragraph stays here.

Speaker B: The text ### 00:07:00 stays spoken text.

```text
### 00:15:00
```

### 01:00:00

Speaker A: The next paragraph stays here.

### Meeting ended after 01:02:03 👋

"""
    document += (
        "*This editable transcript was computer generated and might contain errors. "
        "People can also change the text after it was created.*\n"
    )
    markdown = convert.exported_markdown(document)
    assert markdown == document
    chunks = chunk_document(markdown, size=1500, stride=1200)
    assert [chunk.section_path for chunk in chunks] == [
        ("Attendees",),
        ("Transcript", "00:05:00"),
        ("Transcript", "01:00:00"),
        ("Transcript", "Meeting ended after 01:02:03 👋"),
    ]
    assert [chunk.text for chunk in chunks[:3]] == [
        "Speaker A, Speaker B",
        "Speaker A: The first paragraph stays here.\n\n"
        "Speaker B: The text ### 00:07:00 stays spoken text.\n\n"
        "```text\n### 00:15:00\n```",
        "Speaker A: The next paragraph stays here.",
    ]
    assert chunks[-1].text == (
        "*This editable transcript was computer generated and might contain errors. "
        "People can also change the text after it was created.*"
    )
    assert [chunk.idx for chunk in chunks] == list(range(len(chunks)))
    for chunk in chunks:
        start = chunk.char_start
        assert markdown[start : start + len(chunk.text)] == chunk.text
    for chunk, stamp in zip(chunks[1:3], ("00:05:00", "01:00:00"), strict=True):
        assert chunk.meta["section_path"] == ["Transcript", stamp]
        assert chunk.meta["section_depth"] == 3
        assert chunk_context("Example meeting transcript", "", chunk.breadcrumb) == (
            f"Example meeting transcript\nTranscript > {stamp}"
        )
