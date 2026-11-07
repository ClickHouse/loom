"""A correction keeps the new truth.

"Forget X, it's Y now" must keep Y: the write step must not drop every fact
whose key a forget request named or whose words its clause matched, since the
extractor's own shapes put Y exactly there. Each end-to-end case below is an
extractor output observed on a real model (bench/forget_switch_eval.py).
"""

from __future__ import annotations

from typing import Any

import pytest

import server.memory.app.handlers as memory_handlers
import server.memory.app.pipeline as mp
from server.memory.app import bulk_set as bulk_set_mod
from server.memory.app import pipeline_state as pipeline_state
from server.memory.domain import extracted_facts as extracted
from server.memory.domain import forget_requests as forget


class _Out:
    tombstoned = True
    version = 1


async def _write(
    monkeypatch: pytest.MonkeyPatch,
    user_turns: list[tuple[str, str]],
    existing: list[dict[str, str]],
    extracted: list[dict[str, Any]],
    fetched: list[list[str]] | None = None,
) -> tuple[dict[str, str], list[str]]:
    written: dict[str, str] = {}
    deleted: list[str] = []

    async def _bulk(inp: Any) -> Any:
        for e in inp.entries:
            written[e.memory_key] = e.value
        return type("O", (), {"written": [], "skipped_blacklisted": [], "stage_ms": {}})()

    async def _delete(inp: Any) -> Any:
        deleted.append(inp.memory_key)
        return _Out()

    async def _fetch(state: Any, requests: Any, *, limit: int = 10) -> list:
        if fetched is not None:
            fetched.append([r["value"] for r in requests])
        return []

    monkeypatch.setattr(bulk_set_mod, "handle_memory_bulk_set", _bulk)
    monkeypatch.setattr(memory_handlers, "handle_memory_delete", _delete)
    monkeypatch.setattr(mp, "_fetch_for_forget_requests", _fetch)
    state = pipeline_state.WorkflowState(
        org="dev",
        namespace="ns",
        agent="a",
        messages=[{"role": r, "content": t} for r, t in user_turns],
        existing_memories=existing,
        extracted_facts=extracted,
    )
    await mp._step_bulk_write(state, {})
    return written, sorted(deleted)


COFFEE = {"memory_key": "user_coffee_preference", "value": "User prefers dark roast coffee."}
EDITOR = {"memory_key": "user_editor", "value": "User prefers Neovim."}
RETRIES = {
    "memory_key": "user_retries_backoff",
    "value": "User decided retries back off exponentially, capped at 30 seconds.",
}
SALARY = {"memory_key": "user_salary", "value": "User earns $120k a year."}


@pytest.mark.asyncio
async def test_a_correct_replacement_under_the_forgotten_key_is_written(monkeypatch) -> None:
    """The extractor is right (one fact, the forgotten key, linked) and the
    request read from the sentence ("the dark roast thing — I drink light
    roast now") matches it: the write keeps it, so the user loses neither."""
    written, deleted = await _write(
        monkeypatch,
        [("user", "Forget the dark roast thing — I drink light roast now.")],
        [COFFEE],
        [
            {
                "key": "user_coffee_preference",
                "value": "User drinks light roast coffee now.",
                "linked_keys": ["user_coffee_preference"],
                "source_message_indices": [0],
            }
        ],
    )
    assert written == {"user_coffee_preference": "User drinks light roast coffee now."}
    assert deleted == ["user_coffee_preference"]  # the old version retired, the new one live


@pytest.mark.asyncio
async def test_a_forget_entry_carrying_the_replacement_is_split(monkeypatch) -> None:
    written, deleted = await _write(
        monkeypatch,
        [("user", "forget that I prefer Neovim, I switched to VS Code")],
        [EDITOR],
        [
            {
                "key": "user_ide",
                "value": "User switched to VS Code from Neovim.",
                "forget": True,
                "linked_keys": ["user_editor"],
                "tags": ["preference", "forget"],
                "source_message_indices": [0],
            }
        ],
    )
    assert written == {"user_ide": "User switched to VS Code from Neovim."}
    assert "user_editor" in deleted


@pytest.mark.asyncio
async def test_a_linked_replacement_retires_the_row_the_request_names(monkeypatch) -> None:
    """No forget entry at all: one fact linked to the 30s row."""
    written, deleted = await _write(
        monkeypatch,
        [("user", "forget the 30s cap, retries now cap at 60s")],
        [RETRIES],
        [
            {
                "key": "user_retries_cap",
                "linked_keys": ["user_retries_backoff"],
                "value": "User decided retries now cap at 60 seconds (was 30 seconds).",
                "source_message_indices": [0],
            }
        ],
    )
    assert written == {
        "user_retries_cap": "User decided retries now cap at 60 seconds (was 30 seconds)."
    }
    assert deleted == ["user_retries_backoff"]


