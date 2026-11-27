"""Lifecycle changes use private Git fixtures and fake translators, with no provider calls."""

from __future__ import annotations

import pathlib
import subprocess

import pytest

from scripts.i18n import lifecycle as lc
from scripts.i18n import translate as t

JA = t.LANGUAGES[1]


def git(root: pathlib.Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=root, text=True).strip()


def put(root: pathlib.Path, name: str, text: str) -> None:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


@pytest.fixture
def repo(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "config", "user.name", "test")
    git(tmp_path, "config", "user.email", "test@example.test")
    put(tmp_path, "docs/a.md", "# Alpha\n")
    put(tmp_path, "docs/b.md", "# Beta\n")
    git(tmp_path, "add", "docs")
    git(tmp_path, "commit", "-qm", "English")
    monkeypatch.setattr(t, "ROOT", tmp_path)
    monkeypatch.setenv("LOOM_I18N_MODEL", t.DEFAULT_MODEL)
    return tmp_path


def translated(source: str) -> str:
    text, line = t.prepare(source, JA)
    body = "\n".join(t.english_requests(text))
    for english, spanish in (
        ("Alpha", "Uno"),
        ("Beta", "Dos"),
        ("New", "Nuevo"),
        ("Peer", "Par"),
        ("guide", "guía"),
    ):
        # Anchors preserve the English spelling; translate only heading and link prose.
        body = body.replace("# " + english, "# " + spanish).replace(
            "[" + english + "]", "[" + spanish + "]"
        )
    return t.render(source, JA, body, line, t.DEFAULT_MODEL)


def publish(root: pathlib.Path, source: str) -> None:
    put(root, t.target_path("ja", source), translated(source))


@pytest.mark.parametrize("preferred", ["docs/use-cases/index.md", "docs/index.md"])
def test_subset_retires_its_landing_and_preserves_other_readme_navigation(
    repo: pathlib.Path, monkeypatch: pytest.MonkeyPatch, preferred: str
) -> None:
    put(repo, "README.md", "# Loom\n\n<!-- languages -->\n<!-- /languages -->\n")
    landings = {preferred, "docs/use-cases/index.md"}
    for source in landings:
        put(repo, source, "# Alpha\n")
    git(repo, "add", "README.md", "docs")
    for code in ("ja", "es"):
        lang = next(lg for lg in t.LANGUAGES if lg.code == code)
        for source in ("README.md", *sorted(landings)):
            text, line = t.prepare(source, lang)
            body = "\n".join(t.english_requests(text)).replace("# Alpha", "# Uno")
            put(
                repo,
                t.target_path(code, source),
                t.render(source, lang, body, line, t.DEFAULT_MODEL),
            )
    publish(repo, "docs/a.md")
    publish(repo, "docs/b.md")
    t.write_language_lines()
    assert not t.language_lines_differ()
    spanish = repo / "i18n/es/README.md"
    retained = spanish.read_bytes()
    (repo / preferred).unlink()
    t.write_language_lines(["ja"])
    assert spanish.read_bytes() == retained
    assert not t.language_lines_differ()

    # An unrelated selected page still progresses through the normal fake-provider path.
    put(repo, "docs/extra.md", "# Alpha\n")
    git(repo, "add", "docs/extra.md")
    calls = []
    monkeypatch.setenv("LOOM_I18N_MAX_USD", "5")
    monkeypatch.setattr(t, "bedrock_client", object)

    def translate(source, lang, client, spend):
        calls.append((source, lang.code))
        lc.stage(t.target_path(lang.code, source), translated(source))

    monkeypatch.setattr(t, "translate_page", translate)
    assert t.update(codes=["ja"]) == 0
    assert calls == [("docs/extra.md", "ja")]
    assert not (repo / t.target_path("ja", preferred)).exists()
    assert (repo / t.target_path("es", preferred)).is_file()
    assert spanish.read_bytes() == retained
    assert not t.language_lines_differ()
    assert t.check(skip_stale=True) == 0

    assert lc.retire_obsolete() == (1, 0)
    assert not (repo / t.target_path("es", preferred)).exists()
    assert not t.language_lines_differ()
    assert t.check(skip_stale=True) == 0


def test_retiring_the_last_native_landing_removes_the_readme_site_link(repo: pathlib.Path) -> None:
    from scripts.i18n import site

    put(repo, "README.md", "# Loom\n\n<!-- languages -->\n<!-- /languages -->\n")
    put(repo, "docs/index.md", "# Alpha\n")
    git(repo, "add", "README.md", "docs/index.md")
    for source in ("README.md", "docs/index.md"):
        publish(repo, source)
    t.write_language_lines()
    assert site.SITE in t.language_line("i18n/ja/README.md")
    (repo / "docs/index.md").unlink()
    assert lc.retire_obsolete(["ja"]) == (1, 0)
    assert site.SITE not in (repo / "i18n/ja/README.md").read_text()
    assert not t.language_lines_differ()
    assert t.check(skip_stale=True) == 0


def test_inventory_tracks_nested_generated_additions_but_not_untracked_or_deleted(
    repo: pathlib.Path,
) -> None:
    put(repo, "README.md", "# Loom\n")
    put(repo, "docs/reference/operations/deep.md", "# Nested\n<!-- generated -->\n")
    put(repo, "docs/not tracked.md", "# Draft\n")
    git(repo, "add", "README.md", "docs/reference")
    (repo / "docs/a.md").unlink()
    assert t.sources() == ("README.md", "docs/b.md", "docs/reference/operations/deep.md")
    git(repo, "mv", "docs/b.md", "docs/renamed.md")
    assert "docs/renamed.md" in t.sources() and "docs/b.md" not in t.sources()


def test_edit_invalidates_only_the_changed_source(repo: pathlib.Path) -> None:
    publish(repo, "docs/a.md")
    publish(repo, "docs/b.md")
    assert t.stale((JA,)) == []
    put(repo, "docs/a.md", "# Alpha\n\nChanged.\n")
    assert t.stale((JA,)) == [("docs/a.md", JA)]


def test_links_fall_back_until_the_translated_target_and_anchor_exist(repo: pathlib.Path) -> None:
    text = "[guide](b.md#beta) ![image](asset.svg)"
    assert f"[guide]({t.REPO_URL}/blob/main/docs/b.md#beta)" in t.rewrite_links(
        text, "docs/a.md", "ja"
    )
    publish(repo, "docs/b.md")
    output = t.rewrite_links(text, "docs/a.md", "ja")
    assert "[guide](b.md#beta)" in output
    assert f"![image]({t.REPO_URL}/raw/main/docs/asset.svg)" in output
    assert "blob/main/docs/b.md#missing" in t.rewrite_links(
        "[guide](b.md#missing)", "docs/a.md", "ja"
    )


