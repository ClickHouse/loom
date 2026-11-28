"""The edits that bring a memory's text up to a rename its link followed: which
spellings are edited, which withhold the whole memory and why, and what the listing
namespace.validate reads returns."""

from __future__ import annotations

import json
from collections import defaultdict
from typing import Any

import pytest

from server.code.domain.anchor import Definition
from server.core.hashing import served_content_sha256
from server.memory.app import code_link_reads, code_renames
from server.memory.domain.code_rename import REASONS, Plan, plan, revised, written_as

RULES = "rules-1"
PATH = "src/io/reader.py"
OLD = "src.io.reader::Reader::read_rows"
NEW = "src.io.reader::Reader::fetch_rows"


def _d(path: str, qn: str) -> Definition:
    return Definition(path=path, qualified_name=qn, name=qn.replace("::", ".").split(".")[-1])


def _link(path: str, qn: str, *, was: tuple[str, str] | None = None, **fields: Any) -> dict:
    """A link at (path, qn), written against `was`: `moved` when that is elsewhere."""
    was_path, was_name = was or (path, qn)
    row = {
        "path": path,
        "qualified_name": qn,
        "state": "moved" if (was_path, was_name) != (path, qn) else "fresh",
        "complete": 1,
        "carried": 0,
        "body_hash": "h1",
        "checked_hash": "h1",
        "fingerprint_version": RULES,
        "anchored_path": was_path,
        "anchored_name": was_name,
        "former": [],
        "code_namespace": "code",
        "suggestion": "",
    }
    return row | fields


RENAMED = _link(PATH, NEW, was=(PATH, OLD))


def _plan(
    text: Any,
    *,
    links: list[dict] | None = None,
    defs: list[Definition] | None = None,
    gone: tuple[str, ...] = (),
    **memory: Any,
) -> Plan:
    by_name: dict[str, list[Definition]] = defaultdict(list)
    for d in [_d(PATH, NEW)] if defs is None else defs:
        by_name[d.name].append(d)
    return plan(
        text, memory, [RENAMED] if links is None else links, by_name, gone=gone, rules=RULES
    )


def _revised(text: str, **kw: Any) -> str:
    """`text` with its planned edits applied; the plan offers some."""
    planned = _plan(text, **kw)
    assert planned.reason == "" and planned.edits, planned
    return revised(text, planned.edits)


@pytest.mark.parametrize(
    ("text", "want"),
    [
        ("`read_rows` retries twice.", "`fetch_rows` retries twice."),
        ("`read_rows()` retries twice.", "`fetch_rows()` retries twice."),
        ("`Reader.read_rows` retries.", "`Reader.fetch_rows` retries."),
        ("`Reader::read_rows` retries.", "`Reader::fetch_rows` retries."),
        ("Reader::read_rows retries.", "Reader::fetch_rows retries."),
        ("Reader.read_rows() retries.", "Reader.fetch_rows() retries."),
        ("Call read_rows() once.", "Call fetch_rows() once."),
        (
            "`src.io.reader.Reader.read_rows` retries.",
            "`src.io.reader.Reader.fetch_rows` retries.",
        ),
        (
            "`read_rows` retries; so does Reader.read_rows().",
            "`fetch_rows` retries; so does Reader.fetch_rows().",
        ),
    ],
)
def test_every_explicit_spelling_is_edited_at_its_own_depth_and_separator(
    text: str, want: str
) -> None:
    planned = _plan(text)
    assert revised(text, planned.edits) == want
    assert planned.sha256 == served_content_sha256(text)
    for e in planned.edits:
        assert text[e.start : e.end] == e.old


def test_text_outside_the_edits_is_byte_identical() -> None:
    text = "Ünïcode — get_read_rows_fast and read_rowsx stay;\n\t`read_rows` goes.\r\n"
    planned = _plan(text)
    (edit,) = planned.edits
    assert (edit.old, edit.new) == ("read_rows", "fetch_rows")
    assert text[: edit.start] + edit.new + text[edit.end :] == revised(text, planned.edits)
    assert revised(text, planned.edits) == text.replace("`read_rows`", "`fetch_rows`")


