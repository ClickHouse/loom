"""A Prisma schema's models, enums, mapped tables, relations
and per-block chunks."""

from __future__ import annotations

from server.code.app.index.ingest_parse import parse_file
from server.code.domain.extract.lang_prisma import (
    blocks,
    parse_prisma,
    prisma_chunks,
    prisma_edges,
    prisma_units,
)

_SCHEMA = """// Prisma schema
generator client {
  provider = "prisma-client-js"
}

datasource db {
  provider = "postgresql"
  url      = env("DATABASE_URL")
}

// Necessary for auth
model Account {
  id     String @id @default(cuid())
  userId String @map("user_id")
  user   User   @relation(fields: [userId], references: [id], onDelete: Cascade)
  scope  ApiKeyScope @default(PROJECT)

  @@unique([provider, providerAccountId]) // one per provider
  @@map("accounts")
}

model User {
  id       String    @id
  accounts Account[]
  role     Role?
}

enum ApiKeyScope {
  ORGANIZATION
  PROJECT
}

enum Role {
  OWNER
  ADMIN
}
"""


def test_blocks_models_enums_and_mapped_tables() -> None:
    b = blocks(_SCHEMA)
    assert [(x.kind, x.name, x.start_line, x.end_line) for x in b] == [
        ("generator", "client", 2, 4),
        ("datasource", "db", 6, 9),
        ("model", "Account", 12, 20),
        ("model", "User", 22, 26),
        ("enum", "ApiKeyScope", 28, 31),
        ("enum", "Role", 33, 36),
    ]
    assert b[2].doc == "// Necessary for auth" and b[2].mapped_name() == "accounts"
    assert b[3].mapped_name() == "User"
    u = prisma_units("packages/shared/prisma/schema.prisma", _SCHEMA)
    s = {x.qualified_name: x for x in u.symbols}
    m = "packages.shared.prisma.schema"
    assert s[f"{m}::Account"].kind == "model" and s[f"{m}::Account"].signature == "accounts"
    assert s[f"{m}::ApiKeyScope"].kind == "enum" and s[f"{m}::Role"].kind == "enum"
    assert [(f.kind, f.name, f.line) for f in u.facets] == [
        ("creates", "accounts", 12),
        ("creates", "User", 22),
    ]
    assert u.edges == [
        (f"{m}::Account", f"{m}::User", "uses_type", 15),
        (f"{m}::Account", f"{m}::ApiKeyScope", "uses_type", 16),
        (f"{m}::User", f"{m}::Account", "uses_type", 24),
        (f"{m}::User", f"{m}::Role", "uses_type", 25),
    ]


def test_parse_file_routes_prisma_and_chunks_are_blocks() -> None:
    pf = parse_file("packages/shared/prisma/schema.prisma", _SCHEMA.encode())
    assert pf is not None and pf.lang == "prisma" and len(pf.symbols) == 4
    chunks = prisma_chunks(pf)
    assert [(c.kind, c.start_line, c.end_line) for c in chunks] == [
        ("other", 2, 4),
        ("other", 6, 9),
        ("class_skeleton", 12, 20),
        ("class_skeleton", 22, 26),
        ("class_skeleton", 28, 31),
        ("class_skeleton", 33, 36),
    ]
    account = chunks[2]
    assert account.content.startswith("// Necessary for auth\nmodel Account {")
    assert account.symbol_ids == [pf.symbols[0].id]
    edges = prisma_edges(pf)
    assert len(edges) == 4 and {e.edge_kind for e in edges} == {"uses_type"}
    assert edges[0].from_symbol_id == pf.symbols[0].id and edges[0].confidence == 1.0


def test_parse_prisma_matches_units() -> None:
    pf = parse_prisma("a/schema.prisma", _SCHEMA.encode())
    assert [s.name for s in pf.symbols] == ["Account", "User", "ApiKeyScope", "Role"]
