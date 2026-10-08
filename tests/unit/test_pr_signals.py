from server.code.domain.index.pr_signals import is_revert_pr


def test_revert_titles_and_trailers() -> None:
    assert is_revert_pr('Revert "Merge pull request #12345 from x/y"')
    assert is_revert_pr("revert: speed up parts loading")
    assert is_revert_pr("  Revert of #100")
    assert is_revert_pr("Fix flaky test", "…\n\nThis reverts commit abcdef0123.")
    assert not is_revert_pr("Reverting is not what this does: reverse iterator fix")
    assert not is_revert_pr("Add revert button to UI"), (
        "revert mid-title is a feature, not a revert"
    )
    assert not is_revert_pr("", "")
