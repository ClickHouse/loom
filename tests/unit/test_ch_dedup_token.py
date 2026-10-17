from server.core.ch_writes import dedup_settings


def test_a_callers_dedup_token_is_kept_only_for_the_journal() -> None:
    mine = {"insert_deduplication_token": "mine"}
    journal = dedup_settings("drawn", mine, table="loom.memory_temporal_commits")
    assert journal["insert_deduplication_token"] == "mine"
    assert dedup_settings("drawn", mine, table="loom.tasks")["insert_deduplication_token"] == (
        "drawn"
    )
    assert dedup_settings("drawn", None)["insert_deduplication_token"] == "drawn"
