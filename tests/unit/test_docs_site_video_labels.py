"""Localized video labels use the translated fallback text without changing source files."""

from html.parser import HTMLParser
from types import SimpleNamespace

import pytest

from scripts.docs_site import locales


class _Videos(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.attributes = []
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        if tag == "video":
            self.attributes.append(dict(attrs))


@pytest.mark.parametrize(
    ("language", "caption", "label"),
    [
        (
            "pt-BR",
            "Ask AI respondendo com fontes verificadas (clipe)",
            "Ask AI respondendo com fontes verificadas (clipe)",
        ),
        ("ja", "<em>出典を確認</em>して回答 &amp; 検証", "出典を確認して回答 & 検証"),
        ("ja", "<script>hidden()</script>出典を確認", "出典を確認"),
    ],
)
def test_localized_video_uses_its_translated_fallback(language, caption, label):
    text = (
        '<html lang="en"><video src="clip.webm?a=1&amp;b=2" controls muted\n'
        ' width="100%" title=\'marker aria-label="keep" > 3\' aria-label="English description">'
        f'<a href="clip.webm">{caption}</a></video></html>'
    )
    page = SimpleNamespace(meta={"language": language})
    output = locales.on_post_page(text, page, None)
    before, after = _Videos(text).attributes[0], _Videos(output).attributes[0]
    assert after.pop("aria-label") == label
    before.pop("aria-label")
    assert after == before
    assert f'<a href="clip.webm">{caption}</a></video>' in output


@pytest.mark.parametrize(
    ("language", "body"),
    [
        ("en", '<video aria-label="English"><a>English caption</a></video>'),
        ("ja", '<video aria-label="English"></video>'),
        ("ja", '<video aria-label="English"><a> </a></video>'),
        ("ja", "<video><a>出典を確認</a></video>"),
        ("ja", '<pre><code>&lt;video aria-label="Example"&gt;</code></pre>'),
        ("ja", '<textarea><video aria-label="Example"><a>Literal</a></video></textarea>'),
        ("ja", '<title><video aria-label="Example"><a>Literal</a></video></title>'),
        ("ja", "<script>const example = '<video aria-label=\"Example\">';</script>"),
    ],
)
def test_video_label_projection_leaves_other_content(language, body):
    text = f'<html lang="{language}">{body}</html>'
    assert locales.on_post_page(text, SimpleNamespace(meta={"language": language}), None) == text


def test_each_video_keeps_its_own_fallback_and_escaped_label():
    text = (
        '<html lang="ja"><video aria-label="English"><a>最初 &quot;引用&quot;</a></video>\n'
        '<video aria-label="English"><a>次の<br>&lt;例&gt;</a></video></html>'
    )
    output = locales.on_post_page(text, SimpleNamespace(meta={"language": "ja"}), None)
    assert [attrs["aria-label"] for attrs in _Videos(output).attributes] == [
        '最初 "引用"',
        "次の <例>",
    ]
