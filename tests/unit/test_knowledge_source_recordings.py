"""A `recording` source: its config, the transcript conversion, the Zoom
client and its sync turn, over the in-memory registry and documents of
test_knowledge_source_sync and meeting stores answered by a mock transport. Every
response is written here; none is a recording of real content."""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import Callable
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import unquote

import httpx
import pytest

from server.clients import recording
from server.core.errors import BadConfigError
from server.core.secrets_at_rest import seal
from server.knowledge.app import source_probes, source_sync, source_sync_recording
from server.knowledge.domain import sources as domain
from server.knowledge.domain.sources import SYNC_WORKER_ACTOR, KnowledgeSource
from server.knowledge.domain.transcripts import Entry, parse_vtt, transcript_markdown
from tests.unit.test_knowledge_source_sync import NS, ORG, _detail, _World, world  # noqa: F401

MEET_KEY = json.dumps(
    {"type": "authorized_user", "client_id": "c", "client_secret": "s", "refresh_token": "r"}
)
ZOOM_KEY = json.dumps({"account_id": "acc", "client_id": "zc", "client_secret": "zs"})
ZOOM_SCOPES = (
    "cloud_recording:read:list_recording_files:admin meeting:read:list_past_instances:admin"
)
SPACE = "jQCFfuBOdN5z"
MEETING = "85746065432"

VTT = """WEBVTT

1
00:00:01.000 --> 00:00:04.500
Ana Lima: We keep the hash join.

2
00:00:05.000 --> 00:00:09.000
Ana Lima: The planner pass moves later.

3
00:06:10.000 --> 00:06:15.000
Ben Ode: # not a heading, a hash sign
"""