def test_planning_the_revised_text_again_offers_no_edit() -> None:
    text = "`Reader.read_rows` retries; `read_rows` backs off."
    after = _revised(text)
    # with the links not yet made again, and once the link pass has made them from it
    assert _plan(after).edits == ()
    assert _plan(after, links=[_link(PATH, NEW)]) == Plan(reason="no_code_token")


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        # a word of the prose, which the link pass reads inside the memory's files
        ("read_rows retries twice.", "prose_occurrence"),
        ("`read_rows` retries; read_rows backs off.", "prose_occurrence"),
        ("pass --read_rows to `read_rows`", "prose_occurrence"),
        ("read_rows.py holds `read_rows`", "prose_occurrence"),
        # quoted code
        ("see https://example.org/api/read_rows for `read_rows`", "quoted_or_literal"),
        ('the "read_rows" key feeds `read_rows`', "quoted_or_literal"),
        ("```\nread_rows(conn)\n```\n`read_rows` is hot", "quoted_or_literal"),
        ("`rows = read_rows(conn)` then `read_rows`", "quoted_or_literal"),
        # the sentence is about the name, or about a point in time
        ("We should rename `read_rows` to something shorter.", "about_naming"),
        ("`read_rows`, formerly the slow path, retries.", "about_naming"),
        ("`read_rows` was called from the loader.", "about_naming"),
        ("`read_rows` is a bad name for this.", "about_naming"),
        ("Never name a helper `read_rows`: it collides with the fixture.", "about_naming"),
        ("The loader names it `read_rows`.", "about_naming"),
        ("The naming of `read_rows` follows the loader.", "about_naming"),
        ("The helper is called `read_rows`.", "about_naming"),
        ("`read_rows` is an alias of the loader.", "about_naming"),
        ("`read_rows` regressed in a1b2c3d4e5.", "dated_claim"),
        ("`read_rows` retries since v2.3.", "dated_claim"),
        ("`read_rows` retries, see #1234.", "dated_claim"),
        ("In March `read_rows` retried twice.", "dated_claim"),
        # the new name is there already
        ("`read_rows` calls `fetch_rows`.", "new_name_present"),
        ("`read_rows` and the fetch_rows helper.", "new_name_present"),
        # written through its receiver, with no file that says whose
        ("`self.read_rows()` leaks.", "receiver_form"),
    ],
)
def test_a_spelling_or_sentence_that_cannot_be_swapped_withholds_the_memory(
    text: str, reason: str
) -> None:
    assert _plan(text) == Plan(reason=reason)


def test_a_cue_in_another_sentence_does_not_withhold() -> None:
    text = "The loader was renamed in 2019. `read_rows` retries twice.\nSee #12 for the loader."
    assert _revised(text) == text.replace("read_rows", "fetch_rows")


def test_a_longer_word_that_holds_a_cue_or_a_call_of_the_code_does_not_withhold() -> None:
    for text in (
        "`read_rows` reads the filename, the hostname and the namespace.",
        "The worker calls `read_rows` twice.",
    ):
        assert _revised(text) == text.replace("read_rows", "fetch_rows")


def test_a_cue_inside_the_edited_name_itself_does_not_withhold() -> None:
    was, now = "src/v2/reader2019.py", "src/v2/read2019.py"
    link = _link(now, "", was=(was, ""))
    text = "`src/v2/reader2019.py` holds the retry loop."
    planned = _plan(text, links=[link], defs=[], gone=(was,), files=["read2019.py"])
    assert revised(text, planned.edits) == "`src/v2/read2019.py` holds the retry loop."


def test_a_plain_word_or_a_part_of_a_longer_name_is_not_an_occurrence() -> None:
    # `close` renamed: the prose word stays and withholds; a longer name is another name
    link = _link(PATH, "src.io.reader::Reader::shut", was=(PATH, "src.io.reader::Reader::close"))
    defs = [_d(PATH, "src.io.reader::Reader::shut")]
    assert _plan("`close` runs when files close.", links=[link], defs=defs).reason == (
        "old_name_remains"
    )
    text = "`close` runs; close_all and unclose do not."
    assert _revised(text, links=[link], defs=defs) == "`shut` runs; close_all and unclose do not."


def test_a_receiver_form_is_edited_when_the_files_name_its_file() -> None:
    assert _revised("`self.read_rows()` leaks.", files=[PATH]) == "`self.fetch_rows()` leaks."
    assert _revised("cls.read_rows() leaks.", files=["reader.py"]) == "cls.fetch_rows() leaks."


