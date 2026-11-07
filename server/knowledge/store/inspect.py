"""The reads behind `knowledge.inspect`: a namespace's documents grouped by format, source
scheme and state, its live chunks by embedding model, the documents whose chunks search
cannot serve, what the extractor kept of the ingested ones, the namespace's
knowledge.ingest jobs per source, and, for the newest documents, their chunks, retired
versions and jobs.

Every statement filters on (org, namespace), the prefix of each table's sort key.
`loom.documents` holds one row per document, its current version, read with FINAL; an
earlier version is known only through its chunks, which stay in `loom.chunks` and are
retired by a `loom.chunk_supersessions` row. Documents are the namespace's under any
read scope, as every `knowledge.*` read serves them. No statement reads a document's
text; the per-document chunk read measures the listed documents' chunk text only. A
job's error message is not read either: its class is derived from it in the statement.
`loom.knowledge_ingestions` is a ReplacingMergeTree read with FINAL, beside the cancels
in `loom.knowledge_ingest_cancels`; its rows are the namespace's under any read scope, as
knowledge.ingest_status serves any of them.
"""

from __future__ import annotations

from typing import Any

from server.core.repo_base import _CHClient, _default_client
from server.knowledge.domain.ingest_states import OPEN_STATES
from server.knowledge.domain.live_chunks import LIVE_CHUNK_SQL
from server.knowledge.store.ingestions import CANCELS, INGESTIONS

# The most format and scheme pairs the grouping returns.
MAX_GROUPS = 1000  # not a setting: a bound on one report's rows, flagged when reached
# The most sample URIs one check collects; a request's sample_limit cuts it further.
MAX_SAMPLES = 100  # not a setting: sample_limit (at most 100) is the caller's knob

_NONE = "'(none)'"
# A source_uri's scheme: the letters before "://", lowercased; '(none)' for a path.
_SCHEME = "lower(extract(source_uri, '^([A-Za-z][A-Za-z0-9+.-]*)://'))"
SCHEME_SQL = f"if({_SCHEME} != '', {_SCHEME}, {_NONE})"
# The path a file extension is read from: the original filename when one was uploaded,
# else the source_uri without its scheme, host, query and fragment.
_PATH = (
    "if(original_filename != '', original_filename, replaceRegexpOne(replaceRegexpOne("
    "source_uri, '^[A-Za-z][A-Za-z0-9+.-]*://[^/]*', ''), '[?#].*$', ''))"
)
_EXT = f"lower(extract({_PATH}, '[.]([A-Za-z0-9]{{1,10}})$'))"
# The format the row records (knowledge.ingest sets it, knowledge.get serves it), else
# the format a source sync converted it from (metadata `ingested_format`: transcript,
# markdown, slides, or the extractor's pdf, pptx or docx), else the stored content
# type, else ".<extension>", else '(none)'.
_SYNCED = "JSONExtractString(metadata, 'ingested_format')"
FORMAT_SQL = (
    f"if(format != '', toString(format), if({_SYNCED} != '', {_SYNCED}, "
    f"if(blob_content_type != '', toString(blob_content_type), "
    f"if({_EXT} != '', concat('.', {_EXT}), {_NONE}))))"
)

_DOCS = "FROM loom.documents FINAL WHERE org = {org:String} AND namespace = {ns:String}"
_LIVE_CHUNKS = (
    f"FROM loom.chunks WHERE org = {{org:String}} AND namespace = {{ns:String}} "
    f"AND {LIVE_CHUNK_SQL}"
)