class _Stores:
    """Zoom answers; no retired provider has a transport route."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.instances: list[dict[str, Any]] = []
        self.recordings: dict[str, dict[str, Any] | None] = {}
        self.meeting_status: dict[str, int] = {}
        self.recording_status: dict[str, int] = {}
        self.vtt = VTT
        self.on_download: Callable[[], None] | None = None
        self.token_status = 200
        self.scopes: dict[str, str | None] = {"zoom.us": ZOOM_SCOPES}

    def answer(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        host, path = request.url.host, request.url.path
        if host == "zoom.us" and path.endswith("/token"):
            if self.token_status != 200:
                return httpx.Response(self.token_status, json={"error": "invalid_grant"})
            answer: dict[str, Any] = {"access_token": "tok"}
            if self.scopes[host] is not None:
                answer["scope"] = self.scopes[host]
            return httpx.Response(200, json=answer)
        if host == "api.zoom.us":
            return self._zoom(path.removeprefix("/v2/"), request)
        if host == "files.zoom.us":
            if self.on_download is not None:
                self.on_download()
            return httpx.Response(200, text=self.vtt)
        return httpx.Response(404)

    def _zoom(self, path: str, request: httpx.Request) -> httpx.Response:
        if path.startswith("past_meetings/") and path.endswith("/instances"):
            meeting = path.split("/")[1]
            if (status := self.meeting_status.get(meeting, 200)) != 200:
                return httpx.Response(status)
            return httpx.Response(200, json={"meetings": self.instances})
        if path.startswith("meetings/") and path.endswith("/recordings"):
            uuid = unquote(unquote(request.url.raw_path.decode().split("/")[3]))
            if (status := self.recording_status.get(uuid, 200)) != 200:
                return httpx.Response(status)
            found = self.recordings.get(uuid)
            if found is None:
                return httpx.Response(404, json={"code": 3301})
            return httpx.Response(200, json=found)
        return httpx.Response(404)


def _zoom_recording(**file: Any) -> dict[str, Any]:
    transcript = {
        "id": "f1",
        "file_type": "TRANSCRIPT",
        "file_extension": "VTT",
        "status": "completed",
        "download_url": "https://files.zoom.us/rec/download/abc",
        **file,
    }
    return {
        "share_url": "https://zoom.us/rec/share/x",
        "recording_files": [
            {
                "id": "m1",
                "file_type": "MP4",
                "download_url": "https://files.zoom.us/rec/download/mp4",
            },
            transcript,
        ],
    }


@pytest.fixture
def stores(world: _World, monkeypatch: pytest.MonkeyPatch) -> _Stores:  # noqa: F811
    s = _Stores()
    client = httpx.AsyncClient(transport=httpx.MockTransport(s.answer))
    monkeypatch.setattr(recording, "get_client", lambda: client)
    return s


def _source(provider: str, ids: list[str], key: str) -> KnowledgeSource:
    return KnowledgeSource(
        org=ORG,
        namespace=NS,
        name="calls",
        kind="recording",
        source_id="id-calls",
        config={"provider": provider, "ids": ids},
        credential_enc=seal("knowledge_sources.credential", key),
    )


# config


def test_a_recording_config_keeps_zoom_ids_and_refuses_the_rest() -> None:
    assert domain.parse_config(
        "recording", {"provider": "zoom", "ids": [85746065432, MEETING]}
    ) == {
        "provider": "zoom",
        "ids": [MEETING],
    }
    for bad in (
        {"provider": "meet", "ids": ["spaces/ab-C_1"]},
        {"provider": "meet", "ids": ["ab-C_1"]},
        {"provider": "other", "ids": ["x"]},
        {"provider": "zoom", "ids": ["abc"]},
        {"provider": "zoom", "ids": [True]},
        {"provider": "zoom", "ids": []},
        {"provider": "zoom", "ids": [str(100000000 + i) for i in range(21)]},
        {"provider": "zoom", "ids": [MEETING], "host": "h"},
    ):
        with pytest.raises(BadConfigError):
            domain.parse_config("recording", bad)


def _ago(hours: float) -> str:
    """A store time `hours` before now, so a recording is recent or settled whatever
    day the test runs."""
    return (datetime.now(UTC) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


# the conversion


def test_a_vtt_file_becomes_timed_sections_with_its_speakers() -> None:
    entries = parse_vtt(VTT)
    assert entries[0] == Entry(1.0, 4.5, "Ana Lima", "We keep the hash join.")
    text = transcript_markdown(entries)
    assert text.split("\n\n") == [
        "# 00:01–00:09: We keep the hash join.",
        "Ana Lima: We keep the hash join. The planner pass moves later.",
        "# 06:10–06:15: # not a heading, a hash sign",
        "Ben Ode: # not a heading, a hash sign",
    ]


def test_a_voice_span_names_the_speaker_and_a_line_never_reads_as_structure() -> None:
    vtt = (
        "WEBVTT\n\n01:02:03.000 --> 01:02:05.000\n<v Ana>Agreed.</v>\n\n"
        "01:02:06.000 --> 01:02:07.000\n&lt;div&gt; stays text\n\n"
        "01:02:08.000 --> 01:02:09.000\n<v Ana>Yes.</v>\n\n"
        "01:02:10.000 --> 01:02:11.000\n# no speaker\n"
    )
    text = transcript_markdown(parse_vtt(vtt))
    assert text.split("\n\n") == [
        "# 62:03–62:11: Agreed.",
        "Ana: Agreed.",
        "\\<div> stays text",
        "Ana: Yes.",
        "\\# no speaker",
    ]
    assert transcript_markdown([]) == ""


def test_a_vtt_file_s_bom_entities_bad_timings_and_run_on_cues_are_read() -> None:
    vtt = (
        "﻿WEBVTT\n\n"
        "1\n00:00:01.000 --> 00:00:02.000\nAna: Fish &amp; chips.\n"
        "2\n00:00:03.000 --> 00:00:04.000\nBen: Run on.\n"
        "00:00:05.000 --> 00:00:06.000\nAna: No id, no blank.\n\n"
        "3\n00:0x:07.000 --> 00:00:08.000\nAna: A timing that cannot be read.\n\n"
        "4\n00:00:09.000 --> 00:00:10.000\nBen: Last.\n"
    )
    assert parse_vtt(vtt) == [
        Entry(1.0, 2.0, "Ana", "Fish & chips."),
        Entry(3.0, 4.0, "Ben", "Run on."),
        Entry(5.0, 6.0, "Ana", "No id, no blank."),
        Entry(9.0, 10.0, "Ben", "Last."),
    ]


def test_a_name_prefix_is_a_speaker_only_when_most_cues_have_one() -> None:
    vtt = (
        "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nNote: the build is green.\n\n"
        "00:00:03.000 --> 00:00:04.000\nWe ship on Friday.\n\n"
        "00:00:05.000 --> 00:00:06.000\nThen we rest.\n"
    )
    assert [(e.speaker, e.text) for e in parse_vtt(vtt)] == [
        ("", "Note: the build is green."),
        ("", "We ship on Friday."),
        ("", "Then we rest."),
    ]


# the stores' clients


def test_a_zoom_token_uses_the_account_grant_and_a_uuid_is_encoded_as_asked(
    stores: _Stores,
) -> None:
    assert asyncio.run(recording.zoom_token(ZOOM_KEY)) == "tok"
    sent = stores.requests[-1]
    assert sent.headers["authorization"] == "Basic " + base64.b64encode(b"zc:zs").decode()
    assert b"grant_type=account_credentials" in sent.content and b"account_id=acc" in sent.content
    assert recording.uuid_path("ab+c==") == "ab%2Bc%3D%3D"
    assert recording.uuid_path("/ab//c") == "%252Fab%252F%252Fc"
    with pytest.raises(recording.CredentialRefused):
        asyncio.run(recording.zoom_token('{"client_id": "x"}'))


@pytest.mark.parametrize(
    "scope",
    [
        f"{ZOOM_SCOPES} cloud_recording:delete:recording_file:admin",
        "recording:write:admin",
        "",
        None,
    ],
)
def test_a_token_with_more_than_read_scopes_is_refused(stores: _Stores, scope: str | None) -> None:
    stores.scopes["zoom.us"] = scope
    with pytest.raises(recording.CredentialRefused, match="read-only"):
        asyncio.run(recording.zoom_token(ZOOM_KEY))


def test_a_transcript_download_goes_only_to_the_store_s_hosts() -> None:
    assert recording.zoom_download_allowed("https://files.zoom.us/rec/download/a")
    assert recording.zoom_download_allowed("https://zoom.us/rec/download/a")
    for url in ("http://zoom.us/a", "https://zoom.us.example.com/a", "https://example.com/zoom.us"):
        assert not recording.zoom_download_allowed(url)


def test_a_refused_account_token_is_a_refused_credential(stores: _Stores) -> None:
    stores.token_status = 400
    with pytest.raises(recording.CredentialRefused):
        asyncio.run(recording.zoom_token(ZOOM_KEY))


def test_a_rate_limit_reason_in_a_403_is_a_rate_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    def _quota(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": {"errors": [{"reason": "quotaExceeded"}]}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(_quota))
    monkeypatch.setattr(recording, "get_client", lambda: client)
    session = source_sync_recording.Session("zoom", "tok")
    with pytest.raises(recording.RateLimited):
        asyncio.run(source_sync_recording.list_recordings(session, [MEETING]))


# the sync turn


def test_zoom_transcripts_keep_import_metadata_and_a_recent_one_is_checked_again(
    world: _World,  # noqa: F811
    stores: _Stores,
) -> None:
    world.sources = [_source("zoom", [MEETING], ZOOM_KEY)]
    first, second = _ago(5), _ago(3)
    stores.instances = [
        {"uuid": "rec1", "start_time": first},
        {"uuid": "rec2", "start_time": second},
    ]
    stores.recordings = {"rec1": _zoom_recording(), "rec2": None}
    result = asyncio.run(source_sync.run_sync())
    assert (result.changed, result.unconverted, result.failed) == (1, 1, 0)
    ((inp, actor),) = world.upserts
    assert actor == SYNC_WORKER_ACTOR
    assert inp.source_uri == "https://api.zoom.us/v2/meetings/rec1/recordings"
    assert inp.content.split("\n\n") == [
        "# 00:01–00:09: We keep the hash join.",
        "Ana Lima: We keep the hash join. The planner pass moves later.",
        "# 06:10–06:15: # not a heading, a hash sign",
        "Ben Ode: # not a heading, a hash sign",
    ]
    assert inp.title == f"Meeting {MEETING} {first[:10]} {first[11:16]} UTC"
    assert inp.metadata["ingested_format"] == "transcript"
    assert inp.metadata["source_kind"] == "recording"
    assert inp.source_class == "imported"
    assert "source:calls" in inp.tags
    detail = _detail(world, "calls")
    assert detail["status"] == "ok"
    assert detail["unconverted_reasons"] == {"no_transcript": 1}
    assert detail["documents_seen"] == 2
    assert detail["file_waits"] == {}
    assert not any(r.url.path.endswith("/mp4") for r in stores.requests)
    assert world.states["calls"]["state_value"] == second

    stores.recordings["rec2"] = _zoom_recording()
    world.upserts.clear()
    stores.requests.clear()
    asyncio.run(source_sync.run_sync())
    assert [i.source_uri for i, _ in world.upserts] == [
        "https://api.zoom.us/v2/meetings/rec2/recordings"
    ]
    assert not any("/rec1/" in r.url.path for r in stores.requests)
    assert _detail(world, "calls")["unconverted_reasons"] == {}


@pytest.mark.parametrize("provider", ["meet", "unknown", ""])
def test_retired_or_unknown_provider_refuses_every_dispatch_before_transport(
    stores: _Stores, provider: str
) -> None:
    expected = "retired" if provider == "meet" else "unsupported"
    session = source_sync_recording.Session(provider, "tok")
    with pytest.raises(recording.VendorError, match=expected):
        asyncio.run(source_sync_recording.open_session(provider, MEET_KEY))
    with pytest.raises(recording.VendorError, match=expected):
        asyncio.run(source_sync_recording.list_recordings(session, [SPACE]))
    with pytest.raises(recording.VendorError, match=expected):
        asyncio.run(source_sync_recording.transcript(session, {"id": "old-record"}, max_bytes=1000))
    with pytest.raises(source_probes.SourceUnreachable, match=expected):
        asyncio.run(
            source_probes.probe(
                "recording", {"provider": provider, "ids": [SPACE]}, MEET_KEY, items=[]
            )
        )
    assert stores.requests == []


def test_a_settled_recording_without_a_transcript_is_given_up_until_a_full_listing(
    world: _World,  # noqa: F811
    stores: _Stores,
) -> None:
    world.sources = [_source("zoom", [MEETING], ZOOM_KEY)]
    stores.instances = [{"uuid": "rec1", "start_time": _ago(49)}]
    asyncio.run(source_sync.run_sync())
    detail = _detail(world, "calls")
    assert detail["unconverted_reasons"] == {"no_transcript": 1}
    assert detail["file_waits"]["rec1"][2] == "no_transcript"
    stores.requests.clear()
    asyncio.run(source_sync.run_sync())
    assert not any("/rec1/" in r.url.path for r in stores.requests)
    stores.recordings["rec1"] = _zoom_recording()
    asyncio.run(source_sync.run_sync(full=True))
    assert list(world.docs) == ["https://api.zoom.us/v2/meetings/rec1/recordings"]


@pytest.mark.parametrize("hours", [3, 50])
def test_unrecorded_instances_ahead_of_a_recorded_one_do_not_hold_it_back(
    world: _World,  # noqa: F811
    stores: _Stores,
    monkeypatch: pytest.MonkeyPatch,
    hours: float,
) -> None:
    monkeypatch.setattr(source_sync.settings, "knowledge_source_sync_documents_per_pass", 2)
    world.sources = [_source("zoom", [MEETING], ZOOM_KEY)]
    stores.instances = [{"uuid": f"none{i}", "start_time": _ago(hours + i)} for i in range(3)]
    stores.instances.append({"uuid": "rec1", "start_time": _ago(hours + 3)})
    stores.recordings = {"rec1": _zoom_recording()}
    for _ in range(3):
        asyncio.run(source_sync.run_sync())
    assert len(world.docs) == 1
    detail = _detail(world, "calls")
    assert (detail["status"], detail["error"], detail["unconverted"]) == ("ok", "", 3)
    assert world.states["calls"]["state_value"]  # the turn completed: the stamp moved


def test_a_recording_source_at_its_bound_holds_new_ones_and_unrecorded_take_no_room(
    world: _World,  # noqa: F811
    stores: _Stores,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(source_sync.settings, "knowledge_source_max_documents", 1)
    world.sources = [_source("zoom", [MEETING], ZOOM_KEY)]
    stores.instances = [{"uuid": "none0", "start_time": _ago(3)}]
    stores.instances += [{"uuid": f"rec{i}", "start_time": _ago(4 + i)} for i in (1, 2)]
    stores.recordings = {"rec1": _zoom_recording(), "rec2": _zoom_recording()}
    asyncio.run(source_sync.run_sync())
    assert len(world.docs) == 1
    assert _detail(world, "calls")["error"] == "too_many_documents: 1 new files past 1"


def test_a_recording_the_store_no_longer_lists_is_kept(
    world: _World,  # noqa: F811
    stores: _Stores,
) -> None:
    world.sources = [_source("zoom", [MEETING], ZOOM_KEY)]
    stores.instances = [{"uuid": "rec1", "start_time": _ago(2)}]
    stores.recordings = {"rec1": _zoom_recording()}
    asyncio.run(source_sync.run_sync())
    stores.instances = []
    result = asyncio.run(source_sync.run_sync(full=True))
    assert result.removed == 0
    assert list(world.docs) == ["https://api.zoom.us/v2/meetings/rec1/recordings"]
    assert world.audits == []


def test_a_meeting_the_account_lost_fails_the_turn_and_the_others_are_written(
    world: _World,  # noqa: F811
    stores: _Stores,
) -> None:
    lost = "123456789"
    world.sources = [_source("zoom", [lost, MEETING], ZOOM_KEY)]
    stores.meeting_status = {lost: 404}
    stores.instances = [{"uuid": "rec1", "start_time": _ago(2)}]
    stores.recordings = {"rec1": _zoom_recording()}
    asyncio.run(source_sync.run_sync())
    detail = _detail(world, "calls")
    assert (detail["status"], detail["error"]) == ("failed", f"meeting {lost}: http_404")
    assert len(world.docs) == 1
    assert world.states["calls"]["state_value"] == ""


def test_zoom_instances_with_a_transcript_are_written_and_the_rest_given_up(
    world: _World,  # noqa: F811
    stores: _Stores,
) -> None:
    world.sources = [_source("zoom", [MEETING], ZOOM_KEY)]
    stores.instances = [
        {"uuid": "/ab//c==", "start_time": _ago(100)},
        {"uuid": "noRec+1==", "start_time": _ago(99)},
        {"uuid": "elsewhere1", "start_time": _ago(98)},
    ]
    stores.recordings = {
        "/ab//c==": _zoom_recording(),
        "noRec+1==": None,
        "elsewhere1": _zoom_recording(download_url="https://example.com/t.vtt"),
    }
    result = asyncio.run(source_sync.run_sync())
    assert (result.changed, result.unconverted) == (1, 2)
    ((inp, _),) = world.upserts
    assert (
        inp.source_uri == "https://api.zoom.us/v2/meetings/%252Fab%252F%252Fc%253D%253D/recordings"
    )
    assert inp.content.startswith("# 00:01–00:09: We keep the hash join.\n\nAna Lima: We keep")
    assert inp.metadata["ingested_from"] == "/ab//c=="
    detail = _detail(world, "calls")
    assert detail["unconverted_reasons"] == {"no_transcript": 1, "transcript_off_host": 1}
    assert {k: v[2] for k, v in detail["file_waits"].items()} == {
        "noRec+1==": "no_transcript",
        "elsewhere1": "transcript_off_host",
    }
    # The bearer token went to the store's hosts only, and the media was never fetched.
    hosts = {r.url.host for r in stores.requests if "authorization" in r.headers}
    assert hosts <= {"zoom.us", "api.zoom.us", "files.zoom.us"}
    assert not any(r.url.path.endswith("/mp4") for r in stores.requests)


def test_a_refused_credential_a_wide_scope_or_a_rate_limit_is_the_turn_s(
    world: _World,  # noqa: F811
    stores: _Stores,
) -> None:
    world.sources = [_source("zoom", [MEETING], ZOOM_KEY)]
    stores.token_status = 401
    asyncio.run(source_sync.run_sync())
    assert _detail(world, "calls")["status"] == "credential_invalid"
    stores.token_status = 429
    asyncio.run(source_sync.run_sync())
    assert _detail(world, "calls")["status"] == "rate_limited"
    stores.token_status = 200
    stores.scopes["zoom.us"] = "meeting:write:admin"
    stores.requests.clear()
    asyncio.run(source_sync.run_sync())
    assert _detail(world, "calls")["status"] == "credential_invalid"
    assert [r.url.host for r in stores.requests] == ["zoom.us"]


def test_the_probe_counts_recordings_and_refuses_an_unseen_id_or_a_wide_scope(
    stores: _Stores,
) -> None:
    first, second = _ago(5), _ago(4)
    stores.instances = [
        {"uuid": "rec1", "start_time": first},
        {"uuid": "rec2", "start_time": second},
    ]
    config = {"provider": "zoom", "ids": [MEETING]}
    found = asyncio.run(source_probes.probe("recording", config, ZOOM_KEY, items=[]))
    assert (found.documents, found.sample) == (
        2,
        [
            f"Meeting {MEETING} {second[:10]} {second[11:16]} UTC",
            f"Meeting {MEETING} {first[:10]} {first[11:16]} UTC",
        ],
    )
    stores.meeting_status = {MEETING: 404}
    with pytest.raises(source_probes.SourceUnreachable):
        asyncio.run(source_probes.probe("recording", config, ZOOM_KEY, items=[]))
    with pytest.raises(source_probes.CredentialRefused):
        asyncio.run(source_probes.probe("recording", config, "{}", items=[]))
    stores.scopes["zoom.us"] = "meeting:write:admin"
    with pytest.raises(source_probes.CredentialRefused):
        asyncio.run(source_probes.probe("recording", config, ZOOM_KEY, items=[]))


def test_a_session_never_shows_its_token() -> None:
    assert "secret-token" not in repr(source_sync_recording.Session("zoom", "secret-token"))


def test_a_zoom_full_listing_reads_the_last_thirty_days_and_keeps_older_documents(
    world: _World,  # noqa: F811
    stores: _Stores,
) -> None:
    world.sources = [_source("zoom", [MEETING], ZOOM_KEY)]
    stores.instances = [
        {"uuid": "old1", "start_time": _ago(24 * 31)},
        {"uuid": "new1", "start_time": _ago(24 * 29)},
    ]
    stores.recordings = {"old1": _zoom_recording(), "new1": _zoom_recording()}
    asyncio.run(source_sync.run_sync())
    assert [d["metadata"]["ingested_from"] for d in world.docs.values()] == ["new1"]
    # A document written from an instance that has since aged out stays.
    stores.instances = [{"uuid": "new1", "start_time": _ago(24 * 31)}]
    result = asyncio.run(source_sync.run_sync(full=True))
    assert (result.removed, len(world.docs)) == (0, 1)
    assert _detail(world, "calls")["documents_seen"] == 0


def test_a_vtt_speaker_name_reads_as_one_line(stores: _Stores) -> None:
    stores.vtt = "WEBVTT\n\n00:00:05.000 --> 00:00:06.000\n<v Ana\n  # Lima>Hello.</v>\n"
    stores.recordings = {"rec1": _zoom_recording()}
    session = source_sync_recording.Session("zoom", "tok")
    text = asyncio.run(source_sync_recording.transcript(session, {"id": "rec1"}, max_bytes=1000))
    assert text.split("\n\n") == ["# 00:05–00:06: Hello.", "Ana # Lima: Hello."]


def test_a_meeting_the_account_may_not_read_is_lost_not_a_refused_credential(
    world: _World,  # noqa: F811
    stores: _Stores,
) -> None:
    closed = "123456789"
    world.sources = [_source("zoom", [closed, MEETING], ZOOM_KEY)]
    stores.meeting_status = {closed: 403}
    stores.instances = [{"uuid": "rec1", "start_time": _ago(2)}]
    stores.recordings = {"rec1": _zoom_recording()}
    asyncio.run(source_sync.run_sync(full=True))
    detail = _detail(world, "calls")
    assert (detail["status"], detail["error"]) == ("failed", f"meeting {closed}: http_403")
    assert list(world.docs) == ["https://api.zoom.us/v2/meetings/rec1/recordings"]
    assert world.states["calls"]["state_value"] == ""
    assert world.audits == []
    config = {"provider": "zoom", "ids": [closed, MEETING]}
    with pytest.raises(source_probes.SourceUnreachable, match=f"meeting {closed}: http_403"):
        asyncio.run(source_probes.probe("recording", config, ZOOM_KEY, items=[]))


def test_a_401_on_a_meeting_after_the_token_exchange_is_the_turn_s_credential(
    world: _World,  # noqa: F811
    stores: _Stores,
) -> None:
    world.sources = [_source("zoom", [MEETING], ZOOM_KEY)]
    stores.meeting_status = {MEETING: 401}
    stores.instances = [{"uuid": "rec1", "start_time": _ago(2)}]
    stores.recordings = {"rec1": _zoom_recording()}
    asyncio.run(source_sync.run_sync())
    detail = _detail(world, "calls")
    assert (detail["status"], detail["error"]) == (
        "credential_invalid",
        f"meeting {MEETING}: http_401",
    )
    assert world.docs == {} and world.upserts == []


def test_a_401_on_recording_files_stops_the_turn(
    world: _World,  # noqa: F811
    stores: _Stores,
) -> None:
    world.sources = [_source("zoom", [MEETING], ZOOM_KEY)]
    stores.instances = [
        {"uuid": "rec1", "start_time": _ago(5)},
        {"uuid": "rec2", "start_time": _ago(4)},
    ]
    stores.recordings = {"rec1": _zoom_recording(), "rec2": _zoom_recording()}
    stores.recording_status = {"rec1": 401}
    asyncio.run(source_sync.run_sync())
    detail = _detail(world, "calls")
    assert (detail["status"], detail["error"]) == ("credential_invalid", "recording: http_401")
    assert world.upserts == []
    assert not any("/rec2/" in r.url.path for r in stores.requests)
    assert world.states["calls"]["state_value"] == ""


def test_a_zoom_transcript_keeps_its_download_size_bound(stores: _Stores) -> None:
    stores.vtt = "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nAna: " + "a" * 1200 + "\n"
    stores.recordings = {"rec1": _zoom_recording()}
    session = source_sync_recording.Session("zoom", "tok")
    size = len(stores.vtt.encode())
    text = asyncio.run(source_sync_recording.transcript(session, {"id": "rec1"}, max_bytes=size))
    assert "Ana: " + "a" * 1200 in text
    with pytest.raises(recording.TooLarge):
        asyncio.run(source_sync_recording.transcript(session, {"id": "rec1"}, max_bytes=size - 1))


def test_a_meeting_s_listing_error_names_the_configured_id(monkeypatch: pytest.MonkeyPatch) -> None:
    def _answer(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    client = httpx.AsyncClient(transport=httpx.MockTransport(_answer))
    monkeypatch.setattr(recording, "get_client", lambda: client)
    session = source_sync_recording.Session("zoom", "tok")
    with pytest.raises(recording.VendorError, match=f"^meeting {MEETING}: http_500$"):
        asyncio.run(source_sync_recording.list_recordings(session, [MEETING]))


def test_two_meeting_ids_listing_one_instance_write_it_once(
    world: _World,  # noqa: F811
    stores: _Stores,
) -> None:
    world.sources = [_source("zoom", ["123456789", MEETING], ZOOM_KEY)]
    stores.instances = [{"uuid": "rec1", "start_time": _ago(2)}]
    stores.recordings = {"rec1": _zoom_recording()}
    asyncio.run(source_sync.run_sync())
    assert len(world.upserts) == 1
    assert _detail(world, "calls")["documents_seen"] == 1
    assert sum("/rec1/" in r.url.path for r in stores.requests) == 1


def test_a_transcript_is_mined_for_decisions_by_the_same_leg(
    world: _World,  # noqa: F811
    stores: _Stores,
) -> None:
    world.sources = [_source("zoom", [MEETING], ZOOM_KEY)]
    stores.instances = [{"uuid": "rec1", "start_time": _ago(5)}]
    stores.recordings = {"rec1": _zoom_recording()}
    result = asyncio.run(source_sync.run_sync())
    uri = "https://api.zoom.us/v2/meetings/rec1/recordings"
    assert [u for u, actor, _ in world.mined] == [uri] and result.extracted == 1
    assert world.mined[0][1] == SYNC_WORKER_ACTOR
    revision = world.docs[uri]["metadata"]["source_revision"]
    assert _detail(world, "calls")["mined"] == {uri: revision}
    asyncio.run(source_sync.run_sync())
    assert len(world.mined) == 1


def test_a_transcript_whose_address_is_taken_during_the_fetch_is_counted_skipped(
    world: _World,  # noqa: F811
    stores: _Stores,
) -> None:
    world.sources = [_source("zoom", [MEETING], ZOOM_KEY)]
    stores.instances = [{"uuid": "rec1", "start_time": _ago(2)}]
    stores.recordings = {"rec1": _zoom_recording()}
    uri = "https://api.zoom.us/v2/meetings/rec1/recordings"

    def _during_download() -> None:
        world.docs[uri] = {"actor": "human:ann", "tags": [], "metadata": {}}

    stores.on_download = _during_download
    asyncio.run(source_sync.run_sync())
    detail = _detail(world, "calls")
    assert world.upserts == []
    assert world.docs[uri]["actor"] == "human:ann"
    assert (detail["skipped"], detail["changed"]) == (1, 0)


@pytest.mark.parametrize("provider", ["meet", "unknown"])
def test_a_stored_unsupported_provider_preserves_config_and_turn_bookkeeping(
    monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    source = _source(provider, [SPACE], MEET_KEY)
    source_before = deepcopy(source)
    state = {
        "state_value": "2026-01-02T10:00:00Z",
        "detail": json.dumps({"file_waits": {"old-record": [1, 2, "no_transcript"]}, "full": True}),
    }
    state_before = deepcopy(state)

    def _must_not_run(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("retired provider touched credentials, wait bookkeeping or documents")

    monkeypatch.setattr(source_sync_recording.source_ops, "credential_of", _must_not_run)
    monkeypatch.setattr(source_sync_recording.sync, "start_waits", _must_not_run)
    monkeypatch.setattr(source_sync_recording.sync, "stored_copies_with_room", _must_not_run)
    monkeypatch.setattr(recording, "get_client", _must_not_run)
    run: Any = object()
    result = asyncio.run(source_sync_recording.turn(source, state, run))
    assert result.status == ("provider_removed" if provider == "meet" else "failed")
    assert result.complete is False
    assert result.waits_read is False
    assert result.full is False
    assert result.kind_state == {}
    assert source == source_before
    assert state == state_before
    assert domain.stored_config(source) == source_before.config