def test_an_old_name_another_definition_still_bears_withholds() -> None:
    homonym = _d("src/io/writer.py", "src.io.writer::Writer::read_rows")
    defs = [_d(PATH, NEW), homonym]
    assert _plan("`read_rows` retries.", defs=defs).reason == "old_name_still_defined"
    # the spelling decides: `Reader.read_rows` names no other definition
    assert _revised("`Reader.read_rows` retries.", defs=defs) == "`Reader.fetch_rows` retries."
    # a definition in a scope the index numbers (a second `impl`) is still that name
    numbered = [_d(PATH, NEW), _d(PATH, "src.io.reader::Reader#2::read_rows")]
    assert _plan("`Reader.read_rows` retries.", defs=numbered).reason == "old_name_still_defined"


def test_a_name_another_definition_is_written_by_is_left_alone() -> None:
    other = _d("src/io/writer.py", "src.io.writer::Writer::read_rows")
    text = "`Writer.read_rows` buffers; `Reader.read_rows` retries."
    links = [RENAMED, _link("src/io/writer.py", other.qualified_name)]
    assert _revised(text, links=links, defs=[_d(PATH, NEW), other]) == (
        "`Writer.read_rows` buffers; `Reader.fetch_rows` retries."
    )


def test_a_new_name_that_does_not_resolve_to_the_link_withholds() -> None:
    homonym = _d("src/io/writer.py", "src.io.writer::Writer::fetch_rows")
    assert _plan("`read_rows` retries.", defs=[_d(PATH, NEW), homonym]).reason == "would_unlink"
    # inside the memory's files it is one definition
    assert _plan("`read_rows` retries.", defs=[_d(PATH, NEW), homonym], files=[PATH]).edits
    # the index does not have the new definition yet
    assert _plan("`read_rows` retries.", defs=[]).reason == "would_unlink"


def test_an_edit_that_would_drop_another_link_withholds() -> None:
    # the update relinks the memory from its text: a link no name of it resolves to goes
    other = _link("src/io/writer.py", "src.io.writer::Writer::flush")
    assert _plan("`read_rows` retries.", links=[RENAMED, other]).reason == "would_unlink"
    defs = [_d(PATH, NEW), _d("src/io/writer.py", "src.io.writer::Writer::flush")]
    text = "`read_rows` retries before `Writer.flush`."
    assert _plan(text, links=[RENAMED, other], defs=defs).reason == "about_naming"  # "before"
    text = "`read_rows` retries and `Writer.flush` runs."
    assert _revised(text, links=[RENAMED, other], defs=defs).startswith("`fetch_rows`")


@pytest.mark.parametrize(
    ("fields", "reason"),
    [
        ({"state": "pending", "path": "", "qualified_name": "pkg.later"}, "link_not_settled"),
        ({"state": "changed"}, "link_not_settled"),
        ({"state": "unresolved"}, "link_not_settled"),
        ({"state": "missing"}, "link_not_settled"),
        ({"carried": 1}, "link_not_settled"),
        ({"checked_hash": "h2"}, "link_not_settled"),
        ({"fingerprint_version": "rules-0"}, "link_not_settled"),
        ({"complete": 0}, "incomplete"),
    ],
)
def test_a_link_that_is_not_settled_or_was_cut_withholds(fields: dict, reason: str) -> None:
    other = _link("src/io/writer.py", "src.io.writer::Writer::flush") | fields
    assert _plan("`read_rows` retries.", links=[RENAMED, other]).reason == reason


def test_an_ordinal_name_withholds() -> None:
    numbered = "src.io.reader::Reader#2::fetch_rows"
    for was, now in ((OLD + "#2", NEW), (OLD, NEW + "#2"), (OLD, numbered)):
        link = _link(PATH, now, was=(PATH, was))
        assert _plan("`Reader.read_rows` retries.", links=[link]).reason == "overloaded"


def test_a_value_that_is_not_one_string_is_not_text() -> None:
    assert _plan(None) == Plan(reason="not_text")
    # what the listing passes for a stored object, list or number
    for value in ({"claim": "`read_rows` retries."}, ["`read_rows`"], 7):
        assert code_renames.link_pass.claim_of(json.dumps(value)) is None