async def document_groups(
    org: str, namespace: str, *, ch: _CHClient | None = None
) -> list[dict[str, Any]]:
    """(doc_format, source_scheme, live, deleted, newest live ingest, earlier revisions,
    unnumbered rows) per pair, at most MAX_GROUPS + 1: one more than MAX_GROUPS means the
    pairs were cut. A row's `revision` numbers its published revisions (1, 2, ...), so
    `revision - 1` earlier ones were published; a row no write numbered reads 0."""
    return await (ch or _default_client()).query_all(
        f"""
        SELECT {FORMAT_SQL} AS doc_format, {SCHEME_SQL} AS source_scheme,
               countIf(superseded_at IS NULL) AS live,
               countIf(superseded_at IS NOT NULL) AS deleted,
               maxIf(ingested_at, superseded_at IS NULL) AS newest,
               sumIf(revision - 1, revision > 0) AS earlier_revisions,
               countIf(revision = 0) AS unnumbered
        {_DOCS}
        GROUP BY doc_format, source_scheme
        ORDER BY live DESC, deleted DESC, doc_format, source_scheme
        LIMIT {{cap:UInt32}}
        """,
        # One past the cap, so a full read is told apart from a cut one.
        params={"org": org, "ns": namespace, "cap": MAX_GROUPS + 1},
    )


async def retired_versions(org: str, namespace: str, *, ch: _CHClient | None = None) -> int:
    """Distinct (document, chunk version) pairs whose chunks were retired, across models:
    the revisions a later write or a delete replaced, and every knowledge.ingest attempt
    that inserted chunks and never published a document row (failed, cancelled, or beaten
    by a later write), which no revision numbers."""
    row = await (ch or _default_client()).query_one(
        "SELECT uniqExact(source_uri, doc_version) AS n FROM loom.chunk_supersessions "
        "WHERE org = {org:String} AND namespace = {ns:String}",
        params={"org": org, "ns": namespace},
    )
    return int(row["n"]) if row else 0


async def chunk_models(
    org: str, namespace: str, *, ch: _CHClient | None = None
) -> list[dict[str, Any]]:
    """(model, live, embedded) over the namespace's live chunks, most first: one row per
    embedding model the namespace was written under."""
    return await (ch or _default_client()).query_all(
        f"""
        SELECT toString(embedding_model) AS model, count() AS live,
               countIf(embedding_dim > 0) AS embedded
        {_LIVE_CHUNKS}
        GROUP BY model
        ORDER BY live DESC, model
        """,
        params={"org": org, "ns": namespace},
    )


async def chunk_health(
    org: str, namespace: str, model: str, *, ch: _CHClient | None = None
) -> dict[str, Any] | None:
    """Over the live documents: how many there are, and how many (with up to MAX_SAMPLES
    URIs each) have no live chunk, a live chunk with no vector, live chunks none of which
    is under `model`, or live chunks of more than one version under `model`. The
    documents are filtered in a derived table before the join (the knowledge.list read
    does the same), and a missing chunk side reads as 0 under either `join_use_nulls`."""
    n = MAX_SAMPLES
    checks = {
        "without_chunks": "ifNull(c.live, 0) = 0",
        "unembedded": "ifNull(c.unembedded, 0) > 0",
        "other_model": "ifNull(c.live, 0) > 0 AND ifNull(c.current, 0) = 0",
        "mixed_versions": "ifNull(c.versions, 0) > 1",
    }
    # Per check: the matches, their URIs, and the ones knowledge.ingest published (a text
    # write would turn those into text documents), with theirs.
    ingested = "d.ingestion_id != ''"
    columns = ",\n".join(
        f"countIf({cond}) AS {name}, groupArrayIf({n})(d.source_uri, {cond}) AS {name}_uris, "
        f"countIf({cond} AND {ingested}) AS {name}_ingested, "
        f"groupArrayIf({n})(d.source_uri, {cond} AND {ingested}) AS {name}_ingested_uris"
        for name, cond in checks.items()
    )
    return await (ch or _default_client()).query_one(
        f"""
        SELECT count() AS live_docs,
               {columns}
        FROM (
            SELECT source_uri, ingestion_id {_DOCS} AND superseded_at IS NULL
        ) AS d
        LEFT JOIN (
            SELECT source_uri, count() AS live,
                   countIf(embedding_model = {{model:String}}) AS current,
                   countIf(embedding_dim = 0) AS unembedded,
                   uniqExactIf(doc_version, embedding_model = {{model:String}}) AS versions
            {_LIVE_CHUNKS}
            GROUP BY source_uri
        ) AS c ON d.source_uri = c.source_uri
        """,
        params={"org": org, "ns": namespace, "model": model},
    )


