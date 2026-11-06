"""Wire models of `knowledge.source.*`: the document sources a repository's
maintainers register for Loom to pull from. No output carries a credential."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class KnowledgeSourceProbe(BaseModel):
    """What a probe that succeeded found; a probe that fails refuses the call."""

    # Documents the source lists now: at most 1000 (gdrive, recording), each
    # project's first page (linear), the listed pages and the first 100 pages of each
    # of up to three databases (notion).
    documents: int = 0
    # True when more documents match than were listed, so `documents` is a lower bound.
    more: bool = False
    # Up to five document titles, most recently changed first.
    sample: list[str] = Field(default_factory=list)


class KnowledgeSourceSync(BaseModel):
    """A source's sync state: the request waiting for the worker, and what its last
    pass over the source did."""

    # When `knowledge.source.sync_now` asked for a pass the worker has not run yet,
    # ISO UTC; '' = none waiting.
    requested_at: str = ""
    # When the worker last finished a turn on the source, ISO UTC; '' = never.
    last_synced_at: str = ""
    # How that turn ended: ok, failed, rate_limited or credential_invalid; '' = never run.
    status: str = ""
    # Files the turn's listing returned (every file on a full listing, else those
    # changed since the last complete one).
    documents_seen: int = 0
    # Documents the turn wrote: new or changed files.
    changed: int = 0
    # Documents the turn retired: files the source no longer has.
    removed: int = 0
    # Files not ingested: the store gave no https link, no extractor serves their
    # format, the extractor or the source refused them, they are over
    # LOOM_KNOWLEDGE_SOURCE_SYNC_MAX_FILE_BYTES, or they were given up after three
    # passes on the same revision.
    unconverted: int = 0
    # Why, as reason -> files: no_address (no https link), no_extractor, too_large,
    # http_<status> (the source refused or failed that file), unreachable,
    # extraction_failed:<code>, extractor_unavailable, fetch_failed:<error>,
    # write_refused:<code>, write_failed:<error>.
    unconverted_reasons: dict[str, int] = Field(default_factory=dict)
    # Files not written because a live document at their address is another writer's
    # or another source's.
    skipped: int = 0
    # Why the turn failed (an error class and HTTP status, `http_403`, never the
    # source's response text), or why a turn that did not fail is incomplete (the
    # pass bound, an incomplete listing); '' when neither.
    error: str = ""
    # Documents whose stored revision is not yet mined for decisions, as of the last pass.
    pending_extract: int = 0
    # When the worker last mined the source's written documents for decisions, ISO
    # UTC; '' = never.
    last_extracted_at: str = ""
    # Documents that last mining turn read through, each with or without a decision.
    extracted: int = 0
    # Decision rows it tagged source-changed rather than rewriting them (pinned,
    # attested, or with a recorded outcome): their document now says otherwise.
    flagged_pinned: int = 0
    # Documents given up on: three failed tries, or too large for a pass; tried again
    # once the sync writes them again.
    unextracted: int = 0
    # Why an extraction failed: not_run (it could not run on the document, such as one
    # with no chunks for the current embedding model), refused (a read or write it
    # needed was refused for a reason of the moment, or failed), out_of_time (its model
    # reads ran past the pass's read deadline), too_large_for_pass (it ran out of a
    # whole pass's read time) or failed:<error>; or why documents wait: over_cap,
    # switched_off, switch_unread, no_model, held, held:<code> or pass_bound. '' when
    # none.
    extract_error: str = ""


class KnowledgeSourceEntry(BaseModel):
    # The source's name, unique in the namespace.
    name: str
    # The source's kind (`gdrive`: a shared folder of docs, slides, decks, Word documents
    # and PDFs; `linear`: a project tracker's project documents; `notion`: a page store's
    # pages and databases; `recording`: the transcripts of a meeting store's recordings).
    kind: str
    # The ids and filters the source reads; never a credential.
    config: dict[str, Any] = Field(default_factory=dict)
    # Token subject of the maintainer who added it; '' with auth off.
    created_by: str = ""
    # When it was added, ISO UTC.
    created_at: str = ""
    # The source's sync state: a waiting request and the last pass.
    sync: KnowledgeSourceSync = Field(default_factory=KnowledgeSourceSync)


class KnowledgeSourceListInput(BaseModel):
    """The document sources registered on a repository's code namespace, with each
    one's sync state. Any reader of the repository may list them; the credential
    is never returned."""

    org: str = Field(..., description="The org.")
    namespace: str = Field(..., description="The repository's trunk code namespace.")


class KnowledgeSourceListOutput(BaseModel):
    # The namespace's active sources, by name.
    sources: list[KnowledgeSourceEntry] = Field(default_factory=list)
    # Whether this deployment runs the sync worker (LOOM_KNOWLEDGE_SOURCE_SYNC_ENABLED);
    # false: sources are stored and probed, but nothing pulls from them.
    enabled: bool = False


class KnowledgeSourceAddInput(BaseModel):
    """Register a document source on a repository's trunk code namespace: a kind,
    the ids it reads and a read-only credential. A namespace admin (the
    repository's maintainer) may. The credential is probed (a check and a listing
    of each folder or project) before anything is stored; a probe that fails stores nothing. A name
    is refused while documents the sync worker wrote for an earlier source of that
    name are live. The credential is stored encrypted and never returned."""

    org: str = Field(..., description="The org.")
    namespace: str = Field(..., description="The repository's trunk code namespace.")
    name: str = Field(
        ...,
        description="Source name: 2-32 lower-case letters, digits, - or _, starting with a "
        "letter; trimmed and lower-cased.",
    )
    kind: str = Field(
        ...,
        description="The source kind: `gdrive` (a shared folder), `linear` (a project "
        "tracker's project documents), `notion` (a page store's pages and databases) or "
        "`recording` (a meeting store's transcripts).",
    )
    config: dict[str, Any] = Field(
        ...,
        description="The ids and filters the source reads, per kind; gdrive: {folder_ids: "
        "[1-20 folder ids], include: [docs, slides, pdf, pptx, docx] (default all)}; linear: "
        "{project_ids: [1-20 project ids, UUIDs]}; notion: {page_ids: [...], database_ids: "
        "[...]} (1-20 ids in all); recording: {provider: meet | zoom, ids: "
        "[1-20 space ids (meet) or meeting ids (zoom)]}.",
    )
    credential: str = Field(
        ...,
        description="The source's read-only credential (gdrive: a service account's JSON key, "
        "with the folders shared to the account read-only; linear: a read-only API key of an "
        "account that sees the projects; notion: an integration token with read content "
        "access, the pages and databases shared to it; recording: for meet, an "
        "authorized_user JSON file with the meetings.space.readonly scope only; for zoom, a "
        "server-to-server app's {account_id, client_id, client_secret} with read-only "
        "scopes); stored encrypted, never returned.",
    )
    dry_run: bool = Field(False, description="Probe and write nothing.")


class KnowledgeSourceAddOutput(BaseModel):
    # The registered source's name, normalised.
    name: str
    # The source kind, normalised.
    kind: str
    # The repository's trunk code namespace the source was registered on.
    namespace: str
    # The config as stored.
    config: dict[str, Any] = Field(default_factory=dict)
    # What the probe with the credential found.
    probe: KnowledgeSourceProbe = Field(default_factory=KnowledgeSourceProbe)
    # True when the call only probed.
    dry_run: bool = False


class KnowledgeSourceInspectInput(BaseModel):
    """One registered document source: its config, its sync state and the
    documents it wrote (the 200 most recently ingested). Any reader of the
    repository may; the credential is never returned."""

    org: str = Field(..., description="The org.")
    namespace: str = Field(..., description="The repository's trunk code namespace.")
    name: str = Field(..., description="The source name.")


class KnowledgeSourceDocument(BaseModel):
    # The document's address in loom.documents (the source's own link to it).
    source_uri: str
    # The document's title.
    title: str = ""
    # The source's own revision stamp of the ingested copy.
    source_revision: str = ""
    # When the copy was ingested, ISO UTC.
    ingested_at: str = ""


class KnowledgeSourceInspectOutput(BaseModel):
    # The source's name.
    name: str
    # The source's kind.
    kind: str
    # The ids and filters the source reads; never a credential.
    config: dict[str, Any] = Field(default_factory=dict)
    # Token subject of the maintainer who added it; '' with auth off.
    created_by: str = ""
    # When it was added, ISO UTC.
    created_at: str = ""
    # The source's sync state: a waiting request and the last pass.
    sync: KnowledgeSourceSync = Field(default_factory=KnowledgeSourceSync)
    # The source's live documents, most recently ingested first, at most 200.
    documents: list[KnowledgeSourceDocument] = Field(default_factory=list)


class KnowledgeSourceCheckInput(BaseModel):
    """Probe a registered document source with its stored credential now (a check
    and a listing of each folder or project), nothing written. A namespace admin
    may, since the probe spends the source's rate limit."""

    org: str = Field(..., description="The org.")
    namespace: str = Field(..., description="The repository's trunk code namespace.")
    name: str = Field(..., description="The source name.")