def test_the_old_name_in_the_key_or_a_tag_withholds() -> None:
    text = "`read_rows` retries."
    assert _plan(text, memory_key="repo/lesson/read_rows-retry").reason == "named_in_key_or_tags"
    assert _plan(text, tags=["repo:x", "Read_Rows"]).reason == "named_in_key_or_tags"
    assert _plan(text, memory_key="repo/lesson/read_rows_twice", tags=["thread_rows"]).edits


def test_a_dated_memory_withholds() -> None:
    for column in ("valid_to", "invalid_at", "temporal_anchor"):
        assert _plan("`read_rows` retries.", **{column: "2031-01-01 00:00:00"}).reason == (
            "dated_claim"
        )


def test_one_name_two_links_wrote_withholds() -> None:
    # two definitions moved apart under one written name
    a = _link("src/a.py", "src.a::load_all", was=("src/a.py", "src.a::load"))
    b = _link("src/b.py", "src.b::load_some", was=("src/b.py", "src.b::load"))
    defs = [_d("src/a.py", "src.a::load_all"), _d("src/b.py", "src.b::load_some")]
    assert _plan("`load` is slow.", links=[a, b], defs=defs).reason == "names_several"


def test_a_scope_change_edits_only_the_spellings_that_name_the_old_scope() -> None:
    # the class was renamed and its own link followed it: the bare name still holds
    link = _link(PATH, "src.io.reader::Loader", was=(PATH, "src.io.reader::Reader"))
    defs = [_d(PATH, "src.io.reader::Loader")]
    assert _revised("`Reader` wraps the socket.", links=[link], defs=defs) == (
        "`Loader` wraps the socket."
    )
    # a member written through the old class is not this link's to rename
    assert _plan("`Reader` wraps; `Reader.get` reads.", links=[link], defs=defs).reason == (
        "old_name_remains"
    )
    # moved to another module under its own name: only a qualified spelling changes
    moved = _link(
        "src/net/util.py", "src.net.util::backoff", was=("src/io/util.py", "src.io.util::backoff")
    )
    defs = [_d("src/net/util.py", "src.net.util::backoff")]
    assert _plan("`backoff` doubles.", links=[moved], defs=defs) == Plan(reason="no_code_token")
    assert _revised("`io.util.backoff` doubles.", links=[moved], defs=defs) == (
        "`net.util.backoff` doubles."
    )
    text = "`io.util.backoff` doubles; io.util.backoff is shared."
    assert _plan(text, links=[moved], defs=defs).reason == "old_name_remains"


def test_the_module_a_definition_left_withholds_where_no_edit_reaches_it() -> None:
    was, now = "src/io/util.py", "src/net/util.py"
    moved = _link(now, "src.net.util::backoff", was=(was, "src.io.util::backoff"))
    defs = [_d(now, "src.net.util::backoff")]
    for text, reason in (
        ("`io.util.backoff` doubles; `from src.io.util import backoff`.", "quoted_or_literal"),
        ("`io.util.backoff` doubles; it lives in the io.util module.", "old_name_remains"),
        ("`io.util.backoff` doubles; see `src.io.util`.", "old_name_remains"),
        ("`backoff` doubles; it lives in src::io::util.", "old_name_remains"),
    ):
        assert _plan(text, links=[moved], defs=defs).reason == reason, text
    # the part both modules end in is not the old one, and one segment may be a word
    text = "`io.util.backoff` doubles; util and io hold the rest."
    assert _revised(text, links=[moved], defs=defs).startswith("`net.util.backoff`")
    # two definitions that left one module are both edited: each covers its own module
    jitter = _link(now, "src.net.util::jitter", was=(was, "src.io.util::jitter"))
    text = "`src.io.util.backoff` and `src.io.util.jitter` double."
    both = [*defs, _d(now, "src.net.util::jitter")]
    assert _revised(text, links=[moved, jitter], defs=both) == (
        "`src.net.util.backoff` and `src.net.util.jitter` double."
    )


