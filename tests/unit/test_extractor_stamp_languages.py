"""A per-language extractor stamp scopes the heal to the
files whose language's modules moved."""

from __future__ import annotations

import pathlib

from server.code.app.index.ingest_pass import _heal_languages, _scope_paths
from server.code.domain.extract.extractor_stamp import (
    EVERY_LANGUAGE,
    EXTRACTOR_MODULES,
    LANGUAGE_FAMILIES,
    MODULE_LANGUAGES,
    compute_language_stamps,
    language_family_of_path,
    language_modules,
    moved_languages,
)


def _tree(tmp_path: pathlib.Path, **contents: str) -> pathlib.Path:
    for m in EXTRACTOR_MODULES:
        (tmp_path / m).write_text(contents.get(m, f"NAME = {m!r}\n"))
    return tmp_path


def test_every_stamped_module_has_a_language_or_shapes_every_file():
    assert set(MODULE_LANGUAGES) <= set(EXTRACTOR_MODULES), "a mapped module that is not stamped"
    for m in EXTRACTOR_MODULES:
        lang = MODULE_LANGUAGES.get(m, EVERY_LANGUAGE)
        assert lang == EVERY_LANGUAGE or lang in LANGUAGE_FAMILIES, (m, lang)
    assert {m for m in EXTRACTOR_MODULES if m.startswith(("lang_python", "py_"))} == {
        m for m, lang in MODULE_LANGUAGES.items() if lang == "python"
    }
    assert "edges_treesitter.py" not in MODULE_LANGUAGES and "treesitter.py" not in MODULE_LANGUAGES


def test_a_python_only_change_moves_only_the_python_stamp(tmp_path):
    grammars = {"tree-sitter": "0.25", "tree-sitter-python": "0.23", "tree-sitter-cpp": "0.23"}
    before = compute_language_stamps(_tree(tmp_path), grammars)
    after = compute_language_stamps(
        _tree(tmp_path, **{"lang_python.py": "NAME = 'changed'\n"}), grammars
    )
    assert moved_languages(before, after) == {"python"}
    shared = compute_language_stamps(
        _tree(tmp_path, **{"edges_treesitter.py": "NAME = 'changed'\n"}), grammars
    )
    assert moved_languages(before, shared) == set(LANGUAGE_FAMILIES), (
        "a shared module moves every family"
    )
    cpp_grammar = compute_language_stamps(_tree(tmp_path), {**grammars, "tree-sitter-cpp": "0.24"})
    assert moved_languages(before, cpp_grammar) == {"cpp"}, "a grammar upgrade moves its own family"
    base_grammar = compute_language_stamps(_tree(tmp_path), {**grammars, "tree-sitter": "0.26"})
    assert moved_languages(before, base_grammar) == set(LANGUAGE_FAMILIES)
    assert "lang_python.py" in language_modules(
        "python"
    ) and "lang_python.py" not in language_modules("cpp")
    assert "edges_treesitter.py" in language_modules("cpp")


def test_a_file_follows_its_extensions_family():
    assert language_family_of_path("server/core/ch.py") == "python"
    assert language_family_of_path("nb/analysis.ipynb") == "python"
    assert language_family_of_path("tests/run.sh") == "shell"
    assert language_family_of_path("web/src/app.tsx") == "typescript"
    assert language_family_of_path("src/IO/Foo.cpp") == "cpp"
    assert language_family_of_path("docs/README.md") == "other"
    assert language_family_of_path("bin/deploy") == "other"


def test_the_heal_scopes_its_paths_to_the_moved_families():
    paths = ["a.py", "b.cpp", "c.sh", "d.md", "e.ts"]
    assert _scope_paths(paths, frozenset({"python"})) == ["a.py"]
    assert _scope_paths(paths, frozenset({"python", "shell"})) == ["a.py", "c.sh"]
    assert _scope_paths(paths, frozenset(LANGUAGE_FAMILIES)) == paths
    current = {lang: f"cur-{lang}" for lang in LANGUAGE_FAMILIES}
    recorded = dict(current, python="old")
    assert _heal_languages({}, recorded, current) == {"python"}
    assert _heal_languages({"languages": ["cpp"]}, recorded, current) == {"cpp"}, (
        "a heal in progress keeps its scope"
    )
    assert _heal_languages({}, {}, current) == set(LANGUAGE_FAMILIES), (
        "no family record: every family"
    )
    assert _heal_languages({}, current, current) == set(LANGUAGE_FAMILIES), (
        "a move no family explains"
    )