def test_bounded_batches_progress_without_repeating_completed_pages(
    repo: pathlib.Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = []

    def fake(source: str, lang: t.Language, client: object, spend: t.Spend) -> None:
        calls.append(source)
        lc.stage(t.target_path(lang.code, source), translated(source))

    monkeypatch.setenv("LOOM_I18N_MAX_USD", "1")
    monkeypatch.setattr(t, "estimate_usd", lambda model, chars: 1.0)
    monkeypatch.setattr(t, "bedrock_client", lambda: None)
    monkeypatch.setattr(t, "translate_page", fake)
    assert t.update(codes=["ja"]) == 0
    assert calls == ["docs/a.md"]
    assert t.update(codes=["ja"]) == 0
    assert calls == ["docs/a.md", "docs/b.md"]
    assert t.stale((JA,)) == []
    assert "translated=1 failed=0 deferred=1 obsolete=0" in capsys.readouterr().out


def test_changed_present_pages_precede_new_pages(repo: pathlib.Path) -> None:
    publish(repo, "docs/b.md")
    put(repo, "docs/b.md", "# Beta\n\nEdited.\n")
    selected, _ = t.batch(False, ["ja"], 5)
    assert selected[0] == ("docs/b.md", JA)


def test_default_batches_progress_across_all_five_locales_with_one_shared_cap(
    repo: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, str]] = []

    def fake(source: str, lang: t.Language, client: object, spend: t.Spend) -> None:
        calls.append((source, lang.code))
        text, line = t.prepare(source, lang)
        body = "\n".join(t.english_requests(text))
        body = body.replace("# Alpha", "# Uno").replace("# Beta", "# Dos")
        lc.stage(
            t.target_path(lang.code, source), t.render(source, lang, body, line, t.DEFAULT_MODEL)
        )

    monkeypatch.setenv("LOOM_I18N_MAX_USD", "1")
    monkeypatch.setattr(t, "estimate_usd", lambda model, chars: 1.0)
    monkeypatch.setattr(t, "bedrock_client", lambda: None)
    monkeypatch.setattr(t, "translate_page", fake)
    expected = {(source, code) for source in t.sources() for code in t.ROLLOUT_LANGUAGES}
    for completed in range(1, len(expected) + 1):
        assert t.main(["update"]) == 0
        assert len(calls) == completed
        assert len(set(calls)) == completed
    assert set(calls) == expected
    assert t.stale() == []


def test_checkpoint_waits_for_inbound_anchor_repair_without_retranslation(
    repo: pathlib.Path,
) -> None:
    put(repo, "docs/a.md", "# New\n")
    put(repo, "docs/b.md", "# Peer\n\n[guide](a.md#new)\n")
    put(
        repo,
        "i18n/ja/docs/a.md",
        t.header("docs/a.md", JA, t.DEFAULT_MODEL, "0" * 40) + '<a id="old"></a>\n# Anterior\n',
    )
    put(
        repo,
        "i18n/ja/docs/b.md",
        t.header("docs/b.md", JA, t.DEFAULT_MODEL, "0" * 40)
        + '<a id="peer"></a>\n# Par\n[guía](a.md#old)\n',
    )
    lc.stage("i18n/ja/docs/a.md", translated("docs/a.md"))
    assert lc.activate_ready() == 0
    assert '<a id="old">' in (repo / "i18n/ja/docs/a.md").read_text()
    selected, _ = t.batch(False, ["ja"], 5)
    assert selected == [("docs/b.md", JA)]
    lc.stage("i18n/ja/docs/b.md", translated("docs/b.md"))
    assert lc.activate_ready() == 2
    assert t.stale((JA,)) == [] and t.link_problems() == []
    assert lc.checkpoint_files() == []


def test_delete_waits_for_inbound_dependency_closure_and_removes_cycles(repo: pathlib.Path) -> None:
    publish(repo, "docs/a.md")
    publish(repo, "docs/b.md")
    for path, target in (("a", "b"), ("b", "a")):
        with (repo / f"i18n/ja/docs/{path}.md").open("a") as stream:
            stream.write(f"[otra]({target}.md)\n")
    put(repo, "docs/keep.md", "# Peer\n")
    git(repo, "add", "docs/keep.md")
    publish(repo, "docs/keep.md")
    with (repo / "i18n/ja/docs/keep.md").open("a") as stream:
        stream.write("[guía](a.md)\n")
    (repo / "docs/a.md").unlink()
    (repo / "docs/b.md").unlink()
    assert lc.retire_obsolete() == (0, 2)
    lc.stage("i18n/ja/docs/keep.md", translated("docs/keep.md"))
    assert lc.activate_ready() == 1
    assert lc.retire_obsolete() == (2, 0)
    assert t.link_problems() == []


