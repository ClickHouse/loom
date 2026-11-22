"""Task identity and fingerprints shared by setup and grading."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

MODES = ("baseline", "natural")
TRANSPORT_TIMEOUT_S = 300


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


@dataclass(frozen=True)
class Task:
    task_id: str
    kind: str
    repository: str
    base_commit: str
    namespace: str
    problem: str
    symbol: str
    files: dict[str, str]
    source_sha256: str
    read_only_files: dict[str, str] = field(default_factory=dict)

    @property
    def read_only_roots(self) -> list[str]:
        return sorted({name.split("/", 1)[0] for name in self.read_only_files})
