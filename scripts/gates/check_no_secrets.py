#!/usr/bin/env python3
"""Fail if a tracked file carries a credential.

`.gitignore` only stops the next file with a name you already thought of. It does
nothing about a differently-named backup, a key pasted into a doc, or one
hardcoded in a script. So this guard is on content, and it runs in CI on every
push.

Deliberately narrow. A scanner that cries wolf gets disabled, which is worse than
no scanner, so it matches only shapes that are unambiguously credentials:
provider-prefixed keys with their own length and alphabet requirements, and
assignments to the variables loom keeps secrets in — and for that last, generic
case the captured value must additionally have the SHAPE of a credential. Gating
on shape rather than on a list of known-fake strings matters: an allowlist is a
file people add to until it matches everything.

    python scripts/gates/check_no_secrets.py          # scan tracked files
    python scripts/gates/check_no_secrets.py --all    # include untracked (local pre-commit use)
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# A credential-shaped value: long, and drawn only from the alphabet real keys use
# (so `${VAR}`, `pk-lf-...`, `changeme` and `dev-jwt-token` are all out), with at
# least one digit and one letter. Placeholders overwhelmingly fail one of these,
# which is why the check gates on SHAPE rather than on a growing allowlist of
# known-fake strings — an allowlist is a file people add to until it matches
# everything.
_CREDENTIAL_SHAPE = re.compile(r"^[A-Za-z0-9+/=_-]{16,}$")
_OBVIOUSLY_FAKE = re.compile(
    r"(?i)\b(ci[-_]?only|test|fake|dummy|example|placeholder|changeme|replace[-_]?me|sample|your[-_])",
)


def _looks_like_a_credential(value: str) -> bool:
    v = value.strip().strip("\"'")
    if not _CREDENTIAL_SHAPE.match(v):
        return False
    if not (any(c.isdigit() for c in v) and any(c.isalpha() for c in v)):
        return False
    return not _OBVIOUSLY_FAKE.search(v)


# (label, regex). Each must capture the secret itself in group 1, so the shape
# gate applies to the VALUE and not to the whole line. The OpenAI pattern
# excludes the other `sk-` providers so one key is one finding.
_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("OpenAI API key", re.compile(r"\b(sk-(?!ant-|lf-)(?:proj-)?[A-Za-z0-9_-]{24,})")),
    ("Anthropic API key", re.compile(r"\b(sk-ant-[A-Za-z0-9_-]{24,})")),
    ("Langfuse secret key", re.compile(r"\b(sk-lf-[0-9a-fA-F-]{16,})")),
    ("Langfuse public key", re.compile(r"\b(pk-lf-[0-9a-fA-F-]{16,})")),
    ("AWS access key id", re.compile(r"\b(AKIA[0-9A-Z]{16})\b")),
    ("private key block", re.compile(r"(-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----)")),
    (
        "loom secret assignment",
        re.compile(
            r"""(?x)
            \b(?:LOOM_CH_PASSWORD|LOOM_JWT_SECRET|LOOM_LANGFUSE_ADMIN_TOKEN
              |OPENAI_API_KEY|ANTHROPIC_API_KEY)
            \s*[:=]\s*["']?([^\s"'#,}]{8,})
            """
        ),
    ),
]

# Binary-ish and vendored paths where a match is noise.
_SKIP_SUFFIX = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".svg",
    ".ico",
    ".pdf",
    ".woff",
    ".woff2",
    ".ttf",
    ".eot",
    ".zip",
    ".gz",
    ".tar",
    ".whl",
    ".so",
    ".bin",
    ".lock",
}
_SKIP_PARTS = {".git", "node_modules", ".venv", "dist", "build", "__pycache__"}
# This file necessarily contains the patterns it looks for.
_SKIP_FILES = {Path(__file__).resolve().relative_to(ROOT).as_posix()}


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    label: str
    value: str

    def __str__(self) -> str:
        masked = self.value[:4] + "…" + self.value[-2:] if len(self.value) > 8 else "…"
        return f"{self.path}:{self.line}: {self.label} ({masked})"


def _tracked_files(include_untracked: bool) -> list[str]:
    cmd = ["git", "ls-files", "-z"]
    if include_untracked:
        cmd = ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"]
    out = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, check=True)
    return [p for p in out.stdout.split("\0") if p]


def _scan(path: str) -> list[Finding]:
    p = ROOT / path
    if path in _SKIP_FILES or p.suffix.lower() in _SKIP_SUFFIX:
        return []
    if set(Path(path).parts) & _SKIP_PARTS:
        return []
    try:
        text = p.read_text(encoding="utf-8", errors="strict")
    except (OSError, UnicodeDecodeError):
        return []
    hits: list[Finding] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for label, pattern in _PATTERNS:
            for m in pattern.finditer(line):
                value = m.group(1)
                # Provider-prefixed patterns are self-validating (they encode
                # their own length and alphabet); the generic assignment pattern
                # is not, so it has to clear the shape gate.
                if label == "loom secret assignment" and not _looks_like_a_credential(value):
                    continue
                if label != "private key block" and _OBVIOUSLY_FAKE.search(value):
                    continue
                # Random key bodies carry digits; `sk-some-long-kebab-name` is
                # an identifier.
                if value.startswith("sk-") and not any(c.isdigit() for c in value):
                    continue
                # A BEGIN marker truncated with an ellipsis is documentation
                # showing the SHAPE of a key, not a key. A real one is followed
                # by base64, not by "...".
                if label == "private key block" and "..." in line:
                    continue
                # A provider key assigned to a secret variable is one finding.
                if any(h.line == lineno and h.value == value for h in hits):
                    continue
                hits.append(Finding(path, lineno, label, value))
    return hits


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--all",
        action="store_true",
        help="Also scan untracked files (useful as a local pre-commit check).",
    )
    args = ap.parse_args(argv)

    # A tracked env file is a finding on its own, whatever is inside it.
    env_like = re.compile(r"(^|/)\.env($|\.)")
    tracked = _tracked_files(args.all)
    bad_paths = [p for p in tracked if env_like.search(p) and not p.endswith(".example")]

    findings = [hit for path in tracked for hit in _scan(path)]

    if not findings and not bad_paths:
        print(f"no secrets found in {len(tracked)} files")
        return 0

    for p in bad_paths:
        print(f"{p}: an env file is tracked — untrack it and add it to .gitignore")
    for finding in findings:
        print(finding)
    print(
        f"\n{len(findings) + len(bad_paths)} finding(s). If one is a false positive, "
        "make the value obviously fake (a `test-`/`example-` prefix) rather than "
        "widening the pattern.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