@pytest.mark.asyncio
async def test_a_pure_request_is_never_turned_into_a_fact(monkeypatch) -> None:
    """No replacement clause → nothing to keep, whatever the entry says."""
    written, deleted = await _write(
        monkeypatch,
        [("user", "please forget my salary")],
        [SALARY],
        [
            {
                "key": "user_salary",
                "value": "User no longer wants their salary stored.",
                "forget": True,
                "linked_keys": ["user_salary"],
                "source_message_indices": [0],
            }
        ],
    )
    assert written == {}
    assert deleted == ["user_salary"]


@pytest.mark.asyncio
async def test_restating_the_forgotten_value_in_the_same_turn_is_not_a_replacement(
    monkeypatch,
) -> None:
    written, deleted = await _write(
        monkeypatch,
        [("user", "please forget that I earn $120k, it's private")],
        [SALARY],
        [
            {
                "key": "user_salary",
                "value": "User earns $120k a year.",
                "linked_keys": ["user_salary"],
                "source_message_indices": [0],
            },
            {
                "key": "user_salary_forget",
                "value": "User asked to forget their salary.",
                "forget": True,
                "linked_keys": ["user_salary"],
                "source_message_indices": [0],
            },
        ],
    )
    assert "user_salary" not in written
    assert "user_salary" in deleted


@pytest.mark.asyncio
async def test_a_link_to_an_unrelated_row_retires_nothing(monkeypatch) -> None:
    """ "forget that, I prefer coffee" after "I prefer tea": the extractor
    folded the coffee into a forget entry linked to the dark-roast row."""
    written, deleted = await _write(
        monkeypatch,
        [
            ("user", "I prefer tea in the morning"),
            ("assistant", "Noted."),
            ("user", "forget that, I prefer coffee in the morning"),
        ],
        [COFFEE],
        [
            {
                "key": "user_morning_preference",
                "value": "User prefers coffee in the morning.",
                "forget": True,
                "linked_keys": ["user_coffee_preference"],
                "source_message_indices": [2],
            }
        ],
    )
    assert written == {"user_morning_preference": "User prefers coffee in the morning."}
    assert "user_coffee_preference" not in deleted


@pytest.mark.asyncio
async def test_a_mislinked_request_does_not_retire_an_unrelated_row(monkeypatch) -> None:
    written, deleted = await _write(
        monkeypatch,
        [("user", "stop using emojis in your replies")],
        [EDITOR],
        [
            {
                "key": "user_emoji_forget",
                "value": "User asked the assistant to stop using emojis.",
                "forget": True,
                "linked_keys": ["user_editor"],
                "source_message_indices": [0],
            }
        ],
    )
    assert "user_editor" not in deleted


def test_a_forget_entry_never_supersedes_the_fact_it_follows() -> None:
    facts = [
        {"key": "user_morning_drink", "value": "User prefers coffee in the morning."},
        {"key": "user_morning_drink", "value": "User asked to forget tea.", "forget": True},
    ]
    kept, dropped = extracted.keep_latest_per_key(facts)
    assert kept == facts and dropped == []
    two = [{"key": "k", "value": "a"}, {"key": "k", "value": "b"}]
    assert extracted.keep_latest_per_key(two) == ([two[1]], ["k"])


@pytest.mark.parametrize(
    "clause, head, rest",
    [
        (
            "that I prefer Neovim, I switched to VS Code",
            "that I prefer Neovim",
            "I switched to VS Code",
        ),
        (
            "the dark roast thing — I drink light roast now",
            "the dark roast thing",
            "I drink light roast now",
        ),
        ("Rust; I'm on Haskell", "Rust", "I'm on Haskell"),
        ("that I like red, green and blue", "that I like red, green and blue", ""),
        ("my salary earlier", "my salary earlier", ""),
    ],
)
def test_the_request_clause_ends_where_the_replacement_starts(clause, head, rest) -> None:
    assert forget.split_replacement(clause) == (head, rest)


def test_forget_sentences_mark_the_message_and_what_it_says_instead() -> None:
    msgs = [
        {"role": "user", "content": "I prefer tea."},
        {"role": "assistant", "content": "Please forget that I like jazz."},
        {"role": "user", "content": "forget that, I prefer coffee in the morning"},
        {"role": "user", "content": "please forget my salary"},
        {"role": "user", "content": "if I forget my charger, remind me"},
    ]
    assert forget.forget_sentences(msgs) == {2: "I prefer coffee in the morning", 3: ""}
    # the request list itself is unchanged: a pronoun names nothing to resolve
    assert [r["value"] for r in forget.forget_requests_from_messages(msgs)] == ["my salary"]


def test_a_message_with_two_forget_requests_keeps_its_other_sentences_once() -> None:
    # each request's replacement, then the sentences that are not requests, once
    msgs = [
        {
            "role": "user",
            "content": "forget that I like jazz. I play the drums now. "
            "forget my old address, it's 12 Elm Street now.",
        }
    ]
    assert forget.forget_sentences(msgs) == {0: "it's 12 Elm Street now. I play the drums now."}
    assert forget.forget_sentences(msgs, same_sentence=True) == {0: "it's 12 Elm Street now."}


