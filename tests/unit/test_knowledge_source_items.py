"""A source's items: their parser, the link parser, the config form a build
without items reads, the storage rule that keeps such a build off a source it
cannot read, and the shared-folder listing the items build (queries, a recursive
walk, a shared drive, single files, filters). Every response is written here."""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

import httpx
import pydantic
import pytest

from server.clients import gdrive, linear
from server.core.errors import BadConfigError
from server.knowledge.api import models_sources
from server.knowledge.app import source_gdrive_items as items_listing
from server.knowledge.app import source_probes
from server.knowledge.domain import sources as domain
from server.knowledge.domain.source_links import parse_links
from server.knowledge.domain.sources import KnowledgeSource
from server.knowledge.store import sources as store
from tests.unit.ch_fake import RecordingCH

FOLDER = "1AbCdEfGhIjKlMnOp"
FILE = "1FiLeIdOfADocument_x"
DRIVE = "0AbCdEfGhIjKlUk9PVA"
PAGE = "0123456789abcdef0123456789abcdef"
PAGE_DASHED = "01234567-89ab-cdef-0123-456789abcdef"
PROJECT = "0b6d7a52-1c1e-4c2b-9a4e-5d0f3b1e2a01"
DOCS = "application/vnd.google-apps.document"


def _folder(folder_id: str = FOLDER, recursive: bool = False) -> dict[str, Any]:
    return {"type": "folder", "id": folder_id, "recursive": recursive}


# ---- parse_items -----------------------------------------------------------------


def test_every_item_type_is_stored_in_one_form() -> None:
    found = domain.parse_items(
        "gdrive",
        [
            {"type": "folder", "id": FOLDER},
            {"type": "file", "id": FILE},
            {"type": "shared_drive", "id": DRIVE},
            {
                "type": "filter",
                "name_contains": " Roadmap ",
                "kinds": ["pdf", "docs"],
                "modified_after": "2026-01-31",
                "in": FOLDER,
            },
        ],
    )
    assert found == [
        _folder(),
        {"type": "file", "id": FILE},
        {"type": "shared_drive", "id": DRIVE},
        {
            "type": "filter",
            "name_contains": "Roadmap",
            "kinds": ["docs", "pdf"],
            "modified_after": "2026-01-31",
            "in": FOLDER,
        },
    ]
    assert domain.parse_items("notion", [{"type": "database", "id": PAGE}]) == [
        {"type": "database", "id": PAGE_DASHED}
    ]
    assert domain.parse_items("linear", [{"type": "project", "id": PROJECT.upper()}]) == [
        {"type": "project", "id": PROJECT}
    ]
    assert domain.parse_items("linear", [{"type": "project", "slug_id": "1a2b3c4d5e6f"}]) == [
        {"type": "project", "slug_id": "1a2b3c4d5e6f"}
    ]


def test_a_later_item_of_the_same_type_and_id_replaces_the_earlier() -> None:
    found = domain.parse_items(
        "gdrive", [_folder(), {"type": "file", "id": FILE}, _folder(recursive=True)]
    )
    assert found == [{"type": "file", "id": FILE}, _folder(recursive=True)]


@pytest.mark.parametrize(
    ("kind", "item"),
    [
        ("gdrive", {"type": "folder", "id": "x' or '1"}),
        ("gdrive", {"type": "folder", "id": FOLDER, "recursive": "yes"}),
        ("gdrive", {"type": "folder", "id": FOLDER, "query": "x"}),
        ("gdrive", {"type": "page", "id": PAGE}),
        ("gdrive", {"type": "filter"}),
        ("gdrive", {"type": "filter", "name_contains": "a\nb"}),
        ("gdrive", {"type": "filter", "name_contains": "x" * 101}),
        ("gdrive", {"type": "filter", "owner": "a.person@example.com"}),
        ("gdrive", {"type": "filter", "modified_after": "2026-02-30"}),
        ("gdrive", {"type": "filter", "modified_after": "2026-01-01T00:00:00"}),
        ("gdrive", {"type": "filter", "kinds": ["xlsx"]}),
        ("gdrive", {"type": "filter", "kinds": []}),
        ("gdrive", {"type": "filter", "in": "../x"}),
        ("notion", {"type": "page", "id": "short"}),
        ("linear", {"type": "project", "id": "not-a-uuid"}),
        ("linear", {"type": "project", "slug_id": "zz"}),
        ("linear", {"type": "project", "id": PROJECT, "slug_id": "1a2b3c4d5e6f"}),
        ("gdrive", "a folder"),
    ],
)
def test_a_malformed_item_is_refused(kind: str, item: Any) -> None:
    with pytest.raises(BadConfigError):
        domain.parse_items(kind, [item])


