"""Translation requests bound source bytes without breaking Markdown units."""

from collections import Counter
from dataclasses import replace
from types import SimpleNamespace

import pytest

from scripts.i18n import chunks
from scripts.i18n import translate as t
from scripts.i18n.context import Label

MAXIMUM = 16_384


def assert_bounded(text: str) -> list[str]:
    requests = t.english_requests(text)
    assert "\n".join(requests) == t.anchor_headings(text, Counter())
    assert all(len(part.encode()) <= MAXIMUM for part in requests)
    return requests


def test_one_large_section_splits_at_complete_table_rows() -> None:
    header = "| Loom | Loom |\n| --- | --- |\n"
    text = "# Loom\n\n## Loom\n\n" + header
    text += "".join(f"| Loom {i} | `Loom` |\n" for i in range(1800))
    requests = assert_bounded(text)
    assert len(requests) > 2
    assert any(header in part for part in requests)
    assert all(not part.startswith("| --- |") for part in requests)


def test_a_short_multibyte_page_still_obeys_the_byte_bound() -> None:
    text = "# Loom\n\n" + ("메모리" * 400 + "\n\n") * 6
    assert len(text) < t.WHOLE_PAGE_CHARS
    assert len(assert_bounded(text)) > 1


def test_slices_keep_code_html_and_anchor_heading_pairs_together() -> None:
    fence = "```python\nvalue = 12\n\n# Loom\n```"
    inline = "`one\n\ntwo`"
    html = '<details title="a>\n\nb"><summary>Loom</summary>\n\nLoom\n</details>'
    comment = "<!-- Loom\n\nLoom -->"
    blocks = [fence, inline, html, comment, "## Next"]
    text = "# Loom\n\n" + "\n\n".join(("Loom " * 900 + "\n\n" + block) for block in blocks)
    requests = assert_bounded(text)
    assert len(requests) > 1
    for block in blocks[:-1]:
        assert any(block in part for part in requests)
    assert any('<a id="next"></a>\n\n## Next' in part for part in requests)


@pytest.mark.parametrize(
    "code",
    [
        "    <div>\n    content\n",
        "    </div>\n    content\n",
        "    ```python\n    <div>\n",
        "- ```python\n  <div>\n  ```\n",
    ],
    ids=["opening-tag", "closing-tag", "literal-fence", "nested-fence"],
)
def test_html_literals_inside_code_remain_one_request(code):
    requests = assert_bounded("Loom\n\n" * 3000 + code)
    assert any(code.rstrip("\n") in request for request in requests)


@pytest.mark.parametrize("marker", ["-", "1."])
def test_multiline_list_continuation_never_becomes_a_separate_request(marker) -> None:
    item = marker + " Loom " + "x" * 4000 + "\n\n  continued " + "y" * 4000 + "\n"
    text = "# Loom\n\n" + "Loom\n\n" * 1800 + item + "\n- next\n"
    requests = assert_bounded(text)
    assert any(item.rstrip("\n") in part for part in requests)


def test_html_crossing_a_heading_stays_in_one_request() -> None:
    html = "<details>\n\n## Loom\n\nLoom\n</details>\n"
    text = "# Loom\n\n" + "Loom\n\n" * 3000 + html
    requests = assert_bounded(text)
    assert any(t.anchor_headings(html, Counter({"loom": 1})) in part for part in requests)


def test_table_without_outer_pipes_splits_between_complete_rows() -> None:
    header = "Loom | Loom\n--- | ---\n"
    text = header + "Loom | Loom\n" * 2000
    requests = assert_bounded(text)
    assert len(requests) > 1
    assert requests[0].startswith(header)


def test_single_column_table_keeps_header_with_delimiter_at_byte_boundary() -> None:
    header = "| " + "x" * 176 + " |\n| --- |\n"
    text = "Loom\n\n" * 2700 + header
    requests = assert_bounded(text)
    assert any(header in part for part in requests)
    assert all(not part.startswith("| --- |") for part in requests)


@pytest.mark.parametrize(
    "unit",
    [
        "```\n" + "x" * MAXIMUM + "\n```\n",
        "<details>\n" + "x" * MAXIMUM + "\n</details>\n",
        "- Loom " + "x" * 9000 + "\n\n  continued " + "y" * 9000 + "\n",
        "- " + "x" * 9000 + "\n\n\t" + "y" * 9000 + "\n",
        "1. Loom " + "x" * 9000 + "\n\n  continued " + "y" * 9000 + "\n",
        "> 1. Loom " + "x" * 9000 + "\n>\n>   continued " + "y" * 9000 + "\n",
        "!!! note\n\n    1. Loom " + "x" * 9000 + "\n\n      continued " + "y" * 9000 + "\n",
    ],
    ids=["fence", "html", "list", "tab-list", "site-ordered", "quote-ordered", "note-ordered"],
)
def test_indivisible_oversize_input_stops_before_any_provider_call(monkeypatch, unit) -> None:
    calls = []

    def unexpected_call(*args):
        calls.append(args)
        raise AssertionError("indivisible oversized input reached the provider")

    monkeypatch.setattr(t, "prepare", lambda source, lang: (unit, ""))
    monkeypatch.setattr(t, "call", unexpected_call)
    with pytest.raises(t.TranslationDiffers, match="indivisible"):
        t.translate_page("docs/example.md", t.LANGUAGES[0], None, t.Spend(t.DEFAULT_MODEL, 1))
    assert calls == []


