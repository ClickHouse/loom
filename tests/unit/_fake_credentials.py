"""Credential shapes `reads_high` matches, for tests. Never real secrets: each is split
so the repository's secret scan (`scripts/gates/check_no_secrets.py`) does not read it as a
key."""

OPENAI_KEY = "sk-" + "proj-" + "Ab3" * 12
AWS_KEY_ID = "AKIA" + "IOSFODNN7EXAMPLE"
# A 2048-bit RSA key's size in PEM form: 27 lines of 64 base64 characters between its
# header and its closing line.
PRIVATE_KEY_LINES = [(f"Kq{n:02d}" + "Zx7Wv3Bn") * 5 + f"Kq{n:02d}" for n in range(27)]
PRIVATE_KEY_BLOCK = "\n".join(
    [
        "-----BEGIN RSA " + "PRIVATE KEY-----",
        *PRIVATE_KEY_LINES,
        "-----END RSA " + "PRIVATE KEY-----",
    ]
)