def test_many_items_are_left_to_the_count() -> None:
    many = [{"type": "file", "id": f"{FILE}{n:03d}"} for n in range(domain.MAX_ITEMS + 1)]
    # The op counts, once short ids are resolved; the parser only checks each item.
    assert len(domain.parse_items("gdrive", many)) == domain.MAX_ITEMS + 1


# ---- config and items ------------------------------------------------------------


def test_id_list_config_turns_into_items_and_back() -> None:
    config = {"folder_ids": [FOLDER, "2QrStUvWxYzAbCdEf"], "include": ["docs", "pdf"]}
    items = domain.items_from_config("gdrive", config)
    assert items == [_folder(), _folder("2QrStUvWxYzAbCdEf")]
    assert domain.config_from_items("gdrive", items, ["docs", "pdf"]) == config
    notion = {"page_ids": [PAGE_DASHED], "database_ids": [PROJECT]}
    assert (
        domain.config_from_items("notion", domain.items_from_config("notion", notion), []) == notion
    )
    linear_config = {"project_ids": [PROJECT]}
    items = domain.items_from_config("linear", linear_config)
    assert domain.config_from_items("linear", items, []) == linear_config
    assert domain.items_from_config("recording", {"provider": "zoom", "ids": ["123456789"]}) == []


@pytest.mark.parametrize(
    "items",
    [
        [_folder(recursive=True)],
        [_folder(), {"type": "file", "id": FILE}],
        [{"type": "shared_drive", "id": DRIVE}],
        [_folder(f"{FOLDER}{n:02d}") for n in range(domain.MAX_FOLDERS + 1)],
    ],
)
def test_an_item_id_list_config_cannot_hold_has_no_config_form(items: list[Any]) -> None:
    assert domain.config_from_items("gdrive", items, ["docs"]) is None


def test_a_project_named_by_its_short_id_has_no_config_form() -> None:
    assert (
        domain.config_from_items("linear", [{"type": "project", "slug_id": "1a2b3c4d5e6f"}], [])
        is None
    )


# ---- the link parser -------------------------------------------------------------


@pytest.mark.parametrize(
    ("link", "kind", "item"),
    [
        (f"https://drive.google.com/drive/folders/{FOLDER}?usp=sharing", "gdrive", _folder()),
        (f"https://drive.google.com/drive/u/2/folders/{FOLDER}", "gdrive", _folder()),
        (f"drive.google.com/drive/folders/{FOLDER}/", "gdrive", _folder()),
        (
            f"https://drive.google.com/drive/mobile/folders/{FOLDER}?usp=sharing",
            "gdrive",
            _folder(),
        ),
        (f"https://drive.google.com/file/d/{FILE}/view?usp=drive_link", "gdrive", None),
        (f"https://docs.google.com/document/d/{FILE}/edit#heading=h.1", "gdrive", None),
        (f"https://docs.google.com/a/example.com/document/d/{FILE}/edit", "gdrive", None),
        (f"https://docs.google.com/presentation/d/{FILE}/edit", "gdrive", None),
        (
            f"https://www.notion.so/Design-Notes-{PAGE}",
            "notion",
            {"type": "page", "id": PAGE_DASHED},
        ),
        (f"https://notion.so/acme/{PAGE.upper()}", "notion", {"type": "page", "id": PAGE_DASHED}),
        (
            f"https://acme.notion.site/Design-Notes-{PAGE}",
            "notion",
            {"type": "page", "id": PAGE_DASHED},
        ),
        (
            f"https://www.notion.so/acme/{PAGE}?v=abc",
            "notion",
            {"type": "database", "id": PAGE_DASHED},
        ),
        (
            f"https://www.notion.so/acme/{'b' * 32}?v=abc&p={PAGE}&pm=s",
            "notion",
            {"type": "page", "id": PAGE_DASHED},
        ),
        (
            "https://linear.app/acme/project/q3-planning-1a2b3c4d5e6f/overview",
            "linear",
            {"type": "project", "slug_id": "1a2b3c4d5e6f"},
        ),
        (
            "https://linear.app/acme/project/1a2b3c4d5e6f",
            "linear",
            {"type": "project", "slug_id": "1a2b3c4d5e6f"},
        ),
    ],
)
def test_each_link_form_names_its_item(link: str, kind: str, item: Any) -> None:
    [parsed] = parse_links([link])
    assert (parsed.kind, parsed.item, parsed.unreadable) == (
        kind,
        item or {"type": "file", "id": FILE},
        "",
    )


