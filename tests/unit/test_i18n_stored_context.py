"""Stored bodies retain source identities without treating metadata as source prose."""

from pathlib import Path

import pytest

from scripts.i18n import translate as t

ROOT = t.ROOT
CODE = "docs/reference/operations/code.md"
HEADER = "| Field | Type | Meaning |\n| --- | --- | --- |\n"


def _code_row(field):
    return next(
        line
        for line in (ROOT / CODE).read_text().splitlines()
        if line.startswith(f"| `{field}` |")
        and "worst branch" in line
        or line.startswith(f"| `{field}` |")
        and "per-branch unknown reasons" in line
    )


def test_actual_causes_explanation_translates_without_changing_its_wire_tokens():
    source = HEADER + _code_row("causes") + "\n"
    output = (
        source.replace("Field", "フィールド")
        .replace("Type", "型")
        .replace("Meaning", "意味")
        .replace("per-branch unknown reasons", "ブランチごとの不明な理由")
    )
    assert t.differences(source, output, source_path=CODE) == []
    assert t.differences(source, output.replace("no_branch_version", "版なし"), source_path=CODE)
    assert t.differences(source, source, source_path=CODE)


def test_actual_verdict_unknown_remains_a_protected_wire_value():
    source = HEADER + _code_row("verdict") + "\n"
    output = source.replace("the worst branch's word", "最も悪いブランチの判定")
    assert t.differences(source, output, source_path=CODE) == []
    assert t.differences(source, output.replace("unknown", "不明"), source_path=CODE)


def _stored_page(monkeypatch, source):
    lang = t.LANGUAGES[1]
    path = ROOT / t.target_path(lang.code, "README.md")
    stored = [t.render("README.md", lang, source, "", t.DEFAULT_MODEL)]
    read_text = Path.read_text
    monkeypatch.setattr(t, "sources", lambda: ("README.md",))
    monkeypatch.setattr(t, "LANGUAGES", (lang,))
    monkeypatch.setattr(t, "prepare", lambda source_path, language: (source, ""))
    monkeypatch.setattr(
        Path,
        "read_text",
        lambda p, *args, **kwargs: stored[0] if p == path else read_text(p, *args, **kwargs),
    )
    return lang, stored


@pytest.mark.parametrize("name", ["README", "README.md"])
def test_real_rendered_self_filename_passes_stored_check_but_mutation_fails(monkeypatch, name):
    target = t.REPO_URL + "/blob/main/README.md"
    source = f"[{name}]({target})\n"
    assert t.differences(source, source, source_path="README.md") == []
    lang, stored = _stored_page(monkeypatch, source)
    assert t.untranslated_pages() == []
    assert t.stale((lang,), t.DEFAULT_MODEL) == []
    changed = source.replace(f"[{name}]", "[名前]")
    stored[0] = t.render("README.md", lang, changed, "", t.DEFAULT_MODEL)
    assert any("protected source strings" in p for p in t.untranslated_pages())


@pytest.mark.parametrize("changed", ["source", "banner", "blob"])
def test_rendered_metadata_tampering_remains_stale(monkeypatch, changed):
    source = f"[README.md]({t.REPO_URL}/blob/main/README.md)\n"
    lang, stored = _stored_page(monkeypatch, source)
    if changed == "source":
        stored[0] = stored[0].replace("source=README.md", "source=other.md", 1)
    elif changed == "banner":
        stored[0] = stored[0].replace("which is authoritative", "different banner", 1)
    else:
        stored[0] = stored[0].replace(t.blob("README.md"), "0" * 40, 1)
    assert t.stale((lang,), t.DEFAULT_MODEL) == [("README.md", lang)]
    if changed in {"source", "banner"}:
        assert any("protected source strings" in p for p in t.untranslated_pages())
    else:
        assert t.untranslated_pages() == []