# What an extraction's coverage (the artifact's, kept on the document row as JSON) says,
# without its failed units' names, which can be the source's own keys.
_LOST = "arrayDistinct(JSONExtract(coverage, 'lost', 'Array(String)'))"
_COVERAGE_COLUMNS = (
    "coverage != '' AS has_coverage, JSONExtractString(coverage, 'unit') AS cov_unit, "
    "JSONExtractInt(coverage, 'total') AS cov_total, "
    "JSONExtractInt(coverage, 'extracted') AS cov_extracted, "
    "JSONLength(coverage, 'failed') AS cov_failed, "
    f"{_LOST} AS cov_lost"
)
# Not complete: a unit not extracted, failed, or a capability lost. Read from the counts, not
# the stored `complete`, so a coverage written without it is not judged complete.
_INCOMPLETE = (
    "(JSONExtractInt(coverage, 'extracted') < JSONExtractInt(coverage, 'total') "
    "OR JSONLength(coverage, 'failed') > 0 OR length(" + _LOST + ") > 0)"
)


async def extraction_health(
    org: str, namespace: str, *, ch: _CHClient | None = None
) -> dict[str, Any] | None:
    """Over the live documents an extraction published (a coverage on the row): how many,
    how many are incomplete with up to MAX_SAMPLES URIs, and per capability `lost` names
    the documents that lost it (`lost_names`, `lost_counts`, most first, at most
    MAX_GROUPS + 1: one more means cut)."""
    lost = f"sumMap({_LOST}, arrayMap(x -> toUInt64(1), {_LOST}))"
    return await (ch or _default_client()).query_one(
        f"""
        SELECT count() AS documents, countIf({_INCOMPLETE}) AS incomplete,
               groupArrayIf({MAX_SAMPLES})(source_uri, {_INCOMPLETE}) AS incomplete_uris,
               arraySlice(arraySort((n, c) -> (-c, n), ({lost}).1, ({lost}).2), 1,
                          {{cap:UInt32}}) AS lost_names,
               arraySlice(arrayReverseSort(({lost}).2), 1, {{cap:UInt32}}) AS lost_counts
        {_DOCS} AND superseded_at IS NULL AND coverage != ''
        """,
        params={"org": org, "ns": namespace, "cap": MAX_GROUPS + 1},
    )


async def newest_documents(
    org: str, namespace: str, *, limit: int, ch: _CHClient | None = None
) -> list[dict[str, Any]]:
    """The newest live documents, at most `limit`, ties broken by source_uri, with their
    revision and the ingestion that published them ('' for a text write)."""
    return await (ch or _default_client()).query_all(
        f"""
        SELECT source_uri, title, version, revision, ingestion_id, ingested_at,
               {FORMAT_SQL} AS doc_format, {SCHEME_SQL} AS source_scheme,
               {_COVERAGE_COLUMNS}
        {_DOCS} AND superseded_at IS NULL
        ORDER BY ingested_at DESC, source_uri DESC
        LIMIT {{lim:UInt32}}
        """,
        params={"org": org, "ns": namespace, "lim": limit},
    )


async def document_chunks(
    org: str, namespace: str, source_uris: list[str], model: str, *, ch: _CHClient | None = None
) -> list[dict[str, Any]]:
    """Per listed document, its live chunks: counts by state, versions under `model`,
    characters per chunk (min, median, max) and the models they were written under."""
    if not source_uris:
        return []
    return await (ch or _default_client()).query_all(
        f"""
        SELECT source_uri, count() AS live,
               countIf(embedding_model = {{model:String}} AND embedding_dim > 0) AS searchable,
               countIf(embedding_dim = 0) AS unembedded,
               countIf(embedding_model != {{model:String}}) AS other_model,
               uniqExactIf(doc_version, embedding_model = {{model:String}}) AS versions,
               min(lengthUTF8(content)) AS min_chars,
               toUInt64(quantileExact(0.5)(lengthUTF8(content))) AS median_chars,
               max(lengthUTF8(content)) AS max_chars,
               arraySort(groupUniqArray(toString(embedding_model))) AS models
        {_LIVE_CHUNKS} AND source_uri IN {{uris:Array(String)}}
        GROUP BY source_uri
        """,
        params={"org": org, "ns": namespace, "uris": source_uris, "model": model},
    )


