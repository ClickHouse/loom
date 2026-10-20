"""Credential shapes `reads_high` matches, for tests. Never real secrets: each is split
so the repository's secret scan (`scripts/gates/check_no_secrets.py`) does not read it as a
key."""

OPENAI_KEY = "sk-" + "proj-" + "Ab3" * 12
AWS_KEY_ID = "AKIA" + "IOSFODNN7EXAMPLE"
