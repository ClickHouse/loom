"""One participant id per process, carried by every ClickHouse client it opens."""

from uuid import UUID, uuid4

from server.core import ch_target
from server.core import provider_process as process


def test_identity_is_stable_within_a_process_and_new_after_fork(monkeypatch):
    first = process.process_identity()
    assert process.process_identity() == first
    monkeypatch.setattr(process.os, "getpid", lambda: -1)
    assert process.process_identity() != first


def test_every_client_names_the_participant_in_its_user_agent():
    tag = process.participant_tag(process.process_identity())
    assert ch_target.connection_kwargs()["client_name"] == tag
    identity = uuid4()
    assert process.participant_tag(str(identity)) == f"loom-participant/{UUID(str(identity)).hex}"