def test_request_estimate_and_calls_use_the_same_slices_and_usage(monkeypatch) -> None:
    text = "# Loom\n\n" + "Loom\n\n" * 6000
    calls = []

    def reply(client, model, system, messages):
        source = messages[0]["content"]
        assert len(source.encode()) <= MAXIMUM
        calls.append(source)
        return {
            "stop_reason": "end_turn",
            "content": [{"type": "text", "text": source}],
            "usage": {"input_tokens": 7, "output_tokens": 11},
        }

    monkeypatch.setattr(t, "call", reply)
    monkeypatch.setattr(t, "prepare", lambda source, lang: (text, ""))
    lang = t.LANGUAGES[0]
    spend = t.Spend(t.DEFAULT_MODEL, 1)
    requests = assert_bounded(text)
    result = "\n".join(t.translate_text(part, lang, None, spend) for part in requests)
    assert result == "\n".join(requests)
    assert calls == requests
    assert spend.input_tokens == 7 * len(requests)
    assert spend.output_tokens == 11 * len(requests)
    system = t.SYSTEM.format(language=lang.english, style=lang.style)
    assert t.request_chars("docs/example.md", lang) == sum(len(p) + len(system) for p in calls)


def test_assembled_table_layout_is_verified_before_writing(monkeypatch, tmp_path) -> None:
    source = "# Loom\n\n| Loom | Loom |\n| --- | --- |\n| Loom | Loom |\n"
    monkeypatch.setattr(t, "ROOT", tmp_path)
    monkeypatch.setattr(t, "prepare", lambda path, lang: (source, ""))
    monkeypatch.setattr(t, "blob", lambda path: "0" * 40)
    monkeypatch.setattr(
        t, "translate_text", lambda part, *args, **_kwargs: part + "| Loom | Loom | Loom |\n"
    )
    with pytest.raises(t.TranslationDiffers, match="table"):
        t.translate_page("docs/example.md", t.LANGUAGES[0], None, t.Spend(t.DEFAULT_MODEL, 1))
    assert not (tmp_path / "i18n").exists()


def test_assembled_table_without_outer_pipes_rejects_row_loss(monkeypatch, tmp_path) -> None:
    source = "Loom | Loom\n--- | ---\nLoom | Loom\n"
    monkeypatch.setattr(t, "ROOT", tmp_path)
    monkeypatch.setattr(t, "prepare", lambda path, lang: (source, ""))
    monkeypatch.setattr(t, "blob", lambda path: "0" * 40)
    monkeypatch.setattr(t, "translate_text", lambda *args, **_kwargs: "Loom | Loom\n--- | ---\n")
    with pytest.raises(t.TranslationDiffers, match="table"):
        t.translate_page("docs/example.md", t.LANGUAGES[0], None, t.Spend(t.DEFAULT_MODEL, 1))
    assert not (tmp_path / "i18n").exists()


@pytest.mark.parametrize("delimiter", ["| Loom | Loom |\n", "| --- | --- |\n\n"])
def test_assembled_table_preserves_delimiter_and_membership(monkeypatch, tmp_path, delimiter):
    source = "| Loom | Loom |\n| --- | --- |\n| Loom | Loom |\n"
    reply = "| Loom | Loom |\n" + delimiter + "| Loom | Loom |\n"
    monkeypatch.setattr(t, "ROOT", tmp_path)
    monkeypatch.setattr(t, "prepare", lambda path, lang: (source, ""))
    monkeypatch.setattr(t, "blob", lambda path: "0" * 40)
    monkeypatch.setattr(t, "translate_text", lambda *args, **_kwargs: reply)
    with pytest.raises(t.TranslationDiffers, match="table"):
        t.translate_page("docs/example.md", t.LANGUAGES[0], None, t.Spend(t.DEFAULT_MODEL, 1))
    assert not (tmp_path / "i18n").exists()