@pytest.mark.asyncio
async def test_a_request_that_names_the_replacement_keeps_the_users_words(monkeypatch) -> None:
    """Observed: one request-shaped entry holding the replacement and no
    other fact — the new truth is kept in the user's own words."""
    written, deleted = await _write(
        monkeypatch,
        [("user", "don't keep the Neovim preference, I'm on Emacs")],
        [EDITOR],
        [
            {
                "key": "user_preference_neovim",
                "forget": True,
                "linked_keys": ["user_editor"],
                "tags": ["preference", "forget"],
                "source_message_indices": [0],
                "value": "User asked the assistant not to keep their preference for Neovim, "
                "as they are now on Emacs",
            }
        ],
    )
    assert list(written.values()) == ['User said: "I\'m on Emacs"']
    assert "user_editor" in deleted


@pytest.mark.asyncio
async def test_no_verbatim_copy_when_a_fact_already_carries_the_replacement(monkeypatch) -> None:
    written, _ = await _write(
        monkeypatch,
        [("user", "don't keep the Neovim preference, I'm on Emacs")],
        [EDITOR],
        [
            {
                "key": "user_preference_neovim",
                "forget": True,
                "linked_keys": ["user_editor"],
                "source_message_indices": [0],
                "value": "User asked the assistant not to keep their Neovim preference, now on Emacs",
            },
            {
                "key": "user_editor",
                "value": "User uses Emacs.",
                "linked_keys": ["user_editor"],
                "source_message_indices": [0],
            },
        ],
    )
    assert written == {"user_editor": "User uses Emacs."}


@pytest.mark.asyncio
async def test_a_replacement_the_extractor_dropped_is_kept_in_the_users_words(monkeypatch) -> None:
    """Observed (1 run in 3): only a pure request came back — no Emacs anywhere."""
    written, deleted = await _write(
        monkeypatch,
        [("user", "don't keep the Neovim preference, I'm on Emacs")],
        [EDITOR],
        [
            {
                "key": "user_neovim_preference",
                "forget": True,
                "linked_keys": ["user_editor"],
                "value": "User asked the assistant to forget their preference for Neovim.",
                "source_message_indices": [0],
            }
        ],
    )
    assert list(written.values()) == ['User said: "I\'m on Emacs"']
    assert "user_editor" in deleted


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "turn",
    [
        "forget it, let's move on",
        "please forget my salary",
        "forget my salary. Also I got a cat named Miso",
    ],
)
async def test_no_rescue_without_a_first_person_replacement_in_the_sentence(
    monkeypatch, turn
) -> None:
    written, _ = await _write(
        monkeypatch,
        [("user", turn)],
        [SALARY],
        [
            {
                "key": "user_salary_forget",
                "value": "User asked to forget their salary.",
                "forget": True,
                "linked_keys": ["user_salary"],
                "source_message_indices": [0],
            }
        ],
    )
    assert written == {}


@pytest.mark.asyncio
async def test_a_replacement_key_no_row_holds_is_not_reported_forgotten(monkeypatch) -> None:
    _, deleted = await _write(
        monkeypatch,
        [("user", "forget that I prefer Neovim, I switched to VS Code")],
        [EDITOR],
        [
            {
                "key": "user_ide",
                "value": "User switched to VS Code from Neovim.",
                "forget": True,
                "linked_keys": ["user_editor"],
                "source_message_indices": [0],
            }
        ],
    )
    assert deleted == ["user_editor"]


@pytest.mark.asyncio
async def test_a_correction_past_the_forget_cap_keeps_its_replacement(monkeypatch) -> None:
    """Past the requests resolved by content, the extractor's own forget entry
    still retires the old row, and the message's replacement is still written."""
    cap = forget.MAX_FORGET_REQUESTS
    turns = [("user", "forget that I use Vim, I switched to Emacs.")]
    turns += [("user", f"forget jazz{i}.") for i in range(cap)]
    fetched: list[list[str]] = []
    written, deleted = await _write(
        monkeypatch,
        turns,
        [{"memory_key": "user_editor", "value": "User uses Vim."}],
        [
            {
                "key": "user_editor_forget",
                "value": "User asked to forget that they use Vim.",
                "forget": True,
                "source_message_indices": [0],
            },
            {"key": "user_editor", "value": "User uses Emacs.", "source_message_indices": [0]},
        ],
        fetched,
    )
    assert written.get("user_editor") == "User uses Emacs."
    assert "user_editor" in deleted
    # the per-request candidate reads stop at the cap, on the latest requests
    assert len(fetched[0]) == cap and "I use Vim" not in fetched[0]
    # the words of every request, resolved or not, guard what is a replacement
    msgs = [{"role": r, "content": t} for r, t in turns]
    restated = {"key": "user_vim", "value": "use Vim", "source_message_indices": [0]}
    requests = forget.forget_requests_from_messages(msgs)
    _, protected, _ = forget.replacement_facts([restated], requests, [], msgs, keep_said=False)
    assert "user_vim" not in protected
