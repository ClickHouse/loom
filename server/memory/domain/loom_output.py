"""Loom's own words, recognised when they come back as input.

The plugin and the server print fixed prefixes into a session (the primer,
the test gate, the recall block, the duplicate-check verdicts). A transcript
chunk or a hit that carries one is Loom quoting itself: never evidence, so
capture's junk classifier and loom_search's answer assembly both drop it.
"""

from __future__ import annotations

import re
from typing import Any

# The fixed prefixes Loom's plugin and server print into a session. A
# transcript chunk carrying one is Loom quoting itself.
LOOM_OUTPUT_RE = re.compile(
    r"loom-code-shunt:|Loom test gate|Loom validate gate:|Loom task inbox:|"
    r"# Loom memory primer|# Loom code index|Loom mentor [—(]|Loom recall for this prompt|"
    r"Loom index pointers for this prompt|tracker record \(qa-|"
    r"has no likely duplicate in the tracker index|"
    r"no likely duplicate in the indexed records|"
    # The nearest rows' header (server and plugin)
    r"nearest reported issues, below the related line"
)


def is_loom_output(text: Any) -> bool:
    return bool(LOOM_OUTPUT_RE.search(str(text or "")))
