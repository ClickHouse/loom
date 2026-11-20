"""Known English Drive transcript exports keep their coarse source time headings."""

from __future__ import annotations

import pytest

from server.knowledge.domain.transcripts import is_drive_transcript_markdown

DOCUMENT = (
    """## **Example meeting transcript**

# **Attendees**

Speaker A, Speaker B

# **Transcript**

### 00:05:00

Speaker A: The first discussion paragraph stays here.

Speaker B: The next paragraph stays separate.

### 01:00:00

Speaker B: A later discussion paragraph stays here.

### Meeting ended after 01:02:03 👋

"""
    "*This editable transcript was computer generated and might contain errors. "
    "People can also change the text after it was created.*\n"
)


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("final_newline", [True, False])
def test_recognizes_timed_speaker_groups_across_the_hour(newline: str, final_newline: bool) -> None:
    text = DOCUMENT.replace("\n", newline)
    if not final_newline:
        text = text.removesuffix(newline)
    assert is_drive_transcript_markdown(text) is True


def test_one_actual_source_marker_is_sufficient() -> None:
    text = DOCUMENT.replace("### 01:00:00\n\n", "")
    assert is_drive_transcript_markdown(text) is True


def test_plain_speech_can_contain_colons_and_exported_escapes() -> None:
    text = DOCUMENT.replace(
        "The first discussion paragraph stays here.",
        r"Decision: keep A \& B and the \$5 example.",
    )
    assert is_drive_transcript_markdown(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "",
        "Speaker A: A paragraph with no time.\n\nSpeaker B: Another paragraph.\n",
        "# Notes\n\n### 00:05:00\n\nSpeaker A: A note.\n\n### 01:00:00\n",
        "Speaker A: ### 00:05:00\n\nSpeaker B: ### 01:00:00\n",
        "```markdown\n" + DOCUMENT + "```\n",
        DOCUMENT.replace("# **Transcript**", "# **Notes**"),
        DOCUMENT.replace("### 00:05:00\n\n", "").replace("### 01:00:00\n\n", ""),
        DOCUMENT.replace("### 00:05:00\n\n", ""),
        DOCUMENT.replace("### 00:05:00", "Speaker A: ### 00:05:00"),
        DOCUMENT.replace("### 00:05:00", "```\n### 00:05:00\n```"),
        DOCUMENT.replace("### 00:05:00", "> ### 00:05:00"),
        DOCUMENT.replace("### 00:05:00", "- ### 00:05:00"),
        DOCUMENT.replace("### 00:05:00", "    ### 00:05:00"),
        DOCUMENT.replace("### 00:05:00", "### 00:05:00 is a note"),
        DOCUMENT.replace("### 00:05:00", "### 00:60:00"),
        DOCUMENT.replace("### 00:05:00", "### 00:05:60"),
        DOCUMENT.replace("### 00:05:00", "### 0:05:00"),
        DOCUMENT.replace("### 00:05:00", "### ٠٠:٠٥:٠٠"),
        DOCUMENT.replace("### 00:05:00", "### 00:05"),
        DOCUMENT.replace("### 01:00:00", "### 00:04:59"),
        DOCUMENT.replace("### 01:00:00", "### 00:05:00"),
        DOCUMENT.replace("Speaker B: A later discussion paragraph stays here.\n\n", ""),
        DOCUMENT.replace("Speaker A: The first discussion paragraph stays here.\n\n", "").replace(
            "Speaker B: The next paragraph stays separate.\n\n", ""
        ),
        DOCUMENT.replace("Speaker A: The first discussion paragraph stays here.", "Plain notes."),
        DOCUMENT.replace("Speaker A: The first discussion paragraph stays here.", "Speaker A: "),
        DOCUMENT.replace(
            "Speaker A: The first discussion paragraph stays here.", "* Speaker A: A list item."
        ),
        DOCUMENT.replace(
            "Speaker A: The first discussion paragraph stays here.", "1. Speaker A: A list item."
        ),
        DOCUMENT.replace(
            "Speaker A: The first discussion paragraph stays here.", "**Speaker A**: Styled name."
        ),
        DOCUMENT.replace(
            "Speaker A: The first discussion paragraph stays here.",
            "[Speaker A](url): Linked name.",
        ),
        DOCUMENT.replace(
            "Speaker A: The first discussion paragraph stays here.", "Speaker A: Wrapped\nspeech."
        ),
        DOCUMENT.replace("# **Attendees**", "## **Attendees**"),
        DOCUMENT.replace("Speaker A, Speaker B", ""),
        DOCUMENT.replace("## **Example meeting transcript**", "## ****"),
        DOCUMENT.replace("### Meeting ended after 01:02:03 👋", "### Ending notes"),
        DOCUMENT.replace(
            "### Meeting ended after 01:02:03 👋", "### Meeting ended after 00:59:59 👋"
        ),
        DOCUMENT.replace("People can also change the text after it was created.", "Edited notice."),
        DOCUMENT + "\nExtra notes.",
    ],
)
def test_unknown_untimed_ambiguous_and_malformed_documents_are_not_classified(text: str) -> None:
    assert is_drive_transcript_markdown(text) is False
