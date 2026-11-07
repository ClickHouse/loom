"""Per-entry tier health: the last outcome of each index tier and the one fix.

Every tier that keeps an entry current (the mirror fetch, the parser ingest, each
compiler tier) records its outcome per entry in `code_index_state` under
`tier_health:<tier>`, so the cron CLI, the supervised workers and every replica read
the same record. The value is the JSON of `TierHealth`. A failure classifies the
error output into a kind, and each kind names the one fix; a success clears it.
Pure: `server.code.app.index.tier_health` writes the row.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from server.code.domain.index.compiler_tiers import TIERS as COMPILER_TIER_SPECS
from server.code.domain.index.state import IndexState

STATE_PREFIX = "tier_health:"
# The compiler tiers by producer; `clang_full` is the weekly C++ full re-index, kept apart
# so a delta pass that finds the tier current does not clear a failed full run.
COMPILER_TIERS: tuple[str, ...] = tuple(t.producer for t in COMPILER_TIER_SPECS)
# The tiers, in the order a status lists them.
TIERS: tuple[str, ...] = ("fetch", "ingest", *COMPILER_TIERS, "clang_full")
# The watermark each tier advances: its lag is that watermark's.
WATERMARK_FOR_TIER: dict[str, str] = {
    "ingest": "git_head",
    **{t.producer: t.watermark_key for t in COMPILER_TIER_SPECS},
    "clang_full": "scip_commit",
}
# Each error kept is cut to this many characters, after redaction.
ERROR_CHARS = 600


def state_key(tier: str) -> str:
    return f"{STATE_PREFIX}{tier}"


@dataclass(frozen=True)
class TierHealth:
    """One tier's last outcome for one entry. `failing_since` and the counters
    describe the current run of failures; all are empty once the tier is ok."""

    tier: str
    state: str = "ok"  # ok | failing
    kind: str = ""
    error: str = ""
    fix: str = ""
    failing_since: str = ""
    last_failed_at: str = ""
    failed_passes: int = 0
    last_ok_at: str = ""

    @property
    def failing(self) -> bool:
        return self.state == "failing"

    def to_json(self) -> str:
        return json.dumps({k: v for k, v in asdict(self).items() if k != "tier"}, sort_keys=True)


def parse(tier: str, raw: str) -> TierHealth | None:
    """The record stored as `raw`, or None when there is none or it does not parse."""
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    texts = {
        k: str(data.get(k) or "")
        for k in ("state", "kind", "error", "fix", "failing_since", "last_failed_at", "last_ok_at")
    }
    try:
        failed_passes = int(data.get("failed_passes") or 0)
    except (TypeError, ValueError):
        return None
    return TierHealth(tier=tier, failed_passes=failed_passes, **texts)


def from_state(state: IndexState) -> dict[str, TierHealth]:
    """tier -> record, for every tier with a record in a namespace's state."""
    out: dict[str, TierHealth] = {}
    for tier in TIERS:
        entry = state.get(state_key(tier)) or {}
        health = parse(tier, str(entry.get("value") or ""))
        if health is not None:
            out[tier] = health
    return out


def failed(prev: TierHealth | None, tier: str, error: str, now: datetime) -> TierHealth:
    """The record after a failed pass: the run of failures goes on from `prev`."""
    text = redact(error)
    kind = classify(text)
    at = _iso(now)
    ongoing = prev if prev is not None and prev.failing else None
    return TierHealth(
        tier=tier,
        state="failing",
        kind=kind,
        error=text[-ERROR_CHARS:],
        fix=fix_for(kind, text),
        failing_since=(ongoing.failing_since if ongoing else "") or at,
        last_failed_at=at,
        failed_passes=(ongoing.failed_passes if ongoing else 0) + 1,
        last_ok_at=prev.last_ok_at if prev else "",
    )


def recovered(tier: str, now: datetime) -> TierHealth:
    return TierHealth(tier=tier, state="ok", last_ok_at=_iso(now))


def _iso(now: datetime) -> str:
    return now.astimezone(UTC).isoformat(timespec="seconds")


# --- redaction ---------------------------------------------------------------------

