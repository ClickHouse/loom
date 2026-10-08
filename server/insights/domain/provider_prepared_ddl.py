"""Preserve canonical live DDL while resizing CREATE-only vector codecs.

Only fixed-arity Quantized codecs on inventoried vectors are rewritten. Unknown
dimension-bound layouts require an operator plan; they are never downgraded to
plain arrays. Tokens keep quoted defaults, comments and codec chains intact.
"""

import re
from dataclasses import dataclass

from server.insights.domain.provider_rebuild import EMBEDDING_SURFACES

_TOKENS = re.compile(
    r"'(?:\\.|''|[^'\\])*'|`(?:\\.|``|[^`\\])*`|"
    r'"(?:\\.|""|[^"\\])*"|[A-Za-z_][A-Za-z_0-9]*|[0-9]+|[^\s]'
)
_METHODS = {"'rabitq'", "'turboquant'", "'int8'"}


@dataclass(frozen=True)
class PreparedDDL:
    sql: str
    padded_columns: tuple[str, ...] = ()

    def projection(self, columns: tuple[str, ...], dimensions: int) -> str:
        return ", ".join(
            f"if(empty(`{column}`), arrayResize(`{column}`, {dimensions}, 0), `{column}`)"
            if column in self.padded_columns
            else f"`{column}`"
            for column in columns
        )


def _column_ranges(tokens: list[re.Match[str]], start: int) -> tuple[list[tuple[int, int]], int]:
    depth, first = 1, start + 1
    ranges = []
    for index in range(first, len(tokens)):
        token = tokens[index].group()
        if token == "(":
            depth += 1
        elif token == ")":
            depth -= 1
            if depth == 0:
                ranges.append((first, index))
                return ranges, index
        elif token == "," and depth == 1:
            ranges.append((first, index))
            first = index + 1
    raise ValueError("Unbalanced canonical table DDL")


def _codec_edits(
    tokens: list[re.Match[str]], ranges: list[tuple[int, int]], vectors: set[str], dimensions: int
) -> tuple[list[tuple[int, int, str]], tuple[str, ...]]:
    edits, padded = [], []
    for first, end in ranges:
        parts = [token.group() for token in tokens[first:end]]
        column = parts[0].strip("`") if parts else ""
        if parts and parts[0].upper() in {"CONSTRAINT", "INDEX", "PROJECTION"}:
            if any(part.strip("`") in vectors for part in parts):
                raise ValueError("Vector indexes and constraints require a separate dimension plan")
        for offset, part in enumerate(parts):
            if part.lower() != "quantized":
                continue
            codec = parts[offset : offset + 8]
            if (
                column not in vectors
                or parts[1:5] != ["Array", "(", "BFloat16", ")"]
                or offset < 2
                or parts[offset - 2 : offset] != ["CODEC", "("]
                or len(codec) != 8
                or codec[1] != "("
                or codec[2] not in _METHODS
                or codec[3] != ","
                or not codec[4].isdigit()
                or codec[5:] != [",", "0", ")"]
                or column in padded
            ):
                raise ValueError("Unsupported CREATE-only vector codec")
            if codec[2] != "'int8'" and dimensions % 8:
                raise ValueError("Quantized vector dimensions must be a multiple of eight")
            width = tokens[first + offset + 4]
            edits.append((width.start(), width.end(), str(dimensions)))
            padded.append(column)
    return edits, tuple(padded)


def prepared_ddl(ddl: str, table: str, prepared: str, marker: str, dimensions: int) -> PreparedDDL:
    """Rewrite only name, UUID, ownership comment and recognized codec widths."""
    if type(dimensions) is not int or not 1 <= dimensions <= 65535:
        raise ValueError("Invalid prepared vector dimensions")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", prepared) or "'" in marker:
        raise ValueError("Invalid prepared table identity")
    vectors = {surface.column for surface in EMBEDDING_SURFACES if surface.table == table}
    head = re.match(
        rf"^CREATE TABLE (?:`?loom`?\.)`?{re.escape(table)}`?"
        r"(?: UUID '[0-9a-f-]{36}')?\s*(?=\()",
        ddl,
    )
    if head is None:
        raise ValueError("Unsupported canonical table DDL")
    tokens = list(_TOKENS.finditer(ddl))
    start = next(index for index, token in enumerate(tokens) if token.start() == head.end())
    ranges, end = _column_ranges(tokens, start)
    edits, padded = _codec_edits(tokens, ranges, vectors, dimensions)
    edits.append((0, head.end(), f"CREATE TABLE loom.{prepared} "))
    comments = [token for token in tokens[end + 1 :] if token.group().upper() == "COMMENT"]
    if len(comments) > 1:
        raise ValueError("Unsupported table ownership comment")
    if comments:
        comment = comments[0]
        after = ddl[comment.end() :].strip()
        if not re.fullmatch(r"'(?:\\.|''|[^'\\])*'", after):
            raise ValueError("Unsupported table ownership comment")
        edits.append((comment.start(), len(ddl), f"COMMENT '{marker}'"))
    else:
        edits.append((len(ddl), len(ddl), f" COMMENT '{marker}'"))
    for first, last, replacement in sorted(edits, reverse=True):
        ddl = ddl[:first] + replacement + ddl[last:]
    return PreparedDDL(ddl, padded)
