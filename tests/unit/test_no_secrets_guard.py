"""The secret guard has to fire on real credentials and stay quiet on fixtures.

A scanner nobody trusts gets disabled, and a scanner that finds nothing is
indistinguishable from one that is broken. So both directions are tested: real
credential shapes must be caught, and the placeholders that legitimately live in
`.env.example`, compose files and help strings must not be.

`test_repo_is_clean` is the one that runs in anger — it scans every tracked file
on every `pytest tests/unit`, so a finding fails the build rather than waiting for a
review to notice.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from scripts.gates import check_no_secrets as mod
from scripts.gates.check_no_secrets import _looks_like_a_credential, _scan

ROOT = Path(__file__).resolve().parents[2]


def _findings(tmp_path: Path, name: str, content: str, monkeypatch) -> list:
    """Scan one throwaway file through the real scanner."""
    f = tmp_path / name
    f.write_text(content)
    monkeypatch.setattr(mod, "ROOT", tmp_path)
    return _scan(name)


# --- must fire -----------------------------------------------------------------
# Every provider format loom holds.
# Assembled from fragments at runtime rather than written as literals: the
# fixtures ARE credentials by construction, so a literal here is a true positive. Splitting them keeps the
# scanner blind to no file at all, which is better than an exemption it could
# hide behind.
_BODY = "Ab3dEfGh1jKlMn0pQrStUvWx2Yz4Ab6dEfGh8jKl"
_HEX = "7a3b9c1d2e4f5a6b7c8d9e0f1a2b3c4d"


def _line(var: str, *parts: str) -> str:
    return var + "=" + "".join(parts)


CAUGHT = [
    ("openai.txt", _line("OPENAI_API_KEY", "sk-", "proj-", _BODY)),
    ("anthropic.txt", _line("ANTHROPIC_API_KEY", "sk-", "ant-api03-", _BODY[:28])),
    ("lf.txt", _line("LOOM_LANGFUSE_ADMIN_TOKEN", "pk-", "lf-", _HEX)),
    ("chpw.txt", _line("LOOM_CH_PASSWORD", "Xk92mQ7v", "Lp3RtY8w")),
    ("jwt.txt", _line("LOOM_JWT_SECRET", "9f2b7c4e1a8d6503", "bE7fA1c9D2e4F6a8")),
    ("aws.txt", "aws_access_key_id = " + "AKIA" + "IOSFODNN7ABCDEFG"),
    ("key.pem.txt", "-----BEGIN " + "PRIVATE KEY-----\nMIIEvQIBADANBg\n"),
]


@pytest.mark.parametrize("name,content", CAUGHT)
def test_real_credential_shapes_are_caught(
    tmp_path: Path, monkeypatch, name: str, content: str
) -> None:
    hits = _findings(tmp_path, name, content, monkeypatch)
    assert hits, f"scanner missed a live-looking credential in {content[:32]!r}"


# --- must stay quiet -----------------------------------------------------------
# Every one of these is a real line from this repo that a naive scanner flags.
# They are the reason it gates on shape.
IGNORED = [
    ("env.example.txt", "LOOM_JWT_SECRET=dev-only-jwt-secret-do-not-use-in-prod"),
    ("ci.txt", "LOOM_JWT_SECRET: ci-only-jwt-secret"),
    ("compose.txt", "OPENAI_API_KEY=${OPENAI_API_KEY}"),
    ("help.txt", "raise SystemExit(\"set LOOM_LANGFUSE_ADMIN_TOKEN='pk-lf-...:sk-lf-...'\")"),
    ("docs.txt", "LOOM_CH_PASSWORD=your-password-here"),
    ("shell.txt", 'LOOM_CH_PASSWORD="${LOOM_CH_PASSWORD:?set it}"'),
    ("keydoc.txt", '# LOOM_JWT_SECRETS_JSON={"prod-2026":"-----BEGIN PRIVATE KEY-----..."}'),
    ("example_sk.txt", "ANTHROPIC_API_KEY=sk-ant-example-not-a-real-key-000"),
    ("kebab.txt", "css_class = 'sk-" + "navigation-drawer-collapsed-state'"),
    ("binary.bin", "OPENAI_API_KEY=" + "sk-" + "proj-" + _BODY),
]


@pytest.mark.parametrize("name,content", IGNORED)
def test_placeholders_do_not_fire(tmp_path: Path, monkeypatch, name: str, content: str) -> None:
    hits = _findings(tmp_path, name, content, monkeypatch)
    assert not hits, f"false positive on a known-safe line: {content[:60]!r} -> {hits}"


@pytest.mark.parametrize(
    "content,label",
    [
        (_line("X", "sk-", "ant-api03-", _BODY[:28]), "Anthropic API key"),
        (_line("X", "sk-", "lf-", _HEX), "Langfuse secret key"),
        (_line("X", "sk-", "proj-", _BODY), "OpenAI API key"),
    ],
)
def test_one_key_is_one_finding(tmp_path: Path, monkeypatch, content: str, label: str) -> None:
    hits = _findings(tmp_path, "one.txt", content, monkeypatch)
    assert [h.label for h in hits] == [label]


def test_findings_are_redacted(tmp_path: Path, monkeypatch) -> None:
    secret = "sk-" + "proj-" + _BODY
    (hit,) = _findings(tmp_path, "r.txt", "OPENAI_API_KEY=" + secret, monkeypatch)
    assert secret not in str(hit) and _BODY[4:-2] not in str(hit)
    assert str(hit).startswith("r.txt:1: OpenAI API key (")


def test_shape_gate_requires_letters_digits_and_length() -> None:
    assert _looks_like_a_credential("9f2b7c4e1a8d6503bE7fA1c9D2e4F6a8")
    assert not _looks_like_a_credential("short1a")  # too short
    assert not _looks_like_a_credential("all-lowercase-words")  # no digit
    assert not _looks_like_a_credential("1234567890123456")  # no letter
    assert not _looks_like_a_credential("${SOME_VARIABLE_NAME}")  # not the alphabet
    assert not _looks_like_a_credential("ci-only-secret-00000")  # obviously fake


def test_repo_is_clean() -> None:
    """The guard, run in anger, on every tracked file."""
    r = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "gates" / "check_no_secrets.py")],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    assert r.returncode == 0, (
        f"a tracked file appears to contain a live credential:\n{r.stdout}\n{r.stderr}"
    )
