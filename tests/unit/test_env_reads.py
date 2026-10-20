"""The server reads its configuration through `server.settings`, never the
environment directly.

A `LOOM_*` value read with `os.environ` bypasses validation, the dotenv file, the
configuration reference, `loom settings list` and runtime overrides. The files
below read the environment for something that is not a Loom setting; each says
what. A new read goes into a settings field instead.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

ALLOWED: dict[str, str] = {
    "server/core/env_file.py": "which dotenv and settings files to load, before settings exist",
    "server/core/provider_keys.py": "provider credentials under their vendors' variable names",
    "server/core/llm_provider.py": "the LLM provider implied by which vendor key is set",
    "server/observability/problems.py": "which vendor keys are set, for health problems",
    "server/observability/instance.py": "the host name and the process's own instance label",
    "server/core/provider_process.py": "the Kubernetes downward API (pod name and namespace)",
    "server/core/runtime_files.py": "XDG_RUNTIME_DIR",
    "server/core/webhooks.py": "webhook secrets, each named by its registration or inbox row",
    "server/workers/child.py": "the environment handed to the workers child process",
    "server/code/app/index/gitrepo.py": "the environment handed to git",
    "server/tracker/app/patch_history.py": "the environment handed to git",
    "server/code/workers/scip_go_delta.py": "the environment handed to the indexer",
    "server/code/workers/scip_py_delta.py": "the environment handed to the indexer",
    "server/code/workers/scip_ts_delta.py": "the environment handed to the indexer",
}
# The CLI runs on operators' and users' machines: client config, flag defaults and the
# environment of the processes it starts.
ALLOWED_DIRS = ("server/settings/", "server/cli/")


def _reads_environment(tree: ast.Module) -> list[int]:
    lines = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in ("environ", "getenv"):
            if isinstance(node.value, ast.Name) and node.value.id == "os":
                lines.append(node.lineno)
        elif isinstance(node, ast.ImportFrom) and node.module == "os":
            if any(a.name in ("environ", "getenv") for a in node.names):
                lines.append(node.lineno)
    return lines


def test_only_the_listed_files_read_the_environment() -> None:
    found = []
    for path in sorted((ROOT / "server").rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        if rel in ALLOWED or rel.startswith(ALLOWED_DIRS):
            continue
        found += [f"{rel}:{n}" for n in _reads_environment(ast.parse(path.read_text("utf-8")))]
    assert not found, (
        "read configuration as `settings.<field>` (a field in server/settings/), "
        "not from the environment:\n" + "\n".join(found)
    )


def test_every_listed_file_still_reads_it() -> None:
    stale = [
        rel
        for rel in ALLOWED
        if not (ROOT / rel).exists() or not _reads_environment(ast.parse((ROOT / rel).read_text()))
    ]
    assert not stale, f"drop these from ALLOWED: {stale}"


def test_the_walk_sees_each_spelling() -> None:
    src = "import os\nfrom os import getenv\na = os.environ\nb = os.getenv('X')\n"
    assert _reads_environment(ast.parse(src)) == [2, 3, 4]
