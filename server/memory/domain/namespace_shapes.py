"""Value-shape thresholds shared by `namespace.audit` and the mentor.

The audit (identity/app/namespace_audit.py) grades a
namespace's rows by shape — a short hot value is coordination state, a huge
episodic value is a blob, a procedural value is structured by design — and
the mentor (insights/app/mentor.py) names the same shapes in its hints. Both
read the bounds from here so a hint and an audit finding never disagree.
"""

from __future__ import annotations

# coordination_as_memory: a value under this many chars AND this many words
# read ≥ HOT_KEY_READS times is a pointer or a lease, not a memory. The word
# bound tells a lease (`run-42 owner=x until …`) from a short lesson in prose
# that an agent re-reads by key (a 158-char lesson read 32× is not
# coordination state).
COORDINATION_VALUE_CHARS = 160
COORDINATION_MAX_WORDS = 8
COORDINATION_SEGMENTS: tuple[str, ...] = (
    "lease",
    "leases",
    "lock",
    "locks",
    "freeze",
    "freezes",
)

# episodic_blobs: an episodic value over this many chars belongs in context.offload.
EPISODIC_BLOB_CHARS = 50_000

# Memory types whose value is structured BY DESIGN (a procedure is a graph the
# procedure plane reads back as JSON): a shape check that says "this JSON should
# be a document" is wrong about them, and would tell a namespace owner to
# knowledge.upsert the procedures the plugin's own hooks captured.
STRUCTURED_TYPES: frozenset[str] = frozenset({"procedural"})
