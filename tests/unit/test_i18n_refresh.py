"""Explicit refresh jobs advance through private repositories without provider calls."""

from __future__ import annotations

import json
import pathlib

import pytest

from scripts.i18n import lifecycle as lc
from scripts.i18n import refresh as r
from scripts.i18n import translate as t
from tests.unit.test_i18n_lifecycle import JA, git, publish, put, translated


@pytest.fixture
def repo(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "config", "user.name", "test")
    git(tmp_path, "config", "user.email", "test@example.test")
    monkeypatch.setattr(t, "ROOT", tmp_path)
    monkeypatch.setenv("LOOM_I18N_MODEL", t.DEFAULT_MODEL)
    monkeypatch.setenv("LOOM_I18N_MAX_USD", "1")
    monkeypatch.setattr(t, "estimate_usd", lambda model, chars: 1.0)
    monkeypatch.setattr(t, "bedrock_client", lambda: None)
    for source in ("docs/a.md", "docs/b.md", "docs/c.md"):
        put(tmp_path, source, "# Alpha\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "English")
    for source in t.sources():
        publish(tmp_path, source)
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "Current translations")
    return tmp_path


def translator(calls: list[str]):
    def fake(source: str, lang: t.Language, client: object, spend: t.Spend) -> None:
        calls.append(source)
        lc.stage(t.target_path(lang.code, source), translated(source) + "\nTexto renovado.\n")

    return fake


def test_force_refresh_continues_in_default_batches_without_repeating_fresh_pages(
    repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(t, "translate_page", translator(calls))
    assert t.stale((JA,)) == []
    t.update(every=True, codes=["ja"])
    t.update(codes=["ja"])
    t.update(codes=["ja"])
    assert calls == list(t.sources())
    assert r.read()["complete"]
    t.update(codes=["ja"])
    assert calls == list(t.sources())
    # Materialized pending data is insufficient proof that main contains the receipt.
    assert (repo / r.PATH).exists()
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "Verified refresh")
    monkeypatch.setenv("LOOM_I18N_MAX_USD", "0")
    t.update(codes=["ja"])
    assert not (repo / r.PATH).exists()


def test_unmerged_refresh_receipt_authorizes_only_verified_same_source_replacements(
    repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(t, "translate_page", translator(calls))
    git(repo, "switch", "-qc", "i18n/update")
    t.update(every=True, codes=["ja"])
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "Partial refresh")
    sha = git(repo, "rev-parse", "HEAD")
    git(repo, "switch", "-q", "main")
    assert "Texto renovado" not in (repo / "i18n/ja/docs/a.md").read_text()
    assert t.import_pending(sha) == 1
    assert "Texto renovado" in (repo / "i18n/ja/docs/a.md").read_text()
    assert "Texto renovado" not in (repo / "i18n/ja/docs/b.md").read_text()
    t.update(codes=["ja"])
    t.update(codes=["ja"])
    assert calls == list(t.sources())