class KnowledgeSourceCheckOutput(BaseModel):
    # The source checked.
    name: str
    # False when the probe failed; `summary` says why.
    reachable: bool = True
    # Documents the source lists now: at most 1000 (gdrive, recording), each
    # project's first page (linear), the listed pages and the first 100 pages of each
    # of up to three databases (notion).
    documents: int = 0
    # True when more documents match than were listed, so `documents` is a lower bound.
    more: bool = False
    # Up to five document titles, most recently changed first.
    sample: list[str] = Field(default_factory=list)
    # One line: what was found, or why it failed.
    summary: str = ""
    # When the probe ran, ISO UTC.
    checked_at: str = ""


class KnowledgeSourceRotateInput(BaseModel):
    """Replace a registered document source's credential. A namespace admin may;
    the new credential is probed first, and a probe that fails stores nothing."""

    org: str = Field(..., description="The org.")
    namespace: str = Field(..., description="The repository's trunk code namespace.")
    name: str = Field(..., description="The source name.")
    credential: str = Field(
        ...,
        description="The replacement credential, of the same form as add's; stored "
        "encrypted, never returned.",
    )


class KnowledgeSourceRotateOutput(BaseModel):
    # The source whose credential was replaced.
    name: str
    # What the probe with the new credential found.
    probe: KnowledgeSourceProbe = Field(default_factory=KnowledgeSourceProbe)
    # When the credential was replaced, ISO UTC.
    rotated_at: str = ""


