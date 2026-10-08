"""Durable materialized-view identities for source-table replacement."""

import re
from dataclasses import dataclass
from typing import Any
from uuid import UUID

_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class RebuildView:
    name: str
    original: UUID
    replacement: UUID
    ddl: str

    def creation_sql(self) -> str:
        """Recreate only an explicit-TO view; no implicit inner data is dropped."""
        if (
            not _NAME.fullmatch(self.name)
            or not self.original.int
            or not self.replacement.int
            or self.original == self.replacement
        ):
            raise ValueError("Invalid rebuild view identities")
        head = re.compile(
            r"^CREATE MATERIALIZED VIEW(?: IF NOT EXISTS)? "
            r"(?:`?loom`?\.)`?" + re.escape(self.name) + r"`?(?: UUID '[0-9a-f-]{36}')?(?=\s)"
        )
        match = head.match(self.ddl)
        if match is None or not re.search(r"\bTO\s+`?[A-Za-z_][A-Za-z0-9_]*`?\.", self.ddl):
            raise ValueError("Rebuild requires an explicit-TO materialized view")
        return (
            f"CREATE MATERIALIZED VIEW `loom`.`{self.name}` UUID '{self.replacement}'"
            + self.ddl[match.end() :]
        )

    def to_dict(self) -> dict[str, str]:
        self.creation_sql()
        return {
            "name": self.name,
            "original": str(self.original),
            "replacement": str(self.replacement),
            "ddl": self.ddl,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "RebuildView":
        if set(value) != {"name", "original", "replacement", "ddl"}:
            raise ValueError("Invalid rebuild view recipe")
        result = cls(
            str(value["name"]),
            UUID(value["original"]),
            UUID(value["replacement"]),
            str(value["ddl"]),
        )
        result.creation_sql()
        return result
