"""The code-plane hooks send `diffHeaders(diff)` in place of the whole diff; the
server must read the same ranges and function names from it as from the whole
diff (`_parse_unified_diff_full`), with no changed line in what is sent."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from server.code.app.symbol.diff import _parse_unified_diff_full

ROOT = Path(__file__).resolve().parents[2]
CLIENT = ROOT / "integrations" / "claude-code-plugin" / "lib" / "loom-client.mjs"

HEADS = [
    "bool PrivateKey::verify(const std::string & data)",
    "template <class T> void Foo<T>::bar(int x)",
    "std::vector<int> BaseSettings<TTraits>::write(WriteBuffer & out)",
    "pub fn parse<'a>(input: &'a str) -> Result<'a>",
    "export function useState<S = undefined>(init: S)",
    'extern "C" int init(void)',
    "def café(x):",
    "Foo::~Foo()",
    "template <typename T, typename = std::enable_if_t<std::is_integral_v<T>>> void f(T x)",
    "const handler = function handler(req)",
    "public: void g(int)",
    # the server's splitlines ends the line at \x85, so neither side names `h`
    "void\x85h(int)",
    "    if (ready) {",
    "public:",
    "DB_PASSWORD=hunter2",
    "",
]


def _diff() -> str:
    hunks = "".join(
        f"@@ -{10 * i} +{10 * i},2 @@{' ' + head if head else ''}\n-old {i} password=x\n+new {i}\n+more\n"
        for i, head in enumerate(HEADS, start=1)
    )
    return (
        "diff --git a/src/a.cpp b/src/a.cpp\nindex 1111111..2222222 100644\n"
        "--- a/src/a.cpp\n+++ b/src/a.cpp\n"
        + hunks
        + "diff --git a/n.py b/n.py\nnew file mode 100644\n"
        "--- /dev/null\n+++ b/n.py\n@@ -0,0 +1,2 @@\n+def f():\n+    pass\n"
    )


def _headers(diff: str, home: Path) -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    script = (
        f"import({json.dumps(CLIENT.as_uri())}).then((m) => "
        "process.stdout.write(m.diffHeaders(require('fs').readFileSync(0, 'utf8'), 200000, '')))"
    )
    # No LOOM_* settings and an empty HOME: the helper must not read this machine's
    # token or config.
    env = {k: v for k, v in os.environ.items() if not k.startswith(("LOOM_", "CLAUDE_"))}
    env["HOME"] = str(home)
    return subprocess.run(
        [node, "-e", script],
        input=diff,
        capture_output=True,
        text=True,
        check=True,
        env=env,
        timeout=60,
    ).stdout


def test_the_server_reads_the_same_ranges_and_names(tmp_path: Path) -> None:
    diff = _diff()
    sent = _headers(diff, tmp_path)
    assert _parse_unified_diff_full(sent) == _parse_unified_diff_full(diff)
    ranges, names = _parse_unified_diff_full(sent)
    assert {
        "PrivateKey::verify",
        "Foo::bar",
        "BaseSettings::write",
        "parse",
        "useState",
        "init",
        "f",
        "handler",
        "g",
    } <= names["src/a.cpp"]


def test_no_changed_line_is_sent(tmp_path: Path) -> None:
    sent = _headers(_diff(), tmp_path)
    assert "password" not in sent.lower() and "hunter2" not in sent
    assert not [line for line in sent.splitlines() if line.startswith("index ")]
    assert not [
        line
        for line in sent.splitlines()
        if line[:1] in "+- " and not line.startswith(("+++", "---"))
    ]
