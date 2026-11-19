"""Use a translated video's fallback text as its accessible label."""

from __future__ import annotations

import html
from html.parser import HTMLParser
from itertools import accumulate


class _VideoLabels(HTMLParser):
    def __init__(self, content: str):
        super().__init__(convert_charrefs=True)
        self.offsets = list(accumulate([0, *(len(line) + 1 for line in content.split("\n"))]))
        self.video: tuple[int, str, list[tuple[str, str | None]]] | None = None
        self.caption: list[str] = []
        self.link_depth = 0
        self.raw_text: str | None = None
        self.replacements: list[tuple[int, int, str]] = []

    def handle_starttag(self, tag, attrs):
        if self.raw_text is not None:
            return
        if tag in {"textarea", "title", "script", "style"}:
            self.raw_text = tag
            return
        if tag == "video" and self.video is None:
            line, column = self.getpos()
            opening = self.get_starttag_text()
            assert opening is not None
            self.video = (self.offsets[line - 1] + column, opening, attrs)
            self.caption = []
        elif tag == "a" and self.video is not None:
            self.link_depth += 1
        elif tag == "br" and self.link_depth:
            self.caption.append(" ")

    def handle_data(self, data):
        if self.video is not None and self.link_depth and self.raw_text is None:
            self.caption.append(data)

    def handle_endtag(self, tag):
        if self.raw_text is not None:
            if tag == self.raw_text:
                self.raw_text = None
            return
        if tag == "a" and self.link_depth:
            self.link_depth -= 1
        elif tag == "video" and self.video is not None:
            start, opening, attrs = self.video
            label = " ".join("".join(self.caption).split())
            if label and any(name == "aria-label" for name, _value in attrs):
                attributes = []
                for name, value in attrs:
                    if name == "aria-label":
                        value = label
                    attributes.append(
                        name if value is None else f'{name}="{html.escape(value, quote=True)}"'
                    )
                self.replacements.append(
                    (start, start + len(opening), f"<video {' '.join(attributes)}>")
                )
            self.video = None
            self.link_depth = 0


def translated_video_labels(content: str) -> str:
    """Project verified fallback copy without changing protected generated attributes."""
    parser = _VideoLabels(content)
    parser.feed(content)
    parser.close()
    for start, end, opening in reversed(parser.replacements):
        content = content[:start] + opening + content[end:]
    return content
