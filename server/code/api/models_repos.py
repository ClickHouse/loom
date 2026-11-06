"""Repositories: code.repos and the code.repo.* self-service admin ops."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

from server.code.api.models_common import _CodeIdentity
from server.operations.read_projection import ProjectedRow

# ============================================================
# code.repos — every configured repo the caller may read
# ============================================================


class CodeReposInput(_CodeIdentity):
    """Lists the code namespaces (repo@branch entries) configured on this
    deployment that the caller is authorized to read, across orgs — each row
    carries its org. Transport authz targets the default code namespace
    (like every code op); the handler then checks each entry separately, so
    unreadable entries are omitted, never 403'd, and the dashboard renders an
    empty state."""

    # A caller standing in one checkout (the plugin's code shunt, from the origin
    # remote) names it, and the answer is that repository's entries only — one status
    # read instead of one per configured repository. Matched as owner/name,
    # case-insensitive, with a URL, `.git` or trailing slash tolerated. Empty keeps the
    # full listing.
    repo: str = Field(default="", max_length=256)
    # The checkout's remotes as ordered candidates (origin, upstream, then the rest;
    # each owner/name or a URL, as `repo`). The first candidate with an indexed entry
    # the caller reads wins; when none has one, a candidate that is a GitHub fork is
    # retried as its parent, then its source. The answer rows then say how they matched
    # (`matched_via`).
    repos: list[str] = Field(default_factory=list, max_length=8)
    # The output fields the caller reads (a whitelist resolved against the row model; an
    # unknown name is a 422); unset, every field. Every list operation is projectable.
    fields: list[str] | None = None

    @field_validator("repos")
    @classmethod
    def _repos_bounded(cls, v: list[str]) -> list[str]:
        if any(len(str(r)) > 256 for r in v):
            raise ValueError("each repos entry is at most 256 characters")
        return v


class CodeRepoSummary(ProjectedRow):
    org: str
    namespace: str
    repo: str
    branch: str
    git_head: str = ""
    clone_head: str = ""
    clone_fetched_at: str = ""  # The mirror's last fetch
    fetch_overdue: bool = False  # Older than code_fetch_stale_after_seconds
    fast_tier_lag: int = -1
    scip_lag: int = -1  # the entry's own compiler tier
    scip_py_lag: int = -1
    scip_producer: str = ""  # which tier scip_lag is: clang | typescript | go | python
    indexed_at: str = ""  # git_head watermark's updated_at
    last_refresh_at: str = ""
    counts: dict[str, int] = Field(default_factory=dict)
    # False only for org-admin-shaped callers (flat ADMIN+) who hold no grant
    # on the entry: they see it exists so they can adopt it (bootstrap of an
    # auto-registered, ownerless code namespace). Everyone else never sees
    # entries they cannot read.
    readable: bool = True
    # How the namespace is joined and where the caller stands, so the Code view can
    # render Join / Request access / Cancel without a second call. `my_status` is
    # `member` when a live grant exists, `pending` when a request is on file, else
    # `none`.
    join_policy: str = "approval"
    # The access class as resolved for this row (a registry access row wins
    # over the entry): "restricted" | "standard". The Code view's pill reads
    # this, not the viewer's own standing — a maintainer of a restricted
    # repository is a member, and must still see "restricted".
    classification: str = ""
    my_status: str = "none"
    # The caller's role on the entry (`admin` = maintainer), so the card knows who may
    # add a branch; where the entry came from (`config` = the operator's JSON,
    # `registry` = added from the page); and a registry entry's first-index progress:
    # queued → cloning → indexing → embedding → ready | error, with a JSON detail
    # ({done, total, files_per_s, eta} while indexing, {chunks, embedded,
    # embedding_coverage} while embedding, {reason} on error) the card renders. Empty
    # for an entry the operator indexed. `embedding` is the index built but the vector
    # leg still filling; `ready` is only written at LOOM_CODE_READY_EMBED_COVERAGE.
    my_role: str = ""
    source: str = "config"
    stage: str = ""
    stage_detail: str = ""
    # The same coverage triple as code.index_status.
    chunks_total: int = 0
    chunks_embedded: int = 0
    embedding_coverage: float = 1.0
    # embedding_coverage >= LOOM_CODE_READY_EMBED_COVERAGE: the vector leg answers
    # code.search. The server's verdict, so a client never copies the threshold.
    searchable: bool = True
    # Whether ANY active admin grant exists on the entry — the only signal that says
    # "ownerless, adopt it". `readable=False` does not mean that: a restricted entry is
    # unreadable to its own maintainer when the sign-in token does not name it.
    # Fail-open to True when the grants read fails (never offer adopt on a guess).
    has_maintainer: bool = True
    # Why an unreadable row was refused, only as much as the join/request buttons
    # already reveal: `token_scope` (restricted; the caller holds a live grant but this
    # token does not name the namespace — re-sign-in with a token that does),
    # `not_member` (no grant; join or request), `closed` (otherwise). Empty on a
    # readable row.
    unreadable_reason: str = ""
    # The namespace the repository's documents live in (its trunk branch's); the same on
    # every branch row of the repository.
    docs_namespace: str = ""
    # The repository's primary code namespace — its trunk branch's, the same on every
    # branch row (`code-clickhouse` for code-clickhouse-26-x). The repository's shared
    # file rules live there, under the repository's own access; the plugin's
    # file-context hook reads it beside the caller's namespace. Equal to
    # `docs_namespace` by construction (the documents live in the same place).
    primary_namespace: str = ""
    # The trackers this code namespace links, primary first ([] = none), on a row the
    # caller maintains; None (absent) otherwise — a restricted tracker's name is not
    # said to a reader.
    bound_trackers: list[str] | None = None
    # Set only when the call named `repos` candidates. `candidate` = the row's
    # repository is one of them; `fork_of` = a candidate is a GitHub fork of the row's
    # repository. `checkout_slug` is the candidate (lower-case owner/name) that led to
    # the row.
    matched_via: str | None = None
    checkout_slug: str | None = None


class CodeReposOutput(BaseModel):
    # One row per entry the caller reads (plus unreadable rows an org admin may adopt),
    # narrowed to the `repo` / `repos` match when either was given; [] when none.
    repos: list[CodeRepoSummary]


class CodeRepoAddInput(BaseModel):
    """Add a repository's branch as a new read-only index. Any signed-in person of the
    org may; they become its first maintainer. The remote must answer `git ls-remote`
    for the branch with the token given (or none, for a public repository) before
    anything is written."""

    org: str
    repo: str = Field(..., description="GitHub owner/name or an HTTPS/SSH Git URL.")
    # Blank asks the remote for its symbolic default branch.
    branch: str = ""
    # `open`: any org member joins at once; `approval`: a maintainer approves each
    # request. A `restricted` repository is always registered as `approval`.
    join_policy: Literal["open", "approval"] = "open"
    # `standard` or `restricted` (a reader needs an approved grant and a token that
    # names the namespace).
    classification: Literal["standard", "restricted"] = "standard"
    access_token: str = Field(
        "", description="Token for a private HTTPS remote; stored encrypted, never returned."
    )
    # Path prefixes to index (`src/`, `lib/`); blank entries are dropped, [] indexes the
    # whole tree.
    path_prefixes: list[str] = Field(default_factory=list)
    # Globs never indexed for this repository (`vendor/`, `*.generated.go`); [] keeps the
    # deployment's default exclude list.
    exclude: list[str] = Field(default_factory=list)
    # Refuse generated content (Go's `// Code generated … DO NOT EDIT.` header, the
    # `@generated` marker, the repository's `.gitattributes` linguist rules) — on unless
    # the maintainer says otherwise
    exclude_generated: bool = True
    # The compiler tier's producer; "" infers it from the tree's manifests (go.mod → go,
    # tsconfig / package.json → typescript, a root CMakeLists.txt → clang, pyproject →
    # python)
    scip_producer: Literal["", "go", "typescript", "clang", "python"] = ""
    # More branches chosen at add time, each its own index sharing the mirror and
    # inheriting rule, token and paths; per-branch refusals come back in `skipped`,
    # never undoing the ones registered.
    branches: list[str] = Field(default_factory=list, max_length=12)


class CodeRepoRemoteBranchesInput(BaseModel):
    """What a Git remote shows before anything is written — every branch with
    its head, the default branch, the ones that look like release branches, and the ones
    indexed here already — so the add dialog offers a choice instead of one text field.
    The token, if any, is used for this one read and not stored."""

    org: str = Field(..., description="The org adding the repository.")
    repo: str = Field(..., description="GitHub owner/name or an HTTPS/SSH Git URL.")
    access_token: str = Field("", description="Token for a private HTTPS remote; used once.")
    # a repository can have thousands of heads (ClickHouse has ~16,000)
    query: str = Field(
        "", max_length=200, description="Case-insensitive substring of the branch name to match."
    )
    limit: int = Field(
        60, ge=1, le=500, description="How many matching branches to return (default 60)."
    )


class CodeRepoRemoteBranchesOutput(BaseModel):
    org: str
    # Stable repository identity. GitHub keeps owner/name; other hosts include the host.
    repo: str
    remote: str = ""
    provider: str = "git"
    warnings: list[str] = Field(default_factory=list)
    # The branch the remote reports as its default.
    default_branch: str = ""
    # [{name, sha}] — default first, then release-shaped, then the rest
    branches: list[dict[str, str]] = Field(default_factory=list)
    # release-shaped branches not indexed yet (26.8, release/1.2, stable-x)
    suggested: list[str] = Field(default_factory=list)
    # [{branch, namespace}] already indexed in this org
    indexed: list[dict[str, str]] = Field(default_factory=list)
    # how many more indexes the org may add (code_self_service_max_repos − active)
    cap: int = 0
    # `branches` is the first `limit` matches of `query` over `total`
    # heads (the default and the indexed ones always included); `truncated`
    # says a search is needed to see the rest
    total: int = 0
    # How many heads match `query` (all of them when it is empty).
    matched: int = 0
    # True when some matching heads are not in `branches`.
    truncated: bool = False
    # The filter as applied: trimmed and lower-cased; '' = no filter.
    query: str = ""


class CodeRepoAddBranchInput(BaseModel):
    """Index another branch of a repository the caller maintains. `namespace` is any
    existing index of that repository (the transport's ADMIN gate on it is who may
    call); the new index inherits the remote, token, prefixes, classification and join
    rule."""

    org: str
    namespace: str
    # The branch to index as a new namespace; it must exist on the remote.
    branch: str


class CodeRepoRemoveInput(BaseModel):
    """Retire a repository index a person added. `confirm` must equal the namespace. The
    registry row and the directory row go inactive at once; every code_* table keyed by
    the namespace is purged (asynchronous mutations); the bare mirror is deleted when no
    other branch of the repository uses it. Operator-configured entries are refused
    (REPO_FROM_CONFIG)."""

    org: str
    namespace: str
    # Must equal `namespace`, or the call is refused with CONFIRM_REQUIRED.
    confirm: str = ""


class CodeRepoSetAccessInput(BaseModel):
    """A maintainer sets a repository's access — its classification (`standard` |
    `restricted`) and join policy (`open` | `approval` | `closed`). Widening a
    restricted index to standard needs `confirm` equal to the namespace. A restricted
    index is never `open`."""

    org: str
    namespace: str
    # `standard` or `restricted` (case-insensitive); anything else is BAD_CLASSIFICATION.
    classification: str
    # `open`, `approval` or `closed` (case-insensitive); `open` with `restricted` is refused.
    join_policy: str
    # Must equal `namespace` when the call widens `restricted` to `standard`; else unused.
    confirm: str = ""


class CodeRepoSetAccessOutput(BaseModel):
    org: str
    namespace: str
    # The repository's GitHub owner/name.
    repo: str
    # The classification as written (lower-cased).
    classification: str
    # The join policy as written (lower-cased).
    join_policy: str
    # The entry's classification before this call.
    previous_classification: str
    # The directory row's join policy before this call (when it had none: `open` for a
    # public entry, else `approval`).
    previous_join_policy: str
    # True when the namespace's directory row got the new access too; False when the
    # namespace has no directory row (only the registry row was written).
    directory_updated: bool
    # every process re-reads the access row within this many seconds
    effective_within_s: int


class CodeRepoSetSourceInput(BaseModel):
    """A maintainer sets the trackers this repository reads (records of issues, PRs,
    tests and incidents registered with `tracker.source.add`), in order. The FIRST is
    the primary — its numbers are the repository's issue and PR numbers, so every lookup
    by number reads it; every linked tracker is searched. `[]` unlinks all."""

    org: str
    namespace: str
    # Tracker namespaces of this org, primary first (duplicates dropped). A name that is not
    # a tracker namespace is refused with NOT_A_TRACKER_NAMESPACE; a new link needs the
    # tracker's admin (a reader of it gets AUTH_INSUFFICIENT). [] unlinks every tracker.
    tracker_namespaces: list[str] = Field(max_length=8)


class CodeRepoSetSourceOutput(BaseModel):
    org: str
    namespace: str
    # the links as written (primary first)
    tracker_namespaces: list[str] = Field(default_factory=list)
    # The links before this call, primary first ([] = none).
    previous_tracker_namespaces: list[str] = Field(default_factory=list)
    # Seconds within which every process re-reads the links.
    effective_within_s: int
    # Linked trackers that do not list this repository (they read only the repositories
    # they name, so they answer nothing here), and the sentence that says so
    not_covering: list[str] = Field(default_factory=list)
    # One clause per `not_covering` tracker naming the repositories it reads; '' when none.
    warning: str = ""


class CodeRepoRemoveOutput(BaseModel):
    org: str
    namespace: str
    # The retired index's GitHub owner/name.
    repo: str
    # The branch the retired index followed.
    branch: str
    # True when the registry row went inactive by this call.
    registry_deactivated: bool
    # True when the namespace's directory row was deleted.
    directory_deactivated: bool
    # How many code tables keyed by the namespace the erasure saga purges.
    purged_tables: int
    # The erasure saga that purges them — follow it with erasure.status; erasure_recovery
    # resumes it.
    erasure_id: str = ""
    # True when the bare mirror was deleted because no other branch of the repository uses it.
    mirror_removed: bool = False
    # The document sources registered on the namespace (knowledge.source.add), now inactive
    # with their credentials cleared.
    sources_deactivated: list[str] = Field(default_factory=list)


class CodeRepoAddOutput(BaseModel):
    org: str
    namespace: str
    # Stable repository identity. GitHub keeps owner/name; other hosts include the host.
    repo: str
    remote: str = ""
    provider: str = "git"
    # The branch the new index follows.
    branch: str
    # The join policy as registered (`approval` for a restricted repository).
    join_policy: str
    # The classification as registered: `standard` or `restricted`.
    classification: str
    # queued at creation; the worker moves it (see CodeRepoSummary.stage)
    index_stage: str = ""
    # `admin` when the caller became the index's first maintainer; '' for a caller with no
    # subject.
    my_role: str = "admin"
    # The branch head SHA the remote answered with at add time.
    remote_head: str = ""
    # The other branches registered in the same call ({branch, namespace, remote_head})
    also: list[dict[str, str]] = Field(default_factory=list)
    # The extra branches refused, per branch {branch, reason}; the others stay registered.
    skipped: list[dict[str, str]] = Field(default_factory=list)
