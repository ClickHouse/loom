from server.core.ch_writes import dedup_settings


def test_a_callers_dedup_token_is_kept() -> None:
    assert (
        dedup_settings("drawn", {"insert_deduplication_token": "mine"})[
            "insert_deduplication_token"
        ]
        == "mine"
    )
    assert dedup_settings("drawn", None)["insert_deduplication_token"] == "drawn"
