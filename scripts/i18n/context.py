"""Source declarations whose spelling is part of the documentation contract."""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

# These are source-associated name spans, not words allowed in arbitrary English labels.
_FRAMEWORK_NAMES = (
    "LangChain.js",
    "LangChain",
    "LlamaIndex",
    "OpenAI Agents SDK",
    "CrewAI",
    "AutoGen",
    "AG2",
    "Microsoft Agent Framework",
    "Pydantic AI",
    "Mastra",
    "AI SDK",
    "n8n",
    "LangGraph",
    "Strands Agents",
)
_NAMES = {
    "docs/features/integrations/frameworks.md": _FRAMEWORK_NAMES,
    "docs/features/integrations/index.md": (
        *_FRAMEWORK_NAMES,
        "Agent Substrate",
        "GitHub Copilot CLI",
        "Cline CLI",
    ),
    "docs/benchmarks/results.md": (
        "MemBench",
        "MemoryAgentBench",
        "MuSiQue-Ans",
        "PerLTQA",
        "LongMemEval-S",
        "LoCoMo",
    ),
    "docs/development/documentation-style.md": ("ClickHouse Cloud", "ClickHouse OSS"),
    "docs/reference/mcp.md": ("OAuth",),
}

# Format names are attested by the source table's parser extension, not English case.
_FORMAT_NAMES = {
    ("docs/features/code-index/overview.md", ".proto"): "Protocol Buffers",
    ("docs/features/code-index/overview.md", ".fbs"): "FlatBuffers",
}

# Each declaration is confined to this source's documented field, and applies only when
# its entire literal vocabulary occurs in order. Surrounding prose remains translatable.
_FIELDS = {
    ("docs/reference/operations/code.md", "verdict"): (
        "gap",
        "pending",
        "covered",
        "not_affected",
        "unknown",
    ),
    ("docs/reference/operations/code.md", "causes"): (
        "tracker_unbound",
        "tracker_unavailable",
        "fix_not_found",
        "no_fix",
        "no_branches",
        "deadline",
        "no_index",
        "no_branch_version",
    ),
    ("docs/reference/operations/policy.md", "condition"): (
        "op",
        "formerly",
        "count_within",
        "not_since",
        "all",
        "any",
        "not",
        "within",
        "operation",
        "key",
        "agent",
        "session",
        "user",
        "success",
        "args_equal",
    ),
    ("docs/reference/operations/tracker.md", "sections"): (
        "template",
        "title",
        "access",
        "params",
        "why",
        "status",
        "served",
        "empty",
        "restricted_required",
        "tracker_unavailable",
        "planned",
        "cites",
        "rows",
    ),
    ("docs/reference/operations/tracker.md", "verdict"): (
        "planned",
        "answered",
        "empty",
        "unknown",
    ),
    ("docs/reference/operations/observe.md", "kind"): (
        "file_edit",
        "command_run",
        "error",
        "web_fetch",
        "file_read",
        "tool_use",
    ),
    ("docs/reference/operations/working_memory.md", "fill_ratio"): ("len(content) / char_limit",),
}
_MODEL_DEFAULTS = {
    (
        "docs/features/memory/conflicts-and-refusal.md",
        "LOOM_CONFLICT_RESOLVER_MODEL",
    ): "gpt-6.1-sol",
}
# These field groups document REQUIRED in server/tracker/domain/contract.py.
_TYPE_FAMILIES = {
    ("docs/features/tracker/record-contract.md", ("labels", "assignees")): "Array(String)",
    (
        "docs/features/tracker/record-contract.md",
        ("affected_versions", "fix_versions"),
    ): "Array(String)",
    (
        "docs/features/tracker/record-contract.md",
        ("triage_reproduces_on", "triage_clean_on"),
    ): "Array(String)",
    (
        "docs/features/tracker/record-contract.md",
        ("test_types", "reviewers", "src_components"),
    ): "Array(String)",
}
_QUOTED = r'"[A-Za-z_][A-Za-z0-9_.-]*"'
_ALTERNATIVES = re.compile(rf"{_QUOTED}(?:\s*/\s*{_QUOTED})+")