def test_a_renamed_file_edits_its_whole_path_once_the_old_one_is_gone() -> None:
    was, now = "src/io/reader.py", "src/io/read.py"
    link = _link(now, "src.io.read::Reader::get", was=(was, "src.io.reader::Reader::get"))
    defs = [_d(now, "src.io.read::Reader::get")]
    kw: dict[str, Any] = {"links": [link], "defs": defs, "gone": (was,)}
    text = "`Reader.get` in `src/io/reader.py` retries (see src/io/reader.py)."
    assert _revised(text, **kw) == "`Reader.get` in `src/io/read.py` retries (see src/io/read.py)."
    assert _revised("`reader.Reader.get` retries.", **kw) == "`read.Reader.get` retries."
    for text, reason in (
        # the old file is still there: the text may be about the file
        ("`Reader.get` in `src/io/reader.py` retries.", "old_name_remains"),
    ):
        assert _plan(text, links=[link], defs=defs).reason == reason
    for text, reason in (
        ("`Reader.get` at src/io/reader.py:120 retries.", "old_name_remains"),
        ("`Reader.get` in reader.py retries.", "old_name_remains"),
        ("`Reader.get` in lib/src/io/reader.py retries.", "old_name_remains"),
        ("`python src/io/reader.py` runs `Reader.get`.", "quoted_or_literal"),
        ("see https://example.org/src/io/reader.py for `Reader.get`", "quoted_or_literal"),
        ("`src/io/reader.py` became `src/io/read.py`.", "new_name_present"),
    ):
        assert _plan(text, **kw).reason == reason, text


def test_files_and_labels_that_name_the_old_path_withhold() -> None:
    was, now = "src/io/reader.py", "src/io/read.py"
    link = _link(now, "", was=(was, ""))
    text = "`src/io/reader.py` holds the retry loop."
    kw: dict[str, Any] = {"links": [link], "defs": [], "gone": (was,)}
    # memory.update keeps `files`: the old entry would stay, and the file link with it
    assert _plan(text, files=[was], **kw).reason == "named_in_files"
    assert _plan(text, files=[now], memory_key="repo/file/src/io/reader.py", **kw).reason == (
        "named_in_key_or_tags"
    )
    # a file moved under its own bare name is still what `files` names
    moved = _link("lib/reader.py", "", was=("src/reader.py", ""))
    kw = {"links": [moved], "defs": [], "gone": ("src/reader.py",)}
    assert _revised("`src/reader.py` holds it.", files=["reader.py"], **kw) == (
        "`lib/reader.py` holds it."
    )
    # no file of the memory names the file link's path any more
    assert _plan("`src/reader.py` holds it.", files=[], **kw).reason == "would_unlink"


def test_a_link_back_where_it_was_written_or_reanchored_gets_no_edit() -> None:
    # renamed and renamed back: the text names it
    back = _link(PATH, OLD, state="moved", anchored_name=NEW, former=[_former(PATH, OLD)])
    assert _plan("`read_rows` retries.", links=[back], defs=[_d(PATH, OLD)]) == (
        Plan(reason="no_code_token")
    )
    # re-anchored where the code is by a `holds` verdict: no move is planned from it
    held = _link(PATH, NEW, former=[_former(PATH, OLD)])
    assert held["state"] == "fresh"
    assert _plan("`read_rows` retries.", links=[held]) == Plan(reason="no_code_token")
    # beside a renamed link, the update would drop it: its old name resolves nowhere
    other = _link("src/a.py", "src.a::load_all", was=("src/a.py", "src.a::load"))
    defs = [_d(PATH, NEW), _d("src/a.py", "src.a::load_all")]
    text = "`read_rows` retries and `load` is slow."
    assert _plan(text, links=[held, other], defs=defs).reason == "would_unlink"


def _former(path: str, name: str) -> dict[str, str]:
    return {"path": path, "name": name, "hash": "h1"}


def test_written_as_is_the_oldest_identity_of_the_links_own_kind() -> None:
    link = _link(PATH, NEW, was=(PATH, "src.io.reader::Reader::load"))
    assert written_as(link) == (PATH, "src.io.reader::Reader::load")
    link["former"] = [_former(PATH, OLD), _former(PATH, "src.io.reader::Reader::load")]
    assert written_as(link) == (PATH, OLD)
    # a definition link that replaced a file link was never that file
    link["former"] = [_former(PATH, "")]
    assert written_as(link) == (PATH, "src.io.reader::Reader::load")