async def document_retired_versions(
    org: str, namespace: str, source_uris: list[str], *, ch: _CHClient | None = None
) -> dict[str, int]:
    """source_uri -> distinct versions whose chunks were retired, for the listed ones."""
    if not source_uris:
        return {}
    rows = await (ch or _default_client()).query_all(
        """
        SELECT source_uri, uniqExact(doc_version) AS n
        FROM loom.chunk_supersessions
        WHERE org = {org:String} AND namespace = {ns:String}
          AND source_uri IN {uris:Array(String)}
        GROUP BY source_uri
        """,
        params={"org": org, "ns": namespace, "uris": source_uris},
    )
    return {str(r["source_uri"]): int(r["n"]) for r in rows}


# The most longer URIs `longer_document_uris` returns.
MAX_LONGER = 1000  # not a setting: a bound on one report's rows, flagged when reached


async def longer_document_uris(
    org: str, namespace: str, source_uris: list[str], *, ch: _CHClient | None = None
) -> list[str]:
    """The documents, live or deleted, whose source_uri extends one of `source_uris` with
    `#` (`doc#v2` for `doc`), at most MAX_LONGER + 1 (one more means cut): a citation of
    `doc#v2#Intro` names `doc#v2`, not `doc`, as `loom decisions extract` reads it. Reads
    the sort-key column."""
    if not source_uris:
        return []
    rows = await (ch or _default_client()).query_all(
        f"""
        SELECT DISTINCT source_uri {_DOCS}
          AND arrayExists(u -> startsWith(source_uri, concat(u, '#')), {{uris:Array(String)}})
        ORDER BY source_uri
        LIMIT {{cap:UInt32}}
        """,
        params={"org": org, "ns": namespace, "uris": source_uris, "cap": MAX_LONGER + 1},
    )
    return [str(r["source_uri"]) for r in rows]


# ---- knowledge.ingest jobs ----