@pytest.mark.parametrize(
    ("link", "reason"),
    [
        ("https://example.com/drive/folders/x", "not a store"),
        ("ftp://drive.google.com/drive/folders/x", "not a web link"),
        ("https://drive.google.com/drive/my-drive", "no folder or file id"),
        ("https://drive.google.com/drive/folders/x'%20or%20'1", "is not an id"),
        (f"https://docs.google.com/spreadsheets/d/{FILE}/edit", "a spreadsheet"),
        ("https://www.notion.so/acme/Design-Notes", "no page id"),
        (f"https://drive.google.com/open?id={FILE}", "a folder or a file alike"),
        (f"https://docs.google.com/drawings/d/{FILE}/edit", "a drawing"),
        ("https://docs.google.com/document/d/e/2PACX-1vAbC/pub", "a published copy"),
        ("https://linear.app/acme/issue/ENG-12/a-bug", "a single issue"),
        ("https://linear.app/acme/team/ENG/all", "a team link: link one of its projects"),
        ("https://linear.app/acme/project/q3-planning", "no project id"),
        ("", "not a link"),
    ],
)
def test_a_link_that_names_no_item_says_why(link: str, reason: str) -> None:
    [parsed] = parse_links([link])
    assert parsed.kind == "" and parsed.item == {}
    assert reason in parsed.unreadable


def test_a_list_of_links_answers_each_in_order() -> None:
    links = [f"https://drive.google.com/drive/folders/{FOLDER}", "https://example.com/x"]
    assert [bool(p.unreadable) for p in parse_links(links)] == [False, True]


# ---- the storage rule ------------------------------------------------------------


def _src(**kw: Any) -> KnowledgeSource:
    kw.setdefault("items", domain.items_from_config("gdrive", kw.get("config", {})))
    return KnowledgeSource(
        org="o", namespace="n", name="d", kind="gdrive", source_id=str(uuid.uuid4()), **kw
    )


def _written(monkeypatch: pytest.MonkeyPatch, src: KnowledgeSource) -> dict[str, Any]:
    ch = RecordingCH()
    ch.install(monkeypatch)
    asyncio.run(store.insert_source(src))
    [row] = ch.inserted("knowledge_sources")
    return row