def test_unknown_output_is_never_deleted_or_overwritten(
    repo: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    put(repo, "i18n/ja/docs/mine.md", "# My notes\n")
    assert lc.retire_obsolete() == (0, 1)
    monkeypatch.setenv("LOOM_I18N_MAX_USD", "0")
    with pytest.raises(SystemExit, match="without generator ownership"):
        t.update()
    assert (repo / "i18n/ja/docs/mine.md").read_text() == "# My notes\n"


def test_rename_retires_the_old_page_after_surviving_links_are_repaired(repo: pathlib.Path) -> None:
    publish(repo, "docs/a.md")
    put(repo, "docs/b.md", "# Beta\n\n[guide](a.md)\n")
    publish(repo, "docs/b.md")
    git(repo, "mv", "docs/a.md", "docs/new.md")
    put(repo, "docs/b.md", "# Beta\n\n[guide](new.md)\n")
    assert lc.retire_obsolete() == (0, 1)
    for source in ("docs/new.md", "docs/b.md"):
        lc.stage(t.target_path("ja", source), translated(source))
    assert lc.activate_ready() == 2
    assert lc.retire_obsolete() == (1, 0)
    assert t.link_problems() == []


def pending_branch(repo: pathlib.Path, path: str, text: str) -> str:
    git(repo, "switch", "-qc", "i18n/update")
    put(repo, path, text)
    git(repo, "add", "i18n")
    git(repo, "commit", "-qm", "Generated")
    sha = git(repo, "rev-parse", "HEAD")
    git(repo, "switch", "-q", "main")
    return sha


def test_pending_branch_data_is_reused_and_fresh_main_wins(repo: pathlib.Path) -> None:
    text = translated("docs/a.md")
    sha = pending_branch(repo, "i18n/ja/docs/a.md", text)
    assert t.import_pending(sha) == 1
    assert (repo / "i18n/ja/docs/a.md").read_text() == text
    fresh = text + "\nTexto revisado.\n"
    put(repo, "i18n/ja/docs/a.md", fresh)
    assert t.import_pending(sha) == 0
    assert (repo / "i18n/ja/docs/a.md").read_text() == fresh


@pytest.mark.parametrize("change", ["stale", "html", "banner", "model", "code"])
def test_invalid_pending_output_is_rejected(repo: pathlib.Path, change: str) -> None:
    text = translated("docs/a.md")
    if change == "html":
        text += '<img src="x" onerror="alert(1)">\n'
    elif change == "banner":
        text = text.replace("Machine-translated", "Translated")
    elif change == "model":
        text = text.replace(t.DEFAULT_MODEL, "unknown-model")
    elif change == "code":
        text += "```sh\nunsafe-command\n```\n"
    sha = pending_branch(repo, "i18n/ja/docs/a.md", text)
    if change == "stale":
        put(repo, "docs/a.md", "# Alpha\n\nEdited.\n")
    assert t.import_pending(sha) == 0
    assert not (repo / "i18n/ja/docs/a.md").exists()


def test_symlink_source_or_output_is_refused(
    repo: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (repo / "docs/a.md").unlink()
    (repo / "docs/a.md").symlink_to("b.md")
    with pytest.raises(SystemExit, match="not a regular"):
        t.sources()
    (repo / "docs/a.md").unlink()
    put(repo, "i18n/ja/docs/a.md", "temporary")
    (repo / "i18n/ja/docs/a.md").unlink()
    (repo / "i18n/ja/docs/a.md").symlink_to(repo / "docs/b.md")
    monkeypatch.setenv("LOOM_I18N_MAX_USD", "0")
    with pytest.raises(SystemExit, match="nonregular"):
        t.update()
    assert (repo / "docs/b.md").read_text() == "# Beta\n"


def test_strict_reports_missing_stale_and_obsolete_while_async_validates_present_files(
    repo: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    publish(repo, "docs/a.md")
    publish(repo, "docs/b.md")
    put(repo, "docs/b.md", "# Beta\n\nEdited.\n")
    (repo / "docs/a.md").unlink()
    put(repo, "docs/new.md", "# New\n")
    git(repo, "add", "docs/new.md")
    assert t.check() == 1
    output = capsys.readouterr().out
    assert "i18n/ja/docs/a.md is outside" in output
    assert "i18n/ja/docs/new.md is missing or stale" in output
    assert "i18n/ja/docs/b.md is missing or stale" in output
    assert t.check(skip_stale=True) == 0
    with (repo / "i18n/ja/docs/a.md").open("a") as stream:
        stream.write("[rota](absent.md)\n")
    assert t.check(skip_stale=True) == 1


def test_reservation_books_all_verifier_and_api_attempts(repo: pathlib.Path) -> None:
    reservation = t.batch_reservation(codes=["ja"], cap=5)
    minimum_outputs = 2 * 2 * 4 * t.MAX_TOKENS * t.PRICES[t.DEFAULT_MODEL][1] / 1e6
    assert reservation > minimum_outputs
    assert t.batch_reservation(codes=["ja"], cap=0) == 0


def test_encoded_source_paths_are_resolved_and_encoded_once(repo: pathlib.Path) -> None:
    put(repo, "docs/space name.md", "# Beta\n")
    git(repo, "add", "docs/space name.md")
    text = "[guide](space%20name.md#beta)"
    fallback = t.rewrite_links(text, "docs/a.md", "ja")
    assert fallback == f"[guide]({t.REPO_URL}/blob/main/docs/space%20name.md#beta)"
    publish(repo, "docs/space name.md")
    localized = t.rewrite_links(text, "docs/a.md", "ja")
    assert localized == text
    assert lc.canonical_links(localized, "i18n/ja/docs/a.md") == fallback


def test_import_refuses_to_overwrite_unowned_published_data(repo: pathlib.Path) -> None:
    text = translated("docs/a.md")
    sha = pending_branch(repo, "i18n/ja/docs/a.md", text)
    put(repo, "i18n/ja/docs/a.md", "# My notes\n")
    with pytest.raises(SystemExit, match="without generator ownership"):
        t.import_pending(sha)
    assert (repo / "i18n/ja/docs/a.md").read_text() == "# My notes\n"


@pytest.mark.parametrize("limit", ["page", "total"])
def test_import_refuses_size_before_reading_blob_contents(
    repo: pathlib.Path, monkeypatch: pytest.MonkeyPatch, limit: str
) -> None:
    text = translated("docs/a.md")
    sha = pending_branch(repo, "i18n/ja/docs/a.md", text)
    monkeypatch.setattr(lc, "MAX_PAGE_BYTES" if limit == "page" else "MAX_PENDING_BYTES", 1)
    run = subprocess.run

    def checked(*args, **kwargs):
        assert args[0][0:3] != ["git", "cat-file", "blob"], "oversized content was loaded"
        return run(*args, **kwargs)

    monkeypatch.setattr(lc.subprocess, "run", checked)
    with pytest.raises(SystemExit, match="size limit"):
        t.import_pending(sha)
    assert not (repo / "i18n/ja/docs/a.md").exists()


def test_deleted_english_body_fallback_repairs_to_owned_target_before_retirement(
    repo: pathlib.Path,
) -> None:
    put(repo, "docs/b.md", "# Beta\n\n[guide](a.md#alpha)\n")
    publish(repo, "docs/b.md")
    old_url = f"{t.REPO_URL}/blob/main/docs/a.md#alpha"
    assert old_url in (repo / "i18n/ja/docs/b.md").read_text()
    publish(repo, "docs/a.md")
    provenance = (repo / "i18n/ja/docs/a.md").read_text()
    git(repo, "mv", "docs/a.md", "docs/new.md")
    put(repo, "docs/b.md", "# Beta\n\n[guide](new.md#alpha)\n")
    assert any("English destination or anchor" in problem for problem in lc.link_problems())
    assert lc.retire_obsolete() == (0, 1)
    assert "[guía](a.md#alpha)" in (repo / "i18n/ja/docs/b.md").read_text()
    assert old_url not in (repo / "i18n/ja/docs/b.md").read_text()
    assert (repo / "i18n/ja/docs/a.md").read_text() == provenance
    assert lc.link_problems() == []
    for source in ("docs/new.md", "docs/b.md"):
        lc.stage(t.target_path("ja", source), translated(source))
    assert lc.activate_ready() == 2
    assert lc.retire_obsolete() == (1, 0)
    assert lc.link_problems() == []


@pytest.mark.parametrize("target", ["missing", "wrong-fragment"])
def test_unrepairable_deleted_english_fallback_refuses_closure_and_retains_target(
    repo: pathlib.Path,
    target: str,
) -> None:
    put(repo, "docs/b.md", "# Beta\n\n[guide](a.md#alpha)\n")
    publish(repo, "docs/b.md")
    if target == "wrong-fragment":
        put(
            repo,
            "i18n/ja/docs/a.md",
            t.header("docs/a.md", JA, t.DEFAULT_MODEL, "0" * 40) + '<a id="other"></a>\n# Otro\n',
        )
    before = (repo / "i18n/ja/docs/b.md").read_text()
    git(repo, "mv", "docs/a.md", "docs/new.md")
    put(repo, "docs/b.md", "# Beta\n\n[guide](new.md#alpha)\n")
    assert lc.retire_obsolete() == (0, int(target == "wrong-fragment"))
    assert (repo / "i18n/ja/docs/b.md").read_text() == before
    assert any("English destination or anchor" in problem for problem in lc.link_problems())
    assert t.check(skip_stale=True) == 1


def test_fallback_validation_checks_english_fragments_but_excludes_banner_and_external_urls(
    repo: pathlib.Path,
) -> None:
    put(repo, "docs/b.md", "# Beta\n\n[guide](a.md#alpha)\n")
    publish(repo, "docs/b.md")
    assert lc.link_problems() == []
    path = repo / "i18n/ja/docs/b.md"
    path.write_text(path.read_text().replace("a.md#alpha", "a.md#missing"))
    assert any("English destination or anchor" in problem for problem in lc.link_problems())
    path.unlink()
    publish(repo, "docs/a.md")
    with (repo / "i18n/ja/docs/a.md").open("a") as stream:
        stream.write("[externo](https://example.test/blob/main/docs/missing.md#missing)\n")
    (repo / "docs/a.md").unlink()
    assert (
        lc.link_problems() == []
    )  # Missing original-source provenance is distinct from body links.
    assert lc.retire_obsolete() == (1, 0)


def test_selected_retirement_preserves_deferred_locale_artifacts_and_cross_locale_dependencies(
    repo: pathlib.Path,
) -> None:
    publish(repo, "docs/a.md")
    es = next(lang for lang in t.LANGUAGES if lang.code == "es")
    preserved = t.header("docs/a.md", es, t.DEFAULT_MODEL, t.blob("docs/a.md")) + "# Uno\n"
    put(repo, "i18n/es/docs/a.md", preserved)
    cross_locale = (
        t.header("docs/b.md", es, t.DEFAULT_MODEL, t.blob("docs/b.md"))
        + "[guía](../../ja/docs/a.md)\n"
    )
    put(repo, "i18n/es/docs/b.md", cross_locale)
    (repo / "docs/a.md").unlink()
    assert lc.retire_obsolete(["ja"]) == (0, 2)
    assert (repo / "i18n/es/docs/a.md").read_text() == preserved
    assert (repo / "i18n/es/docs/b.md").read_text() == cross_locale
    # The retained nonselected inbound edge prevents deleting the Japanese target.
    assert (repo / "i18n/ja/docs/a.md").exists()


def test_selected_fallback_repair_leaves_deferred_body_bytes_untouched(repo: pathlib.Path) -> None:
    put(repo, "docs/b.md", "# Beta\n\n[guide](a.md#alpha)\n")
    publish(repo, "docs/b.md")
    es = next(lang for lang in t.LANGUAGES if lang.code == "es")
    spanish = translated("docs/b.md").replace(
        t.header("docs/b.md", JA, t.DEFAULT_MODEL, t.blob("docs/b.md")),
        t.header("docs/b.md", es, t.DEFAULT_MODEL, t.blob("docs/b.md")),
        1,
    )
    put(repo, "i18n/es/docs/b.md", spanish)
    publish(repo, "docs/a.md")
    put(
        repo,
        "i18n/es/docs/a.md",
        translated("docs/a.md").replace(
            t.header("docs/a.md", JA, t.DEFAULT_MODEL, t.blob("docs/a.md")),
            t.header("docs/a.md", es, t.DEFAULT_MODEL, t.blob("docs/a.md")),
            1,
        ),
    )
    git(repo, "mv", "docs/a.md", "docs/new.md")
    put(repo, "docs/b.md", "# Beta\n\n[guide](new.md#alpha)\n")
    lc.retire_obsolete(["ja"])
    assert "[guía](a.md#alpha)" in (repo / "i18n/ja/docs/b.md").read_text()
    assert (repo / "i18n/es/docs/b.md").read_text() == spanish
    assert (repo / "i18n/es/docs/a.md").exists()


def test_selected_activation_never_deletes_deferred_stale_checkpoint(repo: pathlib.Path) -> None:
    es = next(lang for lang in t.LANGUAGES if lang.code == "es")
    retained = t.header("docs/a.md", es, t.DEFAULT_MODEL, "0" * 40) + "# Conservado\n"
    put(repo, "i18n/.pending/es/docs/a.md", retained)
    assert lc.activate_ready(["ja"]) == 0
    assert (repo / "i18n/.pending/es/docs/a.md").read_text() == retained


def test_import_never_overwrites_existing_nonselected_published_or_checkpoint_bytes(
    repo: pathlib.Path,
) -> None:
    es = next(lang for lang in t.LANGUAGES if lang.code == "es")
    base = translated("docs/a.md").replace(
        t.header("docs/a.md", JA, t.DEFAULT_MODEL, t.blob("docs/a.md")),
        t.header("docs/a.md", es, t.DEFAULT_MODEL, t.blob("docs/a.md")),
        1,
    )
    incoming = base + "\nTexto remoto.\n"
    sha = pending_branch(repo, "i18n/es/docs/a.md", incoming)
    published = base.replace(t.blob("docs/a.md"), "0" * 40)
    checkpoint = base + "\nTexto conservado.\n"
    put(repo, "i18n/es/docs/a.md", published)
    put(repo, "i18n/.pending/es/docs/a.md", checkpoint)
    with pytest.raises(SystemExit, match="pending replacement refused"):
        t.import_pending(sha, ["ja"])
    assert (repo / "i18n/es/docs/a.md").read_text() == published
    assert (repo / "i18n/.pending/es/docs/a.md").read_text() == checkpoint


@pytest.mark.parametrize("source_state", ["stale", "deleted"])
@pytest.mark.parametrize("incoming_path", ["i18n/es/docs/a.md", "i18n/.pending/es/docs/a.md"])
def test_replacing_pending_branch_preserves_remote_only_deferred_stale_or_orphan_bytes(
    repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    source_state: str,
    incoming_path: str,
) -> None:
    es = next(lang for lang in t.LANGUAGES if lang.code == "es")
    retained = (
        t.header("docs/a.md", es, t.DEFAULT_MODEL, t.blob("docs/a.md"))
        + '<a id="alpha"></a>\n# Uno\n\nTexto conservado.\n'
    )
    previous = pending_branch(repo, incoming_path, retained)
    if source_state == "deleted":
        (repo / "docs/a.md").unlink()
    else:
        put(repo, "docs/a.md", "# Alpha\n\nContenido cambiado.\n")
    git(repo, "add", "docs")
    git(repo, "commit", "-qm", "English lifecycle")
    monkeypatch.setenv("LOOM_I18N_MAX_USD", "0")
    monkeypatch.setattr(
        t, "bedrock_client", lambda: pytest.fail("archival data must not regenerate")
    )
    assert t.import_pending(previous, ["ja"]) == 1
    archive = "i18n/.pending/es/docs/a.md"
    assert (repo / archive).read_bytes() == retained.encode()
    assert not (repo / "i18n/es/docs/a.md").exists()
    assert "i18n/es/docs/a.md" not in lc.current_checkpoints()
    assert t.update(codes=["ja"]) == 0
    assert (repo / archive).read_bytes() == retained.encode()
    git(repo, "add", "i18n")
    tree = git(repo, "write-tree")
    replacement = git(
        repo,
        "commit-tree",
        tree,
        "-p",
        git(repo, "rev-parse", "HEAD"),
        "-m",
        "Prepared Japanese batch",
    )
    git(repo, "update-ref", "refs/heads/i18n/update", replacement, previous)
    assert (
        subprocess.check_output(["git", "show", f"i18n/update:{archive}"], cwd=repo)
        == retained.encode()
    )


@pytest.mark.parametrize("damage", ["banner", "model"])
def test_deferred_archival_refuses_unknown_ownership_metadata(
    repo: pathlib.Path,
    damage: str,
) -> None:
    es = next(lang for lang in t.LANGUAGES if lang.code == "es")
    text = t.header("docs/a.md", es, t.DEFAULT_MODEL, "0" * 40) + "# Uno\n"
    text = (
        text.replace("Machine-translated", "Altered")
        if damage == "banner"
        else text.replace(t.DEFAULT_MODEL, "unknown-model")
    )
    sha = pending_branch(repo, "i18n/.pending/es/docs/a.md", text)
    with pytest.raises(SystemExit, match="ownership/banner/model"):
        t.import_pending(sha, ["ja"])
    assert not (repo / "i18n/.pending/es/docs/a.md").exists()


def readme_baseline(repo: pathlib.Path, codes: list[str]) -> None:
    put(repo, "README.md", "# Source\n\n<!-- languages -->\n<!-- /languages -->\n")
    for code in codes:
        lang = next(item for item in t.LANGUAGES if item.code == code)
        put(repo, t.target_path(code, "README.md"), readme_translation("README.md", lang))
    t.write_language_lines()
    for code in codes:
        lang = next(item for item in t.LANGUAGES if item.code == code)
        put(repo, t.target_path(code, "README.md"), readme_translation("README.md", lang))
    put(repo, ".github/workflows/ci.yml", "name: trusted\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "Current language links")


def readme_translation(source: str, lang: t.Language) -> str:
    text, line = t.prepare(source, lang)
    body = "\n".join(t.english_requests(text))
    for english, target in (("Source", "Traducido"), ("Alpha", "Uno"), ("Beta", "Dos")):
        body = body.replace("# " + english, "# " + target)
    return t.render(source, lang, body, line, t.DEFAULT_MODEL)


def fake_readme_generation(
    monkeypatch: pytest.MonkeyPatch, calls: list[tuple[str, str]], *, cap: str = "1"
) -> None:
    def fake(source: str, lang: t.Language, client: object, spend: t.Spend) -> None:
        calls.append((source, lang.code))
        lc.stage(t.target_path(lang.code, source), readme_translation(source, lang))

    monkeypatch.setenv("LOOM_I18N_MAX_USD", cap)
    monkeypatch.setattr(t, "estimate_usd", lambda model, chars: 1.0)
    monkeypatch.setattr(t, "bedrock_client", lambda: None)
    monkeypatch.setattr(t, "translate_page", fake)


@pytest.mark.parametrize("codes", [None, list(t.ROLLOUT_LANGUAGES)])
@pytest.mark.parametrize("japanese_exists", [False, True])
def test_readme_bootstrap_publishes_valid_language_lines(
    repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    codes: list[str] | None,
    japanese_exists: bool,
) -> None:
    from scripts.i18n import automation

    readme_baseline(repo, ["es", "ja"] if japanese_exists else ["es"])
    calls: list[tuple[str, str]] = []
    fake_readme_generation(monkeypatch, calls, cap="0" if japanese_exists else "1")
    assert t.check(skip_stale=True) == 0
    assert t.update(codes=codes) == 0
    assert calls == ([] if japanese_exists else [("README.md", "ja")])
    assert (repo / "i18n/ja/README.md").is_file()
    assert t.check(skip_stale=True) == 0
    assert t.language_lines_differ() == []
    base = git(repo, "rev-parse", "HEAD")
    git(repo, "add", ".")
    candidate = git(repo, "commit-tree", git(repo, "write-tree"), "-p", base, "-m", "Generated")
    automation.validate_changes(base, candidate)


def test_partial_batch_defers_new_readme_without_spending_or_changing_other_locales(
    repo: pathlib.Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    readme_baseline(repo, ["es"])
    spanish = (repo / "i18n/es/README.md").read_bytes()
    calls: list[tuple[str, str]] = []
    fake_readme_generation(monkeypatch, calls)
    assert t.update(codes=["ja"]) == 0
    assert calls == [("docs/a.md", "ja")]
    assert "deferred i18n/ja/README.md" in capsys.readouterr().out
    assert (repo / "i18n/es/README.md").read_bytes() == spanish
    assert not (repo / "i18n/ja/README.md").exists()
    assert t.check(skip_stale=True) == 0
    pending = repo / lc.checkpoint_path(t.target_path("ja", "README.md"))
    lc.stage(t.target_path("ja", "README.md"), readme_translation("README.md", JA))
    retained = pending.read_bytes()
    assert lc.activate_ready(["ja"]) == 0
    assert pending.read_bytes() == retained
    assert (repo / "i18n/es/README.md").read_bytes() == spanish
    assert t.check(skip_stale=True) == 0
    assert lc.activate_ready(list(t.ROLLOUT_LANGUAGES)) == 1
    assert not pending.exists()
    assert (repo / "i18n/ja/README.md").is_file()
    assert t.check(skip_stale=True) == 0


def test_new_readme_waits_for_a_stale_published_readme_instead_of_restamping_it(
    repo: pathlib.Path,
) -> None:
    readme_baseline(repo, ["es"])
    path = repo / "README.md"
    path.write_text(path.read_text().replace("# Source", "# Changed"))
    spanish = (repo / "i18n/es/README.md").read_bytes()
    t.write_language_lines()
    assert (repo / "i18n/es/README.md").read_bytes() == spanish
    text, line = t.prepare("README.md", JA)
    body = "\n".join(t.english_requests(text)).replace("# Changed", "# Nuevo")
    lc.stage(
        t.target_path("ja", "README.md"), t.render("README.md", JA, body, line, t.DEFAULT_MODEL)
    )
    pending = repo / lc.checkpoint_path(t.target_path("ja", "README.md"))
    retained = pending.read_bytes()
    assert lc.activate_ready(list(t.ROLLOUT_LANGUAGES)) == 0
    assert pending.read_bytes() == retained
    assert (repo / "i18n/es/README.md").read_bytes() == spanish
    assert not (repo / "i18n/ja/README.md").exists()


def test_readme_bar_repair_preserves_refresh_proofs_without_a_second_provider_call(
    repo: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.i18n import refresh

    readme_baseline(repo, ["es"])
    calls: list[tuple[str, str]] = []
    fake_readme_generation(monkeypatch, calls)
    assert t.update(every=True, codes=list(t.ROLLOUT_LANGUAGES)) == 0
    assert t.update(codes=list(t.ROLLOUT_LANGUAGES)) == 0
    assert calls == [("README.md", "es"), ("README.md", "ja")]
    job = refresh.read()
    assert job is not None
    for code in ("es", "ja"):
        path = t.target_path(code, "README.md")
        assert refresh.done(path, job)
        assert job["completed"][path]["blob"] == t.blob("README.md")
        assert job["completed"][path]["sha256"] == refresh.digest((repo / path).read_text())
    monkeypatch.setenv("LOOM_I18N_MAX_USD", "0")
    assert t.update(codes=list(t.ROLLOUT_LANGUAGES)) == 0
    assert calls == [("README.md", "es"), ("README.md", "ja")]
    assert t.check(skip_stale=True) == 0


def test_readme_bootstrap_retains_conflicted_refresh_candidate(
    repo: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.i18n import refresh

    readme_baseline(repo, ["es"])
    calls: list[tuple[str, str]] = []
    fake_readme_generation(monkeypatch, calls)
    assert t.update(every=True, codes=list(t.ROLLOUT_LANGUAGES)) == 0
    spanish_path = repo / "i18n/es/README.md"
    generated = spanish_path.read_text()
    spanish_path.write_text(generated + "\nTexto manual.\n")
    manual = spanish_path.read_bytes()
    lc.stage("i18n/es/README.md", generated)
    lc.stage("i18n/ja/README.md", readme_translation("README.md", JA))
    refresh.record("README.md", JA, "")
    proof = refresh.read()
    assert proof is not None and refresh.blocked("i18n/es/README.md", proof)
    pending = {
        name: (repo / name).read_bytes()
        for name in ("i18n/.pending/es/README.md", "i18n/.pending/ja/README.md")
    }
    assert lc.activate_ready(list(t.ROLLOUT_LANGUAGES)) == 0
    assert spanish_path.read_bytes() == manual
    assert not (repo / "i18n/ja/README.md").exists()
    assert all((repo / name).read_bytes() == text for name, text in pending.items())
    assert refresh.read() == proof
    assert calls == [("README.md", "es")]


@pytest.mark.parametrize("every", [False, True])
@pytest.mark.parametrize("english_changed", [False, True])
def test_pending_readme_bootstrap_is_reused_before_its_english_bar_merges(
    repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    every: bool,
    english_changed: bool,
) -> None:
    readme_baseline(repo, ["es"])
    base = git(repo, "rev-parse", "HEAD")
    calls: list[tuple[str, str]] = []
    fake_readme_generation(monkeypatch, calls)
    assert t.update(every=every, codes=list(t.ROLLOUT_LANGUAGES)) == 0
    if every:
        assert t.update(codes=list(t.ROLLOUT_LANGUAGES)) == 0
    japanese = (repo / "i18n/ja/README.md").read_bytes()
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "Pending verified translations")
    candidate = git(repo, "rev-parse", "HEAD")
    git(repo, "switch", "-qc", "unmerged-main", base)
    assert not (repo / "i18n/ja/README.md").exists()
    if english_changed:
        readme = repo / "README.md"
        readme.write_text(readme.read_text().replace("# Source", "# Changed"))
        english = readme.read_bytes()
        spanish = (repo / "i18n/es/README.md").read_bytes()
        assert t.import_pending(candidate, list(t.ROLLOUT_LANGUAGES)) == 0
        assert not (repo / "i18n/ja/README.md").exists()
        assert readme.read_bytes() == english
        assert (repo / "i18n/es/README.md").read_bytes() == spanish
        return
    retained = lc.pending_blobs(candidate)
    called = list(calls)
    with pytest.raises(SystemExit, match="full approved language selection"):
        t.import_pending(candidate, ["ja"])
    assert git(repo, "status", "--porcelain") == ""
    assert lc.pending_blobs(candidate) == retained
    assert calls == called
    assert t.import_pending(candidate, list(t.ROLLOUT_LANGUAGES)) > 0
    assert (repo / "i18n/ja/README.md").read_bytes() == japanese
    assert t.check(skip_stale=True) == 0
    assert t.update(codes=list(t.ROLLOUT_LANGUAGES)) == 0
    assert calls.count(("README.md", "ja")) == 1
    assert calls.count(("README.md", "es")) == int(every)
    assert calls[-1] == ("README.md", "ko")


@pytest.mark.parametrize("change", ["exact", "spelling", "qualifier"])
def test_composed_lifecycle_keeps_source_attested_name_obligations(
    repo: pathlib.Path, change: str
) -> None:
    source = "docs/benchmarks/results.md"
    english = "# MemBench\n\n## Details\n"
    if change == "qualifier":
        english = english.replace("# MemBench", "# MemBench samples")
    put(repo, source, english)
    git(repo, "add", source)
    body = "\n".join(t.english_requests(english)).replace("## Details", "## 詳細")
    if change == "spelling":
        body = body.replace("# MemBench", "# MemBenxh")
    candidate = t.render(source, JA, body, "", t.DEFAULT_MODEL)
    problems = lc.candidate_problems(t.target_path("ja", source), candidate)
    assert bool(problems) == (change != "exact")


@pytest.mark.parametrize("action", ["candidate", "stage", "import"])
@pytest.mark.parametrize("change", ["row", "column"])
def test_composed_stored_table_layout_is_checked_before_reuse(
    repo: pathlib.Path, action: str, change: str
) -> None:
    source = "docs/a.md"
    english = "# Alpha\n\n| Name | Value |\n| --- | --- |\n| First label | First value |\n"
    english += "| Second label | Second value |\n"
    put(repo, source, english)
    body = "\n".join(t.english_requests(english))
    for old, new in (
        ("# Alpha", "# 日本語"),
        ("Name", "名称"),
        ("Value", "値"),
        ("First label", "最初の名前"),
        ("First value", "最初の値"),
        ("Second label", "次の名前"),
        ("Second value", "次の値"),
    ):
        body = body.replace(old, new)
    if change == "row":
        body = body.replace("| 次の名前 | 次の値 |\n", "")
    else:
        body = body.replace("| 名称 | 値 |", "| 名称 | 値 | 追加 |")
        body = body.replace("| --- | --- |", "| --- | --- | --- |")
    path = t.target_path("ja", source)
    candidate = t.render(source, JA, body, "", t.DEFAULT_MODEL)
    if action == "candidate":
        assert any(
            "table row/cell layout" in problem for problem in lc.candidate_problems(path, candidate)
        )
    elif action == "stage":
        with pytest.raises(t.TranslationDiffers, match="table row/cell layout"):
            lc.stage(path, candidate)
        assert not (repo / lc.checkpoint_path(path)).exists()
    else:
        sha = pending_branch(repo, path, candidate)
        assert t.import_pending(sha) == 0
        assert not (repo / path).exists()


def test_composed_lifecycle_stages_native_table_rows_without_changing_body(
    repo: pathlib.Path,
) -> None:
    source = "docs/a.md"
    put(repo, source, "# Alpha\n\n| Name | Value |\n| --- | --- |\n| First label | First value |\n")
    body = "\n".join(t.english_requests((repo / source).read_text()))
    for old, new in (
        ("# Alpha", "# 日本語"),
        ("Name", "名称"),
        ("Value", "値"),
        ("First label", "名前"),
        ("First value", "値"),
    ):
        body = body.replace(old, new)
    path = t.target_path("ja", source)
    candidate = t.render(source, JA, body, "", t.DEFAULT_MODEL)
    assert lc.candidate_problems(path, candidate) == []
    lc.stage(path, candidate)
    assert (repo / lc.checkpoint_path(path)).read_text() == candidate


@pytest.mark.parametrize("repair", [False, True])
def test_fallback_pass_reuses_english_parse_for_different_anchors_only_within_call(
    repo: pathlib.Path, monkeypatch: pytest.MonkeyPatch, repair: bool
) -> None:
    put(repo, "docs/a.md", "# Alpha\n\n## Beta\n")
    links = "\n".join(
        f"[guide]({t.REPO_URL}/blob/main/docs/a.md{fragment})"
        for fragment in ("#alpha", "#b%65ta", "", "#alpha")
    )
    put(
        repo,
        "i18n/ja/docs/b.md",
        t.header("docs/b.md", JA, t.DEFAULT_MODEL, t.blob("docs/b.md")) + links,
    )
    original = t.english_requests
    parsed = []

    def requests(text: str) -> list[str]:
        parsed.append(text)
        return original(text)

    monkeypatch.setattr(t, "english_requests", requests)
    for _ in range(2):
        if repair:
            lc.repair_english_fallbacks()
        else:
            assert lc.link_failures() == []
    assert parsed == ["# Alpha\n\n## Beta\n"] * 2


def test_fallback_pass_reparses_changed_source_and_rejects_removed_anchor(
    repo: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = t.english_requests
    parsed = []

    def requests(text: str) -> list[str]:
        parsed.append(text)
        put(repo, "docs/a.md", "# Changed\n")
        return original(text)

    monkeypatch.setattr(t, "english_requests", requests)
    link = f"[guide]({t.REPO_URL}/blob/main/docs/a.md#alpha)"
    problems = lc.link_failures({"i18n/ja/docs/b.md": link + "\n" + link})
    assert len(problems) == 1
    assert problems[0][1] == "docs/a.md"
    assert parsed == ["# Alpha\n", "# Changed\n"]


def test_fallback_repair_reparses_changed_source_before_retaining_owned_anchor(
    repo: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    publish(repo, "docs/a.md")
    link = f"[guide]({t.REPO_URL}/blob/main/docs/a.md#alpha)"
    path = repo / "i18n/ja/docs/b.md"
    header = t.header("docs/b.md", JA, t.DEFAULT_MODEL, t.blob("docs/b.md"))
    put(repo, "i18n/ja/docs/b.md", header + link + "\n" + link)
    original = t.english_requests
    parsed = []

    def requests(text: str) -> list[str]:
        parsed.append(text)
        put(repo, "docs/a.md", "# Changed\n")
        return original(text)

    monkeypatch.setattr(t, "english_requests", requests)
    lc.repair_english_fallbacks()
    assert path.read_text() == header + link + "\n[guide](a.md#alpha)"
    assert parsed == ["# Alpha\n", "# Changed\n"]


@pytest.mark.parametrize("replacement", ["deleted", "symlink"])
@pytest.mark.parametrize("repair", [False, True])
def test_fallback_pass_rechecks_regular_source_after_first_parse(
    repo: pathlib.Path, monkeypatch: pytest.MonkeyPatch, replacement: str, repair: bool
) -> None:
    publish(repo, "docs/a.md")
    link = f"[guide]({t.REPO_URL}/blob/main/docs/a.md#alpha)"
    header = t.header("docs/b.md", JA, t.DEFAULT_MODEL, t.blob("docs/b.md"))
    path = repo / "i18n/ja/docs/b.md"
    put(repo, "i18n/ja/docs/b.md", header + link + "\n" + link)
    original = t.english_requests
    parsed = []

    def requests(text: str) -> list[str]:
        parsed.append(text)
        (repo / "docs/a.md").unlink()
        if replacement == "symlink":
            (repo / "docs/a.md").symlink_to("b.md")
        return original(text)

    monkeypatch.setattr(t, "english_requests", requests)
    if repair:
        lc.repair_english_fallbacks()
        assert path.read_text() == header + link + "\n[guide](a.md#alpha)"
    else:
        problems = lc.link_failures()
        assert len(problems) == 1
        assert problems[0][1] == "docs/a.md"
    assert parsed == ["# Alpha\n"]


@pytest.mark.parametrize("fragment", ["", "#alpha"])
@pytest.mark.parametrize("repair", [False, True])
def test_fallback_pass_propagates_english_parser_errors_without_fragment_shortcut(
    repo: pathlib.Path, fragment: str, repair: bool
) -> None:
    put(repo, "docs/a.md", "# Alpha\n\n" + "x" * 20_000)
    link = f"[guide]({t.REPO_URL}/blob/main/docs/a.md{fragment})"
    put(
        repo,
        "i18n/ja/docs/b.md",
        t.header("docs/b.md", JA, t.DEFAULT_MODEL, t.blob("docs/b.md")) + link,
    )
    with pytest.raises(t.TranslationDiffers, match="indivisible Markdown unit"):
        if repair:
            lc.repair_english_fallbacks()
        else:
            lc.link_failures()


@pytest.mark.parametrize("anchor", ["", "#beta"])
def test_import_retargets_missing_native_destinations_to_live_english(
    repo: pathlib.Path, anchor: str
) -> None:
    put(repo, "docs/a.md", f"# Alpha\n\n[guide](b.md{anchor})\n")
    text = translated("docs/a.md").replace(
        f"{t.REPO_URL}/blob/main/docs/b.md{anchor}", f"b.md{anchor}"
    )
    path = "i18n/ja/docs/a.md"
    assert lc.candidate_problems(path, text) == []
    sha = pending_branch(repo, path, text)
    assert t.import_pending(sha, ["ja"]) == 1
    expected = text.replace(f"](b.md{anchor})", f"]({t.REPO_URL}/blob/main/docs/b.md{anchor})")
    assert (repo / path).read_text() == expected
    assert lc.link_problems() == []


@pytest.mark.parametrize("target", ["published", "batch"])
def test_import_keeps_available_native_destinations(repo: pathlib.Path, target: str) -> None:
    put(repo, "docs/a.md", "# Alpha\n\n[guide](b.md#beta)\n")
    text = translated("docs/a.md").replace(f"{t.REPO_URL}/blob/main/docs/b.md", "b.md")
    if target == "batch":
        # The target exists only in the same data-only proposal.
        put(repo, "i18n/.pending/ja/docs/b.md", translated("docs/b.md"))
    sha = pending_branch(repo, "i18n/.pending/ja/docs/a.md", text)
    if target == "published":
        publish(repo, "docs/b.md")
    assert t.import_pending(sha, ["ja"]) == (1 if target == "published" else 2)
    assert (repo / "i18n/ja/docs/a.md").read_text() == text
    assert lc.link_problems() == []


@pytest.mark.parametrize("blocked", ["anchor", "deleted", "existing_bad_anchor"])
def test_import_does_not_mask_invalid_destinations(repo: pathlib.Path, blocked: str) -> None:
    anchor = "#missing" if blocked == "anchor" else "#beta"
    put(repo, "docs/a.md", f"# Alpha\n\n[guide](b.md{anchor})\n")
    text = translated("docs/a.md").replace(f"{t.REPO_URL}/blob/main/docs/b.md", "b.md")
    sha = pending_branch(repo, "i18n/ja/docs/a.md", text)
    if blocked == "deleted":
        (repo / "docs/b.md").unlink()
    elif blocked == "existing_bad_anchor":
        put(repo, "i18n/ja/docs/b.md", translated("docs/b.md").replace('id="beta"', 'id="old"'))
    t.import_pending(sha, ["ja"])
    assert not (repo / "i18n/ja/docs/a.md").exists()


def test_import_fallback_preserves_code_labels_and_unselected_locale(repo: pathlib.Path) -> None:
    english = "# Alpha\n\n[guide](b.md)\n\n`[guide](b.md)`\n\n```md\n[guide](b.md)\n```\n"
    put(repo, "docs/a.md", english)
    prepared, line = t.prepare("docs/a.md", JA)
    body = t.in_prose(
        "\n".join(t.english_requests(prepared)),
        lambda prose: prose.replace("# Alpha", "# Uno").replace("[guide]", "[guía]"),
    )
    text = t.render("docs/a.md", JA, body, line, t.DEFAULT_MODEL).replace(
        f"{t.REPO_URL}/blob/main/docs/b.md", "b.md"
    )
    assert lc.candidate_problems("i18n/ja/docs/a.md", text) == []
    sha = pending_branch(repo, "i18n/ja/docs/a.md", text)
    es = next(lang for lang in t.LANGUAGES if lang.code == "es")
    retained = t.header("docs/b.md", es, t.DEFAULT_MODEL, "0" * 40) + "# Retenido\n"
    put(repo, "i18n/.pending/es/docs/b.md", retained)
    assert t.import_pending(sha, ["ja"]) == 1
    after = (repo / "i18n/ja/docs/a.md").read_text()
    assert after == text.replace("[guía](b.md)", f"[guía]({t.REPO_URL}/blob/main/docs/b.md)")
    assert (repo / "i18n/.pending/es/docs/b.md").read_text() == retained


def test_import_does_not_rebase_active_refresh_proofs(repo: pathlib.Path) -> None:
    from scripts.i18n import refresh

    put(repo, "docs/a.md", "# Alpha\n\n[guide](b.md)\n")
    text = translated("docs/a.md").replace(f"{t.REPO_URL}/blob/main/docs/b.md", "b.md")
    sha = pending_branch(repo, "i18n/ja/docs/a.md", text)
    job = refresh.plan(every=True, codes=["ja"])
    assert job
    job["completed"]["i18n/ja/docs/a.md"] = {
        "blob": t.blob("docs/a.md"),
        "sha256": refresh.digest(text),
        "base": "",
    }
    refresh.save(job)
    before = (repo / refresh.PATH).read_bytes()
    assert t.import_pending(sha, ["ja"]) == 1
    assert not (repo / "i18n/ja/docs/a.md").exists()
    assert (repo / "i18n/.pending/ja/docs/a.md").read_text() == text
    assert (repo / refresh.PATH).read_bytes() == before


@pytest.mark.parametrize("route", ["../../es/docs/b.md", "b.md#missing"])
def test_import_keeps_unavailable_cross_locale_or_anchor_links_pending(
    repo: pathlib.Path, route: str
) -> None:
    source_route = "b.md#missing" if "#" in route else "b.md"
    put(repo, "docs/a.md", f"# Alpha\n\n[guide]({source_route})\n")
    text = translated("docs/a.md").replace(f"{t.REPO_URL}/blob/main/docs/{source_route}", route)
    assert lc.candidate_problems("i18n/ja/docs/a.md", text) == []
    sha = pending_branch(repo, "i18n/ja/docs/a.md", text)
    assert t.import_pending(sha, ["ja"]) == 1
    assert not (repo / "i18n/ja/docs/a.md").exists()
    assert (repo / "i18n/.pending/ja/docs/a.md").read_text() == text
