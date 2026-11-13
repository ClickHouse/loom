"""The translation generator keeps code, links, anchors and numbers, and the committed
translations are stamped and link only to files that exist. Freshness is not checked here:
an English edit must not fail every pull request; `.github/workflows/i18n.yml` refreshes
the translations after it lands."""

from __future__ import annotations

import posixpath
import re
from collections import Counter

import pytest

from scripts.i18n import translate as t

ROOT = t.ROOT


@pytest.mark.parametrize("source", t.SOURCES)
def test_a_page_cuts_into_sections_that_join_back_to_it(source: str) -> None:
    text, _ = t.prepare(source, t.LANGUAGES[0])
    assert "\n".join(t.sections(text)) == text


def test_sections_start_at_level_two_headings_outside_code() -> None:
    page = "# Title\n\nintro\n\n## One\n\n```md\n## not a heading\n```\n\n## Two\n"
    assert [s.split("\n")[0] for s in t.sections(page)] == ["# Title", "## One", "## Two"]


def test_anchors_follow_github_slugs_across_sections() -> None:
    seen: Counter[str] = Counter()
    first = t.anchor_headings("## 1. Choose your `Docker` setup\n\n## Notes", seen)
    second = t.anchor_headings("## Notes", seen)
    assert '<a id="1-choose-your-docker-setup"></a>' in first
    assert '<a id="notes"></a>' in first and '<a id="notes-1"></a>' in second


def test_links_point_at_translations_or_the_english_files() -> None:
    page = (
        "[uc](../use-cases/agent-memory.md#measured) [ops](../operations/deployment.md) "
        "![shot](../images/a.png) [web](https://example.com/x) [here](#local)"
    )
    out = t.rewrite_links(page, "docs/getting-started/quickstart.md", "ja")
    assert "[uc](../use-cases/agent-memory.md#measured)" in out
    assert f"[ops]({t.REPO_URL}/blob/main/docs/operations/deployment.md)" in out
    assert f"![shot]({t.REPO_URL}/raw/main/docs/images/a.png)" in out
    assert "[web](https://example.com/x)" in out and "[here](#local)" in out


def test_a_translation_must_keep_code_links_and_numbers() -> None:
    source = "Run `loom up` on 3 hosts, see [docs](https://x.test/a).\n\n```sh\nloom up\n```\n"
    assert t.differences(source, source.replace("Run", "Lancez")) == []
    assert t.differences(source, source.replace("three", "3")) == []
    broken = {
        "inline code": source.replace("`loom up`", "loom up"),
        "link targets": source.replace("https://x.test/a", "https://x.test/b"),
        "numbers": source.replace("3 hosts", "three hosts"),
        "code block lines": source.replace("```sh\nloom up", "```sh\nloom start"),
    }
    for name, text in broken.items():
        assert any(p.startswith(name) for p in t.differences(source, text)), name


def test_a_code_span_may_wrap_differently() -> None:
    source = "Run `loom up\n--fast` here and `x`.\n"
    assert t.differences(source, "Lancez `loom up --fast` ici et `x`.\n") == []


def test_a_translation_may_write_a_spelled_number_as_digits() -> None:
    assert t.differences("two steps", "2 Schritte") == []


def test_the_cost_cap_stops_the_run() -> None:
    spend = t.Spend("claude-sonnet-5-5", cap_usd=0.01)
    spend.add({"input_tokens": 1000, "output_tokens": 100})
    with pytest.raises(SystemExit, match="LOOM_I18N_MAX_USD"):
        spend.add({"input_tokens": 0, "output_tokens": 1000})


def test_an_unpriced_model_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOOM_I18N_MODEL", "some-model")
    with pytest.raises(SystemExit, match="no price"):
        t.update()


def test_a_cut_off_reply_is_refused() -> None:
    with pytest.raises(SystemExit, match="max_tokens"):
        t.reply_text(
            {"stop_reason": "max_tokens", "content": [{"type": "text", "text": "partial"}]}
        )


def test_the_readme_language_line_is_the_generated_one() -> None:
    assert not t.readme_language_line_differs()


def _translations() -> list[tuple[str, t.Language]]:
    return [
        (s, lang)
        for s in t.SOURCES
        for lang in t.LANGUAGES
        if (ROOT / t.target_path(lang.code, s)).is_file()
    ]


def test_every_translation_exists_with_its_stamp_and_banner() -> None:
    assert len(_translations()) == len(t.SOURCES) * len(t.LANGUAGES)
    for source, lang in _translations():
        text = (ROOT / t.target_path(lang.code, source)).read_text()
        stamp = t.STAMP.match(text)
        assert stamp and stamp.group(1) == source, t.target_path(lang.code, source)
        assert "Machine-translated from the [English page]" in text.split("\n\n", 1)[0]


def test_every_relative_link_in_a_translation_resolves() -> None:
    for source, lang in _translations():
        path = t.target_path(lang.code, source)
        for match in t.LINK.finditer((ROOT / path).read_text()):
            link = match.group(2).split("#", 1)[0]
            if not link or re.match(r"[a-z]+:", link):
                continue
            target = posixpath.normpath(posixpath.join(posixpath.dirname(path), link))
            assert (ROOT / target).is_file(), f"{path} links to {link}"