def test_a_source_id_list_config_holds_is_written_as_before(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = _written(
        monkeypatch,
        _src(config={"include": ["docs"]}, items=[_folder()], credential_enc="sealed"),
    )
    assert row["kind"] == row["source_kind"] == "gdrive"
    assert json.loads(row["config_json"]) == {"folder_ids": [FOLDER], "include": ["docs"]}
    assert json.loads(row["items_json"]) == [_folder()]
    assert (row["credential_enc"], row["secret_enc"]) == ("sealed", "")


def test_a_source_with_an_item_id_list_config_cannot_hold_is_written_without_a_kind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    items = [_folder(recursive=True), {"type": "file", "id": FILE}]
    row = _written(monkeypatch, _src(config={"include": ["docs"]}, items=items, secret_enc="s"))
    assert (row["kind"], row["source_kind"]) == ("", "gdrive")
    assert json.loads(row["config_json"]) == {"include": ["docs"]}
    assert json.loads(row["items_json"]) == items
    assert (row["credential_enc"], row["secret_enc"]) == ("", "s")
    # Read back, it is the source N wrote.
    back = store._source(row)
    assert (back.kind, back.items, back.secret_enc) == ("gdrive", items, "s")
    assert domain.include_in(back.config) == ["docs"]


# A build without items writes neither column, or rewrites a row with no items.
@pytest.mark.parametrize("items_json", ["", "[]"])
def test_a_row_written_before_the_items_columns_reads_its_items_from_its_config(
    items_json: str,
) -> None:
    row = {
        "org": "o",
        "namespace": "n",
        "name": "d",
        "kind": "gdrive",
        "source_id": "i",
        "config_json": json.dumps({"folder_ids": [FOLDER], "include": ["docs"]}),
        "credential_enc": "sealed",
        "source_kind": "",
        "items_json": items_json,
        "secret_enc": "",
    }
    src = store._source(row)
    assert (src.kind, src.items, src.credential_enc) == ("gdrive", [_folder()], "sealed")
    assert domain.id_list_config(src) == {"folder_ids": [FOLDER], "include": ["docs"]}


def test_the_credential_is_read_from_the_column_its_source_was_written_to(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.knowledge.app import sources as app
    from server.settings import settings

    monkeypatch.setattr(settings, "jwt_secret", "unit-test-root-secret")
    flat = app._with_credential(_src(config={"include": ["docs"]}, items=[_folder()]), "key")
    assert flat.credential_enc and not flat.secret_enc
    later = app._with_credential(_src(items=[_folder(recursive=True)]), "key")
    assert later.secret_enc and not later.credential_enc
    assert app.credential_of(flat) == app.credential_of(later) == "key"
    # Each column has its own purpose: neither opens under the other's.
    swapped = _src(items=[_folder(recursive=True)], secret_enc=flat.credential_enc)
    assert app.credential_of(swapped) == ""


# ---- the shared-folder listing ---------------------------------------------------


class _Drive:
    """A shared-folder store: subfolders by parent, files by parent and drive."""

    def __init__(self) -> None:
        self.children: dict[str, list[str]] = {}
        self.files: list[dict[str, Any]] = []
        self.requests: list[httpx.Request] = []
        self.missing: set[str] = set()
        # Merged into every folder query's answer: a next page, an unfinished search.
        self.folder_extra: dict[str, Any] = {}

    def answer(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path.startswith("/drive/v3/files/"):
            file_id = path.rsplit("/", 1)[-1]
            found = [f for f in self.files if f["id"] == file_id]
            if file_id in self.missing or not found:
                return httpx.Response(404)
            return httpx.Response(200, json=found[0])
        if path.startswith("/drive/v3/drives/"):
            return httpx.Response(404 if path.rsplit("/", 1)[-1] in self.missing else 200, json={})
        q = request.url.params["q"]
        if "application/vnd.google-apps.folder" in q:
            ids = [c for p, kids in self.children.items() if f"'{p}' in parents" in q for c in kids]
            return httpx.Response(
                200, json={"files": [{"id": c} for c in ids], **self.folder_extra}
            )
        drive = request.url.params.get("driveId", "")
        if drive in self.missing:
            return httpx.Response(404)
        listed = [
            f
            for f in self.files
            if (drive and f.get("driveId") == drive)
            or (not drive and any(f"'{p}' in parents" in q for p in f.get("parents", [])))
        ]
        return httpx.Response(200, json={"files": listed})


@pytest.fixture
def drive(monkeypatch: pytest.MonkeyPatch) -> _Drive:
    d = _Drive()
    client = httpx.AsyncClient(transport=httpx.MockTransport(d.answer))
    monkeypatch.setattr(gdrive, "get_client", lambda: client)
    return d


def _listings(d: _Drive) -> list[httpx.Request]:
    return [r for r in d.requests if r.url.path == "/drive/v3/files"]


def test_a_filter_s_query_quotes_every_value() -> None:
    query = gdrive.filter_query(
        {
            "type": "filter",
            "name_contains": "it's a \\ test",
            "modified_after": "2026-01-31",
            "in": FOLDER,
        },
        [DOCS],
    )
    assert query == (
        f"trashed = false and (mimeType = '{DOCS}') and name contains 'it\\'s a \\\\ test' "
        f"and modifiedTime > '2026-01-31T00:00:00' "
        f"and '{FOLDER}' in parents"
    )


def test_a_listing_names_at_most_fifty_parents_per_query(drive: _Drive) -> None:
    folders = [f"{FOLDER}{n:03d}" for n in range(120)]
    asyncio.run(gdrive.list_all("tok", folders, [DOCS], pace_s=0))
    queries = [r.url.params["q"] for r in _listings(drive)]
    assert [q.count(" in parents") for q in queries] == [50, 50, 20]


def test_a_recursive_folder_walks_its_subfolders(drive: _Drive) -> None:
    drive.children = {FOLDER: ["sub1aaaaaaaaa"], "sub1aaaaaaaaa": ["sub2aaaaaaaaa"]}
    drive.files = [
        {"id": "f1", "name": "a", "mimeType": DOCS, "parents": ["sub2aaaaaaaaa"]},
        {"id": "f2", "name": "b", "mimeType": DOCS, "parents": ["elsewhere"]},
    ]
    listing = asyncio.run(
        items_listing.list_items("tok", [_folder(recursive=True)], ["docs"], pace_s=0)
    )
    assert [f["id"] for f in listing.files] == ["f1"]
    assert listing.folders == {FOLDER, "sub1aaaaaaaaa", "sub2aaaaaaaaa"} and not listing.walk_cut


def test_a_walk_stops_at_its_folder_bound_and_says_so(
    drive: _Drive, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(items_listing, "MAX_WALK_FOLDERS", 3)
    drive.children = {FOLDER: [f"kid{n}aaaaaaaa" for n in range(5)]}
    listing = asyncio.run(
        items_listing.list_items("tok", [_folder(recursive=True)], ["docs"], pace_s=0)
    )
    # The root and three subfolders: the bound counts subfolders.
    assert len(listing.folders) == 4 and listing.walk_cut


def test_a_walk_reads_no_page_past_its_folder_bound(drive: _Drive) -> None:
    # Every answer says another page follows: only the bound stops the reads.
    drive.children = {FOLDER: ["kid0aaaaaaaaa", "kid1aaaaaaaaa"]}
    drive.folder_extra = {"nextPageToken": "more"}
    found, _ = asyncio.run(gdrive.subfolders("tok", [FOLDER], limit=3, pace_s=0))
    assert len(found) == 4 and len(_listings(drive)) == 2


def _walk(**kw: Any) -> items_listing.Listing:
    out = items_listing.Listing()
    asyncio.run(items_listing.walk("tok", [FOLDER], out, pace_s=0, **kw))
    return out


def test_a_walk_stops_at_its_depth_bound_and_says_so(drive: _Drive) -> None:
    chain = [FOLDER] + [f"level{n}aaaaaaa" for n in range(domain.MAX_DEPTH + 1)]
    drive.children = {parent: [child] for parent, child in zip(chain, chain[1:], strict=False)}
    walked = _walk()
    assert len(walked.folders) == domain.MAX_DEPTH + 1 and walked.whole and walked.walk_cut


def test_a_shared_drive_is_listed_on_its_own_corpus(drive: _Drive) -> None:
    drive.files = [{"id": "f1", "name": "a", "mimeType": DOCS, "driveId": DRIVE}]
    item = {"type": "shared_drive", "id": DRIVE}
    listing = asyncio.run(items_listing.list_items("tok", [item], ["docs"], pace_s=0))
    assert [f["id"] for f in listing.files] == ["f1"]
    [request] = _listings(drive)
    assert request.url.params["driveId"] == DRIVE and request.url.params["corpora"] == "drive"


def test_a_shared_drive_the_account_left_lists_nothing_and_says_nothing_is_absent(
    drive: _Drive,
) -> None:
    drive.missing = {DRIVE}
    item = {"type": "shared_drive", "id": DRIVE}
    listing = asyncio.run(items_listing.list_items("tok", [item], ["docs"], pace_s=0))
    assert listing.files == [] and listing.lost_item and listing.notes == ["item 0: http_404"]


def test_single_files_are_read_as_listed_and_a_missing_one_is_noted(drive: _Drive) -> None:
    drive.files = [
        {"id": FILE, "name": "a", "mimeType": DOCS, "modifiedTime": "2026-01-02T00:00:00.000Z"},
        {"id": "sheet1aaaaaa", "name": "s", "mimeType": "application/vnd.google-apps.spreadsheet"},
    ]
    items = [
        {"type": "file", "id": FILE},
        {"type": "file", "id": "gone1aaaaaaa"},
        {"type": "file", "id": "sheet1aaaaaa"},
    ]
    listing = asyncio.run(items_listing.list_items("tok", items, ["docs"], pace_s=0))
    assert [f["id"] for f in listing.files] == [FILE]
    assert listing.notes[0] == "item 1: http_404" and listing.notes[1].startswith("item 2:")
    # An incremental turn lists a file only once it changed after the stamp.
    later = asyncio.run(
        items_listing.list_items(
            "tok", items[:1], ["docs"], modified_after="2026-01-03T00:00:00.000Z", pace_s=0
        )
    )
    assert later.files == []


def test_a_filter_lists_with_its_own_query(drive: _Drive) -> None:
    item = {"type": "filter", "name_contains": "plan", "in": FOLDER, "kinds": ["docs", "pdf"]}
    drive.files = [{"id": "f1", "name": "plan", "mimeType": DOCS, "parents": [FOLDER]}]
    listing = asyncio.run(items_listing.list_items("tok", [item], ["docs", "slides"], pace_s=0))
    assert [f["id"] for f in listing.files] == ["f1"]
    [request] = _listings(drive)
    # The filter's kinds narrow the source's include: pdf is not in it, slides not asked.
    assert f"mimeType = '{DOCS}'" in request.url.params["q"]
    assert "pdf" not in request.url.params["q"] and "presentation" not in request.url.params["q"]


@pytest.mark.parametrize(
    ("state", "reached"),
    [
        ({"parents": [FOLDER], "mimeType": DOCS}, True),
        ({"parents": ["elsewhere"], "mimeType": DOCS}, False),
        ({"parents": ["elsewhere"], "driveId": DRIVE, "mimeType": DOCS}, True),
        ({"parents": [FOLDER], "mimeType": "application/pdf"}, False),
        ({"parents": ["x"], "name": "Q3 Plan", "mimeType": DOCS}, True),
        ({"parents": ["x"], "name": "Budget", "mimeType": DOCS}, False),
    ],
)
def test_what_the_items_reach(state: dict[str, Any], reached: bool) -> None:
    listing = items_listing.Listing(
        folders={FOLDER},
        drives={DRIVE},
        filters=[({"type": "filter", "name_contains": "plan"}, [DOCS])],
        mime_types=[DOCS],
    )
    assert listing.reaches("some-file", state) is reached


def test_a_file_item_is_reached_wherever_it_lives() -> None:
    listing = items_listing.Listing(file_ids={FILE}, mime_types=[DOCS])
    assert listing.reaches(FILE, {"parents": ["elsewhere"], "mimeType": DOCS})


# ---- the probe -------------------------------------------------------------------


def test_a_probe_checks_the_twenty_items_added_last() -> None:
    items = [{"type": "file", "id": f"{FILE}{n:02d}"} for n in range(25)]
    assert source_probes.sample(items) == items[5:]
    assert source_probes.sample(items[:3]) == items[:3]


def test_a_recursive_folder_that_is_a_shared_drive_s_root_comes_back_as_the_drive(
    drive: _Drive, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _token(account: Any) -> str:
        return "tok"

    monkeypatch.setattr(gdrive, "access_token", _token)
    root = {"id": DRIVE, "mimeType": "application/vnd.google-apps.folder", "driveId": DRIVE}
    drive.files = [root]
    key = '{"type": "service_account", "client_email": "a@b", "private_key": "k"}'

    def probe(item: dict[str, Any]) -> list[dict[str, Any]]:
        found = asyncio.run(source_probes.probe("gdrive", {"include": ["docs"]}, key, items=[item]))
        return found.items

    assert probe(_folder(DRIVE, recursive=True)) == [{"type": "shared_drive", "id": DRIVE}]
    # Not recursive, it reads the root's own files, as a folder: nothing widens.
    assert probe(_folder(DRIVE)) == [_folder(DRIVE)]


def test_a_project_s_short_id_is_resolved_to_its_uuid(monkeypatch: pytest.MonkeyPatch) -> None:
    asked: list[list[str]] = []

    async def _ids(api_key: str, slugs: list[str]) -> dict[str, str]:
        asked.append(slugs)
        return {"1a2b3c4d5e6f": PROJECT}

    monkeypatch.setattr(linear, "project_ids", _ids)
    items = [{"type": "project", "slug_id": "1a2b3c4d5e6f"}]
    assert asyncio.run(source_probes.resolve("linear", items, "key")) == [
        {"type": "project", "id": PROJECT}
    ]
    with pytest.raises(source_probes.SourceUnreachable, match="ffffffffffff"):
        asyncio.run(
            source_probes.resolve("linear", [{"type": "project", "slug_id": "ffffffffffff"}], "k")
        )


def test_the_tracker_lookup_sends_short_ids_as_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[dict[str, Any]] = []

    def _answer(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        nodes = [{"id": PROJECT, "slugId": "1a2b3c4d5e6f"}, {"id": "bad", "slugId": "x"}]
        return httpx.Response(200, json={"data": {"projects": {"nodes": nodes}}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(_answer))
    monkeypatch.setattr(linear, "get_client", lambda: client)
    found = asyncio.run(linear.project_ids("lin_api_key123", ["1a2b3c4d5e6f"]))
    assert found == {"1a2b3c4d5e6f": PROJECT}
    assert sent[0]["variables"]["slugs"] == ["1a2b3c4d5e6f"]
    assert "1a2b3c4d5e6f" not in sent[0]["query"]


def test_a_walk_the_pass_ran_out_for_leaves_the_listing_incomplete(drive: _Drive) -> None:
    drive.children = {FOLDER: ["sub1aaaaaaaaa"]}
    listing = asyncio.run(
        items_listing.list_items(
            "tok", [_folder(recursive=True)], ["docs"], pace_s=0, out_of_time=lambda: True
        )
    )
    # Only the folder itself is listed, and the turn keeps its stamp to list again.
    assert listing.folders == {FOLDER} and listing.ran_out


def test_no_item_is_listed_once_the_pass_runs_out(drive: _Drive) -> None:
    items = [{"type": "file", "id": FILE}, {"type": "shared_drive", "id": "0AbCdEfGhIjKlMnOpQr"}]
    listing = asyncio.run(
        items_listing.list_items("tok", items, ["docs"], pace_s=0, out_of_time=lambda: True)
    )
    # Neither the file's metadata nor the drive is asked for.
    assert not [r for r in drive.requests if FILE in r.url.path or "driveId" in r.url.params]
    assert listing.files == [] and listing.ran_out


def test_recursive_folders_are_walked_together_under_one_bound(
    drive: _Drive, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(items_listing, "MAX_WALK_FOLDERS", 3)
    roots = [f"root{n}aaaaaaaaa" for n in range(4)]
    drive.children = {r: [f"{r}kid"] for r in roots}
    listing = asyncio.run(
        items_listing.list_items(
            "tok", [_folder(r, recursive=True) for r in roots], ["docs"], pace_s=0
        )
    )
    # One subfolder query for the four roots' first level, and the bound is the source's.
    assert (
        sum(
            "application/vnd.google-apps.folder" in r.url.params.get("q", "")
            for r in _listings(drive)
        )
        == 1
    )
    assert len(listing.folders) == 4 + 3 and listing.walk_cut


def test_a_tree_exactly_as_deep_as_the_bound_is_not_cut(drive: _Drive) -> None:
    chain = [FOLDER] + [f"level{n}aaaaaaa" for n in range(domain.MAX_DEPTH)]
    drive.children = {parent: [child] for parent, child in zip(chain, chain[1:], strict=False)}
    walked = _walk()
    # Every level is read and the last holds no folder: nothing was left unread.
    assert len(walked.folders) == domain.MAX_DEPTH + 1
    assert walked.whole and not walked.ran_out
    assert not walked.walk_cut


def test_a_walk_stops_between_levels_once_the_pass_runs_out(drive: _Drive) -> None:
    drive.children = {FOLDER: ["sub1aaaaaaaaa"], "sub1aaaaaaaaa": ["sub2aaaaaaaaa"]}
    asked: list[int] = []

    def out_of_time() -> bool:
        asked.append(1)
        return len(asked) > 1

    walked = _walk(out_of_time=out_of_time)
    # The first level is read, the second is not, and the walk says it ran out.
    assert walked.folders == {FOLDER, "sub1aaaaaaaaa"} and walked.ran_out and not walked.walk_cut


def test_a_walk_the_pass_runs_out_for_at_its_depth_bound_asks_no_more(drive: _Drive) -> None:
    chain = [FOLDER] + [f"level{n}aaaaaaa" for n in range(domain.MAX_DEPTH + 1)]
    drive.children = {parent: [child] for parent, child in zip(chain, chain[1:], strict=False)}
    asked: list[int] = []

    def out_of_time() -> bool:
        asked.append(1)
        return len(asked) > domain.MAX_DEPTH

    queries = len(_listings(drive))
    walked = _walk(out_of_time=out_of_time)
    # Every level is read; the check below the last is not, and the walk says it ran out.
    assert len(walked.folders) == domain.MAX_DEPTH + 1 and walked.ran_out
    assert not walked.walk_cut
    assert len(_listings(drive)) - queries == domain.MAX_DEPTH


@pytest.mark.parametrize("answer", [{"nextPageToken": "p2"}, {"incompleteSearch": True}])
def test_any_subfolder_takes_an_unsure_answer_as_holding_one(
    drive: _Drive, answer: dict[str, Any]
) -> None:
    # The page holds no folder, but the store did not say the parent holds none.
    assert asyncio.run(gdrive.any_subfolder("tok", [FOLDER], set())) is False
    drive.folder_extra = answer
    assert asyncio.run(gdrive.any_subfolder("tok", [FOLDER], set())) is True


def test_the_check_below_the_last_level_reads_one_page_per_batch(drive: _Drive) -> None:
    level = [f"wide{n:03d}aaaaaaa" for n in range(120)]
    drive.children = {level[60]: ["deeperaaaaaaa"]}
    before = len(_listings(drive))
    # 50 parents per query: the second batch finds one and the third is never asked.
    assert asyncio.run(gdrive.any_subfolder("tok", level, set(level))) is True
    assert len(_listings(drive)) - before == 2


def test_a_probe_names_a_file_of_a_kind_the_source_does_not_read(
    drive: _Drive, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _token(account: Any) -> str:
        return "tok"

    monkeypatch.setattr(gdrive, "access_token", _token)
    drive.files = [{"id": FILE, "mimeType": "application/vnd.google-apps.spreadsheet"}]
    key = '{"type": "service_account", "client_email": "a@b", "private_key": "k"}'
    item = {"type": "file", "id": FILE}
    found = asyncio.run(source_probes.probe("gdrive", {"include": ["docs"]}, key, items=[item]))
    assert found.foreign == {FILE: "application/vnd.google-apps.spreadsheet"}
    assert found.documents == 0


def test_the_file_lookup_and_listings_ask_the_store_for_no_person(drive: _Drive) -> None:
    drive.files = [{"id": FILE, "mimeType": DOCS, "parents": [FOLDER]}]
    asyncio.run(gdrive.file_state("tok", FILE))
    # Loom keeps no person's address: no owner, sharing or user field is read, by the
    # retirement lookup, a listing or a probe's first page.
    for fields in (drive.requests[-1].url.params["fields"], gdrive._LISTED, gdrive._PAGE_FIELDS):
        assert not any(word in fields.lower() for word in ("owner", "permission", "user"))


def test_the_tracker_lookup_refuses_more_than_one_page_of_short_ids() -> None:
    too_many = [f"{n:012x}" for n in range(linear.PAGE_SIZE + 1)]
    with pytest.raises(ValueError, match="at most 50 short ids"):
        asyncio.run(linear.project_ids("lin_api_key123", too_many))


def test_one_update_s_short_ids_fit_one_tracker_page() -> None:
    # An update looks up its added and its removed short ids together.
    assert 2 * domain.max_items("linear") <= linear.PAGE_SIZE


def test_a_lookup_answer_without_the_projects_list_is_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _answer(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"projects": None}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(_answer))
    monkeypatch.setattr(linear, "get_client", lambda: client)
    with pytest.raises(linear.LinearError):
        asyncio.run(linear.project_ids("lin_api_key123", ["1a2b3c4d5e6f"]))


@pytest.mark.parametrize(
    ("model", "field", "extra"),
    [
        (
            models_sources.KnowledgeSourceAddInput,
            "links",
            {"name": "d", "kind": "gdrive", "credential": "k"},
        ),
        (models_sources.KnowledgeSourceUpdateInput, "add_links", {"name": "d"}),
        (models_sources.KnowledgeSourceUpdateInput, "remove_links", {"name": "d"}),
        (models_sources.KnowledgeSourceParseLinksInput, "links", {}),
    ],
)
def test_every_link_field_takes_links_up_to_the_wire_bound(
    model: type[pydantic.BaseModel], field: str, extra: dict[str, Any]
) -> None:
    longest = "https://" + "a" * (models_sources.MAX_LINK_CHARS - len("https://"))
    model(org="o", namespace="n", **extra, **{field: [longest]})
    with pytest.raises(pydantic.ValidationError):
        model(org="o", namespace="n", **extra, **{field: [longest + "a"]})
