"""Shell that lives inside other files — host islands.

A repository's shell is not only in `.sh` files. GitHub Actions `run:` blocks
(ClickHouse: 34 workflows, 1,110 of them), GitLab `script:` lists, Dockerfile `RUN`
instructions (49 Dockerfiles, 237), Makefile recipes and Jenkinsfile `sh '''…'''` steps
are shell a plain reader sees as YAML, as nothing (Dockerfile and Makefile have no
extension the language table knows), or as Groovy. What a reader wants from them is what
`lang_shell_islands` gives a shell file: the programs they run (`program` facets), the
repository scripts they invoke (`includes` edges) and any SQL they drive (statement
facets under a dialect), plus the host's own structure as symbols — a workflow's jobs, a
Makefile's targets, a Dockerfile's build stages — so `code.outline` and `code.enclosing`
answer on them.

The islands are extracted LINE-PRESERVINGLY: every island's lines are laid
into a synthetic shell text at their original line numbers (the gaps are
blank), host placeholders are substituted (`${{ expr }}` → `__expr__`,
`{{ var }}` → `__var__`, Make's `$(VAR)`/`$@` → `__var__`, `$$` → `$`,
`$(MAKE)` → `make`), and ONE stamped bash parse of that text feeds the
same `program_facets`, `include_targets` and island finder a `.sh` file
goes through. Chunks stay the line-window chunker's: a host file's text is
its own, and the islands' facts ride as facets, edges and symbols.

Outside the extractor stamp (see `tests/unit/test_code_extractor_stamp.py`):
like `lang_detect` and `lang_bats`, this decides which files index and adds
rows beside the stamped ones; a namespace gains them by a targeted backfill
over its host paths, never by a heal.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass, field

from server.code.domain.extract.lang_bash import parse_bash
from server.code.domain.extract.lang_python import module_scope
from server.code.domain.extract.lang_shell_islands import (
    find_islands,
    include_targets,
    island_facets,
    island_statements,
    program_facets,
)
from server.code.domain.extract.lang_sql_model import Facet, Header
from server.code.domain.extract.treesitter import ParsedFile, Symbol

HOST_LANG = "config"  # a host file is a config file to the language table: chunk-only

HOST_DOCKER = "dockerfile"
HOST_MAKE = "makefile"
HOST_YAML = "yaml"
HOST_JENKINS = "jenkinsfile"

KIND_JOB = "job"
KIND_TARGET = "target"
KIND_STAGE = "stage"

PLACEHOLDER_EXPR = "__expr__"
PLACEHOLDER_VAR = "__var__"

_MAKE_NAMES = frozenset({"makefile", "gnumakefile", "bsdmakefile"})
_YAML_HOST_RE = re.compile(
    r"(^|/)(\.github/workflows/[^/]+\.ya?ml|\.gitlab-ci\.ya?ml|\.gitlab/ci/[^/]+\.ya?ml"
    r"|azure-pipelines[^/]*\.ya?ml|\.travis\.ya?ml|\.circleci/config\.ya?ml"
    r"|bitbucket-pipelines\.ya?ml|\.drone\.ya?ml|\.woodpecker\.ya?ml"
    r"|cloudbuild\.ya?ml|buildspec\.ya?ml|\.pre-commit-config\.ya?ml)$"
)
# YAML keys whose scalar or list value is a shell command line
_YAML_KEY_RE = re.compile(
    r"^(?P<indent>[ \t]*)(?P<dash>-[ \t]+)?"
    r"(?P<key>run|script|before_script|after_script|bash|shell|command|cmd|entrypoint)"
    r"[ \t]*:[ \t]*(?P<val>.*)$"
)
_YAML_LIST_RE = re.compile(r"^(?P<indent>[ \t]*)-[ \t]+(?P<val>.*)$")
_YAML_TOP_KEY_RE = re.compile(
    r"^(?P<key>[A-Za-z_.](?:[\w.\-/ ]*[\w.\-/])?)[ \t]*+:[ \t]*(?P<val>.*)$"
)
_YAML_JOB_KEY_RE = re.compile(r"^  (?P<key>[A-Za-z_][\w\-]*)[ \t]*:[ \t]*$")
_GITLAB_RESERVED = frozenset(
    {
        "stages",
        "variables",
        "default",
        "include",
        "workflow",
        "image",
        "services",
        "before_script",
        "after_script",
        "cache",
        "spec",
        "pages",
    }
)
_SHELL_NAMES = frozenset({"bash", "sh", "zsh", "pwsh", "powershell", "cmd", "python", "fish"})
_GH_EXPR_RE = re.compile(r"\$\{\{(?:(?!\$\{\{).)*?\}\}")  # stops at the next `${{`: linear
_JINJA_RE = re.compile(r"\{\{(?:(?!\{\{).)*?\}\}")  # stops at the next `{{`: linear

_DOCKER_INSTR_RE = re.compile(r"^\s*(?P<instr>[A-Za-z]+)(?:\s+(?P<rest>.*))?$")
_DOCKER_FLAG_RE = re.compile(r"^--[\w-]+(=\S*)?\s+")
_DOCKER_HEREDOC_RE = re.compile(r"<<-?\s*['\"]?(?P<tag>\w+)['\"]?")
_DOCKER_JSON_SH_RE = re.compile(
    r"""^\[\s*"(?:/bin/)?(?:ba)?sh"\s*,\s*"-c"\s*,\s*"(?P<cmd>.*)"\s*\]$"""
)

_MAKE_TARGET_RE = re.compile(r"^(?P<targets>[^\s:#=](?:[^:#=]*[^\s:#=])?)\s*+::?(?!=)(?P<deps>.*)$")
_MAKE_VAR_RE = re.compile(r"\$\((?P<name>[^()]*)\)|\$\{(?P<brace>[^{}]*)\}|\$[@<^*?+|%]")

_JENKINS_SH_RE = re.compile(
    r"""\bsh\s*+\(?\s*+(?:script\s*+:\s*+)?(?P<q>'''|\"\"\"|'|")""",
)


@dataclass(frozen=True)
class HostIsland:
    """One shell fragment of a host file: its text with the host's
    placeholders substituted, the file line its first line sits on, and
    the host construct that held it."""

    text: str
    line: int
    via: str  # run | script | RUN | CMD | recipe | sh


@dataclass
class HostUnits:
    kind: str
    islands: list[HostIsland]
    symbols: list[Symbol]
    facets: list[Facet] = field(default_factory=list)
    includes: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# detection
# --------------------------------------------------------------------------


def host_kind(path: str) -> str:
    """Which host a path is, or '' — Dockerfiles (`Dockerfile`,
    `Dockerfile.ubuntu`, `x.dockerfile`, `Containerfile`), Makefiles
    (`Makefile`, `GNUmakefile`, `*.mk`), Jenkinsfiles, and the CI YAML files
    every forge names (GitHub Actions, GitLab, Azure, Travis, CircleCI,
    Bitbucket, Drone, Woodpecker, Cloud Build, CodeBuild)."""
    name = posixpath.basename(path)
    lower = name.lower()
    if lower.startswith(("dockerfile", "containerfile")) or lower.endswith(".dockerfile"):
        return HOST_DOCKER
    if lower in _MAKE_NAMES or lower.endswith(".mk"):
        return HOST_MAKE
    if lower == "jenkinsfile" or lower.endswith(".jenkinsfile"):
        return HOST_JENKINS
    if _YAML_HOST_RE.search(path):
        return HOST_YAML
    return ""


def detect_host(path: str) -> str:
    """`config` for a host file the language table does not know
    (Dockerfile, Makefile, Jenkinsfile: no extension), else ''. YAML hosts
    are already `config` by extension."""
    return HOST_LANG if host_kind(path) in (HOST_DOCKER, HOST_MAKE, HOST_JENKINS) else ""


# --------------------------------------------------------------------------
# placeholders
# --------------------------------------------------------------------------


def _strip_quotes(s: str) -> str:
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "'\"":
        return s[1:-1]
    return s


def _yaml_placeholders(text: str) -> str:
    text = _GH_EXPR_RE.sub(PLACEHOLDER_EXPR, text)
    return _JINJA_RE.sub(PLACEHOLDER_VAR, text)


def _make_placeholders(text: str) -> str:
    """Make's own expansions out of the recipe: `$$` is the shell's `$`,
    `$(MAKE)` is `make`, `$(shell x)` is the shell's `$(x)`, every other
    `$(VAR)`/`${VAR}` and automatic variable is a placeholder."""
    parts = text.split("$$")

    def sub(m: re.Match[str]) -> str:
        name = m.group("name") if m.group("name") is not None else m.group("brace")
        if name is None:
            return PLACEHOLDER_VAR
        name = name.strip()
        if name == "MAKE":
            return "make"
        if name.startswith("shell "):
            return "$(" + name[len("shell ") :] + ")"
        return PLACEHOLDER_VAR

    return "$".join(_MAKE_VAR_RE.sub(sub, p) for p in parts)


# --------------------------------------------------------------------------
# extraction, per host
# --------------------------------------------------------------------------


def _dedent(lines: list[str]) -> list[str]:
    widths = [len(ln) - len(ln.lstrip(" \t")) for ln in lines if ln.strip()]
    cut = min(widths) if widths else 0
    return [ln[cut:] if ln.strip() else "" for ln in lines]


def yaml_islands(lines: list[str]) -> list[HostIsland]:
    """`run: |` block scalars, `script:` lists and inline `run: cmd` values,
    GitHub's `${{ }}` and Ansible's `{{ }}` substituted."""
    out: list[HostIsland] = []
    i = 0
    n = len(lines)
    while i < n:
        m = _YAML_KEY_RE.match(lines[i])
        if not m:
            i += 1
            continue
        key_indent = len(m.group("indent")) + len(m.group("dash") or "")
        val = m.group("val").strip()
        if val.startswith(("|", ">")):
            j = i + 1
            block: list[str] = []
            while j < n and (
                not lines[j].strip() or len(lines[j]) - len(lines[j].lstrip(" \t")) > key_indent
            ):
                block.append(lines[j])
                j += 1
            while block and not block[-1].strip():
                block.pop()
            if block:
                text = "\n".join(_dedent(block))
                out.append(HostIsland(_yaml_placeholders(text), i + 2, m.group("key")))
            i = j
            continue
        if val and not val.startswith(("[", "{", "&", "*", "!")) and val != "~":
            value = _strip_quotes(val)
            # GitHub's `shell: bash` names the interpreter; Ansible's `shell: cmd` is a command
            if not (m.group("key") == "shell" and value.split()[0] in _SHELL_NAMES):
                out.append(HostIsland(_yaml_placeholders(value), i + 1, m.group("key")))
            i += 1
            continue
        if not val:
            j = i + 1
            while j < n:
                if not lines[j].strip():
                    j += 1
                    continue
                lm = _YAML_LIST_RE.match(lines[j])
                if not lm or len(lm.group("indent")) < key_indent:
                    break
                item = _strip_quotes(lm.group("val"))
                if item and not item.startswith(("|", ">")):
                    out.append(HostIsland(_yaml_placeholders(item), j + 1, m.group("key")))
                j += 1
            i = j if j > i + 1 else i + 1
            continue
        i += 1
    return out


def yaml_symbols(path: str, lines: list[str]) -> list[Symbol]:
    """A workflow's jobs (`jobs:` → the keys two spaces in) and a GitLab
    pipeline's jobs (a top-level key whose block holds `script:`), one
    `job` symbol each spanning to the next sibling."""
    mod = module_scope(path)
    out: list[Symbol] = []
    n = len(lines)
    in_jobs = False
    jobs: list[tuple[str, int]] = []
    for i, ln in enumerate(lines):
        if ln.startswith("jobs:"):
            in_jobs = True
            continue
        if ln and not ln[0].isspace() and not ln.startswith("#"):
            in_jobs = False
        if in_jobs:
            jm = _YAML_JOB_KEY_RE.match(ln)
            if jm:
                jobs.append((jm.group("key"), i))
    if not jobs:
        # GitLab: top-level keys holding a `script:`
        tops = [
            (m.group("key").strip(), i)
            for i, ln in enumerate(lines)
            if (m := _YAML_TOP_KEY_RE.match(ln)) and not ln[0].isspace()
        ]
        for k, (key, start) in enumerate(tops):
            end = tops[k + 1][1] if k + 1 < len(tops) else n
            if key in _GITLAB_RESERVED or key.startswith("."):
                continue
            if any(
                re.match(r"^\s+(script|before_script|after_script)\s*:", lines[j])
                for j in range(start + 1, end)
            ):
                jobs.append((key, start))
        jobs.sort(key=lambda t: t[1])
    for name, start in jobs:
        stop = n
        for j in range(start + 1, n):
            ln = lines[j]
            if (
                ln
                and not ln.startswith("#")
                and (
                    len(ln) - len(ln.lstrip(" "))
                    <= (2 if in_jobs or lines[start].startswith("  ") else 0)
                )
                and ln.strip()
            ):
                stop = j
                break
        end = stop
        while end > start + 1 and not lines[end - 1].strip():
            end -= 1
        out.append(
            Symbol(
                qualified_name=f"{mod}::{name}",
                name=name,
                kind=KIND_JOB,
                signature=lines[start].strip().rstrip(":"),
                path=path,
                start_line=start + 1,
                end_line=max(start + 1, end),
            )
        )
    return out


def _docker_instruction(lines: list[str], i: int) -> tuple[str, list[str], int]:
    """The instruction at row i with its `\\` continuation rows gathered
    (comment rows inside a continuation are skipped, as Docker skips them);
    returns (instruction, body rows, last row index)."""
    m = _DOCKER_INSTR_RE.match(lines[i])
    instr = (m.group("instr") or "") if m else ""
    body = [(m.group("rest") or "") if m else ""]
    while body[-1].rstrip().endswith("\\") and i + 1 < len(lines):
        i += 1
        body.append("\\" if lines[i].lstrip().startswith("#") else lines[i])
    return instr, body, i


def _docker_shell_text(instr: str, first: str) -> str | None:
    """The shell of a RUN / CMD / ENTRYPOINT / HEALTHCHECK first row: the
    keyword's own flags (`--mount=…`) off, `HEALTHCHECK … CMD` cut to its
    command, `["sh","-c","…"]` unwrapped; None for an exec form (a program
    and its argv, not shell) or a HEALTHCHECK without a CMD."""
    if instr == "HEALTHCHECK":
        cut = first.find("CMD")
        if cut < 0:
            return None
        first = first[cut + 3 :]
    while (f := _DOCKER_FLAG_RE.match(first)) is not None:
        first = first[f.end() :]
    jm = _DOCKER_JSON_SH_RE.match(first.strip())
    if jm:
        return jm.group("cmd").replace('\\"', '"')
    if first.strip().startswith("["):
        return None
    return first


def dockerfile_islands(lines: list[str]) -> tuple[list[HostIsland], list[Symbol]]:
    """`RUN` (with `\\` continuations, `<<EOF` heredocs, `--mount` flags
    dropped, `["sh","-c","…"]` unwrapped), `CMD`/`ENTRYPOINT`/`HEALTHCHECK
    CMD` shell forms, and one `stage` symbol per `FROM … AS name`."""
    islands: list[HostIsland] = []
    stages: list[tuple[str, int, str]] = []
    froms: list[int] = []
    n = len(lines)
    i = 0
    while i < n:
        instr, body, last = _docker_instruction(lines, i)
        if not instr or not instr.isupper():
            i += 1
            continue
        start, i = i, last
        if instr == "FROM":
            froms.append(start)
            words = body[0].split()
            upper = [w.upper() for w in words]
            if "AS" in upper and upper.index("AS") + 1 < len(words):
                stages.append((words[upper.index("AS") + 1], start, body[0]))
        elif instr in ("RUN", "CMD", "ENTRYPOINT", "HEALTHCHECK"):
            text_first = _docker_shell_text(instr, body[0])
            hm = _DOCKER_HEREDOC_RE.search(text_first) if text_first is not None else None
            if text_first is None:
                pass
            elif hm and instr == "RUN":
                tag = hm.group("tag")
                j = i + 1
                while j < n and lines[j].strip() != tag:
                    j += 1
                islands.append(HostIsland("\n".join(_dedent(lines[i + 1 : j])), i + 2, instr))
                i = j
            else:
                islands.append(HostIsland("\n".join([text_first, *body[1:]]), start + 1, instr))
        i += 1
    symbols: list[Symbol] = []
    for name, start, sig in stages:
        end = next((f for f in froms if f > start), n)
        while end > start + 1 and not lines[end - 1].strip():
            end -= 1
        symbols.append(
            Symbol(
                qualified_name=name,
                name=name,
                kind=KIND_STAGE,
                signature=f"FROM {sig}",
                path="",
                start_line=start + 1,
                end_line=max(start + 1, end),
            )
        )
    return islands, symbols


def makefile_islands(lines: list[str]) -> tuple[list[HostIsland], list[Symbol]]:
    """Recipe lines (tab-led, `@`/`-`/`+` prefixes off, Make expansions
    substituted) under each rule, and one `target` symbol per plain
    target (pattern rules, special `.NAME` targets and `$(VAR)` targets
    hold no name a reader asks for)."""
    islands: list[HostIsland] = []
    symbols: list[Symbol] = []
    n = len(lines)
    i = 0
    while i < n:
        ln = lines[i]
        if (
            not ln
            or ln[0] in " \t#"
            or ln.startswith(
                (
                    "define",
                    "ifeq",
                    "ifneq",
                    "ifdef",
                    "ifndef",
                    "else",
                    "endif",
                    "include",
                    "-include",
                    "export",
                    "unexport",
                )
            )
        ):
            i += 1
            continue
        m = _MAKE_TARGET_RE.match(ln)
        if not m:
            i += 1
            continue
        targets = m.group("targets").split()
        start = i
        recipe: list[str] = []
        j = i + 1
        while j < n and (lines[j].startswith("\t") or not lines[j].strip()):
            if lines[j].startswith("\t"):
                recipe.append(lines[j])
            j += 1
        if recipe:
            first_line = next(r for r in range(i + 1, j) if lines[r].startswith("\t")) + 1
            text_lines: list[str] = []
            # recipe rows are contiguous save blank rows; keep their positions
            for row in lines[first_line - 1 : j]:
                if row.startswith("\t"):
                    body = row[1:].lstrip("@-+ ")
                    text_lines.append(_make_placeholders(body))
                else:
                    text_lines.append("")
            while text_lines and not text_lines[-1].strip():
                text_lines.pop()
            islands.append(HostIsland("\n".join(text_lines), first_line, "recipe"))
        end = j
        while end > start + 1 and not lines[end - 1].strip():
            end -= 1
        for t in targets:
            if "%" in t or "$" in t or t.startswith("."):
                continue
            symbols.append(
                Symbol(
                    qualified_name=t,
                    name=t,
                    kind=KIND_TARGET,
                    signature=ln.strip(),
                    path="",
                    start_line=start + 1,
                    end_line=max(start + 1, end),
                )
            )
        i = j
    return islands, symbols


def jenkins_islands(text: str) -> list[HostIsland]:
    """`sh '…'`, `sh \"…\"`, `sh '''…'''`, `sh(script: \"\"\"…\"\"\")`."""
    out: list[HostIsland] = []
    pos = 0
    while True:
        m = _JENKINS_SH_RE.search(text, pos)
        if not m:
            break
        q = m.group("q")
        start = m.end()
        end = text.find(q, start)
        if end < 0:
            break
        body = text[start:end]
        line = text.count("\n", 0, start) + 1
        if len(q) == 3:
            rows = body.split("\n")
            if rows and not rows[0].strip():
                rows = rows[1:]
                line += 1
            while rows and not rows[-1].strip():
                rows.pop()
            body = "\n".join(_dedent(rows))
        out.append(HostIsland(body, line, "sh"))
        pos = end + len(q)
    return out


# --------------------------------------------------------------------------
# the units
# --------------------------------------------------------------------------


def synthetic_shell(islands: list[HostIsland], line_count: int) -> str:
    """The islands laid into a blank text at their own line numbers, so one
    bash parse of it reports the host file's lines."""
    rows = [""] * max(
        line_count, max((i.line - 1 + i.text.count("\n") + 1 for i in islands), default=0)
    )
    for isl in islands:
        for k, ln in enumerate(isl.text.split("\n")):
            idx = isl.line - 1 + k
            if 0 <= idx < len(rows):
                rows[idx] = ln
    return "\n".join(rows) + "\n"


def host_units(pf: ParsedFile) -> HostUnits | None:
    """The islands, symbols, facets and include targets of a host file, or
    None when the path is not a host. Includes resolve the way the host
    runs them: a Makefile's recipes from its own directory, a workflow's
    and a Jenkinsfile's steps from the repository root; a Dockerfile's
    paths are the image's, not the repository's, and yield none."""
    kind = host_kind(pf.path)
    if not kind:
        return None
    text = pf.source.decode("utf-8", "replace")
    lines = text.split("\n")
    symbols: list[Symbol] = []
    if kind == HOST_YAML:
        islands = yaml_islands(lines)
        symbols = yaml_symbols(pf.path, lines)
        resolve_from = posixpath.basename(pf.path)
    elif kind == HOST_DOCKER:
        islands, symbols = dockerfile_islands(lines)
        resolve_from = ""
    elif kind == HOST_MAKE:
        islands, symbols = makefile_islands(lines)
        resolve_from = pf.path
    else:
        islands = jenkins_islands(text)
        resolve_from = posixpath.basename(pf.path)
    mod = module_scope(pf.path)
    symbols = [
        Symbol(
            qualified_name=f"{mod}::{s.name}",
            name=s.name,
            kind=s.kind,
            signature=s.signature,
            path=pf.path,
            start_line=s.start_line,
            end_line=s.end_line,
            doc_comment=s.doc_comment,
            has_body=True,
        )
        for s in symbols
    ]
    units = HostUnits(kind, islands, symbols)
    if not islands:
        return units
    shell_text = synthetic_shell(islands, len(lines))
    shell_pf = parse_bash(resolve_from or pf.path, shell_text.encode())
    units.facets.extend(program_facets(shell_pf))
    found = find_islands(shell_pf)
    if found:
        pairs = island_statements(found)
        units.facets.extend(island_facets(Header(), pairs, found))
    if resolve_from:
        units.includes = include_targets(shell_pf, shell_text)
    return units


__all__ = [
    "HOST_DOCKER",
    "HOST_JENKINS",
    "HOST_LANG",
    "HOST_MAKE",
    "HOST_YAML",
    "HostIsland",
    "HostUnits",
    "KIND_JOB",
    "KIND_STAGE",
    "KIND_TARGET",
    "detect_host",
    "dockerfile_islands",
    "host_kind",
    "host_units",
    "jenkins_islands",
    "makefile_islands",
    "synthetic_shell",
    "yaml_islands",
    "yaml_symbols",
]
