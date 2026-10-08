"""SQL lane records: the statement, hint, header, facet and unit types, and the facet
kinds and confidences they carry."""

from __future__ import annotations

from dataclasses import dataclass, field

from server.code.domain.extract.chunker import Chunk
from server.code.domain.extract.treesitter import Symbol

SQL_LANG = "sql"
STATEMENT_KIND = "statement"
JINJA_PLACEHOLDER = "__jinja__"  # What a `{{ … }}` becomes before the split

# Facet kinds. LowCardinality(String) on the table, so a new kind is a line
# here, never a migration.
FACET_STATEMENT = "statement_kind"
FACET_FUNCTION = "function"
FACET_SETTING = "setting"
FACET_ENGINE = "engine"
FACET_SYSTEM_TABLE = "system_table"
FACET_ERROR = "error_code"
FACET_FORMAT = "format"
FACET_TAG = "tag"

CONF_KEYWORD_BOUND = 1.0
CONF_CALL_SHAPED = 0.8

DEFAULT_TARGET_CHARS = 2000
DEFAULT_MAX_CHARS = 6000
_PREFIX_MAX_NAMES = 8


@dataclass(frozen=True)
class Hint:
    """The client's TestHint for one statement."""

    errors: tuple[str, ...] = ()  # names or numbers, as written
    echo: str = ""  # echo | echoOn | echoOff | ''
    retry: str = ""  # 'retry N until' | 'retry N while' | ''

    def summary(self) -> str:
        parts = []
        if self.errors:
            parts.append("expects " + ", ".join(self.errors))
        if self.echo:
            parts.append(self.echo)
        if self.retry:
            parts.append(self.retry)
        return " · ".join(parts)


@dataclass
class Statement:
    ordinal: int  # 1-based position in the file
    start_line: int  # first line of the leading comments, 1-based
    end_line: int  # last line of the statement (its hint line included)
    text: str  # leading comments + statement + trailing same-line comment
    body: str  # the statement with comments stripped
    kind: str  # SELECT | CREATE TABLE | SET | SYSTEM FLUSH | …
    hint: Hint
    comments: str = ""  # the leading comment lines, for doc_comment

    @property
    def name(self) -> str:
        return f"stmt_{self.ordinal:04d}"

    def signature(self) -> str:
        s = self.hint.summary()
        return f"{self.kind} · {s}" if s else self.kind


@dataclass(frozen=True)
class Header:
    tags: tuple[str, ...] = ()
    random_settings_limits: str = ""
    memory_limits: str = ""


@dataclass(frozen=True)
class Facet:
    kind: str
    name: str
    ordinal: int  # 0 for a file-level facet (a tag)
    line: int
    confidence: float


@dataclass
class SqlUnits:
    """Everything one file yields; `prefixes` is aligned with `chunks`."""

    statements: list[Statement]
    header: Header
    symbols: list[Symbol]
    chunks: list[Chunk]
    prefixes: list[str]
    facets: list[Facet] = field(default_factory=list)


# --------------------------------------------------------------------------
# header
# --------------------------------------------------------------------------

_HEADER_KEYS = ("Tags:", "Random settings limits:", "Memory limits:")


def parse_header(text: str, sign: str = "--") -> Header:
    """The runner's header lines, found the way `tests/clickhouse-test`
    finds them: any line starting with the comment sign whose remainder,
    left-stripped, starts with the key. Tags are split on `,` and
    stripped."""
    tags: tuple[str, ...] = ()
    rsl = mem = ""
    for line in text.splitlines():
        if not line.startswith(sign):
            continue
        rest = line[len(sign) :].lstrip()
        if rest.startswith("Tags:") and not tags:
            tags = tuple(t.strip() for t in rest[len("Tags:") :].split(",") if t.strip())
        elif rest.startswith("Random settings limits:") and not rsl:
            rsl = rest[len("Random settings limits:") :].strip()
        elif rest.startswith("Memory limits:") and not mem:
            mem = rest[len("Memory limits:") :].strip()
    return Header(tags=tags, random_settings_limits=rsl, memory_limits=mem)


def is_header_line(line: str, sign: str = "--") -> bool:
    if not line.startswith(sign):
        return False
    rest = line[len(sign) :].lstrip()
    return rest.startswith(_HEADER_KEYS)