_OPEN = "job_state IN {open:Array(String)}"
# A job's order among its source's: by when it was queued, then by id.
_JOB_KEY = "(created_at, ingestion_id)"
# An outage a job is waiting to retry (pending, error_code `retrying`), or one that used up
# knowledge_ingest_max_attempts (failed, `retries_exhausted`): its message tells its class.
# A retried job a worker took again is processing and still carries `retrying` and its
# message until the run ends; that run is the backlog's, whatever stopped the last one.
_OUTAGE = "((job_code = 'retrying' AND job_state = 'pending') OR job_code = 'retries_exhausted')"
# The messages the extractor client raises when its service cannot take the source
# (server/knowledge/app/extractors.py: ExtractorUnavailable), and among them the ones that
# say the service refused the server's credential.
EXTRACTOR_MESSAGE_RE = "^(the extractor|the extraction took|no extractor)"
REFUSED_CREDENTIAL_RE = "^the extractor(.s capabilities)? answered (401|403|407)$"
# Why a job's last attempt failed (FailureClass in the wire model), read from its
# error_code and, for an outage, the class of its message. An outage that is neither the
# extractor's nor a refused credential is `retrying` while it waits and `retries_exhausted`
# once it failed (a store or embedder outage, or runs cut off before they finished). An
# artifact that breaks the contract (`invalid_artifact`, the server's own check) is the
# extractor's, not the file's. Every code not named is the source's: the extractor's own
# refusals (format_mismatch, encoding, corrupt, resource_limit when it passes its own memory
# or output limit, ...) and the server's of what it made (incomplete_extraction, no_text,
# too_large, resource_limit for an artifact over document_extractor_max_artifact_bytes).
FAILURE_CLASS_SQL = (
    "multiIf(job_code = '', '', "
    f"job_code = 'original_gone' OR ({_OUTAGE} AND match(error_message, "
    f"'{REFUSED_CREDENTIAL_RE}')), 'missing_access', "
    "job_code IN ('format_unavailable', 'invalid_artifact') OR "
    f"({_OUTAGE} AND match(error_message, "
    f"'{EXTRACTOR_MESSAGE_RE}')), 'extractor_unavailable', "
    "job_code IN ('retrying', 'retries_exhausted', 'revision_conflict', 'cancelled', "
    "'internal'), job_code, "
    "'bad_content')"
)
# A job's fields as the report returns them, each read off the newest job of a source
# (`latest_<name>`) and off its oldest open one (`open_<name>`).
_JOB_FIELDS = {
    "ingestion_id": "toString(ingestion_id)",
    "state": "job_state",
    "stage": "stage",
    "attempts": "attempts",
    "error_code": "job_code",
    "failure_class": FAILURE_CLASS_SQL,
    "queued_at": "created_at",
    "updated_at": "job_updated_at",
    "next_attempt_at": "next_attempt_at",
    # Processing, and not updated within the job's lease: its worker stopped.
    "stalled": "job_state = 'processing' "
    "AND job_updated_at <= now64(3) - toIntervalMillisecond({lease_ms:UInt64})",
    # What the format it was extracted as is read from: knowledge.ingest's `format`
    # option, the file name and the media type (server.core.document_formats.format_of).
    "filename": "filename",
    "content_type": "toString(content_type)",
    "declared_format": "JSONExtractString(options, 'format')",
}
_STATES = ("pending", "processing", "ready", "partial", "failed", "cancelled")


def _jobs(where: str) -> str:
    """The namespace's job rows, with the state knowledge.ingest_status answers: a pending
    job with a cancel requested (a loom.knowledge_ingest_cancels row, which no worker has
    acted on yet) is cancelled; its other fields are its row's. The columns the report
    derives are renamed (`job_state`, `job_code`, `job_updated_at`), so none shadows a
    stored one."""
    return f"""
        SELECT source_uri, ingestion_id, created_at, next_attempt_at, attempts,
               toString(stage) AS stage, error_message, filename, content_type, options,
               state = 'pending' AND ingestion_id IN (
                   SELECT ingestion_id FROM {CANCELS}
                   WHERE org = {{org:String}} AND namespace = {{ns:String}}
               ) AS cancel_pending,
               if(cancel_pending, 'cancelled', toString(state)) AS job_state,
               if(cancel_pending, 'cancelled', toString(error_code)) AS job_code,
               updated_at AS job_updated_at
        FROM {INGESTIONS} FINAL
        WHERE org = {{org:String}} AND namespace = {{ns:String}}{where}
    """


def _per_source(where: str) -> str:
    """One row per source_uri with jobs in the namespace: its job counts, its newest job and
    its oldest open one (the one a worker runs next; empty fields when none is open);
    whether it has a live document; and whether a write replaced its jobs (`replaced`: none
    is open, and the source's document row was written or deleted after the newest ended).
    A text write and a delete write no job row, so only the document row tells a failure a
    later write made moot. A metadata edit without reindex keeps `ingested_at` and replaces
    nothing; a reindex rewrites the document and counts as a write.
    `ingested_at` and `superseded_at` count seconds: a write in the second the job ended
    does not replace it."""
    latest = ", ".join(f"argMax({e}, {_JOB_KEY}) AS latest_{n}" for n, e in _JOB_FIELDS.items())
    oldest_open = ", ".join(
        f"argMinIf({e}, {_JOB_KEY}, {_OPEN}) AS open_{n}" for n, e in _JOB_FIELDS.items()
    )
    states = ", ".join(f"countIf(job_state = '{s}') AS {s}" for s in _STATES)
    return f"""
        SELECT j.*, ifNull(d.doc_live, 0) AS has_document,
               j.open_jobs = 0 AND ifNull(d.doc_written_at, toDateTime(0))
                   > toDateTime(j.latest_updated_at) AS replaced
        FROM (
            SELECT source_uri, count() AS jobs, countIf({_OPEN}) AS open_jobs,
                   {latest}, {oldest_open}, {states},
                   countIf({_JOB_FIELDS["stalled"]}) AS stalled
            FROM ({_jobs(where)})
            GROUP BY source_uri
        ) AS j
        LEFT JOIN (
            SELECT source_uri, superseded_at IS NULL AS doc_live,
                   ifNull(superseded_at, ingested_at) AS doc_written_at
            {_DOCS}{where}
        ) AS d ON j.source_uri = d.source_uri
    """