@dataclass(frozen=True)
class Label:
    text: str
    minimum: int = 1
    field: str = ""
    string_field: bool = False
    links: tuple[tuple[str, str], ...] = ()
    code: tuple[str, ...] = ()
    start: int = 0
    end: int = 0
    default_for: str = ""
    family_for: tuple[str, ...] = ()


@dataclass(frozen=True)
class SourceContext:
    """Validate one request inside its complete English Markdown containers."""

    path: str
    english: str
    start: int
    end: int

    def candidate(self, reply: str) -> str:
        return self.english[: self.start] + reply + self.english[self.end :]


def pattern(atoms: tuple[str, ...]) -> re.Pattern[str]:
    choices = "|".join(map(re.escape, sorted(atoms, key=len, reverse=True)))
    # Native particles can touch a name; other Unicode identifier continuations cannot.
    native = r"\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af"
    left = rf"(?:(?<![\w-])|(?<=[{native}]))"
    right = rf"(?:(?![\w-])|(?=[{native}]))"
    return re.compile(left + "(?:" + choices + ")" + right)


def _file_target(target: str, source_path: str, files: frozenset[str], repo_url: str) -> str:
    url = urlsplit(target)
    if url.scheme or url.netloc:
        prefixes = (repo_url + "/blob/main/", repo_url + "/raw/main/") if repo_url else ()
        prefix = next((p for p in prefixes if target.startswith(p)), None)
        resolved = unquote(url.path[len(urlsplit(prefix).path) :]) if prefix else ""
    elif source_path and url.path and not url.path.startswith("/"):
        resolved = posixpath.normpath(
            posixpath.join(posixpath.dirname(source_path), unquote(url.path))
        )
    else:
        resolved = ""
    return resolved if resolved in files else ""


def _filename(text: str, target: str) -> str:
    if not target:
        return ""
    parts = target.split("/")
    for start in range(len(parts)):
        name = "/".join(parts[start:])
        if pattern((name,)).search(text):
            return name
    if parts[-1] == "README.md" and pattern(("README",)).search(text):
        return "README"
    return ""


def protected(
    label: Label, source_path: str, *, files: frozenset[str] = frozenset(), repo_url: str = ""
) -> tuple[str, ...]:
    """Derive protection from the source node, never from a translated assertion."""
    atoms = [name for name in _NAMES.get(source_path, ()) if pattern((name,)).search(label.text)]
    for extension in label.code:
        name = _FORMAT_NAMES.get((source_path, extension))
        if name and pattern((name,)).search(label.text):
            atoms.append(name)
    declared = _FIELDS.get((source_path, label.field), ())
    if declared and pattern(declared).findall(label.text) == list(declared):
        atoms.extend(declared)
    default = _MODEL_DEFAULTS.get((source_path, label.default_for))
    if default and pattern((default,)).search(label.text):
        atoms.append(default)
    family = _TYPE_FAMILIES.get((source_path, label.family_for))
    if family and pattern((family,)).search(label.text):
        atoms.append(family)
    if (
        source_path.startswith("docs/reference/operations/")
        and label.string_field
        and _ALTERNATIVES.fullmatch(label.text.strip())
    ):
        atoms.extend(value[1:-1] for value in re.findall(_QUOTED, label.text))
    for text, target in label.links:
        name = _filename(text, _file_target(target, source_path, files, repo_url))
        if name:
            atoms.append(name)
    return tuple(dict.fromkeys(atoms))


def project(label: Label, atoms: tuple[str, ...]) -> tuple[str, tuple[str, ...]]:
    if not atoms:
        return label.text, ()
    matcher = pattern(atoms)
    return matcher.sub(" ", label.text), tuple(matcher.findall(label.text))
