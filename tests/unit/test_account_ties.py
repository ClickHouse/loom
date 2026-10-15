"""A release keeps the pair an account tied, for the claim cap alone; an owner
change that the account's login would follow is refused."""

from __future__ import annotations

import pytest

from server.identity.domain.account_ties import kept_aliases, owner_moved


@pytest.mark.parametrize(
    ("existing", "kept"),
    [
        ({"owner_email": "Bob", "contact_email": "Bob@X.io"}, ["bob", "bob@x.io"]),
        ({"owner_email": "team-admin", "contact_email": ""}, []),  # not a person's account
        ({"owner_email": "", "contact_email": "", "released_aliases": ["a", "a@x"]}, ["a", "a@x"]),
    ],
)
def test_a_release_keeps_the_pair_the_account_tied(
    existing: dict[str, object], kept: list[str]
) -> None:
    assert kept_aliases(existing) == kept


@pytest.mark.parametrize(
    ("existing", "owner", "moved"),
    [
        ({"owner_email": "bob", "contact_email": "bob@x.io"}, "eve", True),
        ({"owner_email": "bob", "contact_email": "bob@x.io"}, "Bob", False),
        # the handle is who the login signs in as: the account's email is another owner
        ({"owner_email": "bob", "contact_email": "bob@x.io"}, "bob@x.io", True),
        # an agent account: no email, its claim code is its password
        ({"owner_email": "agent-x", "contact_email": "", "password_hash": "h"}, "alice", True),
        ({"owner_email": "team-admin", "contact_email": "", "password_hash": ""}, "other", False),
    ],
)
def test_an_owner_change_is_one_the_login_would_follow(
    existing: dict[str, object], owner: str, moved: bool
) -> None:
    assert owner_moved(existing, owner) is moved