class KnowledgeSourceRemoveInput(BaseModel):
    """Retire a registered document source: its registry row goes inactive and its
    credential is cleared. Its documents stay; with `delete_documents` true the
    request to retire them is recorded on the row, and the sync worker retires them
    over its next passes. A namespace admin may; `confirm` must equal the name."""

    org: str = Field(..., description="The org.")
    namespace: str = Field(..., description="The repository's trunk code namespace.")
    name: str = Field(..., description="The source name.")
    confirm: str = Field(..., description="Type the source name to confirm.")
    delete_documents: bool = Field(
        False,
        description="Record a request to retire the source's documents (default keep them).",
    )


class KnowledgeSourceRemoveOutput(BaseModel):
    # The source retired.
    name: str
    # Live documents the recorded delete request covers (none retired yet), 0 without
    # `delete_documents`.
    documents_pending: int = 0


class KnowledgeSourceSyncNowInput(BaseModel):
    """Record a request to sync one document source in its sync state: the sync
    worker starts its next pass with it. Nothing is fetched on the request. A
    namespace admin may."""

    org: str = Field(..., description="The org.")
    namespace: str = Field(..., description="The repository's trunk code namespace.")
    name: str = Field(..., description="The source name.")
    full: bool = Field(
        False,
        description="Ask for a pass that lists every document, not only those changed "
        "since the last pass.",
    )


class KnowledgeSourceSyncNowOutput(BaseModel):
    # The source the request was recorded for.
    name: str
    # Whether this deployment runs the sync worker; false: the request waits until it does.
    enabled: bool = False