@pytest.mark.parametrize("context", ["quote", "list", "indented-code"])
def test_assembled_table_cannot_become_another_block(monkeypatch, tmp_path, context):
    header = "Loom | Loom\n--- | ---\n"
    source = header + "Loom | Loom\n"
    if context == "indented-code":
        reply = "".join("    " + line for line in source.splitlines(keepends=True))
    else:
        reply = header + ("> " if context == "quote" else "- ") + "Loom | Loom\n"
    monkeypatch.setattr(t, "ROOT", tmp_path)
    monkeypatch.setattr(t, "prepare", lambda path, lang: (source, ""))
    monkeypatch.setattr(t, "blob", lambda path: "0" * 40)
    monkeypatch.setattr(t, "translate_text", lambda *args, **_kwargs: reply)
    with pytest.raises(t.TranslationDiffers, match="table"):
        t.translate_page("docs/example.md", t.LANGUAGES[0], None, t.Spend(t.DEFAULT_MODEL, 1))
    assert not (tmp_path / "i18n").exists()


@pytest.mark.parametrize("context", ["quote-row-loss", "raw-pre"])
def test_assembled_tables_preserve_nested_and_raw_html_context(monkeypatch, tmp_path, context):
    table = "| Loom | Loom |\n| --- | --- |\n| Loom | Loom |\n"
    if context == "quote-row-loss":
        source = "".join("> " + line for line in table.splitlines(keepends=True))
        reply = "> | Loom | Loom |\n> | --- | --- |\n"
    else:
        source, reply = table, "<pre>\n" + table + "</pre>\n"
    monkeypatch.setattr(t, "ROOT", tmp_path)
    monkeypatch.setattr(t, "prepare", lambda path, lang: (source, ""))
    monkeypatch.setattr(t, "blob", lambda path: "0" * 40)
    monkeypatch.setattr(t, "translate_text", lambda *args, **_kwargs: reply)
    with pytest.raises(t.TranslationDiffers, match="table"):
        t.translate_page("docs/example.md", t.LANGUAGES[0], None, t.Spend(t.DEFAULT_MODEL, 1))
    assert not (tmp_path / "i18n").exists()


@pytest.mark.parametrize("container", ["list", "admonition"])
def test_assembled_tables_inside_site_containers_reject_row_loss(monkeypatch, tmp_path, container):
    table = "| Loom | Loom |\n| --- | --- |\n| Loom | Loom |\n"
    prefix, indent = ("- Loom\n\n", "  ") if container == "list" else ("!!! note\n\n", "    ")
    source = prefix + "".join(indent + line for line in table.splitlines(keepends=True))
    reply = "\n".join(source.splitlines()[:-1]) + "\n"
    monkeypatch.setattr(t, "ROOT", tmp_path)
    monkeypatch.setattr(t, "prepare", lambda path, lang: (source, ""))
    monkeypatch.setattr(t, "blob", lambda path: "0" * 40)
    monkeypatch.setattr(t, "translate_text", lambda *args, **_kwargs: reply)
    with pytest.raises(t.TranslationDiffers, match="table"):
        t.translate_page("docs/example.md", t.LANGUAGES[0], None, t.Spend(t.DEFAULT_MODEL, 1))
    assert not (tmp_path / "i18n").exists()


@pytest.mark.parametrize("cell", ["header", "body"])
@pytest.mark.parametrize("markup", [False, True])
@pytest.mark.parametrize("container", ["commonmark-list", "admonition"])
def test_assembled_tables_reject_emptying_a_cell(monkeypatch, tmp_path, cell, markup, container):
    value, empty = ("<em>Loom</em>", "<em></em>") if markup else ("Loom", "")
    prefix, indent = (
        ("1. Loom\n\n", "      ") if container == "commonmark-list" else ("!!! note\n\n", "    ")
    )
    row = f"{indent}| Loom | {value} |\n"
    source = prefix + row + indent + "| --- | --- |\n" + row
    lines = source.splitlines(keepends=True)
    index = 2 if cell == "header" else 4
    lines[index] = lines[index].replace(f"| {value} |\n", f"| {empty} |\n")
    monkeypatch.setattr(t, "ROOT", tmp_path)
    monkeypatch.setattr(t, "prepare", lambda path, lang: (source, ""))
    monkeypatch.setattr(t, "blob", lambda path: "0" * 40)
    monkeypatch.setattr(t, "translate_text", lambda *args, **_kwargs: "".join(lines))
    with pytest.raises(t.TranslationDiffers, match="table"):
        t.translate_page("docs/example.md", t.LANGUAGES[0], None, t.Spend(t.DEFAULT_MODEL, 1))
    assert not (tmp_path / "i18n").exists()


