"""A `recording` source: its config, the transcript conversion, the two meeting
stores' clients and its sync turn, over the in-memory registry and documents of
test_knowledge_source_sync and meeting stores answered by a mock transport. Every
response is written here; none is a recording of real content."""

from __future__ import annotations

import asyncio
import base64
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import unquote

import httpx
import pytest

from server.clients import recording
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
    """What the two meeting stores answer."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        # meet: conference records, and per record id its transcripts and entries.
        self.records: list[dict[str, Any]] = []
        self.transcripts: dict[str, list[dict[str, Any]]] = {}
        self.entries: dict[str, list[dict[str, Any]]] = {}
        self.lost_spaces: set[str] = set()
        self.forbidden_spaces: set[str] = set()
        # An HTTP status a space read or a transcript's entries answer instead.
        self.space_status: dict[str, int] = {}
        self.entry_status: dict[str, int] = {}
        # zoom: instances, and per uuid its recording (None: not recorded).
        self.instances: list[dict[str, Any]] = []
        self.recordings: dict[str, dict[str, Any] | None] = {}
        self.vtt = VTT
        self.token_status = 200
        self.display = {"p1": "Ana Lima", "p2": "Ben Ode"}
        # The token answer's `scope`, per store; None leaves it out.
        self.scopes: dict[str, str | None] = {
            "oauth2.googleapis.com": recording.MEET_SCOPE,
            "zoom.us": ZOOM_SCOPES,
        }

    def answer(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        host, path = request.url.host, request.url.path
        if host in ("oauth2.googleapis.com", "zoom.us") and path.endswith("/token"):
            if self.token_status != 200:
                return httpx.Response(self.token_status, json={"error": "invalid_grant"})
            answer: dict[str, Any] = {"access_token": "tok"}
            if self.scopes[host] is not None:
                answer["scope"] = self.scopes[host]
            return httpx.Response(200, json=answer)
        if host == "meet.googleapis.com":
            return self._meet(request, path.removeprefix("/v2/"))
        if host == "api.zoom.us":
            return self._zoom(request, path.removeprefix("/v2/"))
        if host == "files.zoom.us":
            return httpx.Response(200, text=self.vtt)
        return httpx.Response(404)

    def _meet(self, request: httpx.Request, path: str) -> httpx.Response:
        if path.startswith("spaces/"):
            space = path.removeprefix("spaces/")
            if space in self.lost_spaces:
                return httpx.Response(404)
            if space in self.space_status:
                return httpx.Response(self.space_status[space])
            if space in self.forbidden_spaces:
                return httpx.Response(403, json={"error": {"status": "PERMISSION_DENIED"}})
            return httpx.Response(200, json={"name": f"spaces/{space}"})
        if path == "conferenceRecords":
            query = request.url.params["filter"]
            after = query.split('end_time >= "')[1].rstrip('"') if "end_time >=" in query else ""
            found = [r for r in self.records if r.get("endTime", "") >= after or not after]
            return httpx.Response(200, json={"conferenceRecords": found})
        parts = path.split("/")
        record = parts[1]
        if parts[-1] == "transcripts":
            return httpx.Response(200, json={"transcripts": self.transcripts.get(record, [])})
        if parts[-1] == "participants":
            people = [
                {
                    "name": f"{'/'.join(parts[:2])}/participants/p1",
                    "signedinUser": {"displayName": self.display["p1"]},
                },
                {
                    "name": f"{'/'.join(parts[:2])}/participants/p2",
                    "anonymousUser": {"displayName": self.display["p2"]},
                },
            ]
            return httpx.Response(200, json={"participants": people})
        if parts[-1] == "entries":
            if parts[3] in self.entry_status:
                return httpx.Response(self.entry_status[parts[3]])
            return httpx.Response(200, json={"transcriptEntries": self.entries.get(parts[3], [])})
        return httpx.Response(404)

    def _zoom(self, request: httpx.Request, path: str) -> httpx.Response:
        if path == f"past_meetings/{MEETING}/instances":
            return httpx.Response(200, json={"meetings": self.instances})
        if path.startswith("meetings/") and path.endswith("/recordings"):
            uuid = request.url.raw_path.decode().split("/")[3]
            found = self.recordings.get(unquote(unquote(uuid)))
            if found is None:
                return httpx.Response(404, json={"code": 3301})
            return httpx.Response(200, json=found)
        return httpx.Response(404)


def _meet_record(record: str, start: str, end: str) -> dict[str, Any]:
    return {"name": f"conferenceRecords/{record}", "startTime": start, "endTime": end}


def _entry(record: str, who: str, text: str, start: str, end: str) -> dict[str, Any]:
    return {
        "participant": f"conferenceRecords/{record}/participants/{who}",
        "text": text,
        "startTime": start,
        "endTime": end,
    }


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
    monkeypatch.setattr(recording, "PAGE_PACE_S", 0.0)
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


def test_a_recording_config_keeps_ids_and_refuses_the_rest() -> None:
    assert domain.parse_config(
        "recording", {"provider": "meet", "ids": ["spaces/ab-C_1", "ab-C_1"]}
    ) == {
        "provider": "meet",
        "ids": ["ab-C_1"],
    }
    assert domain.parse_config("recording", {"provider": "zoom", "ids": [85746065432]}) == {
        "provider": "zoom",
        "ids": ["85746065432"],
    }
    for bad in (
        {"provider": "other", "ids": ["x"]},
        {"provider": "zoom", "ids": ["abc"]},
        {"provider": "zoom", "ids": [True]},
        {"provider": "meet", "ids": ["a/b"]},
        {"provider": "meet", "ids": []},
        {"provider": "meet", "ids": [f"s{i}" for i in range(21)]},
        {"provider": "meet", "ids": ["x"], "host": "h"},
    ):
        with pytest.raises(domain.SourceConfigError):
            domain.parse_config("recording", bad)


def _ago(hours: float) -> str:
    """A store time `hours` before now, so a recording is recent or settled whatever
    day the test runs."""
    return (datetime.now(UTC) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _generated(record: str, transcript: str = "") -> list[dict[str, Any]]:
    name = f"conferenceRecords/{record}/transcripts/{transcript or 't' + record}"
    return [{"name": name, "state": "FILE_GENERATED"}]


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
    ("host", "scope"),
    [
        ("oauth2.googleapis.com", f"{recording.MEET_SCOPE} https://www.googleapis.com/auth/drive"),
        ("oauth2.googleapis.com", "https://www.googleapis.com/auth/meetings.space.created"),
        ("oauth2.googleapis.com", None),
        ("zoom.us", f"{ZOOM_SCOPES} cloud_recording:delete:recording_file:admin"),
        ("zoom.us", "recording:write:admin"),
        ("zoom.us", ""),
    ],
)
def test_a_token_with_more_than_read_scopes_is_refused(
    stores: _Stores, host: str, scope: str | None
) -> None:
    stores.scopes[host] = scope
    exchange = (
        recording.meet_token(MEET_KEY) if host != "zoom.us" else recording.zoom_token(ZOOM_KEY)
    )
    with pytest.raises(recording.CredentialRefused, match="read-only"):
        asyncio.run(exchange)


def test_a_transcript_download_goes_only_to_the_store_s_hosts() -> None:
    assert recording.zoom_download_allowed("https://files.zoom.us/rec/download/a")
    assert recording.zoom_download_allowed("https://zoom.us/rec/download/a")
    for url in ("http://zoom.us/a", "https://zoom.us.example.com/a", "https://example.com/zoom.us"):
        assert not recording.zoom_download_allowed(url)


def test_a_refused_refresh_token_is_a_refused_credential(stores: _Stores) -> None:
    stores.token_status = 400
    with pytest.raises(recording.CredentialRefused):
        asyncio.run(recording.meet_token(MEET_KEY))


def test_a_rate_limit_reason_in_a_403_is_a_rate_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    def _quota(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": {"errors": [{"reason": "quotaExceeded"}]}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(_quota))
    monkeypatch.setattr(recording, "get_client", lambda: client)
    session = source_sync_recording.Session("meet", "tok")
    with pytest.raises(recording.RateLimited):
        asyncio.run(source_sync_recording.list_recordings(session, [SPACE]))


# the sync turn


def test_meet_records_become_timestamped_documents_and_one_without_a_transcript_is_checked_again(
    world: _World,  # noqa: F811
    stores: _Stores,
) -> None:
    world.sources = [_source("meet", [SPACE], MEET_KEY)]
    r1_start, r1_end, r2_end = _ago(5), _ago(4), _ago(2)
    stores.records = [
        _meet_record("r1", r1_start, r1_end),
        _meet_record("r2", _ago(3), r2_end),
        {"name": "conferenceRecords/r3", "startTime": _ago(1)},  # still running
    ]
    stores.transcripts["r1"] = _generated("r1", "t1")
    zero = datetime.fromisoformat(r1_start)

    def at(minutes: float) -> str:
        return (zero + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")

    stores.entries["t1"] = [
        _entry("r1", "p1", "We keep\n the  hash join.", at(1), at(1.1)),
        _entry("r1", "p2", "Agreed.", at(12), at(12.05)),
    ]
    result = asyncio.run(source_sync.run_sync())
    assert (result.changed, result.unconverted, result.failed) == (1, 1, 0)
    ((inp, actor),) = world.upserts
    assert actor == SYNC_WORKER_ACTOR
    assert inp.source_uri == "https://meet.googleapis.com/v2/conferenceRecords/r1"
    assert inp.content.split("\n\n") == [
        "# 01:00–01:06: We keep the hash join.",
        "Ana Lima: We keep the hash join.",
        "# 12:00–12:03: Agreed.",
        "Ben Ode: Agreed.",
    ]
    assert inp.title == f"Meeting {zero.strftime('%Y-%m-%d %H:%M')} UTC"
    assert inp.metadata["ingested_format"] == "transcript"
    assert inp.metadata["source_kind"] == "recording"
    assert inp.source_class == "imported"
    assert "source:calls" in inp.tags
    detail = _detail(world, "calls")
    assert detail["status"] == "ok"
    assert detail["unconverted_reasons"] == {"no_transcript": 1}
    assert detail["documents_seen"] == 2
    # A recent recording with no transcript yet waits for nothing: no wait entry.
    assert detail["file_waits"] == {}
    # Media is never asked for, and the stamp is the newest end time listed.
    assert not any("recordings" in r.url.path for r in stores.requests)
    assert world.states["calls"]["state_value"] == r2_end

    # The next turn reaches back a day before the stamp: r2's transcript has arrived,
    # r1 is skipped by its revision.
    stores.transcripts["r2"] = _generated("r2", "t2")
    stores.entries["t2"] = [_entry("r2", "p1", "Ship it.", _ago(2.9), _ago(2.89))]
    world.upserts.clear()
    stores.requests.clear()
    asyncio.run(source_sync.run_sync())
    listing = next(r for r in stores.requests if r.url.path == "/v2/conferenceRecords")
    since = (datetime.fromisoformat(r2_end) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert f'end_time >= "{since}"' in listing.url.params["filter"]
    assert [i.source_uri for i, _ in world.upserts] == [
        "https://meet.googleapis.com/v2/conferenceRecords/r2"
    ]
    assert _detail(world, "calls")["unconverted_reasons"] == {}


@pytest.mark.parametrize("states", [["ENDED"], ["STARTED"], ["FILE_GENERATED", "ENDED"], []])
def test_a_transcript_whose_file_is_not_generated_is_no_transcript(
    stores: _Stores, states: list[str]
) -> None:
    stores.transcripts["r1"] = [
        {"name": f"conferenceRecords/r1/transcripts/t{i}", "state": state}
        for i, state in enumerate(states)
    ]
    session = source_sync_recording.Session("meet", "tok")
    item = {"id": "r1", "started": "2026-01-02T10:00:00Z"}
    with pytest.raises(source_sync_recording.NoTranscript):
        asyncio.run(source_sync_recording.transcript(session, item, max_bytes=1000))


def test_a_settled_recording_without_a_transcript_is_given_up_until_a_full_listing(
    world: _World,  # noqa: F811
    stores: _Stores,
) -> None:
    world.sources = [_source("meet", [SPACE], MEET_KEY)]
    stores.records = [_meet_record("r1", _ago(50), _ago(49))]
    asyncio.run(source_sync.run_sync())
    detail = _detail(world, "calls")
    assert detail["unconverted_reasons"] == {"no_transcript": 1}
    assert detail["file_waits"]["r1"][2] == "no_transcript"
    # The next pass checks nothing; a full sync request checks it once more.
    stores.requests.clear()
    asyncio.run(source_sync.run_sync())
    assert not any(r.url.path.endswith("/transcripts") for r in stores.requests)
    stores.transcripts["r1"] = _generated("r1")
    stores.entries["tr1"] = [_entry("r1", "p1", "Late.", _ago(49.5), _ago(49.4))]
    asyncio.run(source_sync.run_sync(full=True))
    assert list(world.docs) == ["https://meet.googleapis.com/v2/conferenceRecords/r1"]


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


def test_a_recording_the_store_no_longer_lists_is_kept(
    world: _World,  # noqa: F811
    stores: _Stores,
) -> None:
    world.sources = [_source("meet", [SPACE], MEET_KEY)]
    stores.records = [_meet_record("r1", "2026-01-02T10:00:00Z", "2026-01-02T10:40:00Z")]
    stores.transcripts["r1"] = _generated("r1", "t1")
    stores.entries["t1"] = [
        _entry("r1", "p1", "Kept.", "2026-01-02T10:01:00Z", "2026-01-02T10:01:01Z")
    ]
    asyncio.run(source_sync.run_sync())
    stores.records = []  # expired on the store's own schedule
    result = asyncio.run(source_sync.run_sync(full=True))
    assert result.removed == 0
    assert list(world.docs) == ["https://meet.googleapis.com/v2/conferenceRecords/r1"]
    assert world.audits == []


def test_a_space_the_account_lost_fails_the_turn_and_the_others_are_written(
    world: _World,  # noqa: F811
    stores: _Stores,
) -> None:
    world.sources = [_source("meet", ["lostSpace1", SPACE], MEET_KEY)]
    stores.lost_spaces = {"lostSpace1"}
    stores.records = [_meet_record("r1", "2026-01-02T10:00:00Z", "2026-01-02T10:40:00Z")]
    stores.transcripts["r1"] = _generated("r1", "t1")
    stores.entries["t1"] = [
        _entry("r1", "p1", "Hi.", "2026-01-02T10:01:00Z", "2026-01-02T10:01:01Z")
    ]
    asyncio.run(source_sync.run_sync())
    detail = _detail(world, "calls")
    assert (detail["status"], detail["error"]) == ("failed", "space lostSpace1: http_404")
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
    stores.records = [
        _meet_record("r1", "2026-01-02T10:00:00Z", "2026-01-02T10:40:00Z"),
        _meet_record("r2", "2026-01-03T10:00:00Z", "2026-01-03T10:30:00Z"),
    ]
    config = {"provider": "meet", "ids": [SPACE]}
    found = asyncio.run(source_probes.probe("recording", config, MEET_KEY))
    assert (found.documents, found.sample) == (
        2,
        ["Meeting 2026-01-03 10:00 UTC", "Meeting 2026-01-02 10:00 UTC"],
    )
    stores.lost_spaces = {SPACE}
    with pytest.raises(source_probes.SourceUnreachable):
        asyncio.run(source_probes.probe("recording", config, MEET_KEY))
    with pytest.raises(source_probes.CredentialRefused):
        asyncio.run(source_probes.probe("recording", {"provider": "zoom", "ids": [MEETING]}, "{}"))
    stores.scopes["oauth2.googleapis.com"] = (
        "https://www.googleapis.com/auth/meetings.space.created"
    )
    with pytest.raises(source_probes.CredentialRefused):
        asyncio.run(source_probes.probe("recording", config, MEET_KEY))


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


def test_a_meet_speaker_name_reads_as_one_line(stores: _Stores) -> None:
    stores.display["p1"] = "Ana\n  # Lima"
    stores.transcripts["r1"] = _generated("r1", "t1")
    stores.entries["t1"] = [
        _entry("r1", "p1", "Hello.", "2026-01-02T10:00:05Z", "2026-01-02T10:00:06Z")
    ]
    session = source_sync_recording.Session("meet", "tok")
    item = {"id": "r1", "started": "2026-01-02T10:00:00Z"}
    text = asyncio.run(source_sync_recording.transcript(session, item, max_bytes=1000))
    assert text.split("\n\n") == ["# 00:05–00:06: Hello.", "Ana # Lima: Hello."]


def test_a_space_the_account_may_not_read_is_lost_not_a_refused_credential(
    world: _World,  # noqa: F811
    stores: _Stores,
) -> None:
    world.sources = [_source("meet", ["closedSpace", SPACE], MEET_KEY)]
    stores.forbidden_spaces = {"closedSpace"}
    stores.records = [_meet_record("r1", "2026-01-02T10:00:00Z", "2026-01-02T10:40:00Z")]
    stores.transcripts["r1"] = _generated("r1", "t1")
    stores.entries["t1"] = [
        _entry("r1", "p1", "Hi.", "2026-01-02T10:01:00Z", "2026-01-02T10:01:01Z")
    ]
    asyncio.run(source_sync.run_sync(full=True))
    detail = _detail(world, "calls")
    assert (detail["status"], detail["error"]) == ("failed", "space closedSpace: http_403")
    assert list(world.docs) == ["https://meet.googleapis.com/v2/conferenceRecords/r1"]
    assert world.states["calls"]["state_value"] == ""
    assert world.audits == []
    config = {"provider": "meet", "ids": ["closedSpace", SPACE]}
    with pytest.raises(source_probes.SourceUnreachable, match="space closedSpace: http_403"):
        asyncio.run(source_probes.probe("recording", config, MEET_KEY))


def test_a_401_on_a_space_after_the_token_exchange_is_the_turn_s_credential(
    world: _World,  # noqa: F811
    stores: _Stores,
) -> None:
    world.sources = [_source("meet", ["revokedSpace", SPACE], MEET_KEY)]
    stores.space_status = {"revokedSpace": 401}
    stores.records = [_meet_record("r1", "2026-01-02T10:00:00Z", "2026-01-02T10:40:00Z")]
    stores.transcripts["r1"] = _generated("r1", "t1")
    asyncio.run(source_sync.run_sync())
    detail = _detail(world, "calls")
    assert (detail["status"], detail["error"]) == (
        "credential_invalid",
        "space revokedSpace: http_401",
    )
    assert world.docs == {} and world.upserts == []


def test_a_401_on_a_transcript_s_entries_stops_the_turn(
    world: _World,  # noqa: F811
    stores: _Stores,
) -> None:
    world.sources = [_source("meet", [SPACE], MEET_KEY)]
    stores.records = [
        _meet_record("r1", "2026-01-02T10:00:00Z", "2026-01-02T10:40:00Z"),
        _meet_record("r2", "2026-01-03T10:00:00Z", "2026-01-03T10:40:00Z"),
    ]
    stores.transcripts = {"r1": _generated("r1", "t1"), "r2": _generated("r2", "t2")}
    stores.entry_status = {"t1": 401}
    asyncio.run(source_sync.run_sync())
    detail = _detail(world, "calls")
    assert (detail["status"], detail["error"]) == (
        "credential_invalid",
        "conferenceRecords/r1/transcripts/t1: http_401",
    )
    assert world.upserts == []
    # The turn stopped at the first record: the second's transcripts were never read.
    assert not any("/r2/" in r.url.path for r in stores.requests)
    assert world.states["calls"]["state_value"] == ""


def test_a_record_s_transcripts_are_merged_under_one_size_bound(
    stores: _Stores,
) -> None:
    stores.transcripts["r1"] = [
        {
            "name": "conferenceRecords/r1/transcripts/ta",
            "state": "FILE_GENERATED",
            "startTime": "2026-01-02T10:00:00Z",
        },
        {
            "name": "conferenceRecords/r1/transcripts/tb",
            "state": "FILE_GENERATED",
            "startTime": "2026-01-02T10:20:00Z",
        },
    ]
    stores.entries["ta"] = [
        _entry("r1", "p1", "a" * 600, "2026-01-02T10:01:00Z", "2026-01-02T10:01:30Z")
    ]
    stores.entries["tb"] = [
        _entry("r1", "p2", "b" * 600, "2026-01-02T10:21:00Z", "2026-01-02T10:21:30Z")
    ]
    session = source_sync_recording.Session("meet", "tok")
    item = {"id": "r1", "started": "2026-01-02T10:00:00Z"}
    text = asyncio.run(source_sync_recording.transcript(session, item, max_bytes=1200))
    assert [line[:12] for line in text.split("\n\n")] == [
        "# 01:00–01:3",
        "Ana Lima: aa",
        "# 21:00–21:3",
        "Ben Ode: bbb",
    ]
    # Each transcript fits 1000 bytes alone; together they do not.
    with pytest.raises(recording.TooLarge):
        asyncio.run(source_sync_recording.transcript(session, item, max_bytes=1000))


def test_a_space_s_listing_error_names_the_configured_id(monkeypatch: pytest.MonkeyPatch) -> None:
    def _answer(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/v2/spaces/"):
            return httpx.Response(200, json={"name": "spaces/canonicalId"})
        return httpx.Response(500)

    client = httpx.AsyncClient(transport=httpx.MockTransport(_answer))
    monkeypatch.setattr(recording, "get_client", lambda: client)
    session = source_sync_recording.Session("meet", "tok")
    with pytest.raises(recording.RecordingError, match="^space abc-mnop-xyz: http_500$"):
        asyncio.run(source_sync_recording.list_recordings(session, ["abc-mnop-xyz"]))


def test_two_ids_naming_one_space_list_each_record_once(
    world: _World,  # noqa: F811
    stores: _Stores,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _canonical(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/v2/spaces/"):
            return httpx.Response(200, json={"name": f"spaces/{SPACE}"})
        return stores.answer(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(_canonical))
    monkeypatch.setattr(recording, "get_client", lambda: client)
    world.sources = [_source("meet", ["abc-mnop-xyz", SPACE], MEET_KEY)]
    stores.records = [_meet_record("r1", "2026-01-02T10:00:00Z", "2026-01-02T10:40:00Z")]
    stores.transcripts["r1"] = _generated("r1", "t1")
    stores.entries["t1"] = [
        _entry("r1", "p1", "Once.", "2026-01-02T10:01:00Z", "2026-01-02T10:01:01Z")
    ]
    asyncio.run(source_sync.run_sync())
    assert len(world.upserts) == 1
    assert _detail(world, "calls")["documents_seen"] == 1
    assert sum(r.url.path.endswith("/transcripts") for r in stores.requests) == 1


def test_a_transcript_is_mined_for_decisions_by_the_same_leg(
    world: _World,  # noqa: F811
    stores: _Stores,
) -> None:
    world.sources = [_source("meet", [SPACE], MEET_KEY)]
    start, end = _ago(5), _ago(4)
    stores.records = [_meet_record("r1", start, end)]
    stores.transcripts["r1"] = _generated("r1", "t1")
    stores.entries["t1"] = [_entry("r1", "p1", "We keep the hash join.", start, end)]
    result = asyncio.run(source_sync.run_sync())
    uri = "https://meet.googleapis.com/v2/conferenceRecords/r1"
    assert [u for u, actor, _ in world.mined] == [uri] and result.extracted == 1
    assert world.mined[0][1] == SYNC_WORKER_ACTOR
    revision = world.docs[uri]["metadata"]["source_revision"]
    assert _detail(world, "calls")["mined"] == {uri: revision}
    asyncio.run(source_sync.run_sync())
    assert len(world.mined) == 1
