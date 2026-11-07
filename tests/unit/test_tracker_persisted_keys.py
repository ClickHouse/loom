"""The keys under which `loom.code_index_state` holds the tracker plane's
state are storage names, separate from the plane's surface names: changing
one would leave every stored fit, binding override, embed cursor and contract
snapshot unread (the duplicate check would answer `uncalibrated`)."""

from __future__ import annotations

from server.tracker.app import binding, dupcheck_fit, embed_index
from server.tracker.app.sources import CONTRACT_SNAPSHOT_KEY, FRESHNESS_SNAPSHOT_KEY


def test_persisted_state_keys_keep_their_stored_names() -> None:
    assert dupcheck_fit.STATE_KEY.startswith("qa_dupcheck_fit")
    assert dupcheck_fit.state_key_for(1) == "qa_dupcheck_fit"
    assert binding.OVERRIDE_KEY == "qa_binding"
    assert binding.BRANCH_OF_KEY == "qa_branch_of"
    assert embed_index.CURSOR_KEY == "qa_embed_cursor"
    assert CONTRACT_SNAPSHOT_KEY == "qa_contract_snapshot"
    assert FRESHNESS_SNAPSHOT_KEY == "qa_freshness_snapshot"