# The job a source's findings judge: its oldest open one when one is open, else its newest.
_CURRENT = "if(j.open_jobs > 0, j.open_{0}, j.latest_{0})"
# code: the predicate over a source's jobs; a source a later write replaced matches none. A
# backlog is an open job whose last attempt did not fail, or failed on an outage it retries;
# an extractor or access failure of an open job is that failure's, not the backlog's.
INGEST_CHECKS = {
    "ingest_backlog": "j.open_jobs > 0 AND j.open_failure_class IN ('', 'retrying')",
    "extractor_unavailable": f"{_CURRENT.format('failure_class')} = 'extractor_unavailable'",
    "ingest_missing_access": f"{_CURRENT.format('failure_class')} = 'missing_access'",
    "ingest_content_rejected": f"{_CURRENT.format('failure_class')} = 'bad_content'",
    # Failed on the server's side: an outage that used up its attempts, runs cut off before
    # they finished, or a defect.
    "ingest_failed_on_server": (
        f"{_CURRENT.format('failure_class')} IN ('retries_exhausted', 'internal')"
    ),
}


def _lease_ms(lease_s: float) -> int:
    return int(lease_s * 1000)


async def ingestion_health(
    org: str, namespace: str, *, lease_s: float, ch: _CHClient | None = None
) -> dict[str, Any] | None:
    """Over the namespace's sources with a job: how many, how many have no live document
    (and no write replaced their jobs), how many a write replaced, jobs by state (and
    processing ones past their lease), when the oldest open job was queued, and per
    INGEST_CHECKS predicate the sources that match, none replaced, up to MAX_SAMPLES
    (source_uri, ingestion_id) pairs, and how many of them have no live document."""
    n = MAX_SAMPLES
    no_doc = "j.has_document = 0"
    current_id = _CURRENT.format("ingestion_id")
    # A source a later write replaced matches no check.
    checks = {code: f"NOT j.replaced AND ({cond})" for code, cond in INGEST_CHECKS.items()}
    columns = ",\n".join(
        f"countIf({c}) AS {code}, "
        f"groupArrayIf({n})(tuple(j.source_uri, {current_id}), {c}) AS {code}_samples, "
        f"countIf({c} AND {no_doc}) AS {code}_without_document"
        for code, c in checks.items()
    )
    states = ", ".join(f"sum(j.{s}) AS {s}" for s in (*_STATES, "stalled"))
    return await (ch or _default_client()).query_one(
        f"""
        SELECT count() AS sources, countIf({no_doc} AND NOT j.replaced) AS without_document,
               countIf(j.replaced) AS replaced, {states},
               sum(j.open_jobs) AS open_jobs,
               minIf(j.open_queued_at, j.open_jobs > 0) AS oldest_open_queued_at,
               {columns}
        FROM ({_per_source("")}) AS j
        """,
        params={
            "org": org,
            "ns": namespace,
            "open": list(OPEN_STATES),
            "lease_ms": _lease_ms(lease_s),
        },
    )


async def source_jobs(
    org: str,
    namespace: str,
    source_uris: list[str],
    *,
    lease_s: float,
    ch: _CHClient | None = None,
) -> list[dict[str, Any]]:
    """Per listed source_uri with a job: its job counts, newest job and oldest open one."""
    if not source_uris:
        return []
    return await (ch or _default_client()).query_all(
        _per_source(" AND source_uri IN {uris:Array(String)}"),
        params={
            "org": org,
            "ns": namespace,
            "uris": source_uris,
            "open": list(OPEN_STATES),
            "lease_ms": _lease_ms(lease_s),
        },
    )