_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    # credentials in a URL: https://user:secret@host/...
    (re.compile(r"(\b[a-z][a-z0-9+.-]*://)[^/\s:@]+:[^/\s@]+@", re.IGNORECASE), r"\1***@"),
    (re.compile(r"(\b[a-z][a-z0-9+.-]*://)[^/\s:@]{20,}@", re.IGNORECASE), r"\1***@"),
    # GitHub token shapes
    (re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"), "***"),
    # an Authorization header or a bearer value
    (re.compile(r"(authorization:\s*)\S+(?:\s+\S+)?", re.IGNORECASE), r"\1***"),
    (re.compile(r"(bearer\s+)[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE), r"\1***"),
    # a credential in a URL's query: any parameter whose name ends in one of these
    (
        re.compile(
            r"([?&][\w.-]*(?:token|key|sig|signature|secret|password)=)[^&\s'\"]+",
            re.IGNORECASE,
        ),
        r"\1***",
    ),
)


def redact(text: str) -> str:
    """`text` with the credentials a git or harness message can echo masked."""
    out = str(text or "")
    for pattern, repl in _REDACTIONS:
        out = pattern.sub(repl, out)
    return out.strip()


# --- classification ----------------------------------------------------------------

# The Go harness's line for a module it skipped because it would not build.
_MODULE_FAILED = re.compile(r"go mod download failed|scip-go failed", re.IGNORECASE)

# First match wins: a missing credential also makes the submodule pin look missing
# ("did not contain <sha>"), so the credential test comes before the pin test.
_KINDS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # A Go module the harness skipped because it would not build: its line ends with the
    # module's own last log line, which can read as any other kind.
    ("module", _MODULE_FAILED),
    # a relative submodule URL: the tier refuses the file transport it resolves to
    ("submodule_url", re.compile(r"transport 'file' not allowed", re.IGNORECASE)),
    # A full disk also cuts a transfer short ("the remote end hung up"): before network.
    ("disk", re.compile(r"no space left on device|disk quota exceeded", re.IGNORECASE)),
    (
        "credential",
        re.compile(
            r"could not read (?:username|password)|authentication failed|terminal prompts disabled"
            r"|invalid username or password|permission denied \(publickey\)"
            r"|repository not found|support for password authentication"
            # git's own wording; a download step's 404 in a harness log is not a credential
            r"|unable to access '[^']*': the requested url returned error: 40[134]\b",
            re.IGNORECASE,
        ),
    ),
    (
        "egress",
        re.compile(
            # the sandbox proxy refuses a host or port with 403 and a method with 405;
            # its 502 (the vetted address did not answer) and 503 (tunnel cap) are network
            r"received http code 40[35] from proxy|connect tunnel failed, response 40[35]\b",
            re.IGNORECASE,
        ),
    ),
    # a submodule's pin also reads missing ("did not contain <sha>") when its host could
    # not be reached, so the connection errors come before the pin test
    (
        "network",
        re.compile(
            r"could not resolve host|failed to connect to|connection refused|connection reset"
            r"|network is unreachable|connection timed out|ssl_connect|gnutls_handshake"
            r"|received http code 50[23] from proxy|connect tunnel failed, response 50[23]\b",
            re.IGNORECASE,
        ),
    ),
    (
        "pin_missing",
        re.compile(
            r"did not contain [0-9a-f]{7,64}|not our ref|reference is not a tree"
            r"|unadvertised object|no such remote ref [0-9a-f]{7,64}",
            re.IGNORECASE,
        ),
    ),
    # a hang-up alone: a refused unadvertised object prints it too, so after the pin test
    ("network", re.compile(r"the remote end hung up unexpectedly", re.IGNORECASE)),
    (
        "timeout",
        re.compile(
            r"timed out after|exceeded \d+(?:\.\d+)?s|timeouterror|stopped by signal",
            re.IGNORECASE,
        ),
    ),
    ("harness", re.compile(r"^(?:\w+: )?harness exit", re.IGNORECASE | re.MULTILINE)),
)


def module_failures(text: str) -> list[str]:
    """The Go harness's `skip <dir>: ... failed: ...` lines in `text`, without `skip `."""
    return [
        line[5:]
        for line in text.splitlines()
        if line.startswith("skip ") and _MODULE_FAILED.search(line)
    ]


def without_module_failures(text: str) -> str:
    """`text` without the module lines: for a run that failed outright, an earlier
    module the harness skipped is not the cause."""
    return "\n".join(
        line
        for line in text.splitlines()
        if not (line.startswith("skip ") and _MODULE_FAILED.search(line))
    )


def classify(text: str) -> str:
    """The kind of a tier failure from its error output."""
    for kind, pattern in _KINDS:
        if pattern.search(text or ""):
            return kind
    return "unknown"


_HOST_RE = re.compile(r"\b[a-z][a-z0-9+.-]*://(?:[^/\s@]+@)?([A-Za-z0-9.-]+)(?::\d+)?(/[^\s'\"]*)?")
# git's scp-like ssh remote: git@host:org/repo.git
_SCP_RE = re.compile(r"\b[\w.-]+@([A-Za-z0-9.-]+\.[A-Za-z]+):([^\s'\":][^\s'\"]*)")
# git names a submodule relative to the checkout ("Failed to clone 'contrib/x'") and,
# in other lines, by its absolute path ("into submodule path '/srv/…/contrib/x'").
_SUBMODULE_RE = re.compile(r"(?:submodule path|failed to clone) '([^']+)'", re.IGNORECASE)


def _where(text: str) -> str:
    """'<host>/<repo>' and the submodule path the error names, as far as it names them:
    the first URL with a path wins over a bare host, and a relative submodule path over
    an absolute one."""
    parts: list[str] = []
    urls = [
        (m.group(1), (m.group(2) or "").split("?", 1)[0].rstrip("/:.,;)").removesuffix(".git"))
        for m in _HOST_RE.finditer(text)
    ] + [
        (m.group(1), "/" + m.group(2).rstrip("/:.,;)").removesuffix(".git"))
        for m in _SCP_RE.finditer(text)
    ]
    if urls:
        host, path = next(((h, p) for h, p in urls if p), urls[0])
        parts.append(f"{host}{path}")
    if paths := _SUBMODULE_RE.findall(text):
        path = next((p for p in paths if not p.startswith("/")), paths[0])
        parts.append(f"submodule {path}")
    return ", ".join(parts)


def fix_for(kind: str, text: str) -> str:
    """The one fix for a failure of `kind`, naming what the error names."""
    where = _where(text)
    at = f" ({where})" if where else ""
    if kind == "credential" and "publickey" in text.lower():
        return (
            f"The ssh key was refused{at}: give this host's ssh key read access to the "
            "repository, or use its https remote with a token."
        )
    if kind == "credential":
        return (
            f"No credential with access{at}: set LOOM_CODE_GITHUB_TOKEN, or the entry's "
            "github_token, to a token that can read the repository and its submodules. "
            "The token is sent only under the remote's owner (https://host/owner/, case as "
            "written): a submodule elsewhere needs its URL moved under that owner."
        )
    if kind == "submodule_url":
        return (
            "A relative submodule URL resolves to the checkout, which has no remote: give "
            "the submodule in .gitmodules an absolute https URL under the remote's owner."
        )
    if kind == "pin_missing":
        return (
            f"A pinned commit is gone upstream{at}: the submodule's history was rewritten. "
            "Point the pin at a commit that exists, or mirror the submodule."
        )
    if kind == "egress":
        return (
            f"The sandbox's egress proxy refused the host{at}: it connects only to public "
            "addresses on port 443, 22 or the remote's own port."
        )
    if kind == "network":
        return f"The host could not be reached{at}: check DNS, routes and the remote's status."
    if kind == "timeout":
        return (
            "The step exceeded its time limit; the error names the step. That step's "
            "timeout setting raises it, or, for a run under entry-lock, the caller's own "
            "time limit."
        )
    if kind == "disk":
        return "The disk is full: free space under the code-index work directory."
    if kind == "module":
        return (
            "A Go module did not build: the error names it and why. The rest of the "
            "repository is indexed; the record clears on the next run that builds the "
            "modules it changes."
        )
    if kind == "harness":
        return "The indexer failed: the error holds the tail of its output."
    return "See the error; the tier retries every pass."
