"""Present-output validation shares discovery and checks each live file at use."""

from __future__ import annotations

import pathlib
import subprocess

import pytest

from scripts.i18n import translate as t

JA = t.LANGUAGES[1]


def git(root: pathlib.Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def publish(root: pathlib.Path, source: str) -> pathlib.Path:
    prepared, line = t.prepare(source, JA)
    body = "\n".join(t.english_requests(prepared))
    for english, japanese in (("A", "一"), ("B", "二"), ("C", "三"), ("Added", "追加")):
        body = body.replace("# " + english + "\n", "# " + japanese + "\n")
    output = root / t.target_path(JA.code, source)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(t.render(source, JA, body, line, t.DEFAULT_MODEL))
    return output


@pytest.fixture
def repo(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    (root / "docs").mkdir()
    for name in ("a", "b"):
        (root / f"docs/{name}.md").write_text(f"# {name.upper()}\n")
    git(root, "add", "docs")
    monkeypatch.setattr(t, "ROOT", root)
    monkeypatch.setenv("LOOM_I18N_MODEL", t.DEFAULT_MODEL)
    return root


@pytest.mark.parametrize("count", [1, 2])
def test_present_outputs_share_one_inventory(
    repo: pathlib.Path, monkeypatch: pytest.MonkeyPatch, count: int
) -> None:
    for name in ("a", "b")[:count]:
        output = publish(repo, f"docs/{name}.md")
    sources = t.sources
    discoveries = []

    def discover() -> tuple[str, ...]:
        discoveries.append(True)
        return sources()

    monkeypatch.setattr(t, "sources", discover)
    assert t.untranslated_pages() == []
    assert len(discoveries) == 1

    output.write_text(output.read_text().replace("# 一", "# A").replace("# 二", "# B"))
    problems = t.untranslated_pages()
    assert problems and all(output.relative_to(repo).as_posix() in p for p in problems)
    assert len(discoveries) == 2


@pytest.mark.parametrize("replacement", ["file", "parent", "missing"])
def test_present_pass_rechecks_source_before_reading(
    repo: pathlib.Path, monkeypatch: pytest.MonkeyPatch, replacement: str
) -> None:
    publish(repo, "docs/a.md")
    source = repo / "docs/a.md"
    outside = repo.parent / f"{repo.name}-outside"
    outside.mkdir()
    (outside / "a.md").write_text(source.read_text())
    sources = t.sources

    def discover() -> tuple[str, ...]:
        inventory = sources()
        if replacement == "parent":
            source.parent.rename(repo / "original-docs")
            source.parent.symlink_to(outside, target_is_directory=True)
        else:
            source.unlink()
            if replacement == "file":
                source.symlink_to(outside / "a.md")
        return inventory

    read_text = pathlib.Path.read_text
    reads = []

    def read(path: pathlib.Path, *args, **kwargs):
        if path == source:
            reads.append(path)
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(t, "sources", discover)
    monkeypatch.setattr(pathlib.Path, "read_text", read)
    with pytest.raises(SystemExit, match="not a regular file"):
        t.untranslated_pages()
    assert reads == []


def test_prepare_reads_changed_source_bytes_with_the_same_membership(repo: pathlib.Path) -> None:
    inventory = frozenset(t.sources())
    assert t.prepare("docs/a.md", JA, inventory=inventory)[0] == "# A\n"
    before = t.blob("docs/a.md")
    (repo / "docs/a.md").write_text("# Changed\n")
    assert t.prepare("docs/a.md", JA, inventory=inventory)[0] == "# Changed\n"
    assert t.blob("docs/a.md") != before


@pytest.mark.parametrize("replacement", ["file", "parent", "missing"])
def test_rewrite_rechecks_used_english_target(repo: pathlib.Path, replacement: str) -> None:
    publish(repo, "docs/b.md")
    inventory = frozenset(t.sources())
    target = repo / "docs/b.md"
    outside = repo.parent / f"{repo.name}-outside"
    outside.mkdir()
    (outside / "b.md").write_text(target.read_text())
    if replacement == "parent":
        target.parent.rename(repo / "original-docs")
        target.parent.symlink_to(outside, target_is_directory=True)
    else:
        target.unlink()
        if replacement == "file":
            target.symlink_to(outside / "b.md")
    with pytest.raises(SystemExit, match="not a regular file"):
        t.rewrite_links("[peer](b.md#b)", "docs/a.md", "ja", inventory=inventory)


@pytest.mark.parametrize("boundary", ["file", "parent"])
def test_present_and_rewrite_reject_output_symlinks(
    repo: pathlib.Path, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    output = publish(repo, "docs/b.md")
    inventory = frozenset(t.sources())
    outside = repo.parent / f"{repo.name}-outside"
    outside.mkdir()
    (outside / "b.md").write_text(output.read_text())
    if boundary == "parent":
        output.parent.rename(output.parent.with_name("original-docs"))
        output.parent.symlink_to(outside, target_is_directory=True)
    else:
        output.unlink()
        output.symlink_to(outside / "b.md")
    read_text = pathlib.Path.read_text
    reads = []

    def read(path: pathlib.Path, *args, **kwargs):
        if path == output:
            reads.append(path)
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "read_text", read)
    rewritten = t.rewrite_links("[peer](b.md#b)", "docs/a.md", "ja", inventory=inventory)
    assert rewritten == f"[peer]({t.REPO_URL}/blob/main/docs/b.md#b)"
    with pytest.raises(SystemExit, match="not a regular generated Markdown page"):
        t.untranslated_pages()
    assert reads == []


@pytest.mark.parametrize("change", ["anchor", "missing", "symlink"])
def test_rewrite_checks_live_target_between_link_occurrences(
    repo: pathlib.Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    output = publish(repo, "docs/b.md")
    inventory = frozenset(t.sources())
    source = repo / "docs/b.md"
    read_text = pathlib.Path.read_text
    reads = []

    def read(path: pathlib.Path, *args, **kwargs):
        text = read_text(path, *args, **kwargs)
        if path == output:
            reads.append(text)
            if len(reads) == 1:
                if change == "anchor":
                    output.write_text(text.replace('<a id="b"></a>', ""))
                else:
                    source.unlink()
                    if change == "symlink":
                        source.symlink_to("a.md")
        return text

    monkeypatch.setattr(pathlib.Path, "read_text", read)
    text = "[first](b.md#b) [second](b.md#b)"
    if change == "anchor":
        assert t.rewrite_links(text, "docs/a.md", "ja", inventory=inventory) == (
            f"[first](b.md#b) [second]({t.REPO_URL}/blob/main/docs/b.md#b)"
        )
        assert len(reads) == 2
    else:
        with pytest.raises(SystemExit, match="not a regular file"):
            t.rewrite_links(text, "docs/a.md", "ja", inventory=inventory)
        assert len(reads) == 1


@pytest.mark.parametrize("entry", ["prepare", "rewrite"])
def test_standalone_calls_discover_added_and_untracked_targets(
    repo: pathlib.Path, monkeypatch: pytest.MonkeyPatch, entry: str
) -> None:
    text = "# A\n\n[peer](c.md#c)"
    (repo / "docs/a.md").write_text(text)
    output = repo / t.target_path("ja", "docs/c.md")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text('<a id="c"></a>')
    sources = t.sources
    discoveries = []

    def discover() -> tuple[str, ...]:
        inventory = sources()
        discoveries.append(inventory)
        return inventory

    def invoke() -> str:
        if entry == "prepare":
            return t.prepare("docs/a.md", JA)[0]
        return t.rewrite_links(text, "docs/a.md", "ja")

    monkeypatch.setattr(t, "sources", discover)
    fallback = f"[peer]({t.REPO_URL}/blob/main/docs/c.md#c)"
    assert fallback in invoke()
    (repo / "docs/c.md").write_text("# C\n")
    git(repo, "add", "docs/c.md")
    assert "[peer](c.md#c)" in invoke()
    git(repo, "rm", "--cached", "docs/c.md")
    assert fallback in invoke()
    assert discoveries == [
        ("docs/a.md", "docs/b.md"),
        ("docs/a.md", "docs/b.md", "docs/c.md"),
        ("docs/a.md", "docs/b.md"),
    ]


def test_explicit_empty_inventory_never_discovers(
    repo: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    publish(repo, "docs/b.md")

    def unexpected() -> tuple[str, ...]:
        pytest.fail("an explicit membership snapshot must not rediscover")

    monkeypatch.setattr(t, "sources", unexpected)
    assert t.prepare("docs/a.md", JA, inventory=frozenset()) == ("# A\n", "")
    assert t.rewrite_links("[peer](b.md#b)", "docs/a.md", "ja", inventory=frozenset()) == (
        f"[peer]({t.REPO_URL}/blob/main/docs/b.md#b)"
    )


def test_prepare_accepts_a_regular_untracked_source(repo: pathlib.Path) -> None:
    (repo / "docs/draft.md").write_text("# Draft\n")
    assert "docs/draft.md" not in t.sources()
    assert t.prepare("docs/draft.md", JA) == ("# Draft\n", "")
    assert t.prepare("docs/draft.md", JA, inventory=frozenset()) == ("# Draft\n", "")


def test_independent_present_passes_discover_and_validate_added_sources(
    repo: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    publish(repo, "docs/a.md")
    sources = t.sources
    discoveries = []

    def discover() -> tuple[str, ...]:
        inventory = sources()
        discoveries.append(inventory)
        return inventory

    monkeypatch.setattr(t, "sources", discover)
    assert t.untranslated_pages() == []
    assert discoveries == [("docs/a.md", "docs/b.md")]
    (repo / "docs/c.md").write_text("# Added\n")
    git(repo, "add", "docs/c.md")
    output = publish(repo, "docs/c.md")
    output.write_text(output.read_text().replace("# 追加", "# Added"))
    discoveries.clear()
    problems = t.untranslated_pages()
    assert problems and all("i18n/ja/docs/c.md" in p for p in problems)
    assert discoveries == [("docs/a.md", "docs/b.md", "docs/c.md")]
    git(repo, "rm", "--cached", "docs/c.md")
    assert t.untranslated_pages() == []
    assert discoveries[-1] == ("docs/a.md", "docs/b.md")
    assert len(discoveries) == 2