@pytest.mark.parametrize("container", ["plain", "quote", "list", "admonition", "commonmark-list"])
@pytest.mark.parametrize("markup", [False, True])
def test_valid_table_reflow_is_written_in_each_supported_container(
    monkeypatch, tmp_path, container, markup
):
    value = "<em>Memory</em>" if markup else "Memory"
    translated = value.replace("Memory", "记忆")
    row = f"| {value} | {value} |\n"
    table = row + "| --- | --- |\n" + row
    prefix, indent = {
        "plain": ("", ""),
        "quote": ("", "> "),
        "list": ("- Loom\n\n", "  "),
        "admonition": ("!!! note\n\n", "    "),
        "commonmark-list": ("1. Loom\n\n", "      "),
    }[container]
    source = prefix + "".join(indent + line for line in table.splitlines(keepends=True))
    source_file = tmp_path / "docs/example.md"
    source_file.parent.mkdir(parents=True)
    source_file.write_text(source)
    monkeypatch.setattr(t, "ROOT", tmp_path)
    monkeypatch.setattr(t, "sources", lambda: ("docs/example.md",))
    monkeypatch.setattr(t, "prepare", lambda path, lang: (source, ""))
    monkeypatch.setattr(t, "blob", lambda path: "0" * 40)
    monkeypatch.setattr(
        t,
        "translate_text",
        lambda part, *args, **_kwargs: part.replace(
            f"| {value} | {value} |", f"{translated} | {translated}"
        ).replace("| --- | --- |", "--- | ---"),
    )
    t.translate_page("docs/example.md", t.LANGUAGES[0], None, t.Spend(t.DEFAULT_MODEL, 1))
    assert (
        f"{translated} | {translated}"
        in (
            tmp_path
            / t.lifecycle.checkpoint_path(t.target_path(t.LANGUAGES[0].code, "docs/example.md"))
        ).read_text()
    )
    assert not (tmp_path / t.target_path(t.LANGUAGES[0].code, "docs/example.md")).exists()


@pytest.mark.parametrize(
    ("text", "protected", "expected"),
    [
        (
            "- one\n  continued\n\noutside\n- two\n",
            [(10, 11), (7, 27), (0, 0), (23, 22)],
            [(0, 27), (27, 33)],
        ),
        (
            "- one\n  continued\n\n> outside\n- two\n",
            [(19, 19), (10, 11), (7, 29)],
            [(0, 19), (29, 35)],
        ),
        (
            "- one\n\noutside\n- two\n",
            [(7, 15)],
            [(0, 7), (15, 21)],
        ),
    ],
)
def test_site_list_boundaries_keep_overlaps_and_exact_starts(text, protected, expected):
    assert chunks._site_lists(text, protected) == expected


def test_site_lists_do_not_revisit_every_interval_for_each_line():
    class Intervals(list):
        visits = 0

        def __iter__(self):
            for interval in super().__iter__():
                self.visits += 1
                yield interval

    protected = Intervals((i * 6 + 1, i * 6 + 2) for i in range(64))
    assert chunks._site_lists("plain\n" * 64, protected) == []
    assert protected.visits <= 4 * len(protected)


def test_table_labels_keep_first_duplicate_offsets_and_unmatched_dialect_rows(monkeypatch):
    def row(text, start=0, end=0):
        return [Label(text, minimum=2, start=start, end=end)]

    github = [
        row("one", 1, 2),
        row("repeat", 3, 4),
        row("unmatched", 5, 6),
        row("repeat", 7, 8),
        row("last", 9, 10),
    ]
    site = [row("repeat"), row("site"), row("one"), row("repeat"), row("repeat")]
    monkeypatch.setattr(chunks, "_github_rows", lambda text: github)
    monkeypatch.setattr(chunks, "_site_tables", lambda text: SimpleNamespace(rows=site))
    assert list(chunks.table_labels("x" * 20).values()) == [
        Label("repeat", minimum=2, start=3, end=4),
        Label("site", minimum=2, end=20),
        Label("one", minimum=2, start=1, end=2),
        Label("repeat", minimum=2, start=7, end=8),
        Label("repeat", minimum=2, end=20),
        Label("unmatched", minimum=2, start=5, end=6),
        Label("last", minimum=2, start=9, end=10),
    ]


def test_table_labels_copy_cells_in_proportion_to_rows(monkeypatch):
    github = [[Label(f"github {i}", start=i, end=i + 1)] for i in range(64)]
    site = [[Label(f"site {i}")] for i in range(64)]
    copies = 0

    def counted_replace(*args, **kwargs):
        nonlocal copies
        copies += 1
        return replace(*args, **kwargs)

    monkeypatch.setattr(chunks, "_github_rows", lambda text: github)
    monkeypatch.setattr(chunks, "_site_tables", lambda text: SimpleNamespace(rows=site))
    monkeypatch.setattr(chunks, "replace", counted_replace)
    labels = chunks.table_labels("example")
    assert list(labels.values()) == [
        *[Label(f"site {i}", end=7) for i in range(64)],
        *[row[0] for row in github],
    ]
    assert copies <= 4 * (len(github) + len(site))