def test_every_reason_is_one_the_plan_can_give() -> None:
    assert len(set(REASONS)) == len(REASONS)
    assert REASONS[0] == "not_text" and REASONS[-1] == "no_code_token"


# ---- the listing namespace.validate reads ----------------------------------------------


def _head(mid: str, text: Any, **fields: Any) -> dict[str, Any]:
    head = {
        "mid": mid,
        "namespace": "ns",
        "memory_key": f"k/{mid}",
        "user_id": "-",
        "value": json.dumps(text),
        "tags": ["repo:x"],
        "files": [],
        "sha": "s",
        "status": "active",
        "tombstoned_at": None,
        "sensitivity": "normal",
    }
    return head | fields


@pytest.fixture
def listing(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """A store of heads and links, an index and one readable repository."""
    world: dict[str, Any] = {"heads": [], "links": [], "defs": [_d(PATH, NEW)], "live": {}}
    monkeypatch.setattr(code_renames, "fingerprint_version", lambda: RULES)
    monkeypatch.setattr(code_link_reads, "inputs_now", lambda org, memory: "inputs")

    async def stale_links(org: str, ns: str, keys: Any = None, *, limit: int, states: Any) -> Any:
        world["asked"] = (keys, limit, tuple(states))
        moved = [
            a for a in world["links"] if a["state"] == "moved" and a["code_namespace"] == "code"
        ]
        return [{"memory_id": a["memory_id"], "code_namespace": "code"} for a in moved]

    async def memory_heads(org: str, ids: list[str]) -> list[dict[str, Any]]:
        return [h for h in world["heads"] if h["mid"] in ids]

    async def for_memories(org: str, ids: list[str]) -> list[dict[str, Any]]:
        return [a for a in world["links"] if a["memory_id"] in ids]

    async def candidates(org: str, cns: str, *, names: list[str]) -> list[Definition]:
        world["names"] = names
        return [d for d in world["defs"] if d.name in names]

    async def live_paths(org: str, cns: str, refs: list[str]) -> dict[str, str]:
        return {r: world["live"].get(r, r) for r in refs if r not in world.get("gone", ())}

    monkeypatch.setattr(code_link_reads, "stale_links", stale_links)
    monkeypatch.setattr(code_renames.store, "memory_heads", memory_heads)
    monkeypatch.setattr(code_renames.store, "for_memories", for_memories)
    monkeypatch.setattr(code_renames.code, "candidates", candidates)
    monkeypatch.setattr(code_renames.code, "live_paths", live_paths)
    return world


def _stored(
    world: dict[str, Any], mid: str, text: Any, link: dict | None = None, **head: Any
) -> None:
    world["heads"].append(_head(mid, text, **head))
    world["links"].append((link or RENAMED) | {"memory_id": mid, "inputs_hash": "inputs"})


@pytest.mark.asyncio
async def test_the_listing_gives_each_memory_its_edits_or_its_reason(listing: dict) -> None:
    _stored(listing, "m1", "`read_rows` retries twice.")
    _stored(listing, "m2", "read_rows retries twice.")
    _stored(listing, "m3", {"claim": "`read_rows` retries twice."})
    # names nothing old: its text holds as it is, and it is not listed
    _stored(listing, "m4", "`Reader` wraps the socket.")
    rows = await code_renames.planned("o", "ns", ["k"])
    assert rows == [
        {
            "memory_key": "k/m1",
            "memory_id": "m1",
            "edits": [{"start": 1, "end": 10, "old": "read_rows", "new": "fetch_rows"}],
            "content_sha256": served_content_sha256("`read_rows` retries twice."),
        },
        {"memory_key": "k/m2", "memory_id": "m2", "reason": "prose_occurrence"},
        {"memory_key": "k/m3", "memory_id": "m3", "reason": "not_text"},
    ]
    assert listing["asked"] == (["k"], code_renames.SCANNED, ("moved",))
    # the index is asked for the old and new bare names and what the text mentions
    assert {"read_rows", "fetch_rows"} <= set(listing["names"])


@pytest.mark.asyncio
async def test_the_listing_reads_no_memory_it_may_not_quote_or_serve(listing: dict) -> None:
    _stored(listing, "m1", "`read_rows` retries.", sensitivity="high")
    _stored(listing, "m2", "`read_rows` retries.", status="superseded")
    _stored(listing, "m3", "`read_rows` retries.", tombstoned_at="2031-01-01 00:00:00")
    # its links were made from an earlier value: the link pass has not read this one
    _stored(listing, "m4", "`read_rows` retries.")
    listing["links"][-1]["inputs_hash"] = "older"
    assert await code_renames.planned("o", "ns") == []


@pytest.mark.asyncio
async def test_the_listing_gives_no_row_for_a_repository_the_caller_may_not_read(
    listing: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stored(listing, "m1", "`read_rows` retries.", link=RENAMED | {"code_namespace": "secret"})
    assert await code_renames.planned("o", "ns") == []
    # through the real read: nothing is read of a namespace with no readable repository
    monkeypatch.setattr(code_link_reads, "stale_links", _REAL_STALE_LINKS)
    monkeypatch.setattr(code_link_reads.code, "trunk_namespace", lambda org, ns: "secret")

    async def none_readable(org: str, namespaces: set[str]) -> set[str]:
        return set()

    async def unread(*a: Any, **kw: Any) -> Any:
        raise AssertionError("no link, memory or index is read")

    monkeypatch.setattr(code_link_reads, "readable_namespaces", none_readable)
    for name in ("stale_of_served", "memory_heads", "for_memories"):
        monkeypatch.setattr(code_renames.store, name, unread)
    monkeypatch.setattr(code_renames.code, "candidates", unread)
    assert await code_renames.planned("o", "ns") == []


_REAL_STALE_LINKS = code_link_reads.stale_links
_REAL_LIVE_PATHS = code_renames.code.live_paths


@pytest.mark.asyncio
async def test_the_listing_asks_the_index_which_old_paths_are_gone(listing: dict) -> None:
    was, now = "src/io/reader.py", "src/io/read.py"
    link = _link(now, "src.io.read::Reader::get", was=(was, "src.io.reader::Reader::get"))
    listing["defs"] = [_d(now, "src.io.read::Reader::get")]
    _stored(listing, "m1", "`Reader.get` in `src/io/reader.py` retries.", link=link)
    (row,) = await code_renames.planned("o", "ns")
    assert row["reason"] == "old_name_remains"  # the index still has the old file
    listing["gone"] = (was,)
    (row,) = await code_renames.planned("o", "ns")
    assert [(e["old"], e["new"]) for e in row["edits"]] == [(was, now)]
    # a bare name the index resolves to another file is not the old path
    listing["gone"], listing["live"] = (), {was: "other/src/io/reader.py"}
    (row,) = await code_renames.planned("o", "ns")
    assert [(e["old"], e["new"]) for e in row["edits"]] == [(was, now)]


class _Files:
    """The file table of an index, answering as the store's reads ask it."""

    def __init__(self, live: set[str]) -> None:
        self.live = live
        self.asked: list[int] = []

    async def query_all(self, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        self.asked.append(len(params["paths"]))
        return [{"path": p} for p in params["paths"] if p in self.live]


@pytest.mark.asyncio
async def test_an_old_path_past_the_store_reads_cap_is_still_asked_for(
    listing: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The store answers for 500 paths of one read: a report with more old paths than
    that asks in slices, so the last path, which the head still has, is not read as gone."""
    from server.code.store.index import CodeRepo

    last = 500
    for i in range(last + 1):
        was, now = f"src/old/f{i:03}.py", f"src/new/f{i:03}.py"
        link = _link(now, "", was=(was, ""))
        _stored(listing, f"m{i:03}", f"`{was}` holds it.", link=link, files=[f"f{i:03}.py"])
    files = _Files({f"src/old/f{last}.py"})
    monkeypatch.setattr(code_renames.code, "live_paths", _REAL_LIVE_PATHS)
    monkeypatch.setattr(code_renames.code, "CodeRepo", lambda: CodeRepo(files))
    rows = {r["memory_id"]: r for r in await code_renames.planned("o", "ns")}
    assert files.asked == [500, 1]
    assert rows[f"m{last}"]["reason"] == "old_name_remains"
    assert [(e["old"], e["new"]) for e in rows["m000"]["edits"]] == [
        ("src/old/f000.py", "src/new/f000.py")
    ]
    assert sum("edits" in r for r in rows.values()) == last