def test_failure_retains_verified_progress_and_remaining_refresh_work(
    repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    success = translator(calls)
    monkeypatch.setenv("LOOM_I18N_MAX_USD", "3")

    def failing(source: str, lang: t.Language, client: object, spend: t.Spend) -> None:
        if source == "docs/b.md":
            raise SystemExit("uncertain provider request")
        success(source, lang, client, spend)

    monkeypatch.setattr(t, "translate_page", failing)
    with pytest.raises(SystemExit, match="uncertain"):
        t.update(every=True, codes=["ja"])
    assert set(r.read()["completed"]) == {"i18n/ja/docs/a.md"}
    monkeypatch.setattr(t, "translate_page", success)
    t.update(codes=["ja"])
    assert calls == list(t.sources())


def test_reservation_planning_is_pure_and_zero_allowance_records_only_the_job(
    repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert t.batch_reservation(True, ["ja"], cap=1) > 0
    assert not (repo / r.PATH).exists()
    monkeypatch.setenv("LOOM_I18N_MAX_USD", "0")
    monkeypatch.setattr(
        t, "bedrock_client", lambda: pytest.fail("zero allowance requested provider")
    )
    t.update(every=True, codes=["ja"])
    assert r.read()["completed"] == {}
    assert len(r.work(r.plan())) == 3


def test_prompt_change_invalidates_completion_and_does_not_activate_old_checkpoints(
    repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(t, "translate_page", translator(calls))
    t.update(every=True, codes=["ja"])
    old = translated("docs/b.md")
    lc.stage("i18n/ja/docs/b.md", old)
    monkeypatch.setattr(t, "SYSTEM", t.SYSTEM + "\nUse concise prose.")
    assert r.plan()["completed"] == {}
    assert "i18n/ja/docs/b.md" not in lc.current_checkpoints()
    t.update(codes=["ja"])
    assert calls == ["docs/a.md", "docs/a.md"]


@pytest.mark.parametrize("change", ["owner", "schema", "language", "mapping", "hash"])
def test_malformed_refresh_metadata_is_never_overwritten_or_deleted(
    repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    data = r.plan(True, ["ja"])
    if change == "owner":
        data["owner"] = "personal notes"
    elif change == "schema":
        data["schema"] = 2
    elif change == "language":
        data["languages"] = ["unknown"]
    else:
        path = "i18n/ja/../../private.md" if change == "mapping" else "i18n/ja/docs/a.md"
        data["completed"][path] = {"blob": "0" * 40, "sha256": "invalid", "base": ""}
    original = json.dumps(data)
    put(repo, r.PATH, original)
    monkeypatch.setenv("LOOM_I18N_MAX_USD", "0")
    with pytest.raises(SystemExit, match="ownership"):
        t.update()
    assert (repo / r.PATH).read_text() == original


def test_unknown_json_remains_unowned_output(repo: pathlib.Path) -> None:
    put(repo, "i18n/.pending/personal.json", "{}")
    with pytest.raises(SystemExit, match="ownership"):
        lc.require_owned_outputs()
    assert (repo / "i18n/.pending/personal.json").read_text() == "{}"


def test_selected_batch_does_not_spend_on_other_active_refresh_languages(
    repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    r.save(r.plan(True, ["ja", "es"]))
    calls: list[str] = []
    monkeypatch.setattr(t, "translate_page", translator(calls))
    t.update(codes=["ja"])
    assert calls == ["docs/a.md"]
    assert {lg.code for _, lg in r.work(r.plan())} == {"ja", "es"}
    assert t.batch(False, [], 100) == ([], 0)


def test_import_with_empty_approval_preserves_new_coverage_as_hidden_data(
    repo: pathlib.Path,
) -> None:
    (repo / "i18n/ja/docs/a.md").unlink()
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "Pending coverage")
    git(repo, "switch", "-qc", "i18n/update")
    publish(repo, "docs/a.md")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "Verified output")
    sha = git(repo, "rev-parse", "HEAD")
    git(repo, "switch", "-q", "main")
    assert t.import_pending(sha, []) == 1
    assert not (repo / "i18n/ja/docs/a.md").exists()
    assert (repo / "i18n/.pending/ja/docs/a.md").exists()
    assert lc.activate_ready(["es"]) == 0
    assert lc.activate_ready(["ja"]) == 1


def test_incomplete_receipt_cannot_claim_completion_and_disappear(repo: pathlib.Path) -> None:
    data = r.plan(True, ["ja"])
    data["complete"] = True
    r.save(data)
    r.clear_merged()
    assert (repo / r.PATH).exists()
    lc.activate_ready([])
    assert r.read()["complete"] is False


def test_completed_unmerged_receipt_keeps_force_authority_until_main_incorporates_it(
    repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(t, "translate_page", translator(calls))
    monkeypatch.setenv("LOOM_I18N_MAX_USD", "3")
    git(repo, "switch", "-qc", "i18n/update")
    t.update(every=True, codes=["ja"])
    assert r.read()["complete"]
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "Completed refresh")
    sha = git(repo, "rev-parse", "HEAD")
    git(repo, "switch", "-q", "main")
    assert t.import_pending(sha) == 3
    assert r.read()["complete"]
    t.update(codes=["ja"])
    assert calls == list(t.sources())
    assert (repo / r.PATH).exists()


def test_forged_hash_receipt_is_rejected_before_replacing_current_main(
    repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(t, "translate_page", translator(calls))
    git(repo, "switch", "-qc", "i18n/update")
    t.update(every=True, codes=["ja"])
    data = r.read()
    data["completed"]["i18n/ja/docs/a.md"]["sha256"] = "0" * 64
    r.save(data)
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "Invalid receipt")
    sha = git(repo, "rev-parse", "HEAD")
    git(repo, "switch", "-q", "main")
    before = (repo / "i18n/ja/docs/a.md").read_text()
    with pytest.raises(ValueError, match="matching verified output"):
        t.import_pending(sha)
    assert (repo / "i18n/ja/docs/a.md").read_text() == before
    assert not (repo / r.PATH).exists()


def test_import_preserves_independent_main_refresh_and_defers_conflicting_checkpoint(
    repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(t, "translate_page", translator(calls))
    git(repo, "switch", "-qc", "i18n/update")
    t.update(every=True, codes=["ja"])
    candidate = (repo / "i18n/ja/docs/a.md").read_text()
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "Candidate refresh")
    sha = git(repo, "rev-parse", "HEAD")
    git(repo, "switch", "-q", "main")
    independent = translated("docs/a.md") + "\nTexto independiente.\n"
    put(repo, "i18n/ja/docs/a.md", independent)
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "Independent verified refresh")
    assert t.import_pending(sha) == 1
    assert (repo / "i18n/ja/docs/a.md").read_text() == independent
    assert (repo / "i18n/.pending/ja/docs/a.md").read_text() == candidate
    assert "i18n/ja/docs/a.md" not in lc.current_checkpoints()
    assert not r.done("i18n/ja/docs/a.md", r.plan())
    assert t.check() == 1
    assert "i18n/ja/docs/a.md is missing or stale" in capsys.readouterr().out
    monkeypatch.setenv("LOOM_I18N_MAX_USD", "0")
    monkeypatch.setattr(t, "bedrock_client", lambda: pytest.fail("conflict must not be repaid"))
    t.update(codes=["ja"])
    assert "translated=0 failed=0 deferred=3" in capsys.readouterr().out
    assert (repo / "i18n/ja/docs/a.md").read_text() == independent
    assert (repo / "i18n/.pending/ja/docs/a.md").read_text() == candidate


def test_delayed_checkpoint_activation_checks_its_original_published_base(
    repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(t, "translate_page", translator(calls))
    activate = lc.activate_ready
    monkeypatch.setattr(lc, "activate_ready", lambda codes=None: 0)
    t.update(every=True, codes=["ja"])
    candidate = (repo / "i18n/.pending/ja/docs/a.md").read_text()
    independent = translated("docs/a.md") + "\nTexto independiente.\n"
    put(repo, "i18n/ja/docs/a.md", independent)
    # Removing the delay must not permit a candidate to replace independently changed output.
    monkeypatch.setattr(lc, "activate_ready", activate)
    assert lc.activate_ready(["ja"]) == 0
    assert (repo / "i18n/ja/docs/a.md").read_text() == independent
    assert (repo / "i18n/.pending/ja/docs/a.md").read_text() == candidate


def test_merged_receipt_projection_matches_reservation_and_mutation_after_source_edit(
    repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(t, "translate_page", translator(calls))
    monkeypatch.setenv("LOOM_I18N_MAX_USD", "3")
    t.update(every=True, codes=["ja"])
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "Verified completed refresh")
    put(repo, "docs/a.md", "# Alpha\n\nTexto adicional.\n")
    before = (repo / r.PATH).read_text()
    reserved, deferred = t.batch(True, ["ja"], 3)
    assert [source for source, _ in reserved] == list(t.sources()) and deferred == 0
    assert (repo / r.PATH).read_text() == before
    assert t.batch_reservation(True, ["ja"], cap=3) > 0
    calls.clear()
    t.update(every=True, codes=["ja"])
    assert calls == [source for source, _ in reserved]


def test_generated_refresh_tree_satisfies_repository_ownership_contract(
    repo: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.unit.test_i18n import test_every_output_has_generator_ownership as contract

    monkeypatch.setattr(t, "translate_page", translator([]))
    t.update(every=True, codes=["ja"])
    assert (repo / r.PATH).exists()
    contract()
    put(repo, "i18n/.pending/personal.json", "{}")
    with pytest.raises(AssertionError):
        contract()


def test_invalid_model_refuses_before_mutating_existing_refresh_receipt(
    repo: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    r.save(r.plan(True, ["ja"]))
    before = (repo / r.PATH).read_text()
    monkeypatch.setenv("LOOM_I18N_MODEL", "unpriced-model")
    with pytest.raises(SystemExit, match="no price"):
        t.update(codes=["ja"])
    assert (repo / r.PATH).read_text() == before
