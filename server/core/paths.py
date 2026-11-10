"""Where the checkout's own files are, independent of where a module sits.

A module that finds files by counting parent directories from its own
`__file__` breaks the moment it moves to another package. Everything that reads
a file shipped with the server (the migrations, schema.sql, the scip harness
scripts, the built dashboard) resolves it from REPO_ROOT instead.
"""

from __future__ import annotations

from pathlib import Path

# server/core/paths.py -> server/core -> server -> the checkout root.
REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS_DIR = REPO_ROOT / "schemas" / "migrations"
PG_MIGRATIONS_DIR = REPO_ROOT / "schemas" / "pg_migrations"
SCHEMA_SQL = REPO_ROOT / "schemas" / "schema.sql"
FRONTEND_DIST = REPO_ROOT / "frontend" / "dist"
SCIP_HARNESS_DIR = REPO_ROOT / "scripts" / "scip_harness"
AGENT_GUIDE_DIR = REPO_ROOT / "skills" / "loom"
# Loom's documentation, which help.search and help.get read (the image copies it).
DOCS_DIR = REPO_ROOT / "docs"
# The assistant's agent definition, which a boot seeds into LibreChat.
ASSISTANT_AGENT = REPO_ROOT / "integrations" / "librechat" / "agent.yaml"
