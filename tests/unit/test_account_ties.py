"""A write that stops an account tying a person's two spellings keeps the old
pair, for the claim cap alone: a token for one spelling stays capped at a grant
revoked under the other."""

from __future__ import annotations

import pytest

from server.identity.domain.account_ties import kept_aliases, owner_moved


@pytest.mark.parametrize(
    ("existing", "owner", "contact", "kept"),
    [
        # released: owner and contact cleared
        ({"owner_email": "Bob", "contact_email": "Bob@X.io"}, "", "", ["bob", "bob@x.io"]),
        # re-registered under another owner: the old handle no longer ties the email
        (
            {"owner_email": "bob", "contact_email": "bob@x.io"},
            "eve",
            "bob@x.io",
            ["bob", "bob@x.io"],
        ),
        # an edit that keeps the pair, case aside, keeps nothing new
        ({"owner_email": "bob", "contact_email": "bob@x.io"}, "Bob", "BOB@x.io", []),
        # not a person's account (no contact): it tied nothing
        ({"owner_email": "team-admin", "contact_email": ""}, "other", "", []),
        # what earlier writes kept stays kept
        (
            {"owner_email": "", "contact_email": "", "released_aliases": ["a", "a@x"]},
            "",
            "",
            ["a", "a@x"],
        ),
    ],
)
def test_the_old_pair_is_kept_when_the_account_stops_tying_it(
    existing: dict[str, object], owner: str, contact: str, kept: list[str]
) -> None:
    assert kept_aliases(existing, owner, contact) == kept


@pytest.mark.parametrize(
    ("existing", "owner", "moved"),
    [
        ({"owner_email": "bob", "contact_email": "bob@x.io"}, "eve", True),
        ({"owner_email": "bob", "contact_email": "bob@x.io"}, "Bob", False),
        ({"owner_email": "bob", "contact_email": "bob@x.io"}, "BOB@x.io", False),  # own email
        # an agent account: no email, its claim code is its password
        ({"owner_email": "agent-x", "contact_email": "", "password_hash": "h"}, "alice", True),
        ({"owner_email": "team-admin", "contact_email": "", "password_hash": ""}, "other", False),
    ],
)
def test_a_login_never_follows_the_account_to_another_owner(
    existing: dict[str, object], owner: str, moved: bool
) -> None:
    assert owner_moved(existing, owner) is moved
