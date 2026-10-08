"""Environment-variable reads are `env_var` facets."""

from __future__ import annotations

from server.code.app.index.ingest_parse import _facets_ride_symbols, parse_file
from server.code.domain.extract.env_reads import env_facets


def test_typescript_reads_in_every_spelling_become_facets() -> None:
    pf = parse_file(
        "src/env.ts",
        b"const url = process.env.DATABASE_URL\n"
        b"const k = process.env['API_KEY']\n"
        b"const v = import.meta.env.VITE_HOST\n"
        b"const { REDIS_HOST, REDIS_PORT = '6379' } = process.env\n"
        b'const d = Deno.env.get("DENO_X")\n'
        b"const n = process.env.npm_config_registry\n",
    )
    got = [(f.kind, f.name, f.line, f.confidence) for f in env_facets(pf)]
    assert got == [
        ("env_var", "DATABASE_URL", 1, 1.0),
        ("env_var", "API_KEY", 2, 1.0),
        ("env_var", "VITE_HOST", 3, 1.0),
        ("env_var", "REDIS_HOST", 4, 1.0),
        ("env_var", "REDIS_PORT", 4, 1.0),
        ("env_var", "DENO_X", 5, 1.0),
    ]
    assert [f.ordinal for f in env_facets(pf)] == [1, 2, 3, 4, 5, 6]


def test_python_reads_become_facets_and_a_plain_file_has_none() -> None:
    pf = parse_file(
        "app/settings.py",
        b"import os\nfrom os import getenv\n"
        b"A = os.environ['DATABASE_URL']\nB = os.environ.get('REDIS_URL', '')\n"
        b"C = getenv('SECRET_KEY')\n",
    )
    assert [(f.name, f.line) for f in env_facets(pf)] == [
        ("DATABASE_URL", 3),
        ("REDIS_URL", 4),
        ("SECRET_KEY", 5),
    ]
    plain = parse_file("app/util.py", b"def f():\n    return 1\n")
    assert env_facets(plain) == []
    assert _facets_ride_symbols(plain, plain.path, False, env_facets(plain)) is False
    assert _facets_ride_symbols(pf, pf.path, False, env_facets(pf)) is True