async def sources_without_document(
    org: str, namespace: str, *, limit: int, lease_s: float, ch: _CHClient | None = None
) -> list[dict[str, Any]]:
    """The sources with a job and no live document that no write replaced (a first ingest,
    or one after a delete, queued, running or failed), newest job first, at most
    `limit`."""
    return await (ch or _default_client()).query_all(
        f"""
        SELECT * FROM ({_per_source("")}) WHERE has_document = 0 AND NOT replaced
        ORDER BY latest_queued_at DESC, source_uri
        LIMIT {{lim:UInt32}}
        """,
        params={
            "org": org,
            "ns": namespace,
            "open": list(OPEN_STATES),
            "lease_ms": _lease_ms(lease_s),
            "lim": limit,
        },
    )


# The most sources `format_unavailable_sources` returns.
MAX_FORMAT_SOURCES = 1000  # not a setting: a bound on one report's rows, flagged when reached


async def format_unavailable_sources(
    org: str, namespace: str, *, lease_s: float, ch: _CHClient | None = None
) -> list[dict[str, Any]]:
    """The sources whose current job (the open one, else the newest) failed or waits
    because no configured extractor takes its format (error_code format_unavailable), no
    write having replaced it: each with that job's id and what its format is read from
    (filename, content_type, declared_format), at most MAX_FORMAT_SOURCES + 1 (one more
    means cut)."""
    current = {n: _CURRENT.format(n) for n in ("ingestion_id", "filename", "content_type")}
    return await (ch or _default_client()).query_all(
        f"""
        SELECT j.source_uri AS source_uri, {current["ingestion_id"]} AS ingestion_id,
               {current["filename"]} AS filename, {current["content_type"]} AS content_type,
               {_CURRENT.format("declared_format")} AS declared_format
        FROM ({_per_source("")}) AS j
        WHERE NOT j.replaced AND {_CURRENT.format("error_code")} = 'format_unavailable'
        ORDER BY source_uri
        LIMIT {{cap:UInt32}}
        """,
        params={
            "org": org,
            "ns": namespace,
            "open": list(OPEN_STATES),
            "lease_ms": _lease_ms(lease_s),
            "cap": MAX_FORMAT_SOURCES + 1,
        },
    )


async def extraction_by_format(
    org: str, namespace: str, *, ch: _CHClient | None = None
) -> list[dict[str, Any]]:
    """Per format of the live documents an extraction published (a coverage on the row):
    how many, how many are incomplete with up to MAX_SAMPLES URIs, and the capabilities
    their coverage lost (`lost`, distinct, sorted), and the media types of the originals
    behind the incomplete extractions and behind the complete ones (`incomplete_types`,
    `complete_types`, distinct, sorted: a media-type route may have sent them), most
    incomplete first, at most MAX_GROUPS + 1 formats (one more means cut)."""
    return await (ch or _default_client()).query_all(
        f"""
        SELECT {FORMAT_SQL} AS doc_format, count() AS documents,
               countIf({_INCOMPLETE}) AS incomplete,
               groupArrayIf({MAX_SAMPLES})(source_uri, {_INCOMPLETE}) AS incomplete_uris,
               arraySort(groupUniqArrayArray({_LOST})) AS lost,
               arraySort(groupUniqArrayIf(toString(blob_content_type), {_INCOMPLETE}))
                   AS incomplete_types,
               arraySort(groupUniqArrayIf(toString(blob_content_type), NOT {_INCOMPLETE}))
                   AS complete_types
        {_DOCS} AND superseded_at IS NULL AND coverage != ''
        GROUP BY doc_format
        ORDER BY incomplete DESC, doc_format
        LIMIT {{cap:UInt32}}
        """,
        params={"org": org, "ns": namespace, "cap": MAX_GROUPS + 1},
    )
