"""A shared photo the speaker describes is a fact, not chit-chat.

On the verified LoCoMo re-ingest, 27 of the 70 gold-evidence turns that
produced no fact were fact-bearing, and most of those shared a photo
("Here's a photo of my horse painting"); the extraction prompt never
mentioned photos. The rule is in the system prompt the extractor sends."""

from __future__ import annotations

from server.memory.app.extract import EXTRACTION_PROMPT_VERSION
from server.memory.domain import extraction_prompts as ep


def test_shared_photo_rule_is_in_the_prompt() -> None:
    text = "\n".join(v for k, v in vars(ep).items() if isinstance(v, str) and k.isupper())
    assert "Shared photos" in text
    assert "with the speaker as" in text and "subject" in text
    assert "using the caption only" in text
    # the rule is in the prompt; the version is pinned
    assert EXTRACTION_PROMPT_VERSION == "extraction-v1"
